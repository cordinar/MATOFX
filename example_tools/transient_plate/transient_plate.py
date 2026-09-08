"""Transient heating-plate field tool — a time-resolved .vtp series.

Companion to ``field_demo`` (the steady plate). Instead of one snapshot it
writes a **sequence** of nodal temperature fields as a heated left edge warms
the plate over time, following a first-order approach to steady state:

    T(node, t) = ambient + heat_load * (1 - exp(-t / tau)) * decay(x)

Each timestep is its own ``temperature_NNNN.vtp`` (same plate mesh, evolving
``temperature`` point-array). The descriptor declares the series via
``field_timesteps``, so the job page animates it (time slider + play) and a
field surrogate can fit it with ``time`` as an extra factor.

Depends on numpy (BSD-3) + pyvista (BSD-3), both permissive and core to the
field path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pyvista as pv

_I_RES = 36  # plate cells along the flow direction
_J_RES = 24  # plate cells across
_I_SIZE = 0.12  # plate length, m
_J_SIZE = 0.08  # plate width, m
_N_STEPS = 12  # number of timesteps emitted
_TAU = 4.0  # time constant of the approach to steady state (in step units)


def _read_inputs() -> dict[str, Any]:
    payload = json.loads(Path("inputs/inputs.json").read_text(encoding="utf-8"))
    return {entry["name"]: entry["value"] for entry in payload["inputs"]}


def main() -> int:
    """Evaluate the transient plate field and write the .vtp timestep series."""
    inputs = _read_inputs()
    heat_load = float(inputs["heat_load"])
    ambient = float(inputs["ambient"])
    conductivity = float(inputs["conductivity"])

    plate = pv.Plane(i_resolution=_I_RES, j_resolution=_J_RES, i_size=_I_SIZE, j_size=_J_SIZE)
    pts = np.asarray(plate.points, dtype=float)
    span_x = float(pts[:, 0].max() - pts[:, 0].min()) or 1.0
    span_y = float(pts[:, 1].max() - pts[:, 1].min()) or 1.0
    xr = (pts[:, 0] - pts[:, 0].min()) / span_x  # 0 (hot edge) -> 1 (far edge)
    yr = (pts[:, 1] - pts[:, 1].min()) / span_y

    # Steady spatial pattern (same shape as field_demo); the transient just
    # ramps its amplitude toward this as t -> infinity.
    spread = 0.10 + 0.30 * (conductivity / 200.0)
    spatial = np.exp(-xr / spread) * (1.0 + 0.25 * np.sin(3.0 * np.pi * yr))

    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)

    t_max_final = ambient
    for step in range(_N_STEPS):
        # First-order approach to steady state: 0 at t=0, ~1 as t grows.
        ramp = 1.0 - np.exp(-step / _TAU)
        temperature = ambient + heat_load * ramp * spatial
        mesh = plate.copy()
        mesh.point_data["temperature"] = temperature
        mesh.save(out_dir / f"temperature_{step:04}.vtp")
        t_max_final = float(temperature.max())

    document = {
        "schema_version": 1,
        "tool_alias": "transient_plate",
        "outputs": [
            {
                "name": "t_max",
                "label": "Peak temperature (final step)",
                "type": "number",
                "unit": "degC",
                "value": round(t_max_final, 2),
            },
            {
                "name": "temperature_field",
                "label": "Temperature field (transient)",
                "type": "file",
                "filename": "outputs/temperature_0000.vtp",
            },
        ],
    }
    (out_dir / "outputs.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
