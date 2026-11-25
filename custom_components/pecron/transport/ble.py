"""BLE transport through Home Assistant's Bluetooth stack (local adapter or ESPHome proxy)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..tsl_map import TslMap
from . import TRANSPORT_BLE, TransportError
from .local import LocalSession

if TYPE_CHECKING:
    from bleak import BleakClient
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CHAR_UUID = "00009c40-0000-1000-8000-00805f9b34fb"
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"
NAME_PREFIX = "QUEC_BLE"
QUECTEL_MANUFACTURER_ID = 0x5551
CCCD_INDICATE = b"\x02\x00"


def matches_device(name: str | None, manufacturer_data: dict[int, bytes], device_key: str) -> bool:
    """True if an advertisement belongs to the Pecron station with ``device_key``."""
    suffix = device_key[-4:].upper()
    if name and name.upper().startswith(NAME_PREFIX) and name.upper().endswith(suffix):
        return True
    mfr = manufacturer_data.get(QUECTEL_MANUFACTURER_ID)
    if mfr:
        try:
            if bytes.fromhex(device_key) in mfr or device_key.upper().encode() in mfr.upper():
                return True
        except ValueError:
            pass
    return False


def find_address(hass: HomeAssistant, device_key: str) -> str | None:
    """Look for the station among current BLE advertisements."""
    from homeassistant.components import bluetooth

    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        if matches_device(info.name, info.manufacturer_data, device_key):
            return info.address
    return None


class BleTransport(LocalSession):
    """Encrypted TTLV over GATT characteristic 0x9C40 (write with response + indications).

    The connection is kept open between polls: every new BLE session is a
    full handshake, and at least one model is reported to switch its AC
    output off when a session starts, so reconnecting each poll is avoided.
    """

    name = TRANSPORT_BLE

    def __init__(
        self,
        hass: HomeAssistant,
        address: str | None,
        device_key: str,
        auth_key_b64: str,
        tsl: TslMap,
    ) -> None:
        super().__init__(device_key, auth_key_b64, tsl)
        self.hass = hass
        self.address = address
        self._client: BleakClient | None = None

    @property
    def _link_open(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def _open(self) -> None:
        from homeassistant.components import bluetooth

        if not self.address:
            self.address = find_address(self.hass, self.device_key)
        if not self.address:
            raise TransportError("station not seen over Bluetooth (no adapter or proxy in range)")
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise TransportError(f"{self.address} not currently reachable over Bluetooth")
        from bleak.exc import BleakError
        from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                f"Pecron {self.device_key[-4:]}",
                disconnected_callback=lambda _c: self._on_link_lost(),
                max_attempts=2,
            )
            self._client = client
            await client.start_notify(CHAR_UUID, lambda _h, data: self._on_bytes(bytes(data)))
            await self._force_indications(client)
        except (BleakError, TimeoutError, OSError) as err:
            await self._close_link()
            raise TransportError(f"BLE connect failed: {err}") from err

    async def _force_indications(self, client: BleakClient) -> None:
        """The station accepts writes only from a client subscribed for indications.

        bleak may enable notifications instead when the characteristic offers
        both, so rewrite the CCCD to 0x0002 (indicate). Works through ESPHome
        proxies; some BlueZ host adapters refuse direct CCCD writes.
        """
        char = client.services.get_characteristic(CHAR_UUID)
        if char is None:
            raise TransportError("characteristic 0x9C40 not found")
        if "indicate" not in char.properties:
            return
        descriptor = char.get_descriptor(CCCD_UUID)
        if descriptor is None:
            return
        try:
            await client.write_gatt_descriptor(descriptor.handle, CCCD_INDICATE)
        except Exception as err:  # noqa: BLE001 - backend-specific errors
            _LOGGER.debug("CCCD rewrite to indications refused: %s", err)

    async def _send(self, data: bytes) -> None:
        if self._client is None:
            raise TransportError("not connected")
        from bleak.exc import BleakError

        try:
            await self._client.write_gatt_char(CHAR_UUID, data, response=True)
        except BleakError as err:
            raise TransportError(f"BLE write failed: {err}") from err

    async def _close_link(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("BLE disconnect: %s", err)
