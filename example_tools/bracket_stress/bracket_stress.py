"""Bracket Stress — layered-solid thermo-mechanical analysis of the L-bracket.

The terminal tool of the NAFEMS showcase chain (`designs/showcase-tools.md`
§4.3). A real scikit-fem (BSD-3) finite-element solve:

- **Layered solid** — one element layer per ply; each element carries its ply's
  orthotropic stiffness rotated into the bracket frame (the rosette that
  follows the flanges × the ply angle).
- **Thermo-mechanical load** — the wired **temperature field** from Thermal
  Soak drives per-ply thermal expansion, plus an external mechanical load at
  the free flange; the mounting flange is clamped.
- **Quadratic (Hex2) displacement** to avoid shear locking in the thin section.
- **First-ply failure** — ply stresses recovered in material axes and assessed
  by **both max-stress and Tsai-Wu** (composite criteria — *not* von Mises).

Outputs the nodal failure-index field (`.vtu`), the peak failure index, the
peak deflection, and the carried mass. Pure scikit-fem + numpy + meshio.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import meshio
import numpy as np
from skfem import (
    Basis,
    BilinearForm,
    ElementHex1,
    ElementHex2,
    ElementVector,
    FacetBasis,
    Functional,
    LinearForm,
    condense,
    solve,
)
from skfem.helpers import ddot, sym_grad

import bracket_mesh as bmod
import laminate_solid as ls

_N_SPAN, _N_DEPTH, _N_LAYERS = 30, 6, 8  # MUST match Thermal Soak (shared mesh)
# Quadratic (Hex2) displacement avoids shear locking; reduced (order-2)
# integration both speeds the solve and further relaxes locking. Shared with
# the temperature interpolation basis so their quadrature points align.
_INTORDER = 2
_MPA = 1.0e6
# Sanity ceiling on nodal displacement (metres). The bracket is ~cm-scale and
# deflects sub-mm under the rated load; anything past this is a diverged /
# singular solve, not a physical result. Generous (km-scale) so it never
# false-fails a real run — it only catches gross blow-ups.
_MAX_PHYSICAL_DISP_M = 1.0e3


def _read_inputs() -> dict[str, Any]:
    payload = json.loads(Path("inputs/inputs.json").read_text(encoding="utf-8"))
    return {entry["name"]: entry["value"] for entry in payload["inputs"]}


_TEMPERATURE_ARRAY = "temperature"


def _load_temperature_field(temp_path: str) -> np.ndarray:
    """Read the nodal temperature field from the supplied ``.vtu``.

    Fails with a clear, engineer-readable message (not a raw ``KeyError``)
    when the file carries no nodal ``temperature`` array — the usual cause is
    pointing this input at the wrong result file (e.g. a stress / failure
    field, which is also a ``.vtu``, instead of a Thermal Soak temperature
    field).
    """
    mesh = meshio.read(str(temp_path))
    if _TEMPERATURE_ARRAY not in mesh.point_data:
        available = ", ".join(mesh.point_data.keys()) or "(none)"
        raise SystemExit(
            f"The temperature-field file '{Path(temp_path).name}' has no nodal "
            f"'{_TEMPERATURE_ARRAY}' array (it carries: {available}). This input "
            f"needs a temperature field produced by the Thermal Soak tool — check "
            f"you didn't select a different result file (e.g. a stress or failure "
            f"field) for it."
        )
    return np.asarray(mesh.point_data[_TEMPERATURE_ARRAY], dtype=float)


def build_layup(theta1: float, theta2: float) -> list[float]:
    """The fixed ``[θ1/−θ1/θ2/−θ2]s`` 8-ply stack (matches Composite Laminate)."""
    unit = [theta1, -theta1, theta2, -theta2]
    return unit + unit[::-1]


def _per_element_rotation(bracket: bmod.BracketMesh, layup: list[float]) -> np.ndarray:
    """``Q`` (3, 3, nelem): ply→global direction cosines for every element."""
    nelem = bracket.n_elements
    q = np.empty((3, 3, nelem))
    for e in range(nelem):
        angle = layup[int(bracket.elem_layer[e]) % len(layup)]
        q[:, :, e] = ls.rotation_ply_to_global(
            bracket.elem_e1[:, e], bracket.elem_e2[:, e], bracket.elem_e3[:, e], angle
        )
    return q


def solve_bracket(
    *,
    e1: float, e2: float, g12: float, nu12: float,
    alpha1: float, alpha2: float,
    theta1: float, theta2: float,
    thickness_m: float,
    t_ref: float,
    mech_load: float,
    temperature: np.ndarray | None = None,
    n_span: int = _N_SPAN, n_depth: int = _N_DEPTH, n_layers: int = _N_LAYERS,
) -> tuple[bmod.BracketMesh, np.ndarray, np.ndarray, float]:
    """Solve the thermo-mechanical problem.

    ``temperature`` is the nodal field (mesh-node order). ``None`` means an
    **isothermal** (mechanical-only) solve — every node at ``t_ref``, so the
    thermal load vanishes. Returns ``(bracket, sigma_material,
    displacement_field, max_deflection_m)`` where ``sigma_material`` is
    ``(3, 3, nelem)`` ply-axis stress per element.
    """
    bracket = bmod.build_bracket(thickness_m, n_span=n_span, n_depth=n_depth, n_layers=n_layers)
    mesh = bracket.mesh
    n_nodes = mesh.p.shape[1]
    if temperature is None:
        temperature = np.full(n_nodes, float(t_ref))
    elif temperature.shape[0] != n_nodes:
        raise SystemExit(
            f"The supplied temperature field has {temperature.shape[0]} nodes but "
            f"this bracket mesh has {n_nodes}. The temperature field must come from "
            f"a Thermal Soak run on the same bracket mesh (matching thickness and "
            f"mesh resolution) so the nodes line up."
        )
    vbasis = Basis(mesh, ElementVector(ElementHex2()), intorder=_INTORDER)
    tbasis = Basis(mesh, ElementHex1(), intorder=_INTORDER)

    layup = build_layup(theta1, theta2)
    q = _per_element_rotation(bracket, layup)  # (3,3,nelem)
    c_mat = ls.ply_stiffness_tensor(e1, e2, g12, nu12)
    cglob = ls.global_stiffness_field(c_mat, q)  # (3,3,3,3,nelem)

    # Per-element thermal stress / strain (rotated CTE).
    alpha_mat = np.diag([alpha1, alpha2, alpha2]) * 1e-6
    alpha_glob = np.einsum("iae,ab,jbe->ije", q, alpha_mat, q)  # (3,3,nelem)
    beta = np.einsum("ijkle,kle->ije", cglob, alpha_glob)  # thermal stress per degree

    dt_nodal = temperature - t_ref

    @BilinearForm
    def elastic(u: Any, v: Any, _w: Any) -> Any:
        eu, ev = sym_grad(u), sym_grad(v)
        sig = np.einsum("ijkle,klen->ijen", cglob, eu)
        return ddot(sig, ev)

    @LinearForm
    def thermal(v: Any, w: Any) -> Any:
        ev = sym_grad(v)
        sig_th = beta[:, :, :, None] * w["dt"]
        return ddot(sig_th, ev)

    k_mat = elastic.assemble(vbasis)
    f_th = thermal.assemble(vbasis, dt=tbasis.interpolate(dt_nodal))

    # Mechanical traction (in +X) over the free-flange end face.
    fb_load = FacetBasis(mesh, ElementVector(ElementHex2()), facets=bracket.load_facets)
    fb_area = FacetBasis(mesh, ElementHex1(), facets=bracket.load_facets)

    @Functional
    def _one(_w: Any) -> float:
        return 1.0

    load_area = float(_one.assemble(fb_area))
    traction_x = mech_load / load_area if load_area > 0 else 0.0

    @LinearForm
    def mech(v: Any, _w: Any) -> Any:
        return traction_x * v[0]

    f_mech = mech.assemble(fb_load)

    clamped = vbasis.get_dofs(facets=bracket.clamp_facets).flatten()
    u = solve(*condense(k_mat, f_th + f_mech, D=clamped))
    # Guard against a singular / ill-conditioned stiffness system: the sparse
    # solve can return a non-finite (NaN/Inf) or astronomically large
    # displacement instead of raising, which then propagates as a poisoned
    # failure-index field (NaN / ~1e300) that breaks every downstream consumer
    # (e.g. POD field surrogates). Fail the run cleanly instead.
    if not np.isfinite(u).all() or float(np.abs(u).max()) > _MAX_PHYSICAL_DISP_M:
        raise SystemExit(
            "The finite-element solve did not produce a physical displacement "
            "field for these inputs (the stiffness system is singular or ill-"
            "conditioned — check the thickness / modulus combination). No result "
            "was written; this run is marked failed rather than emitting "
            "non-physical stresses."
        )

    # --- Stress recovery in ply material axes (element averages) ---
    ub = vbasis.interpolate(u)
    gradu = ub.grad  # (3,3,nelem,nqp)
    eps = 0.5 * (gradu + np.swapaxes(gradu, 0, 1))
    eps_mean = eps.mean(axis=-1)  # (3,3,nelem)
    dt_mean = np.asarray(tbasis.interpolate(dt_nodal)).mean(axis=-1)  # (nelem,)
    eps_th = alpha_glob * dt_mean[None, None, :]
    sig_glob = np.einsum("ijkle,kle->ije", cglob, eps_mean - eps_th)  # (3,3,nelem)
    sig_mat = np.einsum("iae,ije,jbe->abe", q, sig_glob, q)  # ply axes

    disp_mag = np.sqrt((np.asarray(ub) ** 2).sum(axis=0))  # (nelem,nqp)
    max_defl = float(disp_mag.max())
    return bracket, sig_mat, u, max_defl


def failure_indices(
    sig_mat: np.ndarray, *, xt: float, xc: float, yt: float, yc: float, s: float
) -> tuple[np.ndarray, np.ndarray]:
    """Per-element ``(max_stress, tsai_wu)`` first-ply-failure indices."""
    s1 = sig_mat[0, 0] / _MPA
    s2 = sig_mat[1, 1] / _MPA
    t12 = sig_mat[0, 1] / _MPA
    fibre = np.where(s1 >= 0, s1 / xt, -s1 / xc)
    trans = np.where(s2 >= 0, s2 / yt, -s2 / yc)
    shear = np.abs(t12) / s
    ms = np.maximum(np.maximum(fibre, trans), shear)
    f1, f2 = 1.0 / xt - 1.0 / xc, 1.0 / yt - 1.0 / yc
    f11, f22, f66 = 1.0 / (xt * xc), 1.0 / (yt * yc), 1.0 / (s * s)
    f12 = -0.5 * np.sqrt(f11 * f22)
    tw = f1 * s1 + f2 * s2 + f11 * s1**2 + f22 * s2**2 + f66 * t12**2 + 2 * f12 * s1 * s2
    return ms, tw


def _nodal_from_element(mesh: Any, elem_values: np.ndarray) -> np.ndarray:
    """Average an element-wise field onto the mesh nodes (for the .vtu field).

    ``mesh.t`` is ``(nodes_per_elem, n_elem)`` (scikit-fem) and ``elem_values`` is
    one value per element. ``np.add.at`` does NOT broadcast a 1-D values array
    across a 2-D index array like plain ufunc arithmetic does — passing the raw
    ``(n_elem,)`` array scatters garbage (a smooth non-negative element field
    came out with large negative nodal values). Broadcast the per-element value
    to each of its nodes explicitly first.
    """
    nodal = np.zeros(mesh.p.shape[1])
    count = np.zeros(mesh.p.shape[1])
    np.add.at(nodal, mesh.t, np.broadcast_to(elem_values, mesh.t.shape))
    np.add.at(count, mesh.t, 1.0)
    return nodal / np.maximum(count, 1.0)


def main() -> int:
    inputs = _read_inputs()
    temp_path = inputs.get("temperature_field")
    # No field supplied -> isothermal (mechanical-only) check.
    temperature = _load_temperature_field(str(temp_path)) if temp_path else None

    bracket, sig_mat, _u, max_defl = solve_bracket(
        e1=float(inputs["e1"]), e2=float(inputs["e2"]), g12=float(inputs["g12"]),
        nu12=float(inputs["nu12"]),
        alpha1=float(inputs["alpha1"]), alpha2=float(inputs["alpha2"]),
        theta1=float(inputs["theta1"]), theta2=float(inputs["theta2"]),
        thickness_m=float(inputs["thickness"]) / 1000.0,
        temperature=temperature,
        t_ref=float(inputs["t_ref"]),
        mech_load=float(inputs["mech_load"]),
    )
    ms, tw = failure_indices(
        sig_mat,
        xt=float(inputs["xt"]), xc=float(inputs["xc"]),
        yt=float(inputs["yt"]), yc=float(inputs["yc"]), s=float(inputs["s_shear"]),
    )
    fi = np.maximum(ms, tw)
    fi_max = float(fi.max())

    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)
    nodal_fi = _nodal_from_element(bracket.mesh, fi)
    bracket.mesh.save(
        str(out_dir / "failure_index.vtu"), point_data={"failure_index": nodal_fi}
    )
    # Companion scalar for the field: its peak nodal value. It rides the
    # output-validity gate (ADR-0017) so a run whose *field* is non-physical
    # (NaN / blown-up nodes) is flagged from this number alone, without loading
    # the .vtu — even when the element-based fi_max looks sane.
    field_max = float(nodal_fi.max())

    document = {
        "schema_version": 1,
        "tool_alias": "bracket_stress",
        "outputs": [
            {"name": "fi_max", "label": "Peak failure index", "type": "number",
             "value": round(fi_max, 4)},
            {"name": "field_max", "label": "Peak field failure index", "type": "number",
             "value": round(field_max, 4)},
            {"name": "max_stress_index", "label": "Peak max-stress index", "type": "number",
             "value": round(float(ms.max()), 4)},
            {"name": "tsai_wu_index", "label": "Peak Tsai-Wu index", "type": "number",
             "value": round(float(tw.max()), 4)},
            {"name": "max_deflection", "label": "Peak deflection", "type": "number",
             "unit": "mm", "value": round(max_defl * 1000.0, 4)},
            {"name": "mass", "label": "Bracket mass", "type": "number", "unit": "g",
             "value": round(float(inputs["mass"]), 2)},
            {"name": "failure_field", "label": "Failure-index field", "type": "file",
             "filename": "outputs/failure_index.vtu"},
        ],
    }
    (out_dir / "outputs.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
