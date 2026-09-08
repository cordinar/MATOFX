"""Field-snapshot retention floor (ADR-0008 §4.4).

Descriptor-declared ``field`` outputs must survive a tool's
``delete_io_files`` sweep so a POD field surrogate has something to train
on — exactly the guarantee ADR-0003 gives ``csv_plot`` files. A
time-resolved field protects its whole ``field_timesteps`` series.
"""

from __future__ import annotations

from pathlib import Path

from matofx.runner.supervisor import _apply_cleanup_for_tool, _field_retained_paths
from matofx.tools import INPUTS_DIRNAME, OUTPUTS_DIRNAME
from matofx.tools.orm import Tool


def _staged(tmp_path: Path) -> Path:
    wd = tmp_path / "wd"
    (wd / INPUTS_DIRNAME).mkdir(parents=True)
    (wd / OUTPUTS_DIRNAME).mkdir(parents=True)
    (wd / INPUTS_DIRNAME / "inputs.json").write_text("{}")
    (wd / OUTPUTS_DIRNAME / "outputs.json").write_text("{}")
    return wd


def _field_tool(*, timesteps: str | None = None) -> Tool:
    out: dict[str, object] = {
        "name": "t_field",
        "type": "file",
        "file_type": "field",
        "filename": f"{OUTPUTS_DIRNAME}/t_0000.npy",
        "field_format": "npy",
    }
    if timesteps is not None:
        out["field_timesteps"] = timesteps
    return Tool(
        alias="thermal",
        name="thermal",
        version="1.0.0",
        state="released",
        descriptor={"outputs": [out], "delete_io_files": ["*.npy"]},
        bundle_path="/x",
        installed_by_user_id=1,
        execution_target="local",
    )


def test_field_retained_paths_collects_filename(tmp_path: Path) -> None:
    wd = _staged(tmp_path)
    (wd / OUTPUTS_DIRNAME / "t_0000.npy").write_bytes(b"\x00")
    outputs = _field_tool().descriptor["outputs"]
    paths = _field_retained_paths(wd, outputs)
    assert f"{OUTPUTS_DIRNAME}/t_0000.npy" in paths


def test_field_retained_paths_expands_timestep_glob(tmp_path: Path) -> None:
    wd = _staged(tmp_path)
    for i in range(3):
        (wd / OUTPUTS_DIRNAME / f"t_{i:04}.npy").write_bytes(b"\x00")
    outputs = _field_tool(timesteps=f"{OUTPUTS_DIRNAME}/t_*.npy").descriptor["outputs"]
    paths = _field_retained_paths(wd, outputs)
    for i in range(3):
        assert f"{OUTPUTS_DIRNAME}/t_{i:04}.npy" in paths


def test_single_field_snapshot_survives_delete_io(tmp_path: Path) -> None:
    """A field snapshot is kept even though ``delete_io_files`` matches it."""
    wd = _staged(tmp_path)
    snap = wd / OUTPUTS_DIRNAME / "t_0000.npy"
    snap.write_bytes(b"\x00")
    _apply_cleanup_for_tool(wd, _field_tool())
    assert snap.is_file()


def test_transient_field_series_survives_delete_io(tmp_path: Path) -> None:
    """Every timestep file in the series survives the delete sweep."""
    wd = _staged(tmp_path)
    snaps = []
    for i in range(4):
        p = wd / OUTPUTS_DIRNAME / f"t_{i:04}.npy"
        p.write_bytes(b"\x00")
        snaps.append(p)
    _apply_cleanup_for_tool(wd, _field_tool(timesteps=f"{OUTPUTS_DIRNAME}/t_*.npy"))
    for p in snaps:
        assert p.is_file()
