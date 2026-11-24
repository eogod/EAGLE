"""Agent chat stream, run cancellation and provider listing.

SSE helpers live here because this is the only router that streams.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.agent.pipeline import (
    AnalysisRequest,
    EditCall,
    RunCancelled,
    analyze,
    answer_question,
    edit_chain,
)
from app.api import deps
from app.api.deps import AgentRunController
from app.core.config import (
    AGENT_DEFAULT_PROVIDER,
    CHUNK_CHARS,
    MAX_CHUNKS,
    get_settings,
    phases,
)
from app.models import AgentRun, AgentSession, User
from app.schemas.agent import AgentMessageIn
from app.services.agent_chat_service import AgentChatService
from app.services.agent_document_service import AgentDocumentService
from app.services.agent_draft_service import AgentDraftService
from app.services.auth_guard import StreamGuard, guarded_stream
from app.services.chain_proposal_service import ChainProposalService, ProposalInput
from app.services.llm_service import AgentLLMError, ChatStreamProvider
from app.services.ttp_info_service import UKC_PHASE_DESCRIPTIONS

logger = logging.getLogger(__name__)

router = APIRouter()

ChatDep = Annotated[AgentChatService, Depends(deps.get_agent_chat_service)]
DocumentDep = Annotated[AgentDocumentService, Depends(deps.get_agent_document_service)]
DraftDep = Annotated[AgentDraftService, Depends(deps.get_agent_draft_service)]
ProposalDep = Annotated[ChainProposalService, Depends(deps.get_chain_proposal_service)]
UserDep = Annotated[User, Depends(deps.get_current_user)]
AgentRunControllerDep = Annotated[
    AgentRunController, Depends(deps.get_agent_run_controller)
]

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def get_agent_provider(chat: ChatDep) -> ChatStreamProvider:
    """overridable in tests; a missing provider raises 503 in AgentChatService"""
    return chat.get_provider()


ProviderDep = Annotated[ChatStreamProvider, Depends(get_agent_provider)]


def _sse(event: str, data: dict[str, Any], seq: int) -> str:
    payload = json.dumps(data, default=str, ensure_ascii=False)

    return f"id: {seq}\nevent: {event}\ndata: {payload}\n\n"


def _phase_hints() -> str:
    return "\n".join(
        f"- {name}: {UKC_PHASE_DESCRIPTIONS.get(name, '')}".rstrip() for name in phases
    )


def _apply_event(parts: list[dict[str, Any]], name: str, data: dict[str, Any]) -> None:
    """reduce one SSE event into assistant-ui content parts (also used for storage)."""
    if name == "assistant.delta":
        text = str(data.get("text") or "")
        if not text:
            return
        if parts and parts[-1].get("type") == "text":
            parts[-1]["text"] = f"{parts[-1]['text']}{text}"
        else:
            parts.append({"type": "text", "text": text})
        return

    if name == "tool.started":
        args = data.get("args") or {}
        parts.append(
            {
                "type": "tool-call",
                "toolCallId": str(data.get("toolCallId") or ""),
                "toolName": str(data.get("toolName") or ""),
                "args": args,
                "argsText": json.dumps(args, default=str),
                "isPreliminary": True,
            }
        )
        return

    if name == "tool.completed":
        tool_id = str(data.get("toolCallId") or "")
        for part in parts:
            if part.get("type") == "tool-call" and part.get("toolCallId") == tool_id:
                part.pop("isPreliminary", None)
                part["result"] = data.get("result")
                part["isError"] = data.get("status") == "error"
                return

    if name == "edit.proposed":
        proposal_id = str(data.get("public_id") or "")
        if not proposal_id:
            return
        if any(
            part.get("type") == "edit-proposal"
            and part.get("proposalId") == proposal_id
            for part in parts
        ):
            return
        parts.append(
            {
                "type": "edit-proposal",
                "proposalId": proposal_id,
                "kind": data.get("kind") or "edit",
                "status": data.get("status") or "proposed",
            }
        )


async def _event_stream(
    queue: asyncio.Queue,
    task: asyncio.Task,
    start_frame: str,
    run_id: int,
    controller: AgentRunController,
) -> AsyncIterator[str]:
    """SSE frames for one run; a disconnected client cancels the worker."""
    seq = 0
    try:
        yield start_frame
        while True:
            item = await queue.get()
            if item is None:
                break
            seq += 1
            yield _sse(item[0], item[1], seq)
    finally:
        if not task.done():
            controller.cancel(run_id)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive on disconnect
                logger.warning("agent run task failed after client disconnect")


Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class ModeContext:
    request: AnalysisRequest
    payload: AgentMessageIn
    session_row: AgentSession
    user: User
    chat: AgentChatService
    drafts: AgentDraftService
    proposals: ChainProposalService
    emit: Emit
    cancel_event: asyncio.Event
    session_id: int
    run: AgentRun


async def _run_mode(ctx: ModeContext) -> str:
    if ctx.payload.mode == "ask":
        await answer_question(
            ctx.request,
            question=ctx.payload.content,
            emit=ctx.emit,
            cancel_event=ctx.cancel_event,
        )
        return "completed"

    if ctx.payload.mode in ("edit", "execute"):
        kind = "run" if ctx.payload.mode == "execute" else "edit"
        await ctx.drafts.chain_for_edit(ctx.session_row, ctx.user)

        async def propose(
            operations: list[dict[str, Any]], summary: str
        ) -> dict[str, Any]:
            proposal = await ctx.proposals.propose(
                ProposalInput(
                    session_row=ctx.session_row,
                    user=ctx.user,
                    kind=kind,
                    raw_ops=operations,
                    summary=summary,
                    run_id=ctx.run.id,
                    provider=ctx.request.provider.name,
                    model=ctx.request.provider.model,
                )
            )
            return ctx.proposals.preview(proposal)

        await edit_chain(
            ctx.request,
            EditCall(
                instruction=ctx.payload.content,
                propose_ops=propose,
                kind=kind,
                cancel_event=ctx.cancel_event,
            ),
            ctx.emit,
        )
        return "completed"

    result = await analyze(
        ctx.request,
        emit=ctx.emit,
        save_finding=ctx.chat.make_finding_saver(ctx.run),
        cancel_event=ctx.cancel_event,
    )
    saved = await ctx.drafts.save_draft(
        session_id=ctx.session_id,
        run_id=ctx.run.id,
        user=ctx.user,
        payload=result.draft,
        name=result.draft.get("name") or ctx.request.draft_name,
    )
    await ctx.emit(
        "draft.ready",
        {"draft": {**result.draft, "draft_id": saved.id}, "warnings": result.warnings},
    )
    return "completed"


@router.post("/sessions/{session_id}/messages")
async def post_message(  # noqa: PLR0913, PLR0917 - FastAPI dependency signature
    session_id: int,
    payload: AgentMessageIn,
    user: UserDep,
    chat: ChatDep,
    documents: DocumentDep,
    drafts: DraftDep,
    proposals: ProposalDep,
    provider: ProviderDep,
    http_request: Request,
    controller: AgentRunControllerDep,
) -> StreamingResponse:
    session_row = await chat.get_session(session_id, user)
    document, pages = await _load_session_document(
        documents, session_id, user, payload.document_id
    )

    # per-request provider/model override from the picker
    if payload.provider or payload.model:
        provider = chat.get_provider(provider=payload.provider, model=payload.model)

    user_message = await chat.add_user_message(session_id, payload.content)
    run = await chat.create_run(session_id, document.id if document else None)
    await chat.attach_message_to_run(user_message.id, run.id)
    cancel_event = controller.start(run.id)

    parts: list[dict[str, Any]] = []
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    async def emit(name: str, data: dict[str, Any]) -> None:
        _apply_event(parts, name, data)
        await queue.put((name, data))

    async def runner() -> None:
        terminal = "failed"
        error: str | None = None
        try:
            request = AnalysisRequest(
                filename=document.filename if document else "operator brief",
                pages=pages,
                provider=provider,
                phase_names=phases,
                phase_hints=_phase_hints(),
                risky_patterns=get_settings().local_bash.reject,
                chunk_chars=CHUNK_CHARS,
                max_chunks=MAX_CHUNKS,
                draft_name=(document.filename if document else "Operator brief"),
                mode=payload.mode,
                brief=payload.content,
                chain_context=await drafts.chain_context(session_row, user),
                research=get_settings().webtools.enabled,
            )
            terminal = await _run_mode(
                ModeContext(
                    request=request,
                    payload=payload,
                    session_row=session_row,
                    user=user,
                    chat=chat,
                    drafts=drafts,
                    proposals=proposals,
                    emit=emit,
                    cancel_event=cancel_event,
                    session_id=session_id,
                    run=run,
                )
            )
        except RunCancelled:
            terminal = "cancelled"
        except AgentLLMError as exc:
            error = str(exc)
            await queue.put(("run.error", {"code": "llm_error", "message": error}))
        except Exception as exc:  # keep the stream well-formed on any failure
            error = f"{type(exc).__name__}: {exc}"
            await queue.put(("run.error", {"code": "internal", "message": error}))
        finally:
            await chat.finish_run(run.id, status=terminal, error=error)
            message = await chat.save_assistant_message(
                session_id, run.id, parts or [{"type": "text", "text": ""}]
            )
            controller.finish(run.id)
            await queue.put(
                (
                    "run.completed" if terminal == "completed" else f"run.{terminal}",
                    {
                        "run_id": run.id,
                        "message_id": message.id,
                        "status": terminal,
                        "error": error,
                    },
                )
            )
            await queue.put(None)

    task = asyncio.create_task(runner())
    started = _sse(
        "run.started",
        {
            "run_id": run.id,
            "session_id": session_id,
            "mode": payload.mode,
            "provider": getattr(provider, "name", ""),
            "model": getattr(provider, "model", ""),
        },
        0,
    )

    inner = _event_stream(queue, task, started, run.id, controller)
    revoked = _sse(
        "auth.revoked",
        {"run_id": run.id, "session_id": session_id},
        0,
    )
    guarded = guarded_stream(
        inner,
        StreamGuard(
            session_id=http_request.state.auth_session_id,
            user_id=http_request.state.auth_user_id,
            interval=get_settings().security.stream_auth_recheck_secs,
            terminal=revoked,
            on_revoke=lambda: controller.cancel(run.id),
        ),
    )
    return StreamingResponse(
        guarded,
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/providers")
async def list_providers(user: UserDep, chat: ChatDep) -> dict[str, Any]:
    """what the operator actually has: ollama tags, openrouter catalog, g4f pool."""
    found = await chat.list_providers()
    cfg = get_settings().llmservice
    labels = {"ollama": "Ollama", "openrouter": "OpenRouter", "g4f": "g4f (free)"}
    providers = [
        {
            "id": name,
            "label": labels[name],
            "available": data["available"],
            "models": data["models"],
            "default": data["default"],
            "error": data["error"],
        }
        for name, data in found.items()
    ]

    return {
        "providers": providers,
        "default": {
            "provider": AGENT_DEFAULT_PROVIDER,
            "model": cfg.default_model,
            "mode": "plan",
        },
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: int,
    user: UserDep,
    chat: ChatDep,
    controller: AgentRunControllerDep,
) -> dict[str, Any]:
    run = await chat.get_run(run_id, user)
    requested = controller.cancel(run_id)
    if not requested:
        # the stream may already be gone; mark the row so a retry is not left hanging
        await chat.request_cancel(run.id)

    return {"status": "cancelling", "run_id": run.id}


async def _load_session_document(
    documents: AgentDocumentService,
    session_id: int,
    user: User,
    document_id: int | None,
):
    """load the session's document; an id from another session is a 400."""
    if document_id is None:
        return None, []

    document, _ = await documents.get_document(document_id, user)
    if document.session_id != session_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="document belongs to another session",
        )
    if document.parse_status != "parsed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="document is not parsed"
        )

    return document, documents.load_pages(document)
