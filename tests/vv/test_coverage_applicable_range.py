"""Coverage fills in the V&V applicable range (ADR-0020 §10, slice 7).

``vv_record.applicable_range_json`` is authored by hand today and purely
documentary. A surrogate model version already carries a **declared envelope**
— the same object the predict-time guard enforces — so when a V&V record is
recorded against an exported surrogate tool, that envelope is offered as the
range. The envelope stops being an assertion somebody typed and becomes the
thing the platform already checks against.

The offer is a **pre-fill**, never a replacement: a curator can widen, narrow
or ignore it. What matters is that they start from what the model actually
supports rather than from an empty box.

**The schemas do not match, and that is the interesting part.** The V&V column
is ``{factor: {"min", "max"}}`` — numeric bounds only. An envelope also
carries categorical levels and non-numeric pinned values, which those bounds
cannot express. Dropping them silently would make the V&V record claim a
*wider* applicability than the guard enforces, which is the exact inversion
this whole feature exists to prevent — so they are reported instead.
"""

from __future__ import annotations

import datetime as dt

from matofx.studies.coverage import (
    FactorOverride,
    analyse_design,
    build_envelope,
    build_report,
    resolve_points,
)
from matofx.vv.coverage_range import applicable_range_from_report
from tests.studies._coverage_fixtures import (
    boolean,
    descriptor,
    field_training,
    integer,
    num,
    select,
    training,
    two_factor_rows,
)

_NOW = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)


def _report(rows=None, **kwargs) -> dict:
    resolve_kwargs = {k: kwargs.pop(k) for k in ("descriptor", "factor_spec") if k in kwargs}
    rows = rows if rows is not None else two_factor_rows(5, 5)
    points = resolve_points(training(rows), **resolve_kwargs)
    envelope = build_envelope(points, **resolve_kwargs, **kwargs)
    return build_report(
        analyse_design(points, envelope),
        envelope=envelope,
        source_kind="cohort",
        computed_at=_NOW,
    ).to_json_obj()


# --- the numeric axes transfer directly ----------------------------------


def test_continuous_axes_become_min_max_bounds() -> None:
    suggestion = applicable_range_from_report(_report())
    assert suggestion.ranges["thickness"] == {"min": 1.0, "max": 9.0}
    assert suggestion.ranges["heat_load"] == {"min": 1.0, "max": 9.0}


def test_a_narrowed_envelope_is_what_gets_offered() -> None:
    # The curator declared they only trust 3-6; the V&V claim should say so
    # rather than restating the raw training spread.
    report = _report(
        [{"thickness": float(t), "heat_load": float(t)} for t in range(1, 10)],
        overrides={"thickness": FactorOverride(lo=3.0, hi=6.0)},
    )
    assert applicable_range_from_report(report).ranges["thickness"] == {"min": 3.0, "max": 6.0}


def test_an_integer_axis_transfers_as_its_lattice_bounds() -> None:
    rows = [{"ply_count": float(n), "thickness": float(n)} for n in (1, 2, 3, 4, 5)] * 2
    report = _report(
        rows, descriptor=descriptor(integer("ply_count", 1, 5), num("thickness", 0.0, 9.0))
    )
    assert applicable_range_from_report(report).ranges["ply_count"] == {"min": 1.0, "max": 5.0}


def test_a_pinned_numeric_input_becomes_a_zero_width_bound() -> None:
    # The validation covers exactly that value and no other — a range of
    # [v, v] says so in the schema the column actually has.
    rows = [{"thickness": float(i), "ambient": 20.0} for i in range(1, 12)]
    ranges = applicable_range_from_report(_report(rows)).ranges
    assert ranges["ambient"] == {"min": 20.0, "max": 20.0}


def test_a_derived_axis_transfers_because_the_guard_enforces_it() -> None:
    # A field validated over snapshots 0-5 does not cover snapshot 500, and
    # the V&V claim should not imply otherwise.
    rows = [
        {"thickness": float(t), "time": float(s)} for t in (1.0, 3.0, 5.0, 7.0) for s in range(6)
    ]
    points = resolve_points(field_training(rows))
    envelope = build_envelope(points)
    report = build_report(
        analyse_design(points, envelope),
        envelope=envelope,
        source_kind="cohort",
        computed_at=_NOW,
    ).to_json_obj()
    assert applicable_range_from_report(report).ranges["time"] == {"min": 0.0, "max": 5.0}


# --- what the schema cannot carry is reported, never dropped -------------


def test_a_categorical_axis_cannot_be_expressed_and_says_so() -> None:
    """The inversion this feature exists to prevent, in miniature.

    ``{"min", "max"}`` cannot say "cfrp or gfrp, and nothing else". Silently
    omitting the constraint would leave the V&V record claiming applicability
    to materials the model has never seen — a *wider* claim than the guard
    enforces.
    """
    rows = [{"thickness": float(i), "material": "cfrp" if i % 2 else "gfrp"} for i in range(1, 13)]
    suggestion = applicable_range_from_report(
        _report(
            rows,
            descriptor=descriptor(num("thickness", 0.0, 20.0), select("material", "cfrp", "gfrp")),
        )
    )
    assert "material" not in suggestion.ranges
    assert "material" in suggestion.unexpressible
    assert "material" in suggestion.note
    assert "cfrp" in suggestion.note


def test_a_pinned_non_numeric_input_is_reported_too() -> None:
    rows = [{"thickness": float(i), "mesh": "fine"} for i in range(1, 12)]
    suggestion = applicable_range_from_report(_report(rows))
    assert "mesh" not in suggestion.ranges
    assert "mesh" in suggestion.unexpressible
    assert "fine" in suggestion.note


def test_a_boolean_axis_is_reported_rather_than_bounded() -> None:
    rows = [{"thickness": float(i), "radiation": i % 2 == 0} for i in range(1, 13)]
    suggestion = applicable_range_from_report(
        _report(rows, descriptor=descriptor(num("thickness", 0.0, 20.0), boolean("radiation")))
    )
    assert "radiation" in suggestion.unexpressible


def test_nothing_unexpressible_produces_no_caveat() -> None:
    suggestion = applicable_range_from_report(_report())
    assert suggestion.unexpressible == ()
    assert "couldn't" not in suggestion.note.lower()


# --- provenance ----------------------------------------------------------


def test_the_note_says_where_the_range_came_from() -> None:
    # A curator has to be able to tell a pre-filled range from one somebody
    # typed, a year later.
    note = applicable_range_from_report(_report()).note
    assert "coverage" in note.lower()
    assert "edit" in note.lower() or "adjust" in note.lower()


def test_the_note_dates_the_analysis() -> None:
    assert "2026" in applicable_range_from_report(_report()).note


# --- degenerate input ----------------------------------------------------


def test_no_report_offers_nothing() -> None:
    suggestion = applicable_range_from_report(None)
    assert suggestion.ranges == {}
    assert suggestion.note == ""


def test_an_empty_report_offers_nothing() -> None:
    assert applicable_range_from_report({}).ranges == {}


def test_a_report_with_no_numeric_axes_offers_nothing_but_still_explains() -> None:
    rows = [{"material": "cfrp" if i % 2 else "gfrp"} for i in range(1, 13)]
    suggestion = applicable_range_from_report(
        _report(rows, descriptor=descriptor(select("material", "cfrp", "gfrp")))
    )
    assert suggestion.ranges == {}
    assert "material" in suggestion.unexpressible


def test_the_shape_matches_what_the_column_stores() -> None:
    # `{factor: {"min": float, "max": float}}` — the same shape
    # `resolve_envelope_warning` already reads on submit.
    ranges = applicable_range_from_report(_report()).ranges
    for bounds in ranges.values():
        assert set(bounds) == {"min", "max"}
        assert all(isinstance(v, float) for v in bounds.values())
