"""chain service ownership and lookup branches not covered by the api tests"""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import phases
from app.models import AttackChain, AttackStep, CurrentAttackPhase, User
from app.schemas.requests import DraftStepCreate
from app.services.chain_service import ChainService

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _chain(session, user: User, name: str | None = None) -> AttackChain:
    row = AttackChain(
        user_id=user.user_id,
        chain_name=name or f"edges-{uuid.uuid4().hex[:8]}",
        final_status="execution",
    )
    session.add(row)
    await session.flush()
    session.add(CurrentAttackPhase(chain_id=row.id, phase="Reconnaissance"))
    await session.commit()
    return row


def _step(
    chain: AttackChain, *, is_draft: bool, command: str = "echo hi"
) -> AttackStep:
    return AttackStep(
        chain_id=chain.id,
        phase="Execution",
        step_type="bash",
        tool_name="shell",
        command=command,
        mythic_task_id=0,
        mythic_payload_id=0,
        mythic_payload_uuid=str(uuid.uuid4()),
        raw_log="",
        status="pending",
        is_draft=is_draft,
    )


async def _status(exc: pytest.ExceptionInfo[HTTPException]) -> int:
    return exc.value.status_code


async def test_get_chain_n_phase_happy_and_missing_parts(session, default_user):
    service = ChainService(session)
    chain = await _chain(session, default_user)

    name, chain_id, phase = await service.get_chain_n_phase(
        chain.chain_name, default_user
    )
    assert (name, chain_id, phase) == (
        chain.chain_name,
        chain.id,
        "Reconnaissance",
    )

    with pytest.raises(HTTPException) as exc:
        await service.get_chain_n_phase("missing", default_user)
    assert await _status(exc) == 404

    phase_row = await session.scalar(
        select(CurrentAttackPhase).where(CurrentAttackPhase.chain_id == chain.id)
    )
    await session.delete(phase_row)
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await service.get_chain_n_phase(chain.chain_name, default_user)
    assert await _status(exc) == 404


async def test_owned_step_filters_draft_class(session, default_user):
    service = ChainService(session)
    chain = await _chain(session, default_user)
    real = _step(chain, is_draft=False)
    draft = _step(chain, is_draft=True)
    session.add(real)
    session.add(draft)
    await session.commit()

    assert (await service.owned_step(real.id, default_user, drafts=False)).id == real.id
    assert (
        await service.owned_step(draft.id, default_user, drafts=True)
    ).id == draft.id
    assert (await service.owned_step(draft.id, default_user)).id == draft.id

    with pytest.raises(HTTPException):
        await service.owned_step(draft.id, default_user, drafts=False)
    with pytest.raises(HTTPException):
        await service.owned_step(999999, default_user)


async def test_create_chain_duplicate_name_is_a_400(session, default_user):
    service = ChainService(session)
    await service.create_chain(f"dup-{uuid.uuid4().hex[:6]}", default_user)
    with pytest.raises(HTTPException) as exc:
        await service.create_chain(
            (await service.list_chains(default_user))[0]["chain_name"], default_user
        )
    assert await _status(exc) == 400


async def test_delete_draft_rejects_a_step_from_another_chain(session, default_user):
    service = ChainService(session)
    first = await _chain(session, default_user)
    second = await _chain(session, default_user)
    draft = _step(first, is_draft=True)
    session.add(draft)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await service.delete_draft_step(second.id, draft.id, default_user)
    assert await _status(exc) == 404
    assert await session.get(AttackStep, draft.id) is not None


async def test_edit_draft_step_validates_phase_and_type(session, default_user):
    service = ChainService(session)
    chain = await _chain(session, default_user)
    draft = await service.create_draft_step(
        chain.id,
        default_user,
        DraftStepCreate(phase="Execution", command="echo hi"),
    )

    with pytest.raises(HTTPException) as exc:
        await service.edit_draft_step(
            chain.id, draft.id, default_user, {"phase": "Nowhere"}
        )
    assert await _status(exc) == 422

    with pytest.raises(HTTPException) as exc:
        await service.edit_draft_step(
            chain.id, draft.id, default_user, {"step_type": "custom"}
        )
    assert await _status(exc) == 422

    edited = await service.edit_draft_step(
        chain.id,
        draft.id,
        default_user,
        {"command": "", "delay_before_secs": "bad", "unknown": "ignored"},
    )
    assert edited.command == ""


async def test_create_draft_rejects_unknown_phase_and_empty_command(
    session, default_user
):
    service = ChainService(session)
    chain = await _chain(session, default_user)

    with pytest.raises(HTTPException) as exc:
        await service.create_draft_step(
            chain.id,
            default_user,
            DraftStepCreate(phase="Nowhere", command="echo hi"),
        )
    assert await _status(exc) == 422

    with pytest.raises(HTTPException) as exc:
        await service.create_draft_step(
            chain.id,
            default_user,
            DraftStepCreate(phase="Execution", command="   "),
        )
    assert await _status(exc) == 422


async def test_phase_timing_and_phase_helpers(session, default_user):
    service = ChainService(session)
    chain = await _chain(session, default_user)
    step = _step(chain, is_draft=False)
    session.add(step)
    await session.commit()

    assert (
        await service.set_phase_timing(chain.id, "Execution", False, default_user) == 1
    )
    await session.refresh(step)
    assert step.delay_enabled is False

    other = User(
        user_id=str(uuid.uuid4()), email="other@example.com", hashed_password="x"
    )
    with pytest.raises(HTTPException) as exc:
        await service.set_phase_timing(chain.id, "Execution", True, other)
    assert await _status(exc) == 404

    assert len(await service.get_possible_phases()) == len(phases)
    assert await service.get_ukc_phase_description("Execution")
    with pytest.raises(HTTPException) as exc:
        await service.get_ukc_phase_description("Nowhere")
    assert await _status(exc) == 400


async def test_list_chains_handles_missing_phase_and_ownership(session, default_user):
    service = ChainService(session)
    with_phase = await _chain(session, default_user)
    without_phase = await _chain(session, default_user)
    phase_row = await session.scalar(
        select(CurrentAttackPhase).where(
            CurrentAttackPhase.chain_id == without_phase.id
        )
    )
    await session.delete(phase_row)
    await session.commit()

    listed = {
        item["chain_id"]: item for item in await service.list_chains(default_user)
    }
    assert listed[with_phase.id]["current_phase_name"] == "Reconnaissance"
    assert listed[without_phase.id]["current_phase_name"] == "Reconnaissance"

    other = User(
        user_id=str(uuid.uuid4()), email="other2@example.com", hashed_password="x"
    )
    with pytest.raises(HTTPException) as exc:
        await service.delete_chain(with_phase.id, other)
    assert await _status(exc) == 404

    await service.delete_chain(with_phase.id, default_user)
    assert await session.get(AttackChain, with_phase.id) is None
