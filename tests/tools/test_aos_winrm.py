"""Tests for :class:`WinRmAosTarget` with pywinrm mocked.

These tests don't open a real WinRM connection — they monkeypatch
:class:`winrm.Session` with a fake recorder that captures init kwargs
and the PowerShell scripts run via ``run_ps``. The real pywinrm
integration is exercised at deploy-time smoke tests, not in unit
tests.
"""

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
from matofx.secrets import InMemorySecretsStore, SecretNotFoundError
from matofx.tools.exec import (
    HEARTBEAT_DIRNAME,
    INPUTS_DIRNAME,
    LOGS_DIRNAME,
    OUTPUTS_DIRNAME,
    AosTargetError,
    ResourceRequest,
    StagedWorkingDir,
    SubmitResult,
    TargetContext,
    WinRmAosTarget,
)
from tests._migrated_db import sqlite_db_settings

# --- alembic-fed engine -----------------------------------------------


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


# --- AOS host seed ----------------------------------------------------


async def _seed_host(
    factory,
    *,
    name: str = "windows-aos-1",
    hostname: str = "winaos.invalid",
    secret_ref: str = "winkey-1",  # noqa: S107 — fixture identifier, not a credential
    is_active: bool = True,
    transport: str = "winrm",
    os_name: str = "windows",
    port: int = 5986,
) -> int:
    now = _dt.datetime.now(tz=_dt.UTC)
    async with session_scope(factory) as session:
        await session.execute(
            sa.text(
                "INSERT INTO aos_host "
                "(name, hostname, os, transport, port, username, secret_ref, "
                "is_active, row_version, created_at, updated_at) "
                "VALUES (:n, :h, :o, :t, :p, 'matofx', :s, :a, 1, :ts, :ts)"
            ),
            {
                "n": name,
                "h": hostname,
                "o": os_name,
                "t": transport,
                "p": port,
                "s": secret_ref,
                "a": 1 if is_active else 0,
                "ts": now,
            },
        )
        host_id = (
            await session.execute(sa.text("SELECT id FROM aos_host WHERE name = :n"), {"n": name})
        ).scalar_one()
        return int(host_id)


# --- fake winrm -------------------------------------------------------


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
    response: _FakeResponse = field(default_factory=_FakeResponse)
    raise_on_run: BaseException | None = None

    def run_ps(self, script: str) -> _FakeResponse:
        self.scripts.append(script)
        if self.raise_on_run is not None:
            raise self.raise_on_run
        return self.response


@dataclass
class _Recorder:
    init_kwargs: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[_FakeSession] = field(default_factory=list)
    response: _FakeResponse = field(default_factory=_FakeResponse)
    raise_on_init: BaseException | None = None
    raise_on_run: BaseException | None = None


def _patch_session(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    def fake_session_init(**kwargs: Any) -> _FakeSession:
        recorder.init_kwargs.append(kwargs)
        if recorder.raise_on_init is not None:
            raise recorder.raise_on_init
        sess = _FakeSession(
            target_url=kwargs["target"],
            auth=kwargs["auth"],
            transport=kwargs["transport"],
            server_cert_validation=kwargs["server_cert_validation"],
            operation_timeout_sec=kwargs["operation_timeout_sec"],
            read_timeout_sec=kwargs["read_timeout_sec"],
            response=recorder.response,
            raise_on_run=recorder.raise_on_run,
        )
        recorder.sessions.append(sess)
        return sess

    monkeypatch.setattr(winrm, "Session", fake_session_init)


# --- helpers ----------------------------------------------------------


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


def _make_target(*, engine: AsyncEngine, password: bytes = b"hunter2") -> WinRmAosTarget:
    factory = make_session_factory(engine)
    store = InMemorySecretsStore({"winkey-1": password})
    return WinRmAosTarget(session_factory=factory, secrets_store=store)


# --- tests ------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_runs_powershell_and_parses_pid(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory)
    rec = _Recorder(response=_FakeResponse(status_code=0, std_out=b"6789\r\n"))
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    result = await target.submit(
        staged=staged,
        resource_request=ResourceRequest(),
        context=TargetContext(aos_host_id=host_id),
    )
    assert result.pid == 6789

    assert len(rec.init_kwargs) == 1
    kwargs = rec.init_kwargs[0]
    assert kwargs["target"] == "https://winaos.invalid:5986/wsman"
    assert kwargs["auth"] == ("matofx", "hunter2")
    assert kwargs["transport"] == "ntlm"
    assert kwargs["server_cert_validation"] == "validate"

    assert len(rec.sessions) == 1
    script = rec.sessions[0].scripts[0]
    assert "Start-Process" in script
    assert "python.exe" in script
    assert "Write-Output $proc.Id" in script
    assert str(staged.working_dir) in script


@pytest.mark.asyncio
async def test_submit_non_standard_port_still_uses_https_validate(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Audit S4: WinRM always uses HTTPS + certificate validation — even on a
    non-5986 port, credentials are never sent over plaintext HTTP."""
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory, port=5985)
    rec = _Recorder(response=_FakeResponse(status_code=0, std_out=b"42"))
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    await target.submit(
        staged=staged,
        resource_request=ResourceRequest(),
        context=TargetContext(aos_host_id=host_id),
    )
    kwargs = rec.init_kwargs[0]
    assert kwargs["target"] == "https://winaos.invalid:5985/wsman"
    assert kwargs["server_cert_validation"] == "validate"


def test_winrm_connect_options_repr_redacts_password() -> None:
    """Audit B6: the password bytes never appear in repr / logs."""
    from matofx.tools.exec.aos_winrm import WinRmConnectOptions  # noqa: PLC0415

    opts = WinRmConnectOptions(
        endpoint="https://h:5986/wsman",
        username="svc",
        password=b"sup3r-secret",
        transport="ntlm",
        server_cert_validation="validate",
    )
    text = repr(opts)
    assert "sup3r-secret" not in text
    assert "redacted" in text


@pytest.mark.asyncio
async def test_submit_missing_host_raises(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec = _Recorder()
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(AosTargetError, match="no longer registered"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(aos_host_id=999),
        )
    assert rec.init_kwargs == []


@pytest.mark.asyncio
async def test_submit_inactive_host_refused(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory, is_active=False)
    rec = _Recorder()
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(AosTargetError, match="deactivated"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(aos_host_id=host_id),
        )


@pytest.mark.asyncio
async def test_submit_ssh_transport_refused(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    # SSH/Linux pair — WinRM target should refuse.
    host_id = await _seed_host(
        factory,
        name="linux-mistake",
        transport="ssh",
        os_name="linux",
        port=22,
    )
    rec = _Recorder()
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(AosTargetError, match="WinRM transport"):
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
    _patch_session(monkeypatch, rec)

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
    _patch_session(monkeypatch, rec)

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
    rec = _Recorder(response=_FakeResponse(status_code=0, std_out=b"random\r\n"))
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(AosTargetError, match="didn't return a PID"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(aos_host_id=host_id),
        )


@pytest.mark.asyncio
async def test_submit_exception_is_friendly(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory)
    rec = _Recorder(raise_on_init=ConnectionRefusedError("nope"))
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(AosTargetError, match="Could not submit on Windows"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(aos_host_id=host_id),
        )


@pytest.mark.asyncio
async def test_submit_non_zero_status_surfaces(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory)
    rec = _Recorder(
        response=_FakeResponse(status_code=1, std_out=b"", std_err=b"PowerShell crashed")
    )
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    with pytest.raises(AosTargetError, match="PowerShell crashed"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(aos_host_id=host_id),
        )


@pytest.mark.asyncio
async def test_cancel_taskkills_then_force_kills(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory)
    rec = _Recorder(response=_FakeResponse(status_code=0))
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await target.cancel(
        submit_result=SubmitResult(pid=4242),
        staged=staged,
        grace_seconds=12,
        context=TargetContext(aos_host_id=host_id),
    )

    # Two sessions opened (one per WinRM call); one sleep between.
    assert len(rec.sessions) == 2
    term_script = rec.sessions[0].scripts[0]
    kill_script = rec.sessions[1].scripts[0]
    assert "taskkill /PID 4242 /T" in term_script
    assert "/T /F" not in term_script  # term call must not be /F
    assert "taskkill /PID 4242 /T /F" in kill_script
    assert sleeps == [12]


@pytest.mark.asyncio
async def test_cancel_with_no_pid_is_noop(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory)
    rec = _Recorder()
    _patch_session(monkeypatch, rec)

    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    await target.cancel(
        submit_result=SubmitResult(pid=None),
        staged=staged,
        grace_seconds=5,
        context=TargetContext(aos_host_id=host_id),
    )
    assert rec.init_kwargs == []


@pytest.mark.asyncio
async def test_poll_reads_heartbeat_from_shared_mount(engine: AsyncEngine, tmp_path: Path) -> None:
    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    record = {
        "state": "complete",
        "started_at": "2026-05-04T10:00:00+00:00",
        "ended_at": "2026-05-04T10:01:00+00:00",
        "pid": 42,
        "exit_code": 0,
    }
    (staged.heartbeat_dir / "complete.json").write_text(json.dumps(record), encoding="utf-8")
    obs = await target.poll(staged=staged, context=TargetContext(aos_host_id=1))
    assert obs.heartbeat_state == "complete"
    assert obs.exit_code == 0


@pytest.mark.asyncio
async def test_fetch_logs_returns_shared_mount_paths(engine: AsyncEngine, tmp_path: Path) -> None:
    target = _make_target(engine=engine)
    staged = _make_staged(tmp_path)
    logs = await target.fetch_logs(staged=staged, context=TargetContext(aos_host_id=1))
    assert logs.stdout_path == staged.logs_dir / "stdout.log"
    assert logs.stderr_path == staged.logs_dir / "stderr.log"


@pytest.mark.asyncio
async def test_password_must_be_utf8(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-UTF-8 password bytes surface a friendly error rather than UnicodeDecodeError."""
    factory = make_session_factory(engine)
    host_id = await _seed_host(factory)
    rec = _Recorder()
    _patch_session(monkeypatch, rec)

    # 0xFF is invalid UTF-8.
    target = _make_target(engine=engine, password=b"\xffnot-utf8")
    staged = _make_staged(tmp_path)
    with pytest.raises(AosTargetError, match="isn't valid UTF-8"):
        await target.submit(
            staged=staged,
            resource_request=ResourceRequest(),
            context=TargetContext(aos_host_id=host_id),
        )
