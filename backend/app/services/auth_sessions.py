"""server-side auth sessions used by request deps and stream guards"""

from __future__ import annotations

import hashlib
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import AuthSession, User


def hash_refresh_token(raw: str) -> str:
    """only the digest is stored, a database dump cannot mint a session"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def session_expiry() -> int:
    return int(time.time() + get_settings().security.refresh_token_expire_secs)


async def active_session(
    session: AsyncSession,
    session_id: int,
    user_id: str,
    *,
    now: int | None = None,
) -> AuthSession | None:
    current = int(time.time()) if now is None else now
    return await session.scalar(
        select(AuthSession)
        .join(User, AuthSession.user_id == User.user_id)
        .where(
            AuthSession.id == session_id,
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > current,
            User.is_active.is_(True),
        )
    )


async def revoke_session(session: AsyncSession, auth_session: AuthSession) -> None:
    # logging out twice must keep the first revocation time
    if auth_session.revoked_at is None:
        auth_session.revoked_at = int(time.time())
        session.add(auth_session)


async def revoke_all_sessions(session: AsyncSession, user_id: str) -> None:
    rows = await session.scalars(
        select(AuthSession).where(
            AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
        )
    )
    now = int(time.time())
    for row in rows:
        row.revoked_at = now
        session.add(row)
