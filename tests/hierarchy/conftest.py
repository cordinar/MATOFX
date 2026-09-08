"""Shared fixtures for matofx.hierarchy tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import AuthSettings, DbSettings
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.hierarchy import (
    JobService,
    ProjectService,
    ScenarioService,
    make_job_service,
    make_project_service,
    make_scenario_service,
)
from matofx.studies import (
    orm as _studies_orm,  # noqa: F401  -- registers ParametricStudy in the shared MetaData so Job.parametric_study_id's FK resolves
)
from matofx.users import UserService, make_password_hasher, make_user_service
from matofx.workflows import (
    orm as _workflow_orm,  # noqa: F401  -- registers WorkflowRun in the shared MetaData so Job.workflow_run_id's FK resolves
)
from tests._migrated_db import postgres_db_settings, sqlite_db_settings


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def hierarchy_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    if request.param == "sqlite":
        # A byte copy of the session-scoped migrated template —
        # milliseconds, where migrating per test cost ~30 s.
        yield sqlite_db_settings(migrated_template, tmp_path, "h.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def hierarchy_engine(hierarchy_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(hierarchy_db_settings)
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
def project_service(audit_writer: AuditWriter) -> ProjectService:
    return make_project_service(audit_writer)


@pytest.fixture
def scenario_service(audit_writer: AuditWriter) -> ScenarioService:
    return make_scenario_service(audit_writer)


@pytest.fixture
def job_service(audit_writer: AuditWriter) -> JobService:
    return make_job_service(audit_writer)


async def _seed_users(factory, user_service: UserService) -> tuple[int, int, int]:
    """Create three users: admin (super), alice (regular), bob (regular).

    Returns (admin_id, alice_id, bob_id).
    """
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        admin = await user_service.create_user(
            session,
            username="admin",
            email="admin@example.com",
            password="adminpassword",
            full_name="Admin",
            role="super_user",
        )
        alice = await user_service.create_user(
            session,
            username="alice",
            email="alice@example.com",
            password="alicepassword",
            full_name="Alice",
        )
        bob = await user_service.create_user(
            session,
            username="bob",
            email="bob@example.com",
            password="bobpassword",
            full_name="Bob",
        )
        return admin.id, alice.id, bob.id


@pytest.fixture
async def seed_users(
    hierarchy_engine: AsyncEngine, user_service: UserService
) -> tuple[int, int, int]:
    """Yield (admin_id, alice_id, bob_id) — three users seeded into the test DB."""
    factory = make_session_factory(hierarchy_engine)
    return await _seed_users(factory, user_service)


@pytest.fixture
async def seed_tool(hierarchy_engine: AsyncEngine, seed_users: tuple[int, int, int]) -> str:
    """Insert a stub ``tool`` row + initial ``tool_version`` snapshot.

    Job submission resolves the latest ``tool_version`` for the alias
    and pins it onto the new job (NOT NULL FK from Session B), so an
    accompanying snapshot is required for the alias to be usable.

    Returns the alias string (``"demo"``).
    """
    import datetime as _dt  # noqa: PLC0415

    from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415

    admin_id, _, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    descriptor = {"alias": "demo"}
    bundle_path = "bundle/demo"
    async with session_scope(factory) as session:
        session.add(
            Tool(
                alias="demo",
                name="Demo",
                version="1.0",
                state="released",
                descriptor=descriptor,
                bundle_path=bundle_path,
                installed_by_user_id=admin_id,
            )
        )
        session.add(
            ToolVersion(
                alias="demo",
                version="1.0",
                descriptor_snapshot=descriptor,
                bundle_path=bundle_path,
                created_at=_dt.datetime.now(tz=_dt.UTC),
                created_by_user_id=admin_id,
                change_summary="Test fixture seed",
                changes={},
            )
        )
    return "demo"
