"""Named measurement sensors and TSL-driven settings."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from unofficial_pecron_api.models import TslProperty

from custom_components.pecron.tsl_entities import (
    MeasurementSensor,
    SettingNumber,
    SettingSelect,
    SettingSwitch,
    tracker,
)

TSL = [
    TslProperty.from_api(
        {
            "code": "noastime_io",
            "name": "Auto off",
            "dataType": "ENUM",
            "subType": "RW",
            "specs": [
                {"value": "1", "name": "Never"},
                {"value": "2", "name": "3 min"},
                {"value": "3", "name": "1 h"},
                {"value": "4", "name": "12 h"},
            ],
        }
    ),
    TslProperty.from_api(
        {
            "code": "upper_discharge_limit_us",
            "name": "Upper",
            "dataType": "INT",
            "subType": "RW",
            "specs": {"min": "50", "max": "100", "step": "1", "unit": "%"},
        }
    ),
    TslProperty.from_api(
        {"code": "eco_quite_mode_as", "name": "Eco", "dataType": "BOOL", "subType": "RW"}
    ),
    TslProperty.from_api(
        {"code": "ac_switch_hm", "name": "AC", "dataType": "BOOL", "subType": "RW"}
    ),
    TslProperty.from_api({"code": "ota_ots", "name": "OTA", "dataType": "BOOL", "subType": "R"}),
]


def make_coordinator(values: dict) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = {
        "DK": {"device": SimpleNamespace(device_name="F5000"), "tsl": TSL, "values": values}
    }
    coordinator.async_set_property = AsyncMock(return_value="ble")
    return coordinator


VALUES = {
    "noastime_io": 4,
    "upper_discharge_limit_us": 100,
    "eco_quite_mode_as": False,
    "ac_switch_hm": False,
    "ota_ots": True,
    "ac_data_input_hm": {"ac_input_voltage": "117.2", "ac_power": 700},
    "remain_time": 4,
}


def collect(platform: str, values: dict = VALUES) -> list:
    added: list = []
    check = tracker(make_coordinator(values), platform, added.extend)
    check()
    check()
    return added


def test_sensors_created_for_reported_paths_only() -> None:
    sensors = collect("sensor")
    paths = sorted(s._path for s in sensors)
    assert paths == ["ac_data_input_hm.ac_input_voltage", "remain_time"]
    volt = next(s for s in sensors if isinstance(s, MeasurementSensor) and "voltage" in s._path)
    assert volt.native_value == 117.2


def test_settings_by_type_skipping_dedicated_and_readonly() -> None:
    assert [type(e) for e in collect("select")] == [SettingSelect]
    assert [type(e) for e in collect("number")] == [SettingNumber]
    switches = collect("switch")
    assert [e._code for e in switches] == ["eco_quite_mode_as"]
    assert isinstance(switches[0], SettingSwitch)


@pytest.mark.asyncio
async def test_select_maps_labels_and_writes_raw_value() -> None:
    select = collect("select")[0]
    assert select.name == "Auto-off"
    assert select.options == ["Never", "3 min", "1 h", "12 h"]
    assert select.current_option == "12 h"
    await select.async_select_option("3 min")
    select.coordinator.async_set_property.assert_awaited_once_with("DK", "noastime_io", 2)


@pytest.mark.asyncio
async def test_number_uses_tsl_range() -> None:
    number = collect("number")[0]
    assert (number.native_min_value, number.native_max_value, number.native_step) == (50, 100, 1)
    assert number.native_unit_of_measurement == "%"
    await number.async_set_native_value(90.0)
    number.coordinator.async_set_property.assert_awaited_once_with(
        "DK", "upper_discharge_limit_us", 90
    )
