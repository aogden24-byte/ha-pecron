"""Named entities built from what the station reports and its TSL model.

* Measurements: a table of known value paths (inside the reported structs)
  with names, units and device classes. A sensor is created the first time
  its path appears, from whichever link delivered it.
* Settings: every writable BOOL / ENUM / INT / FLOAT property in the TSL
  becomes a switch / select / number, named and ranged from the TSL itself
  (Pecron's own names and option lists). Created once the property has a
  value. Writes go through the coordinator's link priority.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .raw_entities import flatten
from .transport import TransportError

_LOGGER = logging.getLogger(__name__)

# Properties with dedicated entities elsewhere, or managed internally.
SKIP_SETTINGS = {
    "ac_switch_hm",
    "dc_switch_hm",
    "ac_charging_power_ios",
    "battery_percentage",
    "high_frequency_reporting",
    "user_operation",
}

# Friendlier names than the TSL's where its names are terse or missing.
SETTING_NAMES = {
    "noastime_io": "Auto-off",
    "device_standy_times_as": "Standby time",
    "ac_charge_stop_value_iaos": "AC input cutoff",
    "upper_discharge_limit_us": "Charge limit",
    "lower_discharge_limit_us": "Discharge limit",
    "dc_output_v_iaos": "DC output voltage setting",
    "dc_output_a_iaos": "DC output current setting",
    "ac_output_voltage_io": "AC output voltage setting",
    "ac_output_frequency_io": "AC output frequency setting",
    "xt60_input_mode_iaos": "XT60 input mode",
    "xt60_input_source_iaos": "XT60 input source",
    "machine_btn_light_us": "Button light",
    "machine_screen_light_us": "Screen brightness",
    "eco_quite_mode_as": "ECO mode",
    "ac_nor_open_lab": "AC normally on",
    "dc_nor_open_lab": "DC normally on",
    "generator_charging_mode_lab": "Generator charging mode",
}

UNITS = {
    "%": PERCENTAGE,
    "V": UnitOfElectricPotential.VOLT,
    "A": UnitOfElectricCurrent.AMPERE,
    "W": UnitOfPower.WATT,
    "min": UnitOfTime.MINUTES,
    "h": UnitOfTime.HOURS,
}
SETTING_UNITS = {
    "ac_charge_stop_value_iaos": PERCENTAGE,
    "upper_discharge_limit_us": PERCENTAGE,
    "lower_discharge_limit_us": PERCENTAGE,
    "dc_output_v_iaos": UnitOfElectricPotential.VOLT,
    "dc_output_a_iaos": UnitOfElectricCurrent.AMPERE,
}


@dataclass(frozen=True)
class Measurement:
    name: str
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT
    precision: int | None = None


_V = (UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE)
_A = (UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT)
_W = (UnitOfPower.WATT, SensorDeviceClass.POWER)
_T = (UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE)


def _m(name: str, kind: tuple, precision: int | None = None) -> Measurement:
    return Measurement(name, kind[0], kind[1], SensorStateClass.MEASUREMENT, precision)


def _energy(name: str) -> Measurement:
    return Measurement(
        name,
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
        SensorStateClass.TOTAL_INCREASING,
        3,
    )


MEASUREMENTS: dict[str, Measurement] = {
    "ac_data_input_hm.ac_input_voltage": _m("AC input voltage", _V, 1),
    "ac_data_input_hm.ac_input_temp": _m("AC input temperature", _T),
    "ac_data_output_hm.ac_output_power": _m("AC output power", _W),
    "ac_data_output_hm.ac_L1_output_power": _m("AC L1 output power", _W),
    "ac_data_output_hm.ac_L2_output_power": _m("AC L2 output power", _W),
    "ac_data_output_hm.ac_output_voltage": _m("AC output voltage", _V, 1),
    "ac_data_output_hm.ac_output_hz": Measurement(
        "AC output frequency", "Hz", SensorDeviceClass.FREQUENCY
    ),
    "ac_data_output_hm.ac_output_temp": _m("AC output temperature", _T),
    "dc_data_output_hm.dc_output_power": _m("DC output power", _W),
    "dc_data_output_hm.dc_output_voltage": _m("DC output voltage", _V, 1),
    "dc_data_output_hm.dc_output_temp": _m("DC output temperature", _T),
    "dc_data_input_hm.XT60_1_input_voltage": _m("XT60 1 voltage", _V, 1),
    "dc_data_input_hm.XT60_1_input_current": _m("XT60 1 current", _A, 2),
    "dc_data_input_hm.XT60_1_input_power": _m("XT60 1 power", _W),
    "dc_data_input_hm.XT60_2_input_voltage": _m("XT60 2 voltage", _V, 1),
    "dc_data_input_hm.XT60_2_input_current": _m("XT60 2 current", _A, 2),
    "dc_data_input_hm.XT60_2_input_power": _m("XT60 2 power", _W),
    "host_packet_data_jdb.host_packet_status": Measurement("Battery status"),
    "remain_time": Measurement("Remaining time", UnitOfTime.MINUTES, SensorDeviceClass.DURATION),
    "FAULT_ALARM_ENUM": Measurement("Fault code", state_class=None),
    "solar_panel_power_generation": _energy("Solar energy"),
    "total_energy": _energy("Total energy"),
    "ac_charge_energy": _energy("AC charge energy"),
    "total_ac_charge_energy": _energy("AC charge energy lifetime"),
    "ac_output_energy": _energy("AC output energy"),
    "total_ac_output_energy": _energy("AC output energy lifetime"),
    "dc_output_energy": _energy("DC output energy"),
    "total_dc_output_energy": _energy("DC output energy lifetime"),
}


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _device_info(device_key: str) -> dict:
    return {"identifiers": {(DOMAIN, device_key)}}


def _values(coordinator, device_key: str) -> dict[str, Any]:
    data = (coordinator.data or {}).get(device_key) or {}
    return data.get("values") or {}


class _Base(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, device_key: str, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._device_key = device_key
        self._key = key
        self._attr_unique_id = f"{DOMAIN}_{device_key}_{key}"
        self._attr_name = name
        self._attr_device_info = _device_info(device_key)


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


class MeasurementSensor(_Base, SensorEntity):
    def __init__(self, coordinator, device_key: str, path: str, spec: Measurement) -> None:
        super().__init__(coordinator, device_key, f"m_{path}", spec.name)
        self._path = path
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = spec.device_class
        self._attr_state_class = spec.state_class
        if spec.precision is not None:
            self._attr_suggested_display_precision = spec.precision

    def _raw(self) -> Any:
        return flatten(_values(self.coordinator, self._device_key)).get(self._path)

    @property
    def available(self) -> bool:
        return super().available and self._raw() is not None

    @property
    def native_value(self) -> float | None:
        return _num(self._raw())


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class _Setting(_Base):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, device_key: str, prop) -> None:
        name = SETTING_NAMES.get(prop.code) or prop.name or prop.code.replace("_", " ").title()
        super().__init__(coordinator, device_key, f"set_{prop.code}", name)
        self._prop = prop
        self._code = prop.code

    def _raw(self) -> Any:
        return _values(self.coordinator, self._device_key).get(self._code)

    @property
    def available(self) -> bool:
        return super().available and self._raw() is not None

    async def _write(self, value: Any) -> None:
        try:
            used = await self.coordinator.async_set_property(self._device_key, self._code, value)
        except TransportError as err:
            raise HomeAssistantError(f"Could not set {self.name}: {err}") from err
        _LOGGER.info("%s set to %s via %s", self._code, value, used)


class SettingSwitch(_Setting, SwitchEntity):
    @property
    def is_on(self) -> bool | None:
        raw = self._raw()
        if raw is None:
            return None
        if isinstance(raw, str):
            return raw.lower() in ("true", "1", "on")
        return bool(raw)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(False)


class SettingSelect(_Setting, SelectEntity):
    def __init__(self, coordinator, device_key: str, prop) -> None:
        super().__init__(coordinator, device_key, prop)
        self._value_to_label = {str(ev.value): ev.name or str(ev.value) for ev in prop.enum_values}
        self._label_to_value = {label: value for value, label in self._value_to_label.items()}
        self._attr_options = list(self._value_to_label.values())

    @property
    def current_option(self) -> str | None:
        raw = self._raw()
        if raw is None:
            return None
        key = str(int(raw)) if isinstance(raw, float) and raw.is_integer() else str(raw)
        return self._value_to_label.get(key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"raw_value": self._raw()}

    async def async_select_option(self, option: str) -> None:
        value = self._label_to_value[option]
        await self._write(int(value) if value.lstrip("-").isdigit() else value)


class SettingNumber(_Setting, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, device_key: str, prop) -> None:
        super().__init__(coordinator, device_key, prop)
        spec = prop.int_spec
        is_float = prop.data_type in ("FLOAT", "DOUBLE")
        self._attr_native_min_value = _num(getattr(spec, "min", None)) or 0.0
        self._attr_native_max_value = _num(getattr(spec, "max", None)) or 100.0
        self._attr_native_step = _num(getattr(spec, "step", None)) or (0.1 if is_float else 1.0)
        unit = getattr(spec, "unit", None)
        self._attr_native_unit_of_measurement = SETTING_UNITS.get(prop.code) or UNITS.get(
            unit or "", unit or None
        )
        self._is_float = is_float

    @property
    def native_value(self) -> float | None:
        return _num(self._raw())

    async def async_set_native_value(self, value: float) -> None:
        await self._write(value if self._is_float else int(round(value)))


SETTING_CLASSES = {"BOOL": SettingSwitch, "ENUM": SettingSelect, "INT": SettingNumber}
SETTING_CLASSES["FLOAT"] = SettingNumber
SETTING_CLASSES["DOUBLE"] = SettingNumber


def setting_platform(prop) -> str | None:
    cls = SETTING_CLASSES.get(prop.data_type)
    if cls is SettingSwitch:
        return "switch"
    if cls is SettingSelect:
        return "select" if prop.enum_values else None
    if cls is SettingNumber:
        return "number"
    return None


# ---------------------------------------------------------------------------
# Trackers: add entities as their data first appears
# ---------------------------------------------------------------------------


def tracker(coordinator, platform: str, add_entities: Callable[[list], None]) -> Callable[[], None]:
    known: set[tuple[str, str]] = set()

    def check() -> None:
        new: list = []
        for device_key, device_data in (coordinator.data or {}).items():
            values = device_data.get("values") or {}
            if platform == "sensor":
                flat = flatten(values)
                for path, spec in MEASUREMENTS.items():
                    if path in flat and (device_key, path) not in known:
                        known.add((device_key, path))
                        new.append(MeasurementSensor(coordinator, device_key, path, spec))
                continue
            for prop in device_data.get("tsl") or []:
                if (
                    not prop.writable
                    or prop.code in SKIP_SETTINGS
                    or prop.code not in values
                    or (device_key, prop.code) in known
                    or setting_platform(prop) != platform
                ):
                    continue
                known.add((device_key, prop.code))
                new.append(SETTING_CLASSES[prop.data_type](coordinator, device_key, prop))
        if new:
            add_entities(new)

    return check


def setup_tracker(coordinator, entry, platform: str, add_entities) -> None:
    check = tracker(coordinator, platform, add_entities)
    check()
    entry.async_on_unload(coordinator.async_add_listener(check))
