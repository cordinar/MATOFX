"""Thermal-plate field-output demo tool.

A deliberately lightweight analytic model that emits a whole **nodal
temperature field** on a rectangular plate, so the field-surrogate path
(ADR-0008 §4) has a real source to fit from end-to-end. A heated left edge
diffuses across the plate; the three inputs move the field in distinct ways:

* **heat_load** scales the amplitude of the hot edge,
* **conductivity** sets how far the heat spreads before decaying,
* **ambient** sets the baseline the whole field sits on.

That gives a smooth, low-rank field ensemble across a parametric study —
ideal POD material. The field is written as a ``.vtp`` PolyData mesh with a
per-node ``temperature`` point-array (the geometry the predicted-field
viewer colours), plus a ``t_max`` scalar.

Depends on numpy (BSD-3) + pyvista (BSD-3), both permissive and already core
MATOFX deps for the field path.
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


def _read_inputs() -> dict[str, Any]:
    payload = json.loads(Path("inputs/inputs.json").read_text(encoding="utf-8"))
    return {entry["name"]: entry["value"] for entry in payload["inputs"]}


def main() -> int:
    """Evaluate the plate field and write the .vtp snapshot + t_max scalar."""
    inputs = _read_inputs()
    heat_load = float(inputs["heat_load"])
    ambient = float(inputs["ambient"])
    conductivity = float(inputs["conductivity"])

    # A flat rectangular plate as PolyData (so the field is a .vtp the viewer
    # can render directly; extract_surface on it is the plate itself).
    plate = pv.Plane(
        i_resolution=_I_RES, j_resolution=_J_RES, i_size=_I_SIZE, j_size=_J_SIZE
    )
    pts = np.asarray(plate.points, dtype=float)
    span_x = float(pts[:, 0].max() - pts[:, 0].min()) or 1.0
    span_y = float(pts[:, 1].max() - pts[:, 1].min()) or 1.0
    xr = (pts[:, 0] - pts[:, 0].min()) / span_x  # 0 (hot edge) -> 1 (far edge)
    yr = (pts[:, 1] - pts[:, 1].min()) / span_y  # 0 -> 1 across the width

    # Heat decays away from the left edge; higher conductivity spreads it
    # further (slower decay). A gentle cross-width ripple adds 2-D structure.
    spread = 0.10 + 0.30 * (conductivity / 200.0)
    temperature = (
        ambient
        + heat_load * np.exp(-xr / spread) * (1.0 + 0.25 * np.sin(3.0 * np.pi * yr))
    )

    plate.point_data["temperature"] = temperature

    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)
    plate.save(out_dir / "temperature.vtp")

    document = {
        "schema_version": 1,
        "tool_alias": "field_demo",
        "outputs": [
            {
                "name": "t_max",
                "label": "Peak temperature",
                "type": "number",
                "unit": "degC",
                "value": round(float(temperature.max()), 2),
            },
            {
                "name": "temperature_field",
                "label": "Temperature field",
                "type": "file",
                "filename": "outputs/temperature.vtp",
            },
        ],
    }
    (out_dir / "outputs.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
