"""Ownership lookups shared by the agent services.

One security seam: an id owned by another user must look exactly like a missing
row, so every service raises the same 404 here instead of re-implementing joins.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AgentDocument,
    AgentRun,
    AgentSession,
    AttackChain,
    ChainDraft,
    User,
)


async def owned_session(
    session: AsyncSession, session_id: int, user: User
) -> AgentSession:
    row = await session.scalar(
        select(AgentSession).where(
            AgentSession.id == session_id,
            AgentSession.user_id == user.user_id,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="agent session not found"
        )

    return row


async def owned_chain(session: AsyncSession, chain_id: int, user: User) -> AttackChain:
    row = await session.scalar(
        select(AttackChain).where(
            AttackChain.id == chain_id, AttackChain.user_id == user.user_id
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="chain not found"
        )

    return row


async def owned_document(
    session: AsyncSession, document_id: int, user: User
) -> tuple[AgentDocument, AgentSession]:
    row = await session.scalar(
        select(AgentDocument).where(AgentDocument.id == document_id)
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="document not found"
        )

    return row, await owned_session(session, row.session_id, user)


async def owned_run(session: AsyncSession, run_id: int, user: User) -> AgentRun:
    row = await session.scalar(
        select(AgentRun)
        .join(AgentSession, AgentRun.session_id == AgentSession.id)
        .where(AgentRun.id == run_id, AgentSession.user_id == user.user_id)
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="agent run not found"
        )

    return row


async def owned_draft(session: AsyncSession, draft_id: int, user: User) -> ChainDraft:
    row = await session.scalar(
        select(ChainDraft).where(
            ChainDraft.id == draft_id, ChainDraft.user_id == user.user_id
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="draft not found"
        )

    return row
