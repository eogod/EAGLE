"""stream-level session re-checks for guarded_stream and the cancel websocket"""

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from app.api.endpoints.tasks import cancel_chain_ws
from app.services import auth_guard
from app.services.auth_guard import StreamGuard, guarded_stream

pytestmark = pytest.mark.asyncio(loop_scope="session")

TERMINAL = {"event": "auth.revoked"}


class FakeStream:
    """inner async iterator that records aclose and consumed items"""

    def __init__(self, items: list[str] | None = None) -> None:
        self.items = list(items or [])
        self.closed = False

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> str:
        if not self.items:
            raise StopAsyncIteration
        return self.items.pop(0)

    async def aclose(self) -> None:
        self.closed = True


class IdleStream:
    """never produces an event, like a chain waiting on a long command"""

    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self) -> IdleStream:
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


async def _collect(inner, guard: StreamGuard) -> list[object]:
    return [item async for item in guarded_stream(inner, guard)]


async def test_guard_drops_stream_before_first_item_when_session_is_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def dead_session(_session_id: int, _user_id: str) -> bool:
        return False

    monkeypatch.setattr(auth_guard, "session_active", dead_session)
    revoked: list[int] = []
    stream = FakeStream(["secret-1", "secret-2"])

    events = await _collect(
        stream,
        StreamGuard(
            session_id=7,
            user_id="user-1",
            interval=0.01,
            terminal=TERMINAL,
            on_revoke=lambda: revoked.append(7),
        ),
    )

    assert events == [TERMINAL]
    assert revoked == [7]
    assert stream.closed
    # no data leaked out of an already revoked session
    assert stream.items == ["secret-1", "secret-2"]


async def test_guard_delivers_items_until_session_dies_then_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks: Iterator[float] = iter([1.0, 2.0, 3.0, 4.0])
    monkeypatch.setattr(
        auth_guard, "time", SimpleNamespace(monotonic=lambda: next(ticks))
    )
    answers: Iterator[bool] = iter([True, True, False])
    checked: list[tuple[int, str]] = []

    async def fake_session_active(session_id: int, user_id: str) -> bool:
        checked.append((session_id, user_id))
        return next(answers)

    monkeypatch.setattr(auth_guard, "session_active", fake_session_active)
    revoked: list[int] = []
    stream = FakeStream(["a", "b", "c"])

    events = await _collect(
        stream,
        StreamGuard(
            session_id=7,
            user_id="user-1",
            interval=0.5,
            terminal=TERMINAL,
            on_revoke=lambda: revoked.append(7),
        ),
    )

    assert events == ["a", "b", TERMINAL]
    assert revoked == [7]
    assert stream.closed
    assert checked == [(7, "user-1")] * 3


async def test_guard_rechecks_on_timeout_when_stream_is_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: Iterator[bool] = iter([True, False])

    async def fake_session_active(_session_id: int, _user_id: str) -> bool:
        return next(answers)

    monkeypatch.setattr(auth_guard, "session_active", fake_session_active)
    revoked: list[int] = []
    stream = IdleStream()

    events = await _collect(
        stream,
        StreamGuard(
            session_id=7,
            user_id="user-1",
            interval=0.01,
            terminal=TERMINAL,
            on_revoke=lambda: revoked.append(7),
        ),
    )

    # an eventless stream is exactly when the re-check must still run
    assert events == [TERMINAL]
    assert revoked == [7]
    assert stream.closed


class SlowStream:
    """one item arrives after several idle checks, like a slow chain step"""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.sent = False
        self.closed = False

    def __aiter__(self) -> SlowStream:
        return self

    async def __anext__(self) -> str:
        if self.sent:
            raise StopAsyncIteration
        await asyncio.sleep(self.delay)
        self.sent = True
        return "slow"

    async def aclose(self) -> None:
        self.closed = True


async def test_guard_does_not_cancel_a_slow_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a timeout is an idle check, not a reason to close the generator."""

    async def live_session(_session_id: int, _user_id: str) -> bool:
        return True

    monkeypatch.setattr(auth_guard, "session_active", live_session)
    stream = SlowStream(delay=0.08)

    events = await _collect(
        stream,
        StreamGuard(session_id=7, user_id="user-1", interval=0.01),
    )

    assert events == ["slow"]
    assert stream.closed


async def test_guard_wraps_a_true_async_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """async generator asends are awaitables, not coroutine objects."""

    async def live_session(_session_id: int, _user_id: str) -> bool:
        return True

    async def source():
        yield "one"
        await asyncio.sleep(0.05)
        yield "two"

    monkeypatch.setattr(auth_guard, "session_active", live_session)
    events = await _collect(
        source(), StreamGuard(session_id=7, user_id="user-1", interval=0.01)
    )

    assert events == ["one", "two"]


async def test_guard_clean_end_emits_no_terminal_and_closes_inner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def live_session(_session_id: int, _user_id: str) -> bool:
        return True

    monkeypatch.setattr(auth_guard, "session_active", live_session)
    revoked: list[int] = []
    stream = FakeStream(["a", "b"])

    events = await _collect(
        stream,
        StreamGuard(
            session_id=7,
            user_id="user-1",
            interval=1,
            terminal=TERMINAL,
            on_revoke=lambda: revoked.append(7),
        ),
    )

    assert events == ["a", "b"]
    assert revoked == []
    assert stream.closed


class FakeWebSocket:
    def __init__(self, replies: list[object]) -> None:
        self.replies = replies
        self.accepted = False
        self.closed: list[int] = []
        self.state = SimpleNamespace(auth_session_id=11, auth_user_id="user-1")

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)

    async def receive_text(self) -> str:
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply  # type: ignore[return-value]


class FakeChainService:
    def __init__(self, verified: bool = True) -> None:
        self.verified = verified
        self.calls: list[tuple[int, str, object]] = []

    async def verify_chain_name(
        self, chain_id: int, chain_name: str, current_user: object
    ) -> bool:
        self.calls.append((chain_id, chain_name, current_user))
        return self.verified


class FakeChainController:
    def __init__(self) -> None:
        self.cancelled: list[int] = []

    def cancel_chain(self, chain_id: int) -> None:
        self.cancelled.append(chain_id)


async def test_cancel_chain_ws_closes_4401_and_cancels_when_session_died(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def dead_session(session_id: int, user_id: str) -> bool:
        return False

    monkeypatch.setattr("app.api.endpoints.tasks.session_active", dead_session)
    user = object()
    ws = FakeWebSocket([TimeoutError()])
    chain_service = FakeChainService()
    controller = FakeChainController()

    await cancel_chain_ws(
        ws,  # type: ignore[arg-type]
        42,
        current_user=user,  # type: ignore[arg-type]
        chain_service=chain_service,  # type: ignore[arg-type]
        chain_controller=controller,  # type: ignore[arg-type]
    )

    assert ws.accepted
    assert ws.closed == [4401]
    assert controller.cancelled == [42]
    # a dead session cannot even confirm a chain name
    assert chain_service.calls == []


async def test_cancel_chain_ws_cancels_verified_chain_without_closing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[tuple[int, str]] = []

    async def fake_session_active(session_id: int, user_id: str) -> bool:
        checked.append((session_id, user_id))
        return True

    monkeypatch.setattr("app.api.endpoints.tasks.session_active", fake_session_active)
    user = object()
    ws = FakeWebSocket(["night-run"])
    chain_service = FakeChainService(verified=True)
    controller = FakeChainController()

    await cancel_chain_ws(
        ws,  # type: ignore[arg-type]
        42,
        current_user=user,  # type: ignore[arg-type]
        chain_service=chain_service,  # type: ignore[arg-type]
        chain_controller=controller,  # type: ignore[arg-type]
    )

    assert ws.accepted
    assert chain_service.calls == [(42, "night-run", user)]
    assert controller.cancelled == [42]
    # an event arrived, so no timeout re-check and no auth close happened
    assert checked == []
    assert ws.closed == []


async def test_cancel_chain_ws_ignores_a_wrong_chain_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def dead_session(_session_id: int, _user_id: str) -> bool:
        return False

    monkeypatch.setattr("app.api.endpoints.tasks.session_active", dead_session)
    user = object()
    ws = FakeWebSocket(["guess"])
    chain_service = FakeChainService(verified=False)
    controller = FakeChainController()

    await cancel_chain_ws(
        ws,  # type: ignore[arg-type]
        42,
        current_user=user,  # type: ignore[arg-type]
        chain_service=chain_service,  # type: ignore[arg-type]
        chain_controller=controller,  # type: ignore[arg-type]
    )

    assert chain_service.calls == [(42, "guess", user)]
    assert controller.cancelled == []
    assert ws.closed == []
