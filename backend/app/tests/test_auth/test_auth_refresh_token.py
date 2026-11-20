"""
Module for test jwt refresh token scenarios: rotation of a live token,
expiry, missing token and the cookie-less browser case
"""

import time

import pytest
from fastapi import status
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import api_messages
from app.core.config import get_settings
from app.models import RefreshToken, User
from app.tests.test_auth.token_tests_helper import (
    check_token_expire_time,
    refresh,
    validate_token_response,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_refresh_token_fails_with_message_when_token_does_not_exist(
    client: AsyncClient,
) -> None:
    response = await refresh(client, "blaxx")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json() == {"detail": api_messages.REFRESH_TOKEN_NOT_FOUND}


async def test_refresh_token_fails_without_body_and_cookie(
    client: AsyncClient,
) -> None:
    """a browser whose refresh cookie is gone gets 401, not a 422"""
    response = await refresh(client)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json() == {"detail": api_messages.REFRESH_TOKEN_NOT_FOUND}


async def test_refresh_token_fails_with_message_when_token_is_expired(
    client: AsyncClient,
    default_user: User,
    session: AsyncSession,
) -> None:
    session.add(
        RefreshToken(
            user_id=default_user.user_id,
            refresh_token="blaxx",
            exp=int(time.time()) - 1,
        )
    )
    await session.commit()

    response = await refresh(client, "blaxx")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == {"detail": api_messages.REFRESH_TOKEN_EXPIRED}


@freeze_time("2023-01-01")
async def test_refresh_token_rotates_into_a_valid_session(
    client: AsyncClient,
    default_user: User,
    session: AsyncSession,
) -> None:
    session.add(
        RefreshToken(
            user_id=default_user.user_id,
            refresh_token="blaxx",
            exp=int(time.time()) + 1000,
            used=False,
        )
    )
    await session.commit()

    response = await refresh(client, "blaxx")

    assert response.status_code == status.HTTP_200_OK
    token = response.json()
    assert token["token_type"] == "Bearer"
    assert check_token_expire_time(
        token,
        get_settings().security.jwt_access_token_expire_secs,
        "expires_at",
    )
    assert check_token_expire_time(
        token,
        get_settings().security.refresh_token_expire_secs,
        "refresh_token_expires_at",
    )
    assert validate_token_response(token, default_user.user_id)

    old_row = await session.scalar(
        select(RefreshToken).where(RefreshToken.refresh_token == "blaxx")
    )
    assert old_row is not None
    assert old_row.used
    new_row = await session.scalar(
        select(RefreshToken).where(RefreshToken.refresh_token == token["refresh_token"])
    )
    assert new_row is not None
    assert new_row.user_id == default_user.user_id
    assert not new_row.used
