"""BLE transport against a simulated GATT station (no real Bluetooth stack)."""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from custom_components.pecron import ttlv
from custom_components.pecron.transport.ble import CCCD_INDICATE, BleTransport, matches_device
from custom_components.pecron.tsl_map import TslMap

from .test_local_transport import AUTH_B64, AUTH_KEY, RANDOM, RAW_TSL, fast_timeouts, sample_report  # noqa: F401


def test_matches_device_by_name_or_manufacturer_data() -> None:
    assert matches_device("QUEC_BLE_B639", {}, "F8D8117CB639")
    assert not matches_device("QUEC_BLE_0000", {}, "F8D8117CB639")
    assert matches_device(None, {0x5551: bytes.fromhex("00F8D8117CB63900")}, "F8D8117CB639")
    assert not matches_device("Other", {0x1234: b"\xf8\xd8"}, "F8D8117CB639")


class FakeGattStation:
    """Answers TTLV frames written to 0x9C40 by 'indicating' replies in small chunks."""

    def __init__(self) -> None:
        self.notify = None
        self.reassembler = ttlv.FrameReassembler()
        self.iv = ttlv.make_iv(RANDOM)
        self.is_connected = True
        self.cccd_writes: list[bytes] = []
        descriptor = MagicMock(handle=0x13)
        char = MagicMock(properties=["write", "indicate", "notify"])
        char.get_descriptor.return_value = descriptor
        self.services = MagicMock()
        self.services.get_characteristic.return_value = char

    async def start_notify(self, uuid, callback):
        self.notify = callback

    async def write_gatt_descriptor(self, handle, data):
        self.cccd_writes.append(data)

    async def disconnect(self):
        self.is_connected = False

    def _indicate(self, frame: bytes) -> None:
        for i in range(0, len(frame), 20):  # default-MTU sized indications
            self.notify(None, bytearray(frame[i : i + 20]))

    async def write_gatt_char(self, uuid, data, response=True):
        for raw in self.reassembler.feed(bytes(data)):
            frame = ttlv.parse_frame(raw)
            if frame.cmd == ttlv.CMD_RANDOM_REQ:
                self._indicate(
                    ttlv.build_frame(
                        ttlv.CMD_RANDOM_RESP, ttlv.encode_bytes_field(1, RANDOM.encode())
                    )
                )
            elif frame.cmd == ttlv.CMD_LOGIN_REQ:
                self._indicate(ttlv.build_frame(ttlv.CMD_LOGIN_RESP, ttlv.encode_num_field(3, 0)))
            elif frame.cmd == ttlv.CMD_READ:
                self._indicate(
                    ttlv.build_frame(
                        ttlv.CMD_REPORT, ttlv.encrypt(AUTH_KEY, self.iv, sample_report())
                    )
                )
            elif frame.cmd == ttlv.CMD_WRITE:
                self._indicate(ttlv.build_frame(ttlv.CMD_ACK, b""))


@pytest.fixture
def fake_bluetooth():
    module = types.ModuleType("homeassistant.components.bluetooth")
    module.async_ble_device_from_address = MagicMock(return_value=object())
    module.async_discovered_service_info = MagicMock(return_value=[])
    with patch.dict(sys.modules, {"homeassistant.components.bluetooth": module}):
        import homeassistant.components as components

        original = getattr(components, "bluetooth", None)
        components.bluetooth = module
        try:
            yield module
        finally:
            if original is None:
                del components.bluetooth
            else:
                components.bluetooth = original


@pytest.mark.asyncio
async def test_ble_session_read_write_and_persistence(fake_bluetooth, fast_timeouts) -> None:  # noqa: F811
    station = FakeGattStation()

    async def establish(cls, device, name, disconnected_callback=None, max_attempts=2):
        return station

    with patch("bleak_retry_connector.establish_connection", side_effect=establish) as est:
        transport = BleTransport(
            MagicMock(), "AA:BB:CC:DD:EE:FF", "F8D8117CB639", AUTH_B64, TslMap(RAW_TSL)
        )
        values = await transport.read()
        again = await transport.read()
        await transport.write("ac_switch_hm", True)
        await transport.close()

    assert values["battery_percentage"] == 78
    assert again["host_packet_data_jdb"]["host_packet_current"] == 5.12
    assert est.call_count == 1  # one BLE session reused across polls and writes
    assert station.cccd_writes == [CCCD_INDICATE]
    assert not station.is_connected


@pytest.mark.asyncio
async def test_ble_unseen_station_fails_fast(fake_bluetooth) -> None:
    from custom_components.pecron.transport import TransportError

    transport = BleTransport(MagicMock(), None, "F8D8117CB639", AUTH_B64, TslMap(RAW_TSL))
    with pytest.raises(TransportError, match="not seen over Bluetooth"):
        await transport.read()
