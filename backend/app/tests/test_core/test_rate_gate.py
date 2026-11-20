"""The shared request gate: one lock per event loop, one interval per upstream."""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.rate_gate import MinIntervalGate


def test_gate_lock_follows_the_running_loop():
    """A gate outlives any single loop, so each loop needs its own lock."""
    gate = MinIntervalGate()

    first = asyncio.run(_lock_for(gate))
    second = asyncio.run(_lock_for(gate))

    assert first is not second


def test_gate_sleeps_the_remaining_interval(monkeypatch):
    """Upstream throttles per client: a call too soon must actually wait."""
    slept: list[float] = []
    clock = {"now": 0.0}

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    # only the gate's view of the clock is faked, the event loop keeps real time
    monkeypatch.setattr(
        "app.core.rate_gate.time", SimpleNamespace(monotonic=lambda: clock["now"])
    )
    monkeypatch.setattr("app.core.rate_gate.asyncio.sleep", fake_sleep)

    gate = MinIntervalGate()
    asyncio.run(gate.wait(0.5))
    clock["now"] = 0.4
    asyncio.run(gate.wait(0.5))

    # first call waits the whole interval, second only what is left of it
    assert slept == pytest.approx([0.5, 0.1])
    assert gate.last == pytest.approx(0.4)


async def _lock_for(gate: MinIntervalGate) -> asyncio.Lock:
    return gate._lock_for_running_loop()
