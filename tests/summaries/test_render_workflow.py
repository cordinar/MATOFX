"""Slice 2 — the templated workflow-run narrative (pure)."""

from __future__ import annotations

from matofx.summaries import SummaryFacts, render_workflow


def _facts(state: str, members: list[dict[str, str]], **overrides: object) -> SummaryFacts:
    completed = sum(1 for m in members if m["state"] == "complete")
    failed = sum(1 for m in members if m["state"] == "failed")
    details: dict[str, object] = {
        "workflow_name": "Thermal chain",
        "members": members,
        "member_count": len(members),
        "completed_count": completed,
        "failed_count": failed,
        "headless": False,
    }
    first_failed = next(
        ((i + 1, m["label"]) for i, m in enumerate(members) if m["state"] == "failed"),
        None,
    )
    if first_failed is not None:
        details["first_failed_position"] = first_failed[0]
        details["first_failed_label"] = first_failed[1]
    details.update(overrides)
    return SummaryFacts(
        surface_kind="workflow_run",
        surface_id=5,
        title="Thermal chain",
        state=state,
        duration_seconds=660.0,
        details=details,
    )


_FOUR = [
    {"label": "Material properties", "state": "complete"},
    {"label": "Heat transfer", "state": "complete"},
    {"label": "Heat sink", "state": "complete"},
    {"label": "Colour check", "state": "complete"},
]


def test_complete_lists_the_chain() -> None:
    text = render_workflow(_facts("complete", _FOUR))
    joined = " ".join(text.paragraphs)
    assert "Workflow Thermal chain completed: all 4 steps succeeded" in joined
    assert "Material properties → Heat transfer → Heat sink → Colour check" in joined
    assert "Total time 11 min" in joined


def test_complete_surfaces_final_step_results() -> None:
    text = render_workflow(
        _facts(
            "complete",
            _FOUR,
            final_step_label="Heat sink",
            final_results=[
                {"label": "Peak temperature", "value": 91.0, "unit": "°C"},
                {"label": "Mass", "value": 0.18, "unit": "kg"},
            ],
        )
    )
    joined = " ".join(text.paragraphs)
    assert "The final step (Heat sink) produced peak temperature 91 °C, mass 0.18 kg." in joined


def test_failed_names_first_failing_step() -> None:
    members = [
        {"label": "Material properties", "state": "complete"},
        {"label": "Heat transfer", "state": "failed"},
        {"label": "Heat sink", "state": "created"},
        {"label": "Colour check", "state": "created"},
    ]
    text = render_workflow(_facts("failed", members))
    joined = " ".join(text.paragraphs)
    assert "failed: step 2 of 4 (Heat transfer) did not complete" in joined
    assert "1 step completed before the halt" in joined
    assert "succeeded" not in joined


def test_cancelled_reports_progress() -> None:
    members = [
        {"label": "Material properties", "state": "complete"},
        {"label": "Heat transfer", "state": "cancelled"},
    ]
    text = render_workflow(_facts("cancelled", members))
    joined = " ".join(text.paragraphs)
    assert "was cancelled after 11 min" in joined
    assert "1 of 2 steps had completed" in joined
