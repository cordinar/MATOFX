"""Unit tests for :mod:`matofx.ui.pages.compare.workflow_groups`.

Pure step-alignment logic — no NiceGUI, no network — so these run fast
and cover the alignment contract the Compare Workflows grid depends on:
same-definition guard, version-mix flag, union-of-steps ordering, and
the per-run ``None`` slot a renderer turns into a "step not present"
placeholder.
"""

from __future__ import annotations

from typing import Any

from matofx.ui.pages.compare.workflow_groups import (
    WorkflowAlignment,
    WorkflowAlignmentError,
    align_workflow_runs,
)


def _child(
    job_id: int,
    position: int,
    alias: str,
    name: str | None = None,
) -> dict[str, Any]:
    return {
        "id": job_id,
        "workflow_position": position,
        "tool_alias": alias,
        "tool_name": name or alias.replace("_", " ").title(),
    }


def _run(
    *,
    run_id: int,
    alias: str = "heat_pipeline",
    name: str = "Run",
    version: str = "1.0.0",
    children: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": run_id,
        "workflow_alias": alias,
        "workflow_name": "Heat Pipeline",
        "workflow_version_snapshot": version,
        "name": name,
        "children": children,
    }


def test_fewer_than_two_runs_is_an_error() -> None:
    """A single run can't be compared — friendly message, not a crash."""
    result = align_workflow_runs([_run(run_id=1, children=[_child(10, 1, "material_properties")])])
    assert isinstance(result, WorkflowAlignmentError)
    assert "at least two" in result.message


def test_different_workflow_definitions_refused() -> None:
    """Cross-definition comparison is refused with a clear message (G36 Q4)."""
    result = align_workflow_runs(
        [
            _run(run_id=1, alias="heat_pipeline", children=[_child(10, 1, "mp")]),
            _run(run_id=2, alias="stress_pipeline", children=[_child(20, 1, "mp")]),
        ]
    )
    assert isinstance(result, WorkflowAlignmentError)
    assert "different workflows" in result.message


def test_aligns_matching_runs_step_by_step() -> None:
    """Two runs of one definition align per step, in workflow order."""
    runs = [
        _run(
            run_id=1,
            children=[
                _child(11, 1, "material_properties", "Material Properties"),
                _child(12, 2, "heat_transfer", "Heat Transfer"),
            ],
        ),
        _run(
            run_id=2,
            children=[
                _child(21, 1, "material_properties", "Material Properties"),
                _child(22, 2, "heat_transfer", "Heat Transfer"),
            ],
        ),
    ]
    result = align_workflow_runs(runs)
    assert isinstance(result, WorkflowAlignment)
    assert result.workflow_alias == "heat_pipeline"
    assert result.workflow_name == "Heat Pipeline"
    assert result.version_mix is False
    assert [s.tool_alias for s in result.steps] == ["material_properties", "heat_transfer"]
    assert [s.position for s in result.steps] == [1, 2]
    assert result.steps[0].tool_name == "Material Properties"
    # Column order follows the run order supplied to the aligner.
    assert result.steps[0].job_ids == (11, 21)
    assert result.steps[1].job_ids == (12, 22)


def test_steps_ordered_by_position_not_input_order() -> None:
    """Step ordering is by workflow position, regardless of child order."""
    runs = [
        _run(
            run_id=1,
            children=[
                _child(12, 2, "heat_transfer"),
                _child(11, 1, "material_properties"),
            ],
        ),
        _run(
            run_id=2,
            children=[
                _child(21, 1, "material_properties"),
                _child(22, 2, "heat_transfer"),
            ],
        ),
    ]
    result = align_workflow_runs(runs)
    assert isinstance(result, WorkflowAlignment)
    assert [s.tool_alias for s in result.steps] == ["material_properties", "heat_transfer"]


def test_missing_step_in_one_run_leaves_a_none_slot() -> None:
    """A step only some runs have appears, with ``None`` for runs without it.

    This is the version-drift case the renderer turns into a top-aligned
    "this step isn't part of this run" placeholder.
    """
    runs = [
        _run(
            run_id=1,
            version="1.0.0",
            children=[
                _child(11, 1, "material_properties"),
                _child(12, 2, "heat_transfer"),
            ],
        ),
        _run(
            run_id=2,
            version="2.0.0",
            children=[
                _child(21, 1, "material_properties"),
                _child(22, 2, "heat_transfer"),
                _child(23, 3, "stress_check"),
            ],
        ),
    ]
    result = align_workflow_runs(runs)
    assert isinstance(result, WorkflowAlignment)
    assert result.version_mix is True
    assert result.versions == ("1.0.0", "2.0.0")
    stress = next(s for s in result.steps if s.tool_alias == "stress_check")
    # Run 1 (no stress_check) gets a None slot; run 2 supplies the job.
    assert stress.job_ids == (None, 23)


def test_version_mix_false_when_all_snapshots_match() -> None:
    """Identical snapshots → no version-mix flag."""
    runs = [
        _run(run_id=1, version="3.1.0", children=[_child(11, 1, "mp")]),
        _run(run_id=2, version="3.1.0", children=[_child(21, 1, "mp")]),
    ]
    result = align_workflow_runs(runs)
    assert isinstance(result, WorkflowAlignment)
    assert result.version_mix is False


def test_children_without_id_or_alias_are_skipped() -> None:
    """Malformed child rows don't crash the aligner or invent steps."""
    runs = [
        _run(
            run_id=1,
            children=[
                _child(11, 1, "material_properties"),
                {"id": None, "workflow_position": 2, "tool_alias": "heat_transfer"},
                {"id": 99, "workflow_position": 3, "tool_alias": ""},
            ],
        ),
        _run(run_id=2, children=[_child(21, 1, "material_properties")]),
    ]
    result = align_workflow_runs(runs)
    assert isinstance(result, WorkflowAlignment)
    assert [s.tool_alias for s in result.steps] == ["material_properties"]
    assert result.steps[0].job_ids == (11, 21)
