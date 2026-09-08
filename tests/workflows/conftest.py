"""Shared fixtures for matofx.workflows tests.

Provides:

- Parametrised SQLite + Postgres db settings + engine fixtures (the
  Postgres slot skips when ``MATOFX_TEST_POSTGRES_URL`` isn't set).
- An unsigned :class:`AuditWriter`.
- Wired-up :class:`UserService`, :class:`ToolService`,
  :class:`WorkflowService` instances.
- ``seed_roles`` — one user per built-in role, keyed by role name.
- ``install_tool`` — helper that installs and (optionally) promotes
  a tool to ``released`` so workflow tests can build realistic
  selections without re-implementing the tool install dance per file.
"""

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
from matofx.workflows import WorkflowService, make_workflow_service
from tests._migrated_db import postgres_db_settings, sqlite_db_settings


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def workflows_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    if request.param == "sqlite":
        # Byte copy of the session template (see tests/_migrated_db.py).
        yield sqlite_db_settings(migrated_template, tmp_path, "w.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def workflows_engine(
    workflows_db_settings: DbSettings,
) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(workflows_db_settings)
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
def workflow_service(audit_writer: AuditWriter) -> WorkflowService:
    return make_workflow_service(audit_writer)


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
async def seed_roles(
    workflows_engine: AsyncEngine, user_service: UserService
) -> dict[str, int]:
    """One user per built-in role keyed by role name."""
    factory = make_session_factory(workflows_engine)
    return {
        "super_user": await _make_user(
            factory, user_service, username="root", role="super_user"
        ),
        "administrator": await _make_user(
            factory, user_service, username="ada", role="administrator"
        ),
        "beta_tester": await _make_user(
            factory, user_service, username="beth", role="beta_tester"
        ),
        "regular_user": await _make_user(
            factory, user_service, username="alice", role="regular_user"
        ),
    }


def make_tool_descriptor(
    *,
    alias: str,
    name: str | None = None,
    downstream_tools: list[str] | None = None,
    inputs: list[dict] | None = None,
    outputs: list[dict] | None = None,
    teams: list[str] | None = None,
) -> ToolDescriptor:
    """Build a minimal-but-realistic descriptor with optional wiring.

    ``downstream_tools`` and ``inputs`` are how the ordering algorithm
    learns dependencies; pass them when a test needs a chain. ``outputs``
    lets a test declare file outputs an upstream member produces (the
    ADR-0015 file hand-off).
    """
    payload: dict = {
        "alias": alias,
        "name": name or alias.replace("_", " ").title(),
        "version": "1.0.0",
        "description": f"Test fixture for {alias}.",
        "main_script": "static/main.py",
        "inputs": inputs or [],
    }
    if downstream_tools is not None:
        payload["downstream_tools"] = downstream_tools
    if outputs is not None:
        payload["outputs"] = outputs
    if teams is not None:
        payload["teams"] = teams
    return ToolDescriptor(**payload)


async def install_released_tool(
    factory,
    tool_service: ToolService,
    *,
    actor_user_id: int,
    descriptor: ToolDescriptor,
) -> str:
    """Install a tool and promote straight to ``released``.

    Workflow tests usually want released-state tools so the visibility
    gate passes for non-admin owners. Returns the tool's alias.
    """
    async with session_scope(factory) as session:
        await tool_service.install(
            session,
            actor_user_id=actor_user_id,
            descriptor=descriptor,
            bundle_path=f"/tools/{descriptor.alias}",
        )
    async with session_scope(factory) as session:
        await tool_service.promote_state(
            session,
            actor_user_id=actor_user_id,
            alias=descriptor.alias,
            new_state="test",
        )
    async with session_scope(factory) as session:
        await tool_service.promote_state(
            session,
            actor_user_id=actor_user_id,
            alias=descriptor.alias,
            new_state="released",
        )
    return descriptor.alias
