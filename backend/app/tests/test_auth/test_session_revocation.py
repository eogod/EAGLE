"""access-token death when its server-side session dies"""

import time

import jwt as pyjwt
import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import api_messages
from app.api.deps import REFRESH_COOKIE
from app.core.config import get_settings
from app.core.security.jwt import JWT_ALGORITHM, create_jwt_token
from app.main import app
from app.models import AuthSession, RefreshToken, User
from app.services.auth_sessions import hash_refresh_token
from app.tests.conftest import default_user_password
from app.tests.test_auth.token_tests_helper import login, refresh

pytestmark = pytest.mark.asyncio(loop_scope="session")

NEW_PASSWORD = "geralt-after-reset"


def _me(client: AsyncClient, access_token: str):
    return client.get(
        app.url_path_for("read_current_user"),
        headers={"Authorization": f"Bearer {access_token}"},
    )


async def _session_id_of(session: AsyncSession, raw_refresh: str) -> int:
    session_id = await session.scalar(
        select(RefreshToken.session_id).where(
            RefreshToken.token_hash == hash_refresh_token(raw_refresh)
        )
    )
    assert session_id is not None
    return session_id


async def test_logout_kills_the_access_token_issued_by_that_session(
    client: AsyncClient,
    default_user: User,
) -> None:
    tokens = (await login(client, default_user.email, default_user_password)).json()
    assert (await _me(client, tokens["access_token"])).status_code == status.HTTP_200_OK

    logout = await client.post(app.url_path_for("logout"))

    assert logout.status_code == status.HTTP_204_NO_CONTENT
    # the JWT itself is still valid and unexpired, only its session is gone
    after = await _me(client, tokens["access_token"])
    assert after.status_code == status.HTTP_401_UNAUTHORIZED
    assert after.json() == {"detail": api_messages.JWT_ERROR_USER_REMOVED}


async def test_reset_password_kills_old_access_and_new_login_works(
    client: AsyncClient,
    default_user: User,
) -> None:
    tokens = (await login(client, default_user.email, default_user_password)).json()
    old_access = tokens["access_token"]

    reset = await client.post(
        app.url_path_for("reset_current_user_password"),
        json={"password": NEW_PASSWORD},
        headers={"Authorization": f"Bearer {old_access}"},
    )

    assert reset.status_code in (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT)
    assert (await _me(client, old_access)).status_code == status.HTTP_401_UNAUTHORIZED

    # the old password is dead too, the new one gets a fresh session
    assert (
        await login(client, default_user.email, default_user_password)
    ).status_code == status.HTTP_400_BAD_REQUEST
    relogin = await login(client, default_user.email, NEW_PASSWORD)
    assert relogin.status_code == status.HTTP_200_OK
    new_access = relogin.json()["access_token"]
    assert (await _me(client, new_access)).status_code == status.HTTP_200_OK


async def test_access_token_without_sid_gets_401_not_500(
    client: AsyncClient,
    default_user: User,
) -> None:
    now = int(time.time())
    legacy = pyjwt.encode(
        {
            "iss": get_settings().security.jwt_issuer,
            "sub": default_user.user_id,
            "iat": now,
            "exp": now + 60,
        },
        key=get_settings().security.jwt_secret_key.get_secret_value(),
        algorithm=JWT_ALGORITHM,
    )

    response = await _me(client, legacy)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


async def test_access_token_with_unknown_or_revoked_session_is_rejected(
    client: AsyncClient,
    default_user: User,
    session: AsyncSession,
) -> None:
    unknown = create_jwt_token(default_user.user_id, session_id=999999)
    response = await _me(client, unknown.access_token)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED

    revoked = AuthSession(
        user_id=default_user.user_id,
        expires_at=int(time.time()) + 3600,
        created_at=int(time.time()),
        revoked_at=int(time.time()),
    )
    session.add(revoked)
    await session.commit()
    await session.refresh(revoked)

    response = await _me(
        client,
        create_jwt_token(default_user.user_id, session_id=revoked.id).access_token,
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


async def _assert_live_tokens(
    session: AsyncSession, session_id: int, expected: int
) -> None:
    """only an unused token can refresh; used rows are just history"""
    count = await session.scalar(
        select(func.count())
        .select_from(RefreshToken)
        .where(
            RefreshToken.session_id == session_id,
            RefreshToken.used_at.is_(None),
        )
    )
    assert count == expected


async def test_refresh_replay_inside_grace_keeps_session_and_after_grace_kills_only_it(
    client: AsyncClient,
    default_user: User,
    session: AsyncSession,
) -> None:
    stolen = (
        await login(client, default_user.email, default_user_password)
    ).cookies.get(REFRESH_COOKIE)
    victim_session_id = await _session_id_of(session, stolen)
    assert (await refresh(client, stolen)).status_code == status.HTTP_200_OK

    # inside the grace window two tabs race: 409, nothing is revoked
    in_grace = await refresh(client, stolen)
    assert in_grace.status_code == status.HTTP_409_CONFLICT
    assert in_grace.json() == {"detail": api_messages.REFRESH_TOKEN_ALREADY_USED}
    victim = await session.get(AuthSession, victim_session_id)
    assert victim is not None and victim.revoked_at is None
    await _assert_live_tokens(session, victim_session_id, 1)

    # another device gets its own session before the replay is detected
    other_session_id = await _session_id_of(
        session,
        (await login(client, default_user.email, default_user_password)).json()[
            "refresh_token"
        ],
    )
    assert other_session_id != victim_session_id

    rotated = await session.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_refresh_token(stolen)
        )
    )
    assert rotated is not None and rotated.used_at is not None
    rotated.used_at -= get_settings().security.refresh_rotation_grace_secs + 1
    await session.commit()

    # replay after grace is theft: 401, only this session burned
    after_grace = await refresh(client, stolen)
    assert after_grace.status_code == status.HTTP_401_UNAUTHORIZED
    assert after_grace.json() == {"detail": api_messages.REFRESH_TOKEN_ALREADY_USED}
    victim = await session.get(AuthSession, victim_session_id)
    assert victim is not None and victim.revoked_at is not None
    await _assert_live_tokens(session, victim_session_id, 0)
    other = await session.get(AuthSession, other_session_id)
    assert other is not None and other.revoked_at is None
    await _assert_live_tokens(session, other_session_id, 1)
