"""operator drafts stay on the draft routes, real steps on the step routes"""

import time
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.security.jwt import create_jwt_token
from app.models import (
    AgentSession,
    AttackStep,
    AuthSession,
    ChainDraft,
    User,
)
from app.services.chain_service import ChainService

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _step_row(
    chain_id: int,
    *,
    command: str,
    is_draft: bool,
    status: str = "pending",
) -> AttackStep:
    return AttackStep(
        chain_id=chain_id,
        phase="Execution",
        step_type="bash",
        tool_name="shell",
        command=command,
        mythic_task_id=0,
        mythic_payload_id=0,
        mythic_payload_uuid=str(uuid.uuid4()),
        raw_log="",
        status=status,
        is_draft=is_draft,
    )


async def _new_chain(client: AsyncClient, headers) -> tuple[int, str]:
    chain_name = f"drafts-{uuid.uuid4().hex[:8]}"
    response = await client.post(
        "/api/cmd/new-chain", json={"chain_name": chain_name}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()["chain_id"], chain_name


async def _create_draft(
    client: AsyncClient, headers, chain_id: int, command: str = "echo draft"
) -> dict:
    response = await client.post(
        f"/api/cmd/chain/{chain_id}/draft-steps",
        json={
            "phase": "Execution",
            "step_type": "bash",
            "tool_name": "shell",
            "command": command,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _chain_steps(client: AsyncClient, headers, chain_id: int) -> list[dict]:
    response = await client.get(f"/api/cmd/chain/{chain_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["steps"]


async def _other_user_headers(session) -> dict:
    other = User(
        user_id=str(uuid.uuid4()),
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
    )
    auth_session = AuthSession(
        user_id=other.user_id,
        expires_at=int(time.time()) + 3600,
        created_at=int(time.time()),
    )
    session.add(other)
    session.add(auth_session)
    await session.commit()
    await session.refresh(auth_session)

    token = create_jwt_token(other.user_id, auth_session.id).access_token
    return {"Authorization": f"Bearer {token}"}


async def test_create_draft_step_is_visible_on_chain(
    client: AsyncClient, default_user_headers
):
    chain_id, _ = await _new_chain(client, default_user_headers)

    body = await _create_draft(client, default_user_headers, chain_id, "echo draft")
    assert body["id"] > 0
    assert body["is_draft"] is True
    assert body["status"] == "pending"
    assert body["command"] == "echo draft"

    rows = await _chain_steps(client, default_user_headers, chain_id)
    assert [row["id"] for row in rows] == [body["id"]]
    assert rows[0]["is_draft"] is True


async def test_patch_and_delete_draft_only_touch_that_draft(
    client: AsyncClient, default_user_headers
):
    chain_id, _ = await _new_chain(client, default_user_headers)
    first = await _create_draft(client, default_user_headers, chain_id, "echo first")
    second = await _create_draft(client, default_user_headers, chain_id, "echo second")

    patched = await client.patch(
        f"/api/cmd/chain/{chain_id}/draft-steps/{first['id']}",
        json={"command": "echo patched"},
        headers=default_user_headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["command"] == "echo patched"

    rows = await _chain_steps(client, default_user_headers, chain_id)
    by_id = {row["id"]: row for row in rows}
    assert by_id[second["id"]]["command"] == "echo second"

    deleted = await client.delete(
        f"/api/cmd/chain/{chain_id}/draft-steps/{second['id']}",
        headers=default_user_headers,
    )
    assert deleted.status_code == 200, deleted.text

    rows = await _chain_steps(client, default_user_headers, chain_id)
    assert [row["id"] for row in rows] == [first["id"]]


async def test_draft_and_real_step_routes_do_not_cross(
    client: AsyncClient, session, default_user_headers
):
    chain_id, _ = await _new_chain(client, default_user_headers)
    real = _step_row(chain_id, command="echo real", is_draft=False, status="planned")
    session.add(real)
    await session.commit()
    await session.refresh(real)

    # a real step is invisible to every draft route
    assert (
        await client.patch(
            f"/api/cmd/chain/{chain_id}/draft-steps/{real.id}",
            json={"command": "hijacked"},
            headers=default_user_headers,
        )
    ).status_code == 404
    assert (
        await client.delete(
            f"/api/cmd/chain/{chain_id}/draft-steps/{real.id}",
            headers=default_user_headers,
        )
    ).status_code == 404

    draft = await _create_draft(client, default_user_headers, chain_id, "echo draft")

    # and a draft is invisible to the normal step routes
    assert (
        await client.patch(
            f"/api/cmd/step/{draft['id']}",
            json={"command": "hijacked"},
            headers=default_user_headers,
        )
    ).status_code == 404
    assert (
        await client.delete(
            f"/api/cmd/step/{draft['id']}", headers=default_user_headers
        )
    ).status_code == 404

    rows = await _chain_steps(client, default_user_headers, chain_id)
    commands = {row["id"]: row["command"] for row in rows}
    assert commands[real.id] == "echo real"
    assert commands[draft["id"]] == "echo draft"


async def test_draft_routes_require_chain_ownership(
    client: AsyncClient, session, default_user_headers
):
    chain_id, _ = await _new_chain(client, default_user_headers)
    draft = await _create_draft(client, default_user_headers, chain_id)
    other_headers = await _other_user_headers(session)

    created = await client.post(
        f"/api/cmd/chain/{chain_id}/draft-steps",
        json={
            "phase": "Execution",
            "step_type": "bash",
            "tool_name": "shell",
            "command": "echo theirs",
        },
        headers=other_headers,
    )
    assert created.status_code == 404

    patched = await client.patch(
        f"/api/cmd/chain/{chain_id}/draft-steps/{draft['id']}",
        json={"command": "hijacked"},
        headers=other_headers,
    )
    assert patched.status_code == 404

    deleted = await client.delete(
        f"/api/cmd/chain/{chain_id}/draft-steps/{draft['id']}",
        headers=other_headers,
    )
    assert deleted.status_code == 404

    rows = await _chain_steps(client, default_user_headers, chain_id)
    assert [row["command"] for row in rows] == ["echo draft"]


async def test_promote_drafts_only_promotes_filled_steps(
    client: AsyncClient, session, default_user_headers
):
    chain_id, _ = await _new_chain(client, default_user_headers)
    filled = _step_row(chain_id, command="echo filled", is_draft=True)
    empty = _step_row(chain_id, command="", is_draft=True)
    session.add_all([filled, empty])
    await session.commit()

    promoted = await ChainService(session).promote_drafts(chain_id)
    assert promoted == 1

    rows = (
        await session.scalars(select(AttackStep).where(AttackStep.chain_id == chain_id))
    ).all()
    flags = {row.command: row.is_draft for row in rows}
    assert flags == {"echo filled": False, "": True}


async def test_execution_skips_drafts_while_editor_sees_them(
    client: AsyncClient, session, default_user_headers
):
    chain_id, _ = await _new_chain(client, default_user_headers)
    real = _step_row(chain_id, command="echo real", is_draft=False, status="planned")
    draft = _step_row(chain_id, command="echo draft", is_draft=True)
    session.add_all([real, draft])
    await session.commit()

    service = ChainService(session)
    execution = await service.steps_for_execution(chain_id)
    editor = await service.steps_for_editor(chain_id)

    assert [row.command for row in execution] == ["echo real"]
    assert [row.command for row in editor] == ["echo real", "echo draft"]


async def test_agent_draft_import_keeps_operator_drafts(
    client: AsyncClient, session, default_user, default_user_headers
):
    chain_id, chain_name = await _new_chain(client, default_user_headers)
    operator_draft = _step_row(chain_id, command="echo hand-written", is_draft=True)
    session.add(operator_draft)

    agent_session = AgentSession(
        user_id=default_user.user_id,
        chain_id=chain_id,
        title="case",
    )
    session.add(agent_session)
    await session.flush()
    draft = ChainDraft(
        session_id=agent_session.id,
        user_id=default_user.user_id,
        name="plan",
        status="needs_review",
        payload={
            "steps": [
                {
                    "phase": "Execution",
                    "step_type": "bash",
                    "tool_name": "shell",
                    "command": "echo imported",
                }
            ]
        },
    )
    session.add(draft)
    await session.commit()
    await session.refresh(draft)

    imported = await client.post(
        f"/api/agent/drafts/{draft.id}/import",
        json={"chain_name": chain_name},
        headers=default_user_headers,
    )
    assert imported.status_code == 200, imported.text

    rows = (
        await session.scalars(select(AttackStep).where(AttackStep.chain_id == chain_id))
    ).all()
    by_command = {row.command: row.is_draft for row in rows}
    assert by_command == {"echo hand-written": True, "echo imported": False}
