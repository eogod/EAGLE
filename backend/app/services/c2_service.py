from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cmd.c2_tool import MythicClient
from app.cmd.proc import (
    CallbackInfo,
    ChainContext,
    PayloadParams,
    check_and_process_agent_cmd,
    check_and_process_local_cmd,
    get_agent_status,
    process_approved_cmd,
)
from app.models import AttackChain, AttackStep, BashTask, MythicTask, User
from app.services.llm_service import analyze_command_output_with_llm
from app.services.step_reuse import apply_execution, reusable


class CommandsC2ExecService:
    def __init__(
        self,
        session: AsyncSession,
        mythic_client: MythicClient,
    ) -> None:
        self.session = session
        self.mythic_client = mythic_client

    async def run_local_command(  # noqa: PLR0913, PLR0917
        self,
        command: str,
        display_id: int,
        chain_id: int,
        phase_name: str,
        current_user: User,
        is_analyze_llm: bool = False,
        step_id: int | None = None,
    ) -> dict[str, Any]:
        """execute bash/C2 command via local agent, returns ticket now,
        raw_output & llm_analysis updates by events service via stream"""
        ctx = ChainContext(chain_id=chain_id, phase_name=phase_name)
        produced, _, mythic_task = await check_and_process_local_cmd(
            command, display_id, ctx, self.mythic_client, is_analyze_llm
        )

        step = await self._reuse_step(step_id, chain_id, produced)
        if step is None:
            step = produced
            self.session.add(step)
        try:
            await self.session.commit()
            mythic_task.step_id = step.id
            self.session.add(mythic_task)
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise HTTPException(status_code=400) from exc
        return {
            "user_id": current_user.user_id,
            "callback_display_id": display_id,
            "step_id": step.id,
            "mythic_task_id": step.mythic_task_id,
            "tool_name": step.tool_name,
            "command": step.command,
            "status": step.status,
            "raw_output": step.raw_log or "",
            "llm_analysis": step.llm_analysis or "",
        }

    async def run_agent_command(  # noqa: PLR0913, PLR0917
        self,
        command_params: str,
        display_id: int,
        tool: str,
        chain_id: int,
        phase_name: str,
        current_user: User,
        is_analyze_llm: bool = False,
        step_id: int | None = None,
    ) -> dict[str, Any]:
        """execute agent command, returns ticket now,
        raw_output & llm_analysis updates by events service via stream"""
        ctx = ChainContext(chain_id=chain_id, phase_name=phase_name)
        cb = CallbackInfo(display_id=display_id, step_type="agent", tool_name=tool)
        produced, _, c_agent, mythic_task = await check_and_process_agent_cmd(
            cb, ctx, command_params, self.mythic_client, is_analyze_llm
        )

        step = await self._reuse_step(step_id, chain_id, produced)
        if step is None:
            step = produced
            self.session.add(step)
        try:
            await self.session.commit()
            mythic_task.step_id = step.id
            c_agent.step_id = step.id
            self.session.add(mythic_task)
            self.session.add(c_agent)
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise HTTPException(status_code=400) from exc
        return {
            "user_id": current_user.user_id,
            "callback_display_id": display_id,
            "step_id": step.id,
            "mythic_task_id": step.mythic_task_id,
            "tool_name": step.tool_name,
            "command": step.command,
            "status": step.status,
            "raw_output": step.raw_log or "",
            "llm_analysis": step.llm_analysis or "",
        }

    async def _reuse_step(
        self, step_id: int | None, chain_id: int, produced: AttackStep
    ) -> AttackStep | None:
        """execute the clicked row rather than inserting a new one."""
        if step_id is None:
            return None
        existing = await self.session.scalar(
            select(AttackStep).where(
                AttackStep.id == step_id, AttackStep.chain_id == chain_id
            )
        )

        target = reusable(existing, chain_id)
        if target is None:
            return None
        return apply_execution(target, produced)

    async def approve_action(  # noqa: PLR0913, PLR0917
        self,
        command: str,
        chain_id: int,
        phase: str,
        agent_id: int,
        type_cmd: str,
        type_tool: str,
        target_os_type: str,
    ) -> dict[str, Any]:
        """route by proc and save step + ticket of task for tracking"""
        ctx = ChainContext(chain_id=chain_id, phase_name=phase)
        cb = CallbackInfo(
            display_id=agent_id,
            step_type=type_cmd,
            tool_name=type_tool,
        )
        result = await process_approved_cmd(
            cmd=command,
            ctx=ctx,
            cb=cb,
            payload_params=PayloadParams(os_type=target_os_type),
            mythic_client=self.mythic_client,
        )
        if hasattr(result, "__await__"):
            attack_step, llm_analysis, agent, mythic_task = await result
        else:
            attack_step, llm_analysis, agent, mythic_task = result
        if not isinstance(attack_step, AttackStep):
            raise HTTPException(400, "only processed steps can be approved")
        self.session.add(attack_step)
        try:
            await self.session.commit()
            await self.session.refresh(attack_step)
            if mythic_task is not None:
                mythic_task.step_id = attack_step.id
                self.session.add(mythic_task)
                await self.session.commit()
            if agent != "":
                agent.step_id = attack_step.id
                self.session.add(agent)
                await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise HTTPException(
                status_code=400, detail="Database integrity error"
            ) from exc
        return {
            "attack_step_id": attack_step.id,
            "status": attack_step.status,
            "llm_analysis": llm_analysis,
        }

    async def execute_approved_action(
        self,
        command: str,
        agent_display_id: int,
        chain_id: int,
        phase: str,
    ) -> dict[str, Any]:
        """run command on agent with ticket flow, like run_local_command"""
        ctx = ChainContext(chain_id=chain_id, phase_name=phase)
        step, _, mythic_task = await check_and_process_local_cmd(
            command, agent_display_id, ctx, self.mythic_client
        )
        self.session.add(step)
        try:
            await self.session.commit()
            mythic_task.step_id = step.id
            self.session.add(mythic_task)
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise HTTPException(status_code=400) from exc
        return {
            "attack_step_id": step.id,
            "status": step.status,
            "mythic_task_id": step.mythic_task_id,
            "llm_analysis": "",
        }

    async def get_step_with_tasks(self, step_id: int) -> dict[str, Any]:
        """step + all mythic task statuses, snapshot for sse attach"""
        step = await self.session.scalar(
            select(AttackStep).where(AttackStep.id == step_id)
        )
        if step is None:
            raise HTTPException(404, "step not found")
        rows = await self.session.execute(
            select(MythicTask).where(MythicTask.step_id == step_id)
        )
        bash_rows = await self.session.execute(
            select(BashTask).where(BashTask.step_id == step_id)
        )
        return {
            "step_id": step.id,
            "chain_id": step.chain_id,
            "phase": step.phase,
            "step_type": step.step_type,
            "tool_name": step.tool_name,
            "command": step.command,
            "status": step.status,
            "raw_log": step.raw_log,
            "llm_analysis": step.llm_analysis,
            "executed_at": str(step.executed_at),
            # id that callback agent connects to mythic on kali,
            # needed for executing shell commands on it
            "callback_display_id": step.mythic_task_id,
            "tasks": [
                {
                    "mythic_task_id": t.mythic_task_id,
                    "status": t.status,
                    "output": t.output,
                    "auto_analyze": t.auto_analyze,
                }
                for t in rows.scalars()
            ],
            "bash_tasks": [
                {
                    "bash_task_id": t.id,
                    "status": t.status,
                    "output": t.output,
                    "exit_code": t.exit_code,
                    "duration_ms": t.duration_ms,
                    "full_log_path": t.full_log_path,
                }
                for t in bash_rows.scalars()
            ],
        }

    async def analyze_step(self, step_id: int, current_user: User) -> dict[str, Any]:
        """explicit llm analysis by agent/user after output from events,
        result saved on step and mirrored to stream"""
        step = await self.session.execute(
            select(AttackStep)
            .join(AttackChain, AttackStep.chain_id == AttackChain.id)
            .where(AttackStep.id == step_id)
        )
        step_row = step.scalars().first()
        if step_row is None or step_row.attack_chain.user_id != current_user.user_id:
            raise HTTPException(404, "step not found")
        analysis = await analyze_command_output_with_llm(
            step_row.raw_log or "", step_row.command
        )
        now_out = analysis
        step_row.llm_analysis = now_out
        await self.session.commit()
        return {"step_id": step_id, "llm_analysis": now_out}

    async def get_agent_status(self, display_id: int) -> str:
        return await get_agent_status(display_id, self.mythic_client)
