"""TTLV framing, field codec and crypto."""

import pytest

from custom_components.pecron import ttlv


def test_frame_roundtrip_with_stuffing() -> None:
    payload = bytes([0xAA, 0x55, 0xAA, 0xAA, 0x01])
    raw = ttlv.build_frame(ttlv.CMD_REPORT, payload, packet_id=0xAAAA)
    assert raw.count(b"\xaa\x55") >= 1  # stuffing applied
    frame = ttlv.parse_frame(raw)
    assert frame.cmd == ttlv.CMD_REPORT
    assert frame.packet_id == 0xAAAA
    assert frame.payload == payload


def test_reassembler_handles_split_and_merged_frames() -> None:
    a = ttlv.build_frame(ttlv.CMD_ACK, b"\x01\x02", 1)
    b = ttlv.build_frame(ttlv.CMD_REPORT, bytes([0xAA, 0x55]) * 10, 2)
    stream = b"\x00junk" + a + b
    r = ttlv.FrameReassembler()
    frames = []
    for i in range(0, len(stream), 3):
        frames += r.feed(stream[i : i + 3])
    assert [ttlv.parse_frame(f).cmd for f in frames] == [ttlv.CMD_ACK, ttlv.CMD_REPORT]
    assert ttlv.parse_frame(frames[1]).payload == bytes([0xAA, 0x55]) * 10


@pytest.mark.parametrize(
    ("value", "decimals"),
    [(0, 0), (1, 0), (255, 0), (256, 0), (-5, 0), (53.9, 1), (13.6, 1), (-0.25, 2), (100000, 0)],
)
def test_num_roundtrip(value, decimals) -> None:
    payload = ttlv.encode_num_field(7, value, decimals)
    (field,) = ttlv.parse_fields(payload)
    assert field.fid == 7
    assert field.kind == "NUM"
    assert field.value == pytest.approx(value)


def test_num_write_has_meta_byte() -> None:
    # Enum write of 4 to data point 51: tag, meta (1 byte, no sign/decimals), value.
    assert ttlv.encode_num_field(51, 4) == bytes([0x01, 0x9A, 0x00, 0x04])


def test_bool_and_bytes_and_struct_parse() -> None:
    payload = (
        ttlv.encode_bool_field(38, True)
        + ttlv.encode_bool_field(40, False)
        + ttlv.encode_bytes_field(52, b"hello")
    )
    fields = ttlv.parse_fields(payload)
    assert [(f.fid, f.kind, f.value) for f in fields] == [
        (38, "BOOL", True),
        (40, "BOOL", False),
        (52, "BYTES", b"hello"),
    ]


def test_crypto_roundtrip_and_iv() -> None:
    key = bytes(range(16))
    iv = ttlv.make_iv("7DAFD8E50291D9D7")
    assert len(iv) == 16
    data = b"some fields"
    assert ttlv.decrypt(key, iv, ttlv.encrypt(key, iv, data)) == data


def test_login_helpers() -> None:
    payload = ttlv.encode_bytes_field(1, b"9DE748C937B1C0CD")
    assert ttlv.extract_random(payload) == "9DE748C937B1C0CD"
    proof = ttlv.parse_fields(ttlv.login_payload(b"\x01" * 16, "abc"))
    assert proof[0].fid == 2 and len(proof[0].value) == 64
    assert ttlv.login_ok(ttlv.encode_num_field(3, 0))
    assert not ttlv.login_ok(ttlv.encode_num_field(3, -1))
