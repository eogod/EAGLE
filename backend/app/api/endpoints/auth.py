"""
Module that handles all responsibilities and endpoints
that related to authentication, access tokens, registration
"""

import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import api_messages, deps
from app.core.config import get_settings
from app.core.security.jwt import create_jwt_token, verify_jwt_token
from app.core.security.password import (
    DUMMY_PASSWORD,
    get_password_hash,
    verify_password,
)
from app.models import AuthSession, RefreshToken, User
from app.schemas.requests import RefreshTokenRequest, UserCreateRequest
from app.schemas.responses import AccessTokenResponse, UserResponse
from app.services.auth_sessions import (
    hash_refresh_token,
    revoke_all_sessions,
    revoke_session,
    session_expiry,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# refresh cookie stays scoped to the refresh endpoint only
COOKIE_SECURE = bool(get_settings().security.cookie_secure)


def _drop_legacy_refresh_cookie(response: Response) -> None:
    """old builds scoped the cookie to the refresh path only"""
    response.delete_cookie(
        deps.REFRESH_COOKIE,
        path="/api/auth/refresh-token",
        secure=COOKIE_SECURE,
        samesite="strict",
    )


def _set_session_cookies(
    response: Response, access_token: str, refresh_token: str
) -> None:
    """HttpOnly, keep the tokens out of the frontend storage"""
    _drop_legacy_refresh_cookie(response)
    cfg = get_settings().security
    response.set_cookie(
        deps.ACCESS_COOKIE,
        access_token,
        max_age=cfg.jwt_access_token_expire_secs,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite="strict",
    )
    response.set_cookie(
        deps.REFRESH_COOKIE,
        refresh_token,
        max_age=cfg.refresh_token_expire_secs,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite="strict",
        path=deps.REFRESH_COOKIE_PATH,
    )


def _clear_session_cookies(response: Response) -> None:
    _drop_legacy_refresh_cookie(response)
    response.delete_cookie(deps.ACCESS_COOKIE, secure=COOKIE_SECURE, samesite="strict")
    response.delete_cookie(
        deps.REFRESH_COOKIE,
        path=deps.REFRESH_COOKIE_PATH,
        secure=COOKIE_SECURE,
        samesite="strict",
    )


ACCESS_TOKEN_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "description": "Invalid email or password",
        "content": {
            "application/json": {"example": {"detail": api_messages.PASSWORD_INVALID}}
        },
    },
}

REFRESH_TOKEN_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "description": "Refresh token expired or is already used",
        "content": {
            "application/json": {
                "examples": {
                    "refresh token expired": {
                        "summary": api_messages.REFRESH_TOKEN_EXPIRED,
                        "value": {"detail": api_messages.REFRESH_TOKEN_EXPIRED},
                    },
                    "refresh token already used": {
                        "summary": api_messages.REFRESH_TOKEN_ALREADY_USED,
                        "value": {"detail": api_messages.REFRESH_TOKEN_ALREADY_USED},
                    },
                }
            }
        },
    },
    401: {
        "description": "Replayed rotated refresh token, all user sessions revoked",
        "content": {
            "application/json": {
                "example": {
                    "detail": api_messages.REFRESH_TOKEN_ALREADY_USED,
                }
            }
        },
    },
    404: {
        "description": "Refresh token does not exist",
        "content": {
            "application/json": {
                "example": {"detail": api_messages.REFRESH_TOKEN_NOT_FOUND}
            }
        },
    },
}


@router.post(
    "/access-token",
    response_model=AccessTokenResponse,
    responses=ACCESS_TOKEN_RESPONSES,
    description="OAuth2 compatible token, get an access token "
    "for future requests using username and password",
)
async def login_access_token(
    response: Response,
    request: Request,
    session: AsyncSession = Depends(deps.get_session),
    form_data: OAuth2PasswordRequestForm = Depends(),
) -> AccessTokenResponse:
    """get user by email -> compare hashed password
    return jwt token, cookies are set alongside the body for the browser"""
    query = select(User).where(User.email == form_data.username).with_for_update()
    user = await session.scalar(query)

    if user is None:
        # this is naive method to not return early
        verify_password(form_data.password, DUMMY_PASSWORD)

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=api_messages.PASSWORD_INVALID,
        )

    if not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=api_messages.PASSWORD_INVALID,
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=api_messages.PASSWORD_INVALID,
        )

    raw_refresh = secrets.token_urlsafe(32)
    auth_session = AuthSession(
        user_id=user.user_id,
        expires_at=session_expiry(),
        created_at=int(time.time()),
        last_used_at=int(time.time()),
        user_agent=request.headers.get("user-agent", "")[:256] or None,
        ip=request.client.host if request.client else None,
    )
    session.add(auth_session)
    await session.flush()

    refresh_token_new = RefreshToken(
        user_id=user.user_id,
        session_id=auth_session.id,
        token_hash=hash_refresh_token(raw_refresh),
        exp=auth_session.expires_at,
    )
    session.add(refresh_token_new)
    await session.commit()

    jwt_token = create_jwt_token(user_id=user.user_id, session_id=auth_session.id)
    _set_session_cookies(response, jwt_token.access_token, raw_refresh)

    return AccessTokenResponse(
        access_token=jwt_token.access_token,
        expires_at=jwt_token.payload.exp,
        refresh_token=raw_refresh,
        refresh_token_expires_at=refresh_token_new.exp,
    )


@router.post(
    "/refresh-token",
    response_model=AccessTokenResponse,
    responses=REFRESH_TOKEN_RESPONSES,
    description="OAuth2 compatible token, get an access token"
    "for future requests using refresh token",
)
async def refresh_token(
    response: Response,
    request: Request,
    data: RefreshTokenRequest | None = None,
    session: AsyncSession = Depends(deps.get_session),
) -> AccessTokenResponse:
    """cookie first, json body second;
    replay of a rotated token wipes the whole family of that user"""
    if not data or not data.refresh_token:
        cookie_token = request.cookies.get(deps.REFRESH_COOKIE)
        if not cookie_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=api_messages.REFRESH_TOKEN_NOT_FOUND,
            )
        data = RefreshTokenRequest(refresh_token=cookie_token)
    now = int(time.time())
    token = await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_refresh_token(data.refresh_token))
        .with_for_update(skip_locked=True)
    )

    if token is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_messages.REFRESH_TOKEN_NOT_FOUND,
        )
    # not elif cuz else raise
    if now > token.exp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=api_messages.REFRESH_TOKEN_EXPIRED,
        )

    auth_session = await session.get(AuthSession, token.session_id)
    if (
        auth_session is None
        or auth_session.revoked_at is not None
        or auth_session.expires_at <= now
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=api_messages.REFRESH_TOKEN_ALREADY_USED,
        )

    if token.used_at is not None:
        grace = get_settings().security.refresh_rotation_grace_secs
        if now - token.used_at <= grace:
            # another tab rotated first; its cookie is already in the jar
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=api_messages.REFRESH_TOKEN_ALREADY_USED,
            )
        # a replay means the token leaked; revoke this session and leave other devices alone
        await revoke_session(session, auth_session)
        await session.execute(
            delete(RefreshToken).where(RefreshToken.session_id == auth_session.id)
        )
        await session.commit()
        logger.warning(
            "refresh token reuse for session %s, revoked that session",
            auth_session.id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=api_messages.REFRESH_TOKEN_ALREADY_USED,
        )

    token.used_at = now
    session.add(token)
    auth_session.last_used_at = now
    session.add(auth_session)

    raw_refresh = secrets.token_urlsafe(32)
    refresh_token_new = RefreshToken(
        user_id=token.user_id,
        session_id=auth_session.id,
        token_hash=hash_refresh_token(raw_refresh),
        exp=auth_session.expires_at,
    )
    session.add(refresh_token_new)
    await session.commit()

    jwt_token = create_jwt_token(user_id=token.user_id, session_id=auth_session.id)
    _set_session_cookies(response, jwt_token.access_token, raw_refresh)

    return AccessTokenResponse(
        access_token=jwt_token.access_token,
        expires_at=jwt_token.payload.exp,
        refresh_token=raw_refresh,
        refresh_token_expires_at=refresh_token_new.exp,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    description="Revoke the session and clear cookies, even after access expires",
)
async def logout(
    response: Response,
    request: Request,
    data: RefreshTokenRequest | None = None,
    session: AsyncSession = Depends(deps.get_session),
) -> None:
    """refresh cookie identifies the session; access is only a fallback"""
    raw_refresh = data.refresh_token if data and data.refresh_token else None
    raw_refresh = raw_refresh or request.cookies.get(deps.REFRESH_COOKIE)

    if raw_refresh:
        token = await session.scalar(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(raw_refresh)
            )
        )
        if token is not None:
            auth_session = await session.get(AuthSession, token.session_id)
            if auth_session is not None:
                await revoke_session(session, auth_session)
            await session.execute(
                delete(RefreshToken).where(RefreshToken.session_id == token.session_id)
            )
    else:
        raw_access = request.headers.get("authorization", "").removeprefix("Bearer ")
        raw_access = raw_access or request.cookies.get(deps.ACCESS_COOKIE)
        if raw_access:
            try:
                payload = verify_jwt_token(raw_access)
            except HTTPException:
                payload = None
            if payload is not None:
                await revoke_all_sessions(session, payload.sub)
                await session.execute(
                    delete(RefreshToken).where(RefreshToken.user_id == payload.sub)
                )

    await session.commit()
    _clear_session_cookies(response)


@router.post(
    "/register",
    response_model=UserResponse,
    description="Create new user",
    status_code=status.HTTP_201_CREATED,
)
async def register_new_user(
    new_user: UserCreateRequest,
    session: AsyncSession = Depends(deps.get_session),
    current_user: User = Depends(deps.get_current_user),
) -> User:
    """open registration + bash tool on same box = anyone owns the machine"""
    # TODO: role system (admin/operator/auditor) + admin UI, for now only
    # the bootstrap admin from .env exists and registration stays closed
    if not get_settings().security.registration_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is disabled, ask admin to create the account",
        )
    query = select(User).where(User.email == new_user.email)
    user = await session.scalar(query)
    if user is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=api_messages.EMAIL_ADDRESS_ALREADY_USED,
        )

    user = User(
        email=new_user.email,
        hashed_password=get_password_hash(new_user.password),
        role=new_user.role,
        username=new_user.username,
    )
    session.add(user)

    try:
        await session.commit()
    except IntegrityError as exc:  # pragma: no cover
        await session.rollback()

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=api_messages.EMAIL_ADDRESS_ALREADY_USED,
        ) from exc

    return user
