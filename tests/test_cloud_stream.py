"""Cloud MQTT push stream parsing."""

import base64
import json
from types import SimpleNamespace

from custom_components.pecron.transport.cloud_stream import (
    CloudStream,
    channel_id,
    coerce_kv,
    token_uid,
)


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"h.{body}.s"


def test_token_uid_and_channel() -> None:
    assert token_uid(_jwt({"uid": 12345})) == "12345"
    assert token_uid("garbage") is None
    assert channel_id("p11vwW", "F8D8117CB639") == "qdp11vwWF8D8117CB639"


def test_coerce_kv_matches_rest_shapes() -> None:
    types = {"battery_percentage": "INT", "ac_switch_hm": "BOOL", "host_packet_data_jdb": "STRUCT"}
    out = coerce_kv(
        {
            "battery_percentage": "78",
            "ac_switch_hm": "true",
            "host_packet_data_jdb": '{"host_packet_voltage": 52.1}',
            "output_power": 120,
        },
        types,
    )
    assert out == {
        "battery_percentage": 78,
        "ac_switch_hm": True,
        "host_packet_data_jdb": {"host_packet_voltage": 52.1},
        "output_power": 120,
    }


def test_bus_message_reaches_callback() -> None:
    got = []
    stream = CloudStream("US", lambda: None, lambda dk, kv: got.append((dk, kv)))
    stream.add_device("p11vwW", "F8D8117CB639")
    msg = SimpleNamespace(
        topic="q/2/d/qdp11vwWF8D8117CB639/bus_",
        payload=json.dumps({"data": {"kv": {"battery_percentage": 80}}}).encode(),
    )
    stream._handle_message(None, None, msg)
    assert got == [("F8D8117CB639", {"battery_percentage": 80})]
    assert stream.host == "iot-south.landecia.com"
