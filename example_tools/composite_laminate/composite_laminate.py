"""Composite Laminate — the material-homogeniser tool of the NAFEMS showcase chain.

Defines a symmetric balanced layup ``[(±θ₁/±θ₂)ₙ]s`` of a **measured** UD lamina
(CFRP T300/5208 or GFRP E-glass/epoxy, Daniel & Ishai) and emits the **material
card** the downstream Thermal Soak / Bracket Stress solvers apply per ply:

* the per-ply lamina properties (stiffness, CTE, strengths, conductivity, ρ, cₚ),
* the layup (θ₁, θ₂, repeats → thickness) and resulting **mass**,
* the laminate-level effective constants / CTE / conductivity (CLT diagnostics),
* two renderings — an exploded **ply-stack** and the iconic directional-modulus
  **polar plot**.

All maths lives in :mod:`laminate_clt` (verified against textbook results); this
script is just the inputs/outputs.json glue + rendering. Pure numpy (BSD-3) +
plotly (MIT) — both permissive, already MATOFX deps. No solver, runs instantly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import plotly.graph_objects as go

import laminate_clt as clt

_POLAR_SAMPLES = 181  # directional-modulus sweep resolution (0–360°)


def _read_inputs() -> dict[str, Any]:
    payload = json.loads(Path("inputs/inputs.json").read_text(encoding="utf-8"))
    return {entry["name"]: entry["value"] for entry in payload["inputs"]}


def _angle_colour(theta: float) -> str:
    """Map a ply angle (deg) to a colour — a perceptual ramp 0°→90°."""
    t = min(1.0, max(0.0, abs(theta) / 90.0))
    stops = ((0.0, (33, 102, 172)), (0.5, (146, 197, 222)), (1.0, (178, 24, 43)))
    for i in range(len(stops) - 1):
        p0, c0 = stops[i]
        p1, c1 = stops[i + 1]
        if t <= p1:
            f = 0.0 if p1 == p0 else (t - p0) / (p1 - p0)
            r, g, b = (int(a + (c1[j] - a) * f) for j, a in enumerate(c0))
            return f"rgb({r},{g},{b})"
    return "rgb(178,24,43)"


def _write_ply_stack(out_dir: Path, summary: clt.LaminateSummary) -> None:
    """Exploded ply-stack: one bar per ply through the thickness, coloured by angle."""
    angles = summary.angles
    n = len(angles)
    t_ply = summary.thickness_mm / n
    fig = go.Figure()
    for k, theta in enumerate(angles):
        z0 = (k - n / 2.0) * t_ply
        fig.add_trace(
            go.Bar(
                x=[1.0],
                y=[t_ply],
                base=[z0],
                orientation="v",
                marker={"color": _angle_colour(theta), "line": {"color": "white", "width": 0.5}},
                hovertemplate=f"Ply {k + 1}/{n}<br>{theta:+.0f}°<extra></extra>",
                showlegend=False,
            )
        )
    fig.update_layout(
        title={"text": f"Ply stack — {summary.material} {_layup_label(summary)} ({n} plies)"},
        xaxis={"visible": False, "range": [0, 1]},
        yaxis={"title": {"text": "Through-thickness position (mm)"}},
        bargap=0.0,
        paper_bgcolor="white",
        plot_bgcolor="white",
        font={"color": "#1f2937"},
        margin={"l": 70, "r": 20, "t": 50, "b": 30},
    )
    fig.write_html(out_dir / "ply_stack.html", include_plotlyjs="cdn", full_html=True)


def _write_polar_modulus(out_dir: Path, summary: clt.LaminateSummary) -> None:
    """Directional effective modulus Ex(φ) — the iconic composites polar plot."""
    lam = summary.lamina
    phis = np.linspace(0.0, 360.0, _POLAR_SAMPLES)
    moduli = [clt.directional_modulus(lam, summary.angles, float(p)) for p in phis]
    fig = go.Figure(
        go.Scatterpolar(
            r=moduli,
            theta=phis,
            mode="lines",
            line={"color": "rgb(178,24,43)", "width": 2},
            fill="toself",
            name="Ex(φ)",
        )
    )
    fig.update_layout(
        title={"text": f"Directional modulus Ex(φ) — {summary.material} {_layup_label(summary)}"},
        polar={"radialaxis": {"title": {"text": "Ex (GPa)"}, "angle": 90}},
        paper_bgcolor="white",
        font={"color": "#1f2937"},
        margin={"l": 40, "r": 40, "t": 60, "b": 40},
    )
    fig.write_html(out_dir / "polar_modulus.html", include_plotlyjs="cdn", full_html=True)


def _layup_label(summary: clt.LaminateSummary) -> str:
    t1, t2 = summary.angles[0], summary.angles[2]
    return f"[±{abs(t1):.0f}/±{abs(t2):.0f}]s"


def _num(name: str, label: str, value: float, unit: str | None, precision: float) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "type": "number",
        "unit": unit,
        "value": round(float(value), _decimals(precision)),
    }


def _decimals(precision: float) -> int:
    return max(0, len(f"{precision:.10f}".rstrip("0").split(".")[-1])) if precision < 1 else 0


def main() -> int:
    """Evaluate the laminate, render the plots, write outputs.json."""
    inputs = _read_inputs()
    material = str(inputs["material"])
    theta1 = float(inputs["theta1"])
    theta2 = float(inputs["theta2"])
    thickness = float(inputs["thickness"])

    s = clt.summarise(material, theta1, theta2, thickness)
    lam = s.lamina

    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)
    _write_ply_stack(out_dir, s)
    _write_polar_modulus(out_dir, s)

    outputs: list[dict[str, Any]] = [
        # --- Ply (lamina) card — wired to the FE solvers, applied per ply ---
        _num("e1", "Lamina E₁ (fibre)", lam.e1, "GPa", 0.1),
        _num("e2", "Lamina E₂ (transverse)", lam.e2, "GPa", 0.1),
        _num("g12", "Lamina G₁₂", lam.g12, "GPa", 0.01),
        _num("nu12", "Lamina ν₁₂", lam.nu12, None, 0.001),
        _num("alpha1", "Lamina α₁", lam.alpha1, "1e-6/degC", 0.01),
        _num("alpha2", "Lamina α₂", lam.alpha2, "1e-6/degC", 0.1),
        _num("xt", "Strength Xt (fibre tension)", lam.xt, "MPa", 1),
        _num("xc", "Strength Xc (fibre comp.)", lam.xc, "MPa", 1),
        _num("yt", "Strength Yt (transverse tension)", lam.yt, "MPa", 1),
        _num("yc", "Strength Yc (transverse comp.)", lam.yc, "MPa", 1),
        _num("s_shear", "Strength S (in-plane shear)", lam.s, "MPa", 1),
        _num("k1", "Lamina k₁ (fibre)", lam.k1, "W/m/K", 0.1),
        _num("k2", "Lamina k₂ (transverse)", lam.k2, "W/m/K", 0.1),
        _num("rho", "Density", lam.rho, "kg/m3", 1),
        _num("cp", "Specific heat", lam.cp, "J/kg/K", 1),
        # --- Layup (echoed so the chain stays self-describing) ---
        _num("theta1", "Ply angle θ₁", theta1, "deg", 1),
        _num("theta2", "Ply angle θ₂", theta2, "deg", 1),
        _num("n_plies", "Total plies", s.n_plies, None, 1),
        _num("thickness", "Laminate thickness", s.thickness_mm, "mm", 0.001),
        # --- Laminate effective (CLT diagnostics) ---
        _num("ex", "Effective Ex", s.ex, "GPa", 0.1),
        _num("ey", "Effective Ey", s.ey, "GPa", 0.1),
        _num("gxy", "Effective Gxy", s.gxy, "GPa", 0.01),
        _num("nuxy", "Effective νxy", s.nuxy, None, 0.001),
        _num("alphax", "Effective αx", s.alphax, "1e-6/degC", 0.01),
        _num("alphay", "Effective αy", s.alphay, "1e-6/degC", 0.01),
        _num("kx", "Effective kx (in-plane)", s.kx, "W/m/K", 0.01),
        _num("ky", "Effective ky (in-plane)", s.ky, "W/m/K", 0.01),
        _num("kz", "Effective kz (through-thickness)", s.kz, "W/m/K", 0.01),
        # --- Geometry & mass ---
        _num("mass", "Laminate mass", s.mass_g, "g", 0.01),
        _num("areal_density", "Areal density", s.areal_density, "kg/m2", 0.001),
        # --- Plots ---
        {
            "name": "ply_stack",
            "label": "Ply stack",
            "type": "file",
            "filename": "outputs/ply_stack.html",
        },
        {
            "name": "polar_modulus",
            "label": "Directional modulus (polar)",
            "type": "file",
            "filename": "outputs/polar_modulus.html",
        },
    ]

    document = {"schema_version": 1, "tool_alias": "composite_laminate", "outputs": outputs}
    (out_dir / "outputs.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
