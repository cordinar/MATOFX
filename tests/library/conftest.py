"""Shared fixtures for matofx.library tests.

Schema is built by Alembic ``upgrade head`` (the migration is the source
of truth, mirroring ``tests/studies/conftest.py``), so these tests also
exercise migration ``0045`` on both engines. The ``library.orm`` module
is imported so the four library tables register in the shared MetaData;
``studies.orm`` / ``workflows.orm`` are imported so the ``job`` table's
``parametric_study_id`` / ``workflow_run_id`` FKs resolve when ``Job`` is
mapped (``job_library_file_use`` FKs *to* ``job``).
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
from matofx.library import orm as _library_orm  # noqa: F401  -- registers library tables
from matofx.studies import orm as _studies_orm  # noqa: F401  -- registers ParametricStudy
from matofx.users import make_password_hasher, make_user_service
from matofx.workflows import orm as _workflow_orm  # noqa: F401  -- registers WorkflowRun
from tests._migrated_db import postgres_db_settings, sqlite_db_settings


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def library_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    if request.param == "sqlite":
        # A byte copy of the session-scoped migrated template —
        # milliseconds, where migrating per test cost ~30 s.
        yield sqlite_db_settings(migrated_template, tmp_path, "lib.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def library_engine(library_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(library_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


class Basics:
    """Prerequisite FK parents for inserting library rows directly."""

    def __init__(
        self,
        *,
        user_id: int,
        other_user_id: int,
        project_id: int,
        scenario_id: int,
        tool_alias: str,
        tool_version_id: int,
        job_id: int,
    ) -> None:
        self.user_id = user_id
        self.other_user_id = other_user_id
        self.project_id = project_id
        self.scenario_id = scenario_id
        self.tool_alias = tool_alias
        self.tool_version_id = tool_version_id
        self.job_id = job_id


@pytest.fixture
async def basics(library_engine: AsyncEngine) -> Basics:
    """Seed two users, a project, a scenario, a tool + version, and a job.

    Returns their ids so constraint tests can construct library ORM rows
    directly (the FKs are enforced).
    """
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

    factory = make_session_factory(library_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="sam",
            email="sam@example.com",
            password="sampassword",
            full_name="Sam",
            role="super_user",
        )
        other = await user_service.create_user(
            session,
            username="ravi",
            email="ravi@example.com",
            password="ravipassword",
            full_name="Ravi",
            role="regular_user",
        )
        project = await project_service.create(session, actor_user_id=user.id, name="P1")
        scenario = await scenario_service.create(
            session, actor_user_id=user.id, project_id=project.id, name="S1"
        )
        descriptor = {"alias": "demo"}
        session.add(
            Tool(
                alias="demo",
                name="Demo",
                version="1.0",
                state="released",
                descriptor=descriptor,
                bundle_path="bundle/demo",
                installed_by_user_id=user.id,
            )
        )
        tv = ToolVersion(
            alias="demo",
            version="1.0",
            descriptor_snapshot=descriptor,
            bundle_path="bundle/demo",
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=user.id,
            change_summary="seed",
            changes={},
        )
        session.add(tv)
        await session.flush()
        job = Job(
            scenario_id=scenario.id,
            name="J1",
            tool_alias="demo",
            tool_version_id=tv.id,
            state="created",
            inputs={},
            created_by_user_id=user.id,
        )
        session.add(job)
        await session.flush()
        return Basics(
            user_id=user.id,
            other_user_id=other.id,
            project_id=project.id,
            scenario_id=scenario.id,
            tool_alias="demo",
            tool_version_id=tv.id,
            job_id=job.id,
        )
