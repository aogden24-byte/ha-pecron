"""Pecron Home Assistant integration."""

import logging
from typing import Final

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr

from .const import (
    ATTR_PROPERTY_CODE,
    ATTR_VALUE,
    DOMAIN,
    SERVICE_SET_PROPERTY,
)
from .coordinator import PecronDataUpdateCoordinator
from .transport import TransportError

_LOGGER = logging.getLogger(__name__)
PLATFORMS: Final = ["sensor", "binary_sensor", "switch", "select", "number"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Pecron from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = PecronDataUpdateCoordinator(hass, entry)

    # Raises ConfigEntryNotReady on failure; Home Assistant retries setup.
    await coordinator.async_config_entry_first_refresh()

    # Show persistent notification if no devices found
    if coordinator.data is not None and not coordinator.data:
        persistent_notification.async_create(
            hass,
            "No Pecron devices found on your account. "
            "Please check that devices are registered in the Pecron mobile app and try reloading the integration.",
            title="Pecron: No Devices Found",
            notification_id=f"{DOMAIN}_no_devices_{entry.entry_id}",
        )

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    async def async_handle_set_property(call: ServiceCall) -> None:
        """Handle the set_property service call."""
        device_id = call.data["device_id"]
        property_code = call.data[ATTR_PROPERTY_CODE]
        value = call.data[ATTR_VALUE]

        # Convert value to appropriate type
        # Handle boolean strings
        if isinstance(value, str):
            if value.lower() in ("true", "on", "yes", "1"):
                value = True
            elif value.lower() in ("false", "off", "no", "0"):
                value = False
            else:
                # Try to convert to number
                try:
                    if "." in value:
                        value = float(value)
                    else:
                        value = int(value)
                except ValueError:
                    # Keep as string
                    pass

        # Find the device key from device_id
        device_registry = dr.async_get(hass)
        device_entry = device_registry.async_get(device_id)

        if not device_entry:
            _LOGGER.error("Device %s not found in registry", device_id)
            return

        # Extract device_key from identifiers
        device_key = None
        for identifier in device_entry.identifiers:
            if identifier[0] == DOMAIN:
                device_key = identifier[1]
                break

        if not device_key:
            _LOGGER.error("Could not find device_key for device %s", device_id)
            return

        # Get the device data from coordinator
        if not coordinator.data or device_key not in coordinator.data:
            _LOGGER.error("Device %s not found in coordinator data", device_key)
            return

        device_data = coordinator.data[device_key]
        device = device_data["device"]
        tsl = device_data.get("tsl")

        # Validate property_code against TSL if available
        if tsl:
            tsl_property_codes = {prop.code: prop for prop in tsl}
            if property_code not in tsl_property_codes:
                _LOGGER.error(
                    "Property '%s' not found in TSL for device %s. Available: %s",
                    property_code,
                    device.device_name,
                    list(tsl_property_codes.keys()),
                )
                persistent_notification.async_create(
                    hass,
                    f"Property '{property_code}' is not supported by {device.device_name}. "
                    f"Available properties: {', '.join(sorted(tsl_property_codes.keys()))}",
                    title="Pecron: Invalid Property",
                    notification_id=f"{DOMAIN}_invalid_property_{device_key}",
                )
                return

            # Check if property is writable
            tsl_prop = tsl_property_codes[property_code]
            if not tsl_prop.writable:
                _LOGGER.error(
                    "Property '%s' is read-only for device %s",
                    property_code,
                    device.device_name,
                )
                persistent_notification.async_create(
                    hass,
                    f"Property '{property_code}' is read-only and cannot be modified.",
                    title="Pecron: Read-Only Property",
                    notification_id=f"{DOMAIN}_readonly_property_{device_key}",
                )
                return

        try:
            used = await coordinator.async_set_property(device_key, property_code, value)
        except TransportError as err:
            _LOGGER.error(
                "Failed to set property '%s' for device %s: %s",
                property_code,
                device.device_name,
                err,
            )
            persistent_notification.async_create(
                hass,
                f"Failed to set property '{property_code}' on {device.device_name}: {err}",
                title="Pecron: Property Set Failed",
                notification_id=f"{DOMAIN}_set_property_failed_{device_key}",
            )
            return
        _LOGGER.info(
            "Set property '%s' to '%s' for device %s via %s",
            property_code,
            value,
            device.device_name,
            used,
        )

    # Register the service (only once for the first config entry)
    if not hass.services.has_service(DOMAIN, SERVICE_SET_PROPERTY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_PROPERTY,
            async_handle_set_property,
        )

    # Register options update listener
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the options change.

    The coordinator also updates ``entry.data`` to cache discovered hosts,
    TSL and auth keys; those updates must not trigger a reload.
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and dict(entry.options) == coordinator.options_snapshot:
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

        # Unregister service if this is the last config entry
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_SET_PROPERTY)

    return unload_ok
