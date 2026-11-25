"""Coordinator: per-device transport selection with health tracking and failover.

Every enabled transport (BLE, Wi-Fi TCP, cloud) has its own health state,
evaluated on every poll and exposed as an entity. A priority selector uses
the highest-priority transport that is not marked down; a transport is
demoted after ``LINK_FAIL_LIMIT`` consecutive failures and then only
re-probed with exponential backoff, so a flaky link cannot cause flapping.
A failure on one poll falls through to the next transport immediately, so
data keeps flowing while the primary recovers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from unofficial_pecron_api.models import Device, DeviceProperties, TslProperty

from .const import (
    CONF_DEVICE_CACHE,
    CONF_EMAIL,
    CONF_FAST_REPORT_INTERVAL,
    CONF_HOST_PREFIX,
    CONF_KEY_PREFIX,
    CONF_PASSWORD,
    CONF_PUSH_STREAM,
    CONF_REFRESH_INTERVAL,
    CONF_REGION,
    CONF_SCAN_INTERVAL_LOCAL,
    DEFAULT_FAST_REPORT_INTERVAL,
    DEFAULT_PRIORITIES,
    DEFAULT_PUSH_STREAM,
    DEFAULT_REFRESH_INTERVAL,
    DEFAULT_REGION,
    DEFAULT_SCAN_INTERVAL_LOCAL,
    DOMAIN,
    FAST_REPORT_CODE,
    FAST_REPORT_MODE,
    LINK_FAIL_LIMIT,
    LINK_PROBE_BACKOFF_MAX,
    LINK_PROBE_BACKOFF_START,
    PRIORITY_KEYS,
    STALE_POLL_LIMIT,
)
from .transport import (
    ALL_TRANSPORTS,
    TRANSPORT_BLE,
    TRANSPORT_CLOUD,
    TRANSPORT_TCP,
    Transport,
    TransportError,
)
from .transport.cloud import CloudAccount, CloudTransport
from .transport.cloud_stream import CloudStream, coerce_kv
from .tsl_map import TslMap, to_tsl_info

_LOGGER = logging.getLogger(__name__)

READ_TIMEOUT = {TRANSPORT_BLE: 50.0, TRANSPORT_TCP: 50.0, TRANSPORT_CLOUD: 30.0}


def configured_priorities(entry: ConfigEntry) -> list[str]:
    """Ordered, de-duplicated list of enabled transports from the options."""
    order: list[str] = []
    for key, default in zip(PRIORITY_KEYS, DEFAULT_PRIORITIES, strict=True):
        value = entry.options.get(key, default)
        if value in ALL_TRANSPORTS and value not in order:
            order.append(value)
    return order


@dataclass
class LinkHealth:
    """Health of one transport to one device (observable, fail-safe)."""

    name: str
    consecutive_failures: int = 0
    total_failures: int = 0
    down: bool = False
    last_error: str | None = None
    last_success: float | None = None
    next_probe: float = 0.0
    backoff: float = LINK_PROBE_BACKOFF_START

    def eligible(self, now: float) -> bool:
        return not self.down or now >= self.next_probe

    def success(self, now: float) -> None:
        if self.down:
            _LOGGER.info("Link %s recovered", self.name)
        self.consecutive_failures = 0
        self.down = False
        self.last_error = None
        self.last_success = now
        self.backoff = LINK_PROBE_BACKOFF_START

    def failure(self, now: float, error: str) -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_error = error
        if self.down:
            self.backoff = min(self.backoff * 2, LINK_PROBE_BACKOFF_MAX)
            self.next_probe = now + self.backoff
        elif self.consecutive_failures >= LINK_FAIL_LIMIT:
            _LOGGER.warning(
                "Link %s marked down after %d failures: %s",
                self.name,
                self.consecutive_failures,
                error,
            )
            self.down = True
            self.next_probe = now + self.backoff

    @property
    def state(self) -> str:
        if self.down:
            return "down"
        if self.consecutive_failures:
            return "degraded"
        if self.last_success is None:
            return "unknown"
        return "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "last_error": self.last_error,
        }


class DeviceLink:
    """All transports to one device plus the priority selector."""

    def __init__(
        self,
        device: Device,
        tsl_raw: list[dict],
        transports: list[Transport],
        tsl_map: TslMap | None = None,
    ) -> None:
        self.device = device
        self.tsl_map = tsl_map or TslMap(tsl_raw)
        self.tsl_props = [TslProperty.from_api(item) for item in tsl_raw] or None
        self.transports = transports
        self.health = {t.name: LinkHealth(t.name) for t in transports}
        self.active: str | None = None
        self.values: dict[str, Any] | None = None
        self.stale_polls = 0
        # Last values each transport delivered (for diagnostics / coverage).
        self.by_transport: dict[str, dict[str, Any]] = {}

    @property
    def device_key(self) -> str:
        return self.device.device_key

    async def poll(self) -> dict[str, Any] | None:
        now = time.monotonic()
        for transport in self.transports:
            health = self.health[transport.name]
            if not health.eligible(now):
                continue
            try:
                values = await asyncio.wait_for(
                    transport.read(), READ_TIMEOUT.get(transport.name, 30.0)
                )
            except (TransportError, TimeoutError, OSError) as err:
                health.failure(time.monotonic(), str(err) or type(err).__name__)
                _LOGGER.debug("%s via %s failed: %s", self.device.device_name, transport.name, err)
                continue
            health.success(time.monotonic())
            self.by_transport[transport.name] = {
                **self.by_transport.get(transport.name, {}),
                **values,
            }
            if self.active != transport.name:
                _LOGGER.info("%s: using %s", self.device.device_name, transport.name)
            self.active = transport.name
            # Links report different subsets (BLE omits some settings the
            # cloud has); keep the last known value of every property.
            self.values = {**(self.values or {}), **values}
            self.stale_polls = 0
            return self.values
        self.active = None
        self.stale_polls += 1
        return None

    async def write(self, code: str, value: Any) -> str:
        """Write via the active transport first, then any other healthy one."""
        order = sorted(
            self.transports, key=lambda t: (t.name != self.active, self.health[t.name].down)
        )
        errors = []
        for transport in order:
            if self.health[transport.name].down:
                continue
            try:
                await transport.write(code, value)
                return transport.name
            except TransportError as err:
                errors.append(f"{transport.name}: {err}")
        raise TransportError("; ".join(errors) or "no usable transport")

    def snapshot(self) -> dict[str, Any] | None:
        if self.values is None or self.stale_polls > STALE_POLL_LIMIT:
            return None
        props = DeviceProperties.from_api(to_tsl_info(self.values, self.tsl_map))
        return {
            "device": self.device,
            "properties": props,
            "tsl": self.tsl_props,
            "values": self.values,
            "transport": self.active,
        }

    async def close(self) -> None:
        for transport in self.transports:
            try:
                await transport.close()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("closing %s: %s", transport.name, err)


def _device_from_cache(device_key: str, cached: dict) -> Device:
    return Device(
        device_name=cached.get("name", device_key),
        product_key=cached.get("product_key", ""),
        device_key=device_key,
        product_name=cached.get("product_name", ""),
        online=True,
        protocol="",
    )


class PecronDataUpdateCoordinator(DataUpdateCoordinator):
    """Owns the cloud account and one DeviceLink per station."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        self.entry = config_entry
        self.options_snapshot = dict(config_entry.options)
        self.priorities = configured_priorities(config_entry)
        self.local_interval = int(
            config_entry.options.get(CONF_SCAN_INTERVAL_LOCAL, DEFAULT_SCAN_INTERVAL_LOCAL)
        )
        self.cloud_interval = int(
            config_entry.options.get(
                CONF_REFRESH_INTERVAL,
                config_entry.data.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL),
            )
        )
        uses_local = any(p in (TRANSPORT_BLE, TRANSPORT_TCP) for p in self.priorities)
        self.push_enabled = TRANSPORT_CLOUD in self.priorities and bool(
            config_entry.options.get(CONF_PUSH_STREAM, DEFAULT_PUSH_STREAM)
        )
        self.fast_report_interval = int(
            config_entry.options.get(CONF_FAST_REPORT_INTERVAL, DEFAULT_FAST_REPORT_INTERVAL)
        )
        self.stream: CloudStream | None = None
        self._fast_report_unsub = None
        # Reading pushed values costs nothing, so poll at the local rate.
        fast = uses_local or self.push_enabled
        interval = self.local_interval if fast else self.cloud_interval
        self.account = CloudAccount(
            hass,
            config_entry.data[CONF_EMAIL],
            config_entry.data[CONF_PASSWORD],
            config_entry.data.get(CONF_REGION, DEFAULT_REGION),
        )
        self.links: dict[str, DeviceLink] = {}
        self.known_device_keys: set[str] = set()
        self._bootstrapped = False
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )

    # -- compatibility with platform code ------------------------------------

    @property
    def api(self):
        return self.account.api

    @property
    def devices(self) -> list[Device]:
        return [link.device for link in self.links.values()]

    # -- bootstrap ------------------------------------------------------------

    async def _bootstrap(self) -> None:
        cache: dict[str, dict] = {
            k: dict(v) for k, v in self.entry.data.get(CONF_DEVICE_CACHE, {}).items()
        }
        changed = False
        wants_local = any(p in (TRANSPORT_BLE, TRANSPORT_TCP) for p in self.priorities)
        devices: list[Device]
        try:
            devices = await self.account.async_login()
        except Exception as err:
            if not cache:
                raise UpdateFailed(
                    f"Cannot reach the Pecron cloud and no cached devices: {err}"
                ) from err
            _LOGGER.warning("Pecron cloud unavailable (%s); starting from cached device data", err)
            devices = [_device_from_cache(dk, c) for dk, c in cache.items()]
        else:
            for device in devices:
                entry = cache.setdefault(device.device_key, {})
                meta = {
                    "name": device.device_name,
                    "product_key": device.product_key,
                    "product_name": device.product_name,
                }
                if any(entry.get(k) != v for k, v in meta.items()):
                    entry.update(meta)
                    changed = True
                if not entry.get("tsl"):
                    try:
                        entry["tsl"] = await self.account.async_raw_tsl(device)
                        changed = True
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning("Could not fetch TSL for %s: %s", device.device_name, err)
                manual_key = self.entry.options.get(f"{CONF_KEY_PREFIX}{device.device_key}")
                if wants_local and not entry.get("auth_key") and not manual_key:
                    try:
                        # No cached key at all: regeneration is allowed as a
                        # one-time fallback (some models reject getAuthKey).
                        entry["auth_key"] = await self.account.async_auth_key(
                            device, allow_regenerate=True
                        )
                        changed = True
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning(
                            "No local auth key for %s (%s); local transports disabled for it",
                            device.device_name,
                            err,
                        )

        for device in devices:
            self.links[device.device_key] = self._build_link(
                device, cache.get(device.device_key, {})
            )
        if changed:
            self._save_cache(cache)
        self._bootstrapped = True
        if self.push_enabled:
            await self._start_stream()

    def _build_link(self, device: Device, cached: dict) -> DeviceLink:
        transports: list[Transport] = []
        auth_key = self.entry.options.get(f"{CONF_KEY_PREFIX}{device.device_key}") or cached.get(
            "auth_key"
        )
        tsl_map = TslMap(cached.get("tsl", []))
        for name in self.priorities:
            if name == TRANSPORT_CLOUD:
                transports.append(
                    CloudTransport(
                        self.account,
                        device,
                        self.cloud_interval,
                        push_fresh=self.cloud_interval if self.push_enabled else 0.0,
                    )
                )
            elif name in (TRANSPORT_TCP, TRANSPORT_BLE) and not auth_key:
                # Keep the link visible (as failing, with the reason) rather than
                # silently dropping it.
                transports.append(
                    _UnavailableTransport(name, "no local key for this station (see log)")
                )
            elif name == TRANSPORT_TCP:
                host = self.entry.options.get(
                    f"{CONF_HOST_PREFIX}{device.device_key}"
                ) or cached.get("host")
                transports.append(_DiscoveringTcp(self, device.device_key, host, auth_key, tsl_map))
            elif name == TRANSPORT_BLE:
                from .transport.ble import BleTransport

                transports.append(
                    BleTransport(
                        self.hass, cached.get("ble_address"), device.device_key, auth_key, tsl_map
                    )
                )
        return DeviceLink(device, cached.get("tsl", []), transports, tsl_map)

    def _save_cache(self, cache: dict[str, dict]) -> None:
        data = dict(self.entry.data)
        data[CONF_DEVICE_CACHE] = cache
        self.hass.config_entries.async_update_entry(self.entry, data=data)

    def remember(self, device_key: str, **values: Any) -> None:
        """Persist discovered per-device details (host, BLE address)."""
        cache = {k: dict(v) for k, v in self.entry.data.get(CONF_DEVICE_CACHE, {}).items()}
        entry = cache.setdefault(device_key, {})
        if all(entry.get(k) == v for k, v in values.items()):
            return
        entry.update(values)
        self._save_cache(cache)

    # -- polling ----------------------------------------------------------------

    async def _async_update_data(self) -> dict:
        if not self._bootstrapped:
            await self._bootstrap()
        if not self.links:
            return {}
        links = list(self.links.values())
        await asyncio.gather(*(link.poll() for link in links))
        data = {}
        for link in links:
            snap = link.snapshot()
            if snap is not None:
                data[link.device_key] = snap
            if link.active == TRANSPORT_BLE:
                ble = next(t for t in link.transports if t.name == TRANSPORT_BLE)
                address = getattr(ble, "address", None)
                if address:
                    self.remember(link.device_key, ble_address=address)
        if not data:
            errors = {
                link.device.device_name: {n: h.last_error for n, h in link.health.items()}
                for link in links
            }
            raise UpdateFailed(f"No transport could reach any station: {errors}")
        self.known_device_keys.update(data)
        return data

    async def async_set_property(self, device_key: str, code: str, value: Any) -> str:
        """Write one property; returns the transport used. Raises TransportError."""
        link = self.links.get(device_key)
        if link is None:
            raise TransportError(f"unknown device {device_key}")
        used = await link.write(code, value)
        await self.async_request_refresh()
        return used

    def get_new_devices(self, existing_keys: set[str]) -> dict:
        if not self.data:
            return {}
        return {key: value for key, value in self.data.items() if key not in existing_keys}

    # -- cloud push stream (pecron-monitor's fast path) -------------------------

    async def _start_stream(self) -> None:
        stream = CloudStream(
            self.account.region,
            self.account.token,
            lambda dk, kv: self.hass.loop.call_soon_threadsafe(self._on_push, dk, kv),
        )
        for link in self.links.values():
            stream.add_device(link.device.product_key, link.device_key)
        try:
            await self.hass.async_add_executor_job(stream.start)
        except Exception as err:  # noqa: BLE001 - REST polling still works
            _LOGGER.warning("Pecron push stream unavailable: %s", err)
            return
        self.stream = stream
        if self.fast_report_interval > 0:
            from homeassistant.helpers.event import async_track_time_interval

            self._fast_report_unsub = async_track_time_interval(
                self.hass, self._request_fast_reports, timedelta(seconds=self.fast_report_interval)
            )
            self.hass.async_create_task(self._request_fast_reports())

    async def _request_fast_reports(self, _now=None) -> None:
        """Ask each station to report every few seconds (1 cloud request each)."""
        for link in self.links.values():
            if FAST_REPORT_CODE not in link.tsl_map.by_code and link.tsl_map.by_code:
                continue
            try:
                await self.account.async_call(
                    "set_device_property", link.device, {FAST_REPORT_CODE: FAST_REPORT_MODE}
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("high-frequency request for %s failed: %s", link.device_key, err)

    @callback
    def _on_push(self, device_key: str, kv: dict[str, Any]) -> None:
        link = self.links.get(device_key)
        if link is None:
            return
        cloud = next((t for t in link.transports if t.name == TRANSPORT_CLOUD), None)
        if not isinstance(cloud, CloudTransport):
            return
        types = {code: e.data_type for code, e in link.tsl_map.by_code.items()}
        pushed = coerce_kv(kv, types)
        link.by_transport["cloud_push"] = {**link.by_transport.get("cloud_push", {}), **pushed}
        values = cloud.apply_push(pushed)
        # Only take over the entities if no higher-priority link is delivering.
        if link.active not in (None, TRANSPORT_CLOUD):
            return
        link.values = {**(link.values or {}), **values}
        link.active = TRANSPORT_CLOUD
        link.stale_polls = 0
        link.health[TRANSPORT_CLOUD].success(time.monotonic())
        snap = link.snapshot()
        if snap is None:
            return
        data = dict(self.data or {})
        data[device_key] = snap
        # Update entities without resetting the poll timer.
        self.data = data
        self.async_update_listeners()

    async def async_shutdown(self) -> None:
        if self._fast_report_unsub is not None:
            self._fast_report_unsub()
            self._fast_report_unsub = None
        if self.stream is not None:
            await self.hass.async_add_executor_job(self.stream.stop)
            self.stream = None
        await super().async_shutdown()
        for link in self.links.values():
            await link.close()
        await self.account.async_close()


def _make_tcp_base():
    from .transport.tcp import TcpTransport

    return TcpTransport


class _DiscoveringTcp(Transport):
    """TCP transport that finds the station on the LAN when no host is known."""

    name = TRANSPORT_TCP

    def __init__(
        self,
        coordinator: PecronDataUpdateCoordinator,
        device_key: str,
        host: str | None,
        auth_key: str,
        tsl_map: TslMap,
    ) -> None:
        self.coordinator = coordinator
        self.device_key = device_key
        self.auth_key = auth_key
        self.tsl_map = tsl_map
        self._inner = _make_tcp_base()(host, device_key, auth_key, tsl_map) if host else None
        self._next_scan = 0.0
        self._scan: asyncio.Task | None = None

    async def _resolve(self) -> None:
        if self._inner is not None:
            return
        if self._scan is None or self._scan.done():
            if self._scan is not None and self._scan.done():
                host = (
                    None
                    if self._scan.cancelled() or self._scan.exception()
                    else self._scan.result()
                )
                self._scan = None
                if host:
                    self.coordinator.remember(self.device_key, host=host)
                    self._inner = _make_tcp_base()(
                        host, self.device_key, self.auth_key, self.tsl_map
                    )
                    return
                self._next_scan = time.monotonic() + LINK_PROBE_BACKOFF_MAX
            if time.monotonic() >= self._next_scan:
                self._scan = self.coordinator.hass.async_create_background_task(
                    self._discover(), f"pecron_lan_scan_{self.device_key}"
                )
        raise TransportError(
            "station not located on the LAN yet (searching, or set its IP in the options)"
        )

    async def _discover(self) -> str | None:
        from homeassistant.components import network

        from .transport.tcp import discover_host

        source_ip = await network.async_get_source_ip(self.coordinator.hass)
        return await discover_host(source_ip, self.auth_key)

    async def read(self) -> dict[str, Any]:
        await self._resolve()
        assert self._inner is not None
        return await self._inner.read()

    async def write(self, code: str, value: Any) -> None:
        await self._resolve()
        assert self._inner is not None
        await self._inner.write(code, value)

    async def close(self) -> None:
        if self._scan is not None:
            self._scan.cancel()
        if self._inner is not None:
            await self._inner.close()


class _UnavailableTransport(Transport):
    """Placeholder for an enabled transport that cannot work (e.g. no local key)."""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason

    async def read(self) -> dict[str, Any]:
        raise TransportError(self.reason)

    async def write(self, code: str, value: Any) -> None:
        raise TransportError(self.reason)
