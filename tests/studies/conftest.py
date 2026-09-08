"""Shared fixtures for matofx.studies tests.

Schema is built by Alembic ``upgrade head`` (the migration is the
source of truth, mirroring ``tests/hierarchy/conftest.py``), so these
tests also exercise migration ``0032`` on both engines. The
``studies.orm`` / ``workflows.orm`` modules are imported so the
``job`` table's ``parametric_study_id`` / ``workflow_run_id`` FKs
resolve in the shared MetaData.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import AuthSettings, DbSettings
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.hierarchy import make_project_service, make_scenario_service
from matofx.studies import orm as _studies_orm  # noqa: F401  -- registers ParametricStudy
from matofx.studies.optimisation import (
    orm as _opt_orm,  # noqa: F401  -- registers OptimisationStudy
)
from matofx.users import make_password_hasher, make_user_service
from matofx.workflows import orm as _workflow_orm  # noqa: F401  -- registers WorkflowRun
from tests._migrated_db import postgres_db_settings, sqlite_db_settings


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def studies_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    if request.param == "sqlite":
        # A byte copy of the session-scoped migrated template —
        # milliseconds, where migrating per test cost ~30 s.
        yield sqlite_db_settings(migrated_template, tmp_path, "s.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def studies_engine(studies_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(studies_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def _auth_settings() -> AuthSettings:
    return AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )


class Basics:
    """Prerequisite FK parents for inserting studies / jobs directly."""

    def __init__(
        self,
        *,
        user_id: int,
        project_id: int,
        scenario_id: int,
        tool_alias: str,
        tool_version_id: int,
    ) -> None:
        self.user_id = user_id
        self.project_id = project_id
        self.scenario_id = scenario_id
        self.tool_alias = tool_alias
        self.tool_version_id = tool_version_id


@pytest.fixture
async def basics(
    studies_engine: AsyncEngine, _auth_settings: AuthSettings, tmp_path: Path
) -> Basics:
    """Seed one user, project, scenario, tool + tool_version snapshot.

    Returns their ids so constraint tests can construct ``Job`` /
    ``ParametricStudy`` ORM rows directly (the FKs are enforced).

    The bundle directory is **real**. Queueing a job checks the captured
    snapshot's bundle exists on disk, so a made-up path was fine while nothing
    here queued anything and became a wall the moment the coverage path started
    submitting the study it commissions (ADR-0020 §11.5).
    """
    bundle = tmp_path / "bundle" / "demo"
    bundle.mkdir(parents=True, exist_ok=True)
    mount = tmp_path / "mount"
    mount.mkdir(parents=True, exist_ok=True)
    from matofx.tools.descriptor import (  # noqa: PLC0415
        InputField,
        OutputField,
        ToolDescriptor,
    )
    from matofx.tools.exec_orm import StorageLocation  # noqa: PLC0415
    from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415

    audit = AuditWriter(signing_key=None, key_id=None)
    user_service = make_user_service(
        _auth_settings, audit, hasher=make_password_hasher(_auth_settings)
    )
    project_service = make_project_service(audit)
    scenario_service = make_scenario_service(audit)

    factory = make_session_factory(studies_engine)
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
        project = await project_service.create(session, actor_user_id=user.id, name="P1")
        scenario = await scenario_service.create(
            session, actor_user_id=user.id, project_id=project.id, name="S1"
        )
        # A **valid** descriptor, not a stub. It used to be `{"alias": "demo"}`,
        # which is enough for anything that only reads the alias and useless to
        # anything that parses it — so a study built on this tool could be
        # created but never generated. Now that the coverage path submits the
        # study it commissions (ADR-0020 §11.5), the generator parses this
        # snapshot, and a fixture that cannot be parsed hides the very step
        # under test.
        descriptor = ToolDescriptor(
            alias="demo",
            name="Demo",
            version="1.0",
            description="a seeded tool for study and cohort tests",
            main_script="static/main.py",
            inputs=[
                InputField(
                    name=name,
                    label=name.replace("_", " ").title(),
                    type="float",
                    minimum=0.0,
                    maximum=100.0,
                )
                for name in ("thermal_conductivity", "thickness", "heat_load", "width")
            ]
            + [
                # A non-numeric input, so a test can pin one and check it is
                # carried as a constant rather than becoming a column.
                InputField(
                    name="mesh_density",
                    label="Mesh density",
                    type="select",
                    options=["coarse", "medium", "fine"],
                    default="medium",
                )
            ],
            outputs=[OutputField(name="peak_temperature", label="Peak temperature", type="number")],
        ).model_dump(mode="json")
        session.add(
            Tool(
                alias="demo",
                name="Demo",
                version="1.0",
                state="released",
                descriptor=descriptor,
                bundle_path=str(bundle),
                installed_by_user_id=user.id,
            )
        )
        # Queueing needs somewhere to put the job's working directory. Without
        # an active location the fan-out refuses, which nothing here noticed
        # while nothing here queued.
        session.add(StorageLocation(name="studies-local", mount_path=str(mount)))
        tv = ToolVersion(
            alias="demo",
            version="1.0",
            descriptor_snapshot=descriptor,
            bundle_path=str(bundle),
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=user.id,
            change_summary="seed",
            changes={},
        )
        session.add(tv)
        await session.flush()
        return Basics(
            user_id=user.id,
            project_id=project.id,
            scenario_id=scenario.id,
            tool_alias="demo",
            tool_version_id=tv.id,
        )


def study_descriptor() -> Any:
    """A real :class:`ToolDescriptor` for StudyService.submit tests.

    Two numeric inputs ``a`` / ``b`` with a SumCheck ``a + b == 100``
    so tests can exercise the cross-field skip path and the
    no-valid-points refusal.
    """
    from matofx.tools.descriptor import (  # noqa: PLC0415
        InputField,
        SumCheck,
        ToolDescriptor,
    )

    return ToolDescriptor(
        alias="sweep",
        name="Sweep Tool",
        version="2.0.0",
        description="study submit test",
        main_script="static/main.py",
        inputs=[
            InputField(
                name="a",
                label="A pct",
                type="float",
                sum_check=SumCheck(value=100.0, sum_parameters=["a", "b"]),
            ),
            InputField(name="b", label="B pct", type="float"),
        ],
    )


@pytest.fixture
async def valid_tool(studies_engine: AsyncEngine, basics: Basics) -> str:
    """Seed a ``sweep`` tool whose tool_version snapshot is a real,
    parseable descriptor (slice-1's ``demo`` snapshot is a stub).

    Returns the alias; ``basics`` carries the user / project ids.
    """
    from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415

    descriptor = study_descriptor()
    snapshot = descriptor.model_dump()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        session.add(
            Tool(
                alias="sweep",
                name="Sweep Tool",
                version="2.0.0",
                state="released",
                descriptor=snapshot,
                bundle_path="bundle/sweep",
                installed_by_user_id=basics.user_id,
            )
        )
        session.add(
            ToolVersion(
                alias="sweep",
                version="2.0.0",
                descriptor_snapshot=snapshot,
                bundle_path="bundle/sweep",
                created_at=_dt.datetime.now(tz=_dt.UTC),
                created_by_user_id=basics.user_id,
                change_summary="seed",
                changes={},
            )
        )
        await session.flush()
    return "sweep"


async def seed_gated_tool(
    studies_engine: AsyncEngine,
    basics: Basics,
    *,
    alias: str,
    policy: str,
    approver: str,
) -> int:
    """Seed an approval-gated tool (descriptor ``approvals_policy`` =
    ``policy``, one approver). Returns the tool_version id.

    Same a/b SumCheck inputs as :func:`study_descriptor` so the §7
    tests can drive a real (deterministic) generate.
    """
    from matofx.tools.descriptor import (  # noqa: PLC0415
        InputField,
        SumCheck,
        ToolDescriptor,
    )
    from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415

    descriptor = ToolDescriptor(
        alias=alias,
        name=alias,
        version="1.0.0",
        description="gated study tool",
        main_script="static/main.py",
        inputs=[
            InputField(
                name="a",
                label="A pct",
                type="float",
                sum_check=SumCheck(value=100.0, sum_parameters=["a", "b"]),
            ),
            InputField(name="b", label="B pct", type="float"),
        ],
        approvals_policy=policy,  # type: ignore[arg-type]
        approver_usernames=[approver] if policy != "none" else [],
    )
    snapshot = descriptor.model_dump()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        session.add(
            Tool(
                alias=alias,
                name=alias,
                version="1.0.0",
                state="released",
                descriptor=snapshot,
                bundle_path=f"bundle/{alias}",
                installed_by_user_id=basics.user_id,
            )
        )
        tv = ToolVersion(
            alias=alias,
            version="1.0.0",
            descriptor_snapshot=snapshot,
            bundle_path=f"bundle/{alias}",
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=basics.user_id,
            change_summary="seed",
            changes={},
        )
        session.add(tv)
        await session.flush()
        return int(tv.id)


@pytest.fixture
async def outsider_user_id(studies_engine: AsyncEngine) -> int:
    """A regular user with no ACL grant on any study's project."""
    audit = AuditWriter(signing_key=None, key_id=None)
    settings = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )
    user_service = make_user_service(settings, audit, hasher=make_password_hasher(settings))
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="nina",
            email="nina@example.com",
            password="ninapassword",
            full_name="Nina",
            role="regular_user",
        )
        return user.id


@pytest.fixture
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Fake ``JobService.queue_for_runner`` for study-submit tests.

    Slice 4's ``StudyService.submit`` / ``retry_child`` enqueue each
    child via ``queue_for_runner`` (tool/bundle/storage validation +
    working-dir staging). That path is exercised by the large
    hierarchy/api/runner job suites; here it's replaced with a recorder
    that just flips the job ``created -> queued`` so the study tests
    pin StudyService's own fan/state contract without standing up real
    staging. Returns the list of queued child job ids.
    """
    from matofx.hierarchy.orm import Job  # noqa: PLC0415

    calls: list[int] = []

    async def _fake(_self, session, *, job_id, **_kw):
        calls.append(job_id)
        job = await session.get(Job, job_id)
        job.state = "queued"
        await session.flush()
        return job

    monkeypatch.setattr("matofx.hierarchy.service.JobService.queue_for_runner", _fake)
    return calls
