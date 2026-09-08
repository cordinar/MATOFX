"""End-to-end tests for the rendered launcher script.

The launcher is auto-generated and dependency-free, so the test
strategy is: stage a real working directory, invoke the rendered
launcher via the current Python interpreter, and assert that the
heartbeat files appear with the expected contents.

These tests run a real subprocess, so they're flagged ``slow`` —
they finish in a few hundred milliseconds each but cost more than
the pure-Python tests around them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from matofx.tools import (
    HEARTBEAT_DIRNAME,
    LOGS_DIRNAME,
    InputsDocument,
    InputValue,
    render_launcher_script,
    stage_working_dir,
)


def _stage(
    tmp_path: Path,
    *,
    main_script_body: str,
    main_script_name: str = "demo.py",
    cancel_grace_seconds: int = 5,
) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / main_script_name).write_text(main_script_body, encoding="utf-8")
    mount = tmp_path / "mount"
    mount.mkdir()
    inputs = InputsDocument(
        job_id=99,
        tool_alias="demo",
        tool_version="1.0.0",
        inputs=[InputValue(name="x", label="X", type="integer", value=1)],
    )
    staged = stage_working_dir(
        mount_path=str(mount),
        working_dir_relative="jobs/job_0099",
        bundle_path=str(bundle),
        inputs_document=inputs,
        interpreter_path=sys.executable,
        main_script_relative=main_script_name,
        cancel_grace_seconds=cancel_grace_seconds,
    )
    return staged.working_dir


@pytest.mark.slow
def test_launcher_writes_complete_on_clean_exit(tmp_path: Path) -> None:
    wd = _stage(
        tmp_path,
        main_script_body="print('hello'); import sys; sys.exit(0)\n",
    )
    result = subprocess.run(  # noqa: S603 — running our own launcher under sys.executable
        [sys.executable, str(wd / "launcher.py")],
        cwd=wd,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    complete = wd / HEARTBEAT_DIRNAME / "complete.json"
    assert complete.is_file()
    payload = json.loads(complete.read_text())
    assert payload["state"] == "complete"
    assert payload["exit_code"] == 0
    assert payload["pid"]
    assert payload["host"]
    stdout_log = wd / LOGS_DIRNAME / "stdout.log"
    assert "hello" in stdout_log.read_text()


@pytest.mark.slow
def test_launcher_writes_failed_on_non_zero_exit(tmp_path: Path) -> None:
    wd = _stage(
        tmp_path,
        main_script_body="import sys; sys.exit(7)\n",
    )
    result = subprocess.run(  # noqa: S603 — running our own launcher under sys.executable
        [sys.executable, str(wd / "launcher.py")],
        cwd=wd,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 7
    failed = wd / HEARTBEAT_DIRNAME / "failed.json"
    assert failed.is_file()
    payload = json.loads(failed.read_text())
    assert payload["state"] == "failed"
    assert payload["exit_code"] == 7
    assert "non-zero" in payload["failure_reason"]


@pytest.mark.slow
def test_launcher_writes_failed_when_main_script_missing(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "demo.py").write_text("# placeholder so staging accepts the bundle\n")
    mount = tmp_path / "mount"
    mount.mkdir()
    inputs = InputsDocument(job_id=1, tool_alias="demo", tool_version="1.0", inputs=[])
    staged = stage_working_dir(
        mount_path=str(mount),
        working_dir_relative="jobs/jm",
        bundle_path=str(bundle),
        inputs_document=inputs,
        interpreter_path=sys.executable,
        main_script_relative="nope.py",
        cancel_grace_seconds=5,
    )
    result = subprocess.run(  # noqa: S603 — running our own launcher under sys.executable
        [sys.executable, str(staged.launcher_path)],
        cwd=staged.working_dir,
        capture_output=True,
        timeout=30,
        check=False,
    )
    # Subprocess starts (interpreter exists), then errors on the
    # missing main script — the child returns non-zero and the
    # launcher records ``failed``.
    failed = staged.working_dir / HEARTBEAT_DIRNAME / "failed.json"
    assert failed.is_file(), result.stderr.decode()
    payload = json.loads(failed.read_text())
    assert payload["state"] == "failed"


def test_render_launcher_rejects_negative_grace() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        render_launcher_script(
            interpreter_path="/usr/bin/python3",
            main_script_relative="demo.py",
            cancel_grace_seconds=-1,
        )


def test_render_launcher_bakes_in_constants() -> None:
    text = render_launcher_script(
        interpreter_path="/opt/python/bin/python3.12",
        main_script_relative="alpha.py",
        cancel_grace_seconds=42,
    )
    # Bake-in is positional in the template; verify each constant
    # appears in the rendered source.
    assert "/opt/python/bin/python3.12" in text
    assert "alpha.py" in text
    assert "CANCEL_GRACE_SECONDS = 42" in text
