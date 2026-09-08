"""Integration tests for :class:`RunnerSupervisor`.

These tests stage a real working directory, seed the DB so the
runner can pick the job up, then start the supervisor for a few
seconds and assert the canonical state transitions land.

The runner's pickup/heartbeat intervals are tightened well below
defaults so a clean-exit job finishes in well under the 60 s
pytest timeout.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.runner import RunnerSupervisor
from matofx.tools import (
    InputsDocument,
    LocalTarget,
    stage_working_dir,
)
from matofx.tools.exec_orm import AosHost, StorageLocation
from matofx.tools.orm import Tool, ToolVersion
from matofx.users import UserService, make_password_hasher, make_user_service

# Seed helpers go through the ORM rather than raw INSERTs so the
# datetime columns get the dialect's canonical TIMESTAMP serialisation
# (space-separated). Mixing raw isoformat() strings (T-separated) with
# ORM-written rows breaks SQLite's lexicographic CHECK comparisons.


async def _seed_super_user(factory) -> int:
    auth_settings_kwargs = {
        "argon2_time_cost": 1,
        "argon2_memory_cost_kib": 8192,
        "argon2_parallelism": 1,
        "password_min_length": 8,
    }
    from matofx.config import AuthSettings  # noqa: PLC0415

    auth = AuthSettings(**auth_settings_kwargs)
    audit = AuditWriter(signing_key=None, key_id=None)
    user_service: UserService = make_user_service(auth, audit, hasher=make_password_hasher(auth))
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="root",
            email="root@example.com",
            password="securepassword1",
            full_name="Root User",
            role="super_user",
        )
        return user.id


async def _seed_storage_location(factory, mount_path: Path) -> int:
    async with session_scope(factory) as session:
        loc = StorageLocation(name="local", mount_path=str(mount_path))
        session.add(loc)
        await session.flush()
        return int(loc.id)


async def _seed_hpc_cluster(factory, *, name: str = "cluster") -> int:
    """Seed a minimal HpcCluster row so an HPC-target tool's FK validates."""
    from matofx.tools.exec_orm import HpcCluster  # noqa: PLC0415

    async with session_scope(factory) as session:
        cluster = HpcCluster(
            name=name,
            scheduler="slurm",
            head_node="head.example",
            transport="ssh",
            port=22,
            username="svc",
            secret_ref="ref",
        )
        session.add(cluster)
        await session.flush()
        return int(cluster.id)


async def _seed_aos_host(
    factory,
    *,
    name: str = "aoshost",
    os_name: str = "linux",
    transport: str = "ssh",
    max_concurrency: int = 1,
    cpu_cores: int | None = None,
    memory_mb: int | None = None,
) -> int:
    """Seed an AosHost row with capacity for the per-host gating tests."""
    async with session_scope(factory) as session:
        host = AosHost(
            name=name,
            hostname="host.example",
            os=os_name,
            transport=transport,
            port=22 if transport == "ssh" else 5986,
            username="svc",
            secret_ref="ref",
            max_concurrency=max_concurrency,
            cpu_cores=cpu_cores,
            memory_mb=memory_mb,
        )
        session.add(host)
        await session.flush()
        return int(host.id)


async def _seed_tool(
    factory,
    *,
    alias: str,
    user_id: int,
    execution_target: str = "local",
    requires_gpu: bool = False,
    resource_request: dict | None = None,
    hpc_cluster_id: int | None = None,
    aos_host_id: int | None = None,
) -> int:
    """Seed a Tool row + matching ToolVersion. Returns the tool_version_id.

    Defaults reproduce the original local/CPU fixture; the extra kwargs let
    scheduler-lane tests seed GPU and HPC tools with explicit resource
    requests without each test hand-rolling the ORM.
    """
    import datetime as _dt  # noqa: PLC0415

    descriptor = {"alias": alias, "retain_files": [], "delete_io_files": []}
    bundle_path = "/tmp/x"  # noqa: S108 — fixture-only stub path, never read on disk
    async with session_scope(factory) as session:
        tool = Tool(
            alias=alias,
            name=alias,
            version="1.0.0",
            state="released",
            descriptor=descriptor,
            bundle_path=bundle_path,
            installed_by_user_id=user_id,
            execution_target=execution_target,
            requires_gpu=requires_gpu,
            resource_request=resource_request,
            hpc_cluster_id=hpc_cluster_id,
            aos_host_id=aos_host_id,
        )
        session.add(tool)
        version = ToolVersion(
            alias=alias,
            version="1.0.0",
            descriptor_snapshot=descriptor,
            bundle_path=bundle_path,
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=user_id,
            change_summary="Test fixture seed",
            changes={},
        )
        session.add(version)
        await session.flush()
        return int(version.id)


async def _seed_project_scenario_job(
    factory,
    *,
    user_id: int,
    storage_location_id: int,
    tool_alias: str,
    tool_version_id: int,
    working_dir_relative: str,
) -> int:
    async with session_scope(factory) as session:
        project = Project(name="p", description="", creator_user_id=user_id)
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="s", description="")
        session.add(scenario)
        await session.flush()
        job = Job(
            scenario_id=scenario.id,
            name="j",
            tool_alias=tool_alias,
            tool_version_id=tool_version_id,
            state="queued",
            inputs={},
            created_by_user_id=user_id,
            storage_location_id=storage_location_id,
            working_dir_relative=working_dir_relative,
        )
        session.add(job)
        await session.flush()
        return int(job.id)


async def _wait_for_job_state(
    factory, job_id: int, target_states: tuple[str, ...], timeout: float = 30.0
) -> str | None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with session_scope(factory) as session:
            row = await session.get(Job, job_id)
            if row is not None and row.state in target_states:
                return row.state
        await asyncio.sleep(0.05)
    return None


@pytest.mark.slow
@pytest.mark.asyncio
async def test_supervisor_drives_queued_job_to_complete(
    runner_engine: AsyncEngine,
    runner_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    factory = make_session_factory(runner_engine)

    user_id = await _seed_super_user(factory)

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "demo.py").write_text(
        "import json, pathlib\n"
        "out = pathlib.Path('outputs')\n"
        "out.mkdir(exist_ok=True)\n"
        "(out / 'outputs.json').write_text(\n"
        "    json.dumps({'schema_version': 1, 'tool_alias': 'demo', 'outputs': []})\n"
        ")\n",
        encoding="utf-8",
    )
    mount = tmp_path / "mount"
    mount.mkdir()

    inputs = InputsDocument(job_id=1, tool_alias="demo", tool_version="1.0", inputs=[])
    stage_working_dir(
        mount_path=str(mount),
        working_dir_relative="jobs/job_0001",
        bundle_path=str(bundle),
        inputs_document=inputs,
        interpreter_path=sys.executable,
        main_script_relative="demo.py",
        cancel_grace_seconds=5,
    )

    storage_id = await _seed_storage_location(factory, mount)
    tool_version_id = await _seed_tool(factory, alias="demo", user_id=user_id)
    job_id = await _seed_project_scenario_job(
        factory,
        user_id=user_id,
        storage_location_id=storage_id,
        tool_alias="demo",
        tool_version_id=tool_version_id,
        working_dir_relative="jobs/job_0001",
    )

    supervisor = RunnerSupervisor(
        factory=factory,
        audit=runner_audit,
        pickup_interval_s=0.1,
        cancel_loop_interval_s=0.1,
        settings_reload_interval_s=10.0,
    )
    # Tighten heartbeat polling so the test finishes fast.
    supervisor.settings = supervisor.settings.__class__(
        local_concurrency=2,
        heartbeat_local_aos_interval_s=0.1,
        heartbeat_hpc_running_interval_s=10.0,
        heartbeat_hpc_queued_interval_s=30.0,
        cancel_grace_s=5,
        study_concurrency_cap=10,
    )

    await supervisor.start()
    try:
        final_state = await _wait_for_job_state(factory, job_id, ("complete", "failed"))
        if final_state != "complete":
            async with session_scope(factory) as session:
                row = await session.get(Job, job_id)
                pytest.fail(
                    f"final_state={final_state!r} job.state={row.state if row else None!r} "
                    f"heartbeat_state={row.heartbeat_state if row else None!r} "
                    f"working_dir_relative={row.working_dir_relative if row else None!r} "
                    f"storage_location_id={row.storage_location_id if row else None!r}"
                )
        assert final_state == "complete"
    finally:
        await supervisor.stop()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_supervisor_marks_failed_when_tool_missing(
    runner_engine: AsyncEngine,
    runner_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    # Seed the tool first so the job FK validates, then pull the tool
    # out from under the job. The FK is RESTRICT, so the delete needs
    # PRAGMA foreign_keys = OFF to simulate a corrupted-DB scenario
    # the supervisor's None-guard exists to handle. The PRAGMA only
    # takes effect outside a transaction, so we run it on a raw
    # connection with autocommit.
    tool_version_id = await _seed_tool(factory, alias="ghost", user_id=user_id)
    job_id = await _seed_project_scenario_job(
        factory,
        user_id=user_id,
        storage_location_id=storage_id,
        tool_alias="ghost",
        tool_version_id=tool_version_id,
        working_dir_relative="jobs/missing",
    )
    async with runner_engine.connect() as conn:
        ac = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await ac.execute(sa.text("PRAGMA foreign_keys = OFF"))
        await ac.execute(sa.text("DELETE FROM tool WHERE alias='ghost'"))
        await ac.execute(sa.text("PRAGMA foreign_keys = ON"))

    # Sanity check the delete actually landed.
    async with session_scope(factory) as session:
        cnt = (
            await session.execute(sa.text("SELECT COUNT(*) FROM tool WHERE alias='ghost'"))
        ).scalar_one()
        assert cnt == 0, f"tool ghost still present (count={cnt})"

    supervisor = RunnerSupervisor(
        factory=factory,
        audit=runner_audit,
        pickup_interval_s=0.1,
        cancel_loop_interval_s=0.1,
        settings_reload_interval_s=10.0,
    )
    await supervisor.start()
    try:
        final_state = await _wait_for_job_state(factory, job_id, ("failed",))
        assert final_state == "failed"
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_supervisor_start_twice_raises(
    runner_engine: AsyncEngine, runner_audit: AuditWriter
) -> None:
    factory = make_session_factory(runner_engine)
    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit)
    await supervisor.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            await supervisor.start()
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_supervisor_stop_is_idempotent(
    runner_engine: AsyncEngine, runner_audit: AuditWriter
) -> None:
    factory = make_session_factory(runner_engine)
    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit)
    await supervisor.start()
    await supervisor.stop()
    await supervisor.stop()  # second call is a no-op


@pytest.mark.asyncio
async def test_fail_job_is_noop_when_row_vanished(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    """A job deleted out from under the runner must make ``_fail_job`` a clean no-op.

    A running job's row can vanish mid-poll — e.g. its parent study is removed and
    the ``ON DELETE CASCADE`` on ``job.parametric_study_id`` drops the job row.
    The walltime killer would then flush ``state='failed'`` against a row that is
    gone, raising ``StaleDataError`` and aborting the runner's work cycle. The
    existence guard must instead treat the vanished row as nothing to fail.
    """
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    tool_version_id = await _seed_tool(factory, alias="vanish", user_id=user_id)
    job_id = await _seed_project_scenario_job(
        factory,
        user_id=user_id,
        storage_location_id=storage_id,
        tool_alias="vanish",
        tool_version_id=tool_version_id,
        working_dir_relative="job_vanish",
    )
    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit)
    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        # Simulate the row disappearing mid-poll (external cascade delete).
        await session.delete(job)
        await session.flush()
        # Must not raise, and must not resurrect the row.
        await supervisor._fail_job(session, job, reason="walltime kill")
        assert await session.scalar(sa.select(Job.id).where(Job.id == job_id)) is None


def test_context_for_threads_scheduler_job_id() -> None:
    """`_context_for` populates `TargetContext.scheduler_job_id` from the Job row.

    Phase 1's HPC poll fallback (Slurm reports terminal but no
    launcher heartbeat) needs the cluster-side job id; the
    supervisor reads it off ``Job.scheduler_job_id`` and threads it
    through every poll / cancel call.
    """
    from matofx.runner.supervisor import _internal_context_for  # noqa: PLC0415

    tool = Tool(
        alias="t",
        name="t",
        version="1.0.0",
        state="released",
        descriptor={},
        bundle_path="/x",
        installed_by_user_id=1,
        execution_target="hpc",
        hpc_cluster_id=42,
    )
    job = Job(
        scenario_id=1,
        name="j",
        tool_alias="t",
        state="running",
        inputs={},
        created_by_user_id=1,
        scheduler_job_id="998877",
    )
    ctx = _internal_context_for(tool, job)
    assert ctx.hpc_cluster_id == 42
    assert ctx.aos_host_id is None
    assert ctx.scheduler_job_id == "998877"


def test_context_for_with_no_scheduler_job_id_yet() -> None:
    """Submit-time call: scheduler_job_id is `None` until sbatch returns one."""
    from matofx.runner.supervisor import _internal_context_for  # noqa: PLC0415

    tool = Tool(
        alias="t",
        name="t",
        version="1.0.0",
        state="released",
        descriptor={},
        bundle_path="/x",
        installed_by_user_id=1,
        execution_target="hpc",
        hpc_cluster_id=7,
    )
    job = Job(
        scenario_id=1,
        name="j",
        tool_alias="t",
        state="queued",
        inputs={},
        created_by_user_id=1,
    )
    ctx = _internal_context_for(tool, job)
    assert ctx.scheduler_job_id is None
    assert ctx.hpc_cluster_id == 7


@pytest.mark.asyncio
async def test_supervisor_uses_registered_target_for_pickup(
    runner_engine: AsyncEngine, runner_audit: AuditWriter
) -> None:
    """A custom registered target is consulted instead of the default."""
    factory = make_session_factory(runner_engine)
    captured = {}

    class _RecordingTarget(LocalTarget):
        async def submit(self, *, staged, resource_request):
            captured["called"] = True
            return await super().submit(staged=staged, resource_request=resource_request)

    from matofx.runner import TargetRegistry  # noqa: PLC0415

    registry = TargetRegistry()
    registry.register("local", _RecordingTarget())

    supervisor = RunnerSupervisor(
        factory=factory,
        audit=runner_audit,
        registry=registry,
    )
    assert supervisor.registry.resolve("local").__class__ is _RecordingTarget


# --- Branch coverage: pickup / heartbeat / cancel error paths --------
#
# These exercise the supervisor's helper methods directly with seeded
# Job rows, avoiding the cost of standing the loop tasks up. The aim
# is branch coverage on the failure / cancellation surfaces, not the
# end-to-end happy path (already covered by the earlier @slow tests).


async def _seed_job_with_optional_storage(
    factory,
    *,
    user_id: int,
    storage_location_id: int | None,
    tool_alias: str,
    tool_version_id: int,
    working_dir_relative: str | None = "jobs/job_x",
) -> int:
    from matofx.hierarchy.orm import Project, Scenario  # noqa: PLC0415

    async with session_scope(factory) as session:
        project = Project(name="p", description="", creator_user_id=user_id)
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="s", description="")
        session.add(scenario)
        await session.flush()
        job = Job(
            scenario_id=scenario.id,
            name="j",
            tool_alias=tool_alias,
            tool_version_id=tool_version_id,
            state="queued",
            inputs={},
            created_by_user_id=user_id,
            storage_location_id=storage_location_id,
            working_dir_relative=working_dir_relative,
        )
        session.add(job)
        await session.flush()
        return int(job.id)


@pytest.mark.asyncio
async def test_pickup_fails_job_when_storage_location_missing(
    runner_engine: AsyncEngine, runner_audit: AuditWriter
) -> None:
    """No storage_location_id on the queued row → 'no staged working directory' fail."""
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    tool_version_id = await _seed_tool(factory, alias="demo", user_id=user_id)
    job_id = await _seed_job_with_optional_storage(
        factory,
        user_id=user_id,
        storage_location_id=None,
        tool_alias="demo",
        tool_version_id=tool_version_id,
        working_dir_relative=None,
    )
    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit)

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        await supervisor._pickup_job(session, job)

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.state == "failed"
        assert job.started_at is not None
        assert job.completed_at is not None


@pytest.mark.asyncio
async def test_pickup_fails_job_when_target_submit_raises(
    runner_engine: AsyncEngine,
    runner_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """Target raises during submit → job goes to failed with the error message."""
    from matofx.runner import TargetRegistry  # noqa: PLC0415

    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    tool_version_id = await _seed_tool(factory, alias="demo", user_id=user_id)
    job_id = await _seed_job_with_optional_storage(
        factory,
        user_id=user_id,
        storage_location_id=storage_id,
        tool_alias="demo",
        tool_version_id=tool_version_id,
    )

    class _RaisingTarget(LocalTarget):
        async def submit(self, *, staged, resource_request, context=None):  # type: ignore[no-untyped-def]  # noqa: ARG002
            raise RuntimeError("ssh: connection refused")

    registry = TargetRegistry()
    registry.register("local", _RaisingTarget())
    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit, registry=registry)

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        await supervisor._pickup_job(session, job)

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.state == "failed"


@pytest.mark.asyncio
async def test_heartbeat_running_state_only_updates_heartbeat_fields(
    runner_engine: AsyncEngine,
    runner_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """`heartbeat_state="running"` → record state, don't transition canonical state."""
    from matofx.runner import TargetRegistry  # noqa: PLC0415
    from matofx.tools.exec.base import JobObservation  # noqa: PLC0415

    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    tool_version_id = await _seed_tool(factory, alias="demo", user_id=user_id)
    job_id = await _seed_job_with_optional_storage(
        factory,
        user_id=user_id,
        storage_location_id=storage_id,
        tool_alias="demo",
        tool_version_id=tool_version_id,
    )
    # Move the job to running so _poll_heartbeat_for processes it.
    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.state = "running"
        import datetime as _dt  # noqa: PLC0415

        job.started_at = _dt.datetime.now(tz=_dt.UTC)

    class _RunningTarget(LocalTarget):
        async def poll(self, *, staged, context=None):  # type: ignore[no-untyped-def]  # noqa: ARG002
            return JobObservation(heartbeat_state="running")

    registry = TargetRegistry()
    registry.register("local", _RunningTarget())
    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit, registry=registry)

    from matofx.runner.supervisor import _PollState  # noqa: PLC0415

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        tool = await supervisor._load_tool_for_job(session, job)
        assert tool is not None
        await supervisor._poll_heartbeat_for(session, job, tool=tool, poll_state=_PollState())

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.state == "running"  # unchanged
        assert job.heartbeat_state == "running"
        assert job.heartbeat_last_seen_at is not None


@pytest.mark.asyncio
async def test_cancel_job_full_path_drives_state_to_failed(
    runner_engine: AsyncEngine,
    runner_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """`_cancel_job` calls target.cancel and drives state to failed with audit row."""
    from matofx.runner import TargetRegistry  # noqa: PLC0415

    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    tool_version_id = await _seed_tool(factory, alias="demo", user_id=user_id)
    job_id = await _seed_job_with_optional_storage(
        factory,
        user_id=user_id,
        storage_location_id=storage_id,
        tool_alias="demo",
        tool_version_id=tool_version_id,
    )

    cancel_calls: list[dict[str, object]] = []

    class _CapturingTarget(LocalTarget):
        async def cancel(self, *, submit_result, staged, grace_seconds, context=None):  # type: ignore[no-untyped-def]  # noqa: ARG002
            cancel_calls.append({"grace": grace_seconds, "pid": submit_result.pid})

    registry = TargetRegistry()
    registry.register("local", _CapturingTarget())
    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit, registry=registry)

    # Move job to running and stamp cancel_requested_at.
    import datetime as _dt  # noqa: PLC0415

    now = _dt.datetime.now(tz=_dt.UTC)
    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.state = "running"
        job.started_at = now
        job.cancel_requested_at = now
        job.cancel_requested_by_user_id = user_id

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        await supervisor._cancel_job(session, job)

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.state == "failed"
        assert job.completed_at is not None

    assert len(cancel_calls) == 1
    assert cancel_calls[0]["grace"] == supervisor.settings.cancel_grace_s


@pytest.mark.asyncio
async def test_cancel_job_with_missing_tool_marks_failed(
    runner_engine: AsyncEngine,
    runner_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """Tool deleted before cancel → job marked failed without invoking any target."""
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    tool_version_id = await _seed_tool(factory, alias="ghosted", user_id=user_id)
    job_id = await _seed_job_with_optional_storage(
        factory,
        user_id=user_id,
        storage_location_id=storage_id,
        tool_alias="ghosted",
        tool_version_id=tool_version_id,
    )
    # Force-delete the tool with FK off.
    async with runner_engine.connect() as conn:
        ac = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await ac.execute(sa.text("PRAGMA foreign_keys = OFF"))
        await ac.execute(sa.text("DELETE FROM tool WHERE alias='ghosted'"))
        await ac.execute(sa.text("PRAGMA foreign_keys = ON"))

    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit)

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        await supervisor._cancel_job(session, job)

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.state == "failed"


@pytest.mark.asyncio
async def test_read_pid_from_heartbeat_returns_none_when_absent(tmp_path: Path) -> None:
    """No heartbeat files at all → returns None (cancel falls through gracefully)."""
    from matofx.tools.exec.staging import (  # noqa: PLC0415
        HEARTBEAT_DIRNAME,
        INPUTS_DIRNAME,
        LOGS_DIRNAME,
        OUTPUTS_DIRNAME,
        StagedWorkingDir,
    )

    wd = tmp_path / "wd"
    wd.mkdir()
    for sub in (INPUTS_DIRNAME, OUTPUTS_DIRNAME, LOGS_DIRNAME, HEARTBEAT_DIRNAME):
        (wd / sub).mkdir()
    staged = StagedWorkingDir(
        working_dir=wd,
        inputs_dir=wd / INPUTS_DIRNAME,
        outputs_dir=wd / OUTPUTS_DIRNAME,
        logs_dir=wd / LOGS_DIRNAME,
        heartbeat_dir=wd / HEARTBEAT_DIRNAME,
        inputs_json_path=wd / INPUTS_DIRNAME / "inputs.json",
        launcher_path=wd / "launcher.py",
    )

    # `_read_pid_from_heartbeat` is a pure-IO helper — never touches the
    # session factory — so we can pass a None placeholder safely.
    supervisor = RunnerSupervisor(
        factory=None,  # type: ignore[arg-type]
        audit=AuditWriter(signing_key=None, key_id=None),
    )
    pid = await supervisor._read_pid_from_heartbeat(staged)
    assert pid is None


@pytest.mark.asyncio
async def test_read_pid_from_heartbeat_reads_running_json(tmp_path: Path) -> None:
    """`running.json` with a pid → that pid is returned."""
    import json  # noqa: PLC0415

    from matofx.tools.exec.staging import (  # noqa: PLC0415
        HEARTBEAT_DIRNAME,
        INPUTS_DIRNAME,
        LOGS_DIRNAME,
        OUTPUTS_DIRNAME,
        StagedWorkingDir,
    )

    wd = tmp_path / "wd"
    wd.mkdir()
    for sub in (INPUTS_DIRNAME, OUTPUTS_DIRNAME, LOGS_DIRNAME, HEARTBEAT_DIRNAME):
        (wd / sub).mkdir()
    (wd / HEARTBEAT_DIRNAME / "running.json").write_text(
        json.dumps({"pid": 4242, "state": "running"}), encoding="utf-8"
    )
    staged = StagedWorkingDir(
        working_dir=wd,
        inputs_dir=wd / INPUTS_DIRNAME,
        outputs_dir=wd / OUTPUTS_DIRNAME,
        logs_dir=wd / LOGS_DIRNAME,
        heartbeat_dir=wd / HEARTBEAT_DIRNAME,
        inputs_json_path=wd / INPUTS_DIRNAME / "inputs.json",
        launcher_path=wd / "launcher.py",
    )

    supervisor = RunnerSupervisor(
        factory=None,  # type: ignore[arg-type]
        audit=AuditWriter(signing_key=None, key_id=None),
    )
    pid = await supervisor._read_pid_from_heartbeat(staged)
    assert pid == 4242


# --- P1: heartbeat interval is per-target ----------------------------


def _make_minimal_tool(*, alias: str, target: str = "local") -> Tool:
    return Tool(
        alias=alias,
        name=alias,
        version="1.0.0",
        state="released",
        descriptor={},
        bundle_path="/x",
        installed_by_user_id=1,
        execution_target=target,
        hpc_cluster_id=99 if target == "hpc" else None,
    )


def test_heartbeat_interval_local_uses_local_setting() -> None:
    """**P1**: local jobs use ``heartbeat_local_aos_interval_s``."""
    supervisor = RunnerSupervisor(
        factory=None,  # type: ignore[arg-type]
        audit=AuditWriter(signing_key=None, key_id=None),
    )
    tool = _make_minimal_tool(alias="t", target="local")
    job = Job(
        scenario_id=1,
        name="j",
        tool_alias="t",
        state="running",
        inputs={},
        created_by_user_id=1,
    )
    interval = supervisor._heartbeat_interval_for(tool, job)
    assert interval == supervisor.settings.heartbeat_local_aos_interval_s


def test_heartbeat_interval_hpc_running_uses_hpc_running_setting() -> None:
    """**P1**: HPC jobs with a running heartbeat use the HPC-running interval."""
    supervisor = RunnerSupervisor(
        factory=None,  # type: ignore[arg-type]
        audit=AuditWriter(signing_key=None, key_id=None),
    )
    tool = _make_minimal_tool(alias="t", target="hpc")
    job = Job(
        scenario_id=1,
        name="j",
        tool_alias="t",
        state="running",
        inputs={},
        created_by_user_id=1,
        heartbeat_state="running",
    )
    interval = supervisor._heartbeat_interval_for(tool, job)
    assert interval == supervisor.settings.heartbeat_hpc_running_interval_s


def test_heartbeat_interval_hpc_queued_uses_hpc_queued_setting() -> None:
    """**P1**: HPC jobs without a running heartbeat use the (longer) HPC queued interval."""
    supervisor = RunnerSupervisor(
        factory=None,  # type: ignore[arg-type]
        audit=AuditWriter(signing_key=None, key_id=None),
    )
    tool = _make_minimal_tool(alias="t", target="hpc")
    job = Job(
        scenario_id=1,
        name="j",
        tool_alias="t",
        state="running",
        inputs={},
        created_by_user_id=1,
        heartbeat_state=None,
    )
    interval = supervisor._heartbeat_interval_for(tool, job)
    assert interval == supervisor.settings.heartbeat_hpc_queued_interval_s


# --- B2: bounded retry on missing staging ----------------------------


@pytest.mark.asyncio
async def test_missing_staging_fails_job_after_threshold(
    runner_engine: AsyncEngine,
    runner_audit: AuditWriter,
) -> None:
    """**B2**: after ``_MAX_CONSECUTIVE_MISSING_STAGING_POLLS`` the job is failed.

    Forces the storage_location row to point at an unreachable mount
    via the supervisor's missing-staging path: when storage_location_id
    is NULL on the job, ``_resolve_staged_working_dir`` returns None
    every time. After the threshold, the job should advance to failed
    with the documented "lost contact" reason.
    """
    from matofx.runner.supervisor import (  # noqa: PLC0415
        _MAX_CONSECUTIVE_MISSING_STAGING_POLLS,
        _PollState,
    )

    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    tool_version_id = await _seed_tool(factory, alias="demo", user_id=user_id)
    job_id = await _seed_job_with_optional_storage(
        factory,
        user_id=user_id,
        storage_location_id=None,  # never resolves to a staged dir
        tool_alias="demo",
        tool_version_id=tool_version_id,
        working_dir_relative=None,
    )
    # Move job to running so the heartbeat path applies.
    import datetime as _dt  # noqa: PLC0415

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.state = "running"
        job.started_at = _dt.datetime.now(tz=_dt.UTC)

    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit)
    poll_state = _PollState()

    async with session_scope(factory) as session:
        for _ in range(_MAX_CONSECUTIVE_MISSING_STAGING_POLLS):
            job = await session.get(Job, job_id)
            assert job is not None
            tool = await supervisor._load_tool_for_job(session, job)
            assert tool is not None
            await supervisor._poll_heartbeat_for(session, job, tool=tool, poll_state=poll_state)

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.state == "failed"
        assert job.completed_at is not None


@pytest.mark.asyncio
async def test_missing_staging_resets_counter_on_recovery(
    runner_engine: AsyncEngine,
    runner_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """**B2**: a successful poll between misses resets the counter."""
    from matofx.runner.supervisor import _PollState  # noqa: PLC0415

    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    tool_version_id = await _seed_tool(factory, alias="demo", user_id=user_id)
    job_id = await _seed_job_with_optional_storage(
        factory,
        user_id=user_id,
        storage_location_id=storage_id,
        tool_alias="demo",
        tool_version_id=tool_version_id,
        working_dir_relative="jobs/job_x",
    )
    # Move to running.
    import datetime as _dt  # noqa: PLC0415

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.state = "running"
        job.started_at = _dt.datetime.now(tz=_dt.UTC)

    # Pre-create the staged working dir so resolve succeeds.
    wd = tmp_path / "jobs/job_x"
    wd.mkdir(parents=True)
    for sub in ("inputs", "outputs", "logs", "heartbeat"):
        (wd / sub).mkdir()

    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit)
    poll_state = _PollState()
    poll_state.consecutive_missing_staging = 5  # halfway to the threshold

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        tool = await supervisor._load_tool_for_job(session, job)
        assert tool is not None
        await supervisor._poll_heartbeat_for(session, job, tool=tool, poll_state=poll_state)

    assert poll_state.consecutive_missing_staging == 0


# --- §16.15 post gate at run finalisation ----------------------------


async def _seed_policy_tool(
    factory, *, alias: str, user_id: int, policy: str, approvers: list[str]
) -> int:
    """Seed a Tool + ToolVersion whose snapshot carries a post policy.

    The runner reads the policy off the snapshot as a plain mapping
    (fail-safe), so a minimal dict is enough — no full ToolDescriptor.
    """
    import datetime as _dt  # noqa: PLC0415

    snapshot = {
        "alias": alias,
        "approvals_policy": policy,
        "approver_usernames": approvers,
        "retain_files": [],
        "delete_io_files": [],
    }
    async with session_scope(factory) as session:
        session.add(
            Tool(
                alias=alias,
                name=alias,
                version="1.0.0",
                state="released",
                descriptor=snapshot,
                bundle_path="/tmp/x",  # noqa: S108 — never read on disk here
                installed_by_user_id=user_id,
                execution_target="local",
                requires_gpu=False,
            )
        )
        version = ToolVersion(
            alias=alias,
            version="1.0.0",
            descriptor_snapshot=snapshot,
            bundle_path="/tmp/x",  # noqa: S108
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=user_id,
            change_summary="seed",
            changes={},
        )
        session.add(version)
        await session.flush()
        return int(version.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["post", "both"])
async def test_open_post_approval_holds_and_opens_request(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, policy: str
) -> None:
    from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    tv_id = await _seed_policy_tool(
        factory, alias="demo", user_id=user_id, policy=policy, approvers=["bob"]
    )
    job_id = await _seed_job_with_optional_storage(
        factory,
        user_id=user_id,
        storage_location_id=None,
        tool_alias="demo",
        tool_version_id=tv_id,
    )
    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit)

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        held = await supervisor._open_post_approval_if_required(session, job=job)
        assert held is not None  # returns the created post ApprovalRequest
        assert held.kind == "post"

    async with session_scope(factory) as session:
        rows = list(
            (
                await session.execute(
                    sa.select(ApprovalRequest).where(ApprovalRequest.job_id == job_id)
                )
            ).scalars()
        )
        assert len(rows) == 1
        req = rows[0]
        assert req.kind == "post"
        assert req.status == "pending"
        assert req.eligible_approvers == ["bob"]
        assert req.requested_by_user_id == user_id  # the job's creator


@pytest.mark.asyncio
async def test_open_post_approval_noop_for_non_post_policy(
    runner_engine: AsyncEngine, runner_audit: AuditWriter
) -> None:
    from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    tv_id = await _seed_policy_tool(
        factory, alias="demo", user_id=user_id, policy="none", approvers=[]
    )
    job_id = await _seed_job_with_optional_storage(
        factory,
        user_id=user_id,
        storage_location_id=None,
        tool_alias="demo",
        tool_version_id=tv_id,
    )
    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit)

    async with session_scope(factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        held = await supervisor._open_post_approval_if_required(session, job=job)
        assert held is None  # non-post policy → no request opened

    async with session_scope(factory) as session:
        rows = list(
            (
                await session.execute(
                    sa.select(ApprovalRequest).where(ApprovalRequest.job_id == job_id)
                )
            ).scalars()
        )
        assert rows == []


@pytest.mark.asyncio
async def test_open_post_approval_skips_parametric_study_child(
    runner_engine: AsyncEngine, runner_audit: AuditWriter
) -> None:
    """A study child of a post-policy tool gets **no** per-child gate.

    The parent study owns a single results-release gate (opened by the
    rollup once every child is terminal); a per-child gate would hold each
    child in ``pending_results`` so the parent never sees them terminal and
    its one gate never opens — the N-gates-instead-of-1 bug.
    """
    from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415
    from matofx.studies.orm import ParametricStudy  # noqa: PLC0415

    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    tv_id = await _seed_policy_tool(
        factory, alias="demo", user_id=user_id, policy="both", approvers=["bob"]
    )
    async with session_scope(factory) as session:
        project = Project(name="p", description="", creator_user_id=user_id)
        session.add(project)
        await session.flush()
        study = ParametricStudy(
            project_id=project.id,
            name="sweep",
            tool_alias="demo",
            tool_version_id=tv_id,
            mode="list",
            factor_spec={},
            sampling_params={},
            run_cap=200,
            state="running",
            created_by_user_id=user_id,
        )
        session.add(study)
        await session.flush()
        child = Job(
            scenario_id=None,
            parametric_study_id=study.id,
            name="child",
            tool_alias="demo",
            tool_version_id=tv_id,
            state="running",
            inputs={},
            created_by_user_id=user_id,
            working_dir_relative="jobs/child",
        )
        session.add(child)
        await session.flush()
        child_id = int(child.id)

    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit)
    async with session_scope(factory) as session:
        child = await session.get(Job, child_id)
        assert child is not None
        held = await supervisor._open_post_approval_if_required(session, job=child)
        assert held is None  # study child → no per-child gate

    async with session_scope(factory) as session:
        rows = list(
            (
                await session.execute(
                    sa.select(ApprovalRequest).where(ApprovalRequest.job_id == child_id)
                )
            ).scalars()
        )
        assert rows == []


@pytest.mark.asyncio
async def test_open_post_approval_skips_optimisation_workflow_member(
    runner_engine: AsyncEngine, runner_audit: AuditWriter
) -> None:
    """A workflow-target optimisation's member job gets **no** per-job gate.

    The member runs as part of a headless ``WorkflowRun`` whose
    ``optimisation_study_id`` is set, so the per-job ``optimisation_study_id``
    is ``None`` and the original guard missed it — every post-policy member
    landed in ``pending_results``, stalling the run (and the whole campaign).
    """
    from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415
    from matofx.studies.optimisation.orm import OptimisationStudy  # noqa: PLC0415
    from matofx.workflows.orm import Workflow, WorkflowRun  # noqa: PLC0415

    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    tv_id = await _seed_policy_tool(
        factory, alias="demo", user_id=user_id, policy="both", approvers=["bob"]
    )
    async with session_scope(factory) as session:
        project = Project(name="p", description="", creator_user_id=user_id)
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="s", description="")
        workflow = Workflow(
            alias="chain",
            name="Chain",
            version="1.0.0",
            descriptor={},
            installed_by_user_id=user_id,
        )
        study = OptimisationStudy(
            project_id=project.id,
            name="wf-opt",
            target_kind="workflow",
            workflow_alias="chain",
            state="running",
            created_by_user_id=user_id,
        )
        session.add_all([scenario, workflow, study])
        await session.flush()
        run = WorkflowRun(
            workflow_id=workflow.id,
            workflow_version_snapshot="1.0.0",
            scenario_id=scenario.id,
            name="cand",
            submitted_by_user_id=user_id,
            optimisation_study_id=study.id,
        )
        session.add(run)
        await session.flush()
        member = Job(
            scenario_id=scenario.id,
            workflow_run_id=run.id,  # no optimisation_study_id on the job itself
            name="member",
            tool_alias="demo",
            tool_version_id=tv_id,
            state="running",
            inputs={},
            created_by_user_id=user_id,
            working_dir_relative="jobs/member",
        )
        session.add(member)
        await session.flush()
        member_id = int(member.id)

    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit)
    async with session_scope(factory) as session:
        member = await session.get(Job, member_id)
        assert member is not None
        held = await supervisor._open_post_approval_if_required(session, job=member)
        assert held is None  # optimisation workflow member → no per-member gate

    async with session_scope(factory) as session:
        rows = list(
            (
                await session.execute(
                    sa.select(ApprovalRequest).where(ApprovalRequest.job_id == member_id)
                )
            ).scalars()
        )
        assert rows == []


# --- Parametric-study lane + per-study concurrency cap (slice 2-PS-a/4) ---


class _OkTarget(LocalTarget):
    """Fake target whose ``submit`` hands back a pid without spawning."""

    async def submit(self, *, staged, resource_request, context=None):  # type: ignore[no-untyped-def]  # noqa: ARG002
        from matofx.tools.exec.base import SubmitResult  # noqa: PLC0415

        return SubmitResult(pid=4321, scheduler_job_id=None)


async def _seed_study_and_children(
    factory, *, user_id: int, storage_id: int, tool_version_id: int, n_children: int
):
    from matofx.studies.orm import ParametricStudy  # noqa: PLC0415

    async with session_scope(factory) as session:
        project = Project(name="p", description="", creator_user_id=user_id)
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="s", description="")
        session.add(scenario)
        await session.flush()
        study = ParametricStudy(
            project_id=project.id,
            name="sweep",
            tool_alias="demo",
            tool_version_id=tool_version_id,
            mode="list",
            factor_spec={},
            sampling_params={},
            run_cap=200,
            state="running",
            created_by_user_id=user_id,
        )
        session.add(study)
        await session.flush()
        child_ids: list[int] = []
        for i in range(n_children):
            job = Job(
                scenario_id=None,
                parametric_study_id=study.id,
                name=f"c{i}",
                tool_alias="demo",
                tool_version_id=tool_version_id,
                state="queued",
                inputs={},
                created_by_user_id=user_id,
                storage_location_id=storage_id,
                working_dir_relative=f"jobs/c{i}",
            )
            session.add(job)
            await session.flush()
            child_ids.append(int(job.id))
        solo = Job(
            scenario_id=scenario.id,
            name="solo",
            tool_alias="demo",
            tool_version_id=tool_version_id,
            state="queued",
            inputs={},
            created_by_user_id=user_id,
            storage_location_id=storage_id,
            working_dir_relative="jobs/solo",
        )
        session.add(solo)
        await session.flush()
        return child_ids, int(solo.id)


def _supervisor_with(
    factory,
    runner_audit,
    *,
    local_concurrency: int,
    study_cap: int = 10,
    gpu_concurrency: int = 1,
    local_cpu_cores: int = 4096,
    local_memory_mb: int = 8_388_608,
    extra_targets: dict | None = None,
):
    """Build a supervisor with fake targets + explicit runner settings.

    The CPU / memory budgets default to the catalog ceilings so the
    resource gate never interferes with count-based tests on a small CI
    host; lane / budget tests pass tighter values explicitly.
    """
    from matofx.runner import TargetRegistry  # noqa: PLC0415

    registry = TargetRegistry()
    registry.register("local", _OkTarget())
    for kind, target in (extra_targets or {}).items():
        registry.register(kind, target)
    sup = RunnerSupervisor(factory=factory, audit=runner_audit, registry=registry)
    sup.settings = sup.settings.__class__(
        local_concurrency=local_concurrency,
        heartbeat_local_aos_interval_s=0.1,
        heartbeat_hpc_running_interval_s=10.0,
        heartbeat_hpc_queued_interval_s=30.0,
        cancel_grace_s=5,
        study_concurrency_cap=study_cap,
        gpu_concurrency=gpu_concurrency,
        local_cpu_cores=local_cpu_cores,
        local_memory_mb=local_memory_mb,
    )
    return sup


@pytest.mark.asyncio
async def test_pickup_enforces_per_study_concurrency_cap(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    tvid = await _seed_tool(factory, alias="demo", user_id=user_id)
    child_ids, solo_id = await _seed_study_and_children(
        factory, user_id=user_id, storage_id=storage_id, tool_version_id=tvid, n_children=3
    )
    sup = _supervisor_with(factory, runner_audit, local_concurrency=10, study_cap=2)

    await sup._pickup_once()

    async with session_scope(factory) as session:
        states = {
            jid: (await session.get(Job, jid)).state  # type: ignore[union-attr]
            for jid in [*child_ids, solo_id]
        }
    assert states[solo_id] == "running"  # ordinary job never blocked by the cap
    running = [c for c in child_ids if states[c] == "running"]
    queued = [c for c in child_ids if states[c] == "queued"]
    assert len(running) == 2  # capped at study_concurrency_cap
    assert len(queued) == 1


@pytest.mark.asyncio
async def test_pickup_study_lane_sorts_after_ordinary_jobs(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    tvid = await _seed_tool(factory, alias="demo", user_id=user_id)
    child_ids, solo_id = await _seed_study_and_children(
        factory, user_id=user_id, storage_id=storage_id, tool_version_id=tvid, n_children=1
    )
    # One worker slot only: the ordinary job must win the lane.
    sup = _supervisor_with(factory, runner_audit, local_concurrency=1, study_cap=10)

    await sup._pickup_once()

    async with session_scope(factory) as session:
        solo = await session.get(Job, solo_id)
        child = await session.get(Job, child_ids[0])
    assert solo is not None
    assert child is not None
    assert solo.state == "running"
    assert child.state == "queued"  # study child queued behind the ordinary job


# --- Part A: three-lane scheduler (GPU cap + resource budget + HPC exempt) ---


async def _seed_one_job(
    factory,
    *,
    user_id: int,
    storage_id: int,
    tool_alias: str,
    tool_version_id: int,
    wdr: str,
    state: str = "queued",
) -> int:
    """Seed a single job in its own uniquely-named project + scenario.

    Each job gets its own project (the ``project(creator, name)`` unique
    constraint forbids reusing "p") and its own scenario (the
    ``job(scenario_id, tool_alias)`` unique constraint forbids two jobs
    of the same tool sharing a scenario). ``wdr`` is unique per job, so it
    doubles as the project name.
    """
    import datetime as _dt  # noqa: PLC0415

    async with session_scope(factory) as session:
        project = Project(name=f"proj-{wdr}", description="", creator_user_id=user_id)
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="s", description="")
        session.add(scenario)
        await session.flush()
        job = Job(
            scenario_id=scenario.id,
            name="j",
            tool_alias=tool_alias,
            tool_version_id=tool_version_id,
            state="queued",
            inputs={},
            created_by_user_id=user_id,
            storage_location_id=storage_id,
            working_dir_relative=wdr,
        )
        session.add(job)
        await session.flush()  # stamps created_at
        if state != "queued":
            job.state = state
            if state == "running":
                # Set started_at *after* created_at is stamped so the
                # ``started_at >= created_at`` CHECK holds.
                job.started_at = _dt.datetime.now(tz=_dt.UTC)
            await session.flush()
        return int(job.id)


async def _states(factory, ids: list[int]) -> dict[int, str]:
    async with session_scope(factory) as session:
        return {
            jid: (await session.get(Job, jid)).state  # type: ignore[union-attr]
            for jid in ids
        }


@pytest.mark.asyncio
async def test_pickup_enforces_strict_gpu_concurrency_cap(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    """Two queued local GPU jobs, gpu_concurrency=1 → only one starts."""
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    gpu_tvid = await _seed_tool(factory, alias="gpu", user_id=user_id, requires_gpu=True)
    ids = [
        await _seed_one_job(
            factory,
            user_id=user_id,
            storage_id=storage_id,
            tool_alias="gpu",
            tool_version_id=gpu_tvid,
            wdr=f"jobs/g{i}",
        )
        for i in range(2)
    ]
    # Plenty of local slots — the GPU lane, not the count cap, must bind.
    sup = _supervisor_with(factory, runner_audit, local_concurrency=4, gpu_concurrency=1)

    await sup._pickup_once()

    states = await _states(factory, ids)
    assert sorted(states.values()) == ["queued", "running"]


@pytest.mark.asyncio
async def test_pickup_gpu_job_also_consumes_a_local_slot(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    """local_concurrency=2 with a GPU + two CPU jobs → exactly 2 run, GPU among them.

    A local GPU job consumes a local slot *and* the GPU lane, so the box
    never runs three local processes just because two are CPU-only.
    """
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    gpu_tvid = await _seed_tool(factory, alias="gpu", user_id=user_id, requires_gpu=True)
    cpu_tvid = await _seed_tool(factory, alias="cpu", user_id=user_id)

    gpu_id = await _seed_one_job(
        factory,
        user_id=user_id,
        storage_id=storage_id,
        tool_alias="gpu",
        tool_version_id=gpu_tvid,
        wdr="jobs/gpu",
    )
    cpu_ids = [
        await _seed_one_job(
            factory,
            user_id=user_id,
            storage_id=storage_id,
            tool_alias="cpu",
            tool_version_id=cpu_tvid,
            wdr=f"jobs/c{i}",
        )
        for i in range(2)
    ]
    sup = _supervisor_with(factory, runner_audit, local_concurrency=2, gpu_concurrency=1)

    await sup._pickup_once()

    states = await _states(factory, [gpu_id, *cpu_ids])
    running = [jid for jid, s in states.items() if s == "running"]
    assert len(running) == 2  # local_concurrency honoured across both lanes
    assert states[gpu_id] == "running"  # GPU job got a slot


@pytest.mark.asyncio
async def test_pickup_respects_cpu_budget(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    """Count cap is generous; the CPU-core budget is what limits admission."""
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    tvid = await _seed_tool(
        factory, alias="heavy", user_id=user_id, resource_request={"cores": 2, "memory_mb": 256}
    )
    ids = [
        await _seed_one_job(
            factory,
            user_id=user_id,
            storage_id=storage_id,
            tool_alias="heavy",
            tool_version_id=tvid,
            wdr=f"jobs/h{i}",
        )
        for i in range(3)
    ]
    # 4-core budget, 2 cores each → 2 fit, third blocked. Count cap of 10
    # would otherwise let all three run.
    sup = _supervisor_with(
        factory, runner_audit, local_concurrency=10, local_cpu_cores=4, local_memory_mb=100_000
    )

    await sup._pickup_once()

    states = await _states(factory, ids)
    assert sorted(states.values()) == ["queued", "running", "running"]


@pytest.mark.asyncio
async def test_pickup_respects_memory_budget(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    """The RAM budget limits admission independently of cores."""
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    tvid = await _seed_tool(
        factory, alias="ram", user_id=user_id, resource_request={"cores": 1, "memory_mb": 4000}
    )
    ids = [
        await _seed_one_job(
            factory,
            user_id=user_id,
            storage_id=storage_id,
            tool_alias="ram",
            tool_version_id=tvid,
            wdr=f"jobs/r{i}",
        )
        for i in range(3)
    ]
    # 9000 MB budget, 4000 each → 2 fit (8000), third would need 12000.
    sup = _supervisor_with(
        factory, runner_audit, local_concurrency=10, local_cpu_cores=64, local_memory_mb=9000
    )

    await sup._pickup_once()

    states = await _states(factory, ids)
    assert sorted(states.values()) == ["queued", "running", "running"]


@pytest.mark.asyncio
async def test_pickup_fails_job_too_large_for_budget(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    """A job that can't fit even on an idle host is failed, not left queued forever."""
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    tvid = await _seed_tool(
        factory, alias="huge", user_id=user_id, resource_request={"cores": 1, "memory_mb": 100_000}
    )
    jid = await _seed_one_job(
        factory,
        user_id=user_id,
        storage_id=storage_id,
        tool_alias="huge",
        tool_version_id=tvid,
        wdr="jobs/huge",
    )
    sup = _supervisor_with(
        factory, runner_audit, local_concurrency=10, local_cpu_cores=64, local_memory_mb=8000
    )

    await sup._pickup_once()

    states = await _states(factory, [jid])
    assert states[jid] == "failed"


@pytest.mark.asyncio
async def test_pickup_hpc_running_does_not_count_against_local_cap(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    """Running HPC jobs don't consume local slots (the P4 over-count fix).

    With local_concurrency=1 and two HPC jobs already running, a queued
    local job must still start — under the old ``local_concurrency -
    len(running)`` arithmetic it would have been starved.
    """
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    cluster_id = await _seed_hpc_cluster(factory)
    hpc_tvid = await _seed_tool(
        factory,
        alias="hpc",
        user_id=user_id,
        execution_target="hpc",
        hpc_cluster_id=cluster_id,
    )
    for i in range(2):
        await _seed_one_job(
            factory,
            user_id=user_id,
            storage_id=storage_id,
            tool_alias="hpc",
            tool_version_id=hpc_tvid,
            wdr=f"jobs/hpc{i}",
            state="running",
        )
    local_tvid = await _seed_tool(factory, alias="cpu", user_id=user_id)
    local_id = await _seed_one_job(
        factory,
        user_id=user_id,
        storage_id=storage_id,
        tool_alias="cpu",
        tool_version_id=local_tvid,
        wdr="jobs/local",
    )
    sup = _supervisor_with(factory, runner_audit, local_concurrency=1)

    await sup._pickup_once()

    states = await _states(factory, [local_id])
    assert states[local_id] == "running"


@pytest.mark.asyncio
async def test_pickup_hpc_exempt_when_local_full(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    """HPC jobs are handed off even when every local slot is occupied."""
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)

    # Fill the single local slot with a running local job.
    local_tvid = await _seed_tool(factory, alias="cpu", user_id=user_id)
    await _seed_one_job(
        factory,
        user_id=user_id,
        storage_id=storage_id,
        tool_alias="cpu",
        tool_version_id=local_tvid,
        wdr="jobs/busy",
        state="running",
    )

    cluster_id = await _seed_hpc_cluster(factory)
    hpc_tvid = await _seed_tool(
        factory,
        alias="hpc",
        user_id=user_id,
        execution_target="hpc",
        hpc_cluster_id=cluster_id,
    )
    hpc_ids = [
        await _seed_one_job(
            factory,
            user_id=user_id,
            storage_id=storage_id,
            tool_alias="hpc",
            tool_version_id=hpc_tvid,
            wdr=f"jobs/hpc{i}",
        )
        for i in range(2)
    ]
    # Register a fake HPC target so the hand-off resolves and "runs".
    sup = _supervisor_with(
        factory,
        runner_audit,
        local_concurrency=1,
        extra_targets={"hpc": _OkTarget()},
    )

    await sup._pickup_once()

    states = await _states(factory, hpc_ids)
    assert all(s == "running" for s in states.values())


# --- Part B: AOS per-host gating -------------------------------------


@pytest.mark.asyncio
async def test_aos_per_host_concurrency_cap(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    """An AOS host with max_concurrency=1 runs one job at a time, even though
    AOS is exempt from the local lanes."""
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    host_id = await _seed_aos_host(factory, max_concurrency=1)
    tvid = await _seed_tool(
        factory, alias="aos", user_id=user_id, execution_target="aos", aos_host_id=host_id
    )
    ids = [
        await _seed_one_job(
            factory,
            user_id=user_id,
            storage_id=storage_id,
            tool_alias="aos",
            tool_version_id=tvid,
            wdr=f"jobs/a{i}",
        )
        for i in range(2)
    ]
    # Generous local lane; the per-host AOS cap must bind.
    sup = _supervisor_with(
        factory, runner_audit, local_concurrency=10, extra_targets={"aos": _OkTarget()}
    )

    await sup._pickup_once()

    states = await _states(factory, ids)
    assert sorted(states.values()) == ["queued", "running"]


@pytest.mark.asyncio
async def test_aos_per_host_resource_budget(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    """A host CPU budget limits admission independently of the count cap."""
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    host_id = await _seed_aos_host(factory, max_concurrency=10, cpu_cores=4)
    tvid = await _seed_tool(
        factory,
        alias="aos",
        user_id=user_id,
        execution_target="aos",
        aos_host_id=host_id,
        resource_request={"cores": 2, "memory_mb": 128},
    )
    ids = [
        await _seed_one_job(
            factory,
            user_id=user_id,
            storage_id=storage_id,
            tool_alias="aos",
            tool_version_id=tvid,
            wdr=f"jobs/a{i}",
        )
        for i in range(3)
    ]
    sup = _supervisor_with(
        factory, runner_audit, local_concurrency=10, extra_targets={"aos": _OkTarget()}
    )

    await sup._pickup_once()

    states = await _states(factory, ids)
    # 4-core budget, 2 cores each → 2 fit, third blocked.
    assert sorted(states.values()) == ["queued", "running", "running"]


@pytest.mark.asyncio
async def test_aos_hosts_are_independent(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    """Two hosts each at max_concurrency=1 run one job each — caps don't pool."""
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    host_a = await _seed_aos_host(factory, name="hostA", max_concurrency=1)
    host_b = await _seed_aos_host(factory, name="hostB", max_concurrency=1)
    tv_a = await _seed_tool(
        factory, alias="aosA", user_id=user_id, execution_target="aos", aos_host_id=host_a
    )
    tv_b = await _seed_tool(
        factory, alias="aosB", user_id=user_id, execution_target="aos", aos_host_id=host_b
    )
    id_a = await _seed_one_job(
        factory,
        user_id=user_id,
        storage_id=storage_id,
        tool_alias="aosA",
        tool_version_id=tv_a,
        wdr="jobs/a",
    )
    id_b = await _seed_one_job(
        factory,
        user_id=user_id,
        storage_id=storage_id,
        tool_alias="aosB",
        tool_version_id=tv_b,
        wdr="jobs/b",
    )
    sup = _supervisor_with(
        factory, runner_audit, local_concurrency=10, extra_targets={"aos": _OkTarget()}
    )

    await sup._pickup_once()

    states = await _states(factory, [id_a, id_b])
    assert states[id_a] == "running"
    assert states[id_b] == "running"


@pytest.mark.asyncio
async def test_aos_job_too_large_for_host_budget_fails(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    """A job exceeding the host's set budget is failed, not left queued forever."""
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    host_id = await _seed_aos_host(factory, max_concurrency=10, cpu_cores=2)
    tvid = await _seed_tool(
        factory,
        alias="aos",
        user_id=user_id,
        execution_target="aos",
        aos_host_id=host_id,
        resource_request={"cores": 4, "memory_mb": 128},
    )
    jid = await _seed_one_job(
        factory,
        user_id=user_id,
        storage_id=storage_id,
        tool_alias="aos",
        tool_version_id=tvid,
        wdr="jobs/big",
    )
    sup = _supervisor_with(
        factory, runner_audit, local_concurrency=10, extra_targets={"aos": _OkTarget()}
    )

    await sup._pickup_once()

    states = await _states(factory, [jid])
    assert states[jid] == "failed"


@pytest.mark.asyncio
async def test_aos_host_full_does_not_block_local(
    runner_engine: AsyncEngine, runner_audit: AuditWriter, tmp_path: Path
) -> None:
    """A saturated AOS host doesn't stop local jobs (separate lanes)."""
    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    host_id = await _seed_aos_host(factory, max_concurrency=1)
    aos_tv = await _seed_tool(
        factory, alias="aos", user_id=user_id, execution_target="aos", aos_host_id=host_id
    )
    # One AOS job already running on the single-slot host.
    await _seed_one_job(
        factory,
        user_id=user_id,
        storage_id=storage_id,
        tool_alias="aos",
        tool_version_id=aos_tv,
        wdr="jobs/aos-busy",
        state="running",
    )
    aos_queued = await _seed_one_job(
        factory,
        user_id=user_id,
        storage_id=storage_id,
        tool_alias="aos",
        tool_version_id=aos_tv,
        wdr="jobs/aos-wait",
    )
    local_tv = await _seed_tool(factory, alias="cpu", user_id=user_id)
    local_id = await _seed_one_job(
        factory,
        user_id=user_id,
        storage_id=storage_id,
        tool_alias="cpu",
        tool_version_id=local_tv,
        wdr="jobs/local",
    )
    sup = _supervisor_with(
        factory, runner_audit, local_concurrency=2, extra_targets={"aos": _OkTarget()}
    )

    await sup._pickup_once()

    states = await _states(factory, [aos_queued, local_id])
    assert states[aos_queued] == "queued"  # host full
    assert states[local_id] == "running"  # local lane unaffected


@pytest.mark.asyncio
async def test_one_poisoned_heartbeat_poll_does_not_take_the_cycle_down(
    runner_engine: AsyncEngine,
    runner_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """A failing poll must cost that job its turn, nothing more.

    The savepoint rollback expires every attribute on the job it isolated, so
    the handler that logs the failure must not read one back off the instance:
    the lazy refresh is sync IO on the async path and raises MissingGreenlet
    *out of* the except clause, aborting the whole cycle and starving every
    sibling job of its heartbeat. Seen in the field behind a transient SQLite
    "database is locked" on the heartbeat UPDATE.
    """
    import datetime as _dt  # noqa: PLC0415

    from matofx.runner import TargetRegistry  # noqa: PLC0415
    from matofx.tools.exec.base import JobObservation  # noqa: PLC0415

    factory = make_session_factory(runner_engine)
    user_id = await _seed_super_user(factory)
    storage_id = await _seed_storage_location(factory, tmp_path)
    tool_version_id = await _seed_tool(factory, alias="demo", user_id=user_id)
    job_ids: list[int] = []
    async with session_scope(factory) as session:
        for n in range(2):
            project = Project(name=f"p{n}", description="", creator_user_id=user_id)
            session.add(project)
            await session.flush()
            scenario = Scenario(project_id=project.id, name=f"s{n}", description="")
            session.add(scenario)
            await session.flush()
            job = Job(
                scenario_id=scenario.id,
                name=f"j{n}",
                tool_alias="demo",
                tool_version_id=tool_version_id,
                state="queued",
                inputs={},
                created_by_user_id=user_id,
                storage_location_id=storage_id,
                working_dir_relative=f"jobs/job_{n}",
            )
            session.add(job)
            await session.flush()
            job_ids.append(job.id)
    poisoned, healthy = sorted(job_ids)
    async with session_scope(factory) as session:
        for job_id in job_ids:
            job = await session.get(Job, job_id)
            assert job is not None
            job.state = "running"
            job.started_at = _dt.datetime.now(tz=_dt.UTC)

    class _FlakyTarget(LocalTarget):
        async def poll(self, *, staged, context=None):  # type: ignore[no-untyped-def]  # noqa: ARG002
            return JobObservation(heartbeat_state="running")

    registry = TargetRegistry()
    registry.register("local", _FlakyTarget())
    supervisor = RunnerSupervisor(factory=factory, audit=runner_audit, registry=registry)

    real_poll = supervisor._poll_heartbeat_for

    async def _poll(session, job, *, tool, poll_state):  # type: ignore[no-untyped-def]
        # Dirty the row first, so the savepoint has something to roll back and
        # the instance comes out of it expired — the shape of the real failure.
        job.heartbeat_state = "running"
        if job.id == poisoned:
            raise RuntimeError("poll blew up")
        await real_poll(session, job, tool=tool, poll_state=poll_state)

    supervisor._poll_heartbeat_for = _poll  # type: ignore[method-assign]

    await supervisor._heartbeat_once()

    async with session_scope(factory) as session:
        good = await session.get(Job, healthy)
        bad = await session.get(Job, poisoned)
        assert good is not None
        assert bad is not None
        # The sibling was still polled...
        assert good.heartbeat_last_seen_at is not None
        # ...and the poisoned job's write was rolled back with its savepoint.
        assert bad.heartbeat_last_seen_at is None
