"""Priority selector: failover, demotion, probing and promotion."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.pecron import coordinator as coord
from custom_components.pecron.const import LINK_FAIL_LIMIT, LINK_PROBE_BACKOFF_START
from custom_components.pecron.coordinator import DeviceLink, configured_priorities
from custom_components.pecron.transport import Transport, TransportError


class FakeTransport(Transport):
    def __init__(self, name: str, ok: bool = True) -> None:
        self.name = name
        self.ok = ok
        self.reads = 0
        self.writes: list = []

    async def read(self):
        self.reads += 1
        if not self.ok:
            raise TransportError(f"{self.name} down")
        return {"battery_percentage": 50, "source": self.name}

    async def write(self, code, value):
        if not self.ok:
            raise TransportError(f"{self.name} down")
        self.writes.append((code, value))


@pytest.fixture
def clock(monkeypatch):
    now = SimpleNamespace(t=1000.0)
    monkeypatch.setattr(coord.time, "monotonic", lambda: now.t)
    return now


def make_link(*transports: FakeTransport) -> DeviceLink:
    device = MagicMock(device_key="dk", device_name="F5000")
    return DeviceLink(device, [], list(transports))


@pytest.mark.asyncio
async def test_uses_highest_priority(clock) -> None:
    ble, tcp, cloud = FakeTransport("ble"), FakeTransport("tcp"), FakeTransport("cloud")
    link = make_link(ble, tcp, cloud)
    values = await link.poll()
    assert values["source"] == "ble"
    assert link.active == "ble"
    assert tcp.reads == 0
    # The cloud is read only to fill properties BLE never reports; it does not
    # override values the active link delivers.
    assert cloud.reads == 1


@pytest.mark.asyncio
async def test_failure_falls_through_in_same_poll(clock) -> None:
    ble, tcp = FakeTransport("ble", ok=False), FakeTransport("tcp")
    link = make_link(ble, tcp)
    values = await link.poll()
    assert values["source"] == "tcp"
    assert link.health["ble"].state == "degraded"
    assert not link.health["ble"].down


@pytest.mark.asyncio
async def test_demotion_then_backoff_probe_then_promotion(clock) -> None:
    ble, tcp = FakeTransport("ble", ok=False), FakeTransport("tcp")
    link = make_link(ble, tcp)
    for _ in range(LINK_FAIL_LIMIT):
        await link.poll()
    assert link.health["ble"].down
    reads_when_down = ble.reads

    # While down and before the probe time, BLE is not attempted at all.
    clock.t += 10
    await link.poll()
    assert ble.reads == reads_when_down
    assert link.active == "tcp"

    # Probe time reached, BLE still failing: backoff doubles.
    clock.t += LINK_PROBE_BACKOFF_START
    await link.poll()
    assert ble.reads == reads_when_down + 1
    assert link.health["ble"].backoff == LINK_PROBE_BACKOFF_START * 2

    # BLE recovers: next probe promotes it back.
    ble.ok = True
    clock.t += LINK_PROBE_BACKOFF_START * 2
    values = await link.poll()
    assert values["source"] == "ble"
    assert link.health["ble"].state == "ok"


@pytest.mark.asyncio
async def test_all_down_returns_none_and_goes_stale(clock) -> None:
    link = make_link(FakeTransport("tcp"))
    await link.poll()
    assert link.snapshot() is not None
    link.transports[0].ok = False
    for _ in range(coord.STALE_POLL_LIMIT):
        assert await link.poll() is None
        assert link.snapshot() is not None  # last values kept during the grace period
    await link.poll()
    assert link.snapshot() is None


@pytest.mark.asyncio
async def test_write_prefers_active_then_falls_back(clock) -> None:
    ble, tcp, cloud = FakeTransport("ble", ok=False), FakeTransport("tcp"), FakeTransport("cloud")
    link = make_link(ble, tcp, cloud)
    await link.poll()  # tcp becomes active
    assert await link.write("ac_switch_hm", True) == "tcp"
    tcp.ok = False
    assert await link.write("ac_switch_hm", False) == "cloud"
    assert cloud.writes == [("ac_switch_hm", False)]


def test_priority_options_allow_disabling() -> None:
    entry = MagicMock(options={"priority_1": "tcp", "priority_2": "none", "priority_3": "tcp"})
    assert configured_priorities(entry) == ["tcp"]
    entry = MagicMock(options={})
    assert configured_priorities(entry) == ["ble", "tcp", "cloud"]
