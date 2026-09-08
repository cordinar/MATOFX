"""Correctness tests for the Composite Laminate CLT engine.

These are the *defensibility gate* for the NAFEMS showcase laminate tool: the
maths must reproduce textbook composite results, not merely run. Checks:

- a UD ``[0]`` laminate returns the lamina constants (Ex=E1, Ey=E2, nuxy=nu12,
  alphax=alpha1, alphay=alpha2);
- a balanced symmetric layup has ``A16=A26~0`` and ``B~0`` (the well-behaved
  laminate the design relies on);
- a ``[0/90]`` cross-ply is in-plane square-symmetric (Ex=Ey);
- the directional modulus rotates correctly (E(0)=E1, E(90)=E2 for UD);
- mass scales linearly with the ply-repeat count (the thickness lever).

The engine module is imported directly from the tool bundle.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_ENGINE = (
    Path(__file__).resolve().parents[2] / "example_tools" / "composite_laminate" / "laminate_clt.py"
)
_spec = importlib.util.spec_from_file_location("laminate_clt", _ENGINE)
assert _spec is not None
assert _spec.loader is not None
clt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = clt  # dataclass needs the module registered
_spec.loader.exec_module(clt)


def test_layup_is_symmetric_balanced() -> None:
    angles = clt.build_layup(45.0, 0.0)
    assert len(angles) == clt.N_PLIES == 8  # fixed 8-ply
    assert angles == angles[::-1]  # symmetric
    assert sum(angles) == pytest.approx(0.0)  # balanced: every +theta has a -theta


def test_ud_laminate_returns_lamina_constants() -> None:
    lam = clt.LAMINA["CFRP"]
    angles = clt.build_layup(0.0, 0.0)  # all 0 deg
    a, _b, _d, h = clt.abd_matrices(lam, angles, 0.004)
    ex, ey, gxy, nuxy = clt.effective_constants(a, h)
    assert ex == pytest.approx(lam.e1, rel=1e-6)
    assert ey == pytest.approx(lam.e2, rel=1e-6)
    assert gxy == pytest.approx(lam.g12, rel=1e-6)
    assert nuxy == pytest.approx(lam.nu12, rel=1e-6)
    alphax, alphay = clt.effective_cte(lam, angles, a, 0.004)
    assert alphax == pytest.approx(lam.alpha1, abs=0.05)
    assert alphay == pytest.approx(lam.alpha2, rel=1e-3)


def test_balanced_symmetric_has_no_coupling() -> None:
    lam = clt.LAMINA["CFRP"]
    angles = clt.build_layup(45.0, 30.0)
    a, b, _d, _h = clt.abd_matrices(lam, angles, 0.004)
    scale = abs(a[0, 0])
    # Balanced: no shear-extension coupling.
    assert abs(a[0, 2]) / scale < 1e-9
    assert abs(a[1, 2]) / scale < 1e-9
    # Symmetric: no bend-stretch coupling (B ~ 0).
    assert np.max(np.abs(b)) / scale < 1e-6


def test_cross_ply_is_square_symmetric() -> None:
    lam = clt.LAMINA["CFRP"]
    angles = clt.build_layup(0.0, 90.0)  # 50% 0 deg, 50% 90 deg
    a, _b, _d, h = clt.abd_matrices(lam, angles, 0.004)
    ex, ey, _gxy, _nuxy = clt.effective_constants(a, h)
    assert ex == pytest.approx(ey, rel=1e-9)


def test_directional_modulus_rotates() -> None:
    lam = clt.LAMINA["GFRP"]
    angles = clt.build_layup(0.0, 0.0)  # UD
    assert clt.directional_modulus(lam, angles, 0.0) == pytest.approx(lam.e1, rel=1e-6)
    assert clt.directional_modulus(lam, angles, 90.0) == pytest.approx(lam.e2, rel=1e-6)


def test_mass_scales_linearly_with_thickness() -> None:
    s2 = clt.summarise("CFRP", 45.0, 0.0, 2.0)
    s4 = clt.summarise("CFRP", 45.0, 0.0, 4.0)
    assert s2.n_plies == s4.n_plies == 8  # fixed 8-ply, only thickness scales
    assert s4.mass_g == pytest.approx(2.0 * s2.mass_g, rel=1e-9)
    # Effective moduli are thickness-independent (CLT).
    assert s4.ex == pytest.approx(s2.ex, rel=1e-12)


def test_baseline_cfrp_mass_is_plausible() -> None:
    # 4 mm thickness; mass = rho * midsurface_area * t,
    # midsurface area = path_length * depth = 0.0077425 m2 (fillet-shortened).
    s = clt.summarise("CFRP", 45.0, 0.0, 4.0)
    assert s.thickness_mm == pytest.approx(4.0, rel=1e-9)
    assert s.mass_g == pytest.approx(49.55, abs=0.05)  # 1600 * 0.0077425 * 0.004 * 1000


def test_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="material"):
        clt.summarise("STEEL", 45.0, 0.0, 4.0)
    with pytest.raises(ValueError, match="theta"):
        clt.summarise("CFRP", 120.0, 0.0, 4.0)
