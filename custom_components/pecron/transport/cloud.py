"""Pecron (Quectel) cloud account and cloud transport."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from unofficial_pecron_api import PecronAPI
from unofficial_pecron_api.exceptions import PecronAPIError

from ..tsl_map import from_tsl_info
from . import TRANSPORT_CLOUD, Transport, TransportError

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from unofficial_pecron_api.models import Device

_LOGGER = logging.getLogger(__name__)

AUTH_ERROR_MARKERS = ("authentication", "401", "unauthorized", "5032", "token")


def is_auth_error(err: Exception) -> bool:
    text = str(err).lower()
    return any(marker in text for marker in AUTH_ERROR_MARKERS)


class CloudAccount:
    """Thread-safe-by-executor wrapper around the cloud API with token refresh."""

    def __init__(self, hass: HomeAssistant, email: str, password: str, region: str) -> None:
        self.hass = hass
        self.email = email
        self.password = password
        self.region = region
        self.api: PecronAPI | None = None
        self.devices: list[Device] = []

    def _login(self) -> None:
        api = PecronAPI(region=self.region)
        api.login(self.email, self.password)
        self.api = api
        self.devices = api.get_devices()

    def _run(self, func_name: str, *args: Any) -> Any:
        """Call an API method, logging in again once if the token has expired."""
        for attempt in range(2):
            if self.api is None:
                self._login()
            assert self.api is not None
            try:
                return getattr(self.api, func_name)(*args)
            except Exception as err:
                if attempt == 0 and is_auth_error(err):
                    _LOGGER.warning("Pecron cloud token rejected (%s); logging in again", err)
                    self.api = None
                    continue
                raise
        raise RuntimeError("unreachable")

    async def async_login(self) -> list[Device]:
        await self.hass.async_add_executor_job(self._login)
        return self.devices

    async def async_call(self, func_name: str, *args: Any) -> Any:
        return await self.hass.async_add_executor_job(self._run, func_name, *args)

    # -- raw endpoints the library does not wrap ---------------------------

    def _raw_tsl(self, product_key: str) -> list[dict]:
        assert self.api is not None
        result = self.api._request(  # noqa: SLF001 - endpoint not exposed with ids
            "GET", "/v2/binding/enduserapi/productTSL", params={"pk": product_key}
        )
        if isinstance(result, dict):
            tsl_json = result.get("tslJson")
            if isinstance(tsl_json, str):
                tsl_json = json.loads(tsl_json)
            if isinstance(tsl_json, dict):
                return list(tsl_json.get("properties", []))
            return list(result.get("properties", []))
        if isinstance(result, list):
            return result
        return []

    def _auth_key(self, product_key: str, device_key: str, allow_regenerate: bool = False) -> str:
        """Fetch the device's local auth key.

        ``getAuthKey`` only reads the current key. Some models/accounts (the
        F5000LFP among them) reject it, and only ``regenerateAuthKey`` works,
        which issues a new key and invalidates any copy another local tool
        holds. Regeneration is therefore only allowed when the caller has no
        cached key at all; the result is cached and reused forever after.
        """
        assert self.api is not None
        endpoints = ["getAuthKey"] + (["regenerateAuthKey"] if allow_regenerate else [])
        last_error: Exception | None = None
        for endpoint in endpoints:
            try:
                result = self.api._request(  # noqa: SLF001
                    "POST",
                    f"/v2/binding/enduserapi/{endpoint}",
                    form_data={"pk": product_key, "dk": device_key},
                )
            except Exception as err:  # noqa: BLE001 - HTTP errors are not PecronAPIError
                if is_auth_error(err):
                    raise
                last_error = err
                _LOGGER.warning("%s failed for %s: %s", endpoint, device_key, err)
                continue
            key = result.get("authKey") if isinstance(result, dict) else None
            if key:
                if endpoint == "regenerateAuthKey":
                    _LOGGER.warning(
                        "getAuthKey is not supported for %s; generated a new local key. "
                        "Any other local tool using the old key (pecron-monitor, other "
                        "Pecron integrations) must be re-set up or removed.",
                        device_key,
                    )
                return str(key)
            _LOGGER.warning("%s returned no key for %s: %r", endpoint, device_key, result)
            last_error = PecronAPIError(f"{endpoint} returned no key")
        raise last_error or PecronAPIError("no auth key endpoint succeeded")

    async def async_raw_tsl(self, device: Device) -> list[dict]:
        return await self.hass.async_add_executor_job(self._run_raw, "_raw_tsl", device.product_key)

    async def async_auth_key(self, device: Device, allow_regenerate: bool = False) -> str:
        return await self.hass.async_add_executor_job(
            self._run_raw, "_auth_key", device.product_key, device.device_key, allow_regenerate
        )

    def _run_raw(self, method: str, *args: Any) -> Any:
        for attempt in range(2):
            if self.api is None:
                self._login()
            try:
                return getattr(self, method)(*args)
            except Exception as err:
                if attempt == 0 and is_auth_error(err):
                    self.api = None
                    continue
                raise
        raise RuntimeError("unreachable")

    async def async_close(self) -> None:
        if self.api is not None:
            await self.hass.async_add_executor_job(self.api.close)
            self.api = None


class CloudTransport(Transport):
    """Reads through the cloud, at most once per ``min_interval`` seconds (account quota)."""

    name = TRANSPORT_CLOUD

    def __init__(self, account: CloudAccount, device: Device, min_interval: float) -> None:
        self.account = account
        self.device = device
        self.min_interval = min_interval
        self._cache: dict[str, Any] | None = None
        self._cache_time = 0.0

    async def read(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._cache is not None and now - self._cache_time < self.min_interval:
            return self._cache
        try:
            props = await self.account.async_call("get_device_properties", self.device)
        except Exception as err:
            raise TransportError(f"cloud read failed: {err}") from err
        values = from_tsl_info(getattr(props, "raw", []) or [])
        if not values:
            raise TransportError("cloud returned no properties (device offline?)")
        self._cache, self._cache_time = values, now
        return values

    async def write(self, code: str, value: Any) -> None:
        try:
            result = await self.account.async_call(
                "set_device_property", self.device, {code: value}
            )
        except Exception as err:
            raise TransportError(f"cloud write failed: {err}") from err
        if not getattr(result, "success", False):
            message = getattr(result, "error_message", None) or getattr(result, "message", None)
            raise TransportError(f"cloud rejected write: {message or 'unknown error'}")
        self._cache = None  # force a fresh read
