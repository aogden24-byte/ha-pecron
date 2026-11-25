"""Wi-Fi TCP transport (port 6607) and LAN discovery."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging

from .. import ttlv
from ..tsl_map import TslMap
from . import TRANSPORT_TCP, TransportError
from .local import HANDSHAKE_TIMEOUT, LocalSession

_LOGGER = logging.getLogger(__name__)

TCP_PORT = 6607
CONNECT_TIMEOUT = 5.0


class TcpTransport(LocalSession):
    """Encrypted TTLV over TCP. The firmware usually closes the socket after each reply."""

    name = TRANSPORT_TCP

    def __init__(self, host: str, device_key: str, auth_key_b64: str, tsl: TslMap) -> None:
        super().__init__(device_key, auth_key_b64, tsl)
        self.host = host
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._rx_task: asyncio.Task | None = None

    @property
    def _link_open(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def _open(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, TCP_PORT), CONNECT_TIMEOUT
            )
        except (OSError, TimeoutError) as err:
            raise TransportError(f"cannot connect to {self.host}:{TCP_PORT}: {err}") from err
        self._rx_task = asyncio.get_running_loop().create_task(self._rx_loop(self._reader))

    async def _rx_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                self._on_bytes(data)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            if reader is self._reader:
                self._on_link_lost()

    async def _send(self, data: bytes) -> None:
        if self._writer is None:
            raise TransportError("not connected")
        self._writer.write(data)
        await self._writer.drain()

    async def _close_link(self) -> None:
        writer, task = self._writer, self._rx_task
        self._reader = self._writer = None
        self._rx_task = None
        if task is not None:
            task.cancel()
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, TimeoutError):
                pass


# ---------------------------------------------------------------------------
# LAN discovery
# ---------------------------------------------------------------------------


async def _probe(host: str, auth_key: bytes) -> bool:
    """True if ``host`` answers on 6607 and accepts this device's auth key."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, TCP_PORT), 1.5)
    except (OSError, TimeoutError):
        return False
    reassembler = ttlv.FrameReassembler()

    async def next_frame(cmd: int) -> ttlv.Frame | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HANDSHAKE_TIMEOUT
        while loop.time() < deadline:
            data = await asyncio.wait_for(reader.read(1024), deadline - loop.time())
            if not data:
                return None
            for raw in reassembler.feed(data):
                frame = ttlv.parse_frame(raw)
                if frame.cmd == cmd:
                    return frame
        return None

    try:
        writer.write(ttlv.build_frame(ttlv.CMD_RANDOM_REQ, b"", 1))
        await writer.drain()
        frame = await next_frame(ttlv.CMD_RANDOM_RESP)
        random_str = ttlv.extract_random(frame.payload) if frame else None
        if not random_str:
            return False
        writer.write(
            ttlv.build_frame(ttlv.CMD_LOGIN_REQ, ttlv.login_payload(auth_key, random_str), 2)
        )
        await writer.drain()
        frame = await next_frame(ttlv.CMD_LOGIN_RESP)
        return frame is not None and ttlv.login_ok(frame.payload)
    except (OSError, TimeoutError, ttlv.TtlvError):
        return False
    finally:
        writer.close()


async def discover_host(source_ip: str | None, auth_key_b64: str, prefix: int = 24) -> str | None:
    """Scan the local subnet for the device whose auth key opens a session."""
    if not source_ip:
        return None
    try:
        network = ipaddress.ip_network(f"{source_ip}/{prefix}", strict=False)
    except ValueError:
        return None
    auth_key = base64.b64decode(auth_key_b64)
    sem = asyncio.Semaphore(48)

    async def check(host: str) -> str | None:
        async with sem:
            return host if await _probe(host, auth_key) else None

    hosts = [str(h) for h in network.hosts() if str(h) != source_ip]
    for result in await asyncio.gather(*(check(h) for h in hosts)):
        if result:
            _LOGGER.info("Found Pecron device at %s", result)
            return result
    return None
