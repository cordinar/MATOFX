"""The coverage report as stored evidence (ADR-0020 §9, §10, slice 5).

Coverage is **persisted and versioned, not a transient panel**: the verdict,
the criterion, the declared envelope and the breakdowns travel with the model
so they reach the model record, the per-job report and the V&V trail.

Two rules shape everything here. The columns are **nullable**, so every
version fitted before this feature existed is untouched — and an absent report
renders "not assessed", **never** "good", because silence about coverage and a
clean bill of health are opposite claims. And the stored shape must survive a
JSON round-trip without losing anything the guard (§8) or the V&V pre-fill
(§10) will later read back.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from matofx.config import CoverageSettings
from matofx.studies.coverage import (
    CoverageReport,
    FactorOverride,
    analyse_design,
    build_envelope,
    build_report,
    resolve_points,
)
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

_NOW = dt.datetime(2026, 8, 24, 11, 30, tzinfo=dt.UTC)


def _report(rows=None, **kwargs) -> CoverageReport:
    resolve_kwargs = {k: kwargs.pop(k) for k in ("descriptor", "factor_spec") if k in kwargs}
    rows = rows if rows is not None else two_factor_rows(4, 4)
    points = resolve_points(training(rows), **resolve_kwargs)
    envelope = build_envelope(points, **resolve_kwargs, **kwargs)
    return build_report(
        analyse_design(points, envelope),
        envelope=envelope,
        source_kind="cohort",
        computed_at=_NOW,
        points=points,
    )


# --- the round trip -------------------------------------------------------


def test_a_report_survives_a_json_round_trip() -> None:
    original = _report()
    restored = CoverageReport.from_json_obj(json.loads(json.dumps(original.to_json_obj())))

    assert restored.verdict == original.verdict
    assert restored.source_kind == original.source_kind
    assert restored.design.criterion == original.design.criterion
    assert restored.design.message == original.design.message
    assert restored.design.coverage_score == pytest.approx(original.design.coverage_score)
    assert restored.computed_at == original.computed_at


def test_the_stored_form_is_actually_json_serialisable() -> None:
    # The in-memory report carries tuple-keyed correlations and numpy-derived
    # floats; neither survives `json.dumps` untouched.
    payload = _report().to_json_obj()
    assert json.loads(json.dumps(payload)) == payload


def test_the_declared_envelope_round_trips_because_the_guard_enforces_it() -> None:
    # §9: the envelope is stored *inside* the report and is the single source
    # for the predict-time guard and the V&V range. A copy that lost a bound
    # would let the guard pass a point the analysis had excluded.
    original = _report(
        [{"thickness": float(t), "heat_load": float(t)} for t in range(1, 10)],
        overrides={"thickness": FactorOverride(lo=2.0, hi=6.0)},
    )
    restored = CoverageReport.from_json_obj(original.to_json_obj())

    axis = restored.envelope.by_name("thickness")
    assert (axis.lo, axis.hi) == (2.0, 6.0)
    assert restored.envelope.contains({"thickness": 4.0, "heat_load": 4.0})
    assert not restored.envelope.contains({"thickness": 9.0, "heat_load": 4.0})


def test_every_factor_kind_round_trips() -> None:
    rows = [
        {
            "thickness": float(i),
            "ply_count": float(i % 4 + 1),
            "material": "cfrp" if i % 2 else "gfrp",
            "use_radiation": True,
            "mesh": "fine",
        }
        for i in range(1, 13)
    ]
    desc = descriptor(
        num("thickness", 0.0, 20.0),
        integer("ply_count", 1, 4),
        select("material", "cfrp", "gfrp"),
        boolean("use_radiation"),
    )
    original = _report(rows, descriptor=desc)
    restored = CoverageReport.from_json_obj(original.to_json_obj())

    kinds = {f.name: f.kind for f in restored.envelope.factors}
    assert kinds["thickness"] == "continuous"
    assert kinds["ply_count"] == "integer"
    assert kinds["material"] == "categorical"
    assert kinds["mesh"] == "pinned"
    assert restored.envelope.by_name("mesh").pinned_value == "fine"
    assert restored.envelope.by_name("ply_count").lattice() == (1, 2, 3, 4)


def test_a_derived_axis_round_trips_so_the_guard_can_still_reject_beyond_it() -> None:
    rows = [
        {"thickness": float(t), "time": float(s)} for t in (1.0, 3.0, 5.0, 7.0) for s in range(6)
    ]
    points = resolve_points(field_training(rows))
    envelope = build_envelope(points)
    original = build_report(
        analyse_design(points, envelope),
        envelope=envelope,
        source_kind="cohort",
        computed_at=_NOW,
        points=points,
    )
    restored = CoverageReport.from_json_obj(original.to_json_obj())

    axis = restored.envelope.by_name("time")
    assert axis.kind == "derived"
    assert not restored.envelope.contains({"thickness": 4.0, "time": 500.0})


def test_the_per_factor_strip_round_trips() -> None:
    original = _report()
    restored = CoverageReport.from_json_obj(original.to_json_obj())
    assert {(f.name, f.measure, f.verdict) for f in restored.design.per_factor} == {
        (f.name, f.measure, f.verdict) for f in original.design.per_factor
    }


def test_the_warnings_round_trip_with_their_factor_attribution() -> None:
    rows = [{"thickness": float(i), "mesh_density": "fine"} for i in range(1, 21)]
    restored = CoverageReport.from_json_obj(_report(rows).to_json_obj())
    pinned = next(w for w in restored.warnings if w.code == "pinned_factor")
    assert pinned.factor == "mesh_density"
    assert pinned.message


def test_the_correlation_matrix_round_trips_through_its_flattened_keys() -> None:
    # JSON cannot express a tuple key, so the pair flattens to "a|b" on the
    # way out and must come back as a pair.
    original = _report()
    restored = CoverageReport.from_json_obj(original.to_json_obj())
    assert restored.design.diagnostics.factor_correlations == pytest.approx(
        original.design.diagnostics.factor_correlations
    )


def test_correlations_that_were_omitted_stay_omitted() -> None:
    # None ("we cannot tell") must not come back as {} ("we checked, nothing").
    rows = [{"thickness": float(i), "material": "cfrp" if i % 2 else "gfrp"} for i in range(1, 11)]
    restored = CoverageReport.from_json_obj(_report(rows).to_json_obj())
    assert restored.design.diagnostics.factor_correlations is None
    assert restored.design.diagnostics.max_abs_correlation is None


def test_the_diagnostics_counts_round_trip() -> None:
    td = training(
        [{"thickness": float(i), "heat_load": float(20 - i)} for i in range(20)],
        n_excluded_invalid=7,
        n_excluded_filtered=3,
    )
    points = resolve_points(td)
    envelope = build_envelope(points)
    original = build_report(
        analyse_design(points, envelope),
        envelope=envelope,
        source_kind="cohort",
        computed_at=_NOW,
        points=points,
    )
    restored = CoverageReport.from_json_obj(original.to_json_obj())
    assert restored.design.diagnostics.n_excluded_invalid == 7
    assert restored.design.diagnostics.n_excluded_filtered == 3


def test_the_training_points_are_not_stored() -> None:
    # The scatter matrix is a live view of the cohort; storing a copy of every
    # training point on every version would bloat the row for data the model
    # already references, and go stale the moment the cohort grows.
    payload = _report().to_json_obj()
    assert "design_points" not in payload
    assert CoverageReport.from_json_obj(payload).design_points == ()


# --- degenerate and forward-compatible payloads --------------------------


def test_an_insufficient_report_round_trips() -> None:
    original = _report([{"thickness": 1.0, "heat_load": 2.0} for _ in range(6)])
    assert original.verdict == "insufficient"
    restored = CoverageReport.from_json_obj(original.to_json_obj())
    assert restored.verdict == "insufficient"
    assert restored.design.message


def test_an_unknown_key_from_a_newer_release_is_ignored_not_fatal() -> None:
    # A stored report written by a later version must not break an older
    # reader: the row outlives the release that wrote it.
    payload = _report().to_json_obj()
    payload["something_new"] = {"added": "later"}
    payload["design"]["also_new"] = 42
    assert CoverageReport.from_json_obj(payload).verdict


def test_a_missing_optional_section_defaults_rather_than_raising() -> None:
    payload = _report().to_json_obj()
    del payload["warnings"]
    del payload["per_response"]
    restored = CoverageReport.from_json_obj(payload)
    assert restored.warnings == ()
    assert restored.per_response == ()


def test_the_verdict_summary_is_the_indexed_column_value() -> None:
    # `coverage_verdict` is stored beside the JSON so a catalogue can filter
    # on it without parsing every report.
    original = _report()
    assert original.verdict in ("good", "thin", "poor", "insufficient")
    assert original.to_json_obj()["verdict"] == original.verdict


def test_the_stored_report_is_small_enough_to_live_on_the_row() -> None:
    # §9 chose a column over a table on the grounds that a report is a few kB.
    # A 12-factor design is the realistic worst case.
    rows = [{f"x{j}": float((i * (j + 3)) % 17) for j in range(12)} for i in range(200)]
    payload = _report(rows).to_json_obj()
    assert len(json.dumps(payload)) < 64_000


def test_settings_used_are_recorded_so_a_verdict_can_be_read_back_in_context() -> None:
    # Thresholds are config-tunable, so "poor" means nothing a year later
    # without knowing what the floor was when it was measured.
    points = resolve_points(training(two_factor_rows(4, 4)))
    envelope = build_envelope(points)
    design = analyse_design(
        points, envelope, settings=CoverageSettings(verdict_thresholds={"good": 0.9, "thin": 0.5})
    )
    payload = build_report(
        design, envelope=envelope, source_kind="cohort", computed_at=_NOW, points=points
    ).to_json_obj()
    assert payload["thresholds"] == {"good": 0.9, "thin": 0.5}
