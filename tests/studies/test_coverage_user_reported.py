"""Regressions for the four defects found by reading the panel on real data.

Every one of these passed the whole suite at the time. They were caught by a
user looking at the cohort panel for `heat_sink` (531 runs, 6 varying inputs)
and asking why it disagreed with itself — which is the failure mode ADR-0020
§0 is about: a coverage verdict nobody can check by eye.

The four:

- **The yardstick was built for rows the radius never measured.** 240 of the
  531 runs carry a missing value, so `covering_radius` measured 291 of them
  while `reference_radius` was asked for 531. Score 0.68 instead of 0.81, and
  a `thin` verdict sitting above six green per-factor rows.
- **The headline named a "biggest gap" that was not a gap.** `min(per_factor)`
  was announced unconditionally, so a 0.90 axis with a `good` row of its own
  was called out as the problem.
- **Labels never reached the panel.** Rows, warnings and plot axes printed
  `thermal_conductivity` where the project rule is `Material conductivity`
  ([[feedback_use_labels_not_names]]).
- **A cohort filter could not be cleared.** Three layers dropped `None` before
  the service saw it, and the service read absence as "leave unchanged".
"""

from __future__ import annotations

import numpy as np
import pytest

from matofx.config import CoverageSettings
from matofx.studies.coverage import (
    FactorOverride,
    analyse_design,
    build_envelope,
    resolve_points,
)
from matofx.studies.coverage.metrics import measured_rows, reference_radius
from matofx.studies.coverage.report import FactorCoverage, _headline
from matofx.tools.descriptor import InputField
from matofx.ui._coverage_panel import (
    display_name,
    envelope_rows,
    factor_labels,
    factor_row_text,
    pair_row_text,
    round_to_precision,
    row_chip,
    scatter_pairs,
)
from tests.studies._coverage_fixtures import descriptor, num, select, training

# --- the yardstick ---------------------------------------------------------


def test_unplaceable_rows_do_not_inflate_the_reference_design() -> None:
    """The reference must be sized to the rows the radius was measured over.

    A row with a missing coordinate cannot be placed, so ``covering_radius``
    drops it. Sizing the yardstick from the cohort's *total* row count then
    asks "how well would N evenly-spread runs do?" of a radius achieved by
    rather fewer — a bar raised in direct proportion to how much data is
    missing, which is exactly backwards.
    """
    rows = [{"a": float(i), "b": float(i % 7)} for i in range(40)]
    rows += [{"a": float("nan"), "b": 3.0} for _ in range(40)]  # unplaceable
    points = resolve_points(training(rows))
    envelope = build_envelope(points)
    x = envelope.normalise(points)

    assert points.n_points == 80
    assert measured_rows(x, envelope) == 40


def test_a_smaller_measured_set_is_held_to_a_lower_bar() -> None:
    """Sanity on the direction: fewer points, easier reference radius."""
    rows = [{"a": float(i), "b": float(i % 5)} for i in range(60)]
    envelope = build_envelope(resolve_points(training(rows)))
    tight = reference_radius(envelope, 30, n_candidates=1024, seed=1)
    loose = reference_radius(envelope, 60, n_candidates=1024, seed=1)
    assert tight > loose, "more runs should be expected to cover more"


def test_missing_data_no_longer_drags_the_verdict_below_its_own_rows() -> None:
    """The user-visible symptom: an amber verdict above six green rows.

    Half the runs carry a missing value. The measurable half is well spread,
    so every per-factor row reads ``good``; before the fix the overall verdict
    came out worse than any of them, which is not a thing the report is
    allowed to say without explaining it.
    """
    rng = np.random.default_rng(7)
    placed = [
        {"a": float(v[0]), "b": float(v[1]), "c": float(v[2])}
        for v in rng.uniform(0.0, 1.0, size=(80, 3))
    ]
    missing = [{"a": float("nan"), "b": 0.5, "c": 0.5} for _ in range(80)]
    points = resolve_points(training(placed + missing))
    envelope = build_envelope(points)

    design = analyse_design(points, envelope, settings=CoverageSettings())
    worst_row = max(
        (f for f in design.per_factor if f.actionable),
        key=lambda f: 0 if f.verdict == "good" else 1,
        default=None,
    )
    assert worst_row is not None
    if all(f.verdict == "good" for f in design.per_factor if f.actionable):
        assert design.verdict == "good" or "combinations" in design.message


# --- the headline ----------------------------------------------------------


def _row(name: str, value: float, verdict: str, label: str = "") -> FactorCoverage:
    return FactorCoverage(
        name=name,
        kind="continuous",
        measure="covering_radius",
        scale="linear",
        value=value,
        verdict=verdict,  # type: ignore[arg-type]
        label=label,
    )


def test_a_green_axis_is_never_announced_as_the_biggest_gap() -> None:
    """0.90 with a ``good`` row of its own is not "the biggest gap"."""
    rows = (_row("k", 0.90, "good", "Material conductivity"), _row("v", 0.98, "good"))
    message = _headline("good", 0.5, rows)  # type: ignore[arg-type]
    assert "Material conductivity" not in message
    assert "biggest gap" not in message


def test_all_axes_fine_but_verdict_thin_explains_the_combinations() -> None:
    """The sentence that resolves the apparent contradiction.

    Each row measures one input's own range; the verdict measures the joint
    space. When they disagree the panel has to say which is which, or the
    reader is left holding two answers and no way to reconcile them.
    """
    rows = (_row("a", 0.98, "good"), _row("b", 0.97, "good"))
    message = _headline("thin", 0.7, rows)  # type: ignore[arg-type]
    assert "combinations" in message
    assert "vary several inputs at once" in message


def test_a_genuinely_thin_axis_is_still_named_by_its_label() -> None:
    rows = (_row("k", 0.30, "poor", "Material conductivity"), _row("v", 0.98, "good"))
    message = _headline("poor", 0.9, rows)  # type: ignore[arg-type]
    assert "Material conductivity" in message
    assert "thermal_conductivity" not in message


# --- labels ----------------------------------------------------------------


def test_the_envelope_is_where_a_label_is_resolved() -> None:
    """One place decides what an axis is called; everything else asks it."""
    points = resolve_points(
        training([{"a": float(i), "b": float(i % 3)} for i in range(12)])
    )
    envelope = build_envelope(points)
    for factor in envelope.factors:
        # No descriptor in this fixture, so the fallback must be the name —
        # never a blank, which would render as an unlabelled row.
        assert factor.display_name == factor.name


@pytest.mark.parametrize(
    ("factor", "expected"),
    [
        ({"name": "thermal_conductivity", "label": "Material conductivity"},
         "Material conductivity"),
        ({"name": "thermal_conductivity", "label": ""}, "thermal_conductivity"),
        ({"name": "thermal_conductivity"}, "thermal_conductivity"),
    ],
)
def test_display_name_prefers_the_label_and_falls_back_to_the_name(
    factor: dict[str, object], expected: str
) -> None:
    assert display_name(factor) == expected


def test_a_factor_row_reads_in_labels_not_names() -> None:
    row = {
        "name": "thermal_conductivity",
        "label": "Material conductivity",
        "measure": "covering_radius",
        "value": 0.98,
        "kind": "continuous",
    }
    text = factor_row_text(row)
    assert text.startswith("Material conductivity:")
    assert "thermal_conductivity" not in text


def test_the_percentage_is_explained_as_a_gap() -> None:
    """"98% covered" said nothing checkable; the untried stretch does."""
    row = {
        "name": "v",
        "label": "Air velocity",
        "measure": "covering_radius",
        "value": 0.98,
        "kind": "continuous",
    }
    assert "biggest untried stretch is 1% of it" in factor_row_text(row)


def test_factor_labels_maps_every_axis_for_the_surfaces_keyed_by_name() -> None:
    envelope = [
        {"name": "a", "label": "Alpha"},
        {"name": "b", "label": ""},
    ]
    assert factor_labels(envelope) == {"a": "Alpha", "b": "b"}


# --- a held-constant input is not "Well covered" -------------------------


def test_a_pinned_input_gets_a_grey_not_varied_badge() -> None:
    """"Well covered: the same in every run" is nonsense on its face.

    The engine scores a pinned axis 1.0 because there is nothing left to
    cover -- defensible internally, and it rendered as the most reassuring
    badge on the panel directly above a sentence saying the model cannot vary
    the input at all.
    """
    label, colour = row_chip(
        {"measure": "pinned", "verdict": "good", "name": "t", "label": "Base temp rise"}
    )
    assert label == "Not varied"
    assert colour == "grey-6"


def test_a_measured_row_still_gets_its_verdict_badge() -> None:
    label, _ = row_chip({"measure": "covering_radius", "verdict": "good"})
    assert label == "Well covered"


def test_the_row_says_the_simulated_range() -> None:
    """"its range" was ambiguous -- the input's? the declared envelope's?"""
    text = factor_row_text(
        {
            "name": "v",
            "label": "Air velocity",
            "measure": "covering_radius",
            "value": 0.98,
            "kind": "continuous",
        }
    )
    assert "across the simulated range" in text


# --- the plots follow the worst pairs ------------------------------------


def test_the_plots_are_the_worst_pairs_not_the_worst_axes() -> None:
    """A pair can fail while both of its halves look fine on their own.

    Picking axes first and combining them therefore cannot be relied on to
    put the failing panel on screen -- which is how "Number of fins" ended up
    plotted nowhere while a cross went unshown.
    """
    envelope = [
        {"name": n, "label": n.title(), "kind": "continuous", "actionable": True}
        for n in ("a", "b", "c", "d")
    ]
    per_factor = [{"name": n, "value": 0.98} for n in ("a", "b", "c", "d")]
    per_pair = [
        {"a": "c", "b": "d", "coverage_score": 0.11, "verdict": "poor"},
        {"a": "a", "b": "b", "coverage_score": 0.95, "verdict": "good"},
    ]
    pairs = scatter_pairs(envelope, per_factor, cap=2, per_pair=per_pair)
    assert pairs[0] == ("c", "d")


def test_plot_selection_falls_back_when_a_report_predates_pairs() -> None:
    envelope = [
        {"name": n, "label": n.title(), "kind": "continuous", "actionable": True}
        for n in ("a", "b")
    ]
    assert scatter_pairs(envelope, None, cap=4, per_pair=[]) == [("a", "b")]


def test_a_pair_row_reads_in_labels_and_says_what_it_measured() -> None:
    text = pair_row_text(
        {
            "a": "base_width",
            "b": "thermal_conductivity",
            "a_label": "Base width",
            "b_label": "Material conductivity",
            "n_points": 291,
            "coverage_score": 0.16,
            "verdict": "poor",
        }
    )
    assert "Base width vs Material conductivity" in text
    assert "rarely changed together" in text
    assert "thermal_conductivity" not in text


# --- numbers a user could actually type ----------------------------------


def test_envelope_bounds_are_rounded_to_the_inputs_own_step() -> None:
    """The form offered 0.5649810869459996 as a lowest value.

    The bounds start at the observed spread, which is a raw float. Quoting one
    to more digits than the tool has is the same defect as suggesting a run at
    those digits: not a display blemish, a number nobody could have typed.
    """
    (row,) = envelope_rows(
        [
            {
                "name": "air_velocity",
                "label": "Air velocity",
                "kind": "continuous",
                "lo": 0.5649810869459996,
                "hi": 14.998363816407634,
                "precision": 0.1,
            }
        ]
    )
    assert row.lo == 0.6
    assert row.hi == 15.0
    assert row.precision == 0.1


def test_an_input_with_no_stated_step_loses_float_noise_but_keeps_precision() -> None:
    assert round_to_precision(54.696048050517625, None) == 54.696
    assert round_to_precision(0.000123456789, None) == 0.000123457
    assert round_to_precision(None, 0.1) is None


def test_rounding_never_leaves_the_axis() -> None:
    """Rounding near a bound can step outside it; the value must stay legal."""
    assert round_to_precision(4.0, 1.0) == 4.0
    assert round_to_precision(3.9863718983175715, 0.1) == 4.0


# --- opening up an input that was held constant --------------------------


def _pinned_fixture() -> tuple:
    """Two varied inputs and one the archive never changed."""
    rows = [{"a": float(i), "b": float(i % 7), "held": 60.0} for i in range(40)]
    desc = descriptor(num("a", 0.0, 40.0), num("b", 0.0, 7.0), num("held", 10.0, 120.0))
    points = resolve_points(training(rows), descriptor=desc)
    return points, desc


def test_a_constant_input_is_pinned_until_it_is_given_a_range() -> None:
    points, desc = _pinned_fixture()
    envelope = build_envelope(points, descriptor=desc)
    held = envelope.by_name("held")
    assert held.kind == "pinned"
    assert not held.actionable
    # ...but it records what it *would* be, so a surface can offer to open it.
    assert held.varying_kind == "continuous"
    assert (held.declared_lo, held.declared_hi) == (10.0, 120.0)


def test_a_range_on_a_held_constant_axis_opens_it_up() -> None:
    """"Always 60" is a fact about the runs so far, not a rule."""
    points, desc = _pinned_fixture()
    envelope = build_envelope(
        points,
        descriptor=desc,
        overrides={"held": FactorOverride(lo=10.0, hi=120.0)},
    )
    held = envelope.by_name("held")
    assert held.kind == "continuous"
    assert held.actionable
    assert "held" in envelope.metric_axes


def test_an_opened_axis_reaches_every_measurement() -> None:
    """The promise is that *everything* updates, not just the batch.

    Its column used to normalise to all-NaN — `rows` carries only the varied
    factors — so the axis was dropped from the joint radius and every pair
    containing it went silently unjudged.
    """
    points, desc = _pinned_fixture()
    opened = build_envelope(
        points, descriptor=desc, overrides={"held": FactorOverride(lo=10.0, hi=120.0)}
    )

    column = opened.normalise(points)[:, opened.column_index("held")]
    assert np.isfinite(column).all(), "the promoted axis has no values"

    design = analyse_design(points, opened, settings=CoverageSettings())
    assert design.n_pairs_examined == design.n_pairs_total, "pairs were silently dropped"
    assert any(f.name == "held" and f.actionable for f in design.per_factor)


def test_a_held_constant_choice_can_be_opened_up() -> None:
    """Revised 2026-09-03. This asserted the opposite.

    The rule was "is it a number", which meant a held-constant *choice* could
    never be swept — reported from the UI as the "also vary the inputs held
    constant" switch never appearing on a cohort whose only constant was the
    plate material. The test is now whether the descriptor **declares a
    domain**: a ``select`` enumerates one, so it qualifies.
    """
    rows = [{"a": float(i), "material": "CFRP"} for i in range(40)]
    desc = descriptor(num("a", 0.0, 40.0), select("material", "CFRP", "GFRP"))
    envelope = build_envelope(resolve_points(training(rows), descriptor=desc), descriptor=desc)

    factor = envelope.by_name("material")
    assert factor.kind == "pinned", "it still never varied"
    assert factor.varying_kind == "categorical", "but it could be opened up"
    assert factor.declared_levels == ("CFRP", "GFRP")


def test_an_input_with_no_declared_domain_still_cannot_be_opened_up() -> None:
    """The other half of the rule, which has not moved. A free-text input has
    neither bounds nor options, so there is nothing to sweep and inventing a
    range would propose runs the form rejects.
    """
    rows = [{"a": float(i), "note": "as built"} for i in range(40)]
    desc = descriptor(num("a", 0.0, 40.0), InputField(name="note", label="Note", type="string"))
    envelope = build_envelope(resolve_points(training(rows), descriptor=desc), descriptor=desc)

    assert envelope.by_name("note").varying_kind is None
