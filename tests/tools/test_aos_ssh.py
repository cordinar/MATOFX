"""Tests for :class:`SshAosTarget` with asyncssh mocked.

These tests don't open a real SSH connection — they monkeypatch
:func:`asyncssh.connect` with a fake context manager that records
the commands we'd have sent, plus optionally surfaces stub
exit / stdout / stderr values. The real asyncssh integration is
exercised at deploy-time smoke tests, not in unit tests.
"""

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
from matofx.secrets import InMemorySecretsStore, SecretNotFoundError
from matofx.tools.exec import (
    HEARTBEAT_DIRNAME,
    INPUTS_DIRNAME,
    LOGS_DIRNAME,
    OUTPUTS_DIRNAME,
    AosTargetError,
    ResourceRequest,
    SshAosTarget,
    StagedWorkingDir,
    SubmitResult,
    TargetContext,
)
from tests._migrated_db import sqlite_db_settings

# --- minimal alembic-fed engine ----------------------------------------


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
    return sqlite_db_settings(migrated_template, tmp_path, "aos.db")


@pytest.fixture
async def engine(db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(db_settings)
    try:
        yield eng
    finally:
        await eng.dispose()


# --- fake AOS host row --------------------------------------------------


async def _seed_host(
    factory,
    *,
    name: str = "linux-aos-1",
    hostname: str = "test.invalid",
    secret_ref: str = "key-1",  # noqa: S107 — fixture identifier, not a credential
    is_active: bool = True,
    transport: str = "ssh",
    os_name: str = "linux",
) -> int:
    now = _dt.datetime.now(tz=_dt.UTC)
    async with session_scope(factory) as session:
        await session.execute(
            sa.text(
                "INSERT INTO aos_host "
                "(name, hostname, os, transport, port, username, secret_ref, "
                "is_active, row_version, created_at, updated_at) "
                "VALUES (:n, :h, :o, :t, 22, 'matofx', :s, :a, 1, :ts, :ts)"
            ),
            {
                "n": name,
                "h": hostname,
                "o": os_name,
                "t": transport,
                "s": secret_ref,
                "a": 1 if is_active else 0,
                "ts": now,
            },
        )
        host_id = (
            await session.execute(sa.text("SELECT id FROM aos_host WHERE name = :n"), {"n": name})
        ).scalar_one()
        return int(host_id)


# --- fake asyncssh ------------------------------------------------------


@dataclass
class _FakeProcess:
    exit_status: int | None = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _FakeConn:
    """Minimal stand-in for asyncssh.SSHClientConnection."""

    commands: list[str] = field(default_factory=list)
    response: _FakeProcess = field(default_factory=_FakeProcess)
    raise_on_run: BaseException | None = None

    async def run(self, command: str, *, check: bool = True) -> _FakeProcess:  # noqa: ARG002
        self.commands.append(command)
        if self.raise_on_run is not None:
            raise self.raise_on_run
        return self.response


@dataclass
class _Recorder:
    """Captures asyncssh.connect kwargs across calls and serves a fake conn."""

    conn: _FakeConn = field(default_factory=_FakeConn)
    connect_kwargs: list[dict[str, Any]] = field(default_factory=list)
    raise_on_connect: BaseException | None = None


def _patch_connect(monkeypatch, recorder: _Recorder) -> None:
    @asynccontextmanager
    async def fake_connect(**kwargs: Any):
        recorder.connect_kwargs.append(kwargs)
        if recorder.raise_on_connect is not None:
            raise recorder.raise_on_connect
        yield recorder.conn

    monkeypatch.setattr(asyncssh, "connect", fake_connect)


# --- fixtures -----------------------------------------------------------


def _make_staged(tmp_path: Path) -> StagedWorkingDir:
    """Build a StagedWorkingDir with the directory tree in place."""
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


def _make_target(
    *,
    engine: AsyncEngine,
    secret_material: bytes = b"-----BEGIN OPENSSH PRIVATE KEY-----\nstub\n",
) -> SshAosTarget:
    factory = make_session_factory(engine)
    store = InMemorySecretsStore({"key-1": secret_material})
    return SshAosTarget(session_factory=factory, secrets_store=store)


# --- tests --------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_runs_command_and_parses_pid(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory)
    rec = _Recorder(conn=_FakeConn(response=_FakeProcess(exit_status=0, stdout="12345\n")))
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    result = await target.submit(
        staged=staged,
        resource_request=ResourceRequest(),
        context=TargetContext(aos_host_id=host_id),
    )
    assert result.pid == 12345

    # Inspect connect kwargs and the issued command.
    assert len(rec.connect_kwargs) == 1
    kwargs = rec.connect_kwargs[0]
    assert kwargs["host"] == "test.invalid"
    assert kwargs["username"] == "matofx"
    assert kwargs["port"] == 22
    assert kwargs["client_keys"] == [b"-----BEGIN OPENSSH PRIVATE KEY-----\nstub\n"]
    assert "known_hosts" not in kwargs  # unset since no resolver supplied

    assert len(rec.conn.commands) == 1
    cmd = rec.conn.commands[0]
    assert "cd " in cmd
    assert "setsid nohup python3" in cmd
    assert "echo $!" in cmd
    assert str(staged.working_dir) in cmd or repr(str(staged.working_dir)) in cmd


@pytest.mark.asyncio
async def test_submit_missing_host_raises_aos_error(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec = _Recorder()
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(AosTargetError, match="no longer registered"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(aos_host_id=999),
        )
    # Should never have opened a connection for an unknown host.
    assert rec.connect_kwargs == []


@pytest.mark.asyncio
async def test_submit_inactive_host_refused(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory, is_active=False)
    rec = _Recorder()
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(AosTargetError, match="deactivated"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(aos_host_id=host_id),
        )


@pytest.mark.asyncio
async def test_submit_without_host_id_raises(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec = _Recorder()
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(AosTargetError, match="linked to an always-on server"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(),
        )


@pytest.mark.asyncio
async def test_submit_missing_secret_surfaces(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory, secret_ref="never-stored")
    rec = _Recorder()
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(SecretNotFoundError):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(aos_host_id=host_id),
        )


@pytest.mark.asyncio
async def test_submit_no_pid_in_stdout_raises(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory)
    rec = _Recorder(conn=_FakeConn(response=_FakeProcess(exit_status=0, stdout="garbage\n")))
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(AosTargetError, match="didn't return a PID"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(aos_host_id=host_id),
        )


@pytest.mark.asyncio
async def test_submit_host_key_mismatch_friendly(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory)
    rec = _Recorder(raise_on_connect=asyncssh.HostKeyNotVerifiable("bad host key", []))
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(AosTargetError, match="host key that doesn't match"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(aos_host_id=host_id),
        )


@pytest.mark.asyncio
async def test_submit_known_hosts_resolver_passed_through(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory)
    rec = _Recorder(conn=_FakeConn(response=_FakeProcess(exit_status=0, stdout="42")))
    _patch_connect(monkeypatch, rec)

    pinned = tmp_path / "known_hosts"
    pinned.write_text("test.invalid ssh-rsa AAAA...\n", encoding="utf-8")

    async def resolver(host_id: int) -> str | None:  # noqa: ARG001
        return str(pinned)

    factory_again = make_session_factory(engine)
    store = InMemorySecretsStore({"key-1": b"x"})
    target = SshAosTarget(
        session_factory=factory_again,
        secrets_store=store,
        known_hosts_path_resolver=resolver,
    )
    staged = _make_staged(tmp_path)
    await target.submit(
        staged=staged,
        resource_request=ResourceRequest(),
        context=TargetContext(aos_host_id=host_id),
    )
    assert rec.connect_kwargs[0]["known_hosts"] == str(pinned)


@pytest.mark.asyncio
async def test_cancel_sends_term_then_kill(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory)
    rec = _Recorder(conn=_FakeConn(response=_FakeProcess(exit_status=0)))
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)

    # Stub asyncio.sleep so the test doesn't actually wait the grace period.
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await target.cancel(
        submit_result=SubmitResult(pid=4242),
        staged=staged,
        grace_seconds=7,
        context=TargetContext(aos_host_id=host_id),
    )

    # Two SSH connections (one TERM, one KILL); one sleep between.
    assert len(rec.connect_kwargs) == 2
    assert "kill -TERM" in rec.conn.commands[0]
    assert "kill -KILL" in rec.conn.commands[1]
    assert "4242" in rec.conn.commands[0]
    assert "4242" in rec.conn.commands[1]
    assert sleeps == [7]


@pytest.mark.asyncio
async def test_cancel_with_no_pid_is_noop(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory)
    rec = _Recorder()
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    await target.cancel(
        submit_result=SubmitResult(pid=None),
        staged=staged,
        grace_seconds=5,
        context=TargetContext(aos_host_id=host_id),
    )
    # No SSH calls when there's nothing to kill.
    assert rec.connect_kwargs == []


@pytest.mark.asyncio
async def test_poll_reads_heartbeat_from_shared_mount(engine: AsyncEngine, tmp_path: Path) -> None:
    """No SSH needed — poll reads the heartbeat file straight off the shared FS."""
    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    record = {
        "state": "running",
        "started_at": "2026-05-04T10:00:00+00:00",
        "pid": 7,
    }
    (staged.heartbeat_dir / "running.json").write_text(json.dumps(record), encoding="utf-8")
    obs = await target.poll(staged=staged, context=TargetContext(aos_host_id=1))
    assert obs.heartbeat_state == "running"
    assert obs.exit_code is None


@pytest.mark.asyncio
async def test_fetch_logs_returns_shared_mount_paths(engine: AsyncEngine, tmp_path: Path) -> None:
    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    logs = await target.fetch_logs(staged=staged, context=TargetContext(aos_host_id=1))
    assert logs.stdout_path == staged.logs_dir / "stdout.log"
    assert logs.stderr_path == staged.logs_dir / "stderr.log"


@pytest.mark.asyncio
async def test_submit_connection_refused_friendly(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory)
    rec = _Recorder(raise_on_connect=ConnectionRefusedError("nope"))
    _patch_connect(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(AosTargetError, match="Could not submit on always-on server"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(aos_host_id=host_id),
        )
