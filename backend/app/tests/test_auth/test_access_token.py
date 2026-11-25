"""
Module for test jwt access token scenarios, their correct http response
"""

import pytest
from fastapi import status
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import api_messages
from app.core.config import get_settings
from app.models import RefreshToken, User
from app.services.auth_sessions import hash_refresh_token
from app.tests.conftest import default_user_password
from app.tests.test_auth.token_tests_helper import (
    check_token_expire_time,
    login,
    validate_token_response,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


@freeze_time("2023-01-01", real_asyncio=True)
async def test_login_returns_a_valid_session(
    client: AsyncClient,
    default_user: User,
    session: AsyncSession,
) -> None:
    response = await login(client, default_user.email, default_user_password)

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

    refresh_row = await session.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_refresh_token(token["refresh_token"])
        )
    )
    assert refresh_row is not None
    assert refresh_row.user_id == default_user.user_id
    assert refresh_row.exp == token["refresh_token_expires_at"]
    assert refresh_row.used_at is None


async def test_auth_access_token_fail_for_not_existing_user_with_message(
    client: AsyncClient,
) -> None:
    response = await login(client, "non-existing", "bla")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == {"detail": api_messages.PASSWORD_INVALID}


async def test_auth_access_token_fail_for_invalid_password_with_message(
    client: AsyncClient,
    default_user: User,
) -> None:
    response = await login(client, default_user.email, "invalid")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == {"detail": api_messages.PASSWORD_INVALID}
