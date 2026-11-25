"""Config flow for Pecron integration."""

import logging
from typing import Any

import requests
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from unofficial_pecron_api import PecronAPI
from unofficial_pecron_api.exceptions import AuthenticationError

from .const import (
    CONF_DEVICE_CACHE,
    CONF_EMAIL,
    CONF_HOST_PREFIX,
    CONF_KEY_PREFIX,
    CONF_PASSWORD,
    CONF_REFRESH_INTERVAL,
    CONF_REGION,
    CONF_SCAN_INTERVAL_LOCAL,
    DEFAULT_PRIORITIES,
    DEFAULT_REFRESH_INTERVAL,
    DEFAULT_REGION,
    DEFAULT_SCAN_INTERVAL_LOCAL,
    DOMAIN,
    PRIORITY_KEYS,
    REGIONS,
)

_LOGGER = logging.getLogger(__name__)


class PecronConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Pecron."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return PecronOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self.hass.async_add_executor_job(
                    self._validate_pecron_credentials,
                    user_input[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    user_input.get(CONF_REGION, DEFAULT_REGION),
                )
            except PecronAuthError as err:
                _LOGGER.error("Authentication failed: %s", err)
                errors["base"] = "invalid_auth"
            except PecronConnectionError as err:
                _LOGGER.error("Connection failed: %s", err)
                errors["base"] = "cannot_connect"
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error: %s", err)
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data=user_input,
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_REGION, default=DEFAULT_REGION): vol.In(REGIONS),
                vol.Optional(CONF_REFRESH_INTERVAL, default=DEFAULT_REFRESH_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=60, max=3600)
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)

    async def async_step_import(self, import_data: dict[str, Any]) -> FlowResult:
        """Import configuration from YAML."""
        return await self.async_step_user(import_data)

    @staticmethod
    def _validate_pecron_credentials(email: str, password: str, region: str) -> None:
        """Validate Pecron credentials.

        The login endpoint rejects a request with an ``AuthenticationError``
        both for a wrong password and for a wrong region: each region signs
        requests with a different secret, so picking the wrong region looks
        identical to bad credentials from the API's point of view. Surface
        both as ``invalid_auth`` (with a region hint in the error string)
        rather than the generic ``cannot_connect``, which previously misled
        users into thinking it was a network problem. See GH#6.
        """
        try:
            api = PecronAPI(region=region)
            api.login(email, password)
            devices = api.get_devices()
            api.close()
        except AuthenticationError as err:
            raise PecronAuthError(str(err)) from err
        except requests.exceptions.RequestException as err:
            raise PecronConnectionError(str(err)) from err

        if not devices:
            raise PecronAuthError("No devices found on account")


TRANSPORT_CHOICES = {
    "ble": "Bluetooth (local adapter or ESPHome proxy)",
    "tcp": "Wi-Fi (local TCP, port 6607)",
    "cloud": "Pecron cloud",
    "none": "Not used",
}


class PecronOptionsFlow(config_entries.OptionsFlow):
    """Transport priority, poll rates and per-device addresses."""

    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        options = self.config_entry.options
        if user_input is not None:
            chosen = [user_input[k] for k in PRIORITY_KEYS if user_input[k] != "none"]
            if not chosen:
                errors["base"] = "no_transport"
            elif len(set(chosen)) != len(chosen):
                errors["base"] = "duplicate_transport"
            else:
                self._pending = dict(user_input)
                if any(t in ("tcp", "ble") for t in chosen) and self._cached_devices():
                    return await self.async_step_devices()
                return self.async_create_entry(title="", data={**options, **self._pending})

        current_cloud = options.get(
            CONF_REFRESH_INTERVAL,
            self.config_entry.data.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL),
        )
        fields: dict[Any, Any] = {}
        for key, default in zip(PRIORITY_KEYS, DEFAULT_PRIORITIES, strict=True):
            fields[vol.Required(key, default=options.get(key, default))] = vol.In(TRANSPORT_CHOICES)
        fields[
            vol.Required(
                CONF_SCAN_INTERVAL_LOCAL,
                default=options.get(CONF_SCAN_INTERVAL_LOCAL, DEFAULT_SCAN_INTERVAL_LOCAL),
            )
        ] = vol.All(vol.Coerce(int), vol.Range(min=5, max=600))
        fields[vol.Required(CONF_REFRESH_INTERVAL, default=current_cloud)] = vol.All(
            vol.Coerce(int), vol.Range(min=60, max=3600)
        )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields), errors=errors)

    def _cached_devices(self) -> dict[str, dict]:
        return dict(self.config_entry.data.get(CONF_DEVICE_CACHE, {}))

    async def async_step_devices(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Optional fixed IP per station (blank = find it on the LAN automatically)."""
        options = self.config_entry.options
        devices = self._cached_devices()
        errors: dict[str, str] = {}
        if user_input is not None:
            values: dict[str, str] = {}
            for dk in devices:
                values[f"{CONF_HOST_PREFIX}{dk}"] = (
                    user_input.get(f"{CONF_HOST_PREFIX}{dk}") or ""
                ).strip()
                key = (user_input.get(f"{CONF_KEY_PREFIX}{dk}") or "").strip()
                if key and not _valid_key(key):
                    errors[f"{CONF_KEY_PREFIX}{dk}"] = "invalid_key"
                values[f"{CONF_KEY_PREFIX}{dk}"] = key
            if not errors:
                return self.async_create_entry(
                    title="", data={**options, **self._pending, **values}
                )
        fields: dict[Any, Any] = {}
        for dk, cached in devices.items():
            fields[
                vol.Optional(
                    f"{CONF_HOST_PREFIX}{dk}",
                    description={
                        "suggested_value": options.get(f"{CONF_HOST_PREFIX}{dk}")
                        or cached.get("host", "")
                    },
                )
            ] = str
            fields[
                vol.Optional(
                    f"{CONF_KEY_PREFIX}{dk}",
                    description={"suggested_value": options.get(f"{CONF_KEY_PREFIX}{dk}", "")},
                )
            ] = str
        names = ", ".join(f"{c.get('name', dk)} = {dk}" for dk, c in devices.items())
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(fields),
            description_placeholders={"devices": names},
            errors=errors,
        )


def _valid_key(key: str) -> bool:
    """A local key is base64 for 16 bytes (AES-128)."""
    import base64
    import binascii

    try:
        return len(base64.b64decode(key, validate=True)) == 16
    except (binascii.Error, ValueError):
        return False


class PecronAuthError(HomeAssistantError):
    """Pecron authentication error."""


class PecronConnectionError(HomeAssistantError):
    """Pecron connection error."""
