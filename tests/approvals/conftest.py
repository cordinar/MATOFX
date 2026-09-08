"""Shared fixtures for matofx.approvals tests.

Self-contained (mirrors tests/hierarchy/conftest.py): migrate a DB to
head so the ``approval_request`` table and the widened
``ck_job_state`` from migration 0031 exist, then seed the FK graph an
:class:`ApprovalRequest` needs — users, a tool + tool_version, and a
real project → scenario → job.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.approvals import ApprovalService, make_approval_service
from matofx.approvals import orm as _approvals_orm  # noqa: F401  -- registers ApprovalRequest
from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import AuthSettings, DbSettings
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.hierarchy import JobService, make_job_service
from matofx.hierarchy.orm import Job, Project, Scenario

# Register every table ApprovalRequest's FK columns point at, so the
# shared mapper resolves them when the suite runs in isolation (the full
# test run gets these via cross-suite imports; alone it needs them here).
from matofx.library import orm as _library_orm  # noqa: F401
from matofx.materials import orm as _materials_orm  # noqa: F401
from matofx.studies.optimisation import orm as _optimisation_orm  # noqa: F401
from matofx.users import UserService, make_password_hasher, make_user_service
from matofx.vv import orm as _vv_orm  # noqa: F401
from matofx.workflows import (
    orm as _workflow_orm,  # noqa: F401  -- registers WorkflowRun for Job's FK
)
from tests._migrated_db import postgres_db_settings, sqlite_db_settings


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def approvals_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    if request.param == "sqlite":
        # Byte copy of the session template (see tests/_migrated_db.py).
        yield sqlite_db_settings(migrated_template, tmp_path, "a.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def approvals_engine(
    approvals_db_settings: DbSettings,
) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(approvals_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def audit_writer() -> AuditWriter:
    return AuditWriter(signing_key=None, key_id=None)


@pytest.fixture
def approval_service(audit_writer: AuditWriter) -> ApprovalService:
    return make_approval_service(audit_writer)


@pytest.fixture
def user_service(audit_writer: AuditWriter) -> UserService:
    auth = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )
    return make_user_service(auth, audit_writer, hasher=make_password_hasher(auth))


@dataclass(frozen=True)
class SeededJob:
    """The FK graph an ApprovalRequest needs, ready to use in a test."""

    factory: object
    admin_id: int
    alice_id: int  # the requestor in tests
    bob_id: int  # an approver in tests
    job_id: int
    tool_alias: str
    tool_version_id: int


@pytest.fixture
async def seeded_job(approvals_engine: AsyncEngine, user_service: UserService) -> SeededJob:
    """Seed admin/alice/bob + a tool + tool_version + project→scenario→job.

    ``alice`` is the job's requestor; ``bob`` is used as an approver.
    """
    from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415

    factory = make_session_factory(approvals_engine)
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
        admin_id, alice_id, bob_id = admin.id, alice.id, bob.id

    descriptor = {"alias": "demo", "approvals_policy": "pre"}
    async with session_scope(factory) as session:
        session.add(
            Tool(
                alias="demo",
                name="Demo",
                version="1.0",
                state="released",
                descriptor=descriptor,
                bundle_path="bundle/demo",
                installed_by_user_id=admin_id,
            )
        )
        tv = ToolVersion(
            alias="demo",
            version="1.0",
            descriptor_snapshot=descriptor,
            bundle_path="bundle/demo",
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=admin_id,
            change_summary="seed",
            changes={},
        )
        session.add(tv)
        await session.flush()
        tool_version_id = tv.id

    async with session_scope(factory) as session:
        project = Project(name="P", creator_user_id=alice_id)
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="S")
        session.add(scenario)
        await session.flush()
        job = Job(
            scenario_id=scenario.id,
            name="run-1",
            tool_alias="demo",
            tool_version_id=tool_version_id,
            state="pending_approval",
            inputs={"foo": "bar"},
            created_by_user_id=alice_id,
        )
        session.add(job)
        await session.flush()
        job_id = job.id

    return SeededJob(
        factory=factory,
        admin_id=admin_id,
        alice_id=alice_id,
        bob_id=bob_id,
        job_id=job_id,
        tool_alias="demo",
        tool_version_id=tool_version_id,
    )


# --- Phase 2: the pre-gate -------------------------------------------------


@dataclass(frozen=True)
class Phase2Env:
    """A migrated DB + JobService + three users + real staging infra.

    ``new_draft`` seeds a fresh tool whose captured ``tool_version``
    snapshot is a *valid* :class:`ToolDescriptor` with the requested
    ``approvals_policy`` / approver list (and a real on-disk bundle),
    then creates a ``created``-state draft job (via
    ``JobService.create``) in its own project+scenario owned by
    ``alice``. An active ``StorageLocation`` is seeded, so a job that
    routes to ``queue_for_runner`` actually stages and reaches
    ``queued`` — the tests assert that real outcome rather than a
    proxy exception.
    """

    factory: object
    job_service: JobService
    admin_id: int
    alice_id: int  # owner / requestor
    bob_id: int  # approver
    _new_draft: object  # async (policy, approvers) -> (job_id, alias)

    async def new_draft(
        self, policy: str, *, approvers: tuple[str, ...] = ("bob",)
    ) -> tuple[int, str]:
        return await self._new_draft(policy, approvers)  # type: ignore[operator]


@pytest.fixture
async def phase2(
    approvals_engine: AsyncEngine,
    user_service: UserService,
    audit_writer: AuditWriter,
    tmp_path: Path,
) -> Phase2Env:
    from itertools import count  # noqa: PLC0415

    from matofx.tools.descriptor import InputField, ToolDescriptor  # noqa: PLC0415
    from matofx.tools.exec_orm import StorageLocation  # noqa: PLC0415
    from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415

    factory = make_session_factory(approvals_engine)
    job_service = make_job_service(audit_writer)
    mount = tmp_path / "mount"
    mount.mkdir()
    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()

    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        session.add(StorageLocation(name="local", mount_path=str(mount)))
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
        admin_id, alice_id, bob_id = admin.id, alice.id, bob.id

    counter = count(1)

    async def _new_draft(policy: str, approvers: tuple[str, ...]) -> tuple[int, str]:
        n = next(counter)
        alias = f"demo{n}"
        descriptor = ToolDescriptor(
            alias=alias,
            name=f"Demo {n}",
            version="1.0",
            description="x",
            main_script="m.py",
            inputs=[InputField(name="foo", label="Foo", type="string")],
            approvals_policy=policy,  # type: ignore[arg-type]
            approver_usernames=[] if policy == "none" else list(approvers),
        ).model_dump(mode="json")

        # A real on-disk bundle so queue_for_runner's bundle-dir check
        # passes and staging actually runs (job → queued).
        bundle_dir = bundles_root / alias
        bundle_dir.mkdir()
        (bundle_dir / "m.py").write_text("print('noop')\n", encoding="utf-8")
        bundle_path = str(bundle_dir)

        async with session_scope(factory) as session:
            session.add(
                Tool(
                    alias=alias,
                    name=f"Demo {n}",
                    version="1.0",
                    state="released",
                    descriptor=descriptor,
                    bundle_path=bundle_path,
                    installed_by_user_id=admin_id,
                )
            )
            session.add(
                ToolVersion(
                    alias=alias,
                    version="1.0",
                    descriptor_snapshot=descriptor,
                    bundle_path=bundle_path,
                    created_at=_dt.datetime.now(tz=_dt.UTC),
                    created_by_user_id=admin_id,
                    change_summary="seed",
                    changes={},
                )
            )

        async with session_scope(factory) as session:
            project = Project(name=f"P{n}", creator_user_id=alice_id)
            session.add(project)
            await session.flush()
            scenario = Scenario(project_id=project.id, name="S")
            session.add(scenario)
            await session.flush()
            job = await job_service.create(
                session,
                actor_user_id=alice_id,
                scenario_id=scenario.id,
                name=f"run-{n}",
                tool_alias=alias,
                inputs={"foo": "bar"},
            )
            return job.id, alias

    return Phase2Env(
        factory=factory,
        job_service=job_service,
        admin_id=admin_id,
        alice_id=alice_id,
        bob_id=bob_id,
        _new_draft=_new_draft,
    )
