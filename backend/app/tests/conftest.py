"""
Module for defining basic test fixtures
"""

import logging
import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
import sqlalchemy
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from app.core import database_session
from app.core.config import get_settings
from app.core.security.jwt import create_jwt_token
from app.core.security.password import get_password_hash
from app.main import app as fastapi_app
from app.models import Base, User

# tests default to a throwaway sqlite file per xdist worker; export
# EAGLE_TEST_DB=postgres to run them against a real database like production
TEST_DB_BACKEND = os.getenv("EAGLE_TEST_DB", "sqlite")

default_user_id = "b75365d9-7bf9-4f54-add5-aeab333a087b"
default_user_email = "geralt@wiedzmin.pl"
default_user_password = "geralt"
default_user_access_token = create_jwt_token(default_user_id).access_token


@pytest_asyncio.fixture(scope="session", autouse=True)
async def fixture_setup_new_test_database(tmp_path_factory) -> None:
    """create new db for tests, setup as fixture session"""
    worker_name = os.getenv("PYTEST_XDIST_WORKER", "gw0")

    session_mpatch = pytest.MonkeyPatch()
    session_mpatch.setenv("SECURITY__PASSWORD_BCRYPT_ROUNDS", "4")

    if TEST_DB_BACKEND == "postgres":
        test_db_name = f"test_db_{worker_name}"

        # create new test db using connection to current database
        conn = await database_session._ASYNC_ENGINE.connect()
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(sqlalchemy.text(f"DROP DATABASE IF EXISTS {test_db_name}"))
        await conn.execute(sqlalchemy.text(f"CREATE DATABASE {test_db_name}"))
        await conn.close()

        # force settings to use now monkeypatched environments
        session_mpatch.setenv("DATABASE__DB", test_db_name)
        get_settings.cache_clear()
        uri = get_settings().sqlalchemy_database_uri
    else:
        # a fresh file per worker: xdist workers must not share one database,
        # and tmp_path_factory cleans it up after the run
        db_path = tmp_path_factory.mktemp("db") / f"test_{worker_name}.db"
        uri = URL.create("sqlite+aiosqlite", database=str(db_path))

    get_settings.cache_clear()

    # monkeypatch test database engine
    engine = database_session.new_async_engine(uri)
    if TEST_DB_BACKEND != "postgres":

        def _enable_foreign_keys(dbapi_conn, _) -> None:
            """sqlite ignores FK constraints unless asked; postgres enforces them"""
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        event.listen(engine.sync_engine, "connect", _enable_foreign_keys)

    session_mpatch.setattr(
        database_session,
        "_ASYNC_ENGINE",
        engine,
    )
    session_mpatch.setattr(
        database_session,
        "_ASYNC_SESSIONMAKER",
        async_sessionmaker(engine, expire_on_commit=False),
    )

    # create app tables in test database
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(scope="function", autouse=True)
async def fixture_clean_get_settings_between_tests() -> AsyncGenerator[None]:
    """prevents test cache interference -> more stable tests"""
    yield

    get_settings.cache_clear()


@pytest_asyncio.fixture(name="default_hashed_password", scope="session")
async def fixture_default_hashed_password() -> str:
    """default password stub"""
    return get_password_hash(default_user_password)


@pytest_asyncio.fixture(name="session", scope="function")
async def fixture_session_with_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncSession]:
    """to rollback db commits after each test function"""
    # we want to monkeypatch get_async_session with one bound to session
    # that we will always rollback on function scope

    connection = await database_session._ASYNC_ENGINE.connect()
    transaction = await connection.begin()

    session = AsyncSession(bind=connection, expire_on_commit=False)

    monkeypatch.setattr(
        database_session,
        "get_async_session",
        lambda: session,
    )

    yield session

    logging.critical("Rolling back transaction")
    await session.close()
    await transaction.rollback()
    await connection.close()


@pytest_asyncio.fixture(name="client", scope="function")
async def fixture_client(session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """setup http api client"""
    asgit = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=asgit, base_url="http://test") as aclient:
        aclient.headers.update({"Host": "localhost"})
        yield aclient


@pytest_asyncio.fixture(name="default_user", scope="function")
async def fixture_default_user(
    session: AsyncSession, default_hashed_password: str
) -> AsyncGenerator[User]:
    """create new user for tests as default_user"""
    default_user = User(
        user_id=default_user_id,
        email=default_user_email,
        hashed_password=default_hashed_password,
    )
    session.add(default_user)

    await session.commit()
    await session.refresh(default_user)

    yield default_user


@pytest_asyncio.fixture(name="default_user_headers", scope="function")
async def fixture_default_user_headers(default_user: User) -> dict[str, str]:
    """for all client requests set auth header with token"""
    return {"Authorization": f"Bearer {default_user_access_token}"}
