"""Unit tests for the job ``field-frames`` parsing helpers (ADR-0008 §4.4).

The endpoint's filesystem-facing helpers are pure (no DB / HTTP), so they
exercise the steady-vs-transient resolution + the snapshot loading directly.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from matofx.api.routers.jobs import (
    _find_field_output,
    _load_field_frames,
    _resolve_field_frame_paths,
)


def test_find_field_output_matches_by_name_and_kind() -> None:
    descriptor = {
        "outputs": [
            {"name": "t_max", "type": "number"},
            {"name": "temperature_field", "type": "file", "file_type": "field", "filename": "x"},
        ]
    }
    spec = _find_field_output(descriptor, "temperature_field")
    assert spec is not None
    assert spec["filename"] == "x"
    assert _find_field_output(descriptor, "t_max") is None  # not a field output
    assert _find_field_output(descriptor, "missing") is None
    assert _find_field_output(None, "x") is None


def test_resolve_steady_frame_path(tmp_path: Path) -> None:
    wd = tmp_path / "wd"
    (wd / "outputs").mkdir(parents=True)
    snap = wd / "outputs" / "temperature.vtp"
    snap.write_bytes(b"x")
    spec = {"filename": "outputs/temperature.vtp"}
    paths = _resolve_field_frame_paths(wd, wd / "outputs", spec)
    assert paths == [snap.resolve()]


def test_resolve_transient_series_sorted(tmp_path: Path) -> None:
    wd = tmp_path / "wd"
    (wd / "outputs").mkdir(parents=True)
    for i in (2, 0, 1):  # written out of order
        (wd / "outputs" / f"t_{i:04}.vtp").write_bytes(b"x")
    spec = {"filename": "outputs/t_0000.vtp", "field_timesteps": "outputs/t_*.vtp"}
    paths = _resolve_field_frame_paths(wd, wd / "outputs", spec)
    assert [p.name for p in paths] == ["t_0000.vtp", "t_0001.vtp", "t_0002.vtp"]


def test_load_field_frames_npy_series(tmp_path: Path) -> None:
    paths = []
    for step in range(4):
        p = tmp_path / f"f_{step}.npy"
        np.save(p, np.arange(10.0) * (step + 1))
        paths.append(p)
    points, faces, frames, n_nodes = _load_field_frames(paths, "npy", None)
    assert points is None
    assert faces is None
    assert n_nodes == 10
    assert len(frames) == 4
    assert frames[0][1] == 1.0  # step 0: arange*1
    assert frames[3][1] == 4.0  # step 3: arange*4


def test_load_field_frames_vtp_series_has_geometry(tmp_path: Path) -> None:
    import pyvista as pv  # noqa: PLC0415

    base = pv.Sphere(theta_resolution=10, phi_resolution=10)
    paths = []
    for step in range(3):
        mesh = base.copy()
        mesh.point_data["temperature"] = np.asarray(base.points[:, 2], dtype=float) * (step + 1)
        p = tmp_path / f"temp_{step:04}.vtp"
        mesh.save(p)
        paths.append(p)
    points, faces, frames, n_nodes = _load_field_frames(paths, "vtp", "temperature")
    assert points is not None
    assert faces is not None
    assert n_nodes == base.n_points
    assert len(frames) == 3
    assert all(len(f) == n_nodes for f in frames)
    i, j, k = faces
    assert max([*i, *j, *k]) < len(points)
