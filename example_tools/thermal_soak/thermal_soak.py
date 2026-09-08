"""Thermal Soak — transient anisotropic conduction on the composite L-bracket.

The middle tool of the NAFEMS showcase chain (`designs/showcase-tools.md` §4.2).
A real scikit-fem (BSD-3) finite-element transient heat-conduction solve on the
shared layered-hex bracket mesh:

    rho*cp dT/dt = div(K grad T)

with a heat-flux patch (a mounted component dissipating ``heat_load`` watts),
convective (Robin) cooling on the exposed surfaces, and an insulated mounting
foot. The conductivity tensor ``K`` is the laminate's effective orthotropic
conductivity (``kx, ky, kz`` wired from Composite Laminate) **rotated per
element** into the bracket's material frame (the rosette that follows the
flanges around the fold). Backward-Euler time stepping with a single
pre-factorised system.

Outputs:

* a **time-resolved temperature field** — one ``.vtu`` snapshot per step
  (``field_timesteps`` glob) the viewer animates and a POD field surrogate can
  fit with ``time`` as a factor;
* the **final** soaked field as a single ``.vtu`` — wired to Bracket Stress as
  its thermal load (file-wiring, ADR-0015);
* ``T_max`` / ``T_avg`` scalars.

Pure scikit-fem + numpy + scipy + meshio (all permissive). No gmsh.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from scipy.sparse.linalg import splu
from skfem import (
    Basis,
    BilinearForm,
    ElementHex1,
    FacetBasis,
    Functional,
    LinearForm,
)

import bracket_mesh as bmod

_N_STEPS = 20  # backward-Euler steps written as the time-resolved series
# Mesh resolution. MUST match Bracket Stress (the two FE tools share the
# bracket mesh node-for-node so the temperature field wires straight in).
_N_SPAN, _N_DEPTH, _N_LAYERS = 30, 6, 8


def _read_inputs() -> dict[str, Any]:
    payload = json.loads(Path("inputs/inputs.json").read_text(encoding="utf-8"))
    return {entry["name"]: entry["value"] for entry in payload["inputs"]}


def solve_soak(
    *,
    kx: float,
    ky: float,
    kz: float,
    rho: float,
    cp: float,
    thickness_m: float,
    heat_load: float,
    h_conv: float,
    ambient: float,
    duration: float,
    n_steps: int = _N_STEPS,
    n_span: int = _N_SPAN,
    n_depth: int = _N_DEPTH,
    n_layers: int = _N_LAYERS,
) -> tuple[bmod.BracketMesh, list[np.ndarray]]:
    """Run the transient conduction solve; return the mesh + nodal-T snapshots.

    ``snapshots[0]`` is the uniform ambient initial condition; the rest are the
    backward-Euler states (``n_steps`` of them).
    """
    bracket = bmod.build_bracket(
        thickness_m, n_span=n_span, n_depth=n_depth, n_layers=n_layers
    )
    mesh = bracket.mesh
    basis = Basis(mesh, ElementHex1())
    e1, e2, e3 = bracket.elem_e1, bracket.elem_e2, bracket.elem_e3  # (3, nelem)

    def _proj(e: np.ndarray, g: Any) -> Any:
        """(e . grad), broadcasting the per-element unit vector over quadrature."""
        return e[0][:, None] * g[0] + e[1][:, None] * g[1] + e[2][:, None] * g[2]

    @BilinearForm
    def conduction(u: Any, v: Any, _w: Any) -> Any:
        gu, gv = u.grad, v.grad
        return (
            kx * _proj(e1, gu) * _proj(e1, gv)
            + ky * _proj(e2, gu) * _proj(e2, gv)
            + kz * _proj(e3, gu) * _proj(e3, gv)
        )

    @BilinearForm
    def capacitance(u: Any, v: Any, _w: Any) -> Any:
        return rho * cp * u * v

    k_mat = conduction.assemble(basis)
    m_mat = capacitance.assemble(basis)

    # Convective (Robin) cooling on the exposed surfaces.
    fb_conv = FacetBasis(mesh, ElementHex1(), facets=bracket.convection_facets)

    @BilinearForm
    def robin(u: Any, v: Any, _w: Any) -> Any:
        return h_conv * u * v

    @LinearForm
    def robin_load(v: Any, _w: Any) -> Any:
        return h_conv * ambient * v

    h_mat = robin.assemble(fb_conv)
    f_conv = robin_load.assemble(fb_conv)

    # Heat flux into the mounted-component patch (Q spread over its area).
    fb_heat = FacetBasis(mesh, ElementHex1(), facets=bracket.heat_facets)

    @Functional
    def _one(_w: Any) -> float:
        return 1.0

    heat_area = float(_one.assemble(fb_heat))
    q_flux = heat_load / heat_area if heat_area > 0 else 0.0

    @LinearForm
    def heat_load_form(v: Any, _w: Any) -> Any:
        return q_flux * v

    f_heat = heat_load_form.assemble(fb_heat)

    load = f_conv + f_heat
    steady = (k_mat + h_mat).tocsc()
    dt = duration / n_steps
    lhs = (m_mat / dt + steady).tocsc()
    lu = splu(lhs)  # constant system: factorise once, reuse every step
    m_over_dt = (m_mat / dt).tocsr()

    temp = np.full(basis.N, float(ambient))
    snapshots = [temp.copy()]
    for _ in range(n_steps):
        temp = lu.solve(m_over_dt @ temp + load)
        snapshots.append(temp.copy())
    # Guard against a degenerate conduction system: the sparse solve can return
    # a non-finite (NaN/Inf) field instead of raising, which then flows Complete
    # into studies and poisons a POD temperature surrogate. Fail cleanly instead
    # (mirrors the Bracket Stress displacement guard).
    if not np.isfinite(snapshots[-1]).all():
        raise SystemExit(
            "The transient conduction solve produced a non-finite temperature "
            "field for these inputs (the system is degenerate — check the "
            "conductivity / density / thickness combination). No result was "
            "written; this run is marked failed rather than emitting a "
            "non-physical field."
        )
    return bracket, snapshots


def main() -> int:
    inputs = _read_inputs()
    bracket, snapshots = solve_soak(
        kx=float(inputs["kx"]),
        ky=float(inputs["ky"]),
        kz=float(inputs["kz"]),
        rho=float(inputs["rho"]),
        cp=float(inputs["cp"]),
        thickness_m=float(inputs["thickness"]) / 1000.0,  # mm -> m
        heat_load=float(inputs["heat_load"]),
        h_conv=float(inputs["h_conv"]),
        ambient=float(inputs["ambient"]),
        duration=float(inputs["duration"]),
    )
    mesh = bracket.mesh

    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)
    # Time-resolved series (one .vtu per step, distinct ``_step_`` prefix so the
    # series glob never catches the final field) + the final soaked field on
    # its own, which is what Bracket Stress wires in as its thermal load.
    for i, temp in enumerate(snapshots):
        mesh.save(
            str(out_dir / f"temperature_step_{i:04d}.vtu"), point_data={"temperature": temp}
        )
    final = snapshots[-1]
    mesh.save(str(out_dir / "temperature_final.vtu"), point_data={"temperature": final})

    document = {
        "schema_version": 1,
        "tool_alias": "thermal_soak",
        "outputs": [
            {
                "name": "t_max",
                "label": "Peak temperature",
                "type": "number",
                "unit": "degC",
                "value": round(float(final.max()), 2),
            },
            {
                "name": "t_avg",
                "label": "Mean temperature",
                "type": "number",
                "unit": "degC",
                "value": round(float(final.mean()), 2),
            },
            {
                "name": "temperature_series",
                "label": "Temperature field (transient)",
                "type": "file",
                "filename": "outputs/temperature_step_0000.vtu",
            },
            {
                "name": "temperature_final",
                "label": "Temperature field (final soak)",
                "type": "file",
                "filename": "outputs/temperature_final.vtu",
            },
        ],
    }
    (out_dir / "outputs.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
