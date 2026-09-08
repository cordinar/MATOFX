"""Unit tests for :mod:`matofx.ui.pages.compare.scenario_groups`.

Pure cross-scenario matching — no NiceGUI, no network. Covers the
contract the Compare Scenarios view depends on: tools / workflows shared
across two or more scenarios become comparison groups (aligned per
scenario, ``None`` where absent); those in a single scenario fall to the
"not comparable" list; and the cross-project flag.
"""

from __future__ import annotations

from typing import Any

from matofx.ui.pages.compare.scenario_groups import match_scenarios


def _scenario(sid: int, name: str, project_id: int = 1) -> dict[str, Any]:
    return {"id": sid, "name": name, "project_id": project_id}


def _job(job_id: int, alias: str, *, workflow_run_id: int | None = None) -> dict[str, Any]:
    return {"id": job_id, "tool_alias": alias, "workflow_run_id": workflow_run_id}


def _run(run_id: int, alias: str, name: str) -> dict[str, Any]:
    return {
        "id": run_id,
        "workflow_alias": alias,
        "workflow_name": name,
        "children": [],
    }


def test_tool_in_two_scenarios_is_a_group_aligned_by_scenario_order() -> None:
    """A tool with a job in both scenarios → one group, columns in order."""
    scenarios = [_scenario(1, "Variant A"), _scenario(2, "Variant B")]
    standalone = {
        1: [_job(11, "heat_transfer")],
        2: [_job(21, "heat_transfer")],
    }
    result = match_scenarios(scenarios, standalone, {1: [], 2: []})
    assert len(result.tool_groups) == 1
    group = result.tool_groups[0]
    assert group.tool_alias == "heat_transfer"
    assert group.job_ids == (11, 21)
    assert result.not_comparable == ()
    assert result.cross_project is False


def test_tool_missing_in_one_scenario_leaves_a_none_slot() -> None:
    """A tool in two of three scenarios still compares; the third is ``None``."""
    scenarios = [_scenario(1, "A"), _scenario(2, "B"), _scenario(3, "C")]
    standalone = {
        1: [_job(11, "heat_transfer")],
        2: [],
        3: [_job(31, "heat_transfer")],
    }
    result = match_scenarios(scenarios, standalone, {1: [], 2: [], 3: []})
    assert len(result.tool_groups) == 1
    assert result.tool_groups[0].job_ids == (11, None, 31)


def test_tool_in_only_one_scenario_is_not_comparable() -> None:
    """A tool present in a single scenario can't be compared — listed instead."""
    scenarios = [_scenario(1, "A"), _scenario(2, "B")]
    standalone = {
        1: [_job(11, "material_properties")],
        2: [],
    }
    result = match_scenarios(scenarios, standalone, {1: [], 2: []})
    assert result.tool_groups == ()
    assert len(result.not_comparable) == 1
    item = result.not_comparable[0]
    assert item.kind == "tool"
    assert item.alias == "material_properties"
    assert item.scenario_label == "A"


def test_workflow_runs_in_two_scenarios_form_a_group() -> None:
    """A workflow definition run in two scenarios → a workflow group."""
    scenarios = [_scenario(1, "A"), _scenario(2, "B")]
    runs = {
        1: [_run(101, "heat_pipeline", "Heat Pipeline")],
        2: [_run(201, "heat_pipeline", "Heat Pipeline")],
    }
    result = match_scenarios(scenarios, {1: [], 2: []}, runs)
    assert len(result.workflow_groups) == 1
    group = result.workflow_groups[0]
    assert group.workflow_alias == "heat_pipeline"
    assert group.workflow_name == "Heat Pipeline"
    # Runs carried through in scenario order, ready for the workflow renderer.
    assert group.runs[0] is not None
    assert group.runs[0]["id"] == 101
    assert group.runs[1]["id"] == 201


def test_workflow_missing_in_one_scenario_leaves_none_and_compares_rest() -> None:
    """A workflow in two of three scenarios compares; the third run is ``None``."""
    scenarios = [_scenario(1, "A"), _scenario(2, "B"), _scenario(3, "C")]
    runs = {
        1: [_run(101, "heat_pipeline", "Heat Pipeline")],
        2: [],
        3: [_run(301, "heat_pipeline", "Heat Pipeline")],
    }
    result = match_scenarios(scenarios, {1: [], 2: [], 3: []}, runs)
    assert len(result.workflow_groups) == 1
    group = result.workflow_groups[0]
    assert [r["id"] if r else None for r in group.runs] == [101, None, 301]


def test_workflow_run_children_excluded_from_tool_groups() -> None:
    """A workflow-child job (workflow_run_id set) isn't a standalone tool group."""
    scenarios = [_scenario(1, "A"), _scenario(2, "B")]
    standalone = {
        1: [_job(11, "heat_transfer", workflow_run_id=99)],
        2: [_job(21, "heat_transfer", workflow_run_id=99)],
    }
    result = match_scenarios(scenarios, standalone, {1: [], 2: []})
    assert result.tool_groups == ()


def test_cross_project_flag_set_when_scenarios_span_projects() -> None:
    """Two scenarios in different projects flip the cross-project flag."""
    scenarios = [_scenario(1, "A", project_id=1), _scenario(2, "B", project_id=2)]
    result = match_scenarios(scenarios, {1: [], 2: []}, {1: [], 2: []})
    assert result.cross_project is True
    assert result.scenario_names == ("A", "B")
    assert result.scenario_ids == (1, 2)


def test_tool_groups_sorted_by_alias() -> None:
    """Multiple shared tools come out in a stable alphabetical order."""
    scenarios = [_scenario(1, "A"), _scenario(2, "B")]
    standalone = {
        1: [_job(11, "heat_transfer"), _job(12, "colour_check")],
        2: [_job(21, "heat_transfer"), _job(22, "colour_check")],
    }
    result = match_scenarios(scenarios, standalone, {1: [], 2: []})
    assert [g.tool_alias for g in result.tool_groups] == ["colour_check", "heat_transfer"]
