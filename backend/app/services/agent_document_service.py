"""Untrusted document ingestion for the analysis agent.

Bytes stay hostile until `app.agent.parsing` has parsed them; files are written
under the agent workspace, never at a path taken from the client.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.parsing import DocumentError, mime_of, parse_bytes, suffix_of
from app.core.config import AGENT_WORKSPACE_DIR, MAX_DOCUMENT_CHARS, MAX_DOCUMENT_MB
from app.models import AgentDocument, AgentSession, User
from app.services.agent_scope import owned_document, owned_session


class AgentDocumentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_documents(self, session_id: int) -> list[AgentDocument]:
        result = await self.session.scalars(
            select(AgentDocument)
            .where(AgentDocument.session_id == session_id)
            .order_by(AgentDocument.id)
        )

        return list(result)

    async def add_document(
        self, session_id: int, user: User, *, filename: str, data: bytes
    ) -> AgentDocument:
        await owned_session(self.session, session_id, user)
        limit = MAX_DOCUMENT_MB * 1024 * 1024
        if len(data) > limit:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"document is larger than {MAX_DOCUMENT_MB} MB",
            )

        safe_name = Path(filename or "upload").name[:512]
        try:
            parsed = parse_bytes(safe_name, data, max_chars=MAX_DOCUMENT_CHARS)
        except DocumentError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc

        row = AgentDocument(
            session_id=session_id,
            filename=safe_name,
            sha256=_sha256_hex(data),
            mime=mime_of(safe_name),
            size=len(data),
            storage_path="",
            parse_status="parsed",
            page_count=parsed.page_count,
            char_count=parsed.char_count,
            truncated=parsed.truncated,
            error=None,
        )
        self.session.add(row)
        await self.session.flush()

        session_dir = _workspace_dir() / str(session_id)
        session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        suffix = suffix_of(safe_name) or ".bin"
        raw_path = session_dir / f"{row.id}-{row.sha256[:16]}{suffix}"
        parsed_path = session_dir / f"{row.id}-{row.sha256[:16]}.pages.json"
        raw_path.write_bytes(data)
        parsed_path.write_text(
            json.dumps(
                {
                    "extractor": parsed.extractor,
                    "pages": [[page, text] for page, text in parsed.pages],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        row.storage_path = str(raw_path)
        row.parsed_path = str(parsed_path)
        await self.session.commit()
        await self.session.refresh(row)

        return row

    async def get_document(
        self, document_id: int, user: User
    ) -> tuple[AgentDocument, AgentSession]:
        return await owned_document(self.session, document_id, user)

    def load_pages(self, document: AgentDocument) -> list[tuple[int | None, str]]:
        if not document.parsed_path:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="document has no parsed text",
            )
        payload = json.loads(Path(document.parsed_path).read_text(encoding="utf-8"))

        return [(page, text) for page, text in payload.get("pages", [])]


def _workspace_dir() -> Path:
    AGENT_WORKSPACE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

    return AGENT_WORKSPACE_DIR


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
