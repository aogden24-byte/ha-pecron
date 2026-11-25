"""Pecron (Quectel) cloud MQTT push stream.

Mirrors pecron-monitor's cloud path: an MQTT-over-WebSocket session to the
Quectel broker, authenticated with the account token, subscribed to each
station's ``q/2/d/qd<pk><dk>/bus_`` topic. The station publishes property
reports there (``{"data": {"kv": {code: value}}}``); with
``high_frequency_reporting = 3`` it reports every few seconds.

Receiving costs no API quota; only the REST call that sets
``high_frequency_reporting`` does.
"""

from __future__ import annotations

import base64
import json
import logging
import ssl
import threading
import time
from collections.abc import Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

BROKERS = {
    "US": "iot-south.landecia.com",
    "EU": "iot-south.quecteleu.com",
    "CN": "iot-south.quectelcn.com",
}
BROKER_PORT = 8443
BROKER_PATH = "/ws/v2"


def channel_id(product_key: str, device_key: str) -> str:
    return f"qd{product_key}{device_key}"


def token_uid(token: str) -> str | None:
    """The MQTT client id embeds the user id from the access token (a JWT)."""
    try:
        part = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return None
    uid = payload.get("uid")
    return str(uid) if uid is not None else None


def coerce_kv(kv: dict[str, Any], types: dict[str, str]) -> dict[str, Any]:
    """Normalise pushed values to the same shapes the REST path produces."""
    out: dict[str, Any] = {}
    for code, raw in kv.items():
        dtype = types.get(code, "")
        value: Any = raw
        if isinstance(raw, str):
            try:
                if dtype in ("STRUCT", "ARRAY") or raw[:1] in ("{", "["):
                    value = json.loads(raw)
                elif dtype == "BOOL" or raw.lower() in ("true", "false"):
                    value = raw.lower() == "true"
                elif dtype in ("INT", "ENUM") and raw.lstrip("-").isdigit():
                    value = int(raw)
                elif dtype in ("FLOAT", "DOUBLE"):
                    value = float(raw)
            except ValueError:
                value = raw
        out[code] = value
    return out


class CloudStream:
    """One MQTT session for the account; callbacks run on the paho thread."""

    def __init__(
        self,
        region: str,
        token_getter: Callable[[], str | None],
        on_values: Callable[[str, dict[str, Any]], None],
        on_online: Callable[[str, bool], None] | None = None,
    ) -> None:
        self.host = BROKERS.get(region, BROKERS["US"])
        self._token_getter = token_getter
        self._on_values = on_values
        self._on_online = on_online
        self._channels: dict[str, str] = {}  # channel id -> device key
        self._client: Any = None
        self._lock = threading.Lock()
        self.connected = False
        self.last_message = 0.0
        self.last_error: str | None = None

    def add_device(self, product_key: str, device_key: str) -> None:
        self._channels[channel_id(product_key, device_key)] = device_key

    # -- lifecycle (blocking; run in an executor) -----------------------------

    def start(self) -> None:
        import paho.mqtt.client as mqtt

        with self._lock:
            self._stop_locked()
            token = self._token_getter()
            uid = token_uid(token) if token else None
            if not token or not uid:
                raise RuntimeError("no cloud token for the push stream")
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"qu_{uid}_{int(time.time() * 1000)}",
                transport="websockets",
                protocol=mqtt.MQTTv311,
            )
            client.ws_set_options(path=BROKER_PATH)
            client.tls_set_context(ssl.create_default_context())
            client.username_pw_set(username="", password=token)
            client.on_connect = self._handle_connect
            client.on_disconnect = self._handle_disconnect
            client.on_message = self._handle_message
            client.reconnect_delay_set(min_delay=1, max_delay=60)
            client.connect_async(self.host, BROKER_PORT, keepalive=60)
            client.loop_start()
            self._client = client

    def refresh_token(self) -> None:
        """Use a newer token on the next reconnect."""
        token = self._token_getter()
        if token and self._client is not None:
            self._client.username_pw_set(username="", password=token)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        client, self._client = self._client, None
        self.connected = False
        if client is not None:
            try:
                client.disconnect()
                client.loop_stop()
            except Exception as err:  # noqa: BLE001 - best effort shutdown
                _LOGGER.debug("push stream stop: %s", err)

    # -- paho callbacks --------------------------------------------------------

    def _handle_connect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            self.connected = False
            self.last_error = f"broker refused: {reason_code}"
            _LOGGER.warning("Pecron push stream: %s", self.last_error)
            if "authori" in str(reason_code).lower() or "password" in str(reason_code).lower():
                self.refresh_token()
            return
        self.connected = True
        self.last_error = None
        _LOGGER.info("Pecron push stream connected (%d stations)", len(self._channels))
        for cid in self._channels:
            for suffix in ("bus_", "onl_"):
                client.subscribe(f"q/2/d/{cid}/{suffix}", qos=1)

    def _handle_disconnect(self, _client, _userdata, _flags, reason_code, _properties=None) -> None:
        self.connected = False
        self.last_error = f"disconnected: {reason_code}"
        _LOGGER.debug("Pecron push stream %s", self.last_error)
        self.refresh_token()

    def _handle_message(self, _client, _userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        parts = msg.topic.split("/")
        if len(parts) < 5:
            return
        device_key = self._channels.get(parts[3]) or payload.get("deviceKey", "")
        suffix = parts[4]
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not device_key:
            return
        if suffix == "bus_":
            kv = data.get("kv")
            if isinstance(kv, dict) and kv:
                self.last_message = time.monotonic()
                self._on_values(device_key, kv)
        elif suffix == "onl_" and self._on_online is not None:
            self._on_online(device_key, data.get("value", 0) == 1)
