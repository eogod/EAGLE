"""
Module for test registration scenarios: closed by default, and the admin-only
creation path once SECURITY__REGISTRATION_ENABLED is on
"""

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import api_messages
from app.core.config import get_settings
from app.core.security.password import verify_password
from app.main import app
from app.models import User

pytestmark = pytest.mark.asyncio(loop_scope="session")

NEW_USER = {
    "email": "someuser@example.com",
    "password": "somepassword1",
    "role": "user",
    "username": "someuser",
}


async def test_register_new_user_is_forbidden_by_default(
    client: AsyncClient,
    default_user_headers: dict[str, str],
) -> None:
    """registration is disabled: open self-registration plus a bash tool
    on the same box would be anyone-signs-up-owns-the-machine"""
    # the shipped default, not a monkeypatched one: if someone flips it on,
    # this test and the docstring above stop being true
    assert get_settings().security.registration_enabled is False

    response = await client.post(
        app.url_path_for("register_new_user"),
        json=NEW_USER,
        headers=default_user_headers,
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_register_new_user_without_auth_token(
    client: AsyncClient,
) -> None:
    """even worse for anonymous, auth is checked before the gate"""
    response = await client.post(
        app.url_path_for("register_new_user"),
        json=NEW_USER,
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


async def test_register_new_user_creates_the_account(
    client: AsyncClient,
    default_user_headers: dict[str, str],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings().security, "registration_enabled", True)

    response = await client.post(
        app.url_path_for("register_new_user"),
        json=NEW_USER,
        headers=default_user_headers,
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["email"] == NEW_USER["email"]
    created = await session.scalar(select(User).where(User.email == NEW_USER["email"]))
    assert created is not None
    assert created.role == "user"
    assert verify_password(NEW_USER["password"], created.hashed_password)


async def test_register_new_user_rejects_duplicate_email(
    client: AsyncClient,
    default_user: User,
    default_user_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings().security, "registration_enabled", True)

    response = await client.post(
        app.url_path_for("register_new_user"),
        json={**NEW_USER, "email": default_user.email},
        headers=default_user_headers,
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == {"detail": api_messages.EMAIL_ADDRESS_ALREADY_USED}
