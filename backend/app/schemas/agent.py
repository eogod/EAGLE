"""schemas for the document-analysis agent (sessions, uploads, drafts)"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentSessionCreate(BaseModel):
    chain_id: int | None = None
    title: str | None = Field(default=None, max_length=256)


class AgentSessionUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=256)


class AgentMessageIn(BaseModel):
    """a chat turn; ask/plan/build change what the agent does with it"""

    content: str = Field(min_length=1, max_length=8000)
    document_id: int | None = None
    provider: str | None = None
    model: str | None = None
    mode: Literal["ask", "plan", "edit"] = "plan"


class AgentMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: list[dict[str, Any]]
    run_id: int | None
    create_time: datetime


class AgentDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    sha256: str
    mime: str
    size: int
    parse_status: str
    page_count: int | None
    char_count: int | None
    truncated: bool
    error: str | None


class AgentDraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: int
    name: str
    status: str
    version: int
    payload: dict[str, Any]


class AgentSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    chain_id: int | None
    title: str
    status: str
    create_time: datetime
    update_time: datetime


class AgentSessionDetail(AgentSessionOut):
    messages: list[AgentMessageOut] = []
    documents: list[AgentDocumentOut] = []
    draft: AgentDraftOut | None = None


class DraftImportRequest(BaseModel):
    chain_name: str = Field(min_length=1, max_length=256)


class DraftImportResponse(BaseModel):
    """steps are already shaped for ChainCanvas local (_tempId) steps"""

    draft_id: int
    name: str
    steps: list[dict[str, Any]]
    validation: dict[str, Any]
    replaced: int = 0
