"""Physics-correctness tests for the Thermal Soak transient conduction solver.

The NAFEMS showcase needs this FE solve to be *right*, not just to run. Checks:

- the initial state is uniform ambient and the bracket heats up monotonically,
  staying bounded above ambient;
- **energy is conserved** — at near-steady state the applied heat load equals
  the convective heat shed over the cooled surfaces (the strongest check);
- the **thermo-structural coupling** is real — a low-conductivity GFRP laminate
  runs hotter than CFRP under the same load, so it can violate a temperature
  limit a stiffer/cooler CFRP design meets.

Run on a small mesh for speed; the production tool uses a finer one.
"""

from __future__ import annotations

import importlib.util
import sys
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

skfem = pytest.importorskip("skfem")

_BUNDLE = Path(__file__).resolve().parents[2] / "example_tools" / "thermal_soak"
sys.path.insert(0, str(_BUNDLE))  # so the solver can import its sibling bracket_mesh
_spec = importlib.util.spec_from_file_location("thermal_soak", _BUNDLE / "thermal_soak.py")
assert _spec is not None
assert _spec.loader is not None
ts = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ts
_spec.loader.exec_module(ts)

_SMALL = {"n_span": 24, "n_depth": 6, "n_layers": 4}
# CFRP / GFRP effective-conductivity baselines (W/m/K) + thermal mass.
_CFRP = {"kx": 4.0, "ky": 4.0, "kz": 0.6, "rho": 1600.0, "cp": 800.0}
_GFRP = {"kx": 0.9, "ky": 0.9, "kz": 0.5, "rho": 2100.0, "cp": 1000.0}


def test_initial_uniform_and_monotonic_heatup() -> None:
    _bracket, snaps = ts.solve_soak(
        **_CFRP,
        thickness_m=0.004,
        heat_load=6.0,
        h_conv=30.0,
        ambient=25.0,
        duration=600.0,
        n_steps=12,
        **_SMALL,
    )
    assert np.allclose(snaps[0], 25.0)  # uniform ambient initial condition
    peaks = [float(s.max()) for s in snaps]
    assert all(b >= a - 1e-6 for a, b in pairwise(peaks))  # monotonic heat-up
    final = snaps[-1]
    assert final.min() >= 25.0 - 1e-6  # nothing cools below ambient
    assert final.max() < 250.0  # bounded / sane


def test_energy_is_conserved_at_steady_state() -> None:
    from skfem import ElementHex1, FacetBasis, Functional  # noqa: PLC0415

    heat_load, h_conv, ambient = 6.0, 30.0, 25.0
    bracket, snaps = ts.solve_soak(
        **_CFRP,
        thickness_m=0.004,
        heat_load=heat_load,
        h_conv=h_conv,
        ambient=ambient,
        duration=40000.0,
        n_steps=60,
        **_SMALL,  # long enough to approach steady state
    )
    final = snaps[-1]
    # Convective heat shed = h * integral over the cooled faces of (T - T_amb).
    fb = FacetBasis(bracket.mesh, ElementHex1(), facets=bracket.convection_facets)

    @Functional
    def shed(w: object) -> object:
        return h_conv * (w["temp"] - ambient)

    q_out = float(shed.assemble(fb, temp=fb.interpolate(final)))
    # Heat in (the applied load) must balance heat out to a few percent.
    assert q_out == pytest.approx(heat_load, rel=0.05)


def test_low_conductivity_runs_hotter() -> None:
    common = dict(
        thickness_m=0.004,
        heat_load=6.0,
        h_conv=30.0,
        ambient=25.0,
        duration=3600.0,
        n_steps=30,
        **_SMALL,
    )
    _b1, cfrp = ts.solve_soak(**_CFRP, **common)
    _b2, gfrp = ts.solve_soak(**_GFRP, **common)
    # GFRP's lower conductivity concentrates heat -> higher peak temperature.
    assert gfrp[-1].max() > cfrp[-1].max() + 5.0
