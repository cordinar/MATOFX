"""Tests for :class:`SlurmHpcTarget` with asyncssh mocked."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncssh
import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.config import DbSettings
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.secrets import InMemorySecretsStore
from matofx.tools.exec import (
    HEARTBEAT_DIRNAME,
    INPUTS_DIRNAME,
    LOGS_DIRNAME,
    OUTPUTS_DIRNAME,
    HpcTargetError,
    ResourceRequest,
    SlurmHpcTarget,
    StagedWorkingDir,
    SubmitResult,
    TargetContext,
)
from tests._migrated_db import sqlite_db_settings

# --- alembic-fed engine ----------------------------------------------


def _alembic_config(url: str) -> Config:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "src" / "matofx" / "db" / "migrations"))
    cfg.cmd_opts = type("Args", (), {"x": [f"url={url}"]})()
    return cfg


@pytest.fixture
def db_settings(tmp_path: Path, migrated_template: Path) -> DbSettings:
    # Byte copy of the session-scoped migrated template rather than
    # running 90 migrations per test (see tests/_migrated_db.py).
    return sqlite_db_settings(migrated_template, tmp_path, "hpc.db")


@pytest.fixture
async def engine(db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(db_settings)
    try:
        yield eng
    finally:
        await eng.dispose()


# --- Slurm cluster seed ----------------------------------------------


async def _seed_cluster(
    factory,
    *,
    name: str = "slurm-1",
    head_node: str = "headnode.invalid",
    secret_ref: str = "slurmkey",  # noqa: S107 — fixture identifier
    is_active: bool = True,
    scheduler: str = "slurm",
    transport: str = "ssh",
    port: int = 22,
    default_queue: str | None = None,
    account: str | None = None,
    wckey: str | None = None,
) -> int:
    now = _dt.datetime.now(tz=_dt.UTC)
    async with session_scope(factory) as session:
        await session.execute(
            sa.text(
                "INSERT INTO hpc_cluster "
                "(name, scheduler, head_node, transport, port, username, secret_ref, "
                "default_queue, account, wckey, is_active, row_version, created_at, updated_at) "
                "VALUES (:n, :s, :h, :t, :p, 'matofx', :sr, :q, :acct, :wck, :a, 1, :ts, :ts)"
            ),
            {
                "n": name,
                "s": scheduler,
                "h": head_node,
                "t": transport,
                "p": port,
                "sr": secret_ref,
                "q": default_queue,
                "acct": account,
                "wck": wckey,
                "a": 1 if is_active else 0,
                "ts": now,
            },
        )
        cluster_id = (
            await session.execute(
                sa.text("SELECT id FROM hpc_cluster WHERE name = :n"), {"n": name}
            )
        ).scalar_one()
        return int(cluster_id)


# --- fake asyncssh ---------------------------------------------------


@dataclass
class _FakeProcess:
    exit_status: int | None = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _FakeConn:
    commands: list[str] = field(default_factory=list)
    responses: list[_FakeProcess] = field(default_factory=list)

    async def run(self, command: str, *, check: bool = True) -> _FakeProcess:  # noqa: ARG002
        self.commands.append(command)
        if self.responses:
            return self.responses.pop(0)
        return _FakeProcess()


@dataclass
class _Recorder:
    conn: _FakeConn = field(default_factory=_FakeConn)
    connect_kwargs: list[dict[str, Any]] = field(default_factory=list)


def _patch_connect(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    @asynccontextmanager
    async def fake_connect(**kwargs: Any):
        recorder.connect_kwargs.append(kwargs)
        yield recorder.conn

    monkeypatch.setattr(asyncssh, "connect", fake_connect)


# --- helpers ---------------------------------------------------------


def _make_staged(tmp_path: Path) -> StagedWorkingDir:
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / INPUTS_DIRNAME).mkdir()
    (wd / OUTPUTS_DIRNAME).mkdir()
    (wd / LOGS_DIRNAME).mkdir()
    (wd / HEARTBEAT_DIRNAME).mkdir()
    return StagedWorkingDir(
        working_dir=wd,
        inputs_dir=wd / INPUTS_DIRNAME,
        outputs_dir=wd / OUTPUTS_DIRNAME,
        logs_dir=wd / LOGS_DIRNAME,
        heartbeat_dir=wd / HEARTBEAT_DIRNAME,
        inputs_json_path=wd / INPUTS_DIRNAME / "inputs.json",
        launcher_path=wd / "launcher.py",
    )


def _make_target(*, engine: AsyncEngine) -> SlurmHpcTarget:
    factory = make_session_factory(engine)
    store = InMemorySecretsStore({"slurmkey": b"-----BEGIN PRIVATE KEY-----\n"})
    return SlurmHpcTarget(session_factory=factory, secrets_store=store)


# --- tests -----------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_runs_sbatch_and_parses_jobid(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder()
    rec.conn.responses.append(_FakeProcess(exit_status=0, stdout="Submitted batch job 998877\n"))
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    result = await target.submit(
        staged=staged,
        resource_request=ResourceRequest(cpu_cores=8, memory_mb=4096, walltime_minutes=120),
        context=TargetContext(hpc_cluster_id=cluster_id),
    )
    assert result.scheduler_job_id == "998877"
    assert result.pid is None

    cmd = rec.conn.commands[0]
    assert cmd.startswith("sbatch")
    assert "--cpus-per-task=8" in cmd
    assert "--mem=4096M" in cmd
    assert "--time=120" in cmd
    assert "--wrap=" in cmd
    # A cluster that doesn't do accounting gets no accounting flags, and
    # a cluster without partitions gets no --partition (P-02): the
    # customer this was built for has neither.
    assert "--partition" not in cmd
    assert "--account" not in cmd
    assert "--wckey" not in cmd
    # We rely on the human "Submitted batch job N" form to read the id.
    assert "--parsable" not in cmd


@pytest.mark.asyncio
async def test_submit_emits_wckey_and_account(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both accounting identities reach ``sbatch`` when the cluster sets them.

    Slurm's ``--account`` and ``--wckey`` are separate identities, not
    aliases, so neither substitutes for the other and a site may set
    both (P-01 / D-01).
    """
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory, account="cfd-team", wckey="matofx")
    rec = _Recorder()
    rec.conn.responses.append(_FakeProcess(exit_status=0, stdout="Submitted batch job 5"))
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    await target.submit(
        staged=_make_staged(tmp_path),
        resource_request=ResourceRequest(),
        context=TargetContext(hpc_cluster_id=cluster_id),
    )
    cmd = rec.conn.commands[0]
    assert "--account=cfd-team" in cmd
    assert "--wckey=matofx" in cmd


@pytest.mark.asyncio
async def test_submit_wckey_only_omits_account(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The customer's shape: a work key and no account, and no partition."""
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory, wckey="matofx")
    rec = _Recorder()
    rec.conn.responses.append(_FakeProcess(exit_status=0, stdout="Submitted batch job 6"))
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    await target.submit(
        staged=_make_staged(tmp_path),
        resource_request=ResourceRequest(),
        context=TargetContext(hpc_cluster_id=cluster_id),
    )
    cmd = rec.conn.commands[0]
    assert "--wckey=matofx" in cmd
    assert "--account" not in cmd
    assert "--partition" not in cmd


@pytest.mark.asyncio
async def test_submit_quotes_accounting_values(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Accounting values are shell-quoted — they reach the head node over SSH."""
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory, wckey="proj; rm -rf /")
    rec = _Recorder()
    rec.conn.responses.append(_FakeProcess(exit_status=0, stdout="Submitted batch job 7"))
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    await target.submit(
        staged=_make_staged(tmp_path),
        resource_request=ResourceRequest(),
        context=TargetContext(hpc_cluster_id=cluster_id),
    )
    cmd = rec.conn.commands[0]
    assert "--wckey='proj; rm -rf /'" in cmd


@pytest.mark.asyncio
async def test_submit_default_queue_added(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory, default_queue="priority")
    rec = _Recorder()
    rec.conn.responses.append(_FakeProcess(exit_status=0, stdout="Submitted batch job 1"))
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    await target.submit(
        staged=staged,
        resource_request=ResourceRequest(),
        context=TargetContext(hpc_cluster_id=cluster_id),
    )
    cmd = rec.conn.commands[0]
    assert "--partition=priority" in cmd


@pytest.mark.asyncio
async def test_submit_queue_override_wins(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory, default_queue="default-q")
    rec = _Recorder()
    rec.conn.responses.append(_FakeProcess(exit_status=0, stdout="Submitted batch job 1"))
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    await target.submit(
        staged=staged,
        resource_request=ResourceRequest(queue_override="gpu-q"),
        context=TargetContext(hpc_cluster_id=cluster_id),
    )
    assert "--partition=gpu-q" in rec.conn.commands[0]


@pytest.mark.asyncio
async def test_submit_no_jobid_in_stdout_raises(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder()
    rec.conn.responses.append(_FakeProcess(exit_status=0, stdout="garbage"))
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(HpcTargetError, match="didn't return a job id"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(hpc_cluster_id=cluster_id),
        )


@pytest.mark.asyncio
async def test_submit_missing_cluster_raises(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec = _Recorder()
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(HpcTargetError, match="no longer registered"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(hpc_cluster_id=999),
        )
    assert rec.connect_kwargs == []


@pytest.mark.asyncio
async def test_submit_inactive_cluster_refused(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory, is_active=False)
    rec = _Recorder()
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(HpcTargetError, match="deactivated"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(hpc_cluster_id=cluster_id),
        )


@pytest.mark.asyncio
async def test_submit_mshpc_cluster_refused(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(
        factory,
        name="windows-cluster",
        scheduler="mshpc",
        transport="winrm",
        port=5986,
    )
    rec = _Recorder()
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(HpcTargetError, match="only handles Slurm"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(hpc_cluster_id=cluster_id),
        )


@pytest.mark.asyncio
async def test_submit_without_cluster_id_raises(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec = _Recorder()
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(HpcTargetError, match="linked to an HPC cluster"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(),
        )


@pytest.mark.asyncio
async def test_cancel_runs_scancel(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder()
    rec.conn.responses.append(_FakeProcess(exit_status=0))
    _patch_connect(monkeypatch, rec)

    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    await target.cancel(
        submit_result=SubmitResult(scheduler_job_id="42"),
        staged=staged,
        grace_seconds=10,
        context=TargetContext(hpc_cluster_id=cluster_id),
    )
    assert rec.conn.commands == ["scancel 42"]
    assert sleeps == [10]


@pytest.mark.asyncio
async def test_cancel_with_no_jobid_is_noop(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder()
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    await target.cancel(
        submit_result=SubmitResult(scheduler_job_id=None),
        staged=staged,
        grace_seconds=5,
        context=TargetContext(hpc_cluster_id=cluster_id),
    )
    assert rec.connect_kwargs == []


@pytest.mark.asyncio
async def test_poll_returns_terminal_heartbeat_directly(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    record = {
        "state": "complete",
        "started_at": "2026-05-04T10:00:00+00:00",
        "ended_at": "2026-05-04T10:01:00+00:00",
        "pid": 123,
        "exit_code": 0,
    }
    (staged.heartbeat_dir / "complete.json").write_text(json.dumps(record), encoding="utf-8")
    obs = await target.poll(staged=staged, context=TargetContext(hpc_cluster_id=1))
    assert obs.heartbeat_state == "complete"


@pytest.mark.asyncio
async def test_scheduler_state_squeue_then_sacct(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """squeue empty → fall back to sacct."""
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder()
    rec.conn.responses.extend(
        [
            _FakeProcess(exit_status=0, stdout=""),  # squeue: empty
            _FakeProcess(exit_status=0, stdout="COMPLETED\n"),  # sacct
        ]
    )
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    state = await target._scheduler_state(TargetContext(hpc_cluster_id=cluster_id), "12345")
    assert state == "COMPLETED"
    assert "squeue" in rec.conn.commands[0]
    assert "sacct" in rec.conn.commands[1]


@pytest.mark.asyncio
async def test_scheduler_state_squeue_running(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder()
    rec.conn.responses.append(_FakeProcess(exit_status=0, stdout="RUNNING"))
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    state = await target._scheduler_state(TargetContext(hpc_cluster_id=cluster_id), "1")
    assert state == "RUNNING"
    # Only squeue called; sacct skipped.
    assert len(rec.conn.commands) == 1


@pytest.mark.asyncio
async def test_fetch_logs_returns_shared_mount_paths(engine: AsyncEngine, tmp_path: Path) -> None:
    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    logs = await target.fetch_logs(staged=staged, context=TargetContext(hpc_cluster_id=1))
    assert logs.stdout_path == staged.logs_dir / "stdout.log"
    assert logs.stderr_path == staged.logs_dir / "stderr.log"


@pytest.mark.asyncio
async def test_poll_uses_context_scheduler_job_id_for_terminal_fallback(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Heartbeat absent + scheduler reports COMPLETED → promote to failed.

    Proves the supervisor's `scheduler_job_id` thread-through reaches
    the poll path: without it, poll would have to return heartbeat-only
    and the runner would be stuck waiting for a launcher write that
    never comes.
    """
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder()
    # poll → squeue (empty: queue cleared the job) → sacct (COMPLETED).
    rec.conn.responses.extend(
        [
            _FakeProcess(exit_status=0, stdout=""),
            _FakeProcess(exit_status=0, stdout="COMPLETED\n"),
        ]
    )
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    obs = await target.poll(
        staged=staged,
        context=TargetContext(hpc_cluster_id=cluster_id, scheduler_job_id="555"),
    )
    assert obs.heartbeat_state == "failed"
    assert obs.scheduler_state == "COMPLETED"
    assert obs.failure_reason is not None
    assert "killed by the scheduler" in obs.failure_reason
    assert "555" in rec.conn.commands[0]


@pytest.mark.asyncio
async def test_poll_skips_scheduler_query_when_no_scheduler_job_id(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Submit hasn't returned a jobid yet — poll falls back to heartbeat only.

    No SSH connection should be opened in this case.
    """
    rec = _Recorder()
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    obs = await target.poll(
        staged=staged,
        context=TargetContext(hpc_cluster_id=1, scheduler_job_id=None),
    )
    assert obs.heartbeat_state is None
    assert obs.scheduler_state is None
    assert rec.connect_kwargs == []


@pytest.mark.asyncio
async def test_poll_uses_context_scheduler_job_id_running_passthrough(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scheduler reports RUNNING + heartbeat absent → return heartbeat-only (None)."""
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder()
    rec.conn.responses.append(_FakeProcess(exit_status=0, stdout="RUNNING"))
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    obs = await target.poll(
        staged=staged,
        context=TargetContext(hpc_cluster_id=cluster_id, scheduler_job_id="42"),
    )
    assert obs.heartbeat_state is None
    assert obs.scheduler_state == "RUNNING"
    assert "42" in rec.conn.commands[0]
