"""operator review gate for agent edit and run proposals"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.edit_plan import (
    PlanContext,
    StepSnapshot,
    ValidatedPlan,
    chain_fingerprint,
    command_sha256,
    step_fingerprint,
    validate_plan,
)
from app.core.config import get_settings, phases
from app.models import (
    AgentSession,
    AttackChain,
    AttackStep,
    ChainAuditEvent,
    ChainProposal,
    CurrentAttackPhase,
    User,
)
from app.services.agent_scope import owned_chain, owned_session
from app.services.step_runner import StepRunner

PROPOSAL_TTL_SECS = 30 * 60
OPEN_STATUSES = ("proposed", "approved")
REJECTABLE_STATUSES = ("proposed", "approved", "invalid")
RESOLVED_STATUSES = ("applied", "rejected", "expired", "invalid")


@dataclass(frozen=True)
class ProposalInput:
    session_row: AgentSession
    user: User
    kind: str
    raw_ops: Any
    summary: str
    run_id: int | None
    provider: str
    model: str


class ChainProposalService:
    """store a proposal and move it through operator review"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def propose(self, data: ProposalInput) -> ChainProposal:
        await owned_session(self.session, data.session_row.id, data.user)
        chain = await owned_chain(self.session, data.session_row.chain_id, data.user)
        context, fingerprint = await self._plan_context(chain, data.kind)
        await self._supersede_open(data.session_row.id, chain.id, data.kind)

        plan = validate_plan(data.raw_ops, context)
        proposal = ChainProposal(
            public_id=secrets.token_urlsafe(24),
            kind=data.kind,
            status="proposed" if plan.ok else "invalid",
            user_id=data.user.user_id,
            session_id=data.session_row.id,
            chain_id=chain.id,
            run_id=data.run_id,
            base_fingerprint=fingerprint,
            ops=plan.ops,
            validation={
                "errors": plan.errors,
                "warnings": plan.warnings,
                "dangerous_steps": plan.dangerous_steps,
            },
            summary=(data.summary or "")[:1000],
            provider=(data.provider or "")[:128],
            model=(data.model or "")[:256],
            expires_at=int(time.time()) + PROPOSAL_TTL_SECS,
        )
        self.session.add(proposal)
        await self.session.flush()
        self._audit(
            proposal,
            "proposal.created" if plan.ok else "proposal.invalid",
            "ok" if plan.ok else "error",
            {"operations": len(plan.ops), "errors": plan.errors[:5]},
        )
        await self.session.commit()
        await self.session.refresh(proposal)
        return proposal

    async def list_for_session(
        self, session_id: int, user: User, statuses: tuple[str, ...]
    ) -> list[ChainProposal]:
        await owned_session(self.session, session_id, user)
        rows = await self.session.scalars(
            select(ChainProposal)
            .where(
                ChainProposal.session_id == session_id,
                ChainProposal.status.in_(statuses),
            )
            .order_by(ChainProposal.id.desc())
        )
        proposals = list(rows)
        for proposal in proposals:
            self._expire(proposal)
        if proposals:
            await self.session.commit()
        return proposals

    async def get(self, public_id: str, user: User) -> ChainProposal:
        proposal = await self._get_owned(public_id, user, lock=False)
        if self._expire(proposal):
            await self.session.commit()
        return proposal

    async def approve(
        self, public_id: str, user: User, *, acknowledge_dangerous: bool = False
    ) -> ChainProposal:
        proposal = await self._get_owned(public_id, user, lock=True)
        await self._require_open(proposal)
        if proposal.status != "proposed":
            raise HTTPException(status_code=409, detail="proposal already approved")
        await self._verify_fingerprint(proposal)
        self._require_valid(proposal, acknowledge_dangerous)

        await self._claim(proposal, "approved")
        proposal.approved_at = int(time.time())
        proposal.approved_by = user.user_id
        if acknowledge_dangerous:
            proposal.danger_ack_at = proposal.approved_at
        self._audit(proposal, "proposal.approved", "ok", {})
        await self.session.commit()
        await self.session.refresh(proposal)
        return proposal

    async def reject(
        self, public_id: str, user: User, *, reason: str = ""
    ) -> ChainProposal:
        proposal = await self._get_owned(public_id, user, lock=True)
        if proposal.status not in REJECTABLE_STATUSES:
            raise HTTPException(status_code=409, detail="proposal already resolved")
        await self._claim(proposal, "rejected")
        proposal.rejected_at = int(time.time())
        proposal.error = reason[:500]
        self._audit(proposal, "proposal.rejected", "ok", {"reason": reason[:200]})
        await self.session.commit()
        await self.session.refresh(proposal)
        return proposal

    async def apply(
        self, public_id: str, user: User, *, acknowledge_dangerous: bool = False
    ) -> ChainProposal:
        proposal = await self._get_owned(public_id, user, lock=True)
        if proposal.kind != "edit":
            raise HTTPException(status_code=403, detail="not a chain edit proposal")
        plan = await self._ready_plan(proposal, acknowledge_dangerous)
        if not plan.ok:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "proposal no longer validates",
                    "errors": plan.errors,
                },
            )

        await self._claim(proposal, "applying")
        try:
            results = await self._apply_ops(proposal, plan)
        except Exception:
            # the claim and any partial writes must disappear together
            await self.session.rollback()
            raise

        proposal.status = "applied"
        proposal.applied_at = int(time.time())
        proposal.result = results
        self._audit(
            proposal,
            "proposal.applied",
            "ok",
            {"operations": len(results), "step_ids": self._step_ids(results)},
        )
        await self.session.commit()
        await self.session.refresh(proposal)
        return proposal

    async def execute(
        self,
        public_id: str,
        user: User,
        runner: StepRunner,
        *,
        acknowledge_dangerous: bool = False,
    ) -> ChainProposal:
        proposal = await self._get_owned(public_id, user, lock=True)
        if proposal.kind != "run":
            raise HTTPException(status_code=403, detail="not a run proposal")
        plan = await self._ready_plan(proposal, acknowledge_dangerous)
        if not plan.ok or len(plan.ops) != 1:
            raise HTTPException(
                status_code=422, detail="run proposal is no longer valid"
            )
        op = plan.ops[0]
        step = await self._step_for_op(proposal.chain_id, op)
        if (
            step_fingerprint(self._snapshot_step(step))
            != op["expected_step_fingerprint"]
        ):
            raise HTTPException(status_code=409, detail="step changed; review again")

        await self._claim(proposal, "executing")
        proposal.executed_at = int(time.time())
        # commit the claim first: a crash after the runner starts must not allow a second run
        await self.session.commit()

        try:
            outcome = await runner(step)
        except Exception as exc:
            proposal.status = "failed"
            proposal.error = type(exc).__name__
            proposal.finished_at = int(time.time())
            self._audit(
                proposal,
                "run.failed",
                "error",
                {"step_id": step.id, "error": type(exc).__name__},
            )
            await self.session.commit()
            raise HTTPException(status_code=502, detail="run failed") from exc

        proposal.status = "executed"
        proposal.finished_at = int(time.time())
        proposal.result = [outcome]
        self._audit(
            proposal,
            "run.submitted",
            "ok",
            {
                "step_id": step.id,
                "command_sha256": op["command_sha256"],
                "task_step_id": outcome.get("new_step_id"),
            },
            step_id=step.id,
        )
        await self.session.commit()
        await self.session.refresh(proposal)
        return proposal

    def preview(self, proposal: ChainProposal) -> dict[str, Any]:
        status_now = self._status_at(proposal, int(time.time()))
        return {
            "public_id": proposal.public_id,
            "kind": proposal.kind,
            "status": status_now,
            "chain_id": proposal.chain_id,
            "session_id": proposal.session_id,
            "summary": proposal.summary,
            "operations": proposal.ops,
            "validation": proposal.validation,
            "expires_at": proposal.expires_at,
            "base_fingerprint": proposal.base_fingerprint,
            "result": proposal.result,
            "error": proposal.error,
            "can_approve": status_now == "proposed",
            "can_apply": status_now == "approved" and proposal.kind == "edit",
            "can_execute": status_now == "approved" and proposal.kind == "run",
        }

    async def _supersede_open(self, session_id: int, chain_id: int, kind: str) -> None:
        rows = await self.session.scalars(
            select(ChainProposal).where(
                ChainProposal.session_id == session_id,
                ChainProposal.chain_id == chain_id,
                ChainProposal.kind == kind,
                ChainProposal.status.in_(OPEN_STATUSES),
            )
        )
        for row in rows:
            if row.status == "approved":
                # replacing it would throw away a decision the operator already made
                raise HTTPException(
                    status_code=409,
                    detail="an approved proposal is waiting to be applied",
                )
            row.status = "expired"
            self._audit(row, "proposal.superseded", "ok", {})

    async def _ready_plan(
        self, proposal: ChainProposal, acknowledge_dangerous: bool
    ) -> ValidatedPlan:
        if proposal.status != "approved":
            raise HTTPException(status_code=409, detail="proposal is not approved")
        await self._require_open(proposal)
        plan, fingerprint = await self._revalidate(proposal)
        if fingerprint != proposal.base_fingerprint:
            raise HTTPException(
                status_code=409, detail="chain changed; review the proposal again"
            )
        self._require_valid(proposal, acknowledge_dangerous)
        return plan

    async def _plan_context(
        self, chain: AttackChain, kind: str
    ) -> tuple[PlanContext, str]:
        phase = await self.session.scalar(
            select(CurrentAttackPhase.phase).where(
                CurrentAttackPhase.chain_id == chain.id
            )
        )
        rows = await self.session.scalars(
            select(AttackStep)
            .where(AttackStep.chain_id == chain.id, AttackStep.is_draft.is_(False))
            .order_by(AttackStep.id)
        )
        steps = [self._snapshot_step(row) for row in rows]
        snapshots = {step.id: step for step in steps}
        fingerprint = chain_fingerprint(chain.id, phase or "", steps)
        context = PlanContext(
            kind="run" if kind == "run" else "edit",
            phases=tuple(phases),
            steps=snapshots,
            risky_patterns=tuple(get_settings().local_bash.reject),
            current_step_count=len(steps),
        )
        return context, fingerprint

    async def _revalidate(self, proposal: ChainProposal) -> tuple[ValidatedPlan, str]:
        chain = await self.session.get(AttackChain, proposal.chain_id)
        if chain is None:
            raise HTTPException(status_code=404, detail="chain not found")
        context, fingerprint = await self._plan_context(chain, proposal.kind)
        return validate_plan(proposal.ops, context), fingerprint

    async def _verify_fingerprint(self, proposal: ChainProposal) -> None:
        _, current = await self._revalidate(proposal)
        if current != proposal.base_fingerprint:
            raise HTTPException(
                status_code=409, detail="chain changed; review the proposal again"
            )

    async def _require_open(self, proposal: ChainProposal) -> None:
        if proposal.status in RESOLVED_STATUSES:
            raise HTTPException(status_code=409, detail="proposal already resolved")
        if self._expire(proposal):
            # persist the expiry and the audit row before the 410 leaves the request
            await self.session.commit()
            raise HTTPException(status_code=410, detail="proposal expired")

    def _require_valid(
        self, proposal: ChainProposal, acknowledge_dangerous: bool
    ) -> None:
        validation = proposal.validation or {}
        errors = validation.get("errors") or []
        if errors:
            raise HTTPException(
                status_code=422,
                detail={"message": "proposal is invalid", "errors": errors},
            )
        dangerous = validation.get("dangerous_steps") or []
        if dangerous and not acknowledge_dangerous:
            raise HTTPException(
                status_code=422, detail="dangerous steps need explicit acknowledgement"
            )

    def _status_at(self, proposal: ChainProposal, now: int) -> str:
        if proposal.status in OPEN_STATUSES and proposal.expires_at <= now:
            return "expired"
        return proposal.status

    def _expire(self, proposal: ChainProposal) -> bool:
        if proposal.status in OPEN_STATUSES and proposal.expires_at <= int(time.time()):
            proposal.status = "expired"
            self._audit(proposal, "proposal.expired", "ok", {})
            return True
        return False

    async def _get_owned(
        self, public_id: str, user: User, *, lock: bool
    ) -> ChainProposal:
        query = select(ChainProposal).where(
            ChainProposal.public_id == public_id,
            ChainProposal.user_id == user.user_id,
        )
        if lock:
            query = query.with_for_update()
        proposal = await self.session.scalar(query)
        if proposal is None:
            raise HTTPException(status_code=404, detail="proposal not found")
        return proposal

    async def _claim(self, proposal: ChainProposal, target: str) -> None:
        if not await self._transition(proposal, target):
            raise HTTPException(status_code=409, detail="proposal already resolved")

    async def _transition(self, proposal: ChainProposal, target: str) -> bool:
        # compare-and-set: two tabs must not both approve the same proposal
        result = await self.session.execute(
            update(ChainProposal)
            .where(
                ChainProposal.id == proposal.id,
                ChainProposal.status == proposal.status,
            )
            .values(status=target)
        )
        rowcount = int(getattr(result, "rowcount", 0) or 0)
        if rowcount == 1:
            proposal.status = target
        return rowcount == 1

    async def _apply_ops(
        self, proposal: ChainProposal, plan: ValidatedPlan
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for op in plan.ops:
            if op["op"] == "add_step":
                results.append(await self._add_step(proposal, op))
            elif op["op"] == "remove_step":
                results.append(await self._remove_step(proposal, op))
            elif op["op"] == "set_phase":
                results.append(await self._set_phase(proposal, op))
        return results

    async def _add_step(
        self, proposal: ChainProposal, op: dict[str, Any]
    ) -> dict[str, Any]:
        row = AttackStep(
            chain_id=proposal.chain_id,
            phase=op["phase"],
            step_type=op["step_type"],
            tool_name=op["tool_name"],
            command=op["command"],
            mythic_task_id=0,
            mythic_payload_id=0,
            mythic_payload_uuid=str(uuid.uuid4()),
            raw_log="",
            delay_before_secs=op["delay_seconds"],
            delay_reason=op.get("delay_reason") or None,
            status="pending",
            is_draft=False,
        )
        self.session.add(row)
        await self.session.flush()
        return {"op": "add_step", "status": "ok", "step_id": row.id}

    async def _remove_step(
        self, proposal: ChainProposal, op: dict[str, Any]
    ) -> dict[str, Any]:
        step = await self._step_for_op(proposal.chain_id, op)
        if (
            step_fingerprint(self._snapshot_step(step))
            != op["expected_step_fingerprint"]
        ):
            raise HTTPException(status_code=409, detail="step changed; review again")
        self._audit(
            proposal,
            "step.removed",
            "ok",
            {
                "step_id": step.id,
                "phase": step.phase,
                "step_type": step.step_type,
                "command_sha256": command_sha256(step.command),
            },
            step_id=step.id,
        )
        await self.session.delete(step)
        return {"op": "remove_step", "status": "ok", "step_id": op["step_id"]}

    async def _set_phase(
        self, proposal: ChainProposal, op: dict[str, Any]
    ) -> dict[str, Any]:
        current = await self.session.scalar(
            select(CurrentAttackPhase).where(
                CurrentAttackPhase.chain_id == proposal.chain_id
            )
        )
        if current is None:
            # older chains may have steps but no phase row
            current = CurrentAttackPhase(chain_id=proposal.chain_id, phase=op["phase"])
            self.session.add(current)
        else:
            current.phase = op["phase"]
        return {"op": "set_phase", "status": "ok", "phase": op["phase"]}

    async def _step_for_op(self, chain_id: int, op: dict[str, Any]) -> AttackStep:
        step = await self.session.scalar(
            select(AttackStep).where(
                AttackStep.id == int(op["step_id"]),
                AttackStep.chain_id == chain_id,
            )
        )
        if step is None:
            raise HTTPException(status_code=404, detail="step not found")
        return step

    @staticmethod
    def _snapshot_step(step: AttackStep) -> StepSnapshot:
        return StepSnapshot(
            id=step.id,
            phase=step.phase,
            step_type=step.step_type,
            tool_name=step.tool_name,
            command=step.command,
            status=step.status,
            mythic_task_id=step.mythic_task_id,
            mythic_payload_id=step.mythic_payload_id,
            mythic_payload_uuid=str(step.mythic_payload_uuid),
            delay_before_secs=step.delay_before_secs,
        )

    def _audit(
        self,
        proposal: ChainProposal,
        action: str,
        outcome: str,
        detail: dict[str, Any],
        *,
        step_id: int | None = None,
    ) -> None:
        self.session.add(
            ChainAuditEvent(
                chain_id=proposal.chain_id,
                session_id=proposal.session_id,
                proposal_id=proposal.id,
                step_id=step_id,
                user_id=proposal.user_id,
                action=action,
                outcome=outcome,
                detail=detail,
            )
        )

    @staticmethod
    def _step_ids(results: list[dict[str, Any]]) -> list[int]:
        return [
            int(item["step_id"]) for item in results if item.get("step_id") is not None
        ]
