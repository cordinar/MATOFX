"""Material-properties calculator — three-component formulation.

Reads ``inputs/inputs.json`` (a v2 :class:`InputsDocument`), looks each
component up in the small property library below, mass-weights the
results by the percentage inputs, and writes the bulk thermal
conductivity and heat capacity to ``outputs/outputs.json`` (a v2
:class:`OutputsDocument`).

Pure stdlib — no scientific dependencies — so this tool also doubles
as the simplest possible "matofx tool" reference for new authors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Library of per-component properties. Real installs would load these
# from a customer-managed registry; the demo carries them inline so the
# port stays readable in one screen.
_THERMAL_CONDUCTIVITY: dict[str, float] = {
    "Component A": 0.500,
    "Component B": 0.300,
    "Component C": 1.000,
    "Component D": 0.750,
    "Component E": 0.625,
}
_HEAT_CAPACITY: dict[str, float] = {
    "Component A": 1500.0,
    "Component B": 1230.0,
    "Component C": 1740.0,
    "Component D": 1600.0,
    "Component E": 1950.0,
}


def _read_inputs() -> dict[str, Any]:
    """Return ``{name: value}`` for the v2 inputs document on disk."""
    payload = json.loads(Path("inputs/inputs.json").read_text(encoding="utf-8"))
    return {entry["name"]: entry["value"] for entry in payload["inputs"]}


def _write_outputs(tool_alias: str, values: dict[str, float]) -> None:
    """Write a v2 :class:`OutputsDocument` with one numeric output per key."""
    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)
    document = {
        "schema_version": 1,
        "tool_alias": tool_alias,
        "outputs": [
            {
                "name": "thermal_conductivity",
                "label": "Thermal Conductivity",
                "type": "number",
                "unit": "W/m/K",
                "value": round(values["thermal_conductivity"], 4),
            },
            {
                "name": "heat_capacity",
                "label": "Heat Capacity",
                "type": "number",
                "unit": "J/kg/K",
                "value": round(values["heat_capacity"], 2),
            },
        ],
    }
    (out_dir / "outputs.json").write_text(json.dumps(document, indent=2), encoding="utf-8")


def main() -> int:
    """Read the formulation, mass-weight the properties, write the outputs document."""
    inputs = _read_inputs()
    components = (inputs["component_1"], inputs["component_2"], inputs["component_3"])
    fractions = (
        float(inputs["component_1_percent"]) / 100.0,
        float(inputs["component_2_percent"]) / 100.0,
        float(inputs["component_3_percent"]) / 100.0,
    )

    thermal_conductivity = sum(
        _THERMAL_CONDUCTIVITY[c] * f for c, f in zip(components, fractions, strict=True)
    )
    heat_capacity = sum(_HEAT_CAPACITY[c] * f for c, f in zip(components, fractions, strict=True))

    _write_outputs(
        tool_alias="material_properties",
        values={"thermal_conductivity": thermal_conductivity, "heat_capacity": heat_capacity},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
