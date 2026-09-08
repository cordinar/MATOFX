"""Validity + invariance tests for the shared bracket mesh (NAFEMS showcase).

Build-step-1 gate (`designs/showcase-tools.md` §4.4 / §7). Checks that the
procedural layered-hex L-bracket:

- has the expected structured node/element counts;
- contains no inverted elements (uniform-sign Jacobian);
- is **fixed-topology** under a thickness change (bit-identical connectivity +
  node ordering — the property the field hand-off depends on);
- exposes non-empty, disjoint named facet sets (clamp / load / heat / convection);
- carries an orthonormal per-element material rosette;
- assembles + solves a real scikit-fem problem (Laplace), and the bracket
  volume matches path x depth x thickness.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

skfem = pytest.importorskip("skfem")

_MOD = (
    Path(__file__).resolve().parents[2] / "example_tools" / "_shared" / "bracket_mesh.py"
)
_spec = importlib.util.spec_from_file_location("bracket_mesh", _MOD)
assert _spec is not None
assert _spec.loader is not None
bm = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bm
_spec.loader.exec_module(bm)


def _build(t: float = 0.004, **kw: int) -> object:
    return bm.build_bracket(t, n_span=24, n_depth=6, n_layers=4, **kw)


def test_structured_counts() -> None:
    b = _build()
    assert b.n_nodes == (24 + 1) * (6 + 1) * (4 + 1)
    assert b.n_elements == 24 * 6 * 4


def test_no_inverted_elements() -> None:
    b = _build()
    mp = b.mesh.mapping()
    # Evaluate detDF at several reference points; sign must be uniform.
    refs = np.array([[0.25, 0.5, 0.75], [0.25, 0.5, 0.75], [0.25, 0.5, 0.75]])
    det = mp.detDF(refs)
    assert np.all(det > 0) or np.all(det < 0)  # no tangled/inverted hexes
    assert np.min(np.abs(det)) > 0.0


def test_thickness_morph_preserves_topology() -> None:
    thin = _build(0.002)
    thick = _build(0.008)
    # Same connectivity + node ordering — only coordinates move.
    assert np.array_equal(thin.mesh.t, thick.mesh.t)
    assert thin.mesh.p.shape == thick.mesh.p.shape
    assert not np.allclose(thin.mesh.p, thick.mesh.p)  # geometry genuinely differs
    # The in-plane (X, midline) footprint is unchanged; only the through-
    # thickness spread scales — so the depth (Y) coordinates are identical.
    assert np.allclose(thin.mesh.p[1], thick.mesh.p[1])


def test_named_sets_nonempty_and_disjoint() -> None:
    b = _build()
    sets = {
        "clamp": set(b.clamp_facets.tolist()),
        "load": set(b.load_facets.tolist()),
        "heat": set(b.heat_facets.tolist()),
        "convection": set(b.convection_facets.tolist()),
    }
    for name, s in sets.items():
        assert s, f"{name} facet set is empty"
    # Pairwise disjoint.
    names = list(sets)
    for a in range(len(names)):
        for c in range(a + 1, len(names)):
            assert not (sets[names[a]] & sets[names[c]]), f"{names[a]} ∩ {names[c]} not empty"
    # Together they cover exactly the boundary.
    union = set().union(*sets.values())
    assert union == set(b.mesh.boundary_facets().tolist())


def test_rosette_is_orthonormal() -> None:
    b = _build()
    e1, e2, e3 = b.elem_e1, b.elem_e2, b.elem_e3
    for e in (e1, e2, e3):
        assert np.allclose(np.linalg.norm(e, axis=0), 1.0)
    assert np.allclose(np.sum(e1 * e2, axis=0), 0.0)
    assert np.allclose(np.sum(e1 * e3, axis=0), 0.0)
    assert np.allclose(np.sum(e2 * e3, axis=0), 0.0)
    # Right-handed: cross(e1, e2) == e3.
    assert np.allclose(np.cross(e1.T, e2.T).T, e3, atol=1e-9)


def test_volume_matches_geometry() -> None:
    from skfem import Basis, ElementHex1, Functional  # noqa: PLC0415

    t = 0.004
    b = _build(t)
    basis = Basis(b.mesh, ElementHex1())

    @Functional
    def one(_w: object) -> float:
        return 1.0

    volume = float(one.assemble(basis))
    expected = bm.MIDSURFACE_AREA_M2 * t
    assert volume == pytest.approx(expected, rel=0.02)


def test_laplace_solve_runs() -> None:
    """A real scikit-fem solve on the bracket: Laplace, clamp=1, load=0."""
    from skfem import Basis, ElementHex1, condense, solve  # noqa: PLC0415
    from skfem.models.poisson import laplace  # noqa: PLC0415

    b = _build()
    basis = Basis(b.mesh, ElementHex1())
    a = laplace.assemble(basis)
    clamp = basis.get_dofs(facets=b.clamp_facets)
    load = basis.get_dofs(facets=b.load_facets)
    x = basis.zeros()
    x[clamp.flatten()] = 1.0
    x[load.flatten()] = 0.0
    constrained = np.concatenate([clamp.flatten(), load.flatten()])
    sol = solve(*condense(a, x=x, D=constrained))
    # Steady diffusion with 1/0 ends → solution bounded in [0, 1] everywhere.
    assert sol.min() >= -1e-9
    assert sol.max() <= 1.0 + 1e-9
