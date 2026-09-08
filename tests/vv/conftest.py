"""Shared fixtures for matofx.vv tests.

Schema is built by Alembic ``upgrade head`` (the migration is the source
of truth, mirroring ``tests/materials/conftest.py``), so these tests
also exercise migrations ``0053`` (bundle hash column) and ``0054``
(V&V tables) on both engines. The ``vv.orm`` module is imported so the
two V&V tables register in the shared MetaData; ``studies.orm`` /
``workflows.orm`` / ``library.orm`` are imported so the FKs from the
V&V tables (FK to ``library_file_version``) and from ``Job`` (FK to
``parametric_study`` / ``workflow_run``) resolve when their respective
ORM classes are mapped.
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
from matofx.library import orm as _library_orm  # noqa: F401  -- registers library_file_version
from matofx.studies import orm as _studies_orm  # noqa: F401  -- registers ParametricStudy
from matofx.users import make_password_hasher, make_user_service
from matofx.vv import orm as _vv_orm  # noqa: F401  -- registers V&V tables
from matofx.workflows import orm as _workflow_orm  # noqa: F401  -- registers WorkflowRun
from tests._migrated_db import postgres_db_settings, sqlite_db_settings


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def vv_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    if request.param == "sqlite":
        # A byte copy of the session-scoped migrated template —
        # milliseconds, where migrating per test cost ~30 s.
        yield sqlite_db_settings(migrated_template, tmp_path, "vv.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def vv_engine(vv_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(vv_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


class Basics:
    """Prerequisite FK parents for inserting V&V rows directly.

    V&V records FK to ``tool_version`` (RESTRICT), ``user`` (RESTRICT —
    twice), and optionally ``library_file_version`` (RESTRICT). Job V&V
    records FK to ``job`` (CASCADE) plus the user / library FKs. We
    seed one of each so the constraint tests can build records without
    a separate fixture rebuild.
    """

    def __init__(
        self,
        *,
        user_id: int,
        other_user_id: int,
        tool_version_id_a: int,
        tool_version_id_b: int,
        job_id: int,
        library_file_version_id: int,
    ) -> None:
        self.user_id = user_id
        self.other_user_id = other_user_id
        self.tool_version_id_a = tool_version_id_a
        self.tool_version_id_b = tool_version_id_b
        self.job_id = job_id
        self.library_file_version_id = library_file_version_id


@pytest.fixture
async def basics(vv_engine: AsyncEngine) -> Basics:
    """Seed users, two tool versions, a job, and one library file version."""
    from matofx.hierarchy.orm import Job  # noqa: PLC0415
    from matofx.library.orm import LibraryFile, LibraryFileVersion  # noqa: PLC0415
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

    factory = make_session_factory(vv_engine)
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
        curator = await user_service.create_user(
            session,
            username="brendan",
            email="brendan@example.com",
            password="brendanpassword",
            full_name="Brendan",
            role="super_user",
        )
        project = await project_service.create(session, actor_user_id=author.id, name="P1")
        scenario = await scenario_service.create(
            session, actor_user_id=author.id, project_id=project.id, name="S1"
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
                installed_by_user_id=author.id,
            )
        )
        # Two tool versions so we can wire records against different
        # FKs in the same test without needing a second alias.
        tv_a = ToolVersion(
            alias="demo",
            version="1.0.0",
            descriptor_snapshot=descriptor,
            bundle_path="bundle/demo/1.0.0",
            bundle_hash="aaaa" * 16,
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=author.id,
            change_summary="seed",
            changes={},
        )
        tv_b = ToolVersion(
            alias="demo",
            version="2.0.0",
            descriptor_snapshot=descriptor,
            bundle_path="bundle/demo/2.0.0",
            bundle_hash="bbbb" * 16,
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=author.id,
            change_summary="major bump",
            changes={},
        )
        session.add(tv_a)
        session.add(tv_b)
        await session.flush()
        job = Job(
            scenario_id=scenario.id,
            name="J1",
            tool_alias="demo",
            tool_version_id=tv_a.id,
            state="created",
            inputs={},
            created_by_user_id=author.id,
        )
        session.add(job)
        # A library file version to FK V&V evidence against.
        lf = LibraryFile(
            name="Validation evidence",
            description="X-rig dataset",
            tags=["vv_evidence"],
            scope="global",
            owner_user_id=author.id,
            state="active",
            created_by_user_id=author.id,
        )
        session.add(lf)
        await session.flush()
        lfv = LibraryFileVersion(
            library_file_id=lf.id,
            ordinal=1,
            stored_path="/storage/lf/1.bin",
            original_filename="x-rig.csv",
            content_hash="cccc" * 16,
            size_bytes=100,
            extension="csv",
            provenance="rig test",
            publish_state="published",
            created_by_user_id=author.id,
        )
        session.add(lfv)
        await session.flush()
        return Basics(
            user_id=author.id,
            other_user_id=curator.id,
            tool_version_id_a=tv_a.id,
            tool_version_id_b=tv_b.id,
            job_id=job.id,
            library_file_version_id=lfv.id,
        )
