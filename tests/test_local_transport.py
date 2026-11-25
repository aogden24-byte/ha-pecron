"""TSL mapping and an end-to-end TCP session against a simulated station."""

import asyncio
import base64

import pytest

from custom_components.pecron import ttlv
from custom_components.pecron.transport import TransportError
from custom_components.pecron.transport.tcp import TcpTransport, discover_host
from custom_components.pecron.tsl_map import TslMap, from_tsl_info, to_tsl_info

AUTH_KEY = bytes(range(16))
AUTH_B64 = base64.b64encode(AUTH_KEY).decode()
RANDOM = "7DAFD8E50291D9D7"

RAW_TSL = [
    {"id": 1, "code": "battery_percentage", "dataType": "INT", "subType": "RW"},
    {
        "id": 27,
        "code": "dc_output_v_iaos",
        "dataType": "FLOAT",
        "subType": "RW",
        "specs": {"min": "12", "max": "14", "step": "0.1"},
    },
    {
        "id": 29,
        "code": "ac_data_input_hm",
        "dataType": "STRUCT",
        "subType": "R",
        "specs": [
            {"id": 1, "code": "ac_power", "dataType": "INT"},
            {"id": 2, "code": "ac_input_voltage", "dataType": "FLOAT"},
        ],
    },
    {"id": 35, "code": "host_packet_data_jdb", "dataType": "STRUCT", "subType": "R"},
    {
        "id": 36,
        "code": "charging_pack_data_jdb",
        "dataType": "ARRAY",
        "subType": "R",
        "specs": {
            "type": "STRUCT",
            "specs": [
                {"id": 1, "code": "charging_pack_num"},
                {"id": 6, "code": "charging_pack_battery"},
            ],
        },
    },
    {"id": 51, "code": "ac_charging_power_ios", "dataType": "ENUM", "subType": "RW"},
    {"id": 56, "code": "ac_switch_hm", "dataType": "BOOL", "subType": "RW"},
]


def struct_header(fid: int, count: int) -> bytes:
    return ((fid << 3) | ttlv.T_STRUCT).to_bytes(2, "big") + count.to_bytes(2, "big")


def sample_report() -> bytes:
    return (
        ttlv.encode_num_field(1, 78)
        + ttlv.encode_bool_field(56, False)
        + struct_header(29, 2)
        + ttlv.encode_num_field(1, 719)
        + ttlv.encode_num_field(2, 117.2, 1)
        + struct_header(35, 3)
        + ttlv.encode_num_field(2, 53.9, 1)
        + ttlv.encode_num_field(3, 5.12, 2)
        + ttlv.encode_num_field(9, 1)  # unknown sub-field -> field_9
        + struct_header(36, 1)
        + struct_header(0, 2)
        + ttlv.encode_num_field(1, 1)
        + ttlv.encode_num_field(6, 90)
        + ttlv.encode_num_field(999, 5)  # unknown top-level id: ignored
    )


def test_tsl_decode_structs_arrays_and_fallbacks() -> None:
    tsl = TslMap(RAW_TSL)
    values = tsl.decode(ttlv.parse_fields(sample_report()))
    assert values["battery_percentage"] == 78
    assert values["ac_switch_hm"] is False
    assert values["ac_data_input_hm"] == {"ac_power": 719, "ac_input_voltage": 117.2}
    # host_packet has no specs in this TSL -> static fallback names, unknown -> field_n
    assert values["host_packet_data_jdb"] == {
        "host_packet_voltage": 53.9,
        "host_packet_current": 5.12,
        "field_9": 1,
    }
    assert values["charging_pack_data_jdb"] == [
        {"charging_pack_num": 1, "charging_pack_battery": 90}
    ]
    assert 999 not in values


def test_tsl_info_roundtrip_feeds_device_properties() -> None:
    from unofficial_pecron_api.models import DeviceProperties

    tsl = TslMap(RAW_TSL)
    values = tsl.decode(ttlv.parse_fields(sample_report()))
    info = to_tsl_info(values, tsl)
    props = DeviceProperties.from_api(info)
    assert props.battery_percentage == 78
    assert props.ac_switch is False
    assert float(props.battery_pack["host_packet_current"]) == 5.12
    assert from_tsl_info(info)["ac_data_input_hm"]["ac_power"] == 719


def test_encode_write_types() -> None:
    tsl = TslMap(RAW_TSL)
    assert tsl.encode_write("ac_switch_hm", True) == ttlv.encode_bool_field(56, True)
    assert tsl.encode_write("ac_charging_power_ios", "4") == ttlv.encode_num_field(51, 4)
    assert tsl.encode_write("dc_output_v_iaos", 13.6) == ttlv.encode_num_field(27, 13.6, 1)
    with pytest.raises(KeyError):
        tsl.encode_write("nope", 1)


class FakeStation:
    """Minimal port-6607 station: handshake, encrypted read, write ack, then close."""

    def __init__(
        self,
        key: bytes = AUTH_KEY,
        close_after_reply: bool = True,
        reply_cmd: int = ttlv.CMD_REPORT,
    ) -> None:
        self.key = key
        self.reply_cmd = reply_cmd
        self.answer_once = False  # F5000LFP: only the first read per session is answered
        self.sessions = 0
        self.close_after_reply = close_after_reply
        self.writes: list[bytes] = []
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self.server
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        r = ttlv.FrameReassembler()
        iv = ttlv.make_iv(RANDOM)
        self.sessions += 1
        answered = False
        try:
            while data := await reader.read(1024):
                for raw in r.feed(data):
                    frame = ttlv.parse_frame(raw)
                    if frame.cmd == ttlv.CMD_RANDOM_REQ:
                        writer.write(
                            ttlv.build_frame(
                                ttlv.CMD_RANDOM_RESP, ttlv.encode_bytes_field(1, RANDOM.encode())
                            )
                        )
                    elif frame.cmd == ttlv.CMD_LOGIN_REQ:
                        expected = ttlv.login_payload(self.key, RANDOM)
                        ok = frame.payload == expected
                        writer.write(
                            ttlv.build_frame(
                                ttlv.CMD_LOGIN_RESP, ttlv.encode_num_field(3, 0 if ok else -1)
                            )
                        )
                    elif frame.cmd == ttlv.CMD_READ and self.answer_once and answered:
                        pass
                    elif frame.cmd == ttlv.CMD_READ:
                        answered = True
                        # two report packets, like the multi-packet models
                        for body in (sample_report(), ttlv.encode_num_field(51, 4)):
                            writer.write(
                                ttlv.build_frame(self.reply_cmd, ttlv.encrypt(self.key, iv, body))
                            )
                        await writer.drain()
                        if self.close_after_reply:
                            writer.close()
                            return
                    elif frame.cmd == ttlv.CMD_WRITE:
                        self.writes.append(ttlv.decrypt(self.key, iv, frame.payload))
                        writer.write(ttlv.build_frame(ttlv.CMD_ACK, b""))
                    await writer.drain()
        finally:
            writer.close()


@pytest.fixture
def fast_timeouts(monkeypatch):
    from custom_components.pecron.transport import local

    monkeypatch.setattr(local, "REPORT_GAP_TIMEOUT", 0.2)
    monkeypatch.setattr(local, "FIRST_REPORT_TIMEOUT", 1.0)
    monkeypatch.setattr(local, "POST_LOGIN_DELAY", 0.0)


async def _transport(port: int, key_b64: str = AUTH_B64) -> TcpTransport:
    from custom_components.pecron.transport import tcp

    tcp.TCP_PORT = port
    return TcpTransport("127.0.0.1", "F8D8117CB639", key_b64, TslMap(RAW_TSL))


@pytest.mark.asyncio
async def test_tcp_read_reconnects_after_device_closes(fast_timeouts) -> None:
    station = FakeStation(close_after_reply=True)
    port = await station.start()
    transport = await _transport(port)
    try:
        first = await transport.read()
        second = await transport.read()  # station closed the socket; must reconnect
    finally:
        await transport.close()
        await station.stop()
    assert first["battery_percentage"] == 78
    assert first["ac_charging_power_ios"] == 4  # second packet merged
    assert second["host_packet_data_jdb"]["host_packet_current"] == 5.12


@pytest.mark.asyncio
async def test_tcp_write_sends_encrypted_meta_encoded_value(fast_timeouts) -> None:
    station = FakeStation(close_after_reply=False)
    port = await station.start()
    transport = await _transport(port)
    try:
        await transport.write("ac_charging_power_ios", 4)
        await transport.write("ac_switch_hm", True)
    finally:
        await transport.close()
        await station.stop()
    assert station.writes == [ttlv.encode_num_field(51, 4), ttlv.encode_bool_field(56, True)]


@pytest.mark.asyncio
async def test_tcp_wrong_key_is_rejected(fast_timeouts) -> None:
    station = FakeStation()
    port = await station.start()
    transport = await _transport(port, base64.b64encode(b"\x09" * 16).decode())
    try:
        with pytest.raises(TransportError, match="login rejected"):
            await transport.read()
    finally:
        await transport.close()
        await station.stop()


@pytest.mark.asyncio
async def test_discovery_finds_station_by_auth_key(monkeypatch) -> None:
    station = FakeStation()
    port = await station.start()
    from custom_components.pecron.transport import tcp

    monkeypatch.setattr(tcp, "TCP_PORT", port)
    try:
        # /30 around 127.0.0.1 -> hosts 127.0.0.1 and 127.0.0.2; source is .2
        found = await discover_host("127.0.0.2", AUTH_B64, prefix=30)
        missing = await discover_host(
            "127.0.0.2", base64.b64encode(b"\x07" * 16).decode(), prefix=30
        )
    finally:
        await station.stop()
    assert found == "127.0.0.1"
    assert missing is None


@pytest.mark.asyncio
async def test_tcp_read_accepts_data_in_ack_reply(fast_timeouts) -> None:
    station = FakeStation(close_after_reply=False, reply_cmd=ttlv.CMD_ACK)
    port = await station.start()
    transport = await _transport(port)
    try:
        values = await transport.read()
    finally:
        await transport.close()
        await station.stop()
    assert values["battery_percentage"] == 78
    assert values["ac_charging_power_ios"] == 4


@pytest.mark.asyncio
async def test_tcp_read_starts_new_session_when_station_answers_once(
    fast_timeouts, monkeypatch
) -> None:
    from custom_components.pecron.transport import local

    monkeypatch.setattr(local, "PUSH_GRACE", 0.0)  # no recent data to fall back on
    station = FakeStation(close_after_reply=False)
    station.answer_once = True
    port = await station.start()
    transport = await _transport(port)
    try:
        first = await transport.read()
        second = await transport.read()
    finally:
        await transport.close()
        await station.stop()
    assert first["battery_percentage"] == 78
    assert second["battery_percentage"] == 78
    assert station.sessions == 2


@pytest.mark.asyncio
async def test_tcp_silent_read_uses_recent_values_and_requests_fast_reports(fast_timeouts) -> None:
    station = FakeStation(close_after_reply=False)
    station.answer_once = True
    port = await station.start()
    transport = await _transport(port)
    try:
        first = await transport.read()
        second = await transport.read()
    finally:
        await transport.close()
        await station.stop()
    assert second == first
    assert station.sessions == 1
    assert ttlv.encode_num_field(100, 3) in station.writes
