"""Pure-helper coverage for the optimisation detail page (ui/pages/optimisation).

The page handlers render inside a NiceGUI context the test transport can't
drive (see tests/ui/test_help_page.py), so these exercise the pure helpers
that back two recent fixes:

- ``_workflow_label_maps`` + the config→display helpers: a workflow study
  must show descriptor *labels*, not member-qualified internal names
  (feedback_use_labels_not_names).
- ``_SETTLED_STATES``: a finished study awaiting results approval must count
  as settled so the live view stops polling (it used to spin forever and
  read as "stuck on Running").
"""

from __future__ import annotations

from typing import Any

import pytest

from matofx.ui.pages.optimisation import (
    _SETTLED_STATES,
    _TERMINAL_STATES,
    _factors_from_config,
    _load_workflow_fields,
    _objectives_from_config,
    _workflow_label_maps,
)


class _FakeClient:
    """Stand-in exposing just the two async methods ``_workflow_label_maps`` uses."""

    def __init__(self, workflows: list[dict[str, Any]], tools: dict[str, dict[str, Any]]) -> None:
        self._workflows = workflows
        self._tools = tools

    async def list_workflows(self) -> list[dict[str, Any]]:
        return self._workflows

    async def get_tool(self, alias: str) -> dict[str, Any]:
        return self._tools[alias]


def _chain_client() -> _FakeClient:
    workflows = [{"alias": "mat_heat", "name": "Mat Heat", "tool_aliases": ["mat", "heat"]}]
    tools = {
        "mat": {
            "name": "Material",
            "descriptor": {
                "inputs": [
                    {"name": "thickness", "label": "Thickness", "type": "float", "unit": "mm"}
                ],
                "outputs": [{"name": "youngs_modulus", "label": "E", "type": "number"}],
            },
        },
        "heat": {
            "name": "Heat",
            "descriptor": {
                "inputs": [{"name": "mesh_size", "label": "Mesh", "type": "float"}],
                "outputs": [
                    {"name": "max_temp", "label": "Max temp", "type": "number", "unit": "K"}
                ],
            },
        },
    }
    return _FakeClient(workflows, tools)


@pytest.mark.asyncio
async def test_workflow_label_maps_are_member_qualified() -> None:
    in_labels, out_labels, in_units, out_units = await _workflow_label_maps(
        _chain_client(),  # type: ignore[arg-type]
        "mat_heat",
    )
    # Factor keys are member.input → "Tool name · field label".
    assert in_labels["mat.thickness"] == "Material · Thickness"
    assert in_units["mat.thickness"] == "mm"
    # Outputs resolve under both the bare name and the member-qualified form.
    # "Max temp" renders "Max Temp": `_titlecase` capitalises fully
    # lower-case words so a casually-authored label still reads as a
    # heading, while leaving acronyms (CO2, FreeCAD) and particles
    # (von Mises) alone. The author's label is used — only its casing is
    # normalised.
    assert out_labels["max_temp"] == "Heat · Max Temp"
    assert out_labels["heat.max_temp"] == "Heat · Max Temp"
    assert out_units["max_temp"] == "K"


@pytest.mark.asyncio
async def test_load_workflow_fields_returns_member_display_names() -> None:
    # The wizard labels every member surface "<tool name> · <field label>",
    # never the alias — so the loader must hand back alias → display name.
    factor_fields, response_fields, member_names, member_groups = await _load_workflow_fields(
        _chain_client(),  # type: ignore[arg-type]
        "mat_heat",
    )
    assert member_names == {"mat": "Material", "heat": "Heat"}
    assert ("mat", {"name": "thickness", "label": "Thickness", "type": "float", "unit": "mm"}) in (
        factor_fields
    )
    assert any(m == "heat" and o.get("name") == "max_temp" for m, o in response_fields)
    # Every chain member's input_groups come back for the factor picker to nest
    # each tool's inputs under their own group headers (empty when undeclared).
    assert set(member_groups) == {"mat", "heat"}


@pytest.mark.asyncio
async def test_unknown_workflow_yields_empty_maps() -> None:
    maps = await _workflow_label_maps(_chain_client(), "nope")  # type: ignore[arg-type]
    assert maps == ({}, {}, {}, {})


@pytest.mark.asyncio
async def test_workflow_objectives_and_factors_render_labels() -> None:
    """The user-facing fix: setup summary shows labels, not heat.max_temp etc."""
    in_labels, out_labels, in_units, out_units = await _workflow_label_maps(
        _chain_client(),  # type: ignore[arg-type]
        "mat_heat",
    )
    cfg = {
        "factors": [{"name": "mat.thickness", "member": "mat", "input": "thickness"}],
        "responses": [
            {
                "name": "heat.max_temp",
                "goal": "minimise",
                "spec": {"kind": "scalar", "member": "heat", "name": "max_temp"},
            }
        ],
    }
    factors = _factors_from_config(cfg, in_labels, in_units)
    objectives = _objectives_from_config(cfg, out_labels, out_units)
    assert factors[0]["label"] == "Material · Thickness"
    assert objectives[0]["label"] == "Heat · Max Temp"


def test_pending_results_approval_is_settled_not_terminal() -> None:
    # Settled (poll stops) but not a true terminal state (a human can still
    # release it to complete/failed).
    assert "pending_results_approval" in _SETTLED_STATES
    assert "pending_results_approval" not in _TERMINAL_STATES
    assert _TERMINAL_STATES <= _SETTLED_STATES
