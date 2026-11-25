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
FIRST_REPORT_TIMEOUT = 6.0
# Some models (E3600/E3800) leave up to ~3 s between the packets of one
# multi-packet status reply.
REPORT_GAP_TIMEOUT = 3.0
WRITE_ACK_TIMEOUT = 5.0
POST_LOGIN_DELAY = 0.5  # some firmware drops reads sent immediately after login
LINK_CLOSED = -1  # sentinel frame command queued when the link drops


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
        fields: list[ttlv.Field] = []
        for frame in frames:
            if frame.cmd != ttlv.CMD_REPORT or not frame.payload:
                continue
            try:
                plain = ttlv.decrypt(self._auth_key, iv, frame.payload)
            except ValueError as err:
                _LOGGER.debug("%s: undecryptable report: %s", self.name, err)
                continue
            fields.extend(ttlv.parse_fields(plain))
        return fields

    # -- Transport API ------------------------------------------------------

    async def read(self) -> dict[str, Any]:
        async with self._lock:
            try:
                await self._ensure_session()
                # The device may close the link right after replying, which
                # clears the session IV; keep this session's IV for decoding.
                iv = self._iv
                assert iv is not None
                # Reports the device pushed since the last poll are still valid data.
                frames = [f for f in self._drain() if f.cmd == ttlv.CMD_REPORT]
                await self._send(ttlv.build_frame(ttlv.CMD_READ, b"", self._next_id()))
                timeout = FIRST_REPORT_TIMEOUT
                while True:
                    try:
                        frames.append(await self._wait_frame({ttlv.CMD_REPORT}, timeout))
                    except TransportError:
                        break
                    timeout = REPORT_GAP_TIMEOUT
                fields = self._decode_reports(frames, iv)
            except TransportError:
                raise
            except (OSError, ValueError) as err:
                await self._close_link()
                raise TransportError(str(err)) from err
            if not fields:
                await self._close_link()
                raise TransportError("no data fields in the reply")
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
