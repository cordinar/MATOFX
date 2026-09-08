"""Combined scalar+field bundle write / sha-verify / load round-trip (ADR-0018).

Pure on-disk (no DB): fits a scalar surrogate plus one or more POD field
surrogates, writes them through ``write_combined_bundle``, and confirms
``load_combined_bundle`` reconstructs both, that the single sha covers every
file across the ``scalar/`` and per-field ``field/<response>/`` subtrees, and
that tampering anywhere is detected.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from matofx.models.bundle import (
    combined_field_reference_mesh_path,
    load_combined_bundle,
    write_combined_bundle,
)
from matofx.models.errors import ModelBundleCorruptError
from matofx.studies.surrogate import fit_surrogate
from matofx.studies.surrogate.field import fit_field_surrogate

_N_NODES = 20
_XS = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


def _scalar():
    return fit_surrogate({"x": _XS}, [2.0 * x for x in _XS], kind="gp")


def _field(scale: float = 1.0):
    t = np.linspace(0.0, np.pi, _N_NODES)
    mode1, mode2 = np.sin(t), np.cos(t)
    ensemble = [scale * (x * mode1 + 0.5 * x * mode2) for x in _XS]
    return fit_field_surrogate(ensemble, {"x": _XS}, mode_energy_cutoff=0.999)


def test_combined_roundtrip(tmp_path: Path) -> None:
    sc, fs = _scalar(), _field()
    sha = write_combined_bundle(
        tmp_path,
        {"y": sc},
        {"temperature": fs},
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[{"name": "y", "kind": "scalar"}, {"name": "temperature", "kind": "field"}],
        field_formats={"temperature": "npy"},
    )
    assert (tmp_path / "manifest.json").is_file()
    assert (tmp_path / "scalar" / "surrogate_y.joblib").is_file()
    assert (tmp_path / "field" / "temperature" / "mu.npy").is_file()

    scalars, fields = load_combined_bundle(tmp_path, sha)
    assert set(scalars) == {"y"}
    assert set(fields) == {"temperature"}
    assert scalars["y"].fitted
    # The field half predicts equivalently after the round-trip.
    a = fs.predict({"x": 3.5}).field
    b = fields["temperature"].predict({"x": 3.5}).field
    assert np.allclose(a, b)
    # A pure-npy field carries no viewer geometry.
    assert combined_field_reference_mesh_path(tmp_path, "temperature") is None


def test_combined_multi_field(tmp_path: Path) -> None:
    """A combined bundle can carry several field outputs, each in its own subtree."""
    sha = write_combined_bundle(
        tmp_path,
        {"y": _scalar()},
        {"temperature": _field(1.0), "stress": _field(2.0)},
        factors=[{"name": "x", "min": 1.0, "max": 6.0}],
        responses=[
            {"name": "y", "kind": "scalar"},
            {"name": "temperature", "kind": "field"},
            {"name": "stress", "kind": "field"},
        ],
        field_formats={"temperature": "npy", "stress": "npy"},
    )
    assert (tmp_path / "field" / "temperature" / "mu.npy").is_file()
    assert (tmp_path / "field" / "stress" / "mu.npy").is_file()
    _scalars, fields = load_combined_bundle(tmp_path, sha)
    assert set(fields) == {"temperature", "stress"}
    # The two fields differ (stress is scaled 2x) — proves they're not aliased.
    t = fields["temperature"].predict({"x": 4.0}).field
    s = fields["stress"].predict({"x": 4.0}).field
    assert not np.allclose(t, s)


def test_combined_wrong_sha_rejected(tmp_path: Path) -> None:
    write_combined_bundle(
        tmp_path,
        {"y": _scalar()},
        {"temperature": _field()},
        factors=[{"name": "x"}],
        responses=[{"name": "y", "kind": "scalar"}, {"name": "temperature", "kind": "field"}],
        field_formats={"temperature": "npy"},
    )
    with pytest.raises(ModelBundleCorruptError, match="integrity"):
        load_combined_bundle(tmp_path, "0" * 64)


def test_combined_tamper_in_field_subtree_detected(tmp_path: Path) -> None:
    """Corrupting a file in one field subtree fails the single combined sha."""
    sha = write_combined_bundle(
        tmp_path,
        {"y": _scalar()},
        {"temperature": _field()},
        factors=[{"name": "x"}],
        responses=[{"name": "y", "kind": "scalar"}, {"name": "temperature", "kind": "field"}],
        field_formats={"temperature": "npy"},
    )
    np.save(tmp_path / "field" / "temperature" / "modal_basis.npy", np.zeros((1, _N_NODES)))
    with pytest.raises(ModelBundleCorruptError):
        load_combined_bundle(tmp_path, sha)


def test_combined_empty_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty combined bundle"):
        write_combined_bundle(
            tmp_path, {}, {}, factors=[], responses=[], field_formats={}
        )
