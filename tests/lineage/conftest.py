"""Shared fixtures for matofx.lineage tests.

Mirrors :mod:`tests.hierarchy.conftest` — the lineage layer composes the
hierarchy services, so the test stack reuses the same engine /
migration / users / tool fixtures verbatim. The only extra fixture is
:func:`lineage_service`.
"""

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
from matofx.lineage import LineageService, make_lineage_service
from matofx.studies import orm as _studies_orm  # noqa: F401  -- register FK target
from matofx.users import UserService, make_password_hasher, make_user_service
from matofx.users.permissions import Permission
from matofx.vv import orm as _vv_orm  # noqa: F401  -- register FK target (ADR-0011)
from matofx.workflows import orm as _workflow_orm  # noqa: F401  -- register FK target
from tests._migrated_db import postgres_db_settings, sqlite_db_settings


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def lineage_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    if request.param == "sqlite":
        # A byte copy of the session-scoped migrated template —
        # milliseconds, where migrating per test cost ~30 s.
        yield sqlite_db_settings(migrated_template, tmp_path, "l.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def lineage_engine(lineage_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(lineage_db_settings)
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


@pytest.fixture
def lineage_service(audit_writer: AuditWriter) -> LineageService:
    return make_lineage_service(audit_writer)


@pytest.fixture
async def seed_users(
    lineage_engine: AsyncEngine, user_service: UserService
) -> tuple[int, int, int]:
    """Yield (admin_id, alice_id, bob_id).

    ``admin`` is a ``super_user`` — the catalog sentinel gives them
    ``lineage.view`` automatically. ``alice`` and ``bob`` are
    ``regular_user`` and do **not** hold the permission by default;
    individual tests grant it where needed via the role editor (or use
    ``admin`` for build_graph calls and switch the inspected actor for
    ACL checks).
    """
    factory = make_session_factory(lineage_engine)
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
async def seed_tool(lineage_engine: AsyncEngine, seed_users: tuple[int, int, int]) -> str:
    """Seed one tool + initial tool_version snapshot (alias=``demo``)."""
    import datetime as _dt  # noqa: PLC0415

    from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415

    admin_id, _, _ = seed_users
    factory = make_session_factory(lineage_engine)
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


async def _grant_lineage_view_to_regular_users(factory) -> None:
    """Add ``lineage.view`` to the ``regular_user`` role.

    Migration ``0052`` (slice 3) will backfill this on installs that
    were upgraded across the lineage landing; slice 1 tests use the
    catalog default which only grants the slug to ``administrator``
    via :data:`ADMINISTRATOR_DEFAULT_PERMISSIONS`. Regular-user ACL
    tests grant it explicitly here.
    """
    import datetime as _dt  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from matofx.users.role_orm import Role, RolePermission  # noqa: PLC0415

    async with session_scope(factory) as session:
        role_id = (
            await session.execute(select(Role.id).where(Role.name == "regular_user"))
        ).scalar_one()
        existing = (
            await session.execute(
                select(RolePermission.permission).where(
                    RolePermission.role_id == role_id,
                    RolePermission.permission == Permission.LINEAGE_VIEW.value,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                RolePermission(
                    role_id=role_id,
                    permission=Permission.LINEAGE_VIEW.value,
                    granted_at=_dt.datetime.now(tz=_dt.UTC),
                )
            )


@pytest.fixture
async def grant_lineage_view_to_regular_users(lineage_engine: AsyncEngine) -> None:
    """Apply :func:`_grant_lineage_view_to_regular_users`."""
    factory = make_session_factory(lineage_engine)
    await _grant_lineage_view_to_regular_users(factory)
