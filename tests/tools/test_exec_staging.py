"""Tests for the working-directory staging helper."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from matofx.tools import (
    HEARTBEAT_DIRNAME,
    INPUTS_DIRNAME,
    LAUNCHER_FILENAME,
    LOGS_DIRNAME,
    OUTPUTS_DIRNAME,
    InputsDocument,
    InputValue,
    stage_working_dir,
)
from matofx.tools.exec.staging import StagingError


def _make_bundle(tmp_path: Path, alias: str = "demo") -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / f"{alias}.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "p = Path(__file__).resolve().parent / 'inputs' / 'inputs.json'\n"
        "data = json.loads(p.read_text())\n"
        "out_dir = Path(__file__).resolve().parent / 'outputs'\n"
        "out_dir.mkdir(exist_ok=True)\n"
        "(out_dir / 'outputs.json').write_text(\n"
        "    json.dumps({'schema_version': 1, 'tool_alias': data['tool_alias'], 'outputs': []})\n"
        ")\n",
        encoding="utf-8",
    )
    return bundle


def _doc(alias: str = "demo") -> InputsDocument:
    return InputsDocument(
        job_id=1,
        tool_alias=alias,
        tool_version="1.0.0",
        inputs=[InputValue(name="x", label="X", type="integer", value=1)],
    )


def test_stage_lays_down_full_layout(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    mount = tmp_path / "mount"
    mount.mkdir()
    staged = stage_working_dir(
        mount_path=str(mount),
        working_dir_relative="jobs/job_0000001",
        bundle_path=str(bundle),
        inputs_document=_doc(),
        interpreter_path=sys.executable,
        main_script_relative="demo.py",
        cancel_grace_seconds=30,
    )
    assert staged.working_dir == mount / "jobs" / "job_0000001"
    assert staged.working_dir.is_dir()
    assert (staged.working_dir / "demo.py").is_file()
    assert (staged.working_dir / INPUTS_DIRNAME).is_dir()
    assert (staged.working_dir / OUTPUTS_DIRNAME).is_dir()
    assert (staged.working_dir / LOGS_DIRNAME).is_dir()
    assert (staged.working_dir / HEARTBEAT_DIRNAME).is_dir()
    assert staged.launcher_path == staged.working_dir / LAUNCHER_FILENAME
    assert staged.launcher_path.is_file()


def test_stage_writes_inputs_json(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    mount = tmp_path / "mount"
    mount.mkdir()
    staged = stage_working_dir(
        mount_path=str(mount),
        working_dir_relative="jobs/job_0000002",
        bundle_path=str(bundle),
        inputs_document=_doc(),
        interpreter_path=sys.executable,
        main_script_relative="demo.py",
        cancel_grace_seconds=30,
    )
    payload = json.loads(staged.inputs_json_path.read_text())
    assert payload["job_id"] == 1
    assert payload["tool_alias"] == "demo"
    assert payload["inputs"][0]["name"] == "x"


def test_stage_refuses_parent_segment_traversal(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    mount = tmp_path / "mount"
    mount.mkdir()
    with pytest.raises(StagingError, match="parent-segment"):
        stage_working_dir(
            mount_path=str(mount),
            working_dir_relative="../escape",
            bundle_path=str(bundle),
            inputs_document=_doc(),
            interpreter_path=sys.executable,
            main_script_relative="demo.py",
            cancel_grace_seconds=30,
        )


def test_stage_refuses_absolute_relative_path(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    mount = tmp_path / "mount"
    mount.mkdir()
    abs_path = "/jobs/job_x" if sys.platform != "win32" else r"C:\jobs\job_x"
    with pytest.raises(StagingError, match="relative"):
        stage_working_dir(
            mount_path=str(mount),
            working_dir_relative=abs_path,
            bundle_path=str(bundle),
            inputs_document=_doc(),
            interpreter_path=sys.executable,
            main_script_relative="demo.py",
            cancel_grace_seconds=30,
        )


def test_stage_refuses_existing_working_dir(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "jobs").mkdir()
    (mount / "jobs" / "dup").mkdir()
    with pytest.raises(StagingError, match="already exists"):
        stage_working_dir(
            mount_path=str(mount),
            working_dir_relative="jobs/dup",
            bundle_path=str(bundle),
            inputs_document=_doc(),
            interpreter_path=sys.executable,
            main_script_relative="demo.py",
            cancel_grace_seconds=30,
        )


def test_stage_reclaims_orphaned_dir_when_allowed(tmp_path: Path) -> None:
    # A leftover folder from a reused job id / DB rollback sits at the
    # target path with a stale marker file. With reclaim_orphaned=True the
    # staging moves it aside (never deletes) and lays down a fresh tree.
    bundle = _make_bundle(tmp_path)
    mount = tmp_path / "mount"
    mount.mkdir()
    orphan = mount / "jobs" / "dev" / "job_0002820"
    orphan.mkdir(parents=True)
    (orphan / "stale.txt").write_text("old run", encoding="utf-8")

    staged = stage_working_dir(
        mount_path=str(mount),
        working_dir_relative="jobs/dev/job_0002820",
        bundle_path=str(bundle),
        inputs_document=_doc(),
        interpreter_path=sys.executable,
        main_script_relative="demo.py",
        cancel_grace_seconds=30,
        reclaim_orphaned=True,
    )

    # Fresh tree staged at the target.
    assert staged.working_dir == mount / "jobs" / "dev" / "job_0002820"
    assert (staged.working_dir / "demo.py").is_file()
    assert not (staged.working_dir / "stale.txt").exists()

    # Orphan preserved, not deleted, under _orphaned/.
    moved = mount / "_orphaned" / "jobs_dev_job_0002820" / "stale.txt"
    assert moved.is_file()
    assert moved.read_text(encoding="utf-8") == "old run"


def test_stage_reclaim_suffixes_repeated_orphans(tmp_path: Path) -> None:
    # A second reclaim of the same job path must not clobber the first
    # quarantined copy — it gets a numeric suffix instead.
    bundle = _make_bundle(tmp_path)
    mount = tmp_path / "mount"
    mount.mkdir()
    existing = mount / "_orphaned" / "jobs_dev_job_0002820"
    existing.mkdir(parents=True)
    (existing / "first.txt").write_text("first", encoding="utf-8")

    orphan = mount / "jobs" / "dev" / "job_0002820"
    orphan.mkdir(parents=True)
    (orphan / "second.txt").write_text("second", encoding="utf-8")

    stage_working_dir(
        mount_path=str(mount),
        working_dir_relative="jobs/dev/job_0002820",
        bundle_path=str(bundle),
        inputs_document=_doc(),
        interpreter_path=sys.executable,
        main_script_relative="demo.py",
        cancel_grace_seconds=30,
        reclaim_orphaned=True,
    )

    assert (mount / "_orphaned" / "jobs_dev_job_0002820" / "first.txt").is_file()
    assert (mount / "_orphaned" / "jobs_dev_job_0002820__1" / "second.txt").is_file()


def test_stage_refuses_missing_bundle(tmp_path: Path) -> None:
    mount = tmp_path / "mount"
    mount.mkdir()
    with pytest.raises(StagingError, match="does not exist"):
        stage_working_dir(
            mount_path=str(mount),
            working_dir_relative="jobs/jx",
            bundle_path=str(tmp_path / "missing"),
            inputs_document=_doc(),
            interpreter_path=sys.executable,
            main_script_relative="demo.py",
            cancel_grace_seconds=30,
        )
