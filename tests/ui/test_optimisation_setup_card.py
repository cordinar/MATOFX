"""The Setup card must state the whole search definition, not just its names.

Reading a finished optimisation, "Factors: Fin height, Plate material" says
nothing about what the search was *allowed* to try, and nothing at all about
the inputs it held still. Both halves are what makes a run reproducible, so
these cover the two helpers that feed them:

- ``_factors_from_config`` now carries each factor's declared limits (or its
  levels, for a categorical one) through to the card.
- ``_fixed_from_config`` turns ``fixed_inputs`` into labelled rows, resolving
  a held material's version id back to its name.
"""

from __future__ import annotations

from typing import Any

import pytest

from matofx.ui.pages.optimisation import (
    _factor_range_text,
    _factors_from_config,
    _fixed_from_config,
    _material_classes,
    _material_version_labels,
    _workflow_material_classes,
)

_CFG: dict[str, Any] = {
    "factors": [
        {"name": "fin_height", "type": "float", "min": 5.0, "max": 45.0},
        {"name": "n_fins", "type": "int", "min": 4, "max": 20},
        {"name": "material", "type": "categorical", "choices": ["Al 6061", "Cu"]},
    ],
    "fixed_inputs": {
        "inlet_temp": 20.0,
        "flow_rate": 0.35,
        "solver_modes": ["steady", "laminar"],
        "adaptive": False,
    },
}

_LABELS = {
    "fin_height": "Fin height",
    "n_fins": "Number of fins",
    "material": "Plate material",
    "inlet_temp": "Inlet temperature",
    "flow_rate": "Flow rate",
    "solver_modes": "Solver modes",
    "adaptive": "Adaptive stepping",
}
_UNITS = {"fin_height": "mm", "inlet_temp": "degC", "flow_rate": "kg/s"}


# --- factor limits ---------------------------------------------------------


def test_a_numeric_factor_carries_its_declared_limits() -> None:
    facs = _factors_from_config(_CFG, _LABELS, _UNITS)
    assert (facs[0]["min"], facs[0]["max"]) == (5.0, 45.0)
    assert _factor_range_text(facs[0]) == "5 to 45"
    assert (facs[1]["min"], facs[1]["max"]) == (4, 20)
    assert _factor_range_text(facs[1]) == "4 to 20"


def test_a_categorical_factor_states_its_levels_not_a_range() -> None:
    """Bounds are meaningless for a choice list, so the card must not invent one."""
    fac = _factors_from_config(_CFG, _LABELS, _UNITS)[2]
    assert (fac["min"], fac["max"]) == (None, None)
    assert _factor_range_text(fac) == "Al 6061, Cu"


def test_a_categorical_factor_ignores_a_stray_min_max() -> None:
    fac = _factors_from_config(
        {"factors": [{"name": "m", "type": "categorical", "choices": ["a"], "min": 1, "max": 2}]},
        {},
        {},
    )[0]
    assert fac["min"] is None
    assert fac["max"] is None


def test_a_factor_with_no_bounds_reads_as_unset() -> None:
    fac = _factors_from_config({"factors": [{"name": "x", "type": "float"}]}, {}, {})[0]
    assert _factor_range_text(fac) == "—"


# --- held-constant inputs --------------------------------------------------


def test_held_inputs_are_labelled_ordered_and_formatted() -> None:
    held = _fixed_from_config(_CFG, _LABELS, _UNITS)
    assert [r["label"] for r in held] == [
        "Adaptive stepping",
        "Flow rate",
        "Inlet temperature",
        "Solver modes",
    ]
    by_label = {r["label"]: r for r in held}
    assert by_label["Inlet temperature"]["unit"] == "degC"
    assert by_label["Inlet temperature"]["display"] == "20"
    # A multiselect holds a list; str(list) would leak Python repr at the user.
    assert by_label["Solver modes"]["display"] == "steady, laminar"
    assert by_label["Adaptive stepping"]["display"] == "no"


def test_a_study_with_nothing_held_returns_no_rows() -> None:
    assert _fixed_from_config({"factors": []}, _LABELS, _UNITS) == []


def test_an_unlabelled_key_falls_back_to_its_name() -> None:
    held = _fixed_from_config({"fixed_inputs": {"mat.gap": 2}}, {}, {})
    assert held[0]["label"] == "mat.gap"


# --- a held material is a version id, and must not read as one -------------


_MATERIAL_DESCRIPTOR: dict[str, Any] = {
    "inputs": [
        {"name": "plate", "label": "Plate", "type": "material", "material_class": "Metals"},
        {"name": "gap", "label": "Gap", "type": "float"},
    ]
}


def test_material_inputs_are_picked_out_of_the_descriptor() -> None:
    assert _material_classes(_MATERIAL_DESCRIPTOR) == {"plate": "Metals"}
    assert _material_classes(_MATERIAL_DESCRIPTOR, prefix="cold.") == {"cold.plate": "Metals"}


def test_a_held_material_shows_its_name_and_its_version() -> None:
    held = _fixed_from_config(
        {"fixed_inputs": {"plate": "77"}},
        {"plate": "Plate"},
        {},
        material_keys={"plate"},
        material_names={"77": "Aluminium 6061"},
    )
    assert held[0]["display"] == "Aluminium 6061 (material version 77)"


def test_an_unresolvable_material_says_so_rather_than_naming_the_wrong_one() -> None:
    """The picker only offers latest-published versions, so an older held
    version simply won't resolve — the id is then the honest answer."""
    held = _fixed_from_config(
        {"fixed_inputs": {"plate": "12"}},
        {"plate": "Plate"},
        {},
        material_keys={"plate"},
        material_names={"77": "Aluminium 6061"},
    )
    assert held[0]["display"] == "material version 12"


class _MaterialsClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def materials_picker(self, *, material_class: str) -> list[dict[str, Any]]:
        self.calls.append(material_class)
        return [{"version_id": 77, "name": "Aluminium 6061"}]


@pytest.mark.asyncio
async def test_version_labels_are_fetched_once_per_class() -> None:
    client = _MaterialsClient()
    labels = await _material_version_labels(client, {"Metals", ""})  # type: ignore[arg-type]
    assert labels == {"77": "Aluminium 6061"}
    assert client.calls == ["Metals"]  # the blank class is not a class


class _WorkflowClient:
    async def list_workflows(self) -> list[dict[str, Any]]:
        return [{"alias": "chain", "tool_aliases": ["cold", "stress"]}]

    async def get_tool(self, alias: str) -> dict[str, Any]:
        if alias == "cold":
            return {"descriptor": _MATERIAL_DESCRIPTOR}
        return {"descriptor": {"inputs": [{"name": "load", "type": "float"}]}}


@pytest.mark.asyncio
async def test_workflow_material_keys_are_member_qualified() -> None:
    """``fixed_inputs`` keys a workflow's held inputs ``member.input``, so the
    material map has to match or the id never resolves."""
    assert await _workflow_material_classes(_WorkflowClient(), "chain") == {  # type: ignore[arg-type]
        "cold.plate": "Metals"
    }


@pytest.mark.asyncio
async def test_an_unknown_workflow_yields_no_material_keys() -> None:
    assert await _workflow_material_classes(_WorkflowClient(), "nope") == {}  # type: ignore[arg-type]
