"""
Module for the general logic of token tests
"""

import time

from httpx import AsyncClient

from app.core.security.jwt import verify_jwt_token
from app.main import app


def login(client: AsyncClient, email: str, password: str):
    return client.post(
        app.url_path_for("login_access_token"),
        data={"username": email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def refresh(client: AsyncClient, token: str | None = None):
    """empty body -> backend takes the refresh cookie"""
    body = {"refresh_token": token} if token else None
    return client.post(app.url_path_for("refresh_token"), json=body)


def check_token_expire_time(token, sec, place) -> bool:
    """check for time parameters of tokens"""
    current_timestamp = int(time.time())
    return token[place] == current_timestamp + sec


def validate_token_response(token, user_id) -> bool:
    """check token verify response parameters"""
    now = int(time.time())
    token_payload = verify_jwt_token(token["access_token"])

    return (
        token_payload.sub == user_id
        and token_payload.iat == now
        and token_payload.exp == token["expires_at"]
    )
