"""Finned Cold Plate — steady conduction through a fan-cooled baseplate.

The single tool behind the NAFEMS 2026 showcase (`designs/finned-plate-tool.md`). A
component dissipating a fixed heat load is mounted on the underside of a metal plate;
the finned upper surface sheds that heat into air driven by a fan. A real scikit-fem
(BSD-3) finite-element solve of steady conduction:

    div(k grad T) = 0

with a uniform heat flux over the component footprint on the underside, a Robin
(convective) condition on the top face carrying the fin array's effective coefficient,
and adiabatic edges and remaining underside — the plate is taken to be mounted against
an insulating enclosure.

**What the finite-element solve earns its place doing.** The fin array is a closed-form
lumped conductance (:mod:`fin_model`), so a purely analytic tool could report the mean
temperature. What it could *not* report is **spreading resistance**: the heat enters
over a 30 mm square and must fan out through the plate to reach a 100 mm square of
fins. That constriction scales as ``1/(k*thickness)``, so it is what makes base
thickness a real design variable, what puts a hot spot in the field, and what makes a
lower-conductivity metal cost something despite being lighter. It is genuinely a
field problem, which is why it is solved as one.

Outputs: peak component temperature, mass, thermal resistance, the derived air velocity
and fan power draw, a steady ``.vtu`` nodal temperature field, and a to-scale schematic
of the design that was run.

Pure scikit-fem + numpy + scipy + Pillow (all permissive). No gmsh.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fin_model as fmod
import numpy as np
import plate_mesh as pmod
from schematic import write_schematic
from scipy.sparse.linalg import splu
from skfem import Basis, BilinearForm, ElementHex1, FacetBasis, Functional, LinearForm

#: Steady power dissipated by the mounted component. A tool-author constant, not a
#: design variable — exposing it would push the factor count past what a readable
#: coverage scatter matrix can carry (`designs/finned-plate-tool.md` §5).
HEAT_LOAD_W: float = 60.0

#: Production mesh density — 59,049 nodes, about 20 s per solve.
#:
#: **Must be identical for every run in a study.** The field surrogate compares fields
#: node-for-node, so changing this invalidates an existing archive rather than merely
#: refining it. ``n_x`` and ``n_y`` must be multiples of 20 (see
#: :func:`plate_mesh.build_plate`).
#:
#: Chosen for **field resolution**, not for accuracy: the peak temperature is already
#: converged to under 0.01 K on a mesh a quarter this size. What the finer mesh buys is
#: a 6,561-point top surface that renders as a smooth contour rather than a coarse
#: chequerboard, and a solve time long enough that replacing it with a millisecond
#: surrogate prediction is a fair comparison rather than a flattering one.
_N_X, _N_Y, _N_Z = 80, 80, 8


def solve_plate(
    *,
    fin_count: int,
    fin_height_m: float,
    base_thickness_m: float,
    fan_duty: float,
    material: fmod.Material,
    heat_load_w: float = HEAT_LOAD_W,
    n_x: int = _N_X,
    n_y: int = _N_Y,
    n_z: int = _N_Z,
) -> tuple[pmod.PlateMesh, np.ndarray, fmod.FinArray]:
    """Solve steady conduction through the plate; return mesh, nodal T, fin array."""
    array = fmod.evaluate(
        fin_count=fin_count,
        fin_height_m=fin_height_m,
        base_thickness_m=base_thickness_m,
        fan_duty=fan_duty,
        material=material,
    )
    plate = pmod.build_plate(base_thickness_m, n_x=n_x, n_y=n_y, n_z=n_z)
    mesh = plate.mesh
    basis = Basis(mesh, ElementHex1())

    conductivity = material.conductivity
    h_eff = array.h_effective
    air = fmod.AIR_INLET_C

    @BilinearForm
    def conduction(u: Any, v: Any, _w: Any) -> Any:
        gu, gv = u.grad, v.grad
        return conductivity * (gu[0] * gv[0] + gu[1] * gv[1] + gu[2] * gv[2])

    k_mat = conduction.assemble(basis)

    # Convective (Robin) cooling on the finned top face.
    fb_top = FacetBasis(mesh, ElementHex1(), facets=plate.top_facets)

    @BilinearForm
    def robin(u: Any, v: Any, _w: Any) -> Any:
        return h_eff * u * v

    @LinearForm
    def robin_load(v: Any, _w: Any) -> Any:
        return h_eff * air * v

    h_mat = robin.assemble(fb_top)
    f_conv = robin_load.assemble(fb_top)

    # Uniform heat flux over the component footprint on the underside.
    fb_comp = FacetBasis(mesh, ElementHex1(), facets=plate.component_facets)

    @Functional
    def _one(_w: Any) -> float:
        return 1.0

    comp_area = float(_one.assemble(fb_comp))
    if comp_area <= 0.0:
        raise SystemExit("the component footprint has no area — the mesh is degenerate")
    q_flux = heat_load_w / comp_area

    @LinearForm
    def heat_load_form(v: Any, _w: Any) -> Any:
        return q_flux * v

    f_heat = heat_load_form.assemble(fb_comp)

    lhs = (k_mat + h_mat).tocsc()
    temperature = splu(lhs).solve(f_conv + f_heat)

    # Guard against a degenerate system: the sparse solve can return a non-finite
    # field instead of raising, which would then flow Complete into studies and poison
    # a POD temperature surrogate. Fail cleanly instead — a confidently wrong result is
    # exactly the failure this showcase exists to warn about.
    if not np.isfinite(temperature).all():
        raise SystemExit(
            "The steady conduction solve produced a non-finite temperature field for "
            "these inputs (the system is degenerate — check the fin geometry and "
            "material combination). No result was written; this run is marked failed "
            "rather than emitting a non-physical field."
        )
    if temperature.min() < air - 1.0:
        raise SystemExit(
            f"The solve produced a temperature below the air inlet ({air} degC), which "
            "is not physically possible with a positive heat load. No result was "
            "written."
        )
    return plate, temperature, array


def _read_inputs() -> dict[str, Any]:
    payload = json.loads(Path("inputs/inputs.json").read_text(encoding="utf-8"))
    return {entry["name"]: entry["value"] for entry in payload["inputs"]}


def main() -> int:
    """Read the staged inputs, solve, and write the outputs document."""
    inputs = _read_inputs()
    material_name = str(inputs["material"])
    if material_name not in fmod.MATERIALS:
        raise SystemExit(
            f"unknown material {material_name!r}; expected one of {sorted(fmod.MATERIALS)}"
        )
    material = fmod.MATERIALS[material_name]
    fin_count = round(float(inputs["fin_count"]))
    fin_height_mm = float(inputs["fin_height"])
    base_thickness_mm = float(inputs["base_thickness"])
    fan_duty = float(inputs["fan_duty"]) / 100.0  # percent -> fraction

    plate, temperature, array = solve_plate(
        fin_count=fin_count,
        fin_height_m=fin_height_mm / 1000.0,
        base_thickness_m=base_thickness_mm / 1000.0,
        fan_duty=fan_duty,
        material=material,
    )

    point = array.operating_point
    t_source = float(temperature.max())
    t_mean = float(temperature.mean())
    thermal_resistance = (t_source - fmod.AIR_INLET_C) / HEAT_LOAD_W

    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)
    # The field file carries the SOLVED baseplate only. Fin geometry is display-only
    # and must never be added here: the field surrogate ingests this nodal array, and
    # unsolved nodes would corrupt it (`designs/finned-plate-tool.md` §7a).
    plate.mesh.save(str(out_dir / "temperature_field.vtu"), point_data={"temperature": temperature})

    write_schematic(
        out_dir / "design_schematic.png",
        section=pmod.cross_section_grid(plate, temperature),
        fin_count=fin_count,
        fin_height_mm=fin_height_mm,
        base_thickness_mm=base_thickness_mm,
        fan_duty_pct=float(inputs["fan_duty"]),
        material=material,
        t_source=t_source,
        mass_g=array.mass_g,
        velocity_ms=point.velocity_ms,
        turbulent=array.turbulent,
    )

    document = {
        "schema_version": 1,
        "tool_alias": "finned_plate",
        "outputs": [
            {
                "name": "t_source",
                "label": "Peak component temperature",
                "type": "number",
                "unit": "degC",
                "value": round(t_source, 2),
            },
            {
                "name": "mass",
                "label": "Plate + fin mass",
                "type": "number",
                "unit": "g",
                "value": round(array.mass_g, 2),
            },
            {
                "name": "thermal_resistance",
                "label": "Thermal resistance",
                "type": "number",
                "unit": "K/W",
                "value": round(thermal_resistance, 4),
            },
            {
                "name": "t_mean",
                "label": "Mean plate temperature",
                "type": "number",
                "unit": "degC",
                "value": round(t_mean, 2),
            },
            {
                "name": "air_velocity",
                "label": "Channel air velocity",
                "type": "number",
                "unit": "m/s",
                "value": round(point.velocity_ms, 3),
            },
            {
                "name": "fan_power",
                "label": "Fan power draw",
                "type": "number",
                "unit": "W",
                "value": round(point.fan_power_w, 4),
            },
            {
                "name": "temperature_field",
                "label": "Plate temperature field",
                "type": "file",
                "filename": "outputs/temperature_field.vtu",
            },
            {
                "name": "design_schematic",
                "label": "Design schematic",
                "type": "file",
                "filename": "outputs/design_schematic.png",
            },
        ],
    }
    (out_dir / "outputs.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
