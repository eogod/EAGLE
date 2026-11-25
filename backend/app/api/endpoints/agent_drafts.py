"""Agent draft lookup and import into the chain canvas."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api import deps
from app.models import User
from app.schemas.agent import AgentDraftOut, DraftImportRequest, DraftImportResponse
from app.services.agent_draft_service import AgentDraftService

router = APIRouter()

DraftDep = Annotated[AgentDraftService, Depends(deps.get_agent_draft_service)]
UserDep = Annotated[User, Depends(deps.get_current_user)]


@router.get("/drafts/{draft_id}", response_model=AgentDraftOut)
async def get_draft(draft_id: int, user: UserDep, drafts: DraftDep) -> AgentDraftOut:
    row = await drafts.get_draft(draft_id, user)

    return AgentDraftOut.model_validate(row)


@router.post("/drafts/{draft_id}/import", response_model=DraftImportResponse)
async def import_draft(
    draft_id: int,
    payload: DraftImportRequest,
    user: UserDep,
    drafts: DraftDep,
) -> DraftImportResponse:
    row = await drafts.get_draft(draft_id, user)
    result = await drafts.import_draft(row, user, chain_name=payload.chain_name)

    return DraftImportResponse(**result)
