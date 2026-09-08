"""Slice 3 — the parametric collector's method-aware analysis selection.

DB-free: the collector only touches the session for descriptor labels, and
skips that entirely when ``tool_version_id`` is ``None``. So we drive it with
an unpersisted :class:`ParametricStudy` + a hand-built results view carrying
synthetic-but-realistic runs, and assert the ``analysis.kind`` chosen per design
mode plus the computed figures.
"""

from __future__ import annotations

from typing import Any

import pytest

from matofx.studies.orm import ParametricStudy
from matofx.studies.results_service import StudyChildResult, StudyResultsView
from matofx.summaries import NonTerminalResultError
from matofx.summaries.collectors import collect_parametric_facts

pytestmark = pytest.mark.asyncio


def _study(mode: str, *, state: str = "complete") -> ParametricStudy:
    return ParametricStudy(
        mode=mode,
        state=state,
        name=f"{mode} study",
        generated_count=0,
        skipped_invalid_count=0,
        run_cap=100,
        seed=None,
        tool_version_id=None,  # → collector skips the descriptor lookup (no session)
    )


def _view(rows: list[tuple[dict[str, float], dict[str, float]]]) -> StudyResultsView:
    children = [
        StudyChildResult(
            job_id=1000 + i,
            state="complete",
            inputs=dict(inputs),
            scalars=dict(scalars),
            retained_files=[],
        )
        for i, (inputs, scalars) in enumerate(rows)
    ]
    return StudyResultsView(study_id=1, tool_alias="t", tool_version_id=None, children=children)


def _grid_rows() -> list[tuple[dict[str, float], dict[str, float]]]:
    """3x3 grid with a paraboloid response t = x^2 + y^2 (clean RSM minimum)."""
    rows = []
    for x in (-1.0, 0.0, 1.0):
        for y in (-1.0, 0.0, 1.0):
            rows.append(({"x": x, "y": y}, {"t": x * x + y * y}))
    return rows


async def _facts(study: ParametricStudy, view: StudyResultsView) -> Any:
    return await collect_parametric_facts(None, study=study, results=view)  # type: ignore[arg-type]


async def test_non_terminal_is_refused() -> None:
    with pytest.raises(NonTerminalResultError):
        await _facts(_study("lhs", state="running"), _view(_grid_rows()))


async def test_ccd_selects_response_surface_with_minimum() -> None:
    facts = await _facts(_study("ccd"), _view(_grid_rows()))
    analysis = facts.details["analysis"]
    assert analysis["kind"] == "response_surface"
    assert analysis["r2"] is not None
    # the paraboloid's minimum sits at the origin
    assert analysis["stationary_kind"] == "minimum"
    assert abs(analysis["stationary_value"]) < 1e-6


async def test_grid_selects_anova() -> None:
    facts = await _facts(_study("grid"), _view(_grid_rows()))
    analysis = facts.details["analysis"]
    assert analysis["kind"] == "anova"
    assert analysis["terms"]
    assert "residual" in analysis


async def test_plackett_burman_selects_screening_effects() -> None:
    # two-level factors, response driven mostly by x
    rows = [
        ({"x": -1.0, "y": -1.0}, {"t": -10.0}),
        ({"x": 1.0, "y": -1.0}, {"t": 10.0}),
        ({"x": -1.0, "y": 1.0}, {"t": -8.0}),
        ({"x": 1.0, "y": 1.0}, {"t": 12.0}),
    ]
    facts = await _facts(_study("plackett_burman"), _view(rows))
    analysis = facts.details["analysis"]
    assert analysis["kind"] == "screening_effects"
    # x has the larger main effect → first in the ranked list
    assert analysis["effects"][0]["factor"] == "x"


async def test_lhs_selects_src_regression() -> None:
    rows = [({"x": float(i), "y": float(10 - i)}, {"t": 2.0 * i + 1.0}) for i in range(8)]
    facts = await _facts(_study("lhs"), _view(rows))
    analysis = facts.details["analysis"]
    assert analysis["kind"] == "src_regression"
    assert analysis["factors"]


async def test_each_response_gets_a_top_driver() -> None:
    # t rises with x; mass rises with y → each response's dominant driver differs.
    rows = [({"x": float(i), "y": float(2 * i)}, {"t": 3.0 * i, "mass": 5.0 * i}) for i in range(8)]
    facts = await _facts(_study("lhs"), _view(rows))
    responses = {r["label"]: r for r in facts.details["responses"]}
    assert responses["t"]["top_driver"] is not None
    assert responses["mass"]["top_driver"] is not None
    assert responses["t"]["top_driver"]["sign"] == "positive"


async def test_responses_carry_ranges_and_run_ids() -> None:
    facts = await _facts(_study("lhs"), _view(_grid_rows()))
    response = facts.details["responses"][0]
    assert response["min"] == 0.0
    assert response["max"] == 2.0
    assert response["min_run"] is not None
    assert response["max_run"] is not None
    assert facts.details["factors"] == ["x", "y"]


async def test_no_varied_factors_degrades_to_none() -> None:
    rows = [({"x": 1.0}, {"t": 5.0}), ({"x": 1.0}, {"t": 5.0})]
    facts = await _facts(_study("lhs"), _view(rows))
    assert facts.details["analysis"]["kind"] == "none"
