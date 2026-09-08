"""Shared fixtures for matofx.tools tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import AuthSettings, DbSettings
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.tools import ToolDescriptor, ToolService, make_tool_service
from matofx.users import UserRole, UserService, make_password_hasher, make_user_service
from tests._migrated_db import postgres_db_settings, sqlite_db_settings


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def tools_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    if request.param == "sqlite":
        # A byte copy of the session-scoped migrated template —
        # milliseconds, where migrating per test cost ~30 s.
        yield sqlite_db_settings(migrated_template, tmp_path, "t.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def tools_engine(tools_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(tools_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def auth_settings() -> AuthSettings:
    return AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )


@pytest.fixture
def audit_writer() -> AuditWriter:
    return AuditWriter(signing_key=None, key_id=None)


@pytest.fixture
def user_service(auth_settings: AuthSettings, audit_writer: AuditWriter) -> UserService:
    return make_user_service(
        auth_settings, audit_writer, hasher=make_password_hasher(auth_settings)
    )


@pytest.fixture
def tool_service(audit_writer: AuditWriter) -> ToolService:
    return make_tool_service(audit_writer)


@pytest.fixture
def example_descriptor() -> ToolDescriptor:
    """A minimal-but-realistic descriptor for tests."""
    return ToolDescriptor(
        alias="demo-tool",
        name="Demo Tool",
        version="1.0.0",
        description="Used by the test suite.",
        main_script="static/main.py",
        inputs=[],
    )


async def _make_user(
    factory,
    user_service: UserService,
    *,
    username: str,
    role: UserRole,
) -> int:
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username=username,
            email=f"{username}@example.com",
            password="securepassword1",
            full_name=username.title(),
            role=role,
        )
        return user.id


@pytest.fixture
async def seed_roles(tools_engine: AsyncEngine, user_service: UserService) -> dict[str, int]:
    """Yield {role_name: user_id} for one user per built-in role."""
    factory = make_session_factory(tools_engine)
    return {
        "super_user": await _make_user(factory, user_service, username="root", role="super_user"),
        "administrator": await _make_user(
            factory, user_service, username="ada", role="administrator"
        ),
        "beta_tester": await _make_user(factory, user_service, username="beth", role="beta_tester"),
        "regular_user": await _make_user(
            factory, user_service, username="alice", role="regular_user"
        ),
    }
