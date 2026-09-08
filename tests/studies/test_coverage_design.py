"""The response-agnostic design pass and the report it produces.

ADR-0020 §2.1 / §7, slice 1. This is the path cohort preview runs — it makes
no claim about any particular output, which is exactly why it can answer
"is this fit to learn from?" before any response is chosen or any GP exists.
"""

from __future__ import annotations

import datetime as dt

import pytest

from matofx.config import CoverageSettings
from matofx.studies.coverage import (
    FactorOverride,
    analyse_design,
    build_envelope,
    build_report,
    resolve_points,
)
from tests.studies._coverage_fixtures import (
    descriptor,
    grid,
    integer,
    num,
    select,
    training,
)


def _design(rows, *, settings=None, overrides=None, **kwargs):
    resolve_kwargs = {k: kwargs.pop(k) for k in ("descriptor", "factor_spec") if k in kwargs}
    points = resolve_points(training(rows, **kwargs), **resolve_kwargs)
    env = build_envelope(points, **resolve_kwargs, overrides=overrides)
    return analyse_design(points, env, settings=settings or CoverageSettings())


# --- the criterion is always reported, never hidden ----------------------


def test_the_design_pass_is_always_model_free() -> None:
    design = _design(grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0]))
    assert design.criterion == "model_free"


def test_cohort_preview_runs_with_no_fitted_model_at_all() -> None:
    # The sequencing problem: a GP-only design would require fitting a model
    # to decide whether to fit a model.
    design = _design(grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0]))
    assert design.verdict in ("good", "thin", "poor")
    assert design.coverage_score is not None


# --- verdicts -------------------------------------------------------------


def test_a_full_factorial_grid_scores_well() -> None:
    design = _design(grid([1.0, 3.0, 5.0, 7.0, 9.0], [1.0, 3.0, 5.0, 7.0, 9.0]))
    assert design.verdict == "good"


def test_all_points_in_one_corner_scores_poor() -> None:
    rows = [
        {"thickness": 1.0 + i * 0.02, "heat_load": 1.0 + j * 0.02}
        for i in range(5)
        for j in range(5)
    ]
    rows.append({"thickness": 9.0, "heat_load": 9.0})  # sets the envelope
    design = _design(rows)
    assert design.verdict == "poor"


def test_the_score_is_bounded_and_higher_is_better() -> None:
    spread = _design(grid([1.0, 3.0, 5.0, 7.0, 9.0], [1.0, 3.0, 5.0, 7.0, 9.0]))
    corner_rows = [
        {"thickness": 1.0 + i * 0.02, "heat_load": 1.0 + j * 0.02}
        for i in range(5)
        for j in range(5)
    ] + [{"thickness": 9.0, "heat_load": 9.0}]
    clustered = _design(corner_rows)

    for design in (spread, clustered):
        assert 0.0 <= design.coverage_score <= 1.0
    assert spread.coverage_score > clustered.coverage_score


def test_verdict_thresholds_come_from_config() -> None:
    rows = grid([1.0, 3.0, 5.0, 7.0, 9.0], [1.0, 3.0, 5.0, 7.0, 9.0])
    assert (
        _design(
            rows, settings=CoverageSettings(verdict_thresholds={"good": 0.01, "thin": 0.005})
        ).verdict
        == "good"
    )
    assert (
        _design(
            rows, settings=CoverageSettings(verdict_thresholds={"good": 0.999, "thin": 0.998})
        ).verdict
        == "poor"
    )


def test_verdict_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValueError, match=r"(?i)thin.*good|order"):
        CoverageSettings(verdict_thresholds={"good": 0.3, "thin": 0.8})


def test_the_min_points_floor_comes_from_config() -> None:
    rows = grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0])  # 9 points
    assert _design(rows, settings=CoverageSettings(min_points_for_analysis=20)).verdict == (
        "insufficient"
    )
    assert _design(rows, settings=CoverageSettings(min_points_for_analysis=5)).verdict != (
        "insufficient"
    )


def test_analyse_design_uses_config_defaults_when_no_settings_are_passed() -> None:
    # Defaults live in matofx.config, never at a function signature
    # (feedback_no_hardcoded_defaults_outside_config).
    rows = grid([1.0, 3.0, 5.0, 7.0, 9.0], [1.0, 3.0, 5.0, 7.0, 9.0])
    points = resolve_points(training(rows))
    env = build_envelope(points)
    assert analyse_design(points, env) == analyse_design(points, env, settings=CoverageSettings())


# --- the per-factor strip -------------------------------------------------


def test_every_factor_gets_a_row_with_the_right_measure() -> None:
    rows = [
        {
            "thickness": float(i % 5 + 1),
            "ply_count": float(i % 4 + 1),
            "material": "cfrp" if i % 2 else "gfrp",
            "mesh": "fine",
        }
        for i in range(20)
    ]
    design = _design(
        rows,
        descriptor=descriptor(
            num("thickness", 0.0, 9.0),
            integer("ply_count", 1, 4),
            select("material", "cfrp", "gfrp"),
        ),
    )
    by_name = {f.name: f for f in design.per_factor}

    assert by_name["thickness"].measure == "covering_radius"
    assert by_name["ply_count"].measure == "lattice_occupancy"
    assert by_name["material"].measure == "level_balance"
    assert by_name["mesh"].measure == "pinned"


def test_an_integer_factor_names_its_missing_values_in_the_strip() -> None:
    rows = [{"ply_count": float(n), "thickness": float(n)} for n in (1, 3, 5)] * 4
    design = _design(
        rows, descriptor=descriptor(integer("ply_count", 1, 5), num("thickness", 0.0, 9.0))
    )
    ply = next(f for f in design.per_factor if f.name == "ply_count")
    assert ply.missing_values == ("2", "4")
    assert ply.actionable is True


def test_a_fully_occupied_integer_axis_reports_complete_coverage() -> None:
    rows = [{"ply_count": float(n), "thickness": float(n)} for n in (1, 2, 3, 4, 5)] * 4
    design = _design(
        rows, descriptor=descriptor(integer("ply_count", 1, 5), num("thickness", 0.0, 9.0))
    )
    ply = next(f for f in design.per_factor if f.name == "ply_count")
    assert ply.value == pytest.approx(1.0)
    assert ply.verdict == "good"
    assert ply.missing_values == ()


def test_a_large_lattice_falls_back_to_distance_metrics() -> None:
    # A mesh seed declared over [1,1000] is dense enough to behave
    # continuously. The envelope has to be *declared* that wide: bounds
    # default to the observed spread, and 40 observed values would be a
    # small lattice, not a large one.
    rows = [{"mesh_seed": float(n), "thickness": float(n % 9 + 1)} for n in range(1, 41)]
    design = _design(
        rows,
        settings=CoverageSettings(max_lattice_cardinality=100),
        overrides={"mesh_seed": FactorOverride(lo=1.0, hi=1000.0)},
        descriptor=descriptor(integer("mesh_seed", 1, 1000), num("thickness", 0.0, 9.0)),
    )
    seed_axis = next(f for f in design.per_factor if f.name == "mesh_seed")
    assert seed_axis.kind == "integer"
    assert seed_axis.measure == "covering_radius"


def test_the_lattice_crossover_is_asserted_at_the_boundary() -> None:
    rows = [{"mesh_seed": float(n), "thickness": float(n % 9 + 1)} for n in range(1, 11)]
    desc = descriptor(integer("mesh_seed", 1, 10), num("thickness", 0.0, 9.0))

    at = _design(rows, settings=CoverageSettings(max_lattice_cardinality=10), descriptor=desc)
    above = _design(rows, settings=CoverageSettings(max_lattice_cardinality=9), descriptor=desc)

    assert next(f for f in at.per_factor if f.name == "mesh_seed").measure == "lattice_occupancy"
    assert next(f for f in above.per_factor if f.name == "mesh_seed").measure == "covering_radius"


def test_the_per_factor_scale_is_carried_through_to_the_strip() -> None:
    rows = [{"conductivity": c, "thickness": 1.0 + c} for c in (1.0, 10.0, 100.0, 1000.0, 10000.0)]
    design = _design(rows, overrides={"conductivity": FactorOverride(scale="log")})
    axis = next(f for f in design.per_factor if f.name == "conductivity")
    assert axis.scale == "log"


# --- §3.2 the log-scale contrast, pinned by both readings ----------------


def test_a_log_sampled_factor_reads_evenly_covered_in_log_space() -> None:
    rows = [
        {"conductivity": c, "thickness": t}
        for c in (1.0, 10.0, 100.0, 1000.0, 10000.0)
        for t in (1.0, 3.0, 5.0, 7.0, 9.0)
    ]
    log_design = _design(rows, overrides={"conductivity": FactorOverride(scale="log")})
    linear_design = _design(rows, overrides={"conductivity": FactorOverride(scale="linear")})

    log_axis = next(f for f in log_design.per_factor if f.name == "conductivity")
    linear_axis = next(f for f in linear_design.per_factor if f.name == "conductivity")

    # Same points, opposite readings — the fix is pinned by the contrast.
    assert log_axis.verdict == "good"
    assert linear_axis.verdict in ("thin", "poor")
    assert log_design.coverage_score > linear_design.coverage_score


# --- §7 the report --------------------------------------------------------


def test_the_report_carries_the_envelope_and_an_empty_response_list_at_preview() -> None:
    rows = grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0])
    points = resolve_points(training(rows))
    env = build_envelope(points)
    design = analyse_design(points, env)

    report = build_report(
        design,
        envelope=env,
        source_kind="cohort",
        computed_at=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
    )
    assert report.per_response == ()
    assert report.envelope is env
    assert report.verdict == design.verdict
    assert report.computed_at.tzinfo is not None


def test_the_report_verdict_is_the_worst_of_design_and_responses() -> None:
    # With no responses yet the design verdict stands alone; the "worst of"
    # rule is asserted here so slice 2 cannot quietly change it.
    rows = grid([1.0, 3.0, 5.0, 7.0, 9.0], [1.0, 3.0, 5.0, 7.0, 9.0])
    points = resolve_points(training(rows))
    env = build_envelope(points)
    design = analyse_design(points, env)

    report = build_report(
        design,
        envelope=env,
        source_kind="cohort",
        computed_at=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
    )
    assert report.verdict == design.verdict == "good"


@pytest.mark.parametrize("source_kind", ["parametric", "optimisation", "cohort"])
def test_the_report_accepts_the_real_source_kind_vocabulary(source_kind: str) -> None:
    # StudyResult.source_kind is Literal["parametric", "optimisation", "cohort"].
    rows = grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0])
    points = resolve_points(training(rows, source_kind=source_kind))
    env = build_envelope(points)
    report = build_report(
        analyse_design(points, env),
        envelope=env,
        source_kind=source_kind,
        computed_at=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
    )
    assert report.source_kind == source_kind


# --- §7.1 the verdict is framed by where the data came from --------------


def test_an_optimisation_sourced_cohort_carries_the_source_framing_message() -> None:
    # An optimisation study concentrates runs near the optimum — the
    # algorithm working, not a design flaw. A light that is always red is
    # one users learn to ignore.
    rows = [
        {"thickness": 4.9 + i * 0.01, "heat_load": 4.9 + j * 0.01}
        for i in range(5)
        for j in range(5)
    ] + [{"thickness": 1.0, "heat_load": 1.0}, {"thickness": 9.0, "heat_load": 9.0}]
    points = resolve_points(training(rows, source_kind="optimisation"))
    env = build_envelope(points)
    report = build_report(
        analyse_design(points, env),
        envelope=env,
        source_kind="optimisation",
        computed_at=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
    )

    assert report.verdict == "poor"
    framing = next(w for w in report.warnings if w.code == "source_framing")
    assert "optimisation study" in framing.message.lower()
    assert "expected" in framing.message.lower()


def test_a_cohort_sourced_report_carries_no_source_framing_message() -> None:
    rows = grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0])
    points = resolve_points(training(rows, source_kind="cohort"))
    env = build_envelope(points)
    report = build_report(
        analyse_design(points, env),
        envelope=env,
        source_kind="cohort",
        computed_at=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
    )
    assert not any(w.code == "source_framing" for w in report.warnings)


# --- §11 non-actionable axes get the remedy that actually applies --------


def test_a_thin_derived_axis_is_remedied_by_run_length_not_a_design_point() -> None:
    from tests.studies._coverage_fixtures import field_training  # noqa: PLC0415

    ftd = field_training(
        [{"thickness": t, "time": float(s)} for t in (1.0, 3.0, 5.0, 7.0, 9.0) for s in range(2)]
    )
    points = resolve_points(ftd)
    env = build_envelope(points, overrides={"time": FactorOverride(lo=0.0, hi=100.0)})
    design = analyse_design(points, env)
    report = build_report(
        design,
        envelope=env,
        source_kind="cohort",
        computed_at=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
    )

    warning = next(w for w in report.warnings if w.code == "derived_axis_thin")
    assert warning.factor == "time"
    assert "snapshot" in warning.message.lower() or "longer run" in warning.message.lower()
    # Never a proposed design point — naming the wrong remedy would be
    # worse than staying silent.
    assert "run a job at" not in warning.message.lower()


def test_a_pinned_factor_warning_asks_for_varying_runs_not_a_new_cohort() -> None:
    """It names the finding and the remedy in kind — never a place to go.

    The message used to end "needs a new cohort that varies it", which
    promotable choices (§11.5) made false: the *Fill the gaps* card on this
    same panel will sweep a held-constant input into *this* cohort. Naming the
    tick instead would be wrong too — a pinned input is promotable only where
    the descriptor declares a domain — so the warning states the finding and
    the card makes the offer.
    """
    rows = [{"thickness": float(i), "mesh_density": "fine"} for i in range(1, 21)]
    points = resolve_points(training(rows))
    env = build_envelope(points)
    report = build_report(
        analyse_design(points, env),
        envelope=env,
        source_kind="cohort",
        computed_at=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
    )
    warning = next(w for w in report.warnings if w.code == "pinned_factor")
    assert warning.factor == "mesh_density"
    assert "runs that vary it" in warning.message.lower()
    # The retired claim, asserted as absent so it cannot come back.
    assert "new cohort" not in warning.message.lower()
    # Never a proposed design point, for the same reason as the derived-axis
    # warning above.
    assert "run a job at" not in warning.message.lower()


# --- the verdict never blocks a fit --------------------------------------


def test_a_poor_verdict_is_recorded_not_raised() -> None:
    rows = [
        {"thickness": 1.0 + i * 0.02, "heat_load": 1.0 + j * 0.02}
        for i in range(5)
        for j in range(5)
    ] + [{"thickness": 9.0, "heat_load": 9.0}]
    design = _design(rows)
    assert design.verdict == "poor"  # recorded, and nothing raised


def test_the_report_is_hashable_and_frozen() -> None:
    # It is persisted and versioned, not a transient panel (§10).
    rows = grid([1.0, 5.0, 9.0], [1.0, 5.0, 9.0])
    points = resolve_points(training(rows))
    env = build_envelope(points)
    design = analyse_design(points, env)
    with pytest.raises((AttributeError, TypeError)):
        design.verdict = "good"  # type: ignore[misc]


# --- the shape of a *real* model's warnings (found on a 250-run fit) -----


def test_many_pinned_inputs_produce_one_warning_not_one_each() -> None:
    """A real fit routinely holds a dozen inputs constant.

    One near-identical sentence per held-constant input turns the verdict card
    into a wall of text differing only in a name — invisible on a two-factor
    fixture, obvious on a real 250-run bracket fit that pins twelve. The
    per-factor strip is where each one is already listed individually.
    """
    rows = [
        {"thickness": float(i), "heat_load": float(i * 2), **{f"c{j}": float(j) for j in range(12)}}
        for i in range(1, 21)
    ]
    design = _design(rows)

    pinned = [w for w in design.warnings if w.code == "pinned_factor"]
    assert len(pinned) == 1
    # ...and it still names them, so nothing is hidden.
    for j in range(12):
        assert f"c{j}" in pinned[0].message
    # The strip keeps the per-input detail.
    assert sum(1 for f in design.per_factor if f.measure == "pinned") == 12


def test_a_single_pinned_input_still_reads_naturally() -> None:
    rows = [
        {"thickness": float(i), "heat_load": float(i * 2), "mesh": "fine"} for i in range(1, 21)
    ]
    (pinned,) = [w for w in _design(rows).warnings if w.code == "pinned_factor"]
    assert pinned.factor == "mesh"
    assert "'mesh'" in pinned.message


def test_the_report_does_not_repeat_the_design_pass_warnings() -> None:
    # `build_report` copies design.warnings and appends the source framing, so
    # anything concatenating both lists renders every warning twice.
    rows = [
        {"thickness": float(i), "heat_load": float(i * 2), "mesh": "fine"} for i in range(1, 21)
    ]
    points = resolve_points(training(rows))
    envelope = build_envelope(points)
    design = analyse_design(points, envelope)
    report = build_report(
        design,
        envelope=envelope,
        source_kind="cohort",
        computed_at=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
    )

    codes = [w.code for w in report.warnings]
    assert len(codes) == len(set(codes)), codes
    assert {w.code for w in design.warnings} <= set(codes)
