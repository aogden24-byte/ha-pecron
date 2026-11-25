"""Diagnostics: what each link delivers, and the station's full property list."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

TO_REDACT = {"email", "password", "auth_key", "token"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    stations: dict[str, Any] = {}
    for device_key, link in coordinator.links.items():
        tsl_codes = sorted(link.tsl_map.by_code)
        seen = {name: sorted(values) for name, values in link.by_transport.items()}
        all_seen = set().union(*(set(v) for v in link.by_transport.values())) if seen else set()
        stations[device_key] = {
            "name": link.device.device_name,
            "product_key": link.device.product_key,
            "active": link.active,
            "health": {name: h.as_dict() for name, h in link.health.items()},
            "codes_by_link": seen,
            "unknown_ids_by_link": {
                name: sorted(c for c in values if c.startswith("id_"))
                for name, values in link.by_transport.items()
            },
            "tsl_codes_never_received": sorted(set(tsl_codes) - all_seen),
            "tsl": [
                {"id": e.pid, "code": e.code, "type": e.data_type, "writable": e.writable}
                for e in sorted(link.tsl_map.by_code.values(), key=lambda e: e.pid)
            ],
            "values_by_link": link.by_transport,
        }
    return {
        "options": {
            k: ("**REDACTED**" if k.startswith("key_") else v)
            for k, v in async_redact_data(dict(entry.options), TO_REDACT).items()
        },
        "stations": stations,
    }
