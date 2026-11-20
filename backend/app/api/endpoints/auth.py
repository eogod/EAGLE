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
from app.core.security.jwt import create_jwt_token
from app.core.security.password import (
    DUMMY_PASSWORD,
    get_password_hash,
    verify_password,
)
from app.models import RefreshToken, User
from app.schemas.requests import RefreshTokenRequest, UserCreateRequest
from app.schemas.responses import AccessTokenResponse, UserResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# refresh cookie stays scoped to the refresh endpoint only
COOKIE_SECURE = bool(get_settings().security.cookie_secure)


def _set_session_cookies(
    response: Response, access_token: str, refresh_token: str
) -> None:
    """HttpOnly, keep the tokens out of the frontend storage"""
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
    session: AsyncSession = Depends(deps.get_session),
    form_data: OAuth2PasswordRequestForm = Depends(),
) -> AccessTokenResponse:
    """get user by email -> compare hashed password
    return jwt token, cookies are set alongside the body for the browser"""
    query = select(User).where(User.email == form_data.username)
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

    jwt_token = create_jwt_token(user_id=user.user_id)

    refresh_token_new = RefreshToken(
        user_id=user.user_id,
        refresh_token=secrets.token_urlsafe(32),
        exp=int(time.time() + get_settings().security.refresh_token_expire_secs),
    )
    session.add(refresh_token_new)
    await session.commit()

    _set_session_cookies(
        response, jwt_token.access_token, refresh_token_new.refresh_token
    )

    return AccessTokenResponse(
        access_token=jwt_token.access_token,
        expires_at=jwt_token.payload.exp,
        refresh_token=refresh_token_new.refresh_token,
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
    token = await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.refresh_token == data.refresh_token)
        .with_for_update(skip_locked=True)
    )

    if token is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=api_messages.REFRESH_TOKEN_NOT_FOUND,
        )
    # not elif cuz else raise
    if time.time() > token.exp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=api_messages.REFRESH_TOKEN_EXPIRED,
        )
    if token.used:
        # replay means theft -> wipe all sessions of the user
        # (SKIP LOCKED keeps parallel replays serialized, no double delete)
        victim_user_id = token.user_id
        await session.execute(
            delete(RefreshToken).where(RefreshToken.user_id == victim_user_id)
        )
        await session.commit()
        logger.warning(
            "refresh token reuse for user %s, revoked all three sessions",
            victim_user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=api_messages.REFRESH_TOKEN_ALREADY_USED,
        )

    token.used = True
    session.add(token)

    jwt_token = create_jwt_token(user_id=token.user_id)

    refresh_token_new = RefreshToken(
        user_id=token.user_id,
        refresh_token=secrets.token_urlsafe(32),
        exp=int(time.time() + get_settings().security.refresh_token_expire_secs),
    )
    session.add(refresh_token_new)
    await session.commit()

    _set_session_cookies(
        response, jwt_token.access_token, refresh_token_new.refresh_token
    )

    return AccessTokenResponse(
        access_token=jwt_token.access_token,
        expires_at=jwt_token.payload.exp,
        refresh_token=refresh_token_new.refresh_token,
        refresh_token_expires_at=refresh_token_new.exp,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    description="Revoke all refresh tokens of the user and clear session cookies",
)
async def logout(
    response: Response,
    current_user: User = Depends(deps.get_current_user),
    session: AsyncSession = Depends(deps.get_session),
) -> None:
    """all refresh tokens die, no forgotten laptop stays logged in"""
    await session.execute(
        delete(RefreshToken).where(RefreshToken.user_id == current_user.user_id)
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
