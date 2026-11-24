"""agent edit/run proposals: review gate, fingerprints and ownership"""

import time
import uuid

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select

from app.models import (
    AgentSession,
    AttackChain,
    AttackStep,
    ChainAuditEvent,
    CurrentAttackPhase,
    User,
)
from app.services.chain_proposal_service import ChainProposalService, ProposalInput

pytestmark = pytest.mark.asyncio(loop_scope="session")

ADD_STEP = {
    "op": "add_step",
    "phase": "Execution",
    "step_type": "bash",
    "tool_name": "shell",
    "command": "echo proposed",
}


async def _setup_chain(session, user: User) -> tuple[AttackChain, AgentSession]:
    chain = AttackChain(
        user_id=user.user_id,
        chain_name=f"proposal-{uuid.uuid4().hex[:8]}",
        final_status="execution",
    )
    session.add(chain)
    await session.flush()
    session.add(CurrentAttackPhase(chain_id=chain.id, phase="Reconnaissance"))
    agent_session = AgentSession(user_id=user.user_id, chain_id=chain.id, title="case")
    session.add(agent_session)
    await session.commit()
    return chain, agent_session


async def _step(
    session,
    chain: AttackChain,
    *,
    command: str = "echo planned",
    status: str = "planned",
    step_type: str = "bash",
) -> AttackStep:
    row = AttackStep(
        chain_id=chain.id,
        phase="Execution",
        step_type=step_type,
        tool_name="shell",
        command=command,
        mythic_task_id=0,
        mythic_payload_id=0,
        mythic_payload_uuid=str(uuid.uuid4()),
        raw_log="",
        status=status,
        is_draft=False,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _input(
    agent_session: AgentSession, user: User, ops, kind: str = "edit"
) -> ProposalInput:
    return ProposalInput(
        session_row=agent_session,
        user=user,
        kind=kind,
        raw_ops=ops,
        summary="test",
        run_id=None,
        provider="fake",
        model="fake-model",
    )


async def _phase(session, chain: AttackChain) -> str:
    return await session.scalar(
        select(CurrentAttackPhase.phase).where(CurrentAttackPhase.chain_id == chain.id)
    )


def _status(exc: pytest.ExceptionInfo[HTTPException]) -> int:
    return exc.value.status_code


async def test_edit_proposal_needs_approval_then_applies_once(session, default_user):
    chain, agent_session = await _setup_chain(session, default_user)
    service = ChainProposalService(session)

    proposal = await service.propose(
        _input(
            agent_session,
            default_user,
            [ADD_STEP, {"op": "set_phase", "phase": "Execution"}],
        )
    )
    assert proposal.status == "proposed"
    # proposing is not applying
    assert (
        await session.scalar(select(AttackStep).where(AttackStep.chain_id == chain.id))
        is None
    )
    assert await _phase(session, chain) == "Reconnaissance"

    approved = await service.approve(proposal.public_id, default_user)
    assert approved.status == "approved"
    applied = await service.apply(proposal.public_id, default_user)
    assert applied.status == "applied"

    step = await session.scalar(
        select(AttackStep).where(AttackStep.chain_id == chain.id)
    )
    assert step is not None
    assert (step.command, step.status, step.is_draft) == (
        "echo proposed",
        "pending",
        False,
    )
    assert await _phase(session, chain) == "Execution"

    actions = set(
        (
            await session.scalars(
                select(ChainAuditEvent.action).where(
                    ChainAuditEvent.proposal_id == proposal.id
                )
            )
        ).all()
    )
    assert {"proposal.created", "proposal.approved", "proposal.applied"} <= actions


async def test_apply_before_approve_and_repeated_apply_are_rejected(
    session, default_user
):
    _chain, agent_session = await _setup_chain(session, default_user)
    service = ChainProposalService(session)
    proposal = await service.propose(_input(agent_session, default_user, [ADD_STEP]))

    with pytest.raises(HTTPException) as early:
        await service.apply(proposal.public_id, default_user)
    assert _status(early) == 409

    await service.approve(proposal.public_id, default_user)
    await service.apply(proposal.public_id, default_user)

    with pytest.raises(HTTPException) as again:
        await service.apply(proposal.public_id, default_user)
    assert _status(again) == 409

    steps = (
        await session.scalars(
            select(AttackStep).where(AttackStep.chain_id == proposal.chain_id)
        )
    ).all()
    assert len(steps) == 1


async def test_expired_proposal_cannot_be_approved_or_applied(session, default_user):
    _chain, agent_session = await _setup_chain(session, default_user)
    service = ChainProposalService(session)

    expired = await service.propose(_input(agent_session, default_user, [ADD_STEP]))
    expired.expires_at = int(time.time()) - 1
    await session.commit()
    with pytest.raises(HTTPException) as approve_exc:
        await service.approve(expired.public_id, default_user)
    assert _status(approve_exc) == 410

    approved_but_expired = await service.propose(
        _input(agent_session, default_user, [ADD_STEP])
    )
    approved_but_expired.status = "approved"
    approved_but_expired.expires_at = int(time.time()) - 1
    await session.commit()
    with pytest.raises(HTTPException) as apply_exc:
        await service.apply(approved_but_expired.public_id, default_user)
    assert _status(apply_exc) == 410


async def test_chain_changed_between_propose_and_approve(session, default_user):
    chain, agent_session = await _setup_chain(session, default_user)
    service = ChainProposalService(session)
    proposal = await service.propose(_input(agent_session, default_user, [ADD_STEP]))

    current = await session.scalar(
        select(CurrentAttackPhase).where(CurrentAttackPhase.chain_id == chain.id)
    )
    current.phase = "Execution"
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await service.approve(proposal.public_id, default_user)
    assert _status(exc) == 409


async def test_ops_must_match_the_proposal_kind(session, default_user):
    chain, agent_session = await _setup_chain(session, default_user)
    step = await _step(session, chain)
    service = ChainProposalService(session)

    run_in_edit = await service.propose(
        _input(agent_session, default_user, [{"op": "run_step", "step_id": step.id}])
    )
    assert run_in_edit.status == "invalid"
    assert any("run_step" in e for e in run_in_edit.validation["errors"])

    add_in_run = await service.propose(
        _input(agent_session, default_user, [ADD_STEP], kind="run")
    )
    assert add_in_run.status == "invalid"
    assert any("add_step" in e for e in add_in_run.validation["errors"])

    mixed = await service.propose(
        _input(
            agent_session,
            default_user,
            [{"op": "run_step", "step_id": step.id}, ADD_STEP],
            kind="run",
        )
    )
    assert mixed.status == "invalid"
    assert any("add_step" in e for e in mixed.validation["errors"])


async def test_run_proposal_executes_once_and_checks_fingerprint(session, default_user):
    chain, agent_session = await _setup_chain(session, default_user)
    step = await _step(session, chain, command="echo run")
    service = ChainProposalService(session)

    proposal = await service.propose(
        _input(
            agent_session,
            default_user,
            [{"op": "run_step", "step_id": step.id}],
            kind="run",
        )
    )
    assert proposal.status == "proposed"
    await service.approve(proposal.public_id, default_user)

    ran: list[int] = []

    async def runner(row: AttackStep) -> dict:
        ran.append(row.id)
        return {"status": "submitted", "new_step_id": row.id}

    executed = await service.execute(proposal.public_id, default_user, runner)
    assert executed.status == "executed"
    assert executed.result == [{"status": "submitted", "new_step_id": step.id}]
    assert ran == [step.id]

    with pytest.raises(HTTPException) as again:
        await service.execute(proposal.public_id, default_user, runner)
    assert _status(again) == 409

    other = await _step(session, chain, command="echo other")
    stale = await service.propose(
        _input(
            agent_session,
            default_user,
            [{"op": "run_step", "step_id": other.id}],
            kind="run",
        )
    )
    await service.approve(stale.public_id, default_user)
    other.command = "echo changed"
    await session.commit()

    with pytest.raises(HTTPException) as stale_exc:
        await service.execute(stale.public_id, default_user, runner)
    assert _status(stale_exc) == 409
    assert ran == [step.id]


async def test_proposal_is_invisible_to_another_user(session, default_user):
    _chain, agent_session = await _setup_chain(session, default_user)
    service = ChainProposalService(session)
    proposal = await service.propose(_input(agent_session, default_user, [ADD_STEP]))

    other = User(
        user_id=str(uuid.uuid4()),
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
    )
    session.add(other)
    await session.commit()

    for call in (
        service.get(proposal.public_id, other),
        service.approve(proposal.public_id, other),
        service.apply(proposal.public_id, other),
    ):
        with pytest.raises(HTTPException) as exc:
            await call
        assert _status(exc) == 404


async def test_http_apply_rejects_client_supplied_operations(
    client: AsyncClient, session, default_user, default_user_headers
):
    _chain, agent_session = await _setup_chain(session, default_user)
    proposal = await ChainProposalService(session).propose(
        _input(agent_session, default_user, [ADD_STEP])
    )

    response = await client.post(
        f"/api/agent/sessions/{agent_session.id}/proposals/{proposal.public_id}/apply",
        json={"acknowledge_dangerous": False, "operations": [ADD_STEP]},
        headers=default_user_headers,
    )
    assert response.status_code == 422


async def test_invalid_operations_are_stored_as_invalid(session, default_user):
    chain, agent_session = await _setup_chain(session, default_user)
    step = await _step(session, chain)
    service = ChainProposalService(session)

    cases = [
        (
            [{"op": "set_phase", "phase": "Execution"}] * 11,
            "at most 10 operations",
        ),
        (
            [dict(ADD_STEP, phase="Nowhere")],
            "unknown phase",
        ),
        (
            [dict(ADD_STEP, step_type="nmap")],
            "unsupported step_type",
        ),
        (
            [dict(ADD_STEP, command="echo <TARGET>")],
            "unfilled placeholder",
        ),
        (
            [dict(ADD_STEP, command="   ")],
            "add_step needs a command",
        ),
        (
            [
                {"op": "remove_step", "step_id": step.id},
                {"op": "remove_step", "step_id": step.id},
            ],
            "targeted twice",
        ),
    ]

    for ops, expected in cases:
        proposal = await service.propose(_input(agent_session, default_user, ops))
        assert proposal.status == "invalid", expected
        assert any(expected in error for error in proposal.validation["errors"]), (
            proposal.validation["errors"]
        )


async def test_remove_step_of_an_executed_step_is_invalid(session, default_user):
    chain, agent_session = await _setup_chain(session, default_user)
    step = await _step(session, chain, status="success")
    service = ChainProposalService(session)

    proposal = await service.propose(
        _input(
            agent_session,
            default_user,
            [{"op": "remove_step", "step_id": step.id}],
        )
    )
    assert proposal.status == "invalid"
    assert any("is success" in error for error in proposal.validation["errors"])

    assert await session.get(AttackStep, step.id) is not None


async def test_new_proposal_supersedes_proposed_and_approved_blocks(
    session, default_user
):
    _, agent_session = await _setup_chain(session, default_user)
    service = ChainProposalService(session)

    first = await service.propose(_input(agent_session, default_user, [ADD_STEP]))
    second = await service.propose(_input(agent_session, default_user, [ADD_STEP]))
    await session.refresh(first)
    assert first.status == "expired"
    assert second.status == "proposed"

    await service.approve(second.public_id, default_user)
    with pytest.raises(HTTPException) as exc:
        await service.propose(_input(agent_session, default_user, [ADD_STEP]))
    assert _status(exc) == 409


async def test_expired_proposals_persist_on_get_and_list(session, default_user):
    _, agent_session = await _setup_chain(session, default_user)
    service = ChainProposalService(session)
    proposal = await service.propose(_input(agent_session, default_user, [ADD_STEP]))
    proposal.expires_at = int(time.time()) - 1
    session.add(proposal)
    await session.commit()

    fetched = await service.get(proposal.public_id, default_user)
    assert fetched.status == "expired"

    listed = await service.list_for_session(
        agent_session.id, default_user, ("proposed", "approved")
    )
    assert listed == []


async def test_dangerous_steps_need_explicit_ack_every_time(session, default_user):
    _, agent_session = await _setup_chain(session, default_user)
    service = ChainProposalService(session)
    proposal = await service.propose(_input(agent_session, default_user, [ADD_STEP]))
    proposal.validation = {
        "errors": [],
        "warnings": [],
        "dangerous_steps": [1],
    }
    session.add(proposal)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await service.approve(proposal.public_id, default_user)
    assert _status(exc) == 422

    approved = await service.approve(
        proposal.public_id, default_user, acknowledge_dangerous=True
    )
    assert approved.danger_ack_at is not None

    with pytest.raises(HTTPException) as exc:
        await service.apply(proposal.public_id, default_user)
    assert _status(exc) == 422

    applied = await service.apply(
        proposal.public_id, default_user, acknowledge_dangerous=True
    )
    assert applied.status == "applied"


async def test_apply_and_execute_reject_the_wrong_kind(session, default_user):
    chain, agent_session = await _setup_chain(session, default_user)
    step = await _step(session, chain)
    service = ChainProposalService(session)

    edit = await service.propose(_input(agent_session, default_user, [ADD_STEP]))
    await service.approve(edit.public_id, default_user)
    with pytest.raises(HTTPException) as exc:
        await service.execute(edit.public_id, default_user, None)
    assert _status(exc) == 403

    run = await service.propose(
        _input(
            agent_session,
            default_user,
            [{"op": "run_step", "step_id": step.id}],
            kind="run",
        )
    )
    await service.approve(run.public_id, default_user)
    with pytest.raises(HTTPException) as exc:
        await service.apply(run.public_id, default_user)
    assert _status(exc) == 403


async def test_failed_runner_marks_the_run_proposal_failed(session, default_user):
    chain, agent_session = await _setup_chain(session, default_user)
    step = await _step(session, chain)
    service = ChainProposalService(session)
    proposal = await service.propose(
        _input(
            agent_session,
            default_user,
            [{"op": "run_step", "step_id": step.id}],
            kind="run",
        )
    )
    await service.approve(proposal.public_id, default_user)

    async def boom(_step):
        raise RuntimeError("no executor")

    with pytest.raises(HTTPException) as exc:
        await service.execute(proposal.public_id, default_user, boom)
    assert _status(exc) == 502

    await session.refresh(proposal)
    assert proposal.status == "failed"
    assert proposal.error == "RuntimeError"


async def test_reject_refuses_a_run_in_flight(session, default_user):
    chain, agent_session = await _setup_chain(session, default_user)
    step = await _step(session, chain)
    service = ChainProposalService(session)
    proposal = await service.propose(
        _input(
            agent_session,
            default_user,
            [{"op": "run_step", "step_id": step.id}],
            kind="run",
        )
    )
    await service.approve(proposal.public_id, default_user)
    proposal.status = "executing"
    session.add(proposal)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await service.reject(proposal.public_id, default_user)
    assert _status(exc) == 409


async def test_apply_remove_step_and_set_phase_when_phase_row_is_missing(
    session, default_user
):
    chain, agent_session = await _setup_chain(session, default_user)
    step = await _step(session, chain)
    phase_row = await session.scalar(
        select(CurrentAttackPhase).where(CurrentAttackPhase.chain_id == chain.id)
    )
    assert phase_row is not None
    await session.delete(phase_row)
    await session.commit()

    service = ChainProposalService(session)
    proposal = await service.propose(
        _input(
            agent_session,
            default_user,
            [
                {"op": "remove_step", "step_id": step.id},
                {"op": "set_phase", "phase": "Execution"},
            ],
        )
    )
    await service.approve(proposal.public_id, default_user)
    await service.apply(proposal.public_id, default_user)

    assert await session.get(AttackStep, step.id) is None
    assert await _phase(session, chain) == "Execution"


async def test_reject_success_and_refuses_applied(session, default_user):
    _, agent_session = await _setup_chain(session, default_user)
    service = ChainProposalService(session)

    first = await service.propose(_input(agent_session, default_user, [ADD_STEP]))
    rejected = await service.reject(first.public_id, default_user, reason="no")
    assert rejected.status == "rejected"
    assert rejected.rejected_at is not None

    second = await service.propose(_input(agent_session, default_user, [ADD_STEP]))
    await service.approve(second.public_id, default_user)
    await service.apply(second.public_id, default_user)
    with pytest.raises(HTTPException) as exc:
        await service.reject(second.public_id, default_user)
    assert _status(exc) == 409


async def test_invalid_proposal_cannot_be_approved(session, default_user):
    _, agent_session = await _setup_chain(session, default_user)
    service = ChainProposalService(session)
    proposal = await service.propose(
        _input(agent_session, default_user, [dict(ADD_STEP, command="")])
    )
    assert proposal.status == "invalid"

    with pytest.raises(HTTPException) as exc:
        await service.approve(proposal.public_id, default_user)
    assert _status(exc) == 409


async def test_list_returns_open_and_commits_expiry(session, default_user):
    _, agent_session = await _setup_chain(session, default_user)
    service = ChainProposalService(session)
    proposal = await service.propose(_input(agent_session, default_user, [ADD_STEP]))

    open_rows = await service.list_for_session(
        agent_session.id, default_user, ("proposed", "approved")
    )
    assert [row.public_id for row in open_rows] == [proposal.public_id]


async def test_second_approve_is_a_conflict(session, default_user):
    _, agent_session = await _setup_chain(session, default_user)
    service = ChainProposalService(session)
    proposal = await service.propose(_input(agent_session, default_user, [ADD_STEP]))
    await service.approve(proposal.public_id, default_user)

    with pytest.raises(HTTPException) as exc:
        await service.approve(proposal.public_id, default_user)
    assert _status(exc) == 409


async def test_apply_rolls_back_when_remove_target_disappears(session, default_user):
    chain, agent_session = await _setup_chain(session, default_user)
    step = await _step(session, chain)
    service = ChainProposalService(session)
    proposal = await service.propose(
        _input(
            agent_session,
            default_user,
            [{"op": "remove_step", "step_id": step.id}],
        )
    )
    await service.approve(proposal.public_id, default_user)

    # another operator deleted the row after approval
    await session.delete(step)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await service.apply(proposal.public_id, default_user)
    assert _status(exc) == 409

    fresh = await session.get(type(proposal), proposal.id)
    assert fresh is not None and fresh.status == "approved"
