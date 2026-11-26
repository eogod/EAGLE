"""Chat state, run bookkeeping and the LLM facade for the analysis agent."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import AgentFinding, AgentMessage, AgentRun, AgentSession, User
from app.services.agent_scope import owned_chain, owned_run, owned_session
from app.services.llm_service import (
    ChatStreamProvider,
    ProviderNotConfigured,
    build_provider,
    list_models,
    llm_service,
)


class AgentChatService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # headers/endpoints never import app/cmd directly; provider selection,
    # provider listing and the chain-work g4f/ollama pool all go through here

    def get_provider(
        self, *, provider: str | None = None, model: str | None = None
    ) -> ChatStreamProvider:
        try:
            return build_provider(
                get_settings().llmservice, provider=provider, model=model
            )
        except ProviderNotConfigured as exc:
            # a missing provider is config, not a crash; the SSE/UI maps it
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc

    async def list_providers(self) -> dict[str, dict[str, Any]]:
        return await list_models(get_settings().llmservice)

    def llm_providers(self) -> dict[str, str]:
        """registered g4f provider classes for the chain-work LLM path"""
        return llm_service.provider_names()

    async def query_llm(self, prompt: str, provider_name: str | None = None) -> str:
        return await llm_service.query_llm(prompt, provider_name)

    async def query_llm_chat(
        self, messages: list[dict[str, Any]], provider_name: str | None = None
    ) -> str:
        return await llm_service.query_llm_chat(messages, provider_name)

    async def create_session(
        self, user: User, *, chain_id: int | None = None, title: str | None = None
    ) -> AgentSession:
        if chain_id is not None:
            await owned_chain(self.session, chain_id, user)
        row = AgentSession(
            user_id=user.user_id,
            chain_id=chain_id,
            title=(title or "New analysis").strip()[:256],
            status="active",
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)

        return row

    async def list_sessions(self, user: User) -> list[AgentSession]:
        result = await self.session.scalars(
            select(AgentSession)
            .where(AgentSession.user_id == user.user_id)
            .order_by(AgentSession.update_time.desc(), AgentSession.id.desc())
        )

        return list(result)

    async def get_session(self, session_id: int, user: User) -> AgentSession:
        return await owned_session(self.session, session_id, user)

    async def update_session(
        self, session_id: int, user: User, *, title: str
    ) -> AgentSession:
        row = await owned_session(self.session, session_id, user)
        row.title = title.strip()[:256]
        await self.session.commit()
        await self.session.refresh(row)

        return row

    async def delete_session(self, session_id: int, user: User) -> None:
        row = await owned_session(self.session, session_id, user)
        await self.session.delete(row)
        await self.session.commit()

    async def list_messages(self, session_id: int) -> list[AgentMessage]:
        result = await self.session.scalars(
            select(AgentMessage)
            .where(AgentMessage.session_id == session_id)
            .order_by(AgentMessage.id)
        )

        return list(result)

    async def add_user_message(self, session_id: int, content: str) -> AgentMessage:
        row = AgentMessage(
            session_id=session_id,
            role="user",
            content=[{"type": "text", "text": content}],
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)

        return row

    async def attach_message_to_run(self, message_id: int, run_id: int) -> None:
        row = await self.session.get(AgentMessage, message_id)
        if row is not None:
            row.run_id = run_id
            await self.session.commit()

    async def save_assistant_message(
        self, session_id: int, run_id: int, content: list[dict[str, Any]]
    ) -> AgentMessage:
        row = AgentMessage(
            session_id=session_id,
            role="assistant",
            content=content,
            run_id=run_id,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)

        return row

    async def create_run(self, session_id: int, document_id: int | None) -> AgentRun:
        row = AgentRun(
            session_id=session_id,
            document_id=document_id,
            status="running",
            cancel_requested=False,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)

        return row

    async def get_run(self, run_id: int, user: User) -> AgentRun:
        return await owned_run(self.session, run_id, user)

    async def request_cancel(self, run_id: int) -> None:
        row = await self.session.get(AgentRun, run_id)
        if row is not None and row.status == "running":
            row.cancel_requested = True
            await self.session.commit()

    def make_finding_saver(
        self, run: AgentRun
    ) -> Callable[[dict[str, Any]], Awaitable[int]]:
        """returns an async callback the pipeline uses to persist one finding."""
        counter = {"index": 0}

        async def save(finding: dict[str, Any]) -> int:
            counter["index"] += 1
            row = AgentFinding(
                run_id=run.id,
                session_id=run.session_id,
                order_index=counter["index"],
                stage=finding.get("stage"),
                title=finding["title"],
                description=finding.get("description") or "",
                confidence=float(finding.get("confidence") or 0.0),
                uncertainty=finding.get("uncertainty") or None,
                evidence=finding.get("evidence") or [],
                status="accepted",
            )
            self.session.add(row)
            await self.session.commit()
            await self.session.refresh(row)

            return row.id

        return save

    async def finish_run(
        self, run_id: int, *, status: str, error: str | None = None
    ) -> None:
        row = await self.session.get(AgentRun, run_id)
        if row is None:
            return
        row.status = status
        row.cancel_requested = status == "cancelled"
        row.error = error
        row.finished_at = datetime.now(UTC)
        await self.session.commit()
