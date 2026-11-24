"""run one stored step for an approved run proposal"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Agent, AttackChain, AttackStep, User
from app.services.bash_service import BashCommandService
from app.services.c2_service import CommandsC2ExecService

StepRunner = Callable[[AttackStep], Awaitable[dict[str, Any]]]


def make_step_runner(
    session: AsyncSession,
    chain: AttackChain,
    user: User,
    events: Any,
    mythic: Any,
) -> StepRunner:
    """bind the request session, chain and user to one step runner"""

    async def run_step(step: AttackStep) -> dict[str, Any]:
        if step.step_type == "bash":
            if events is None:
                raise RuntimeError("bash executor is not running")
            data = await BashCommandService(session, events).start(
                step.command,
                chain.chain_name,
                user,
                timeout=get_settings().local_bash.timeout,
                step_id=step.id,
            )
            return {
                "status": data["status"],
                "new_step_id": data["step_id"],
            }
        if mythic is None:
            raise RuntimeError("Mythic is not connected")
        c_agent = await session.scalar(select(Agent).where(Agent.step_id == step.id))
        display_id = c_agent.callback_display_id if c_agent else 0
        c2 = CommandsC2ExecService(session, mythic)
        if step.step_type == "agent":
            data = await c2.run_agent_command(
                step.command,
                display_id,
                step.tool_name,
                chain.id,
                step.phase,
                user,
                step_id=step.id,
            )
        else:
            data = await c2.run_local_command(
                step.command,
                display_id,
                chain.id,
                step.phase,
                user,
                step_id=step.id,
            )
        return {"status": data["status"], "new_step_id": data["step_id"]}

    return run_step
