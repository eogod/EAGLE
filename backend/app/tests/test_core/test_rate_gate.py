"""The shared request gate: one lock per event loop, one interval per upstream."""

import asyncio

from app.core.rate_gate import MinIntervalGate


def test_gate_lock_follows_the_running_loop():
    """A gate outlives any single loop, so each loop needs its own lock."""
    gate = MinIntervalGate()

    first = asyncio.run(_lock_for(gate))
    second = asyncio.run(_lock_for(gate))

    assert first is not second


async def _lock_for(gate: MinIntervalGate) -> asyncio.Lock:
    return gate._lock_for_running_loop()
