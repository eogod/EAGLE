from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cmd.c2_tool import MythicClient
from app.cmd.proc import (
    ChainContext,
    PayloadParams,
    check_and_create_mpayload,
    process_new_callback,
)
from app.models import AttackStep


class AgentPayloadService:
    def __init__(self, session: AsyncSession, mythic_client: MythicClient) -> None:
        self.session = session
        self.mythic_client = mythic_client

    # TODO: add local build recipes with a pinned toolchain; Mythic is the only build path.
    async def create_payload(
        self,
        chain_id: int,
        phase_name: str,
        payload_type: str | None,
        os_type: str,
    ) -> dict[str, Any]:
        p_type = "None" if payload_type is None else str(payload_type)
        tool_name = "payload_" + p_type
        ctx = ChainContext(chain_id=chain_id, phase_name=phase_name)
        payload_step, llm_a = await check_and_create_mpayload(
            ctx,
            tool_name,
            p_type,
            PayloadParams(lport=-1, os_type=os_type),
            mythic_client=self.mythic_client,
        )
        self.session.add(payload_step)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise HTTPException(status_code=400) from exc
        ip = os.getenv("MYTHIC__SERVER_IP")
        port = os.getenv("MYTHIC__SERVER_PORT")
        uuid = payload_step.mythic_payload_uuid
        download_url = f"https://{ip}:{port}/direct/download/{uuid}"
        return {
            "chain_id": chain_id,
            "step_id": payload_step.id,
            "status": payload_step.status,
            "phase": payload_step.phase,
            "download_url": download_url,
            "payload_uuid": uuid,
            "payload_id": payload_step.mythic_payload_id,
            "raw_log": payload_step.raw_log,
            "llm_analysis": llm_a,
            "payload_type": payload_step.tool_name,
        }

    async def register_agent(
        self,
        rhost: str,
        chain_id: int,
        phase_name: str,
    ) -> dict[str, Any]:
        chain_steps_list = await self.session.execute(
            select(AttackStep).where(AttackStep.chain_id == chain_id)
        )
        chain_steps_l_ca = list(chain_steps_list.scalars().all())
        last_step = max(chain_steps_l_ca, key=lambda step: step.update_time)
        ctx = ChainContext(chain_id=chain_id, phase_name=phase_name)
        res = await process_new_callback(
            ctx,
            "get_agent_callback_after",
            rhost,
            last_step.id,
            mythic_client=self.mythic_client,
        )
        get_callback_step, new_agent = res
        self.session.add(get_callback_step)
        self.session.add(new_agent)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise HTTPException(status_code=400) from exc
        return {
            "os_type": new_agent.os_type,
            "rhost": rhost,
            "status": new_agent.status,
            "callback_display_id": new_agent.callback_display_id,
        }
