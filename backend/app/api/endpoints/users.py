"""Module for interactions with the user's account"""

from fastapi import APIRouter, Depends, status
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.security.password import get_password_hash
from app.models import RefreshToken, User
from app.schemas.requests import UserUpdatePasswordRequest
from app.schemas.responses import UserResponse
from app.services.auth_sessions import revoke_all_sessions

router = APIRouter()


@router.get("/me", response_model=UserResponse, description="Get current user")
async def read_current_user(
    current_user: User = Depends(deps.get_current_user),
) -> User:
    """get information about user by jwt"""
    return current_user


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    description="Delete current user",
)
async def delete_current_user(
    current_user: User = Depends(deps.get_current_user),
    session: AsyncSession = Depends(deps.get_session),
) -> None:
    """delete user by id"""
    query = delete(User).where(User.user_id == current_user.user_id)
    await session.execute(query)
    await session.commit()


@router.post(
    "/reset-password",
    status_code=status.HTTP_204_NO_CONTENT,
    description="Update current user password",
)
async def reset_current_user_password(
    user_update_password: UserUpdatePasswordRequest,
    session: AsyncSession = Depends(deps.get_session),
    current_user: User = Depends(deps.get_current_user),
) -> None:
    """a password change burns every issued access, not only refresh rows"""
    current_user.hashed_password = get_password_hash(user_update_password.password)
    session.add(current_user)
    await revoke_all_sessions(session, current_user.user_id)
    await session.execute(
        delete(RefreshToken).where(RefreshToken.user_id == current_user.user_id)
    )
    await session.commit()
