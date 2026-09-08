"""Correctness tests for the Bracket Stress orthotropic constitutive.

The rotation algebra is the error-prone core, so it gets pinned hard:

- the ply stiffness tensor has the right symmetries and a fibre-dominated
  ``C_1111``;
- an **isotropic** material is rotation-invariant (the classic check that the
  4th-order rotation is correct);
- rotating a UD ply 90° about the through-thickness axis swaps the in-/out-of-
  fibre stiffnesses (``C'_xxxx`` = the ply's transverse ``C_2222``);
- the failure indices hit 1 exactly at the measured strengths.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_MOD = (
    Path(__file__).resolve().parents[2] / "example_tools" / "bracket_stress" / "laminate_solid.py"
)
_spec = importlib.util.spec_from_file_location("laminate_solid", _MOD)
assert _spec is not None
assert _spec.loader is not None
ls = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ls
_spec.loader.exec_module(ls)

# CFRP T300/5208.
_E1, _E2, _G12, _NU12 = 181.0, 10.3, 7.17, 0.28
_X = {"xt": 1500.0, "xc": 1500.0, "yt": 40.0, "yc": 246.0, "s": 68.0}
_AX = np.array([1.0, 0.0, 0.0])
_AY = np.array([0.0, 1.0, 0.0])
_AZ = np.array([0.0, 0.0, 1.0])


def test_stiffness_symmetries_and_fibre_dominance() -> None:
    c = ls.ply_stiffness_tensor(_E1, _E2, _G12, _NU12)
    assert np.allclose(c, np.swapaxes(c, 0, 1))  # minor symmetry i<->j
    assert np.allclose(c, np.swapaxes(c, 2, 3))  # minor symmetry k<->l
    assert np.allclose(c, np.einsum("klij->ijkl", c))  # major symmetry
    # Fibre direction is far stiffer than transverse.
    assert c[0, 0, 0, 0] > 5.0 * c[1, 1, 1, 1]


def test_isotropic_material_is_rotation_invariant() -> None:
    e, nu = 70.0, 0.33
    g = e / (2.0 * (1.0 + nu))
    iso = ls.ply_stiffness_tensor(e, e, g, nu, nu23=nu)
    # An arbitrary orthonormal rotation.
    rng = np.random.default_rng(0)
    a = rng.standard_normal((3, 3))
    q, _ = np.linalg.qr(a)
    rotated = ls.rotate_stiffness(iso, q)
    assert np.allclose(rotated, iso, atol=1e-3 * iso.max())


def test_identity_rotation_is_noop() -> None:
    c = ls.ply_stiffness_tensor(_E1, _E2, _G12, _NU12)
    q = ls.rotation_ply_to_global(_AX, _AY, _AZ, 0.0)
    assert np.allclose(ls.rotate_stiffness(c, q), c)


def test_ninety_degree_rotation_swaps_fibre_axis() -> None:
    c = ls.ply_stiffness_tensor(_E1, _E2, _G12, _NU12)
    q = ls.rotation_ply_to_global(_AX, _AY, _AZ, 90.0)  # fibre now along global Y
    rot = ls.rotate_stiffness(c, q)
    # Global-X stiffness now equals the ply's transverse stiffness.
    assert rot[0, 0, 0, 0] == pytest.approx(c[1, 1, 1, 1], rel=1e-9)
    assert rot[1, 1, 1, 1] == pytest.approx(c[0, 0, 0, 0], rel=1e-9)


def test_thermal_stress_is_symmetric_diagonal_for_aligned_ply() -> None:
    c = ls.ply_stiffness_tensor(_E1, _E2, _G12, _NU12)
    q = ls.rotation_ply_to_global(_AX, _AY, _AZ, 0.0)
    beta = ls.thermal_stress_per_degree(c, q, 0.02, 22.5)
    assert np.allclose(beta, beta.T)  # symmetric
    assert np.allclose(beta - np.diag(np.diag(beta)), 0.0, atol=1e-6 * abs(beta).max())


def test_failure_indices_hit_one_at_strengths() -> None:
    mpa = 1.0e6
    # Pure fibre tension at Xt → both criteria ~1.
    assert ls.max_stress_index(1500 * mpa, 0.0, 0.0, **_X) == pytest.approx(1.0, rel=1e-9)
    assert ls.tsai_wu_index(1500 * mpa, 0.0, 0.0, **_X) == pytest.approx(1.0, rel=1e-6)
    # Pure shear at S → max-stress = 1.
    assert ls.max_stress_index(0.0, 0.0, 68 * mpa, **_X) == pytest.approx(1.0, rel=1e-9)
    # A benign small stress state is well below 1.
    assert ls.tsai_wu_index(100 * mpa, 5 * mpa, 5 * mpa, **_X) < 0.2
