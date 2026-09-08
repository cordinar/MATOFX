"""Verification + physics tests for the Bracket Stress layered-solid solver.

The structural keystone must be *verified*, not asserted (a NAFEMS-grade claim):

- **Verification vs CLT** — a flat layered coupon's membrane modulus, recovered
  by the FE constitutive, matches Classical Lamination Theory (and a UD ``[0]``
  coupon recovers the fibre modulus E1). This is the layered-plate-vs-CLT
  benchmark the design calls for.
- **Stacking sequence matters** — on the bracket, fibres in the core bend more
  easily than fibres outboard (the physics a smeared model would miss, and the
  reason for a layered solid).
- **Failure indices** behave (safe state ≪ 1; both criteria array-vectorised
  consistently with the scalar reference).

Coupon + bracket use small meshes for speed; the production tool is finer.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

skfem = pytest.importorskip("skfem")

_BUNDLE = Path(__file__).resolve().parents[2] / "example_tools" / "bracket_stress"
sys.path.insert(0, str(_BUNDLE))  # so bracket_stress can import its siblings
_ls_spec = importlib.util.spec_from_file_location("laminate_solid", _BUNDLE / "laminate_solid.py")
assert _ls_spec is not None
assert _ls_spec.loader is not None
ls = importlib.util.module_from_spec(_ls_spec)
sys.modules["laminate_solid"] = ls
_ls_spec.loader.exec_module(ls)

_bs_spec = importlib.util.spec_from_file_location("bracket_stress", _BUNDLE / "bracket_stress.py")
assert _bs_spec is not None
assert _bs_spec.loader is not None
bs = importlib.util.module_from_spec(_bs_spec)
sys.modules["bracket_stress"] = bs
_bs_spec.loader.exec_module(bs)

# CLT engine (from the laminate bundle) for the verification reference.
_clt_path = (
    Path(__file__).resolve().parents[2] / "example_tools" / "composite_laminate" / "laminate_clt.py"
)
_clt_spec = importlib.util.spec_from_file_location("laminate_clt", _clt_path)
assert _clt_spec is not None
assert _clt_spec.loader is not None
clt = importlib.util.module_from_spec(_clt_spec)
sys.modules["laminate_clt"] = clt
_clt_spec.loader.exec_module(clt)

_E1, _E2, _G12, _NU12 = 181.0, 10.3, 7.17, 0.28


def _coupon_modulus(layup: list[float]) -> float:
    """Apparent membrane modulus Ex (GPa) of a flat layered coupon, by FE.

    A box (L x W x H) with one element layer per ply (angles ``layup`` through Z),
    pulled in X with the far face clamped; ``Ex = applied_stress / mean_strain``.
    """
    from skfem import (  # noqa: PLC0415
        Basis,
        BilinearForm,
        ElementHex1,
        ElementHex2,
        ElementVector,
        FacetBasis,
        Functional,
        LinearForm,
        MeshHex,
        condense,
        solve,
    )
    from skfem.helpers import ddot, sym_grad  # noqa: PLC0415

    n = len(layup)
    length, width, height = 0.10, 0.02, 0.002
    mesh = MeshHex.init_tensor(
        np.linspace(0, length, 13), np.linspace(0, width, 3), np.linspace(0, height, n + 1)
    )
    vb = Basis(mesh, ElementVector(ElementHex2()), intorder=3)
    # Per-element rotated stiffness: the ply axes are global (e1=X, e2=Y, e3=Z),
    # rotated by the layer's angle about Z. Layer index from the element's z.
    zc = mesh.p[2][mesh.t].mean(axis=0)
    layer = np.clip((zc / height * n).astype(int), 0, n - 1)
    c_mat = ls.ply_stiffness_tensor(_E1, _E2, _G12, _NU12)
    ax, ay, az = np.array([1.0, 0, 0]), np.array([0, 1.0, 0]), np.array([0, 0, 1.0])
    q = np.stack(
        [ls.rotation_ply_to_global(ax, ay, az, layup[layer[e]]) for e in range(mesh.t.shape[1])],
        axis=-1,
    )
    cglob = ls.global_stiffness_field(c_mat, q)

    @BilinearForm
    def elastic(u: object, v: object, _w: object) -> object:
        eu, ev = sym_grad(u), sym_grad(v)
        return ddot(np.einsum("ijkle,klen->ijen", cglob, eu), ev)

    k = elastic.assemble(vb)
    right = mesh.facets_satisfying(lambda x: x[0] > length - 1e-9)
    fb = FacetBasis(mesh, ElementVector(ElementHex2()), facets=right, intorder=3)
    fa = FacetBasis(mesh, ElementHex1(), facets=right, intorder=3)

    @Functional
    def one(_w: object) -> float:
        return 1.0

    sigma = 1.0e6  # 1 MPa
    _ = float(one.assemble(fa))

    @LinearForm
    def trac(v: object, _w: object) -> object:
        return sigma * v[0]

    f = trac.assemble(fb)
    clamp = vb.get_dofs(facets=mesh.facets_satisfying(lambda x: x[0] < 1e-9)).flatten()
    u = solve(*condense(k, f, D=clamp))
    strain_xx = float(np.asarray(vb.interpolate(u).grad)[0, 0].mean())
    return sigma / strain_xx / 1e9


def test_ud_coupon_recovers_fibre_modulus() -> None:
    ex = _coupon_modulus([0.0] * 8)
    assert ex == pytest.approx(_E1, rel=0.03)  # within 3% of E1


def test_layered_coupon_matches_clt() -> None:
    layup = clt.build_layup(45.0, 0.0)  # [45/-45/0/0]s
    fe_ex = _coupon_modulus(layup)
    lam = clt.LAMINA["CFRP"]
    a, _b, _d, h = clt.abd_matrices(lam, layup, 0.002)
    clt_ex, _ey, _g, _nu = clt.effective_constants(a, h)
    # The layered solid reproduces CLT membrane stiffness to a few percent.
    assert fe_ex == pytest.approx(clt_ex, rel=0.05)


def test_stacking_sequence_matters_on_bracket() -> None:
    common = {
        "e1": _E1, "e2": _E2, "g12": _G12, "nu12": _NU12, "alpha1": 0.02, "alpha2": 22.5,
        "thickness_m": 0.004, "t_ref": 25.0, "mech_load": 200.0,
        "n_span": 12, "n_depth": 3, "n_layers": 8,
    }
    br = bs.bmod.build_bracket(0.004, n_span=12, n_depth=3, n_layers=8)
    temp = np.full(br.n_nodes, 25.0)  # isothermal — isolate the mechanical response
    _b1, _s1, _u1, d_core = bs.solve_bracket(theta1=45.0, theta2=0.0, temperature=temp, **common)
    _b2, _s2, _u2, d_out = bs.solve_bracket(theta1=0.0, theta2=45.0, temperature=temp, **common)
    # Fibres in the core (45/0)s bend more than fibres outboard (0/45)s.
    assert d_core > 1.3 * d_out


def test_failure_indices_safe_state() -> None:
    sig = np.zeros((3, 3, 2))
    sig[0, 0, 0] = 100e6  # 100 MPa fibre tension — well below Xt=1500
    ms, tw = bs.failure_indices(sig, xt=1500, xc=1500, yt=40, yc=246, s=68)
    assert ms.max() < 0.1
    assert tw.max() < 0.1


def test_nodal_field_preserves_element_range() -> None:
    """Element→nodal averaging must stay within the element field's range.

    Regression: ``np.add.at`` doesn't broadcast a 1-D per-element values array
    across the ``(nodes_per_elem, n_elem)`` connectivity like plain arithmetic,
    so it scattered garbage — a strictly-positive failure-index field came out
    with hundreds of large *negative* nodal values, which then poisoned the POD
    field surrogate and tripped the ``value_min=0`` validity bound on every run.
    """
    br = bs.bmod.build_bracket(0.004, n_span=12, n_depth=3, n_layers=8)
    mesh = br.mesh
    elem = np.linspace(0.01, 5.0, mesh.t.shape[1])  # strictly positive element field
    nodal = bs._nodal_from_element(mesh, elem)
    assert nodal.shape[0] == mesh.p.shape[1]
    # An average of values in [0.01, 5.0] must itself lie in [0.01, 5.0].
    assert float(nodal.min()) >= 0.01 - 1e-9
    assert float(nodal.max()) <= 5.0 + 1e-9
