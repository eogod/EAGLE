"""Agent session CRUD and document upload."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.api import deps
from app.core.config import MAX_DOCUMENT_MB
from app.models import User
from app.schemas.agent import (
    AgentDocumentOut,
    AgentDraftOut,
    AgentMessageOut,
    AgentSessionCreate,
    AgentSessionDetail,
    AgentSessionOut,
    AgentSessionUpdate,
)
from app.services.agent_chat_service import AgentChatService
from app.services.agent_document_service import AgentDocumentService
from app.services.agent_draft_service import AgentDraftService

router = APIRouter()

ChatDep = Annotated[AgentChatService, Depends(deps.get_agent_chat_service)]
DocumentDep = Annotated[AgentDocumentService, Depends(deps.get_agent_document_service)]
DraftDep = Annotated[AgentDraftService, Depends(deps.get_agent_draft_service)]
UserDep = Annotated[User, Depends(deps.get_current_user)]


@router.post(
    "/sessions",
    response_model=AgentSessionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    payload: AgentSessionCreate,
    user: UserDep,
    chat: ChatDep,
) -> AgentSessionOut:
    row = await chat.create_session(
        user, chain_id=payload.chain_id, title=payload.title
    )

    return AgentSessionOut.model_validate(row)


@router.get("/sessions", response_model=list[AgentSessionOut])
async def list_sessions(user: UserDep, chat: ChatDep) -> list[AgentSessionOut]:
    rows = await chat.list_sessions(user)

    return [AgentSessionOut.model_validate(row) for row in rows]


@router.get("/sessions/{session_id}", response_model=AgentSessionDetail)
async def get_session(
    session_id: int,
    user: UserDep,
    chat: ChatDep,
    documents: DocumentDep,
    drafts: DraftDep,
) -> AgentSessionDetail:
    row = await chat.get_session(session_id, user)
    messages = await chat.list_messages(session_id)
    documents_rows = await documents.list_documents(session_id)
    draft = await drafts.get_latest_draft(session_id)

    return AgentSessionDetail(
        **AgentSessionOut.model_validate(row).model_dump(),
        messages=[AgentMessageOut.model_validate(m) for m in messages],
        documents=[AgentDocumentOut.model_validate(d) for d in documents_rows],
        draft=AgentDraftOut.model_validate(draft) if draft else None,
    )


@router.patch("/sessions/{session_id}", response_model=AgentSessionOut)
async def update_session(
    session_id: int, payload: AgentSessionUpdate, user: UserDep, chat: ChatDep
) -> AgentSessionOut:
    row = await chat.update_session(session_id, user, title=payload.title)

    return AgentSessionOut.model_validate(row)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: int, user: UserDep, chat: ChatDep) -> None:
    await chat.delete_session(session_id, user)


@router.post(
    "/sessions/{session_id}/documents",
    response_model=AgentDocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    session_id: int,
    user: UserDep,
    documents: DocumentDep,
    file: Annotated[UploadFile, File(...)],
) -> AgentDocumentOut:
    limit = MAX_DOCUMENT_MB * 1024 * 1024
    data = await _read_limited(file, limit)
    row = await documents.add_document(
        session_id, user, filename=file.filename or "upload", data=data
    )

    return AgentDocumentOut.model_validate(row)


async def _read_limited(file: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"document is larger than {limit // (1024 * 1024)} MB",
            )
        chunks.append(chunk)

    if not chunks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="empty upload"
        )

    return b"".join(chunks)
