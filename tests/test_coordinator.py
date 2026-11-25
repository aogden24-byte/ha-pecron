"""Coordinator bootstrap, caching and end-to-end polling with a simulated station."""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from unofficial_pecron_api.models import Device

from custom_components.pecron.const import CONF_DEVICE_CACHE
from custom_components.pecron.coordinator import PecronDataUpdateCoordinator

from .test_local_transport import AUTH_B64, RAW_TSL, FakeStation, fast_timeouts  # noqa: F401

DEVICE = Device(
    device_name="F5000LFP_B639",
    product_key="p11vwW",
    device_key="F8D8117CB639",
    product_name="F5000LFP",
    online=True,
    protocol="",
)


def make_hass() -> MagicMock:
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda f, *a: f(*a))
    hass.config_entries.async_update_entry = MagicMock(
        side_effect=lambda entry, data=None, **kw: entry.data.update(data or {})
    )
    return hass


def make_entry(options: dict, data_extra: dict | None = None) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {"email": "a@b.c", "password": "pw", "region": "US", **(data_extra or {})}
    entry.options = options
    return entry


def fake_api(values_raw: list[dict]) -> MagicMock:
    api = MagicMock()
    api.get_devices.return_value = [DEVICE]

    def request(method, path, params=None, form_data=None, json_body=None):
        if path.endswith("productTSL"):
            return {"tslJson": {"properties": RAW_TSL}}
        if path.endswith("getAuthKey"):
            return {"authKey": AUTH_B64}
        raise AssertionError(path)

    api._request.side_effect = request
    api.get_device_properties.return_value = MagicMock(raw=values_raw)
    return api


def build(hass, entry) -> PecronDataUpdateCoordinator:
    with patch("homeassistant.helpers.frame.report_usage"):
        return PecronDataUpdateCoordinator(hass, entry)


@pytest.mark.asyncio
async def test_cloud_only_bootstrap_caches_tsl_but_not_key() -> None:
    hass = make_hass()
    entry = make_entry(
        {
            "priority_1": "cloud",
            "priority_2": "none",
            "priority_3": "none",
            "push_stream": False,
        }
    )
    api = fake_api(
        [{"resourceCode": "battery_percentage", "resourceValce": "74", "dataType": "INT"}]
    )
    with patch("custom_components.pecron.transport.cloud.PecronAPI", return_value=api):
        coordinator = build(hass, entry)
        data = await coordinator._async_update_data()
    assert data["F8D8117CB639"]["properties"].battery_percentage == 74
    assert data["F8D8117CB639"]["transport"] == "cloud"
    cache = entry.data[CONF_DEVICE_CACHE]["F8D8117CB639"]
    assert cache["tsl"] == RAW_TSL
    assert "auth_key" not in cache  # cloud-only: never asks for the local key
    assert coordinator.update_interval.total_seconds() == 300


@pytest.mark.asyncio
async def test_tcp_first_with_cloud_fallback(fast_timeouts, monkeypatch) -> None:  # noqa: F811
    station = FakeStation()
    port = await station.start()
    from custom_components.pecron.transport import tcp

    monkeypatch.setattr(tcp, "TCP_PORT", port)
    hass = make_hass()
    entry = make_entry(
        {
            "priority_1": "tcp",
            "priority_2": "cloud",
            "priority_3": "none",
            "host_F8D8117CB639": "127.0.0.1",
        }
    )
    api = fake_api(
        [{"resourceCode": "battery_percentage", "resourceValce": "70", "dataType": "INT"}]
    )
    try:
        with patch("custom_components.pecron.transport.cloud.PecronAPI", return_value=api):
            coordinator = build(hass, entry)
            data = await coordinator._async_update_data()
            assert data["F8D8117CB639"]["transport"] == "tcp"
            assert data["F8D8117CB639"]["properties"].battery_percentage == 78
            assert entry.data[CONF_DEVICE_CACHE]["F8D8117CB639"]["auth_key"] == AUTH_B64

            await station.stop()  # Wi-Fi gone -> same poll falls back to the cloud
            data = await coordinator._async_update_data()
            assert data["F8D8117CB639"]["transport"] == "cloud"
            assert data["F8D8117CB639"]["properties"].battery_percentage == 70
            await coordinator.links["F8D8117CB639"].close()
    finally:
        if station.server.is_serving():
            await station.stop()


@pytest.mark.asyncio
async def test_offline_start_from_cache(fast_timeouts, monkeypatch) -> None:  # noqa: F811
    station = FakeStation()
    port = await station.start()
    from custom_components.pecron.transport import tcp

    monkeypatch.setattr(tcp, "TCP_PORT", port)
    cache = {
        "F8D8117CB639": {
            "name": "F5000LFP_B639",
            "product_key": "p11vwW",
            "product_name": "F5000LFP",
            "tsl": RAW_TSL,
            "auth_key": AUTH_B64,
            "host": "127.0.0.1",
        }
    }
    hass = make_hass()
    entry = make_entry(
        {"priority_1": "tcp", "priority_2": "none", "priority_3": "none"},
        {CONF_DEVICE_CACHE: cache},
    )
    broken = MagicMock()
    broken.login.side_effect = OSError("no internet")
    try:
        with patch("custom_components.pecron.transport.cloud.PecronAPI", return_value=broken):
            coordinator = build(hass, entry)
            data = await coordinator._async_update_data()
            await coordinator.links["F8D8117CB639"].close()
    finally:
        await station.stop()
    assert data["F8D8117CB639"]["transport"] == "tcp"
    assert data["F8D8117CB639"]["device"].device_name == "F5000LFP_B639"


def test_auth_key_is_base64() -> None:
    assert len(base64.b64decode(AUTH_B64)) == 16


@pytest.mark.asyncio
async def test_missing_key_keeps_link_visible_and_uses_cloud() -> None:
    from unofficial_pecron_api.exceptions import PecronAPIError

    hass = make_hass()
    entry = make_entry({"priority_1": "tcp", "priority_2": "cloud", "priority_3": "none"})
    api = fake_api(
        [{"resourceCode": "battery_percentage", "resourceValce": "80", "dataType": "INT"}]
    )

    def request(method, path, params=None, form_data=None, json_body=None):
        if path.endswith("productTSL"):
            return {"tslJson": {"properties": RAW_TSL}}
        raise PecronAPIError("unsupported", code=5000)

    api._request.side_effect = request
    with patch("custom_components.pecron.transport.cloud.PecronAPI", return_value=api):
        coordinator = build(hass, entry)
        data = await coordinator._async_update_data()
    link = coordinator.links["F8D8117CB639"]
    assert [t.name for t in link.transports] == ["tcp", "cloud"]
    assert data["F8D8117CB639"]["transport"] == "cloud"
    assert "no local key" in link.health["tcp"].last_error
    paths = [c.args[1] for c in api._request.call_args_list]
    assert any(p.endswith("regenerateAuthKey") for p in paths)  # one-time fallback tried


@pytest.mark.asyncio
async def test_push_stream_updates_entities_without_rest_calls() -> None:
    hass = make_hass()
    entry = make_entry(
        {
            "priority_1": "cloud",
            "priority_2": "none",
            "priority_3": "none",
            "fast_report_interval": 0,
        }
    )
    api = fake_api(
        [{"resourceCode": "battery_percentage", "resourceValce": "74", "dataType": "INT"}]
    )
    with patch("custom_components.pecron.transport.cloud.PecronAPI", return_value=api):
        coordinator = build(hass, entry)
        coordinator.async_update_listeners = MagicMock()
        await coordinator._async_update_data()
        assert coordinator.update_interval.total_seconds() == 15  # poll fast: push is free
        assert coordinator.stream is not None
        coordinator.data = {}
        coordinator._on_push("F8D8117CB639", {"battery_percentage": "61"})
        calls = api.get_device_properties.call_count
        data = await coordinator._async_update_data()
    assert coordinator.data["F8D8117CB639"]["properties"].battery_percentage == 61
    coordinator.async_update_listeners.assert_called_once()
    assert data["F8D8117CB639"]["properties"].battery_percentage == 61
    assert api.get_device_properties.call_count == calls  # fresh push: no REST read
