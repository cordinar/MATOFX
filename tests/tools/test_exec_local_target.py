"""End-to-end tests for :class:`LocalTarget`.

The local target is the only Phase 1 target that exercises real OS
mechanics in CI (AOS / HPC are mocked-only because they need
external hosts). These tests stage a real working dir, submit it,
poll the heartbeat, optionally cancel, and assert the canonical
state transitions.
"""

from __future__ import annotations

import asyncio
import platform
import sys
from pathlib import Path

import pytest

from matofx.tools import (
    InputsDocument,
    LocalTarget,
    ResourceRequest,
    StagedWorkingDir,
    stage_working_dir,
)


def _make_staged(
    tmp_path: Path,
    *,
    main_script_body: str,
    main_script_name: str = "demo.py",
    cancel_grace_seconds: int = 5,
) -> StagedWorkingDir:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / main_script_name).write_text(main_script_body, encoding="utf-8")
    mount = tmp_path / "mount"
    mount.mkdir()
    inputs = InputsDocument(job_id=1, tool_alias="demo", tool_version="1.0", inputs=[])
    return stage_working_dir(
        mount_path=str(mount),
        working_dir_relative="jobs/job_0001",
        bundle_path=str(bundle),
        inputs_document=inputs,
        interpreter_path=sys.executable,
        main_script_relative=main_script_name,
        cancel_grace_seconds=cancel_grace_seconds,
    )


async def _wait_for_terminal_heartbeat(
    target: LocalTarget, staged: StagedWorkingDir, timeout: float = 30.0
) -> str | None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        observation = await target.poll(staged=staged)
        if observation.heartbeat_state in ("complete", "failed"):
            return observation.heartbeat_state
        await asyncio.sleep(0.05)
    return None


@pytest.mark.slow
@pytest.mark.asyncio
async def test_local_submit_runs_to_complete(tmp_path: Path) -> None:
    target = LocalTarget()
    staged = _make_staged(
        tmp_path,
        main_script_body=(
            "import json, pathlib\n"
            "p = pathlib.Path('inputs/inputs.json')\n"
            "data = json.loads(p.read_text())\n"
            "out = pathlib.Path('outputs')\n"
            "out.mkdir(exist_ok=True)\n"
            "(out / 'outputs.json').write_text(json.dumps({\n"
            "    'schema_version': 1,\n"
            "    'tool_alias': data['tool_alias'],\n"
            "    'outputs': []\n"
            "}))\n"
        ),
    )
    submit_result = await target.submit(staged=staged, resource_request=ResourceRequest())
    assert submit_result.pid is not None
    assert submit_result.scheduler_job_id is None

    final_state = await _wait_for_terminal_heartbeat(target, staged)
    assert final_state == "complete"

    observation = await target.poll(staged=staged)
    assert observation.exit_code == 0
    assert observation.failure_reason is None
    assert observation.started_at is not None
    assert observation.ended_at is not None


@pytest.mark.slow
@pytest.mark.asyncio
async def test_local_submit_records_failed_on_non_zero_exit(tmp_path: Path) -> None:
    target = LocalTarget()
    staged = _make_staged(tmp_path, main_script_body="import sys; sys.exit(3)\n")
    await target.submit(staged=staged, resource_request=ResourceRequest())

    final_state = await _wait_for_terminal_heartbeat(target, staged)
    assert final_state == "failed"

    observation = await target.poll(staged=staged)
    assert observation.exit_code == 3
    assert observation.failure_reason is not None
    assert "non-zero" in observation.failure_reason


@pytest.mark.asyncio
async def test_local_poll_returns_none_state_when_heartbeat_empty(tmp_path: Path) -> None:
    target = LocalTarget()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "demo.py").write_text("# stub")
    mount = tmp_path / "mount"
    mount.mkdir()
    inputs = InputsDocument(job_id=1, tool_alias="demo", tool_version="1.0", inputs=[])
    staged = stage_working_dir(
        mount_path=str(mount),
        working_dir_relative="jobs/empty",
        bundle_path=str(bundle),
        inputs_document=inputs,
        interpreter_path=sys.executable,
        main_script_relative="demo.py",
        cancel_grace_seconds=5,
    )
    observation = await target.poll(staged=staged)
    assert observation.heartbeat_state is None
    assert observation.exit_code is None


@pytest.mark.asyncio
async def test_local_fetch_logs_returns_paths(tmp_path: Path) -> None:
    target = LocalTarget()
    staged = _make_staged(tmp_path, main_script_body="print('x')\n")
    logs = await target.fetch_logs(staged=staged)
    assert logs.stdout_path == staged.logs_dir / "stdout.log"
    assert logs.stderr_path == staged.logs_dir / "stderr.log"


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(
    platform.system() == "Windows",
    reason=(
        "POSIX-only: Windows cancellation needs an interactive console for CTRL_BREAK signalling."
    ),
)
async def test_local_cancel_terminates_long_running_job(tmp_path: Path) -> None:
    target = LocalTarget()
    staged = _make_staged(
        tmp_path,
        main_script_body="import time\nwhile True:\n    time.sleep(0.1)\n",
        cancel_grace_seconds=2,
    )
    submit_result = await target.submit(staged=staged, resource_request=ResourceRequest())
    assert submit_result.pid is not None

    # Wait for the launcher to write its running.json so we know the
    # child is actually alive before we ask for cancellation.
    deadline = asyncio.get_event_loop().time() + 10
    while asyncio.get_event_loop().time() < deadline:
        if (await target.poll(staged=staged)).heartbeat_state == "running":
            break
        await asyncio.sleep(0.05)

    await target.cancel(submit_result=submit_result, staged=staged, grace_seconds=3)

    final_state = await _wait_for_terminal_heartbeat(target, staged, timeout=15.0)
    assert final_state == "failed"


@pytest.mark.asyncio
async def test_local_cancel_with_no_pid_is_noop(tmp_path: Path) -> None:
    target = LocalTarget()
    staged = _make_staged(tmp_path, main_script_body="print('x')\n")
    from matofx.tools import SubmitResult  # noqa: PLC0415

    await target.cancel(
        submit_result=SubmitResult(pid=None),
        staged=staged,
        grace_seconds=5,
    )


# --- _read_heartbeat / _parse_iso unit tests --------------------------
#
# These exercise the launcher-side parsing path the runner depends on.
# Heartbeat files can be observed mid-write (atomic-rename happens on
# the second of two writes), so parse failures must degrade silently.


def _make_heartbeat_dir(tmp_path: Path) -> Path:
    hb = tmp_path / "hb"
    hb.mkdir()
    return hb


def test_read_heartbeat_torn_read_returns_none_state(tmp_path: Path) -> None:
    """Mid-rename heartbeat (invalid JSON): treat as if not written yet."""
    from matofx.tools.exec.local import _read_heartbeat  # noqa: PLC0415

    hb = _make_heartbeat_dir(tmp_path)
    (hb / "running.json").write_text("{not valid json", encoding="utf-8")
    obs = _read_heartbeat(hb)
    assert obs.heartbeat_state is None


def test_read_heartbeat_invalid_model_returns_none_state(tmp_path: Path) -> None:
    """JSON parses but doesn't satisfy `HeartbeatRecord`: treat as not-written."""
    from matofx.tools.exec.local import _read_heartbeat  # noqa: PLC0415

    hb = _make_heartbeat_dir(tmp_path)
    (hb / "running.json").write_text('{"unexpected": "shape"}', encoding="utf-8")
    obs = _read_heartbeat(hb)
    assert obs.heartbeat_state is None


def test_read_heartbeat_failed_wins_over_running(tmp_path: Path) -> None:
    """Both files exist: the launcher wrote `failed` after `running` — take terminal."""
    import json  # noqa: PLC0415

    from matofx.tools.exec.local import _read_heartbeat  # noqa: PLC0415

    hb = _make_heartbeat_dir(tmp_path)
    running = {
        "state": "running",
        "started_at": "2026-05-04T10:00:00+00:00",
        "pid": 1,
    }
    failed = {
        "state": "failed",
        "started_at": "2026-05-04T10:00:00+00:00",
        "ended_at": "2026-05-04T10:00:01+00:00",
        "pid": 1,
        "exit_code": 7,
        "failure_reason": "non-zero exit (7)",
    }
    (hb / "running.json").write_text(json.dumps(running), encoding="utf-8")
    (hb / "failed.json").write_text(json.dumps(failed), encoding="utf-8")
    obs = _read_heartbeat(hb)
    assert obs.heartbeat_state == "failed"
    assert obs.exit_code == 7


def test_parse_iso_handles_none_and_garbage() -> None:
    from matofx.tools.exec.local import _parse_iso  # noqa: PLC0415

    assert _parse_iso(None) is None
    assert _parse_iso("not-a-date") is None
    assert _parse_iso("2026-05-04T10:00:00+00:00") is not None


@pytest.mark.asyncio
async def test_local_cancel_with_pid_invokes_kill_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cancel(pid=N)` calls the kill seam with the right pid + grace."""
    from matofx.tools import SubmitResult  # noqa: PLC0415
    from matofx.tools.exec import local as local_mod  # noqa: PLC0415

    captured: dict[str, int] = {}

    def fake_kill(pid: int, grace_seconds: int) -> None:
        captured["pid"] = pid
        captured["grace"] = grace_seconds

    monkeypatch.setattr(local_mod, "_kill_pid_with_grace", fake_kill)

    target = LocalTarget()
    staged = _make_staged(tmp_path, main_script_body="print('x')\n")
    await target.cancel(
        submit_result=SubmitResult(pid=12345),
        staged=staged,
        grace_seconds=7,
    )
    assert captured == {"pid": 12345, "grace": 7}


def test_resolve_windows_tool_returns_none_when_nothing_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No SYSTEMROOT and `which` finds nothing → None (caller bails cleanly)."""
    from matofx.tools.exec import local as local_mod  # noqa: PLC0415

    monkeypatch.delenv("SYSTEMROOT", raising=False)
    monkeypatch.setattr(local_mod.shutil, "which", lambda _name: None)
    assert local_mod._resolve_windows_tool("taskkill") is None


def test_resolve_windows_tool_falls_back_to_which(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SYSTEMROOT set but the tool isn't there → fall back to `shutil.which`."""
    from matofx.tools.exec import local as local_mod  # noqa: PLC0415

    monkeypatch.setenv("SYSTEMROOT", str(tmp_path))  # tmp_path/System32/taskkill.exe absent
    monkeypatch.setattr(local_mod.shutil, "which", lambda _name: "C:/fallback/taskkill.exe")
    assert local_mod._resolve_windows_tool("taskkill") == "C:/fallback/taskkill.exe"
