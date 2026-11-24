"""HTTP surface: ownership, uploads, SSE run, drafts, cancellation."""

import json
import time
import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.api import deps
from app.api.deps import AgentRunController
from app.api.endpoints.agent_chat import get_agent_provider
from app.core.config import get_settings
from app.core.security.jwt import create_jwt_token
from app.main import app
from app.models import AuthSession, User
from app.services.llm_service import ToolTurn
from app.tests.test_agent.fixtures import FINDINGS, FakeProvider, make_draft

DOC_TEXT = (
    "Incident report: 2026-01-01 C2 beacon to 26.26 observed in powershell logs.\n"
)

DRAFT = make_draft([1])

RESEARCH = ToolTurn(content="beacon host uses smb signing [https://lab]", tool_calls=[])


def _fake(replies=(), *, streamed=None) -> FakeProvider:
    """each analysis run starts with the research stage: queue one canned turn"""
    return FakeProvider(replies, tool_replies=[RESEARCH], streamed=streamed)


def _sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
        if event:
            events.append((event, json.loads(data) if data else {}))

    return events


@pytest_asyncio.fixture(autouse=True)
def agent_state(tmp_path, monkeypatch):
    app.state.agent_run_controller = AgentRunController()
    monkeypatch.setattr(
        "app.services.agent_document_service.AGENT_WORKSPACE_DIR", tmp_path
    )
    yield
    app.dependency_overrides.pop(get_agent_provider, None)
    app.dependency_overrides.pop(deps.get_current_user, None)


async def _new_session(client: AsyncClient, headers) -> int:
    response = await client.post(
        "/api/agent/sessions", json={"title": "case"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _new_chained_session(client: AsyncClient, headers):
    chain_name = f"agent-{uuid.uuid4().hex[:8]}"
    chain = await client.post(
        "/api/cmd/new-chain", json={"chain_name": chain_name}, headers=headers
    )
    assert chain.status_code == 200, chain.text

    response = await client.post(
        "/api/agent/sessions",
        json={"title": "case", "chain_id": chain.json()["chain_id"]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], chain_name


async def _upload(client: AsyncClient, headers, session_id: int, name="case.txt"):
    return await client.post(
        f"/api/agent/sessions/{session_id}/documents",
        files={"file": (name, DOC_TEXT.encode(), "text/plain")},
        headers=headers,
    )


async def _chain_id(client: AsyncClient, headers, session_id: int) -> int:
    return (
        await client.get(f"/api/agent/sessions/{session_id}", headers=headers)
    ).json()["chain_id"]


async def _chain_steps(client: AsyncClient, headers, session_id: int) -> list[dict]:
    chain_id = await _chain_id(client, headers, session_id)
    chain = await client.get(f"/api/cmd/chain/{chain_id}", headers=headers)
    assert chain.status_code == 200, chain.text
    return chain.json()["steps"]


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


@pytest.mark.asyncio(loop_scope="session")
async def test_session_crud(client: AsyncClient, default_user_headers):
    session_id = await _new_session(client, default_user_headers)

    listed = await client.get("/api/agent/sessions", headers=default_user_headers)
    assert [item["id"] for item in listed.json()] == [session_id]

    detail = await client.get(
        f"/api/agent/sessions/{session_id}", headers=default_user_headers
    )
    assert detail.status_code == 200
    assert detail.json()["title"] == "case"
    assert detail.json()["messages"] == []
    assert detail.json()["draft"] is None

    patched = await client.patch(
        f"/api/agent/sessions/{session_id}",
        json={"title": "renamed"},
        headers=default_user_headers,
    )
    assert patched.json()["title"] == "renamed"

    deleted = await client.delete(
        f"/api/agent/sessions/{session_id}", headers=default_user_headers
    )
    assert deleted.status_code == 204

    missing = await client.get(
        f"/api/agent/sessions/{session_id}", headers=default_user_headers
    )
    assert missing.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_session_requires_ownership(
    client: AsyncClient, session, default_user_headers
):
    created = await client.post(
        "/api/agent/sessions", json={"title": "theirs"}, headers=default_user_headers
    )
    assert created.status_code == 201
    # a different user id in the token cannot see it
    session_id = created.json()["id"]
    other_headers = await _other_user_headers(session)

    assert (
        await client.get(f"/api/agent/sessions/{session_id}", headers=other_headers)
    ).status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_run_cancel_requires_ownership(
    client: AsyncClient, session, default_user_headers
):
    session_id = await _new_session(client, default_user_headers)
    document_id = (await _upload(client, default_user_headers, session_id)).json()["id"]
    app.dependency_overrides[get_agent_provider] = lambda: _fake(
        [json.dumps(FINDINGS), json.dumps(DRAFT)]
    )
    response = await client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "go", "document_id": document_id},
        headers=default_user_headers,
    )
    run_id = _sse_events(response.text)[0][1]["run_id"]
    other_headers = await _other_user_headers(session)

    cancelled = await client.post(
        f"/api/agent/runs/{run_id}/cancel", headers=other_headers
    )
    assert cancelled.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_draft_access_requires_ownership(
    client: AsyncClient, session, default_user_headers
):
    session_id, chain_name = await _new_chained_session(client, default_user_headers)
    document_id = (await _upload(client, default_user_headers, session_id)).json()["id"]
    app.dependency_overrides[get_agent_provider] = lambda: _fake(
        [json.dumps(FINDINGS), json.dumps(DRAFT)]
    )
    await client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "build a chain", "document_id": document_id},
        headers=default_user_headers,
    )
    detail = await client.get(
        f"/api/agent/sessions/{session_id}", headers=default_user_headers
    )
    draft_id = detail.json()["draft"]["id"]
    other_headers = await _other_user_headers(session)

    assert (
        await client.get(f"/api/agent/drafts/{draft_id}", headers=other_headers)
    ).status_code == 404
    imported = await client.post(
        f"/api/agent/drafts/{draft_id}/import",
        json={"chain_name": chain_name},
        headers=other_headers,
    )
    assert imported.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_parses_and_rejects(client: AsyncClient, default_user_headers):
    session_id = await _new_session(client, default_user_headers)

    ok = await _upload(client, default_user_headers, session_id)
    assert ok.status_code == 201, ok.text
    body = ok.json()
    assert body["parse_status"] == "parsed"
    assert body["char_count"] == len(DOC_TEXT)
    assert len(body["sha256"]) == 64

    unsupported = await client.post(
        f"/api/agent/sessions/{session_id}/documents",
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
        headers=default_user_headers,
    )
    assert unsupported.status_code == 422

    empty = await client.post(
        f"/api/agent/sessions/{session_id}/documents",
        files={"file": ("empty.txt", b"", "text/plain")},
        headers=default_user_headers,
    )
    assert empty.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
async def test_upload_size_cap(client: AsyncClient, default_user_headers, monkeypatch):
    monkeypatch.setattr("app.api.endpoints.agent_sessions.MAX_DOCUMENT_MB", 0)
    session_id = await _new_session(client, default_user_headers)
    response = await _upload(client, default_user_headers, session_id)
    assert response.status_code == 413


@pytest.mark.asyncio(loop_scope="session")
async def test_analysis_stream_and_draft_import(
    client: AsyncClient, default_user_headers
):
    session_id, chain_name = await _new_chained_session(client, default_user_headers)
    document_id = (await _upload(client, default_user_headers, session_id)).json()["id"]
    app.dependency_overrides[get_agent_provider] = lambda: _fake(
        [json.dumps(FINDINGS), json.dumps(DRAFT)]
    )

    response = await client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "build a chain", "document_id": document_id},
        headers=default_user_headers,
    )
    assert response.status_code == 200, response.text
    events = _sse_events(response.text)
    names = [name for name, _ in events]
    assert names[0] == "run.started"
    assert "finding.created" in names
    assert names[-1] == "run.completed"
    assert "event: draft.ready" in response.text

    detail = (
        await client.get(
            f"/api/agent/sessions/{session_id}", headers=default_user_headers
        )
    ).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assistant = detail["messages"][1]["content"]
    assert any(part["type"] == "text" for part in assistant)
    assert any(part["type"] == "tool-call" for part in assistant)
    assert all(
        part.get("argsText") for part in assistant if part["type"] == "tool-call"
    )
    assert detail["draft"]["status"] == "plan"

    draft_id = detail["draft"]["id"]
    imported = await client.post(
        f"/api/agent/drafts/{draft_id}/import",
        json={"chain_name": chain_name},
        headers=default_user_headers,
    )
    assert imported.status_code == 200, imported.text
    step = imported.json()["steps"][0]
    assert step["phase"] == "Command & Control"
    assert step["chain_name"] == chain_name
    assert step["status"] == "planned"
    assert step["id"] > 0

    # imported steps are real rows: they survive a reload and are exportable
    rows = await _chain_steps(client, default_user_headers, session_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "planned"
    assert rows[0]["command"] == "echo beacon"

    again = await client.post(
        f"/api/agent/drafts/{draft_id}/import",
        json={"chain_name": chain_name},
        headers=default_user_headers,
    )
    assert again.status_code == 409


@pytest.mark.asyncio(loop_scope="session")
async def test_brief_mode_drafts_without_document(
    client: AsyncClient, default_user_headers
):
    session_id = await _new_session(client, default_user_headers)
    app.dependency_overrides[get_agent_provider] = lambda: _fake([json.dumps(DRAFT)])
    response = await client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "phish a user then beacon out", "mode": "plan"},
        headers=default_user_headers,
    )
    assert response.status_code == 200, response.text
    events = _sse_events(response.text)
    names = [name for name, _ in events]
    assert "draft.ready" in names
    assert names[-1] == "run.completed"
    # no document means no evidence, so the validator strips the finding ref
    draft = next(data["draft"] for name, data in events if name == "draft.ready")
    assert draft["validation"]["warnings"]


@pytest.mark.asyncio(loop_scope="session")
async def test_ask_mode_streams_answer_without_draft(
    client: AsyncClient, default_user_headers
):
    session_id, _chain_name = await _new_chained_session(client, default_user_headers)
    provider = _fake(streamed=["command and control ", "was observed."])
    app.dependency_overrides[get_agent_provider] = lambda: provider
    response = await client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "what stage is the beacon?", "mode": "ask"},
        headers=default_user_headers,
    )
    assert response.status_code == 200, response.text
    events = _sse_events(response.text)
    names = [name for name, _ in events]
    assert "draft.ready" not in names
    assert names[-2] == "assistant.delta"
    detail = (
        await client.get(
            f"/api/agent/sessions/{session_id}", headers=default_user_headers
        )
    ).json()
    assert detail["draft"] is None

    text = "".join(
        part.get("text", "")
        for part in detail["messages"][-1]["content"]
        if part["type"] == "text"
    )
    assert "command and control" in text
    # ask mode sees the chain bound to the session
    messages, _ = provider.calls[0]
    prompt = " ".join(message["content"] for message in messages)
    assert "Current phase:" in prompt


@pytest.mark.asyncio(loop_scope="session")
async def test_plan_mode_imports_as_planned_and_is_replaced(
    client: AsyncClient, default_user_headers
):
    session_id, chain_name = await _new_chained_session(client, default_user_headers)
    document_id = (await _upload(client, default_user_headers, session_id)).json()["id"]
    plan = {"steps": [dict(DRAFT["steps"][0], command="")]}
    app.dependency_overrides[get_agent_provider] = lambda: _fake(
        [json.dumps(FINDINGS), json.dumps(plan)]
    )
    response = await client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "plan it", "document_id": document_id, "mode": "plan"},
        headers=default_user_headers,
    )
    assert response.status_code == 200, response.text
    draft = next(
        data["draft"]
        for name, data in _sse_events(response.text)
        if name == "draft.ready"
    )
    assert draft["status"] == "plan"
    assert draft["steps"][0]["command"] == ""

    imported = await client.post(
        f"/api/agent/drafts/{draft['draft_id']}/import",
        json={"chain_name": chain_name},
        headers=default_user_headers,
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["steps"][0]["status"] == "planned"
    assert imported.json()["replaced"] == 0

    again = await client.post(
        f"/api/agent/drafts/{draft['draft_id']}/import",
        json={"chain_name": chain_name},
        headers=default_user_headers,
    )
    assert again.status_code == 409

    # a build proposal replaces the unexecuted plan instead of stacking on it
    app.dependency_overrides[get_agent_provider] = lambda: _fake(
        [json.dumps(FINDINGS), json.dumps(DRAFT)]
    )
    build = await client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "plan it again", "document_id": document_id, "mode": "plan"},
        headers=default_user_headers,
    )
    build_draft = next(
        data["draft"] for name, data in _sse_events(build.text) if name == "draft.ready"
    )
    built = await client.post(
        f"/api/agent/drafts/{build_draft['draft_id']}/import",
        json={"chain_name": chain_name},
        headers=default_user_headers,
    )
    assert built.status_code == 200, built.text
    assert built.json()["replaced"] == 1
    assert built.json()["steps"][0]["status"] == "planned"

    rows = await _chain_steps(client, default_user_headers, session_id)
    assert [row["status"] for row in rows] == ["planned"]


@pytest.mark.asyncio(loop_scope="session")
async def test_edit_mode_proposes_then_applies_only_after_approval(
    client: AsyncClient, default_user_headers
):
    session_id, chain_name = await _new_chained_session(client, default_user_headers)
    edits = {
        "operations": [
            {
                "op": "add_step",
                "phase": "Execution",
                "step_type": "bash",
                "tool_name": "shell",
                "command": "echo edited-by-agent",
            },
            {"op": "set_phase", "phase": "Execution"},
        ],
        "summary": "added one step",
    }
    app.dependency_overrides[get_agent_provider] = lambda: _fake([json.dumps(edits)])
    response = await client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "add a step and move to execution", "mode": "edit"},
        headers=default_user_headers,
    )
    assert response.status_code == 200, response.text
    events = _sse_events(response.text)
    proposed = next(data for name, data in events if name == "edit.proposed")
    assert proposed["status"] == "proposed"

    # nothing was applied by the chat itself
    rows = await _chain_steps(client, default_user_headers, session_id)
    assert rows == []

    proposal_id = proposed["public_id"]
    approved = await client.post(
        f"/api/agent/sessions/{session_id}/proposals/{proposal_id}/approve",
        json={"acknowledge_dangerous": False},
        headers=default_user_headers,
    )
    assert approved.status_code == 200, approved.text
    applied = await client.post(
        f"/api/agent/sessions/{session_id}/proposals/{proposal_id}/apply",
        json={"acknowledge_dangerous": False},
        headers=default_user_headers,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"

    rows = await _chain_steps(client, default_user_headers, session_id)
    assert [(row["status"], row["command"]) for row in rows] == [
        ("pending", "echo edited-by-agent")
    ]

    chain_id = await _chain_id(client, default_user_headers, session_id)
    phase_response = await client.get(
        f"/api/cmd/chain-phase/{chain_id}", headers=default_user_headers
    )
    assert phase_response.status_code == 200, phase_response.text
    assert phase_response.json()["current_phase_name"] == "Execution"

    # run_step needs its own approval, the edit response cannot smuggle it in
    run_ops = {"operations": [{"op": "run_step", "step_id": rows[0]["id"]}]}
    app.dependency_overrides[get_agent_provider] = lambda: _fake([json.dumps(run_ops)])
    run = await client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "run it", "mode": "edit"},
        headers=default_user_headers,
    )
    invalid = next(
        data for name, data in _sse_events(run.text) if name == "edit.proposed"
    )
    assert invalid["status"] == "invalid"
    assert any("run_step" in error for error in invalid["validation"]["errors"])


@pytest.mark.asyncio(loop_scope="session")
async def test_providers_endpoint_lists_sources(client: AsyncClient, monkeypatch):
    async def fake_list_models(_cfg):
        return {
            "ollama": {
                "available": True,
                "models": ["llama3"],
                "default": "llama3",
                "error": None,
            },
            "openrouter": {
                "available": False,
                "models": [],
                "default": "",
                "error": "OpenRouter 403 (host blocked by provider policy)",
            },
            "g4f": {
                "available": True,
                "models": ["default"],
                "default": "default",
                "error": None,
            },
        }

    monkeypatch.setattr("app.services.agent_chat_service.list_models", fake_list_models)
    # this test only checks the payload shape; keep the DB out of it
    app.dependency_overrides[deps.get_current_user] = lambda: User(
        user_id="00000000-0000-0000-0000-000000000002",
        email="providers@example.com",
        hashed_password="x",
    )

    response = await client.get("/api/agent/providers")
    assert response.status_code == 200
    body = response.json()
    by_id = {item["id"]: item for item in body["providers"]}
    assert set(by_id) == {"ollama", "openrouter", "g4f"}
    assert by_id["g4f"]["available"] is True
    assert by_id["g4f"]["models"]
    assert by_id["g4f"]["default"] in by_id["g4f"]["models"]
    assert body["default"]["mode"] == "plan"


@pytest.mark.asyncio(loop_scope="session")
async def test_document_must_belong_to_session(
    client: AsyncClient, default_user_headers
):
    first = await _new_session(client, default_user_headers)
    second = await _new_session(client, default_user_headers)
    document_id = (await _upload(client, default_user_headers, second)).json()["id"]
    response = await client.post(
        f"/api/agent/sessions/{first}/messages",
        json={"content": "go", "document_id": document_id},
        headers=default_user_headers,
    )
    assert response.status_code == 400


@pytest.mark.asyncio(loop_scope="session")
async def test_unconfigured_provider_returns_503(
    client: AsyncClient, default_user_headers, monkeypatch
):
    # .env may hold a real key; force the request to run without one
    monkeypatch.setattr(get_settings().llmservice, "openrouter_api_key", None)
    session_id = await _new_session(client, default_user_headers)
    document_id = (await _upload(client, default_user_headers, session_id)).json()["id"]
    response = await client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={
            "content": "go",
            "document_id": document_id,
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
        },
        headers=default_user_headers,
    )
    assert response.status_code == 503


@pytest.mark.asyncio(loop_scope="session")
async def test_cancel_completed_run_is_idempotent(
    client: AsyncClient, default_user_headers
):
    session_id = await _new_session(client, default_user_headers)
    document_id = (await _upload(client, default_user_headers, session_id)).json()["id"]
    app.dependency_overrides[get_agent_provider] = lambda: _fake(
        [json.dumps(FINDINGS), json.dumps(DRAFT)]
    )
    response = await client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "go", "document_id": document_id},
        headers=default_user_headers,
    )
    run_id = _sse_events(response.text)[0][1]["run_id"]

    cancelled = await client.post(
        f"/api/agent/runs/{run_id}/cancel", headers=default_user_headers
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelling"
    assert (
        await client.post("/api/agent/runs/999999/cancel", headers=default_user_headers)
    ).status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_import_missing_draft_is_404(client: AsyncClient, default_user_headers):
    response = await client.post(
        "/api/agent/drafts/999999/import",
        json={"chain_name": "x"},
        headers=default_user_headers,
    )
    assert response.status_code == 404
    assert (
        await client.get("/api/agent/drafts/999999", headers=default_user_headers)
    ).status_code == 404
