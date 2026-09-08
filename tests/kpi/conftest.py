"""Shared fixtures for matofx.kpi tests.

Schema is built by Alembic ``upgrade head`` (the migration is the
source of truth, mirroring ``tests/vv/conftest.py`` / ``tests/materials/
conftest.py``), so these tests also exercise migration ``0057``
end-to-end on both engines. The ``kpi.orm`` module is imported so the
``job_kpi_value`` table registers in the shared MetaData;
``library.orm`` / ``studies.orm`` / ``workflows.orm`` are imported so
the FKs from ``Job`` (to ``parametric_study`` / ``workflow_run``)
resolve when their ORM classes are mapped.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import AuthSettings, DbSettings
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.hierarchy import make_project_service, make_scenario_service
from matofx.kpi import orm as _kpi_orm  # noqa: F401  -- registers job_kpi_value
from matofx.library import orm as _library_orm  # noqa: F401  -- FK targets for Job
from matofx.studies import orm as _studies_orm  # noqa: F401  -- FK targets for Job
from matofx.users import make_password_hasher, make_user_service
from matofx.workflows import orm as _workflow_orm  # noqa: F401  -- FK targets for Job
from tests._migrated_db import postgres_db_settings, sqlite_db_settings


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def kpi_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    if request.param == "sqlite":
        # A byte copy of the session-scoped migrated template —
        # milliseconds, where migrating per test cost ~30 s.
        yield sqlite_db_settings(migrated_template, tmp_path, "kpi.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def kpi_engine(kpi_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(kpi_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


class Basics:
    """Prerequisite FK parents for inserting ``JobKpiValue`` rows directly.

    KPI rows FK to ``job`` (CASCADE) and ``tool_version`` (RESTRICT).
    The same tool seeds two versions so the RESTRICT semantics can be
    exercised without a separate fixture rebuild.
    """

    def __init__(
        self,
        *,
        user_id: int,
        tool_alias: str,
        tool_version_id_a: int,
        tool_version_id_b: int,
        job_id_a: int,
        job_id_b: int,
    ) -> None:
        self.user_id = user_id
        self.tool_alias = tool_alias
        self.tool_version_id_a = tool_version_id_a
        self.tool_version_id_b = tool_version_id_b
        self.job_id_a = job_id_a
        self.job_id_b = job_id_b


@pytest.fixture
async def basics(kpi_engine: AsyncEngine) -> Basics:
    """Seed one user, one tool with two versions, and two jobs on version A."""
    from matofx.hierarchy.orm import Job  # noqa: PLC0415
    from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415

    auth = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )
    audit = AuditWriter(signing_key=None, key_id=None)
    user_service = make_user_service(auth, audit, hasher=make_password_hasher(auth))
    project_service = make_project_service(audit)
    scenario_service = make_scenario_service(audit)

    factory = make_session_factory(kpi_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        author = await user_service.create_user(
            session,
            username="anika",
            email="anika@example.com",
            password="anikapassword",
            full_name="Anika",
            role="super_user",
        )
        project = await project_service.create(session, actor_user_id=author.id, name="P1")
        scenario_a = await scenario_service.create(
            session, actor_user_id=author.id, project_id=project.id, name="S1"
        )
        scenario_b = await scenario_service.create(
            session, actor_user_id=author.id, project_id=project.id, name="S2"
        )
        descriptor = {"alias": "heat_transfer"}
        session.add(
            Tool(
                alias="heat_transfer",
                name="Heat Transfer",
                version="1.0",
                state="released",
                descriptor=descriptor,
                bundle_path="bundle/heat_transfer",
                installed_by_user_id=author.id,
            )
        )
        tv_a = ToolVersion(
            alias="heat_transfer",
            version="1.0.0",
            descriptor_snapshot=descriptor,
            bundle_path="bundle/heat_transfer/1.0.0",
            bundle_hash="aaaa" * 16,
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=author.id,
            change_summary="seed",
            changes={},
        )
        tv_b = ToolVersion(
            alias="heat_transfer",
            version="2.0.0",
            descriptor_snapshot=descriptor,
            bundle_path="bundle/heat_transfer/2.0.0",
            bundle_hash="bbbb" * 16,
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=author.id,
            change_summary="major bump",
            changes={},
        )
        session.add(tv_a)
        session.add(tv_b)
        await session.flush()
        job_a = Job(
            scenario_id=scenario_a.id,
            name="J1",
            tool_alias="heat_transfer",
            tool_version_id=tv_a.id,
            state="complete",
            inputs={},
            created_by_user_id=author.id,
        )
        job_b = Job(
            scenario_id=scenario_b.id,
            name="J2",
            tool_alias="heat_transfer",
            tool_version_id=tv_a.id,
            state="complete",
            inputs={},
            created_by_user_id=author.id,
        )
        session.add(job_a)
        session.add(job_b)
        await session.flush()
        return Basics(
            user_id=author.id,
            tool_alias="heat_transfer",
            tool_version_id_a=tv_a.id,
            tool_version_id_b=tv_b.id,
            job_id_a=job_a.id,
            job_id_b=job_b.id,
        )
