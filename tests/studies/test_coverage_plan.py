"""The batch-size table — what each rung claims, and over which space (§11.5).

The centrepiece of *Fill the gaps*, and until now the only part of the coverage
engine with no tests of its own. What it reports is a **percentage**, and a
percentage is a ratio of two things measured over the same space: get the
denominator from one space and the numerator from another and the number is
wrong in a way nothing downstream can catch, because it is still a plausible
percentage.
"""

from __future__ import annotations

import itertools
import math

import pytest

from matofx.studies.coverage import (
    FactorOverride,
    analyse_design,
    build_envelope,
    resolve_points,
    suggestion_plan,
)
from tests.studies._coverage_fixtures import descriptor, num, select, training


def _archive():
    """Three numeric inputs, twenty-five runs, one input the batch can drop."""
    rows = [
        {
            "thickness": 1.0 + (i % 5) * 1.5,
            "heat_load": 1.0 + (i // 5) * 1.5,
            "width": 2.0 + (i % 3),
        }
        for i in range(25)
    ]
    spec = descriptor(
        num("thickness", 1.0, 9.0), num("heat_load", 1.0, 9.0), num("width", 2.0, 8.0)
    )
    points = resolve_points(training(rows), descriptor=spec)
    return points, spec


# --- the gap is a fraction of the space it was measured over ---------------


def test_the_gap_is_measured_against_the_space_the_figures_describe() -> None:
    """The default reading, and the one that was wrong.

    Untick an input and the batch is confined to a smaller space, but under
    *"Every input"* the radius is still taken over all three axes. Dividing
    that radius by the diagonal of the two-axis batch space reported a 25%
    gap where the honest figure is 20% — inflating in the alarming direction,
    and clamping towards 100% as more inputs are left out.
    """
    points, spec = _archive()
    whole = build_envelope(points, descriptor=spec)
    batch = build_envelope(points, descriptor=spec, overrides={"width": FactorOverride(hold=5.0)})

    plan = suggestion_plan(points, batch, seed=1, sizes=[5], measure_envelope=whole)

    assert len(whole.metric_axes) == 3, "the measurement keeps the unticked input"
    assert len(batch.metric_axes) == 2, "the batch does not"
    expected = plan.options[0].covering_radius / math.sqrt(len(whole.metric_axes))
    assert plan.options[0].gap_after == pytest.approx(expected)


def test_the_current_gap_uses_the_same_denominator_as_the_rungs() -> None:
    """ "Today" and "with N more runs" are read as one column. A denominator
    that differed between them would make the batch look like it had bought
    something it had not."""
    points, spec = _archive()
    whole = build_envelope(points, descriptor=spec)
    batch = build_envelope(points, descriptor=spec, overrides={"width": FactorOverride(hold=5.0)})

    plan = suggestion_plan(points, batch, seed=1, sizes=[5], measure_envelope=whole)

    today = analyse_design(points, whole)
    assert plan.current_gap == pytest.approx(
        today.covering_radius / math.sqrt(len(whole.metric_axes))
    )


def test_the_narrowed_reading_measures_the_batch_s_own_space() -> None:
    """Under *"Only the ones I ticked"* the two spaces coincide, so the
    denominator is the batch's — and the figure is legitimately smaller,
    because it is a statement about a smaller space."""
    points, spec = _archive()
    batch = build_envelope(points, descriptor=spec, overrides={"width": FactorOverride(hold=5.0)})

    narrowed = suggestion_plan(points, batch, seed=1, sizes=[5])
    whole = build_envelope(points, descriptor=spec)
    wide = suggestion_plan(points, batch, seed=1, sizes=[5], measure_envelope=whole)

    assert narrowed.options[0].gap_after == pytest.approx(
        narrowed.options[0].covering_radius / math.sqrt(len(batch.metric_axes))
    )
    assert narrowed.current_gap < wide.current_gap, (
        "a smaller claim is easier to satisfy, and must read that way"
    )


def test_every_reported_gap_is_a_fraction() -> None:
    """It is quoted as a percentage of the space, so it cannot exceed it."""
    points, spec = _archive()
    whole = build_envelope(points, descriptor=spec)
    batch = build_envelope(
        points,
        descriptor=spec,
        overrides={
            "width": FactorOverride(hold=5.0),
            "heat_load": FactorOverride(hold=5.0),
        },
    )

    plan = suggestion_plan(points, batch, seed=1, sizes=[3, 5], measure_envelope=whole)

    assert 0.0 <= plan.current_gap <= 1.0
    assert all(0.0 <= o.gap_after <= 1.0 for o in plan.options)


# --- a batch made only of choices -----------------------------------------


def _choice_archive():
    """One numeric input and one choice, with an option nobody has ever run."""
    spec = descriptor(num("thickness", 1.0, 9.0), select("material", "Al", "Steel", "Mg"))
    rows = [{"thickness": 1.0 + i * 0.4, "material": ["Al", "Steel"][i % 2]} for i in range(20)]
    return resolve_points(training(rows), descriptor=spec), spec


def test_a_choice_only_batch_is_planned_rather_than_refused() -> None:
    """Reported as "untick every number and you get nothing at all".

    A batch that sweeps the material options is exactly what an archive with
    forty runs on one material and two on another needs, and it is the one
    shape the panel refused to price.
    """
    points, spec = _choice_archive()
    batch = build_envelope(
        points, descriptor=spec, overrides={"thickness": FactorOverride(hold=5.0)}
    )

    plan = suggestion_plan(points, batch, seed=1, sizes=[3])

    assert plan.options, "a choice-only space still has gaps worth filling"
    assert plan.points


def test_a_choice_only_batch_reaches_for_the_option_nobody_has_run() -> None:
    """The emptiest cell first — that is what "fill the gaps" means when the
    space is a set of options rather than a volume."""
    points, spec = _choice_archive()
    batch = build_envelope(
        points, descriptor=spec, overrides={"thickness": FactorOverride(hold=5.0)}
    )

    first = suggestion_plan(points, batch, seed=1, sizes=[1]).points[0]

    assert first["material"] == "Mg"
    assert first["thickness"] == 5.0, "the held input still rides along"


def test_a_choice_only_batch_closes_the_option_shortfall() -> None:
    """The gap is a covering radius and says nothing about option balance, so
    the shortfall count is the only column carrying the news here."""
    points, spec = _choice_archive()
    batch = build_envelope(
        points, descriptor=spec, overrides={"thickness": FactorOverride(hold=5.0)}
    )

    plan = suggestion_plan(points, batch, seed=1, sizes=[6])

    assert plan.options[0].levels_short < plan.current_levels_short


# --- and when there really is nothing --------------------------------------


def test_unticking_everything_says_so_rather_than_blaming_the_archive() -> None:
    """The old message read "they are either held constant across every run or
    come from the solver", which after the user has just unticked them all is
    simply untrue."""
    points, spec = _archive()
    batch = build_envelope(
        points,
        descriptor=spec,
        overrides={
            "thickness": FactorOverride(hold=5.0),
            "heat_load": FactorOverride(hold=5.0),
            "width": FactorOverride(hold=5.0),
        },
    )

    plan = suggestion_plan(points, batch, seed=1, sizes=[5])

    (warning,) = plan.warnings
    assert warning.code == "all_inputs_held"
    assert "unticked" in warning.message


def test_an_archive_with_nothing_to_vary_still_blames_the_archive() -> None:
    """The other half of the split: here the message was always true."""
    spec = descriptor(num("thickness", 1.0, 9.0))
    rows = [{"thickness": 4.0, "heat_load": 2.0} for _ in range(8)]
    points = resolve_points(training(rows), descriptor=spec)

    plan = suggestion_plan(points, build_envelope(points, descriptor=spec), seed=1, sizes=[5])

    (warning,) = plan.warnings
    assert warning.code == "nothing_to_suggest"


def test_a_choice_batch_does_not_warn_about_repeats_it_had_to_make() -> None:
    """A batch of six into a three-option grid repeats by arithmetic. Warning
    about that would fire on the very case the feature is for — an option
    nobody has run getting the runs it needs."""
    points, spec = _choice_archive()
    batch = build_envelope(
        points, descriptor=spec, overrides={"thickness": FactorOverride(hold=5.0)}
    )

    plan = suggestion_plan(points, batch, seed=1, sizes=[6])

    assert not [w for w in plan.warnings if w.code == "options_repeated"]


def test_a_choice_batch_says_so_when_every_combination_is_already_run() -> None:
    """Then a repeat is the only move left, and the reader is owed the reason."""
    spec = descriptor(num("thickness", 1.0, 9.0), select("material", "Al", "Steel"))
    rows = [{"thickness": 1.0 + i * 0.4, "material": ["Al", "Steel"][i % 2]} for i in range(20)]
    points = resolve_points(training(rows), descriptor=spec)
    batch = build_envelope(
        points, descriptor=spec, overrides={"thickness": FactorOverride(hold=5.0)}
    )

    plan = suggestion_plan(points, batch, seed=1, sizes=[4])

    (warning,) = [w for w in plan.warnings if w.code == "options_repeated"]
    assert "every" in warning.message.lower()


# --- the ladder can only improve (2026-09-04) ------------------------------

_VERDICT_RANK = {"good": 0, "thin": 1, "poor": 2, "insufficient": 3}


def _monotone(plan) -> None:
    """Every rung at least as good as the one before it, on all three columns."""
    seen = [(plan.current_verdict, plan.current_gap, plan.current_score)]
    seen += [(o.verdict, o.gap_after, o.coverage_score) for o in plan.options]
    for (va, ga, sa), (vb, gb, sb) in itertools.pairwise(seen):
        assert _VERDICT_RANK[vb] <= _VERDICT_RANK[va], f"verdict went backwards: {va} -> {vb}"
        assert gb <= ga + 1e-9, f"gap grew: {ga:.3f} -> {gb:.3f}"
        assert sb >= sa - 1e-9, f"score fell: {sa:.3f} -> {sb:.3f}"


def test_the_ladder_never_goes_backwards() -> None:
    """Reported: *"I get poorly covered, poorly covered, patchy, poorly
    covered, poorly covered with an increasing number of runs, which doesn't
    make sense."*

    "Adding runs can never make the verdict worse" is a guarantee §4.4 went to
    some trouble to establish — the bar is fixed to the dimension, so the
    score moves only when the radius does, and a covering radius never rises
    as points are added. It is a property of the arithmetic, and it holds only
    while every term feeding the verdict is monotone. It broke when the pair
    *spread* reading — an efficiency, whose bar moves with the run count — was
    allowed to vote.
    """
    points, spec = _archive()
    envelope = build_envelope(points, descriptor=spec)

    _monotone(suggestion_plan(points, envelope, seed=1, sizes=[5, 10, 25, 50, 100]))


def test_the_ladder_never_goes_backwards_over_the_full_declared_range() -> None:
    """The reported scope: every input, widened to what the tool allows. It is
    the harshest reading, so it is where a non-monotone term shows first."""
    points, spec = _archive()
    widened = {
        f.name: FactorOverride(lo=f.declared_lo, hi=f.declared_hi)
        for f in build_envelope(points, descriptor=spec).factors
        if f.kind in ("continuous", "integer") and f.declared_lo is not None
    }
    envelope = build_envelope(points, descriptor=spec, overrides=widened)

    _monotone(suggestion_plan(points, envelope, seed=1, sizes=[5, 10, 25, 50, 100]))


def test_the_ladder_never_goes_backwards_with_a_choice_in_play() -> None:
    """A promoted choice brings the option-balance row and the choice pairs
    into the verdict — both new terms, both of which have to be monotone."""
    spec = descriptor(
        num("thickness", 1.0, 9.0), num("heat_load", 1.0, 9.0), select("material", "Al", "Mg")
    )
    rows = [
        {"thickness": 1.0 + (i % 5) * 1.5, "heat_load": 1.0 + (i // 5) * 1.5, "material": "Al"}
        for i in range(25)
    ]
    points = resolve_points(training(rows), descriptor=spec)
    promoted = build_envelope(
        points, descriptor=spec, overrides={"material": FactorOverride(levels=("Al", "Mg"))}
    )

    _monotone(suggestion_plan(points, promoted, seed=1, sizes=[5, 10, 25, 50, 100]))


def test_a_pair_crossing_the_floor_mid_ladder_cannot_worsen_the_verdict() -> None:
    """Reported twice. The second time: *"4, 8, 40 and 80 runs = Poorly
    covered, but 20 runs = Patchy."*

    The pairwise term is ignored below the run count where it can order a good
    design against a bad one. Left to each rung, a big enough batch crossed
    that line mid-ladder — and since the term can only subtract, crossing it
    made the verdict worse for adding runs. Every underlying reading is
    monotone; the *gate* was not. It is settled once, on the archive as it
    stands, and applied to every rung.
    """
    points, spec = _archive()
    envelope = build_envelope(points, descriptor=spec)
    plan = suggestion_plan(points, envelope, seed=1, sizes=[5, 10, 25, 50, 100])

    # The archive is under the floor, so no rung may be judged on its pairs —
    # including the ones whose projected size would clear it.
    assert not any(p.discriminating for p in analyse_design(points, envelope).per_pair)
    _monotone(plan)


def test_the_plan_says_when_the_pairwise_check_will_switch_on_later() -> None:
    """Pinning the gate keeps the ladder readable and makes the plan quieter
    than the panel will be once the runs land. A plan that promised one
    verdict while the report gives another is worse than no plan, so it says
    so rather than leaving it to be discovered."""
    points, spec = _archive()
    plan = suggestion_plan(points, build_envelope(points, descriptor=spec), seed=1, sizes=[5, 100])

    (warning,) = [w for w in plan.warnings if w.code == "pairs_unlock_later"]
    assert "60 runs a pair" in warning.message


def test_no_such_warning_once_the_pairs_already_count() -> None:
    """Nothing switches on later if it is already on."""
    rows = [
        {"thickness": 1.0 + (i % 12) * 0.7, "heat_load": 1.0 + (i // 12) * 0.9} for i in range(96)
    ]
    spec = descriptor(num("thickness", 1.0, 9.0), num("heat_load", 1.0, 9.0))
    points = resolve_points(training(rows), descriptor=spec)
    plan = suggestion_plan(points, build_envelope(points, descriptor=spec), seed=1, sizes=[5, 50])

    assert not [w for w in plan.warnings if w.code == "pairs_unlock_later"]
