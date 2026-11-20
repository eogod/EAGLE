"""
Module for cookie (HttpOnly + SameSite) auth flows on top of the same JWT:
login sets both cookies, bearer-less access works, replay of a rotated token
kills the family, logout revokes + clears
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession as SqlAsyncSession

from app.api import api_messages
from app.api.deps import ACCESS_COOKIE, REFRESH_COOKIE, REFRESH_COOKIE_PATH
from app.core import database_session
from app.core.config import get_settings
from app.core.database_session import new_async_engine
from app.core.security.password import get_password_hash
from app.main import app
from app.models import RefreshToken, User
from app.tests.conftest import default_user_email, default_user_password
from app.tests.test_auth.token_tests_helper import login, refresh

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_login_sets_httponly_session_cookies(
    client: AsyncClient,
    default_user: User,
) -> None:
    response = await login(client, default_user.email, default_user_password)

    assert response.status_code == status.HTTP_200_OK
    assert response.cookies.get(ACCESS_COOKIE)
    refresh_cookie = response.cookies.get(REFRESH_COOKIE)
    assert refresh_cookie
    # refresh cookie must not ride ordinary api calls
    assert any(c.path == REFRESH_COOKIE_PATH for c in response.cookies.jar)


async def test_cookie_only_access_works_without_bearer_header(
    client: AsyncClient,
    default_user: User,
) -> None:
    await login(client, default_user.email, default_user_password)

    response = await client.get(app.url_path_for("read_current_user"))
    # no Authorization header was set anywhere, HttpOnly cookie did it
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["email"] == default_user_email


async def test_refresh_token_via_cookie_without_body(
    client: AsyncClient,
    default_user: User,
    session: AsyncSession,
) -> None:
    cookie_token = (
        await login(client, default_user.email, default_user_password)
    ).cookies.get(REFRESH_COOKIE)

    response = await refresh(client)
    assert response.status_code == status.HTTP_200_OK
    # old token is rotated away (used), fresh one went back into the jar
    row = await session.scalar(
        select(RefreshToken).where(RefreshToken.refresh_token == cookie_token)
    )
    assert row is not None and row.used is True


async def test_reused_refresh_token_revokes_whole_family(
    client: AsyncClient,
    default_user: User,
    session: AsyncSession,
) -> None:
    stolen = (
        await login(client, default_user.email, default_user_password)
    ).cookies.get(REFRESH_COOKIE)
    await refresh(client, stolen)  # legit rotation, stolen is dead now
    # a new login gives the user a second session with its own family row
    await login(client, default_user.email, default_user_password)

    replay = await refresh(client, stolen)
    # replayed rotated token -> 401 + every refresh token of the user dies
    assert replay.status_code == status.HTTP_401_UNAUTHORIZED
    assert replay.json() == {"detail": api_messages.REFRESH_TOKEN_ALREADY_USED}
    count = await session.scalar(
        select(func.count())
        .select_from(RefreshToken)
        .where(RefreshToken.user_id == default_user.user_id)
    )
    assert count == 0


async def test_concurrent_refreshes_with_same_token_allow_exactly_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """real parallel run: the conftest session is shared between requests and
    would serialize them with IllegalStateChangeError, and its rollback
    transaction hides uncommitted rows from any other connection, so this
    test builds its own engine, own user and per-request sessions"""
    if database_session._ASYNC_ENGINE.dialect.name != "postgresql":
        # FOR UPDATE SKIP LOCKED is a postgres guarantee; sqlite serializes writes
        pytest.skip("concurrent refresh needs postgres row locking")

    engine = new_async_engine(get_settings().sqlalchemy_database_uri)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    email = "concurrent@test.local"

    async with maker() as s:
        s.add(
            User(
                email=email,
                username="concurrent",
                role="user",
                hashed_password=get_password_hash("geralt"),
            )
        )
        await s.commit()

    @asynccontextmanager
    async def own_session() -> AsyncGenerator[SqlAsyncSession]:
        session = maker()
        yield session
        await session.close()

    monkeypatch.setattr(database_session, "get_async_session", own_session)
    codes: list[int] = []
    try:
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        async with client as own_client:
            login_response = await login(own_client, email, default_user_password)
            if login_response.status_code != status.HTTP_200_OK:
                codes = [login_response.status_code, login_response.status_code]
            else:
                token = login_response.cookies.get(REFRESH_COOKIE)
                first, second = await asyncio.gather(
                    refresh(own_client, token),
                    refresh(own_client, token),
                )
                codes = [first.status_code, second.status_code]
    finally:
        await engine.dispose()

    # FOR UPDATE SKIP LOCKED: one rotates, the other must not double-rotate
    codes = sorted(codes)
    assert codes.count(status.HTTP_200_OK) == 1
    assert codes[0] in (
        status.HTTP_200_OK,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND,
    )


async def test_logout_revokes_refresh_and_clears_cookies(
    client: AsyncClient,
    default_user: User,
    session: AsyncSession,
) -> None:
    await login(client, default_user.email, default_user_password)

    response = await client.post(app.url_path_for("logout"))
    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert response.cookies.get(ACCESS_COOKIE) in (None, "")
    count = await session.scalar(
        select(func.count())
        .select_from(RefreshToken)
        .where(RefreshToken.user_id == default_user.user_id)
    )
    assert count == 0
    # session is really gone
    after = await client.get(app.url_path_for("read_current_user"))
    assert after.status_code == status.HTTP_401_UNAUTHORIZED
