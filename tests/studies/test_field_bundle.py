"""Field-bundle write / sha-verify / load round-trip (ADR-0008 §5.3, Phase 4).

Pure on-disk — fits a small POD field surrogate, writes it through
``write_field_bundle``, and confirms ``load_field_bundle`` reconstructs an
equivalent predictor, that the sha is enforced on load, and that an
optional reference mesh is captured for the viewer.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from matofx.models.bundle import (
    extract_field_geometry,
    field_reference_mesh_path,
    load_field_bundle,
    write_field_bundle,
)
from matofx.models.errors import ModelBundleCorruptError
from matofx.studies.surrogate.field import fit_field_surrogate

_N_NODES = 20


def _modes() -> tuple[np.ndarray, np.ndarray]:
    t = np.linspace(0.0, np.pi, _N_NODES)
    return np.sin(t), np.cos(t)


def _ensemble(xs: list[float]) -> list[np.ndarray]:
    mode1, mode2 = _modes()
    return [x * mode1 + 0.5 * x * mode2 for x in xs]


def _fit():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    return fit_field_surrogate(_ensemble(xs), {"x": xs}, mode_energy_cutoff=0.999)


def test_write_load_roundtrip_predicts_equivalently(tmp_path: Path) -> None:
    surr = _fit()
    sha = write_field_bundle(
        tmp_path,
        surr,
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "temperature", "kind": "field"}],
        file_format="npy",
    )
    assert (tmp_path / "manifest.json").is_file()
    assert (tmp_path / "field" / "mu.npy").is_file()

    loaded = load_field_bundle(tmp_path, sha)
    assert loaded.n_nodes == _N_NODES
    assert loaded.n_modes == surr.n_modes
    a = surr.predict({"x": 3.5}).field
    b = loaded.predict({"x": 3.5}).field
    assert np.allclose(a, b)


def test_wrong_sha_is_rejected(tmp_path: Path) -> None:
    surr = _fit()
    write_field_bundle(
        tmp_path,
        surr,
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "t", "kind": "field"}],
        file_format="npy",
    )
    with pytest.raises(ModelBundleCorruptError, match="integrity"):
        load_field_bundle(tmp_path, "0" * 64)


def test_tampered_basis_is_detected(tmp_path: Path) -> None:
    surr = _fit()
    sha = write_field_bundle(
        tmp_path,
        surr,
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "t", "kind": "field"}],
        file_format="npy",
    )
    # Corrupt the stored modal basis after the fact.
    basis_path = tmp_path / "field" / "modal_basis.npy"
    np.save(basis_path, np.zeros((surr.n_modes, _N_NODES)))
    with pytest.raises(ModelBundleCorruptError):
        load_field_bundle(tmp_path, sha)


def test_npy_field_has_no_geometry(tmp_path: Path) -> None:
    surr = _fit()
    write_field_bundle(
        tmp_path,
        surr,
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "t", "kind": "field"}],
        file_format="npy",
    )
    assert field_reference_mesh_path(tmp_path) is None


def test_reference_mesh_is_captured(tmp_path: Path) -> None:
    """A geometry-bearing format copies its reference mesh into the bundle."""
    surr = _fit()
    mesh_src = tmp_path / "src.vtu"
    mesh_src.write_bytes(b"<VTKFile/>")  # opaque bytes — bundle copies, never parses
    bundle = tmp_path / "bundle"
    sha = write_field_bundle(
        bundle,
        surr,
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "t", "kind": "field"}],
        file_format="vtu",
        reference_mesh=mesh_src,
    )
    geom = field_reference_mesh_path(bundle)
    assert geom is not None
    assert geom.read_bytes() == b"<VTKFile/>"
    # The reference mesh is part of the integrity envelope.
    assert load_field_bundle(bundle, sha).n_nodes == _N_NODES


def test_extract_field_geometry_from_real_mesh(tmp_path: Path) -> None:
    """A real surface mesh yields full node points + aligned surface triangles."""
    import pyvista as pv  # noqa: PLC0415

    mesh = pv.Sphere(theta_resolution=10, phi_resolution=10)
    path = tmp_path / "sphere.vtp"
    mesh.save(path)
    result = extract_field_geometry(path)
    assert result is not None
    points, i, j, k = result
    assert len(points) == mesh.n_points
    assert len(i) == len(j) == len(k) > 0
    # Every triangle index addresses a node in the full point array (so a
    # per-node field aligns by index for Mesh3d intensity).
    assert max([*i, *j, *k]) < len(points)
    assert all(len(p) == 3 for p in points)
