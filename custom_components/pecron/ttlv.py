"""Quectel TTLV framing and field codec used by Pecron local transports.

Wire format (shared by Wi-Fi TCP port 6607 and BLE characteristic 0x9C40):

    AA AA | LEN(2, BE) | CRC(1) | PACKET_ID(2) | CMD(2) | PAYLOAD

``LEN`` counts CRC + packet id + command + payload. ``CRC`` is the byte sum
of packet id + command + payload. Everything after the ``AA AA`` sync word is
byte-stuffed: a 0x55 is inserted after any 0xAA that is followed by 0x55 or
0xAA. ``LEN`` counts unstuffed bytes.

After the login handshake the payloads of read/write/report commands are
AES-128-CBC encrypted (PKCS7) with the device auth key and the 16-byte IV
the device sends during the handshake.

Protocol facts in this module were established by the MIT-licensed
pecron-monitor project (attractify-logan/pecron-monitor).
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SYNC = b"\xaa\xaa"

CMD_READ = 0x0011
CMD_ACK = 0x0012
CMD_WRITE = 0x0013
CMD_REPORT = 0x0014
CMD_RANDOM_REQ = 0x7032
CMD_RANDOM_RESP = 0x7033
CMD_LOGIN_REQ = 0x7034
CMD_LOGIN_RESP = 0x7035

T_BOOL_FALSE = 0
T_BOOL_TRUE = 1
T_NUM = 2
T_BYTES = 3
T_STRUCT = 4
T_BYTES_ALT = 5


class TtlvError(Exception):
    """Malformed TTLV data."""


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def stuff(frame: bytes) -> bytes:
    """Apply byte stuffing to a frame (sync word excluded)."""
    out = bytearray(frame[:2])
    for i in range(2, len(frame)):
        out.append(frame[i])
        if frame[i] == 0xAA and i + 1 < len(frame) and frame[i + 1] in (0x55, 0xAA):
            out.append(0x55)
    return bytes(out)


def unstuff(frame: bytes) -> bytes:
    """Remove byte stuffing from a frame (sync word excluded)."""
    out = bytearray(frame[:2])
    i = 2
    while i < len(frame):
        out.append(frame[i])
        if frame[i] == 0xAA and i + 1 < len(frame) and frame[i + 1] == 0x55:
            i += 2
        else:
            i += 1
    return bytes(out)


def build_frame(cmd: int, payload: bytes = b"", packet_id: int = 1) -> bytes:
    """Build a complete, stuffed frame."""
    inner = struct.pack(">HH", packet_id & 0xFFFF, cmd) + payload
    crc = sum(inner) & 0xFF
    return stuff(SYNC + struct.pack(">H", len(inner) + 1) + bytes([crc]) + inner)


@dataclass
class Frame:
    """A parsed frame."""

    cmd: int
    packet_id: int
    payload: bytes


def parse_frame(raw: bytes) -> Frame:
    """Parse one stuffed frame (starting at the sync word)."""
    data = unstuff(raw)
    if len(data) < 9 or data[:2] != SYNC:
        raise TtlvError(f"bad frame: {raw[:16].hex()}")
    length = struct.unpack(">H", data[2:4])[0]
    end = 4 + length
    if len(data) < end:
        raise TtlvError("truncated frame")
    inner = data[5:end]
    # The CRC byte (data[4]) is not verified: the proven reference
    # implementation never checks it, and dropping a frame on a CRC quirk
    # would lose data. AES-CBC + PKCS7 unpadding rejects corrupt payloads.
    packet_id, cmd = struct.unpack(">HH", inner[:4])
    return Frame(cmd=cmd, packet_id=packet_id, payload=bytes(inner[4:]))


class FrameReassembler:
    """Incrementally split a byte stream into stuffed frames.

    Used for both TCP (stream) and BLE (indications may split or merge
    frames arbitrarily).
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Add bytes; return any complete raw (stuffed) frames."""
        self._buf += data
        frames: list[bytes] = []
        while True:
            start = self._buf.find(SYNC)
            if start < 0:
                # Keep a trailing 0xAA in case it is half of a sync word.
                self._buf = self._buf[-1:] if self._buf[-1:] == b"\xaa" else bytearray()
                return frames
            if start:
                del self._buf[:start]
            end = _stuffed_frame_end(self._buf)
            if end is None:
                return frames
            frames.append(bytes(self._buf[:end]))
            del self._buf[:end]

    def clear(self) -> None:
        self._buf.clear()


def _stuffed_frame_end(buf: bytes | bytearray) -> int | None:
    """Return the end index of the stuffed frame at ``buf[0]``, or None if incomplete."""
    unstuffed = 0
    needed = 2
    length_bytes = bytearray()
    prev: int | None = None
    i = 2
    while unstuffed < needed:
        if i >= len(buf):
            return None
        byte = buf[i]
        i += 1
        if prev == 0xAA and byte == 0x55:
            prev = None
            continue
        unstuffed += 1
        prev = byte
        if needed == 2:
            length_bytes.append(byte)
            if len(length_bytes) == 2:
                needed = 2 + struct.unpack(">H", bytes(length_bytes))[0]
    return i


# ---------------------------------------------------------------------------
# Field codec
# ---------------------------------------------------------------------------


@dataclass
class Field:
    """A decoded TTLV field. ``kind`` is BOOL, NUM, BYTES or STRUCT."""

    fid: int
    kind: str
    value: Any


def parse_fields(payload: bytes) -> list[Field]:
    """Decode a flat list of TTLV fields. STRUCT fields carry a child count."""
    fields: list[Field] = []
    i = 0
    n = len(payload)
    while i + 2 <= n:
        tag = struct.unpack(">H", payload[i : i + 2])[0]
        fid = (tag >> 3) & 0x1FFF
        ftype = tag & 0x07
        i += 2
        if ftype in (T_BOOL_FALSE, T_BOOL_TRUE):
            fields.append(Field(fid, "BOOL", ftype == T_BOOL_TRUE))
        elif ftype == T_NUM:
            if i >= n:
                break
            meta = payload[i]
            i += 1
            negative = bool(meta & 0x80)
            decimals = (meta >> 3) & 0x0F
            count = (meta & 0x07) + 1
            if i + count > n:
                break
            raw = int.from_bytes(payload[i : i + count], "big")
            i += count
            value: int | float = -raw if negative else raw
            if decimals:
                value = round(value / (10**decimals), decimals)
            fields.append(Field(fid, "NUM", value))
        elif ftype in (T_BYTES, T_BYTES_ALT):
            if i + 2 > n:
                break
            length = struct.unpack(">H", payload[i : i + 2])[0]
            i += 2
            fields.append(Field(fid, "BYTES", bytes(payload[i : i + length])))
            i += length
        elif ftype == T_STRUCT:
            if i + 2 > n:
                break
            count = struct.unpack(">H", payload[i : i + 2])[0]
            i += 2
            fields.append(Field(fid, "STRUCT", count))
        else:
            break
    return fields


def encode_num(value: int | float, decimals: int = 0) -> bytes:
    """Encode a NUM value: meta byte (sign, decimals, byte count) + big-endian magnitude.

    The device parses writes with the same meta byte it uses in reports;
    a bare value byte is read as the meta byte and the write is dropped.
    """
    scaled = round(float(value) * (10**decimals)) if decimals else int(value)
    negative = scaled < 0
    magnitude = abs(scaled)
    count = max(1, (magnitude.bit_length() + 7) // 8)
    if count > 8:
        raise TtlvError(f"value {value} too large")
    meta = (0x80 if negative else 0) | ((decimals & 0x0F) << 3) | (count - 1)
    return bytes([meta]) + magnitude.to_bytes(count, "big")


def encode_bool_field(fid: int, value: bool) -> bytes:
    return struct.pack(">H", (fid << 3) | (T_BOOL_TRUE if value else T_BOOL_FALSE))


def encode_num_field(fid: int, value: int | float, decimals: int = 0) -> bytes:
    return struct.pack(">H", (fid << 3) | T_NUM) + encode_num(value, decimals)


def encode_bytes_field(fid: int, data: bytes) -> bytes:
    return struct.pack(">H", (fid << 3) | T_BYTES) + struct.pack(">H", len(data)) + data


# ---------------------------------------------------------------------------
# Handshake + crypto
# ---------------------------------------------------------------------------


def login_payload(auth_key: bytes, random_str: str) -> bytes:
    """Login proof: SHA-256("<authkey hex>;<random>") as an ASCII hex bytes field (id 2)."""
    digest = hashlib.sha256(f"{auth_key.hex()};{random_str}".encode()).hexdigest()
    return encode_bytes_field(2, digest.encode())


def extract_random(payload: bytes) -> str | None:
    """Pull the random/IV string (bytes field id 1) from a 0x7033 payload."""
    for f in parse_fields(payload):
        if f.fid == 1 and f.kind == "BYTES":
            return f.value.decode("utf-8", "replace")
    return None


def login_ok(payload: bytes) -> bool:
    """A 0x7035 payload is a success when no NUM field is non-zero."""
    return all(not (f.kind == "NUM" and f.value != 0) for f in parse_fields(payload))


def make_iv(random_str: str) -> bytes:
    raw = random_str.encode()
    return raw[:16].ljust(16, b"\x00")


def encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


def decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(data) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()
