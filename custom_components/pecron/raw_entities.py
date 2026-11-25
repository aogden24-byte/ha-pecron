"""One diagnostic sensor per raw field the station reports.

Every property (and every field inside a struct or array) gets a sensor the
first time it appears, from whichever link delivered it. Numbers and booleans
are numeric with a state class, so their history can be graphed and compared
against what the station was doing. This is how unnamed fields (``id_<n>``,
``field_<n>``) get identified.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

MAX_STATE_LEN = 255


def flatten(values: dict[str, Any]) -> dict[str, Any]:
    """``{"a": {"b": 1}, "c": [2, {"d": 3}]}`` -> ``{"a.b": 1, "c.0": 2, "c.1.d": 3}``."""
    out: dict[str, Any] = {}

    def walk(path: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, sub in value.items():
                walk(f"{path}.{key}", sub)
        elif isinstance(value, list):
            for index, sub in enumerate(value):
                walk(f"{path}.{index}", sub)
        else:
            out[path] = value

    for code, value in values.items():
        walk(code, value)
    return out


def _numeric(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return None
    return None


class RawFieldSensor(CoordinatorEntity, SensorEntity):
    """Raw value of one field, for tracking and identification."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(self, coordinator, device_key: str, device: Any, path: str, sample: Any) -> None:
        super().__init__(coordinator)
        self._device_key = device_key
        self._path = path
        self._attr_unique_id = f"{DOMAIN}_{device_key}_raw_{path}"
        self._attr_name = f"Raw {path}"
        self._is_numeric = _numeric(sample) is not None
        if self._is_numeric:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_info = {"identifiers": {(DOMAIN, device_key)}}

    def _value(self) -> Any:
        data = (self.coordinator.data or {}).get(self._device_key) or {}
        return flatten(data.get("values") or {}).get(self._path)

    @property
    def available(self) -> bool:
        return super().available and self._value() is not None

    @property
    def native_value(self) -> Any:
        value = self._value()
        if self._is_numeric:
            return _numeric(value)
        text = value if isinstance(value, str) else json.dumps(value)
        return text[:MAX_STATE_LEN]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        link = getattr(self.coordinator, "links", {}).get(self._device_key)
        sources = []
        if link is not None:
            code = self._path.split(".", 1)[0]
            sources = sorted(n for n, v in link.by_transport.items() if code in v)
        return {"field": self._path, "reported_by": sources}


def raw_sensor_tracker(coordinator, add_entities: Callable[[list], None]) -> Callable[[], None]:
    """Return a coordinator listener that adds a sensor for each new field."""
    known: set[tuple[str, str]] = set()

    def check() -> None:
        new = []
        for device_key, device_data in (coordinator.data or {}).items():
            fields = flatten(device_data.get("values") or {})
            for path, sample in fields.items():
                if (device_key, path) in known:
                    continue
                known.add((device_key, path))
                new.append(
                    RawFieldSensor(coordinator, device_key, device_data["device"], path, sample)
                )
        if new:
            add_entities(new)

    return check
