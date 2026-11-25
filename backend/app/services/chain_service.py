from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import desc, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent.timing import runtime_delay
from app.core.config import phases
from app.models import AttackChain, AttackStep, CurrentAttackPhase, User
from app.schemas.requests import DraftStepCreate
from app.services.ttp_info_service import UKC_PHASE_DESCRIPTIONS


class ChainService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_chain_n_phase(
        self, chain_name: str, current_user: User
    ) -> tuple[str, int, str]:
        chain_ca = await self.session.execute(
            select(AttackChain).where(
                AttackChain.user_id == current_user.user_id,
                AttackChain.chain_name == chain_name,
            )
        )
        chain_c = chain_ca.scalars().first()
        if chain_c is None:
            raise HTTPException(status_code=404, detail="Chain not found")
        c_phase = await self.session.execute(
            select(CurrentAttackPhase).where(CurrentAttackPhase.chain_id == chain_c.id)
        )
        phase_name_ob = c_phase.scalars().first()
        if phase_name_ob is None:
            raise HTTPException(status_code=404, detail="Phase not found")
        phase_name = str(phase_name_ob.phase) or "Reconnaissance"
        return chain_c.chain_name, chain_c.id, phase_name

    async def create_chain(
        self, chain_name: str, current_user: User
    ) -> tuple[int, str, str]:
        chain = AttackChain(
            user_id=current_user.user_id,
            chain_name=chain_name,
            final_status="execution",
        )
        self.session.add(chain)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
            ) from exc
        c_phase = CurrentAttackPhase(
            chain_id=chain.id,
            phase=phases[0],
        )
        self.session.add(c_phase)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
            ) from exc
        return chain.id, chain.chain_name, c_phase.phase

    async def owned_chain(self, chain_id: int, current_user: User) -> AttackChain:
        chain = await self.session.scalar(
            select(AttackChain).where(
                AttackChain.id == chain_id,
                AttackChain.user_id == current_user.user_id,
            )
        )
        if chain is None:
            raise HTTPException(status_code=404, detail="Chain not found")
        return chain

    async def owned_step(
        self, step_id: int, current_user: User, *, drafts: bool | None = None
    ) -> AttackStep:
        query = (
            select(AttackStep)
            .join(AttackChain, AttackStep.chain_id == AttackChain.id)
            .where(
                AttackStep.id == step_id,
                AttackChain.user_id == current_user.user_id,
            )
        )
        if drafts is True:
            query = query.where(AttackStep.is_draft.is_(True))
        elif drafts is False:
            query = query.where(AttackStep.is_draft.is_(False))
        step = await self.session.scalar(query)
        if step is None:
            raise HTTPException(status_code=404, detail="Step not found")
        return step

    async def read_chain_info(
        self, chain_id: int, current_user: User
    ) -> dict[str, Any]:
        chain_ca = await self.owned_chain(chain_id, current_user)
        await self.session.commit()
        chain_username = current_user.username or current_user.email.split("@", 1)[0]
        chain_c_phase_list = await self.session.execute(
            select(CurrentAttackPhase).where(CurrentAttackPhase.chain_id == chain_ca.id)
        )
        chain_c_phase = chain_c_phase_list.scalars().first()
        if chain_c_phase is None:
            raise HTTPException(status_code=404, detail="Phase not found")

        current_phase_n = chain_c_phase.phase or "Reconnaissance"
        res_l_step = await self.session.execute(
            select(AttackStep)
            .where(AttackStep.chain_id == chain_id)
            .order_by(desc(AttackStep.update_time))
            .limit(1)
        )
        last_attack_step_r = res_l_step.scalars().first()
        return {
            "chain_id": chain_ca.id,
            "user_id": chain_ca.user_id,
            "chain_name": chain_ca.chain_name,
            "username": chain_username,
            "user_email": current_user.email,
            "final_status": chain_ca.final_status,
            "current_phase_name": current_phase_n,
            "last_attack_step": last_attack_step_r,
        }

    async def next_phase(self, chain_id: int, current_user: User) -> tuple[int, str]:
        await self.owned_chain(chain_id, current_user)
        res_phase = await self.session.execute(
            select(CurrentAttackPhase).where(CurrentAttackPhase.chain_id == chain_id)
        )
        c_phase = res_phase.scalars().first()
        if c_phase is None:
            raise HTTPException(status_code=404, detail="Phase not found")
        try:
            idx = phases.index(c_phase.phase)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Unknown phase, check UCKC phases"
            ) from exc
        if idx + 1 >= len(phases):
            raise HTTPException(
                status_code=400,
                detail="Already last phase, try to find a way to save the chain.",
            )
        c_phase.phase = phases[idx + 1]
        await self.session.commit()
        return c_phase.chain_id, c_phase.phase

    async def set_phase(
        self, chain_id: int, phase_name: str, current_user: User
    ) -> tuple[int, str]:
        await self.owned_chain(chain_id, current_user)
        if phase_name not in phases:
            raise HTTPException(
                status_code=400, detail="Unknown phase, check UCKC phases"
            )
        res_phase = await self.session.execute(
            select(CurrentAttackPhase).where(CurrentAttackPhase.chain_id == chain_id)
        )
        c_phase = res_phase.scalars().first()

        if not c_phase:
            c_phase = CurrentAttackPhase(chain_id=chain_id, phase=phase_name)
            self.session.add(c_phase)
        else:
            c_phase.phase = phase_name
        await self.session.commit()
        return c_phase.chain_id, c_phase.phase

    async def reject_last_step(self, chain_name: str, current_user: User) -> AttackStep:
        _, chain_id, _ = await self.get_chain_n_phase(chain_name, current_user)
        res_l_step = await self.session.execute(
            select(AttackStep)
            .where(AttackStep.chain_id == chain_id)
            .order_by(desc(AttackStep.update_time))
            .limit(1)
        )
        last_attack_step_obj = res_l_step.scalars().first()
        if last_attack_step_obj is None:
            raise HTTPException(status_code=404, detail="No step to reject")

        await self.session.delete(last_attack_step_obj)
        await self.session.commit()
        return last_attack_step_obj

    async def verify_chain_name(
        self, chain_id: int, chain_name: str, current_user: User
    ) -> bool:
        chain = await self.owned_chain(chain_id, current_user)
        return chain_name == chain.chain_name

    async def steps_for_execution(self, chain_id: int) -> list[AttackStep]:
        chain_steps_list = await self.session.execute(
            select(AttackStep)
            .where(AttackStep.chain_id == chain_id, AttackStep.is_draft.is_(False))
            .order_by(AttackStep.id)
        )
        return list(chain_steps_list.scalars().all())

    async def steps_for_editor(self, chain_id: int) -> list[AttackStep]:
        chain_steps_list = await self.session.execute(
            select(AttackStep)
            .where(AttackStep.chain_id == chain_id)
            .order_by(AttackStep.id)
        )
        return list(chain_steps_list.scalars().all())

    async def get_chain_with_steps(
        self, chain_id: int, current_user: User
    ) -> dict[str, Any]:
        chain_ca = await self.owned_chain(chain_id, current_user)
        chain_username = current_user.username or current_user.email.split("@", 1)[0]
        chain_c_phase_list = await self.session.execute(
            select(CurrentAttackPhase).where(CurrentAttackPhase.chain_id == chain_ca.id)
        )

        chain_c_phase = chain_c_phase_list.scalars().first()
        if chain_c_phase is None:
            raise HTTPException(status_code=404, detail="Phase not found")

        current_phase_n = chain_c_phase.phase or "Reconnaissance"
        steps = await self.session.execute(
            select(AttackStep)
            .where(AttackStep.chain_id == chain_id)
            .order_by(AttackStep.id)
            .options(selectinload(AttackStep.agent))
        )
        steps = list(steps.scalars().all())
        for step in steps:
            # replaying an agent step appends a new Agent row; the last one is current
            agent = step.agent[-1] if step.agent else None
            step.callback_display_id = agent.callback_display_id if agent else 0

        return {
            "chain_id": chain_ca.id,
            "user_id": chain_ca.user_id,
            "chain_name": chain_ca.chain_name,
            "username": chain_username,
            "user_email": current_user.email,
            "final_status": chain_ca.final_status,
            "current_phase_name": current_phase_n,
            "steps": steps,
        }

    async def get_last_step(self, chain_id: int) -> AttackStep | None:
        res_l_step = await self.session.execute(
            select(AttackStep)
            .where(AttackStep.chain_id == chain_id)
            .order_by(desc(AttackStep.update_time))
            .limit(1)
        )
        return res_l_step.scalars().first()

    async def delete_step(self, step_id: int, current_user: User) -> None:
        # editor drafts have their own route; never delete one through a real step id
        step = await self.owned_step(step_id, current_user, drafts=False)
        await self.session.delete(step)
        await self.session.commit()

    async def delete_draft_step(self, chain_id: int, step_id: int, user: User) -> None:
        await self.owned_chain(chain_id, user)
        step = await self.owned_step(step_id, user, drafts=True)
        # owned_step checks the user, not the chain in the URL
        if step.chain_id != chain_id:
            raise HTTPException(status_code=404, detail="Step not found")
        await self.session.delete(step)
        await self.session.commit()

    async def edit_step(
        self, step_id: int, current_user: User, patch: dict[str, Any]
    ) -> AttackStep:
        step = await self.owned_step(step_id, current_user, drafts=False)

        allowed_fields = {
            "tool_name",
            "step_type",
            "command",
            "delay_before_secs",
            "delay_reason",
            "delay_enabled",
        }
        for field, value in patch.items():
            if field not in allowed_fields:
                continue
            if field == "delay_before_secs":
                try:
                    seconds = int(value or 0)
                except TypeError, ValueError:
                    continue
                setattr(step, field, runtime_delay(seconds))
                continue
            setattr(step, field, value)

        await self.session.commit()
        await self.session.refresh(step)
        return step

    async def create_draft_step(
        self, chain_id: int, current_user: User, data: DraftStepCreate
    ) -> AttackStep:
        await self.owned_chain(chain_id, current_user)
        if data.phase not in phases:
            raise HTTPException(status_code=422, detail="Unknown phase")
        if not data.command.strip():
            raise HTTPException(status_code=422, detail="Command is required")
        row = AttackStep(
            chain_id=chain_id,
            phase=data.phase,
            step_type=data.step_type,
            tool_name=data.tool_name[:64] or "shell",
            command=data.command[:4000],
            mythic_task_id=0,
            mythic_payload_id=0,
            mythic_payload_uuid=str(uuid.uuid4()),
            raw_log="",
            delay_before_secs=runtime_delay(data.delay_before_secs),
            status="pending",
            is_draft=True,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def edit_draft_step(
        self, chain_id: int, step_id: int, current_user: User, patch: dict[str, Any]
    ) -> AttackStep:
        await self.owned_chain(chain_id, current_user)
        step = await self.owned_step(step_id, current_user, drafts=True)
        if step.chain_id != chain_id:
            raise HTTPException(status_code=404, detail="Step not found")

        allowed_fields = {
            "phase",
            "tool_name",
            "step_type",
            "command",
            "delay_before_secs",
            "delay_reason",
            "delay_enabled",
        }
        for field, raw_value in patch.items():
            if field not in allowed_fields:
                continue
            value = raw_value
            if field == "phase" and value not in phases:
                raise HTTPException(status_code=422, detail="Unknown phase")
            if field == "step_type" and value not in ("local", "agent", "bash"):
                raise HTTPException(status_code=422, detail="Unsupported step type")
            if field == "command":
                # a draft may be empty while the operator is still typing;
                # promotion skips rows without a command
                value = str(value or "")[:4000]
            if field == "delay_before_secs":
                try:
                    value = runtime_delay(int(value or 0))
                except TypeError, ValueError:
                    continue
            setattr(step, field, value)

        await self.session.commit()
        await self.session.refresh(step)
        return step

    async def promote_drafts(self, chain_id: int) -> int:
        result = await self.session.execute(
            update(AttackStep)
            .where(
                AttackStep.chain_id == chain_id,
                AttackStep.is_draft.is_(True),
                # an empty card is still being edited and cannot run yet
                AttackStep.command != "",
            )
            .values(is_draft=False)
        )
        await self.session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def set_phase_timing(
        self, chain_id: int, phase: str, enabled: bool, current_user: User
    ) -> int:
        """flip dwell for every step in one phase"""
        chain = await self.session.get(AttackChain, chain_id)
        if chain is None or chain.user_id != current_user.user_id:
            raise HTTPException(status_code=404, detail="Chain not found")

        result = await self.session.execute(
            update(AttackStep)
            .where(AttackStep.chain_id == chain_id, AttackStep.phase == phase)
            .values(delay_enabled=enabled)
        )
        await self.session.commit()

        return int(getattr(result, "rowcount", 0) or 0)

    @staticmethod
    async def get_possible_phases() -> list[dict[str, str]]:
        return [
            {"name": p, "description": UKC_PHASE_DESCRIPTIONS.get(p, "")}
            for p in phases
        ]

    @staticmethod
    async def get_ukc_phase_description(phase_name: str) -> str:
        if phase_name not in phases:
            raise HTTPException(
                status_code=400, detail="Unknown phase, check UCKC phases"
            )
        return UKC_PHASE_DESCRIPTIONS.get(phase_name, "")

    async def list_chains(self, current_user: User) -> list[dict[str, Any]]:
        result = await self.session.execute(
            select(AttackChain)
            .where(AttackChain.user_id == current_user.user_id)
            .order_by(desc(AttackChain.create_time))
        )
        chains = result.scalars().all()
        chain_list = []

        for c in chains:
            phase_row = await self.session.execute(
                select(CurrentAttackPhase).where(CurrentAttackPhase.chain_id == c.id)
            )
            phase_obj = phase_row.scalars().first()
            chain_list.append(
                {
                    "chain_id": c.id,
                    "chain_name": c.chain_name,
                    "current_phase_name": phase_obj.phase
                    if phase_obj
                    else "Reconnaissance",
                }
            )
        return chain_list

    async def delete_chain(self, chain_id: int, current_user: User) -> None:
        chain = await self.session.get(AttackChain, chain_id)
        if chain is None or chain.user_id != current_user.user_id:
            raise HTTPException(status_code=404, detail="Chain not found")

        await self.session.delete(chain)
        await self.session.commit()
