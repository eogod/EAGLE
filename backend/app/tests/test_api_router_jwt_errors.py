"""
Module for test authorization jwt token scenarios
"""

from collections.abc import Iterator

import pytest
import pytest_asyncio
from fastapi import status
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import api_messages
from app.api.deps import AgentRunController, ChainController
from app.cmd.c2_tool import MythicClient
from app.cmd.mythic_events import MythicEventsService
from app.core.security.jwt import create_jwt_token
from app.main import app
from app.models import User

# only /api routes count, auth router itself is public
_SKIP_PREFIXES = (
    "/api/openapi.json",
    "/api/docs",
    "/api/redoc",
    "/api/auth",
)
_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _effective_routes(routes) -> Iterator[object]:
    """routes come back as _IncludedRouter in fastapi 0.162,
    so api_router.routes no longer yields APIRoute directly"""
    for route in routes:
        included = getattr(route, "effective_candidates", None)
        if included is not None:
            yield from _effective_routes(included())
        elif getattr(route, "methods", None) or getattr(route, "routes", None):
            yield route


def _protected_method_paths() -> list[tuple[str, str]]:
    """(method, path) of every auth-protected api route"""
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for route in _effective_routes(app.routes):
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if not path.startswith("/api/") or path.startswith(_SKIP_PREFIXES):
            continue
        for method in sorted(methods & _ALLOWED_METHODS):
            pair = (method, path)
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    return pairs


# walk must not miss everything, fails loud if fastapi changes shape again
assert _protected_method_paths(), "effective route walk found no api routes"


@pytest_asyncio.fixture(autouse=True, scope="module")
async def fixture_app_state_stubs():
    """no lifespan in tests, but 0.162 may solve state deps before auth
    and crash with 500 before the 401 we assert; stubs are safe no-ops"""
    state = app.state
    previous = dict(state._state)
    state.mythic_client = MythicClient()
    state.events_service = MythicEventsService()
    state.chain_controller = ChainController()
    state.agent_run_controller = AgentRunController()
    try:
        yield
    finally:
        state._state.clear()
        state._state.update(previous)


def _pair_id(pair: tuple[str, str]) -> str:
    return f"{pair[0]}-{pair[1]}"


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "api_method,api_path",
    _protected_method_paths(),
    ids=_pair_id,
)
async def test_api_routes_raise_401_on_jwt_decode_errors(
    client: AsyncClient,
    api_method: str,
    api_path: str,
) -> None:
    """test for responses on invalid jwt"""
    response = await client.request(
        method=api_method,
        url=api_path,
        headers={"Authorization": "Bearer garbage-invalid-jwt"},
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED, (
        api_method,
        api_path,
    )
    assert response.json() == {"detail": "Token invalid: Not enough segments"}


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "api_method,api_path",
    _protected_method_paths(),
    ids=_pair_id,
)
async def test_api_routes_raise_401_on_jwt_expired_token(
    client: AsyncClient,
    default_user: User,
    api_method: str,
    api_path: str,
) -> None:
    """test for responses on expired jwt"""
    with freeze_time("2023-01-01"):
        jwt = create_jwt_token(default_user.user_id)
    with freeze_time("2023-02-01"):
        response = await client.request(
            method=api_method,
            url=api_path,
            headers={"Authorization": f"Bearer {jwt.access_token}"},
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.json() == {"detail": "Token invalid: Signature has expired"}


@pytest.mark.asyncio(loop_scope="session")
async def test_api_routes_raise_401_on_jwt_user_deleted(
    client: AsyncClient,
    default_user_headers: dict[str, str],
    default_user: User,
    session: AsyncSession,
) -> None:
    """test for responses on invalid jwt cuz without user"""
    query = delete(User).where(User.user_id == default_user.user_id)
    await session.execute(query)
    await session.commit()

    for method, path in _protected_method_paths():
        response = await client.request(
            method=method,
            url=path,
            headers=default_user_headers,
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED, (method, path)
        assert response.json() == {"detail": api_messages.JWT_ERROR_USER_REMOVED}
