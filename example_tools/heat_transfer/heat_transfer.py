"""2D heat-transfer simulation — graded composite, convective boundaries.

Reads geometry, material properties, and ambient conditions from the v2
:class:`InputsDocument` on disk; runs an explicit finite-difference
solve with a CFL-stable timestep; writes corner + core temperatures,
average / top-edge temperature profiles, and a Plotly heatmap of the
final temperature field.

Depends on numpy (BSD-3), pandas (BSD-3), plotly (MIT). All permissive.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go

# Solver grid resolution. Hard-coded — tuning grid resolution is a
# tool-author concern, not an end-user one (per the SPDM-end-user rule).
_NX: int = 200
_NY: int = 100
_CFL_FRACTION: float = 0.25


def _read_inputs() -> dict[str, Any]:
    payload = json.loads(Path("inputs/inputs.json").read_text(encoding="utf-8"))
    return {entry["name"]: entry["value"] for entry in payload["inputs"]}


def _convective_coefficient(air_speed: float) -> float:
    """W/m²K. Empirical correlation: forced-convection over a flat plate."""
    return 15.0 + 25.0 * air_speed


def main() -> int:
    """Solve the 2D heat equation, write the temperature outputs + CSVs + plotly HTML."""
    inputs = _read_inputs()
    Lx = float(inputs["width"])  # noqa: N806 — physical convention
    Ly = float(inputs["height"])  # noqa: N806
    rho_left = float(inputs["density_left"])
    rho_right = float(inputs["density_right"])
    k = float(inputs["thermal_conductivity"])
    cp = float(inputs["heat_capacity"])
    T_init = float(inputs["object_temperature"])  # noqa: N806
    T_inf = float(inputs["air_temperature"])  # noqa: N806
    v_air = float(inputs["air_speed"])
    t_total = float(inputs["simulation_time"])

    dx = Lx / (_NX - 1)
    dy = Ly / (_NY - 1)

    # Density varies linearly along x; broadcast to the full grid.
    rho_x = np.linspace(rho_left, rho_right, _NX)
    rho = np.tile(rho_x, (_NY, 1))
    alpha = k / (rho * cp)  # local thermal diffusivity, m²/s

    h = _convective_coefficient(v_air)
    dt = _CFL_FRACTION * min(dx, dy) ** 2 / float(np.max(alpha))
    nt = max(int(t_total / dt), 1)

    T = np.ones((_NY, _NX)) * T_init  # noqa: N806
    T_new = T.copy()  # noqa: N806

    avg_temp = np.empty((nt, 2))
    top_temp = np.empty((nt, 2))

    for n in range(nt):
        Txx = (T[:, 2:] - 2 * T[:, 1:-1] + T[:, :-2]) / dx**2  # noqa: N806
        Tyy = (T[2:, :] - 2 * T[1:-1, :] + T[:-2, :]) / dy**2  # noqa: N806
        alpha_center = alpha[1:-1, 1:-1]
        T_new[1:-1, 1:-1] = T[1:-1, 1:-1] + alpha_center * dt * (Txx[1:-1, :] + Tyy[:, 1:-1])

        # Convective boundaries on all four sides.
        T_new[:, 0] = T[:, 1] + (h * dx / k) * (T_inf - T[:, 0])
        T_new[:, -1] = T[:, -2] + (h * dx / k) * (T_inf - T[:, -1])
        T_new[0, :] = T[1, :] + (h * dy / k) * (T_inf - T[0, :])
        T_new[-1, :] = T[-2, :] + (h * dy / k) * (T_inf - T[-1, :])

        T = T_new.copy()  # noqa: N806
        avg_temp[n] = (n * dt, float(np.mean(T)))
        top_temp[n] = (n * dt, float(np.mean(T[0, :])))

    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)

    pd.DataFrame({"Time": avg_temp[:, 0], "Avg Temp": avg_temp[:, 1]}).to_csv(
        out_dir / "average_temperature_profile.csv", index=False
    )
    pd.DataFrame({"Time": top_temp[:, 0], "Top Temp": top_temp[:, 1]}).to_csv(
        out_dir / "top_temperature_profile.csv", index=False
    )

    x = np.linspace(0, Lx, _NX)
    y = np.linspace(0, Ly, _NY)
    fig = go.Figure(
        data=go.Heatmap(
            z=T,
            x=x,
            y=y,
            colorscale="RdBu_r",
            colorbar={"title": "Temperature [degC]"},
        )
    )
    fig.update_layout(
        title=f"2D Temperature Distribution after {t_total:.1f} s",
        xaxis_title="x [m]",
        yaxis_title="y [m]",
        plot_bgcolor="white",
        paper_bgcolor="white",
        xaxis={"showgrid": False},
        yaxis={"showgrid": False},
    )
    fig.update_yaxes(scaleanchor="x", scaleratio=1)
    fig.write_html(out_dir / "temperature_field.html", include_plotlyjs="cdn")

    document = {
        "schema_version": 1,
        "tool_alias": "heat_transfer",
        "outputs": [
            {
                "name": "T_core",
                "label": "Core temperature",
                "type": "number",
                "unit": "degC",
                "value": round(float(T[_NY // 2, _NX // 2]), 2),
            },
            {
                "name": "T_0_0",
                "label": "Bottom left",
                "type": "number",
                "unit": "degC",
                "value": round(float(T[0, 0]), 2),
            },
            {
                "name": "T_0_1",
                "label": "Top left",
                "type": "number",
                "unit": "degC",
                "value": round(float(T[0, -1]), 2),
            },
            {
                "name": "T_1_0",
                "label": "Bottom right",
                "type": "number",
                "unit": "degC",
                "value": round(float(T[-1, 0]), 2),
            },
            {
                "name": "T_1_1",
                "label": "Top right",
                "type": "number",
                "unit": "degC",
                "value": round(float(T[-1, -1]), 2),
            },
            {
                "name": "temp_gradient_profile",
                "label": "Temperature gradient plot",
                "type": "file",
                "filename": "temperature_field.html",
            },
            {
                "name": "avg_temp_profile",
                "label": "Average temperature profile",
                "type": "file",
                "filename": "average_temperature_profile.csv",
            },
            {
                "name": "top_temp_profile",
                "label": "Top temperature profile",
                "type": "file",
                "filename": "top_temperature_profile.csv",
            },
        ],
    }
    (out_dir / "outputs.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
