"""Tests for descriptor-driven post-run cleanup (unified keep-list model)."""

from __future__ import annotations

from pathlib import Path

from matofx.tools import (
    HEARTBEAT_DIRNAME,
    INPUTS_DIRNAME,
    LAUNCHER_FILENAME,
    LOGS_DIRNAME,
    OUTPUTS_DIRNAME,
    apply_cleanup,
)


def _populated_working_dir(tmp_path: Path) -> Path:
    wd = tmp_path / "wd"
    wd.mkdir()
    for sub in (INPUTS_DIRNAME, OUTPUTS_DIRNAME, LOGS_DIRNAME, HEARTBEAT_DIRNAME):
        (wd / sub).mkdir()
    (wd / LAUNCHER_FILENAME).write_text("# launcher")
    (wd / "demo.py").write_text("# main")
    (wd / "scratch.tmp").write_text("temporary")
    (wd / "results.csv").write_text("a,b,c")
    (wd / INPUTS_DIRNAME / "inputs.json").write_text("{}")
    (wd / INPUTS_DIRNAME / "uploaded.dat").write_text("blob")
    (wd / OUTPUTS_DIRNAME / "outputs.json").write_text("{}")
    (wd / OUTPUTS_DIRNAME / "declared.csv").write_text("t,y")
    (wd / OUTPUTS_DIRNAME / "intermediate.tmp").write_text("scratch")
    (wd / LOGS_DIRNAME / "stdout.log").write_text("log")
    (wd / HEARTBEAT_DIRNAME / "complete.json").write_text("{}")
    return wd


# --- empty keep-list = keep everything -----------------------------------


def test_empty_keep_list_keeps_everything(tmp_path: Path) -> None:
    wd = _populated_working_dir(tmp_path)
    apply_cleanup(working_dir=wd, retain_files=[])
    assert (wd / "scratch.tmp").is_file()
    assert (wd / "results.csv").is_file()
    assert (wd / INPUTS_DIRNAME / "uploaded.dat").is_file()
    assert (wd / OUTPUTS_DIRNAME / "intermediate.tmp").is_file()
    assert (wd / OUTPUTS_DIRNAME / "declared.csv").is_file()


# --- non-empty keep-list sweeps top level AND outputs/ -------------------


def test_keep_list_drops_non_matching_top_level(tmp_path: Path) -> None:
    wd = _populated_working_dir(tmp_path)
    apply_cleanup(working_dir=wd, retain_files=["*.csv"])
    assert (wd / "results.csv").is_file()
    assert not (wd / "scratch.tmp").exists()
    assert not (wd / "demo.py").exists()
    # Protected entries always survive the sweep.
    assert (wd / LAUNCHER_FILENAME).is_file()
    assert (wd / INPUTS_DIRNAME).is_dir()
    assert (wd / OUTPUTS_DIRNAME).is_dir()
    assert (wd / LOGS_DIRNAME).is_dir()
    assert (wd / HEARTBEAT_DIRNAME).is_dir()


def test_keep_list_sweeps_undeclared_outputs_files(tmp_path: Path) -> None:
    """A non-empty keep-list now reaches into ``outputs/``: undeclared,
    unmatched files there are deleted, but the contract floor survives."""
    wd = _populated_working_dir(tmp_path)
    apply_cleanup(working_dir=wd, retain_files=["*.csv"])
    # Undeclared, unlisted output-side scratch is removed...
    assert not (wd / OUTPUTS_DIRNAME / "intermediate.tmp").exists()
    assert not (wd / OUTPUTS_DIRNAME / "declared.csv").exists()  # not declared here
    # ...but the I/O contract document is always kept.
    assert (wd / OUTPUTS_DIRNAME / "outputs.json").is_file()


def test_declared_outputs_survive_via_extra_protected(tmp_path: Path) -> None:
    """The runner floors every declared output; it survives an empty *and*
    a non-matching keep-list."""
    wd = _populated_working_dir(tmp_path)
    apply_cleanup(
        working_dir=wd,
        retain_files=["*.csv"],
        extra_protected=[f"{OUTPUTS_DIRNAME}/declared.csv"],
    )
    assert (wd / OUTPUTS_DIRNAME / "declared.csv").is_file()
    assert not (wd / OUTPUTS_DIRNAME / "intermediate.tmp").exists()


def test_inputs_are_kept_wholesale_under_keep_list(tmp_path: Path) -> None:
    """``inputs/`` is the submission record — never swept, even undeclared
    files, regardless of the keep-list."""
    wd = _populated_working_dir(tmp_path)
    apply_cleanup(working_dir=wd, retain_files=["*.csv"])
    assert (wd / INPUTS_DIRNAME / "uploaded.dat").is_file()
    assert (wd / INPUTS_DIRNAME / "inputs.json").is_file()


def test_keep_pattern_targets_an_output_side_file(tmp_path: Path) -> None:
    """An ``outputs/`` glob keeps a specific undeclared output file."""
    wd = _populated_working_dir(tmp_path)
    (wd / OUTPUTS_DIRNAME / "keepme.vtk").write_text("mesh")
    apply_cleanup(working_dir=wd, retain_files=["outputs/*.vtk"])
    assert (wd / OUTPUTS_DIRNAME / "keepme.vtk").is_file()
    # Other undeclared output-side scratch still goes.
    assert not (wd / OUTPUTS_DIRNAME / "intermediate.tmp").exists()


# --- legacy delete_io_files deny-list (back-compat) ----------------------


def test_legacy_delete_io_files_scrubs_inputs_and_outputs(tmp_path: Path) -> None:
    wd = _populated_working_dir(tmp_path)
    apply_cleanup(working_dir=wd, retain_files=[], delete_io_files=["*.tmp", "*.dat"])
    assert not (wd / INPUTS_DIRNAME / "uploaded.dat").exists()
    assert not (wd / OUTPUTS_DIRNAME / "intermediate.tmp").exists()
    # Empty keep-list means no top-level sweep; scratch.tmp survives.
    assert (wd / "scratch.tmp").is_file()


def test_legacy_delete_io_never_touches_logs_or_heartbeat(tmp_path: Path) -> None:
    wd = _populated_working_dir(tmp_path)
    apply_cleanup(working_dir=wd, retain_files=[], delete_io_files=["*"])
    assert (wd / LOGS_DIRNAME / "stdout.log").is_file()
    assert (wd / HEARTBEAT_DIRNAME / "complete.json").is_file()


# --- robustness ----------------------------------------------------------


def test_apply_cleanup_idempotent(tmp_path: Path) -> None:
    wd = _populated_working_dir(tmp_path)
    apply_cleanup(working_dir=wd, retain_files=["*.csv"])
    apply_cleanup(working_dir=wd, retain_files=["*.csv"])  # exact same rules, no errors
    assert (wd / "results.csv").is_file()


def test_apply_cleanup_on_missing_dir_is_noop(tmp_path: Path) -> None:
    apply_cleanup(working_dir=tmp_path / "ghost", retain_files=["*.csv"])


# --- ADR-0003 retention floor (2026-05-20) -------------------------------


def test_floor_protects_io_documents_from_wildcard_delete_io(tmp_path: Path) -> None:
    """The two I/O documents survive even ``delete_io_files=["*"]``."""
    wd = _populated_working_dir(tmp_path)
    apply_cleanup(working_dir=wd, retain_files=[], delete_io_files=["*"])
    assert (wd / INPUTS_DIRNAME / "inputs.json").is_file()
    assert (wd / OUTPUTS_DIRNAME / "outputs.json").is_file()
    assert not (wd / OUTPUTS_DIRNAME / "intermediate.tmp").exists()


def test_floor_protects_io_documents_from_keep_sweep(tmp_path: Path) -> None:
    """A keep-list that matches nothing in ``outputs/`` still keeps
    ``outputs.json`` (contract floor)."""
    wd = _populated_working_dir(tmp_path)
    apply_cleanup(working_dir=wd, retain_files=["nothing-here-*"])
    assert (wd / OUTPUTS_DIRNAME / "outputs.json").is_file()


def test_extra_protected_survives_legacy_delete_io(tmp_path: Path) -> None:
    """``extra_protected`` extends the floor against the legacy deny-list."""
    wd = _populated_working_dir(tmp_path)
    (wd / OUTPUTS_DIRNAME / "results.csv").write_text("t,y\n0,0\n")
    (wd / OUTPUTS_DIRNAME / "scratch.csv").write_text("noise")
    apply_cleanup(
        working_dir=wd,
        retain_files=[],
        delete_io_files=["*.csv"],
        extra_protected=[f"{OUTPUTS_DIRNAME}/results.csv"],
    )
    assert (wd / OUTPUTS_DIRNAME / "results.csv").is_file()
    assert not (wd / OUTPUTS_DIRNAME / "scratch.csv").exists()


# --- locked / read-only entries must never crash cleanup -----------------


def test_keep_list_drops_pycache_left_by_a_sibling_import(tmp_path: Path) -> None:
    wd = _populated_working_dir(tmp_path)
    pyc = wd / "__pycache__"
    pyc.mkdir()
    (pyc / "engine.cpython-313.pyc").write_text("bytecode")
    apply_cleanup(working_dir=wd, retain_files=["*.csv"])
    assert not pyc.exists()  # normal path removes it cleanly


def test_safe_delete_clears_a_read_only_file(tmp_path: Path) -> None:
    import stat  # noqa: PLC0415

    from matofx.tools.exec.cleanup import _safe_delete  # noqa: PLC0415

    f = tmp_path / "readonly.pyc"
    f.write_text("x")
    f.chmod(stat.S_IREAD)
    _safe_delete(f)  # clears read-only + deletes; must not raise
    assert not f.exists()


def test_cleanup_does_not_raise_on_undeletable_entry(tmp_path: Path) -> None:
    """A persistently locked entry is logged and skipped — cleanup never raises,
    so the runner loop can't wedge (the WinError 5 / OneDrive lock case)."""
    from matofx.tools.exec.cleanup import _on_rm_error  # noqa: PLC0415

    locked = tmp_path / "locked"
    locked.mkdir()

    def _always_fails(_p: object) -> None:
        raise PermissionError("WinError 5: access denied")

    _on_rm_error(_always_fails, str(locked), PermissionError("WinError 5"))
