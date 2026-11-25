"""Diagnostic entities that make the transport selector observable.

* ``sensor.<device>_connection``: which transport is supplying data now.
* ``binary_sensor.<device>_<transport>_link``: health of each enabled transport,
  with failure counts and the last error as attributes.

They stay available while every transport is failing, so the failure itself
can be seen and automated on.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .transport import TRANSPORT_BLE, TRANSPORT_CLOUD, TRANSPORT_NONE, TRANSPORT_TCP

LINK_LABELS = {TRANSPORT_BLE: "Bluetooth", TRANSPORT_TCP: "Wi-Fi", TRANSPORT_CLOUD: "Cloud"}


class _LinkEntity(CoordinatorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device_key: str) -> None:
        super().__init__(coordinator)
        self._device_key = device_key

    @property
    def _link(self):
        return self.coordinator.links.get(self._device_key)

    @property
    def available(self) -> bool:
        return self._link is not None

    @property
    def device_info(self) -> dict[str, Any]:
        device = self._link.device if self._link else None
        return {
            "identifiers": {(DOMAIN, self._device_key)},
            "name": getattr(device, "device_name", self._device_key),
            "manufacturer": "Pecron",
            "model": getattr(device, "product_name", None),
            "hw_version": self._device_key,
        }


class ConnectionSensor(_LinkEntity, SensorEntity):
    """Which transport the selector is using."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [TRANSPORT_BLE, TRANSPORT_TCP, TRANSPORT_CLOUD, TRANSPORT_NONE]
    _attr_icon = "mdi:transit-connection-variant"

    def __init__(self, coordinator, device_key: str, device_name: str) -> None:
        super().__init__(coordinator, device_key)
        self._attr_unique_id = f"{DOMAIN}_{device_key}_connection"
        self._attr_name = f"{device_name} Connection"

    @property
    def native_value(self) -> str:
        return (self._link.active if self._link else None) or TRANSPORT_NONE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self._link:
            return {}
        return {
            "priority": [t.name for t in self._link.transports],
            "links": {name: h.as_dict() for name, h in self._link.health.items()},
        }


class LinkBinarySensor(_LinkEntity, BinarySensorEntity):
    """Health of one transport: on = usable, off = degraded or down."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, device_key: str, device_name: str, transport: str) -> None:
        super().__init__(coordinator, device_key)
        self._transport = transport
        self._attr_unique_id = f"{DOMAIN}_{device_key}_{transport}_link"
        self._attr_name = f"{device_name} {LINK_LABELS.get(transport, transport)} link"

    @property
    def _health(self):
        return self._link.health.get(self._transport) if self._link else None

    @property
    def is_on(self) -> bool | None:
        health = self._health
        if health is None or health.state == "unknown":
            return None
        return health.state == "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        health = self._health
        return health.as_dict() if health else {}


def link_entities_for(coordinator, kind: str) -> list:
    """Build the diagnostic entities of one platform for every known device."""
    entities: list = []
    for device_key, link in coordinator.links.items():
        name = link.device.device_name
        if kind == "sensor":
            entities.append(ConnectionSensor(coordinator, device_key, name))
        else:
            entities.extend(
                LinkBinarySensor(coordinator, device_key, name, t.name) for t in link.transports
            )
    return entities
