"""Shared encrypted TTLV session used by the BLE and Wi-Fi TCP transports."""

from __future__ import annotations

import asyncio
import base64
import logging
from abc import abstractmethod
from typing import Any

from .. import ttlv
from ..tsl_map import TslMap
from . import Transport, TransportError

_LOGGER = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 8.0
# Some models take well over 6 s before the first status frame arrives.
FIRST_REPORT_TIMEOUT = 15.0
# Some models (E3600/E3800) leave up to ~3 s between the packets of one
# multi-packet status reply.
REPORT_GAP_TIMEOUT = 3.0
WRITE_ACK_TIMEOUT = 5.0
POST_LOGIN_DELAY = 0.5  # some firmware drops reads sent immediately after login
LINK_CLOSED = -1  # sentinel frame command queued when the link drops
DATA_CMDS = {ttlv.CMD_ACK, ttlv.CMD_REPORT}


class LocalSession(Transport):
    """Handshake, encryption and read/write logic over an abstract byte link."""

    def __init__(self, device_key: str, auth_key_b64: str, tsl: TslMap) -> None:
        self.device_key = device_key
        self._auth_key = base64.b64decode(auth_key_b64)
        self._tsl = tsl
        self._frames: asyncio.Queue[ttlv.Frame] = asyncio.Queue()
        self._reassembler = ttlv.FrameReassembler()
        self._iv: bytes | None = None
        self._packet_id = 0
        self._lock = asyncio.Lock()
        self._login_reply = ""
        self._ignored: list[str] = []  # frames seen but not used, for diagnostics

    # -- link primitives implemented by subclasses --------------------------

    @abstractmethod
    async def _open(self) -> None:
        """Open the underlying link. Raise TransportError on failure."""

    @abstractmethod
    async def _send(self, data: bytes) -> None:
        """Send raw bytes."""

    @abstractmethod
    async def _close_link(self) -> None:
        """Close the underlying link."""

    @property
    @abstractmethod
    def _link_open(self) -> bool:
        """True while the underlying link is up."""

    # -- helpers ------------------------------------------------------------

    def _on_bytes(self, data: bytes) -> None:
        """Feed received bytes (called by subclasses)."""
        for raw in self._reassembler.feed(data):
            try:
                self._frames.put_nowait(ttlv.parse_frame(raw))
            except ttlv.TtlvError as err:
                _LOGGER.debug("%s: dropped frame: %s", self.name, err)

    def _on_link_lost(self) -> None:
        """Called by subclasses when the peer closes the link."""
        self._iv = None
        self._frames.put_nowait(ttlv.Frame(cmd=LINK_CLOSED, packet_id=0, payload=b""))

    def _next_id(self) -> int:
        self._packet_id = (self._packet_id + 1) % 0xFFFF
        return self._packet_id

    async def _wait_frame(self, cmds: set[int], timeout: float) -> ttlv.Frame:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TransportError(f"timed out waiting for {sorted(hex(c) for c in cmds)}")
            try:
                frame = await asyncio.wait_for(self._frames.get(), remaining)
            except TimeoutError as err:
                raise TransportError(
                    f"timed out waiting for {sorted(hex(c) for c in cmds)}"
                ) from err
            if frame.cmd == LINK_CLOSED:
                raise TransportError("link closed by the device")
            if frame.cmd in cmds:
                return frame
            _LOGGER.debug("%s: ignoring frame cmd 0x%04x", self.name, frame.cmd)
            self._ignored = [*self._ignored[-7:], f"0x{frame.cmd:04x}/{len(frame.payload)}B"]

    def _drain(self) -> list[ttlv.Frame]:
        frames = []
        while not self._frames.empty():
            frames.append(self._frames.get_nowait())
        return frames

    async def _ensure_session(self) -> None:
        if self._link_open and self._iv is not None:
            return
        await self._close_link()
        self._iv = None
        self._reassembler.clear()
        self._drain()
        await self._open()
        try:
            await self._send(ttlv.build_frame(ttlv.CMD_RANDOM_REQ, b"", self._next_id()))
            frame = await self._wait_frame({ttlv.CMD_RANDOM_RESP}, HANDSHAKE_TIMEOUT)
            random_str = ttlv.extract_random(frame.payload)
            if not random_str:
                raise TransportError("handshake: no random in 0x7033")
            await self._send(
                ttlv.build_frame(
                    ttlv.CMD_LOGIN_REQ,
                    ttlv.login_payload(self._auth_key, random_str),
                    self._next_id(),
                )
            )
            frame = await self._wait_frame({ttlv.CMD_LOGIN_RESP}, HANDSHAKE_TIMEOUT)
            self._login_reply = frame.payload.hex() or "empty"
            if not ttlv.login_ok(frame.payload):
                raise TransportError(
                    "login rejected: the cached auth key is no longer valid "
                    "(another tool may have regenerated it)"
                )
            self._iv = ttlv.make_iv(random_str)
            await asyncio.sleep(POST_LOGIN_DELAY)
        except Exception:
            await self._close_link()
            self._iv = None
            raise

    def _decode_reports(self, frames: list[ttlv.Frame], iv: bytes) -> list[ttlv.Field]:
        """Decode data from status replies.

        Most models answer a read with 0x0014 reports; some (BLE on several
        models, F5000LFP) put the data in the 0x0012 reply itself. Payloads are
        normally AES-CBC; fall back to plain TTLV when decryption fails.
        """
        fields: list[ttlv.Field] = []
        for frame in frames:
            if frame.cmd not in DATA_CMDS or not frame.payload:
                continue
            plain: bytes | None = None
            if len(frame.payload) >= 16 and len(frame.payload) % 16 == 0:
                try:
                    plain = ttlv.decrypt(self._auth_key, iv, frame.payload)
                except ValueError as err:
                    _LOGGER.debug("%s: undecryptable 0x%04x: %s", self.name, frame.cmd, err)
            # Only treat the payload as plain TTLV when it is not ciphertext.
            candidate = plain if plain is not None else frame.payload
            try:
                fields.extend(ttlv.parse_fields(candidate))
            except (ttlv.TtlvError, ValueError, IndexError) as err:
                _LOGGER.debug("%s: unparsable 0x%04x: %s", self.name, frame.cmd, err)
        return fields

    async def _collect_reply(self) -> list[ttlv.Frame]:
        """Send one read and gather every frame of the reply."""
        # Keep reports the station pushed on its own since the last poll.
        frames = [f for f in self._drain() if f.cmd in DATA_CMDS]
        await self._send(ttlv.build_frame(ttlv.CMD_READ, b"", self._next_id()))
        timeout = FIRST_REPORT_TIMEOUT
        while True:
            try:
                frames.append(await self._wait_frame(DATA_CMDS, timeout))
            except TransportError:
                break
            timeout = REPORT_GAP_TIMEOUT
        return frames

    # -- Transport API ------------------------------------------------------

    async def read(self) -> dict[str, Any]:
        async with self._lock:
            try:
                await self._ensure_session()
                # The device may close the link right after replying, which
                # clears the session IV; keep this session's IV for decoding.
                iv = self._iv
                assert iv is not None
                frames = await self._collect_reply()
                fields = self._decode_reports(frames, iv)
                if not fields and self._link_open:
                    # Some firmware needs a moment after login before it answers.
                    await asyncio.sleep(1.0)
                    frames += await self._collect_reply()
                    fields = self._decode_reports(frames, iv)
            except TransportError:
                raise
            except (OSError, ValueError) as err:
                await self._close_link()
                raise TransportError(str(err)) from err
            if not fields:
                # Leave the link open: some stations push 0x0014 reports on
                # their own while a client stays connected.
                seen = [f"0x{f.cmd:04x}/{len(f.payload)}B" for f in frames] + self._ignored
                self._ignored = []
                seen = ", ".join(seen) or "nothing"
                raise TransportError(
                    f"no data fields in the reply (received: {seen}; login reply: {self._login_reply})"
                )
            values = self._tsl.decode(fields)
            if not values:
                raise TransportError("reply contained no known properties")
            return values

    async def write(self, code: str, value: Any) -> None:
        try:
            plain = self._tsl.encode_write(code, value)
        except (KeyError, ValueError) as err:
            raise TransportError(str(err)) from err
        async with self._lock:
            try:
                await self._ensure_session()
                assert self._iv is not None
                self._drain()
                payload = ttlv.encrypt(self._auth_key, self._iv, plain)
                await self._send(ttlv.build_frame(ttlv.CMD_WRITE, payload, self._next_id()))
                await self._wait_frame({ttlv.CMD_ACK, ttlv.CMD_REPORT}, WRITE_ACK_TIMEOUT)
            except TransportError:
                raise
            except (OSError, ValueError) as err:
                await self._close_link()
                raise TransportError(str(err)) from err

    async def close(self) -> None:
        async with self._lock:
            await self._close_link()
            self._iv = None
