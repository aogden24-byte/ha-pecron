"""Transports for Pecron devices: BLE, Wi-Fi TCP and cloud."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

TRANSPORT_BLE = "ble"
TRANSPORT_TCP = "tcp"
TRANSPORT_CLOUD = "cloud"
TRANSPORT_NONE = "none"
ALL_TRANSPORTS = (TRANSPORT_BLE, TRANSPORT_TCP, TRANSPORT_CLOUD)


class TransportError(Exception):
    """A transport could not complete a read or write."""


class Transport(ABC):
    """One path to one device. Implementations raise TransportError on failure."""

    name: str

    @abstractmethod
    async def read(self) -> dict[str, Any]:
        """Return the device's current values as ``{tsl_code: value}``."""

    @abstractmethod
    async def write(self, code: str, value: Any) -> None:
        """Write one property. Raise TransportError if it was not accepted."""

    async def close(self) -> None:  # noqa: B027 - optional hook
        """Release any connection held by this transport."""
