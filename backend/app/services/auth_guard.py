"""re-check a session while an sse/ws stream is already open"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from app.core import database_session
from app.services.auth_sessions import active_session


@dataclass(frozen=True)
class StreamGuard:
    session_id: int
    user_id: str
    interval: float
    terminal: Any = None
    on_revoke: Callable[[], None] | None = None


async def session_active(session_id: int, user_id: str) -> bool:
    # the stream outlives the request session, so each check opens its own
    async with database_session.get_async_session() as db:
        return await active_session(db, session_id, user_id) is not None


async def _close(
    iterator: AsyncIterator[Any], pending: asyncio.Future[Any] | None
) -> None:
    if pending is not None and not pending.done():
        pending.cancel()
        # a producer that ignores cancellation must not hang stream close
        with suppress(asyncio.CancelledError, StopAsyncIteration, TimeoutError):
            await asyncio.wait_for(pending, timeout=1)
    close = getattr(iterator, "aclose", None)
    if close is not None:
        await close()


async def guarded_stream[T](
    inner: AsyncIterator[T], guard: StreamGuard
) -> AsyncIterator[T]:
    """yield from inner and close it once the session dies"""
    iterator = inner.__aiter__()
    pending: asyncio.Future[T] | None = None
    last_check = 0.0
    try:
        while True:
            now = time.monotonic()
            if last_check == 0.0 or now - last_check >= guard.interval:
                if not await session_active(guard.session_id, guard.user_id):
                    if guard.on_revoke is not None:
                        guard.on_revoke()
                    if guard.terminal is not None:
                        yield guard.terminal
                    return
                last_check = now

            if pending is None:
                # a timeout must not cancel the producer: wait_for would close
                # the generator and silently end a slow chain step
                # ensure_future accepts async-generator asends; create_task does not
                pending = asyncio.ensure_future(iterator.__anext__())

            done, _ = await asyncio.wait({pending}, timeout=guard.interval)
            if pending not in done:
                # no events yet; loop back and re-check the session
                continue

            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None

            yield item
    finally:
        await _close(iterator, pending)
