"""
hot state of mythic tasks and payloads in redis,
statuses and events live here only as current state,
db keeps the durable record and is used for recovery
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import redis.asyncio as redis
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.core.database_session import get_async_session
from app.models import AttackStep, BashTask, MythicTask
from app.services.llm_service import llm_service

if TYPE_CHECKING:
    from app.cmd.c2_tool import MythicClient

logger = logging.getLogger(__name__)

TERMINAL = ("completed", "error", "finished", "cancelled")
TASK_EVENTS_ATTRS = "id display_id status completed"
# how long a step stream stays open with no fresh events before closing
STREAM_IDLE_SEC = 90
# gr a terminal status may still be followed by output & analysis
STREAM_GRACE_SEC = 20
# one xread block, low value just makes tests fast
# short block so a dead/idle redis read cannot eat the request; the loop
# re-polls and falls back to the database for a terminal state
STREAM_BLOCK_MS = 3_000


def _row_now() -> str:
    return datetime.now(UTC).isoformat()


def _snap(kind: str, **fields) -> dict:
    """one redis stream row, ndjson like frontend gets"""
    return {"event": kind, "at": _row_now(), **fields}


def _encode(snap: dict) -> dict:
    """streams store field/value dicts, single data field with ndjson inside"""
    return {"data": json.dumps(snap)}


def _decode(raw) -> str:
    """back from redis row to ndjson string"""
    if isinstance(raw, dict):
        return str(raw.get("data", ""))
    return str(raw)


def _ingest_rows(state: dict[str, dict], stream_key: str, rows) -> list[str]:
    """apply one read batch to the shared state, return rows for the client."""
    out: list[str] = []
    for row_id, raw in rows:
        state[stream_key]["last"] = row_id
        row = _decode(raw)
        event = json.loads(row)
        if event.get("event") == "status" and event.get("completed"):
            state[stream_key]["terminal_t"] = time.time()
        if event.get("event") in ("output", "build", "analysis") and (
            state[stream_key].get("terminal_t") is None
        ):
            state[stream_key]["terminal_t"] = time.time() - (STREAM_GRACE_SEC - 2)
        out.append(row)

    return out


def _expire_idle(
    state: dict[str, dict], start_t: float, now_t: float, step_id: int
) -> None:
    """drop settled-by-grace and stale-idle streams so the read loop can end."""
    for k in sorted(state):
        s = state[k]
        if s["terminal_t"] and now_t - s["terminal_t"] > STREAM_GRACE_SEC:
            del state[k]
        elif not s["terminal_t"] and now_t - start_t > STREAM_IDLE_SEC:
            logger.info("step %s stream %s idle, closing", step_id, k)
            del state[k]


class MythicEventsService:
    """single mythic subscription -> redis -> sse & mcp consumers"""

    def __init__(self):
        self._redis: redis.Redis | None = None
        self._sub_task: asyncio.Task | None = None
        self._mythic_client: MythicClient | None = None
        self._watchers: set[asyncio.Task] = set()
        self.available = False

    @property
    def _instance(self):
        if self._mythic_client is not None:
            return self._mythic_client.instance
        return None

    def bind_client(self, mythic_client: MythicClient) -> None:
        """reuse the same mythic websocket instance as c2_tool"""
        self._mythic_client = mythic_client

    @property
    def _mythic_cfg(self) -> dict[str, Any]:
        c = get_settings().mythic
        return {
            "username": c.username,
            "password": c.password.get_secret_value(),
            "server_ip": c.server_ip,
            "server_port": c.server_port,
            "timeout": -1,
        }

    async def start(self) -> None:
        """connect redis, then subscribe to mythic, recovery is done by
        mythic replay + db, so no redis persistence is needed"""
        cfg = get_settings().redis
        self._redis = redis.Redis(
            host=cfg.hostname,
            port=cfg.port,
            password=cfg.password.get_secret_value(),
            decode_responses=True,
        )
        try:
            await self._redis.ping()
        except Exception as e:
            logger.warning("redis is not available, events live in logs only: %s", e)
            return
        self.available = True
        self._sub_task = asyncio.create_task(self._subscribe_loop())
        self._spawn(self._recover_stale())

    async def stop(self) -> None:
        if self._sub_task:
            self._sub_task.cancel()
        for t in tuple(self._watchers):
            t.cancel()
        if self._redis:
            await self._redis.aclose()

    @staticmethod
    def task_key(mythic_task_id: int) -> str:
        return f"eagle:task:{mythic_task_id}"

    @staticmethod
    def bash_task_key(bash_task_id: int) -> str:
        """same stream/hash contract as mythic tasks, own namespace"""
        return f"eagle:bash:{bash_task_id}"

    @staticmethod
    def payload_key(payload_uuid: str) -> str:
        return f"eagle:payload:{payload_uuid}"

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._watchers.add(t)
        t.add_done_callback(self._watchers.discard)
        return t

    async def _subscribe_loop(self) -> None:
        """one mythic task subscription, no reconnect retry"""
        from mythic import mythic  # noqa: PLC0415

        try:
            instance = self._instance or await mythic.login(**self._mythic_cfg)
            async for task in mythic.subscribe_all_tasks_and_updates(
                mythic=instance, custom_return_attributes=TASK_EVENTS_ATTRS
            ):
                await self._on_task_event(task)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("task subscription failed: %s", e)

    async def _recover_stale(self) -> None:
        """tickets left in db by a dead process get a redis row again,
        so sse consumers do not hang on expired keys"""
        if not self.available:
            return
        try:
            async with get_async_session() as session:
                rows = await session.execute(
                    select(MythicTask).where(MythicTask.status == "submitted")
                )
                task_ids = [r.mythic_task_id for r in rows.scalars()]
        except Exception as e:
            # db is another process, events service itself must not die for it
            logger.warning("stale ticket recovery skipped: %s", e)
            return
        for task_id in task_ids:
            await self._emit_task(task_id, "submitted", completed=False)
        try:
            async with get_async_session() as session:
                bash_rows = await session.execute(
                    select(BashTask).where(
                        BashTask.status.in_(("submitted", "running"))
                    )
                )
                stale = list(bash_rows.scalars())
                step_ids = [row.step_id for row in stale]
                if step_ids:
                    # the worker lived in the dead process: it can never finish
                    await session.execute(
                        update(BashTask)
                        .where(BashTask.id.in_([row.id for row in stale]))
                        .values(status="failed", completed_at=func.now())
                    )
                    await session.execute(
                        update(AttackStep)
                        .where(
                            AttackStep.id.in_(step_ids),
                            AttackStep.status.in_(("submitted", "running")),
                        )
                        .values(status="fail")
                    )

                # placeholders left "running" by a dead client/stream have no
                # task row; fail them unless Mythic still owns the step
                active_mythic = select(MythicTask.step_id).where(
                    MythicTask.status.in_(("submitted", "running", "dispatched"))
                )
                await session.execute(
                    update(AttackStep)
                    .where(
                        AttackStep.status.in_(("submitted", "running")),
                        ~AttackStep.id.in_(active_mythic),
                    )
                    .values(status="fail")
                )
                await session.commit()

                for row in stale:
                    await self.emit_bash_status(row.id, "failed", completed=True)
        except Exception as e:
            logger.warning("bash stale recovery skipped: %s", e)
            return
        logger.info("recovered %s stale tickets into redis", len(task_ids))

    async def _on_task_event(self, task: dict) -> None:
        task_id = task.get("display_id")
        if task_id is None:
            return
        status = str(task.get("status") or "submitted").lower()
        terminal = bool(task.get("completed")) or status in TERMINAL
        await self._emit_task(task_id, status, completed=terminal)
        if terminal:
            self._spawn(self._collect_output(int(task_id), status))

    async def _write_state(self, stream_key: str, status: str, snap: dict) -> None:
        """write one status row to the stream & hash, ttl like the contract"""
        if not self.available:
            return
        hash_key = stream_key.removesuffix(":events")
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xadd(stream_key, _encode(snap))
            pipe.hset(hash_key, mapping={"status": status})
            ttl = get_settings().redis.events_ttl
            pipe.expire(hash_key, ttl)
            pipe.expire(stream_key, ttl)
            await pipe.execute()

    async def _emit_task(
        self, task_id: int, status: str, completed: bool = False
    ) -> None:
        k = self.task_key(task_id)
        await self._write_state(
            f"{k}:events",
            status,
            _snap("status", task_id=task_id, status=status, completed=completed),
        )

    async def _collect_output(
        self, task_id: int, event_status: str = "completed"
    ) -> None:
        """after terminal, aggregate output and save to db,
        llm analysis if the ticket was asked for it at run time"""
        from mythic import mythic  # noqa: PLC0415

        try:
            instance = self._instance or await mythic.login(**self._mythic_cfg)
            output = await mythic.waitfor_for_task_output(
                mythic=instance, task_display_id=task_id
            )
            text_out = str(output)
            auto_analyze, status, step_id = await self._persist_output(
                task_id, text_out, event_status
            )
            # always, even for tasks created outside our ticket flow
            await self._write_state(
                f"{self.task_key(task_id)}:events",
                status,
                _snap("output", task_id=task_id, raw_log=text_out, step_id=step_id),
            )
            if auto_analyze and step_id is not None:
                self._spawn(self._auto_analyze(task_id, step_id, text_out))
        except Exception as e:  # general exception cuz mythic lib
            logger.warning("output collection for task %s failed: %s", task_id, e)

    async def _persist_output(
        self, task_id: int, output: str, event_status: str = "completed"
    ) -> tuple[bool, str, int | None]:
        """save final state & output to mythic_task and attack_step durable"""
        auto_analyze, final_status, step_id = False, "completed", None
        async with get_async_session() as session:
            row = await session.scalar(
                select(MythicTask).where(MythicTask.mythic_task_id == task_id)
            )
            if row is None:
                # task not created here (like custom flows), keep it in redis only
                return False, event_status or "completed", None
            auto_analyze = row.auto_analyze
            failed = event_status in ("error", "cancelled")
            final_status = "failed" if failed else "completed"
            row.status = final_status
            row.output = output
            row.completed_at = datetime.now(UTC)
            if row.step_id:
                step_id = row.step_id
                step = await session.scalar(
                    select(AttackStep).where(AttackStep.id == row.step_id)
                )
                if step is not None:
                    step.raw_log = output
                    step.status = "fail" if failed else "success"
            await session.commit()
        return auto_analyze, final_status, step_id

    async def _auto_analyze(self, task_id: int, step_id: int, output: str) -> None:
        """run llm analysis on step output, save & emit like the others,
        only when the caller asked for it at submission time"""
        analysis = ""
        try:
            analysis = await llm_service.query_llm(
                f"command:\noutput: {output}\n"
                "analyze results and find most important things."
            )
        except Exception as e:
            analysis = f"error while analysis LLM: {str(e)}"
        await self._persist_analysis(step_id, analysis)
        await self._redis.xadd(
            f"{self.task_key(task_id)}:events",
            _encode(
                _snap(
                    "analysis", task_id=task_id, step_id=step_id, llm_analysis=analysis
                )
            ),
        )

    async def _persist_analysis(self, step_id: int, analysis: str) -> None:
        async with get_async_session() as session:
            step = await session.scalar(
                select(AttackStep).where(AttackStep.id == step_id)
            )
            if step is None:
                return
            step.llm_analysis = analysis
            await session.commit()

    async def emit_analysis(self, step_id: int, analysis: str) -> None:
        """mirror llm analysis to all task streams of the step"""
        if not self.available:
            return
        keys = await self._task_keys_for_step(step_id)
        raw = _encode(_snap("analysis", step_id=step_id, llm_analysis=analysis))
        for key in keys:
            await self._redis.xadd(key, raw)

    async def watch_payload(self, payload_uuid: str) -> None:
        """background payload build watcher, statuses go to redis & db"""
        self._spawn(self._payload_watch_loop(payload_uuid))

    async def _payload_watch_loop(self, payload_uuid: str) -> None:
        from mythic import mythic  # noqa: PLC0415

        try:
            instance = self._instance or await mythic.login(**self._mythic_cfg)
            # subscription waits until build_phase != building
            result = await mythic.waitfor_payload_complete(
                mythic=instance, payload_uuid=payload_uuid
            )
            build_phase = (result or {}).get("build_phase", "error")
            build_log = str((result or {}).get("build_message") or "")
            await self._write_state(
                f"{self.payload_key(payload_uuid)}:events",
                build_phase,
                _snap(
                    "build",
                    payload_uuid=payload_uuid,
                    status=build_phase,
                    raw_log=build_log,
                ),
            )
            await self._persist_payload_build(payload_uuid, build_phase, build_log)
        except Exception as e:
            logger.warning("payload watch for %s failed: %s", payload_uuid, e)

    async def _persist_payload_build(
        self, payload_uuid: str, build_phase: str, build_log: str
    ) -> None:
        """save final build state on step rows & mirror to their streams"""
        async with get_async_session() as session:
            rows = await session.execute(
                select(AttackStep).where(AttackStep.mythic_payload_uuid == payload_uuid)
            )
            step_ids = []
            for step in rows.scalars():
                step.status = build_phase
                step.raw_log = build_log
                step_ids.append(step.id)
            await session.commit()
        for step_id in step_ids:
            for key in await self._task_keys_for_step(step_id):
                await self._redis.xadd(
                    key,
                    _encode(
                        _snap(
                            "build",
                            step_id=step_id,
                            status=build_phase,
                            raw_log=build_log,
                        )
                    ),
                )

    async def _task_keys_for_step(self, step_id: int) -> list[str]:
        async with get_async_session() as session:
            rows = await session.execute(
                select(MythicTask).where(MythicTask.step_id == step_id)
            )
            keys = [f"{self.task_key(r.mythic_task_id)}:events" for r in rows.scalars()]
            bash_rows = await session.execute(
                select(BashTask).where(BashTask.step_id == step_id)
            )
            keys += [f"{self.bash_task_key(r.id)}:events" for r in bash_rows.scalars()]
            return keys

    def spawn_bash_worker(self, coro) -> None:
        """fire-and-forget bash worker ref, same watcher set as mythic loops"""
        self._spawn(coro)

    async def emit_bash_status(
        self, bash_task_id: int, status: str, completed: bool = False
    ) -> None:
        """bash version of task status rows, same event shape as mythic"""
        await self._write_state(
            f"{self.bash_task_key(bash_task_id)}:events",
            status,
            _snap("status", task_id=bash_task_id, status=status, completed=completed),
        )

    async def emit_bash_output(
        self, bash_task_id: int, output: str, status: str, step_id: int
    ) -> None:
        """single output row when the run finishes, hiders use step_id in event"""
        await self._redis.xadd(
            f"{self.bash_task_key(bash_task_id)}:events",
            _encode(
                _snap(
                    "output",
                    task_id=bash_task_id,
                    raw_log=output,
                    status=status,
                    step_id=step_id,
                )
            ),
        )

    async def follow_task(self, mythic_task_id: int) -> AsyncGenerator[str]:
        """iterate task events from redis, from the current moment"""
        async for raw in self._follow(self.task_key(mythic_task_id) + ":events"):
            yield raw

    async def _follow(self, stream_key: str) -> AsyncGenerator[str]:
        """iterate one stream from the current moment, shared by both namespaces"""
        if not self.available:
            return
        key = stream_key
        last_id = "$"
        while True:
            resp = await self._redis.xread({key: last_id}, count=10, block=10_000)
            if not resp:
                continue
            for stream, rows in resp:
                for row_id, raw in rows:
                    if stream != key:
                        break
                    last_id = row_id
                    yield _decode(raw)

    async def wait_for_task(self, mythic_task_id: int, timeout: int = 300) -> str:
        """bounded wait like old blocking way, returns final status or timeout"""

        async def _inner() -> str:
            async for raw in self._follow(self.task_key(mythic_task_id) + ":events"):
                event = json.loads(raw)
                if event.get("completed") or event.get("status") in TERMINAL:
                    return event.get("status", "completed")
            return "fail"

        try:
            return await asyncio.wait_for(_inner(), timeout)
        except TimeoutError:
            return "timeout"

    async def wait_for_bash(self, bash_task_id: int, timeout: int = 300) -> str:
        """same bounded wait for direct bash tasks in the bash namespace"""
        status = "submitted"

        async def _inner() -> str:
            nonlocal status
            async for raw in self._follow(self.bash_task_key(bash_task_id) + ":events"):
                event = json.loads(raw)
                if event.get("event") == "status":
                    status = str(event.get("status") or status)
                if event.get("completed") or event.get("status") in TERMINAL:
                    return event.get("status", "completed")
            return status

        try:
            return await asyncio.wait_for(_inner(), timeout)
        except TimeoutError:
            return "timeout"

    async def wait_for_task_output(  # noqa: PLR0913, PLR0917
        self, mythic_task_id: int, timeout: int = 300, grace: int = 15
    ) -> tuple[str, str]:
        """like wait_for_task but collects raw output too,
        gr a status may still be followed by output rows"""
        out, status = "", "submitted"
        completed_at = None
        started = asyncio.get_running_loop().time()

        async def _inner() -> tuple[str, str]:
            nonlocal out, status, completed_at
            async for raw in self.follow_task(mythic_task_id):
                event = json.loads(raw)
                loop_t = asyncio.get_running_loop().time()
                completed = (
                    bool(event.get("completed")) or event.get("status") in TERMINAL
                )
                if completed and completed_at is None:
                    completed_at = loop_t
                if event.get("event") == "output":
                    out += str(event.get("raw_log") or "")
                    return out, str(event.get("status") or status)
                if event.get("event") == "status":
                    status = str(event.get("status") or status)
                # keep following after terminal, output collector emits soon
                if completed and (loop_t - completed_at) > grace:
                    return out, status
                if loop_t - started > timeout:
                    return out, status
            return out, status

        try:
            return await asyncio.wait_for(_inner(), timeout + grace)
        except TimeoutError:
            return out, status

    async def get_task_status(self, mythic_task_id: int) -> dict | None:
        """current status from redis hot state, mythic first then bash, or None"""
        if not self.available:
            return None
        hash_map = await self._redis.hgetall(self.task_key(mythic_task_id))
        if hash_map:
            return hash_map
        bash_map = await self._redis.hgetall(self.bash_task_key(mythic_task_id))
        return bash_map if bash_map else None

    async def step_stream_keys(self, step_id: int) -> list[str]:
        """event stream keys of step: all task streams + payload stream"""
        keys = await self._task_keys_for_step(step_id)
        async with get_async_session() as session:
            step = await session.scalar(
                select(AttackStep).where(AttackStep.id == step_id)
            )
            # bash/local steps carry a placeholder uuid but no payload; their
            # payload stream never exists and would keep this stream open
            if step is not None and step.mythic_payload_id:
                keys.append(f"{self.payload_key(step.mythic_payload_uuid)}:events")

        return keys

    async def _terminal_from_db(self, step_id: int) -> dict[str, Any] | None:
        """last resort when redis events were missed: report the settled row."""
        async with get_async_session() as session:
            step = await session.get(AttackStep, step_id)
            if step is None or step.status not in ("success", "fail", "error"):
                return None
            if step.raw_log:
                return {
                    "event": "output",
                    "step_id": step_id,
                    "status": "completed" if step.status == "success" else "failed",
                    "raw_log": step.raw_log,
                }
            return {
                "event": "status",
                "step_id": step_id,
                "status": "completed" if step.status == "success" else "failed",
                "completed": True,
            }

    async def read_streams(
        self, last: dict[str, str], block_ms: int = 15_000
    ) -> list[tuple[str, list[tuple[str, str]]]]:
        """multi-stream xread by caller-owned last ids, low level for sse"""
        if not self.available:
            return []
        return await self._redis.xread(last, count=50, block=block_ms)

    async def stream_step_events(
        self, step_id: int, snapshot: dict
    ) -> AsyncGenerator[str]:
        """ndjson stream of one step: snapshot first, then live task & payload
        events; a stream closes after terminal rows settle (grace), or if it
        stays idle too long, so the client re-snapshots instead of hanging"""
        yield json.dumps({"event": "snapshot", "step": snapshot}) + "\n"
        keys = await self.step_stream_keys(step_id)
        if not keys or not self.available:
            return
        # replay from the beginning: a fast task can emit every event between
        # the snapshot above and the first XREAD, so "$" would miss all of them
        state = {k: {"last": "0-0", "terminal_t": None} for k in keys}
        start_t = time.time()

        def all_settled() -> bool:
            return not state

        while not all_settled():
            live_last = {k: s["last"] for k, s in state.items()}
            try:
                resp = await self.read_streams(live_last, block_ms=STREAM_BLOCK_MS)
            except Exception as exc:
                # redis hiccup must not kill the response after the snapshot
                logger.warning("step %s stream read failed: %s", step_id, exc)
                resp = None

            now_t = time.time()
            if not resp:
                fallback = await self._terminal_from_db(step_id)
                if fallback is not None:
                    yield json.dumps(fallback) + "\n"
                    return

                _expire_idle(state, start_t, now_t, step_id)
                continue

            for stream_key, rows in resp:
                if stream_key not in state:
                    continue
                # NDJSON: every row needs its own newline or the client
                # buffers all of them until the stream closes
                for row in _ingest_rows(state, stream_key, rows):
                    yield f"{row}\n"
