"""Draft lifecycle and chain edits: the model proposes, the operator imports."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.timing import runtime_delay
from app.agent.validation import validate_draft
from app.core.config import get_settings, phases
from app.models import (
    AgentSession,
    AttackChain,
    AttackStep,
    ChainDraft,
    CurrentAttackPhase,
    User,
)
from app.services.agent_scope import owned_chain, owned_draft, owned_session

# ask-mode chain snapshot is bounded so a long chain cannot eat the context
_MAX_CHAIN_CONTEXT_CHARS = 8000
_EDITABLE_STATUSES = ("planned", "pending")
_EDIT_STEP_TYPES = ("local", "agent", "bash")


class EditError(RuntimeError):
    """one operation could not be applied; reported per-op, never aborts the run"""


class AgentDraftService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_latest_draft(self, session_id: int) -> ChainDraft | None:
        return await self.session.scalar(
            select(ChainDraft)
            .where(ChainDraft.session_id == session_id)
            .order_by(ChainDraft.id.desc())
            .limit(1)
        )

    async def save_draft(
        self,
        *,
        session_id: int,
        run_id: int,
        user: User,
        payload: dict[str, Any],
        name: str,
    ) -> ChainDraft:
        row = ChainDraft(
            session_id=session_id,
            run_id=run_id,
            user_id=user.user_id,
            name=(name or "Untitled draft")[:256],
            status=payload.get("status", "needs_review"),
            version=1,
            payload=payload,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)

        return row

    async def get_draft(self, draft_id: int, user: User) -> ChainDraft:
        return await owned_draft(self.session, draft_id, user)

    def build_import(
        self, draft: ChainDraft, *, chain_name: str, require_commands: bool = True
    ) -> dict[str, Any]:
        """re-validate on read, then shape steps exactly like ChainCanvas expects."""
        payload = dict(draft.payload or {})
        validated = validate_draft(
            payload,
            phase_names=phases,
            risky_patterns=get_settings().local_bash.reject,
            require_commands=require_commands,
        )
        if validated["validation"]["errors"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "message": "draft failed validation",
                    "errors": validated["validation"]["errors"],
                },
            )

        steps: list[dict[str, Any]] = []
        for index, step in enumerate(validated["steps"], start=1):
            is_agent = step["step_type"] == "agent"
            steps.append(
                {
                    "_tempId": f"draft-{draft.id}-{index}",
                    "chain_name": chain_name,
                    "phase": step["phase"],
                    "step_type": step["step_type"],
                    "tool_name": step["tool_name"],
                    "command": "" if is_agent else step["command"],
                    "command_params": step["command"] if is_agent else "",
                    "tool": step["tool_name"] if is_agent else "shell",
                    "callback_display_id": 0,
                    "status": "pending",
                    "raw_log": "",
                    "delay_before_secs": step.get("delay_seconds", 0),
                    "delay_reason": step.get("delay_reason", ""),
                    "delay_enabled": True,
                    "rationale": step.get("rationale", ""),
                    "finding_ids": step.get("finding_ids", []),
                    "confidence": step.get("confidence", 0.0),
                    "dangerous": step.get("dangerous", False),
                }
            )

        return {
            "draft_id": draft.id,
            "name": draft.name,
            "steps": steps,
            "validation": validated["validation"],
        }

    async def import_draft(
        self, draft: ChainDraft, user: User, *, chain_name: str
    ) -> dict[str, Any]:
        """materialize draft steps as real, non-executing AttackStep rows."""
        if draft.status == "imported":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="draft was already imported",
            )
        session_row = await owned_session(self.session, draft.session_id, user)
        if session_row.chain_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="agent session is not bound to a chain",
            )
        chain = await owned_chain(self.session, session_row.chain_id, user)
        if chain.chain_name != chain_name:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="session is bound to a different chain",
            )

        is_plan = (draft.payload or {}).get("mode") == "plan" or (
            draft.payload or {}
        ).get("status") == "plan"
        result = self.build_import(
            draft, chain_name=chain.chain_name, require_commands=not is_plan
        )

        # one constructor: a new proposal replaces the previous unexecuted one.
        # Executed rows (success/error/submitted) are never touched.
        replaced = await self.session.execute(
            delete(AttackStep).where(
                AttackStep.chain_id == chain.id,
                AttackStep.status.in_(("planned", "pending")),
                AttackStep.mythic_task_id == 0,
                AttackStep.mythic_payload_id == 0,
            )
        )
        replaced_count = int(getattr(replaced, "rowcount", 0) or 0)
        row_status = "planned" if is_plan else "pending"
        steps: list[dict[str, Any]] = []
        for step in result["steps"]:
            is_agent = step["step_type"] == "agent"
            row = AttackStep(
                chain_id=chain.id,
                phase=step["phase"],
                step_type=step["step_type"],
                tool_name=step["tool_name"] or "shell",
                command=step["command_params"] if is_agent else step["command"],
                mythic_task_id=0,
                mythic_payload_id=0,
                mythic_payload_uuid=str(uuid.uuid4()),
                raw_log="",
                delay_before_secs=step.get("delay_before_secs", 0),
                delay_reason=step.get("delay_reason") or None,
                status=row_status,
            )
            self.session.add(row)
            await self.session.flush()
            steps.append(
                {
                    **step,
                    "status": row_status,
                    "id": row.id,
                    "_tempId": f"step-{row.id}",
                }
            )

        draft.status = "imported"
        await self.session.commit()

        return {
            "draft_id": draft.id,
            "name": draft.name,
            "steps": steps,
            "validation": result["validation"],
            "replaced": replaced_count,
        }

    async def chain_context(self, session_row: AgentSession, user: User) -> str:
        """compact, bounded snapshot of the chain for ask mode."""
        if session_row.chain_id is None:
            return ""
        chain = await owned_chain(self.session, session_row.chain_id, user)
        current = await self.session.scalar(
            select(CurrentAttackPhase.phase).where(
                CurrentAttackPhase.chain_id == chain.id
            )
        )
        steps = await self.session.scalars(
            select(AttackStep)
            .where(AttackStep.chain_id == chain.id)
            .order_by(AttackStep.id)
        )
        lines = [
            f"Chain: {chain.chain_name}",
            f"Current phase: {current or 'unknown'}",
            "Steps (id | phase | type | tool | status | delay | command | output):",
        ]
        for step in steps:
            command = (step.command or "").replace("\n", " ")[:180]
            output = (step.raw_log or "").replace("\n", " ")[:180]
            delay = (
                f"{step.delay_before_secs // 60}m" if step.delay_before_secs else "-"
            )
            lines.append(
                f"- {step.id} | {step.phase} | {step.step_type} | {step.tool_name} "
                f"| {step.status} | {delay} | {command} | {output}"
            )
            if sum(len(line) for line in lines) > _MAX_CHAIN_CONTEXT_CHARS:
                lines.append("- … chain truncated")
                break

        return "\n".join(lines)

    async def chain_for_edit(
        self, session_row: AgentSession, user: User
    ) -> AttackChain:
        if session_row.chain_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="agent session is not bound to a chain",
            )

        return await owned_chain(self.session, session_row.chain_id, user)

    async def apply_edits(
        self,
        operations: list[dict[str, Any]],
        chain: AttackChain,
        user: User,
        *,
        runner: Callable[[AttackStep], Awaitable[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """execute validated chain operations; failures are per-op, not fatal."""
        results: list[dict[str, Any]] = []
        for raw in operations[:10]:
            op = str(raw.get("op") or "")
            try:
                if op == "add_step":
                    results.append(await self._edit_add_step(raw, chain))
                elif op == "remove_step":
                    results.append(await self._edit_remove_step(raw, chain))
                elif op == "run_step":
                    results.append(await self._edit_run_step(raw, chain, runner))
                elif op == "set_phase":
                    results.append(await self._edit_set_phase(raw, chain))
                else:
                    raise EditError(f"unknown operation '{op}'")
            except EditError as exc:
                # validation failure, nothing was written: keep the transaction
                results.append({"op": op, "status": "error", "detail": str(exc)[:200]})
            except SQLAlchemyError as exc:
                await self.session.rollback()
                results.append({"op": op, "status": "error", "detail": str(exc)[:200]})
            except Exception as exc:
                results.append({"op": op, "status": "error", "detail": str(exc)[:200]})

        return results

    async def _edit_add_step(
        self, op: dict[str, Any], chain: AttackChain
    ) -> dict[str, Any]:
        phase = op.get("phase")
        if phase not in phases:
            raise EditError(f"unknown phase '{phase}'")
        step_type = op.get("step_type")
        if step_type not in _EDIT_STEP_TYPES:
            raise EditError(f"unsupported step_type '{step_type}'")
        command = str(op.get("command") or "").strip()
        if not command:
            raise EditError("add_step needs a command")

        try:
            delay = runtime_delay(int(op.get("delay_seconds") or 0))
        except (TypeError, ValueError):
            delay = 0

        row = AttackStep(
            chain_id=chain.id,
            phase=phase,
            step_type=step_type,
            tool_name=str(op.get("tool_name") or "shell")[:64],
            command=command[:4000],
            mythic_task_id=0,
            mythic_payload_id=0,
            mythic_payload_uuid=str(uuid.uuid4()),
            raw_log="",
            delay_before_secs=delay,
            delay_reason=str(op.get("delay_reason") or "").strip()[:200] or None,
            status="pending",
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)

        return {"op": "add_step", "status": "ok", "step_id": row.id}

    async def _chain_step(self, op: dict[str, Any], chain: AttackChain) -> AttackStep:
        try:
            step_id = int(op.get("step_id") or 0)
        except (TypeError, ValueError) as exc:
            raise EditError("invalid step_id") from exc
        step = await self.session.scalar(
            select(AttackStep).where(
                AttackStep.id == step_id, AttackStep.chain_id == chain.id
            )
        )
        if step is None:
            raise EditError(f"step {step_id} is not in this chain")

        return step

    async def _edit_remove_step(
        self, op: dict[str, Any], chain: AttackChain
    ) -> dict[str, Any]:
        step = await self._chain_step(op, chain)
        if step.status not in _EDITABLE_STATUSES:
            raise EditError(
                f"step {step.id} is {step.status}; only unexecuted steps can be removed"
            )

        step_id = step.id
        await self.session.delete(step)
        await self.session.commit()

        return {"op": "remove_step", "status": "ok", "step_id": step_id}

    async def _edit_run_step(
        self,
        op: dict[str, Any],
        chain: AttackChain,
        runner: Callable[[AttackStep], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        step = await self._chain_step(op, chain)
        if step.status not in _EDITABLE_STATUSES:
            raise EditError(
                f"step {step.id} is {step.status}; only unexecuted steps can run"
            )
        if not step.command.strip():
            raise EditError(f"step {step.id} has no command")
        # capture before any commit expires the instance
        step_id = step.id
        outcome = await runner(step)
        new_id = outcome.get("new_step_id")
        if new_id and new_id != step_id:
            # the run creates its own row; drop the placeholder
            await self.session.execute(
                delete(AttackStep).where(AttackStep.id == step_id)
            )
            await self.session.commit()

        return {
            "op": "run_step",
            "status": "ok",
            "step_id": step_id,
            "new_step_id": new_id,
            "run_status": outcome.get("status"),
        }

    async def _edit_set_phase(
        self, op: dict[str, Any], chain: AttackChain
    ) -> dict[str, Any]:
        phase = op.get("phase")
        if phase not in phases:
            raise EditError(f"unknown phase '{phase}'")
        current = await self.session.scalar(
            select(CurrentAttackPhase).where(CurrentAttackPhase.chain_id == chain.id)
        )
        if current is None:
            current = CurrentAttackPhase(chain_id=chain.id, phase=phase)
            self.session.add(current)
        else:
            current.phase = phase

        await self.session.commit()

        return {"op": "set_phase", "status": "ok", "phase": phase}
