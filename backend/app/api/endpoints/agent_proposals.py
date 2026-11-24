"""review and approval endpoints for agent proposals"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api import deps
from app.models import ChainProposal, User
from app.schemas.agent import (
    AgentProposalOut,
    ProposalApplyIn,
    ProposalApproveIn,
    ProposalExecuteIn,
    ProposalRejectIn,
)
from app.services.chain_proposal_service import ChainProposalService
from app.services.chain_service import ChainService
from app.services.step_runner import make_step_runner

router = APIRouter()

UserDep = Annotated[User, Depends(deps.get_current_user)]
ServiceDep = Annotated[ChainProposalService, Depends(deps.get_chain_proposal_service)]


@dataclass(frozen=True)
class ExecDeps:
    request: Request
    user: User
    service: ChainProposalService


def get_exec_deps(
    request: Request,
    user: UserDep,
    service: ServiceDep,
) -> ExecDeps:
    return ExecDeps(request=request, user=user, service=service)


def _out(service: ChainProposalService, proposal: ChainProposal) -> AgentProposalOut:
    return AgentProposalOut(**service.preview(proposal))


def _in_session(proposal: ChainProposal, session_id: int) -> None:
    if proposal.session_id != session_id:
        raise HTTPException(status_code=404, detail="proposal not found")


@router.get(
    "/sessions/{session_id}/proposals",
    response_model=list[AgentProposalOut],
)
async def list_proposals(
    session_id: int,
    user: UserDep,
    service: ServiceDep,
    status: str = Query(default="proposed,approved"),
) -> list[AgentProposalOut]:
    names = tuple(item.strip() for item in status.split(",") if item.strip())
    rows = await service.list_for_session(session_id, user, names or ("proposed",))
    return [_out(service, row) for row in rows]


@router.get(
    "/sessions/{session_id}/proposals/{public_id}",
    response_model=AgentProposalOut,
)
async def get_proposal(
    session_id: int,
    public_id: str,
    user: UserDep,
    service: ServiceDep,
) -> AgentProposalOut:
    proposal = await service.get(public_id, user)
    _in_session(proposal, session_id)
    return _out(service, proposal)


@router.post(
    "/sessions/{session_id}/proposals/{public_id}/approve",
    response_model=AgentProposalOut,
)
async def approve_proposal(
    session_id: int,
    public_id: str,
    body: ProposalApproveIn,
    user: UserDep,
    service: ServiceDep,
) -> AgentProposalOut:
    _in_session(await service.get(public_id, user), session_id)
    proposal = await service.approve(
        public_id, user, acknowledge_dangerous=body.acknowledge_dangerous
    )
    return _out(service, proposal)


@router.post(
    "/sessions/{session_id}/proposals/{public_id}/apply",
    response_model=AgentProposalOut,
)
async def apply_proposal(
    session_id: int,
    public_id: str,
    body: ProposalApplyIn,
    user: UserDep,
    service: ServiceDep,
) -> AgentProposalOut:
    _in_session(await service.get(public_id, user), session_id)
    proposal = await service.apply(
        public_id, user, acknowledge_dangerous=body.acknowledge_dangerous
    )
    return _out(service, proposal)


@router.post(
    "/sessions/{session_id}/proposals/{public_id}/reject",
    response_model=AgentProposalOut,
)
async def reject_proposal(
    session_id: int,
    public_id: str,
    body: ProposalRejectIn,
    user: UserDep,
    service: ServiceDep,
) -> AgentProposalOut:
    _in_session(await service.get(public_id, user), session_id)
    proposal = await service.reject(public_id, user, reason=body.reason)
    return _out(service, proposal)


@router.post(
    "/sessions/{session_id}/proposals/{public_id}/execute",
    response_model=AgentProposalOut,
)
async def execute_proposal(
    session_id: int,
    public_id: str,
    body: ProposalExecuteIn,
    ctx: ExecDeps = Depends(get_exec_deps),
) -> AgentProposalOut:
    proposal = await ctx.service.get(public_id, ctx.user)
    _in_session(proposal, session_id)
    chain = await ChainService(ctx.service.session).owned_chain(
        proposal.chain_id, ctx.user
    )
    events = getattr(ctx.request.app.state, "events_service", None)
    mythic = getattr(ctx.request.app.state, "mythic_client", None)
    runner = make_step_runner(ctx.service.session, chain, ctx.user, events, mythic)
    result = await ctx.service.execute(
        public_id,
        ctx.user,
        runner,
        acknowledge_dangerous=body.acknowledge_dangerous,
    )
    return _out(ctx.service, result)
