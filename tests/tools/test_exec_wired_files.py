"""Unit tests for workflow file-wiring staging (ADR-0015).

:func:`stage_wired_file_inputs` is the copy half of the file hand-off: it
takes a downstream member's resolved input map (whose file-wired fields point
at an *upstream* job's retained output paths) plus a per-field source list,
copies the bytes into ``inputs/uploaded/<field>/`` under the downstream
working dir, and rewrites the field value to the staged copy. These cover the
single-file, time-resolved-series, and missing-source paths without a DB.
"""

from __future__ import annotations

from pathlib import Path

from matofx.tools.exec.uploads import stage_wired_file_inputs


def _upstream_output(tmp_path: Path, name: str, body: bytes = b"x") -> Path:
    """Write a fake upstream output file under a job's ``outputs/`` dir."""
    out_dir = tmp_path / "jobs" / "job_0000001" / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / name
    target.write_bytes(body)
    return target


def test_single_file_copied_and_rewritten(tmp_path: Path) -> None:
    src = _upstream_output(tmp_path, "field.vtp", b"<vtp/>")
    wd = tmp_path / "jobs" / "job_0000002"
    wd.mkdir(parents=True)

    result = stage_wired_file_inputs(
        working_dir_absolute=wd,
        inputs={"mesh_size": 1.5, "thermal_field": str(src)},
        wired_file_sources={"thermal_field": [str(src)]},
    )

    staged = wd / "inputs" / "uploaded" / "thermal_field" / "field.vtp"
    assert staged.is_file()
    assert staged.read_bytes() == b"<vtp/>"
    # The wired field now points at the staged copy; other inputs untouched.
    assert result["thermal_field"] == str(staged)
    assert result["mesh_size"] == 1.5


def test_series_stages_every_timestep_value_is_primary(tmp_path: Path) -> None:
    out_dir = tmp_path / "jobs" / "job_0000001" / "outputs"
    out_dir.mkdir(parents=True)
    paths = []
    for i in range(3):
        p = out_dir / f"temperature_{i}.npy"
        p.write_bytes(f"step{i}".encode())
        paths.append(str(p))
    primary = paths[-1]  # the representative / final snapshot
    wd = tmp_path / "jobs" / "job_0000002"
    wd.mkdir(parents=True)

    result = stage_wired_file_inputs(
        working_dir_absolute=wd,
        inputs={"temp_series": primary},
        wired_file_sources={"temp_series": paths},
    )

    staged_dir = wd / "inputs" / "uploaded" / "temp_series"
    # All three snapshots are staged so the tool can glob the series...
    assert sorted(p.name for p in staged_dir.iterdir()) == [
        "temperature_0.npy",
        "temperature_1.npy",
        "temperature_2.npy",
    ]
    # ...and the input value is the staged *primary* (final) snapshot.
    assert result["temp_series"] == str(staged_dir / "temperature_2.npy")


def test_missing_source_left_pointing_at_original(tmp_path: Path) -> None:
    wd = tmp_path / "jobs" / "job_0000002"
    wd.mkdir(parents=True)
    ghost = str(tmp_path / "jobs" / "job_0000001" / "outputs" / "gone.vtp")

    result = stage_wired_file_inputs(
        working_dir_absolute=wd,
        inputs={"thermal_field": ghost},
        wired_file_sources={"thermal_field": [ghost]},
    )

    # Nothing copied; the value is unchanged so the downstream job's own
    # input validation surfaces a plain "required input missing".
    assert not (wd / "inputs" / "uploaded").exists()
    assert result["thermal_field"] == ghost


def test_empty_sources_is_noop(tmp_path: Path) -> None:
    wd = tmp_path / "jobs" / "job_0000002"
    wd.mkdir(parents=True)
    inputs = {"a": 1, "b": "two"}
    assert stage_wired_file_inputs(
        working_dir_absolute=wd, inputs=inputs, wired_file_sources={}
    ) == inputs
