"""Agent chat stream, run cancellation and provider listing.

SSE helpers live here because this is the only router that streams.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.pipeline import (
    AnalysisRequest,
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
from app.models import Agent, AttackStep, User
from app.schemas.agent import AgentMessageIn
from app.services.agent_chat_service import AgentChatService
from app.services.agent_document_service import AgentDocumentService
from app.services.agent_draft_service import AgentDraftService, EditError
from app.services.bash_service import BashCommandService
from app.services.c2_service import CommandsC2ExecService
from app.services.llm_service import AgentLLMError, ChatStreamProvider
from app.services.ttp_info_service import UKC_PHASE_DESCRIPTIONS

logger = logging.getLogger(__name__)

router = APIRouter()

ChatDep = Annotated[AgentChatService, Depends(deps.get_agent_chat_service)]
DocumentDep = Annotated[AgentDocumentService, Depends(deps.get_agent_document_service)]
DraftDep = Annotated[AgentDraftService, Depends(deps.get_agent_draft_service)]
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


@router.post("/sessions/{session_id}/messages")
async def post_message(  # noqa: PLR0913, PLR0917 - FastAPI dependency signature
    session_id: int,
    payload: AgentMessageIn,
    user: UserDep,
    chat: ChatDep,
    documents: DocumentDep,
    drafts: DraftDep,
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
            if payload.mode == "ask":
                await answer_question(
                    request,
                    question=payload.content,
                    emit=emit,
                    cancel_event=cancel_event,
                )
                terminal = "completed"
            elif payload.mode == "edit":
                chain = await drafts.chain_for_edit(session_row, user)
                run_step = _make_step_runner(chat.session, chain, user, http_request)
                await edit_chain(
                    request,
                    instruction=payload.content,
                    emit=emit,
                    apply_ops=lambda ops: drafts.apply_edits(
                        ops, chain, user, runner=run_step
                    ),
                    cancel_event=cancel_event,
                )
                terminal = "completed"
            else:
                result = await analyze(
                    request,
                    emit=emit,
                    save_finding=chat.make_finding_saver(run),
                    cancel_event=cancel_event,
                )
                saved = await drafts.save_draft(
                    session_id=session_id,
                    run_id=run.id,
                    user=user,
                    payload=result.draft,
                    name=result.draft.get("name") or request.draft_name,
                )
                await queue.put(
                    (
                        "draft.ready",
                        {
                            "draft": {**result.draft, "draft_id": saved.id},
                            "warnings": result.warnings,
                        },
                    )
                )
                terminal = "completed"
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

    return StreamingResponse(
        _event_stream(queue, task, started, run.id, controller),
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


def _make_step_runner(session: AsyncSession, chain, user: User, http_request: Request):
    """executor handed to edit mode: runs the one step the operator clicked."""

    async def run_step(step: AttackStep) -> dict[str, Any]:
        events = getattr(http_request.app.state, "events_service", None)
        mythic = getattr(http_request.app.state, "mythic_client", None)
        if step.step_type == "bash":
            if events is None:
                raise EditError("bash executor is not running")
            data = await BashCommandService(session, events).start(
                step.command,
                chain.chain_name,
                user,
                timeout=get_settings().local_bash.timeout,
            )
            return {
                "status": data["status"],
                "new_step_id": data["step_id"],
            }
        if mythic is None:
            raise EditError("Mythic is not connected")
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
            )
        else:
            data = await c2.run_local_command(
                step.command,
                display_id,
                chain.id,
                step.phase,
                user,
            )

        return {"status": data["status"], "new_step_id": data["step_id"]}

    return run_step
