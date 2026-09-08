"""A suggested run has to be one the tool would accept (ADR-0020 §5.2).

The suggestion path sampled each varying axis inside its own declared bounds,
independently, and proposed the result. A descriptor says a great deal more
than "each input lies in this box": ``set_value`` forces, formula-driven
bounds, ``require`` validity rules, cross-field constraints, and **sum-checks**
— a set of inputs that must total a fixed value.

Measured on a three-percentage sum-check before the fix: **0 of 8** proposed
runs were submittable, every one totalling something other than 100. The batch
was priced, listed run by run and committed, and the generator then dropped
every point as invalid — so the user asked for eight runs and got none, with
the shortfall explained nowhere.
"""

from __future__ import annotations

from typing import Any

from matofx.studies.coverage import build_envelope, resolve_points, suggest_design_points
from matofx.studies.coverage.rules import feasible_points, sum_groups_for
from matofx.tools.descriptor import (
    CrossFieldConstraint,
    Dependency,
    InputField,
    OutputField,
    RequireEffect,
    SetMinMaxEffect,
    SetValueEffect,
    SumCheck,
    ToolDescriptor,
)
from matofx.tools.forms.model import resolve_for_generation
from tests.studies._coverage_fixtures import training

_TOTAL = 100.0


def _percent(name: str, *, with_check: bool = True) -> InputField:
    """One percentage member of a three-way sum-check."""
    return InputField(
        name=name,
        label=name.replace("_", " ").title(),
        type="float",
        minimum=0.0,
        maximum=100.0,
        sum_check=(
            SumCheck(value=_TOTAL, sum_parameters=["p1", "p2", "p3"])
            if with_check
            else None
        ),
    )


def _blend_descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        alias="blend",
        name="Blend",
        version="1.0.0",
        description="three percentages that must total 100",
        main_script="static/main.py",
        inputs=[_percent("p1"), _percent("p2"), _percent("p3")],
        outputs=[OutputField(name="y", label="Y", type="number")],
    )


def _feasible_rows() -> list[dict[str, float]]:
    """Existing runs that *do* satisfy the sum-check."""
    return [
        {"p1": float(a), "p2": float(b), "p3": float(100 - a - b)}
        for a in (10, 20, 30, 40)
        for b in (10, 20, 30, 40)
    ]


def _sums(points: Any) -> list[float]:
    return [round(sum(float(p[k]) for k in ("p1", "p2", "p3")), 6) for p in points]


# --- the sum-check is repaired, not rejected ------------------------------


def test_every_suggested_run_satisfies_the_sum_check() -> None:
    """The regression. Before the fix this was 0 of 8."""
    descriptor = _blend_descriptor()
    points = resolve_points(training(_feasible_rows()), descriptor=descriptor)
    envelope = build_envelope(points, descriptor=descriptor)

    batch = suggest_design_points(points, envelope, n=8, seed=20_020)

    assert len(batch.points) == 8, "the batch could not be filled"
    assert _sums(batch.points) == [_TOTAL] * 8


def test_every_suggested_run_passes_the_generator_that_will_receive_it() -> None:
    """The check that matters: the *same* call the study generator makes.

    A batch that passes here cannot be dropped there, which is the failure the
    rules pass exists to close.
    """
    descriptor = _blend_descriptor()
    points = resolve_points(training(_feasible_rows()), descriptor=descriptor)
    envelope = build_envelope(points, descriptor=descriptor)

    for point in suggest_design_points(points, envelope, n=6, seed=7).points:
        _, errors = resolve_for_generation(descriptor, dict(point))
        assert not errors, f"the generator would reject this run: {errors}"


def test_the_suggestions_still_vary_the_free_members() -> None:
    """Deriving one member must not collapse the batch onto a single blend."""
    descriptor = _blend_descriptor()
    points = resolve_points(training(_feasible_rows()), descriptor=descriptor)
    envelope = build_envelope(points, descriptor=descriptor)

    batch = suggest_design_points(points, envelope, n=8, seed=3)
    for name in ("p1", "p2", "p3"):
        assert len({round(float(p[name]), 6) for p in batch.points}) > 1, (
            f"{name} took a single value across the batch"
        )


# --- the group record ------------------------------------------------------


def test_a_sum_group_is_built_only_for_the_axes_being_varied() -> None:
    descriptor = _blend_descriptor()
    groups = sum_groups_for(descriptor, ["p1", "p2", "p3"])
    assert len(groups) == 1
    assert groups[0]["value"] == _TOTAL
    assert groups[0]["balance"] in {"p1", "p2", "p3"}


def test_one_free_member_is_not_a_group() -> None:
    """With one varied member the sum determines it; there is no freedom left."""
    assert sum_groups_for(_blend_descriptor(), ["p1"]) == []


def test_a_tool_without_a_sum_check_costs_nothing() -> None:
    plain = ToolDescriptor(
        alias="plain",
        name="Plain",
        version="1.0.0",
        description="no rules",
        main_script="static/main.py",
        inputs=[_percent("p1", with_check=False)],
        outputs=[OutputField(name="y", label="Y", type="number")],
    )
    assert sum_groups_for(plain, ["p1"]) == []


# --- everything else is rejected, with the tool's own words ---------------


def test_a_residual_the_balance_cannot_absorb_is_dropped_with_a_reason() -> None:
    """Repair has limits, and reaching one is a rejection — not a silent clamp.

    Two members at 90 leave the third needing -80 to make 100, which is outside
    its own range. Clamping it would produce a run whose inputs do not satisfy
    the very rule the derivation exists to keep, so the point goes and the
    reason is recorded.
    """
    kept, dropped = feasible_points(
        _blend_descriptor(),
        [{"p1": 10.0, "p2": 90.0, "p3": 90.0}],
        varying=["p1", "p2", "p3"],
    )
    assert not kept
    assert dropped, "a rejected point reported no reason"


def test_a_sum_check_repairs_rather_than_rejects() -> None:
    """The distinction the two rules turn on.

    An equality cannot be satisfied by filtering random points — doing so would
    turn a broken feature into an empty one — so the balance member absorbs the
    residual instead.
    """
    descriptor = _blend_descriptor()
    kept, _ = feasible_points(
        descriptor,
        [{"p1": 900.0, "p2": 50.0, "p3": 0.0}],
        varying=["p1", "p2", "p3"],
    )
    assert len(kept) == 1, "a repairable point was thrown away"
    assert _sums(kept) == [_TOTAL]


def test_no_descriptor_means_no_opinion() -> None:
    """An ad-hoc cohort whose tool has been removed still gets its batch."""
    rows = [{"p1": 1.0, "p2": 2.0}]
    kept, dropped = feasible_points(None, rows, varying=["p1", "p2"])
    assert kept == rows
    assert not dropped


# --- every rule kind, not just sum-checks ---------------------------------

_FINE_MAX_THICKNESS = 5.0
_WIDTH_MULTIPLE = 3.0
_FORCED_PASSES = 3


def _ruled_descriptor() -> ToolDescriptor:
    """One tool carrying four rules, each of a different shape.

    They exercise different code paths: a cross-field constraint and a
    sum-check run on every point, while a ``require``, a formula-driven bound
    and a ``set_value`` force only exist when the descriptor declares
    ``dependencies`` -- which is exactly when ``resolve_for_generation`` stops
    short-circuiting.
    """
    return ToolDescriptor(
        alias="ruled",
        name="Ruled",
        version="1.0.0",
        description="a tool with four kinds of input rule",
        main_script="static/main.py",
        inputs=[
            InputField(
                name="thickness", label="Thickness", type="float", minimum=1.0, maximum=10.0
            ),
            InputField(name="width", label="Width", type="float", minimum=1.0, maximum=60.0),
            InputField(name="passes", label="Passes", type="integer", minimum=1, maximum=9),
            InputField(
                name="mode",
                label="Mode",
                type="select",
                options=["coarse", "fine"],
                default="fine",
            ),
        ],
        outputs=[OutputField(name="y", label="Y", type="number")],
        cross_field_constraints=[
            CrossFieldConstraint(
                expression="width > thickness", error_message="Width must exceed Thickness."
            )
        ],
        dependencies=[
            Dependency(
                when="mode == 'fine'",
                effects=[
                    RequireEffect(
                        field="thickness",
                        expr="thickness <= 5",
                        message="In fine mode Thickness must be 5 or less.",
                    ),
                    SetMinMaxEffect(field="width", minimum_expr="thickness * 3"),
                    SetValueEffect(field="passes", value=_FORCED_PASSES),
                ],
            )
        ],
    )


def _ruled_rows() -> list[dict[str, Any]]:
    """Runs spread across the **full** declared ranges.

    A fixture clustered where every rule happens to hold is a test that cannot
    fail: at thickness 9 the formula bound demands width >= 27, and the require
    rule forbids thickness above 5 at all, so the suggestions have to be
    filtered rather than merely happening to comply.
    """
    return [
        {
            "thickness": 1.0 + i * 3.0,
            "width": 1.0 + j * 19.0,
            "passes": _FORCED_PASSES,
            "mode": "fine",
        }
        for i in range(4)
        for j in range(4)
    ]


def _breaks_a_rule(point: Any) -> bool:
    thickness, width = float(point["thickness"]), float(point["width"])
    return not (
        width > thickness
        and thickness <= _FINE_MAX_THICKNESS
        and width >= thickness * _WIDTH_MULTIPLE - 1e-9
        and point.get("passes") == _FORCED_PASSES
    )


def test_every_kind_of_input_rule_is_honoured() -> None:
    """Not only sum-checks: constraints, requires, formula bounds and forces."""
    descriptor = _ruled_descriptor()
    points = resolve_points(training(_ruled_rows()), descriptor=descriptor)
    envelope = build_envelope(points, descriptor=descriptor)

    batch = suggest_design_points(points, envelope, n=8, seed=20_020)

    assert batch.points, "no runs could be proposed at all"
    offenders = [p for p in batch.points if _breaks_a_rule(p)]
    assert not offenders, f"{len(offenders)} proposed runs break a rule: {offenders[:2]}"


def test_those_rules_would_otherwise_be_broken() -> None:
    """The control that makes the test above mean something.

    Withhold the descriptor and the same seed proposes runs that violate the
    rules — so the assertion up there is the filter doing work, not the design
    happening to comply.
    """
    points = resolve_points(training(_ruled_rows()))  # no descriptor, no rules pass
    envelope = build_envelope(points)

    batch = suggest_design_points(points, envelope, n=8, seed=20_020)
    assert sum(_breaks_a_rule(p) for p in batch.points) > 1, (
        "the fixture cannot produce a violation, so it cannot prove a filter"
    )
