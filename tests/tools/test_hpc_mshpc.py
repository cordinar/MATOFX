"""Tests for :class:`MsHpcHpcTarget` with pywinrm mocked."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import winrm
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
    MsHpcHpcTarget,
    ResourceRequest,
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
    return sqlite_db_settings(migrated_template, tmp_path, "mshpc.db")


@pytest.fixture
async def engine(db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(db_settings)
    try:
        yield eng
    finally:
        await eng.dispose()


# --- MS HPC cluster seed ---------------------------------------------


async def _seed_cluster(
    factory,
    *,
    name: str = "mshpc-1",
    head_node: str = "headnode.invalid",
    secret_ref: str = "mshpckey",  # noqa: S107 — fixture identifier
    is_active: bool = True,
    scheduler: str = "mshpc",
    transport: str = "winrm",
    port: int = 5986,
    default_queue: str | None = None,
) -> int:
    now = _dt.datetime.now(tz=_dt.UTC)
    async with session_scope(factory) as session:
        await session.execute(
            sa.text(
                "INSERT INTO hpc_cluster "
                "(name, scheduler, head_node, transport, port, username, secret_ref, "
                "default_queue, is_active, row_version, created_at, updated_at) "
                "VALUES (:n, :s, :h, :t, :p, 'matofx', :sr, :q, :a, 1, :ts, :ts)"
            ),
            {
                "n": name,
                "s": scheduler,
                "h": head_node,
                "t": transport,
                "p": port,
                "sr": secret_ref,
                "q": default_queue,
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


# --- fake winrm ------------------------------------------------------


@dataclass
class _FakeResponse:
    status_code: int = 0
    std_out: bytes = b""
    std_err: bytes = b""


@dataclass
class _FakeSession:
    target_url: str
    auth: tuple[str, str]
    transport: str
    server_cert_validation: str
    operation_timeout_sec: int
    read_timeout_sec: int
    scripts: list[str] = field(default_factory=list)
    responses: list[_FakeResponse] = field(default_factory=list)

    def run_ps(self, script: str) -> _FakeResponse:
        self.scripts.append(script)
        if self.responses:
            return self.responses.pop(0)
        return _FakeResponse()


@dataclass
class _Recorder:
    init_kwargs: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[_FakeSession] = field(default_factory=list)
    responses: list[_FakeResponse] = field(default_factory=list)


def _patch_session(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    def fake_session_init(**kwargs: Any) -> _FakeSession:
        recorder.init_kwargs.append(kwargs)
        sess = _FakeSession(
            target_url=kwargs["target"],
            auth=kwargs["auth"],
            transport=kwargs["transport"],
            server_cert_validation=kwargs["server_cert_validation"],
            operation_timeout_sec=kwargs["operation_timeout_sec"],
            read_timeout_sec=kwargs["read_timeout_sec"],
            responses=list(recorder.responses),
        )
        recorder.sessions.append(sess)
        return sess

    monkeypatch.setattr(winrm, "Session", fake_session_init)


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


def _make_target(*, engine: AsyncEngine) -> MsHpcHpcTarget:
    factory = make_session_factory(engine)
    store = InMemorySecretsStore({"mshpckey": b"hunter2"})
    return MsHpcHpcTarget(session_factory=factory, secrets_store=store)


# --- tests -----------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_runs_powershell_and_parses_jobid(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder(responses=[_FakeResponse(status_code=0, std_out=b"55555\r\n")])
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    result = await target.submit(
        staged=staged,
        resource_request=ResourceRequest(cpu_cores=4, walltime_minutes=90),
        context=TargetContext(hpc_cluster_id=cluster_id),
    )
    assert result.scheduler_job_id == "55555"
    assert result.pid is None

    kwargs = rec.init_kwargs[0]
    assert kwargs["target"] == "https://headnode.invalid:5986/wsman"
    assert kwargs["server_cert_validation"] == "validate"

    script = rec.sessions[0].scripts[0]
    assert "Import-Module Microsoft.HPC" in script
    assert "New-HpcJob" in script
    assert "-NumCores 4" in script
    assert "Add-HpcTask" in script
    assert "Submit-HpcJob" in script


@pytest.mark.asyncio
async def test_submit_runtime_formatted(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder(responses=[_FakeResponse(status_code=0, std_out=b"1")])
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    await target.submit(
        staged=staged,
        resource_request=ResourceRequest(walltime_minutes=125),  # 2:05:00
        context=TargetContext(hpc_cluster_id=cluster_id),
    )
    assert "-RunTime '02:05:00'" in rec.sessions[0].scripts[0]


@pytest.mark.asyncio
async def test_submit_node_groups_from_default_queue(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory, default_queue="GPUNodes")
    rec = _Recorder(responses=[_FakeResponse(status_code=0, std_out=b"7")])
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    await target.submit(
        staged=staged,
        resource_request=ResourceRequest(),
        context=TargetContext(hpc_cluster_id=cluster_id),
    )
    assert "-NodeGroups 'GPUNodes'" in rec.sessions[0].scripts[0]


@pytest.mark.asyncio
async def test_submit_no_jobid_in_stdout_raises(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder(responses=[_FakeResponse(status_code=0, std_out=b"weird")])
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(HpcTargetError, match="didn't return a job id"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(hpc_cluster_id=cluster_id),
        )


@pytest.mark.asyncio
async def test_submit_inactive_cluster_refused(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory, is_active=False)
    rec = _Recorder()
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(HpcTargetError, match="deactivated"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(hpc_cluster_id=cluster_id),
        )


@pytest.mark.asyncio
async def test_submit_slurm_cluster_refused(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(
        factory,
        name="linux-cluster",
        scheduler="slurm",
        transport="ssh",
        port=22,
    )
    rec = _Recorder()
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(HpcTargetError, match="only handles Microsoft HPC"):
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
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(HpcTargetError, match="linked to an HPC cluster"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(),
        )


@pytest.mark.asyncio
async def test_cancel_runs_stop_hpcjob(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder(responses=[_FakeResponse(status_code=0)])
    _patch_session(monkeypatch, rec)

    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    await target.cancel(
        submit_result=SubmitResult(scheduler_job_id="42"),
        staged=staged,
        grace_seconds=8,
        context=TargetContext(hpc_cluster_id=cluster_id),
    )
    assert "Stop-HpcJob -Id 42" in rec.sessions[0].scripts[0]
    assert sleeps == [8]


@pytest.mark.asyncio
async def test_cancel_with_no_jobid_is_noop(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder()
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    await target.cancel(
        submit_result=SubmitResult(scheduler_job_id=None),
        staged=staged,
        grace_seconds=5,
        context=TargetContext(hpc_cluster_id=cluster_id),
    )
    assert rec.init_kwargs == []


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
        "pid": 99,
        "exit_code": 0,
    }
    (staged.heartbeat_dir / "complete.json").write_text(json.dumps(record), encoding="utf-8")
    obs = await target.poll(staged=staged, context=TargetContext(hpc_cluster_id=1))
    assert obs.heartbeat_state == "complete"


@pytest.mark.asyncio
async def test_scheduler_state_returns_get_hpcjob_state(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder(responses=[_FakeResponse(status_code=0, std_out=b"Running\r\n")])
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    state = await target._scheduler_state(TargetContext(hpc_cluster_id=cluster_id), "777")
    assert state == "Running"
    assert "Get-HpcJob -Id 777" in rec.sessions[0].scripts[0]


@pytest.mark.asyncio
async def test_poll_uses_context_scheduler_job_id_for_terminal_fallback(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Heartbeat absent + Get-HpcJob reports `Finished` → promote to failed."""
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder(responses=[_FakeResponse(status_code=0, std_out=b"Finished\r\n")])
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    obs = await target.poll(
        staged=staged,
        context=TargetContext(hpc_cluster_id=cluster_id, scheduler_job_id="777"),
    )
    assert obs.heartbeat_state == "failed"
    assert obs.scheduler_state == "Finished"
    assert obs.failure_reason is not None
    assert "killed by the scheduler" in obs.failure_reason
    assert "Get-HpcJob -Id 777" in rec.sessions[0].scripts[0]


@pytest.mark.asyncio
async def test_poll_skips_scheduler_query_when_no_scheduler_job_id(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Submit hasn't returned a jobid yet — poll falls back to heartbeat only.

    No WinRM session should be opened in this case.
    """
    rec = _Recorder()
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    obs = await target.poll(
        staged=staged,
        context=TargetContext(hpc_cluster_id=1, scheduler_job_id=None),
    )
    assert obs.heartbeat_state is None
    assert obs.scheduler_state is None
    assert rec.init_kwargs == []


@pytest.mark.asyncio
async def test_poll_uses_context_scheduler_job_id_running_passthrough(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Get-HpcJob reports Running + heartbeat absent → return heartbeat-only (None)."""
    factory = make_session_factory(engine)
    cluster_id = await _seed_cluster(factory)
    rec = _Recorder(responses=[_FakeResponse(status_code=0, std_out=b"Running\r\n")])
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    obs = await target.poll(
        staged=staged,
        context=TargetContext(hpc_cluster_id=cluster_id, scheduler_job_id="42"),
    )
    assert obs.heartbeat_state is None
    assert obs.scheduler_state == "Running"
    assert "Get-HpcJob -Id 42" in rec.sessions[0].scripts[0]


@pytest.mark.asyncio
async def test_fetch_logs_returns_shared_mount_paths(engine: AsyncEngine, tmp_path: Path) -> None:
    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    logs = await target.fetch_logs(staged=staged, context=TargetContext(hpc_cluster_id=1))
    assert logs.stdout_path == staged.logs_dir / "stdout.log"
    assert logs.stderr_path == staged.logs_dir / "stderr.log"
