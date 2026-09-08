"""Unit tests for ``_build_run_response`` — the shared workflow-run wire core.

Both the per-run resolver and the batched ``list_my_visible_workflow_runs``
endpoint feed this sync function pre-resolved metadata, so a regression
here (e.g. mis-mapping the batched caches) would surface in both. We drive
it with lightweight fakes — no DB or app needed — and assert the mapping,
including the project / scenario names added for the Compare workflow rows
and the tool-name resolution.
"""

from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace

from matofx.api.routers.workflows import _build_run_response

_NOW = _dt.datetime(2026, 6, 24, 12, 0, tzinfo=_dt.UTC)


def _child(**kw: object) -> SimpleNamespace:
    base = {
        "id": 1,
        "workflow_position": 1,
        "tool_alias": "alpha",
        "name": "step",
        "state": "complete",
        "created_at": _NOW,
        "started_at": None,
        "completed_at": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _run(**kw: object) -> SimpleNamespace:
    base = {
        "id": 185,
        "workflow_id": 7,
        "workflow_version_snapshot": "1.0.0",
        "scenario_id": 3,
        "name": "nominal",
        "submitted_by_user_id": 2,
        "cancel_requested_at": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_run_response_maps_names_children_and_state() -> None:
    resp = _build_run_response(
        _run(),
        [
            _child(id=11, tool_alias="alpha", state="complete"),
            _child(id=12, workflow_position=2, tool_alias="beta", state="complete"),
        ],
        wf=SimpleNamespace(alias="chain", name="Thermo Chain"),
        scenario=SimpleNamespace(name="Scenario 1", project_id=9),
        project=SimpleNamespace(name="Demo testing"),
        submitter=SimpleNamespace(full_name="Bob Stress", username="bob"),
        tool_names={"alpha": "Alpha", "beta": "Thermal Soak"},
    )
    assert resp.id == 185
    assert resp.workflow_alias == "chain"
    assert resp.workflow_name == "Thermo Chain"
    # The fields that power the Compare workflow rows:
    assert resp.scenario_id == 3
    assert resp.scenario_name == "Scenario 1"
    assert resp.project_id == 9
    assert resp.project_name == "Demo testing"
    assert resp.submitted_by_label == "Bob Stress (bob)"
    # Children carry resolved tool names (labels), in order.
    assert [c.id for c in resp.children] == [11, 12]
    assert [c.tool_name for c in resp.children] == ["Alpha", "Thermal Soak"]
    # All children complete → run complete.
    assert resp.state == "complete"


def test_build_run_response_unknown_tool_alias_falls_back_to_alias() -> None:
    resp = _build_run_response(
        _run(),
        [_child(tool_alias="ghost")],
        wf=SimpleNamespace(alias="chain", name="Chain"),
        scenario=SimpleNamespace(name="S", project_id=1),
        project=SimpleNamespace(name="P"),
        submitter=SimpleNamespace(full_name="", username="u"),
        tool_names={},  # no name known
    )
    assert resp.children[0].tool_name == "ghost"
    assert resp.submitted_by_label == "u (u)"  # full_name empty → username


def test_build_run_response_tolerates_missing_metadata() -> None:
    # Batched caches can miss a row (deleted scenario/project); the core
    # must degrade to blanks rather than raise.
    resp = _build_run_response(
        _run(),
        [],
        wf=None,
        scenario=None,
        project=None,
        submitter=None,
        tool_names={},
    )
    assert resp.workflow_alias == ""
    assert resp.scenario_name == ""
    assert resp.project_id == 0
    assert resp.project_name == ""
    assert resp.submitted_by_label == "—"
    assert resp.state == "draft"  # no children
