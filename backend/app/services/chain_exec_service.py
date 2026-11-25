import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.timing import runtime_delay
from app.cmd.c2_tool import MythicClient
from app.cmd.proc import (
    CallbackInfo,
    ChainContext,
    PayloadParams,
    process_approved_cmd,
)
from app.core import database_session
from app.models import Agent, AttackStep
from app.services.llm_service import analyze_command_output_with_llm
from app.services.step_reuse import apply_execution, reusable

logger = logging.getLogger(__name__)

# a silent ndjson response is dropped by proxies long before a 20 minute wait ends
WAIT_TICK_SECS = 30


def _event(kind: str, **fields: object) -> str:
    return json.dumps({"type": kind, **fields}, default=str) + "\n"


def _until(seconds: int) -> str:
    """wall clock end of the wait, so the browser counts down from the server."""
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


class ChainExecutionService:
    def __init__(self, session: AsyncSession, mythic_client: MythicClient) -> None:
        self.session = session
        self.mythic_client = mythic_client

    async def perform_chain_step(
        self,
        steps: list[AttackStep],
        zero_display_id: int,
        cancel_event: asyncio.Event,
        timing: bool = False,
    ) -> AsyncGenerator[str]:
        # TODO: accept from_step and skip lists so a long chain can resume mid-way.
        # TODO: add a chain deadline and fail_fast switch; one failed step does not stop the run.
        for step in steps:
            if cancel_event.is_set():
                break
            # timing is opt-in: without it the chain replays back to back
            async for chunk in self._wait(step, timing, cancel_event):
                yield chunk
            if cancel_event.is_set():
                break
            try:
                # a wait can last 20 minutes, so the step session opens after it
                async with database_session.get_async_session() as session:
                    line = await self._execute_step(session, step, zero_display_id)
                yield line
            except Exception as exc:
                # TODO: apply a per-step on_error policy (stop, retry, goto) instead of always continuing.
                # one bad step must not kill the whole stream: report and continue
                detail = getattr(exc, "detail", None) or str(exc)
                logger.warning("chain step %s failed: %s", step.id, detail)
                yield (
                    json.dumps(
                        {
                            "AttackStep": {
                                "step_id": step.id,
                                "chain_id": step.chain_id,
                                "phase": step.phase,
                                "tool_name": step.tool_name,
                                "status": "error",
                                "raw_log": str(detail),
                                "command": step.command,
                            },
                            "error": str(detail),
                        },
                        default=str,
                    )
                    + "\n"
                )

    async def _wait(
        self, step: AttackStep, timing: bool, cancel_event: asyncio.Event
    ) -> AsyncGenerator[str]:
        """the pause before a step, interruptible and ticking while it lasts."""
        # TODO: add a wait_for step (new callback, open port, log line) separate from dwell.
        seconds = runtime_delay(step.delay_before_secs)
        if not timing or not step.delay_enabled or seconds <= 0:
            return

        yield _event(
            "wait.started",
            step_id=step.id,
            phase=step.phase,
            seconds=seconds,
            reason=step.delay_reason or "",
            until=_until(seconds),
        )
        remaining = seconds
        while remaining > 0:
            try:
                await asyncio.wait_for(
                    cancel_event.wait(), timeout=min(WAIT_TICK_SECS, remaining)
                )
            except TimeoutError:
                remaining -= WAIT_TICK_SECS
                if remaining > 0:
                    yield _event(
                        "wait.tick",
                        step_id=step.id,
                        remaining_secs=remaining,
                        until=_until(remaining),
                    )
                continue
            yield _event("wait.cancelled", step_id=step.id, remaining_secs=remaining)
            return

        yield _event("wait.finished", step_id=step.id)

    async def _execute_step(
        self, session: AsyncSession, step: AttackStep, zero_display_id: int
    ) -> str:
        res_agent = await session.execute(select(Agent).where(Agent.step_id == step.id))
        c_agent = res_agent.scalars().first()
        if c_agent:
            display_id = c_agent.callback_display_id
            p_os_type = c_agent.os_type
        else:
            display_id = zero_display_id
            p_os_type = "Windows"
        # TODO: substitute chain vars and captured output before building the command.
        ctx = ChainContext(chain_id=step.chain_id, phase_name=step.phase)
        cb = CallbackInfo(
            display_id=display_id,
            step_type=step.step_type,
            tool_name=step.tool_name,
        )
        result, llm_a, agent_c, mythic_task = await process_approved_cmd(
            cmd=step.command,
            ctx=ctx,
            cb=cb,
            payload_params=PayloadParams(os_type=p_os_type),
            mythic_client=self.mythic_client,
        )

        # TODO: evaluate an expect rule (exit code, regex, file, session) before marking success.
        # custom steps return raw tuples, not steps, keep them as is
        row = result
        if isinstance(result, AttackStep):
            target = reusable(step, step.chain_id)
            if target is not None:
                # same row, same id, same place in the chain
                row = apply_execution(target, result)
            else:
                self.session.add(result)
            try:
                await self.session.commit()
                if mythic_task is not None:
                    mythic_task.step_id = row.id
                    self.session.add(mythic_task)
                    await self.session.commit()
            except Exception as e:
                logger.warning("chain step %s save failed: %s", step.id, e)

        if isinstance(result, AttackStep):
            resp_step = {
                "step_id": row.id,
                "chain_id": row.chain_id,
                "phase": row.phase,
                "tool_name": row.tool_name,
                "mythic_payload_uuid": row.mythic_payload_uuid,
                "status": row.status,
                "raw_log": row.raw_log,
                "command": row.command,
            }
        else:
            # custom steps have no AttackStep row, so there is no later
            # analyze endpoint to fill llm_analysis; do it before streaming
            strings_out = " ".join(i for i in result if isinstance(i, str))
            llm_a = await analyze_command_output_with_llm(strings_out, step.command)
            resp_step = {
                "step_id": None,
                "chain_id": step.chain_id,
                "phase": step.phase,
                "tool_name": step.tool_name,
                "mythic_payload_uuid": None,
                "status": "executed",
                "raw_log": strings_out,
                "command": step.command,
            }
        out_d = {
            "AttackStep": resp_step,
            "llm_analysis": llm_a,
        }

        return json.dumps(out_d, default=str) + "\n"

    @staticmethod
    def get_sorted_steps(steps: list[AttackStep]) -> list[AttackStep]:
        """every step with a command, in chain order.

        Executed rows are replayed in place (same id/order), so running a
        chain twice does not duplicate steps.
        """
        executable = [
            s
            for s in steps
            if s.status not in ("submitted", "running", "rejected")
            and not s.is_draft
            and s.command.strip()
        ]
        return sorted(executable, key=lambda step: step.id)
