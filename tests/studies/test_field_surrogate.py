"""Tests for the POD field-surrogate engine (ADR-0008 §4.3, Phase 4).

Pure numpy/scipy — no DB, no mesh-format dependency. A synthetic
rank-2 ensemble (``field_i = c1(x)·mode1 + c2(x)·mode2``) is decomposed,
the modes recovered, and a held-out field reconstructed; plus the
common-mesh / too-few / degenerate guards and the ``.npy`` loader (the
VTK/CGNS path raises a clear "dep not installed" error until pyvista is
vendored).
"""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

from matofx.studies import (
    FieldSurrogate,
    FieldSurrogateError,
    fit_field_surrogate,
    load_field_snapshot,
)

_N_NODES = 24


def _modes() -> tuple[np.ndarray, np.ndarray]:
    t = np.linspace(0.0, np.pi, _N_NODES)
    return np.sin(t), np.cos(t)


def _ensemble(xs: list[float]) -> list[np.ndarray]:
    """``field = x·mode1 + 0.5x·mode2`` — exactly rank 2, linear in x."""
    mode1, mode2 = _modes()
    return [x * mode1 + 0.5 * x * mode2 for x in xs]


def test_fit_and_predict_recovers_field() -> None:
    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    surr = fit_field_surrogate(_ensemble(xs), {"x": xs}, mode_energy_cutoff=0.999)
    assert isinstance(surr, FieldSurrogate)
    assert surr.n_nodes == _N_NODES
    # Rank-2 data → at most 2 modes capture ~all the variance.
    assert 1 <= surr.n_modes <= 2
    assert surr.quality.mode_energy >= 0.999

    mode1, mode2 = _modes()
    expected = 4.0 * mode1 + 0.5 * 4.0 * mode2  # held-out-ish interpolation point
    pred = surr.predict({"x": 4.0})
    assert pred.field.shape == (_N_NODES,)
    rel_rmse = float(np.sqrt(np.mean((pred.field - expected) ** 2)) / (np.std(expected) + 1e-9))
    assert rel_rmse < 0.1
    # GP-backed → per-node uncertainty is propagated.
    assert pred.field_std is not None
    assert pred.field_std.shape == (_N_NODES,)


def test_surrogate_kind_selects_per_mode_regressor() -> None:
    """The per-mode coefficient regressor honours ``surrogate_kind`` (ADR-0018 Tier 1)."""
    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    for kind in ("gp", "poly", "gbm"):
        surr = fit_field_surrogate(
            _ensemble(xs), {"x": xs}, mode_energy_cutoff=0.999, surrogate_kind=kind
        )
        assert surr.per_mode_surrogates
        assert all(s.kind == kind for s in surr.per_mode_surrogates)
    # Non-GP backends carry no per-node uncertainty.
    poly = fit_field_surrogate(_ensemble(xs), {"x": xs}, surrogate_kind="poly")
    assert poly.predict({"x": 4.0}).field_std is None


def test_field_quality_reports_field_q2_and_parity() -> None:
    """Field Q² (energy-weighted) + held-out mean-field parity populate (ADR-0018)."""
    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    surr = fit_field_surrogate(_ensemble(xs), {"x": xs}, mode_energy_cutoff=0.999)
    q = surr.quality
    assert q.field_q2 is not None
    assert -1.0 <= q.field_q2 <= 1.0
    # Parity arrays: one (actual, predicted) mean-field pair per training run.
    assert len(q.cv_field_actual) == len(xs)
    assert len(q.cv_field_predicted) == len(xs)
    # A smooth ensemble should predict the mean field closely.
    assert q.cv_field_actual[0] == pytest.approx(q.cv_field_predicted[0], rel=0.1)


def test_auto_selects_a_fitted_regressor_per_mode() -> None:
    """``surrogate_kind='auto'`` fits each mode with a real, fitted candidate."""
    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    surr = fit_field_surrogate(
        _ensemble(xs), {"x": xs}, mode_energy_cutoff=0.999, surrogate_kind="auto"
    )
    assert surr.per_mode_surrogates
    assert all(s.fitted for s in surr.per_mode_surrogates)
    assert all(s.kind in ("gp", "gp_matern", "poly", "gbm") for s in surr.per_mode_surrogates)


def test_max_modes_caps_retained() -> None:
    xs = [float(i) for i in range(1, 9)]
    surr = fit_field_surrogate(_ensemble(xs), {"x": xs}, max_modes=1)
    assert surr.n_modes == 1


def test_mismatched_mesh_raises() -> None:
    snapshots = [np.zeros(10), np.ones(11)]  # different node counts
    with pytest.raises(FieldSurrogateError, match="Mismatched mesh"):
        fit_field_surrogate(snapshots, {"x": [1.0, 2.0]})


def test_too_few_snapshots_raises() -> None:
    with pytest.raises(FieldSurrogateError, match="at least two"):
        fit_field_surrogate([np.zeros(8)], {"x": [1.0]})


def test_identical_snapshots_raises() -> None:
    same = [np.ones(8), np.ones(8), np.ones(8)]
    with pytest.raises(FieldSurrogateError, match="no field variation"):
        fit_field_surrogate(same, {"x": [1.0, 2.0, 3.0]})


def test_load_npy_roundtrip(tmp_path) -> None:
    arr = np.array([1.0, 2.0, 3.0, 4.0])
    path = tmp_path / "field.npy"
    np.save(path, arr)
    loaded = load_field_snapshot(path, file_format="npy")
    assert np.array_equal(loaded, arr)


def test_load_vtk_without_pyvista_raises(tmp_path) -> None:
    """Until pyvista is vendored, the VTK path fails with a clear message."""
    if importlib.util.find_spec("pyvista") is not None:
        pytest.skip("pyvista is installed — the not-vendored error path doesn't apply")
    path = tmp_path / "field.vtu"
    path.write_bytes(b"not really a mesh")
    with pytest.raises(FieldSurrogateError, match="pyvista"):
        load_field_snapshot(path, file_format="vtu")


def test_load_unsupported_format_raises(tmp_path) -> None:
    path = tmp_path / "field.xyz"
    path.write_bytes(b"x")
    with pytest.raises(FieldSurrogateError, match="Unsupported field format"):
        load_field_snapshot(path, file_format="xyz")
