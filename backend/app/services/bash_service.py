"""
service for direct bash runs: persist AttackStep + BashTask,
execute in background, live status/output in redis like mythic tasks
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cmd.local_bash import (
    BashDisabled,
    BashError,
    BashRejected,
    check_reject,
    run_bash,
)
from app.cmd.mythic_events import MythicEventsService
from app.core.config import get_settings
from app.core.database_session import get_async_session
from app.models import AttackStep, BashTask, User
from app.services.chain_service import ChainService
from app.services.step_reuse import apply_execution, reusable

logger = logging.getLogger(__name__)


class BashCommandService:
    """direct bash: one POST = one ticket like run-command for zero agent,
    only the worker is here, not the C2"""

    def __init__(self, session: AsyncSession, events: MythicEventsService) -> None:
        self.session = session
        self.events = events

    async def start(  # noqa: PLR0913, PLR0917
        self,
        command: str,
        chain_name: str,
        current_user: User,
        timeout: int = 300,
        step_id: int | None = None,
    ) -> dict[str, Any]:
        """validate + persist submitted rows + spawn worker, returns ticket"""
        if not get_settings().local_bash.enabled:
            raise HTTPException(
                403,
                "local bash is disabled, set LOCAL_BASH__ENABLED=1 in .env "
                "or use zero agent via run-command",
            )
        try:
            check_reject(command)
        except BashRejected as e:
            raise HTTPException(400, str(e)) from e
        _, chain_id, phase_name = await ChainService(self.session).get_chain_n_phase(
            chain_name, current_user
        )
        cfg = get_settings().local_bash
        timeout = min(int(timeout or cfg.timeout), cfg.max_timeout)
        produced = AttackStep(
            chain_id=chain_id,
            phase=phase_name,
            step_type="bash",
            tool_name=command.split(maxsplit=1)[0] or "bash",
            command=command,
            mythic_task_id=0,  # no C2 ticket, live state is bash_task via redis
            mythic_payload_id=0,
            # schema needs a value, nothing was built
            mythic_payload_uuid=str(uuid.uuid4()),
            raw_log="",
            status="submitted",
        )

        step = None
        if step_id is not None:
            existing = await self.session.scalar(
                select(AttackStep).where(
                    AttackStep.id == step_id,
                    AttackStep.chain_id == chain_id,
                )
            )
            step = reusable(existing, chain_id)
        if step is not None:
            # execute the row the operator clicked; id and order stay stable
            apply_execution(step, produced)
            step.status = "submitted"
            step.executed_at = func.now()
        else:
            step = produced
            self.session.add(step)

        # step.id is assigned on flush; without it BashTask.step_id stays NULL
        await self.session.flush()
        bash_task = BashTask(step_id=step.id, status="submitted")
        self.session.add(bash_task)
        try:
            await self.session.commit()
            await self.session.refresh(bash_task)
        except Exception as e:
            logger.warning("bash step save failed: %s", e)
            await self.session.rollback()
            raise HTTPException(400, "bash step save failed") from e
        # background worker, ref kept by the events service like its own loops
        self.events.spawn_bash_worker(
            self._execute(bash_task.id, step.id, chain_id, command, timeout)
        )
        return {
            "chain_name": chain_name,
            "step_id": step.id,
            "bash_task_id": bash_task.id,
            "command": command,
            "status": bash_task.status,
        }

    async def wait_result(  # noqa: PLR0913, PLR0917
        self, bash_task_id: int, timeout: int = 300
    ) -> dict[str, Any]:
        """bounded wait for terminal row + fresh snapshot, used by MCP wait mode;
        committed rows short-circuit so no redis hang when valkey is down"""
        row = await self._read_row(bash_task_id)
        if row is None or row.status not in ("completed", "failed"):
            await self.events.wait_for_bash(bash_task_id, timeout)
            row = await self._read_row(bash_task_id)
        if row is None:
            return {"bash_task_id": bash_task_id, "status": "unknown"}
        return {
            "bash_task_id": bash_task_id,
            "step_id": row.step_id,
            "status": row.status,
            "output": row.output or "",
            # cut output, exit_code of the shell, both from the committed row
            "exit_code": row.exit_code,
            "duration_ms": row.duration_ms,
            "full_log_path": row.full_log_path,
        }

    @staticmethod
    async def _read_row(bash_task_id: int) -> BashTask | None:
        """fresh committed state of one bash task"""
        async with get_async_session() as session:
            return await session.get(BashTask, bash_task_id)

    async def _execute(
        self,
        bash_task_id: int,
        step_id: int,
        chain_id: int,
        command: str,
        timeout: int,
    ) -> None:
        """worker: run bash, persist db first, then emit to redis, so
        sse readers never get an output row without a committed one"""
        logger.info("bash task %s started on step %s", bash_task_id, step_id)
        await self.events.emit_bash_status(bash_task_id, "running")
        status, output, exit_code, duration_ms, log_path = (
            "failed",
            "",
            None,
            None,
            None,
        )
        try:
            # TODO: pass env and cwd from the request into run_bash; it already accepts cwd.
            result = await run_bash(
                command,
                timeout=timeout,
                chain_id=chain_id,
                log_name=f"bash_{step_id}_{bash_task_id}.log",
            )
            status = "completed" if result.exit_code == 0 else "failed"
            output, exit_code = result.output, result.exit_code
            duration_ms, log_path = result.duration_ms, result.full_log_path
        except BashDisabled as e:
            output = f"[DISABLED] {e}"
        except BashError as e:  # got here past start(), keep the record anyway
            output = f"[REJECTED] {e}"
        except Exception as e:  # noqa: BLE001  # worker must not die silently
            output = f"[BASH ERROR] {type(e).__name__}: {e}"
            logger.error("bash task %s failed: %s", bash_task_id, e, exc_info=True)
        await self._persist(
            bash_task_id, step_id, status, output, exit_code, duration_ms, log_path
        )
        await self.events.emit_bash_status(bash_task_id, status, completed=True)
        await self.events.emit_bash_output(bash_task_id, output, status, step_id)
        logger.info("bash task %s finished: %s", bash_task_id, status)

    async def _persist(  # noqa: PLR0913, PLR0917
        self,
        bash_task_id: int,
        step_id: int,
        status: str,
        output: str,
        exit_code: int | None,
        duration_ms: int | None,
        log_path: str | None,
    ) -> None:
        """separate session like events service persistence, not the request one"""
        try:
            async with get_async_session() as session:
                task = await session.get(BashTask, bash_task_id)
                if task is None:
                    return
                task.status = status
                task.output = output
                task.exit_code = exit_code
                task.duration_ms = duration_ms
                task.full_log_path = log_path
                task.completed_at = datetime.now(UTC)
                step = await session.get(AttackStep, step_id)
                if step is not None:
                    step.status = "fail" if status == "failed" else "success"
                    step.raw_log = output
                await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.error("bash persist %s failed: %s", bash_task_id, e, exc_info=True)
