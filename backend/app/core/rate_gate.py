"""Shared min-interval gate for unauthenticated upstream APIs.

DDG and NVD throttle per client rather than per process, so one gate is shared
per upstream and every call waits on it.
"""

from __future__ import annotations

import asyncio
import time


class MinIntervalGate:
    """monotonic min-interval gate between matching requests; sleeps while
    holding the lock so callers are fully serialized"""

    def __init__(self) -> None:
        self.last = 0.0
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _lock_for_running_loop(self) -> asyncio.Lock:
        """a lock binds to the loop that first waits on it, and module-level
        gates outlive any single loop, so every loop gets its own lock"""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock = asyncio.Lock()
            self._loop = loop

        return self._lock

    async def wait(self, interval: float) -> None:
        """snooze until interval has passed since the previous call"""
        async with self._lock_for_running_loop():
            delay = interval - (time.monotonic() - self.last)
            if delay > 0:
                await asyncio.sleep(delay)

            self.last = time.monotonic()
