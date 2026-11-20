from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import desc, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent.timing import runtime_delay
from app.core.config import phases
from app.models import AttackChain, AttackStep, CurrentAttackPhase, User
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
        await self.session.commit()
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

    async def read_chain_info(
        self, chain_id: int, current_user: User
    ) -> dict[str, Any]:
        chain_ca_list = await self.session.execute(
            select(AttackChain).where(AttackChain.id == chain_id)
        )
        chain_ca = chain_ca_list.scalars().first()
        if chain_ca is None:
            raise HTTPException(status_code=404, detail="Chain not found")
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

    async def next_phase(self, chain_id: int) -> tuple[int, str]:
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

    async def set_phase(self, chain_id: int, phase_name: str) -> tuple[int, str]:
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

    async def verify_chain_name(self, chain_id: int, chain_name: str) -> bool:
        chain_ca_list = await self.session.execute(
            select(AttackChain).where(AttackChain.id == chain_id)
        )
        chain_ca = chain_ca_list.scalars().first()
        if chain_ca is None:
            return False
        return chain_name == chain_ca.chain_name

    async def get_chain_steps(self, chain_id: int) -> list[AttackStep]:
        chain_steps_list = await self.session.execute(
            select(AttackStep)
            .where(AttackStep.chain_id == chain_id)
            .order_by(AttackStep.id)
        )
        return list(chain_steps_list.scalars().all())

    async def get_chain_with_steps(
        self, chain_id: int, current_user: User
    ) -> dict[str, Any]:
        chain_ca_list = await self.session.execute(
            select(AttackChain).where(AttackChain.id == chain_id)
        )
        chain_ca = chain_ca_list.scalars().first()
        if chain_ca is None:
            raise HTTPException(status_code=404, detail="Chain not found")
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
        step = await self.session.get(AttackStep, step_id)
        if step is None:
            raise HTTPException(status_code=404, detail="Step not found")
        chain = await self.session.get(AttackChain, step.chain_id)

        if chain is None or chain.user_id != current_user.user_id:
            raise HTTPException(status_code=404, detail="Chain not found")

        await self.session.delete(step)
        await self.session.commit()

    async def edit_step(
        self, step_id: int, current_user: User, patch: dict[str, Any]
    ) -> AttackStep:
        step = await self.session.get(AttackStep, step_id)
        if step is None:
            raise HTTPException(status_code=404, detail="Step not found")

        chain = await self.session.get(AttackChain, step.chain_id)
        if chain is None or chain.user_id != current_user.user_id:
            raise HTTPException(status_code=404, detail="Chain not found")

        allowed_fields = {
            "tool_name",
            "step_type",
            "command",
            "raw_log",
            "status",
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
                except (TypeError, ValueError):
                    continue
                setattr(step, field, runtime_delay(seconds))
                continue
            setattr(step, field, value)

        await self.session.commit()
        await self.session.refresh(step)
        return step

    async def set_phase_timing(
        self, chain_id: int, phase: str, enabled: bool, current_user: User
    ) -> int:
        """the phase checkbox flips every step in that column at once."""
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
