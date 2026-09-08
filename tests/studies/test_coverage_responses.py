"""Per-response coverage (ADR-0020 §2.2, §2.3, slice 2).

`SurrogateModelVersion.responses` is a *list* and `fit_surrogate` fits one
response at a time, so a version carries N GPs with N sets of ARD length
scales. Over an **identical point set** those GPs give **different verdicts**
— there is no single "GP coverage" for a design, and the report headlines the
worst because a model is only as trustworthy as its least-covered response.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from matofx.config import CoverageSettings
from matofx.studies.coverage import (
    ResponseModel,
    analyse_design,
    analyse_responses,
    build_envelope,
    build_report,
    resolve_points,
)
from tests.studies._coverage_fixtures import fit, fit_field, training, two_factor_rows

_NOW = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)


def _setup(rows):
    points = resolve_points(training(rows))
    return points, build_envelope(points)


# --- §2.2 coverage is response-dependent ---------------------------------


def test_two_responses_over_one_point_set_can_reach_different_verdicts() -> None:
    # Mass may depend almost entirely on thickness while peak temperature
    # depends on everything. Same points, different ARD, different answers.
    rows = [
        {"thickness": float(t), "heat_load": float(h)}
        for t in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0)
        for h in (1.0, 1.5)  # heat_load barely explored
    ]
    mass = ResponseModel.scalar("mass", fit(rows, [r["thickness"] * 2.0 for r in rows]))
    peak = ResponseModel.scalar(
        "peak_temperature",
        fit(rows, [r["thickness"] * 2.0 + r["heat_load"] ** 3 for r in rows]),
    )
    points, envelope = _setup(rows)

    results = analyse_responses({"mass": mass, "peak_temperature": peak}, envelope, points=points)
    by_name = {r.response_name: r for r in results}
    assert by_name["mass"].coverage_score != by_name["peak_temperature"].coverage_score


def test_the_report_headline_is_the_worst_response_not_an_average() -> None:
    rows = two_factor_rows(5, 5)
    good = ResponseModel.scalar("mass", fit(rows, [r["thickness"] * 2.0 for r in rows]))
    points, envelope = _setup(rows)
    design = analyse_design(points, envelope)

    results = analyse_responses({"mass": good}, envelope, points=points)
    # Force one response to a poor verdict via the thresholds.
    poor = analyse_responses(
        {"mass": good},
        envelope,
        points=points,
        settings=CoverageSettings(verdict_thresholds={"good": 0.999, "thin": 0.998}),
    )
    report = build_report(
        design,
        envelope=envelope,
        source_kind="cohort",
        computed_at=_NOW,
        per_response=(*results, *poor),
    )
    assert report.verdict == "poor"


def test_every_response_is_kept_not_just_the_worst() -> None:
    rows = two_factor_rows(4, 4)
    models = {
        "mass": ResponseModel.scalar("mass", fit(rows, [r["thickness"] for r in rows])),
        "peak": ResponseModel.scalar("peak", fit(rows, [r["heat_load"] for r in rows])),
    }
    points, envelope = _setup(rows)
    report = build_report(
        analyse_design(points, envelope),
        envelope=envelope,
        source_kind="cohort",
        computed_at=_NOW,
        per_response=analyse_responses(models, envelope, points=points),
    )
    assert {r.response_name for r in report.per_response} == {"mass", "peak"}


def test_results_come_back_in_a_stable_order() -> None:
    rows = two_factor_rows(4, 4)
    models = {
        "zeta": ResponseModel.scalar("zeta", fit(rows, [r["thickness"] for r in rows])),
        "alpha": ResponseModel.scalar("alpha", fit(rows, [r["heat_load"] for r in rows])),
    }
    points, envelope = _setup(rows)
    names = [r.response_name for r in analyse_responses(models, envelope, points=points)]
    assert names == ["alpha", "zeta"]


def test_suggestions_default_to_the_worst_covered_response() -> None:
    rows = two_factor_rows(4, 4)
    models = {
        "mass": ResponseModel.scalar("mass", fit(rows, [r["thickness"] for r in rows])),
        "peak": ResponseModel.scalar("peak", fit(rows, [r["heat_load"] for r in rows])),
    }
    points, envelope = _setup(rows)
    results = analyse_responses(models, envelope, points=points)
    worst = min(results, key=lambda r: r.coverage_score)
    assert worst.suggested_points  # the worst one carries the proposal


# --- §2.3 method-driven criterion selection ------------------------------


@pytest.mark.parametrize("kind", ["poly", "gbm"])
def test_a_method_with_no_posterior_variance_selects_model_free(kind: str) -> None:
    # Only the two GP kinds carry a posterior variance, which is why the
    # exporter's +/- gate is already GP-only. MOP's auto-best-metamodel
    # picks by Q2 without regard to whether the winner carries uncertainty.
    rows = two_factor_rows(5, 5)
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] for r in rows], kind=kind))
    points, envelope = _setup(rows)

    (result,) = analyse_responses({"y": model}, envelope, points=points)
    assert result.surrogate_method == kind
    assert result.criterion == "model_free"
    assert result.verdict in ("good", "thin", "poor", "insufficient")
    assert result.suggested_points  # loses ARD weighting, not the feature


@pytest.mark.parametrize("kind", ["poly", "gbm"])
def test_a_non_gp_response_reports_no_edge_threshold(kind: str) -> None:
    # `edge` is not computable without a posterior variance. The response
    # says so rather than silently never returning `edge`.
    rows = two_factor_rows(5, 5)
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] for r in rows], kind=kind))
    points, envelope = _setup(rows)

    (result,) = analyse_responses({"y": model}, envelope, points=points)
    assert result.edge_sigma_threshold is None
    assert result.available_statuses == ("inside", "outside")


@pytest.mark.parametrize("kind", ["gp", "gp_matern"])
def test_a_gp_response_carries_a_stored_edge_threshold(kind: str) -> None:
    rows = two_factor_rows(5, 5)
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] for r in rows], kind=kind))
    points, envelope = _setup(rows)

    (result,) = analyse_responses({"y": model}, envelope, points=points)
    assert result.criterion == "gp_variance"
    assert result.edge_sigma_threshold is not None
    assert result.available_statuses == ("inside", "edge", "outside")


def test_the_edge_threshold_is_a_percentile_of_sigma_at_the_training_points() -> None:
    # That reference set *is* the definition of "the trained region"; a
    # percentile over an envelope-wide sample would move with the envelope.
    from matofx.studies.coverage.gp import relative_sigma  # noqa: PLC0415

    rows = two_factor_rows(5, 5)
    surrogate = fit(rows, [r["thickness"] * 2.0 + r["heat_load"] for r in rows])
    points, envelope = _setup(rows)
    settings = CoverageSettings(edge_percentile=90.0)

    (result,) = analyse_responses(
        {"y": ResponseModel.scalar("y", surrogate)}, envelope, points=points, settings=settings
    )
    at_training = relative_sigma(surrogate, list(points.rows))
    assert at_training is not None
    assert result.edge_sigma_threshold == pytest.approx(
        float(np.percentile(np.asarray(at_training), 90.0))
    )


def test_the_edge_percentile_comes_from_config() -> None:
    rows = two_factor_rows(5, 5)
    model = ResponseModel.scalar(
        "y", fit(rows, [r["thickness"] * 2.0 + r["heat_load"] for r in rows])
    )
    points, envelope = _setup(rows)

    low = analyse_responses(
        {"y": model}, envelope, points=points, settings=CoverageSettings(edge_percentile=60.0)
    )[0]
    high = analyse_responses(
        {"y": model}, envelope, points=points, settings=CoverageSettings(edge_percentile=99.0)
    )[0]
    assert low.edge_sigma_threshold is not None
    assert high.edge_sigma_threshold is not None
    assert low.edge_sigma_threshold < high.edge_sigma_threshold


# --- §2.2.1 a response is not always one GP ------------------------------


def _field_setup(kind: str = "gp"):
    """A 4x4 design with a two-pattern nodal field over 12 nodes."""
    rows = two_factor_rows(4, 4)
    base = np.linspace(0.0, 1.0, 12)
    # Two patterns of *comparable* magnitude, so POD genuinely retains two
    # modes — a second mode carrying 0.1 % of the energy would be truncated
    # and the aggregate would have nothing to weight.
    snapshots = [
        (r["thickness"] * np.sin(base * np.pi) + r["heat_load"] * np.cos(base * 2 * np.pi))
        for r in rows
    ]
    return rows, fit_field(rows, snapshots, kind=kind)


def test_a_field_response_aggregates_sigma_by_mode_energy_weight() -> None:
    # A mode carrying 1% of the field's energy must not drive the verdict
    # as hard as the dominant one.
    from matofx.studies.coverage.gp import relative_sigma  # noqa: PLC0415

    _rows, field_surrogate = _field_setup()
    assert field_surrogate.n_modes >= 2

    model = ResponseModel.field("temperature", field_surrogate)
    probe = [{"thickness": 5.0, "heat_load": 5.0}]

    aggregate = relative_sigma(model, probe)
    per_mode = [relative_sigma(s, probe)[0] for s in field_surrogate.per_mode_surrogates]
    weights = np.asarray(field_surrogate.mode_energy_spectrum[: len(per_mode)])
    weights = weights / weights.sum()
    expected = float(np.dot(weights, np.asarray(per_mode)))

    assert aggregate is not None
    assert aggregate[0] == pytest.approx(expected)


def test_the_energy_weighted_aggregate_is_not_dominated_by_mode_magnitude() -> None:
    # Mode 1's coefficients are the largest, so a *raw* sigma aggregate
    # would weight that mode's magnitude twice — once through its own
    # scale and again through the energy weight. Relative sigma removes
    # the first, leaving energy to do the weighting alone.
    from matofx.studies.coverage.gp import relative_sigma  # noqa: PLC0415

    _, field_surrogate = _field_setup()
    probe = [{"thickness": 5.0, "heat_load": 5.0}]
    raw = [s.predict(probe)[1][0] for s in field_surrogate.per_mode_surrogates]
    rel = [relative_sigma(s, probe)[0] for s in field_surrogate.per_mode_surrogates]

    # The raw spread across modes tracks coefficient magnitude; the
    # relative one does not.
    assert max(raw) / max(min(raw), 1e-12) > max(rel) / max(min(rel), 1e-12)


def test_a_field_response_is_in_scope_and_reports_as_a_field() -> None:
    rows, field_surrogate = _field_setup()
    points, envelope = _setup(rows)

    (result,) = analyse_responses(
        {"temperature": ResponseModel.field("temperature", field_surrogate)},
        envelope,
        points=points,
    )
    assert result.response_kind == "field"
    assert result.criterion == "gp_variance"
    assert result.verdict in ("good", "thin", "poor", "insufficient")


def test_a_field_response_with_any_non_gp_mode_falls_back_to_model_free() -> None:
    # Aggregating over a partial set would misreport.
    rows, field_surrogate = _field_setup(kind="poly")
    points, envelope = _setup(rows)

    (result,) = analyse_responses(
        {"temperature": ResponseModel.field("temperature", field_surrogate)},
        envelope,
        points=points,
    )
    assert result.criterion == "model_free"
    assert result.edge_sigma_threshold is None


def test_a_field_and_a_scalar_response_can_be_analysed_together() -> None:
    # The NAFEMS demo's shape: a scalar and two field surrogates on one
    # version.
    rows, field_surrogate = _field_setup()
    models = {
        "temperature": ResponseModel.field("temperature", field_surrogate),
        "mass": ResponseModel.scalar("mass", fit(rows, [r["thickness"] * 2.0 for r in rows])),
    }
    points, envelope = _setup(rows)
    results = analyse_responses(models, envelope, points=points)

    kinds = {r.response_name: r.response_kind for r in results}
    assert kinds == {"temperature": "field", "mass": "scalar"}


# --- shape and provenance -------------------------------------------------


def test_every_response_records_the_criterion_that_produced_its_verdict() -> None:
    rows = two_factor_rows(5, 5)
    models = {
        "gp_one": ResponseModel.scalar("gp_one", fit(rows, [r["thickness"] for r in rows])),
        "poly_one": ResponseModel.scalar(
            "poly_one", fit(rows, [r["heat_load"] for r in rows], kind="poly")
        ),
    }
    points, envelope = _setup(rows)
    by_name = {r.response_name: r for r in analyse_responses(models, envelope, points=points)}

    assert by_name["gp_one"].criterion == "gp_variance"
    assert by_name["poly_one"].criterion == "model_free"


def test_no_responses_yields_an_empty_tuple_not_an_error() -> None:
    rows = two_factor_rows(4, 4)
    points, envelope = _setup(rows)
    assert analyse_responses({}, envelope, points=points) == ()


def test_a_response_coverage_is_frozen() -> None:
    rows = two_factor_rows(4, 4)
    points, envelope = _setup(rows)
    (result,) = analyse_responses(
        {"y": ResponseModel.scalar("y", fit(rows, [r["thickness"] for r in rows]))},
        envelope,
        points=points,
    )
    with pytest.raises((AttributeError, TypeError)):
        result.verdict = "good"  # type: ignore[misc]


def test_cohort_preview_still_needs_no_model_at_all() -> None:
    # Slice 2 must not make the response-agnostic path depend on a fit.
    rows = two_factor_rows(5, 5)
    points, envelope = _setup(rows)
    design = analyse_design(points, envelope)
    report = build_report(design, envelope=envelope, source_kind="cohort", computed_at=_NOW)

    assert report.per_response == ()
    assert report.verdict == design.verdict


# --- what a real 250-run GP fit exposed ----------------------------------


def test_a_well_covered_gp_response_does_not_read_poor() -> None:
    """The per-response score must track the design, not the method.

    An earlier version scored ``threshold / worst_sigma`` — sigma *at* the
    training data over sigma at the emptiest corner — which for any GP differ
    by orders of magnitude, so **every** GP response read ``poor`` however
    good the design. Finding 44's always-red light, reappearing in the
    per-response pass, and invisible until a real fit was scored.
    """
    rows = two_factor_rows(6, 6)
    model = ResponseModel.scalar(
        "y", fit(rows, [r["thickness"] * 2.0 + r["heat_load"] for r in rows])
    )
    points, envelope = _setup(rows)

    (result,) = analyse_responses({"y": model}, envelope, points=points)
    assert result.criterion == "gp_variance"
    assert result.verdict == "good", result.coverage_score


def test_a_clustered_gp_response_still_reads_badly() -> None:
    # The other half of the guarantee: making the score meaningful must not
    # make it uncritical.
    rows = [
        {"thickness": 1.0 + i * 0.05, "heat_load": 1.0 + j * 0.05}
        for i in range(5)
        for j in range(5)
    ] + [{"thickness": 9.0, "heat_load": 9.0}]
    model = ResponseModel.scalar(
        "y", fit(rows, [r["thickness"] * 2.0 + r["heat_load"] for r in rows])
    )
    points, envelope = _setup(rows)

    (result,) = analyse_responses({"y": model}, envelope, points=points)
    assert result.verdict in ("thin", "poor"), result.coverage_score


def test_the_prior_sigma_is_measured_not_assumed() -> None:
    # It is the kernel amplitude relative to the response's spread — ~33 on a
    # real bracket GP, not 1.0 — so it has to be measured per fit.
    from matofx.studies.coverage.gp import prior_sigma, relative_sigma  # noqa: PLC0415

    rows = two_factor_rows(5, 5)
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] * 2.0 for r in rows]))
    _points, envelope = _setup(rows)

    prior = prior_sigma(model, envelope)
    at_data = relative_sigma(model, rows)
    assert prior is not None
    assert at_data is not None
    assert prior > max(at_data) * 100  # the two ends really are far apart


def test_prior_sigma_is_none_without_a_posterior_variance() -> None:
    from matofx.studies.coverage.gp import prior_sigma  # noqa: PLC0415

    rows = two_factor_rows(5, 5)
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] for r in rows], kind="gbm"))
    _points, envelope = _setup(rows)
    assert prior_sigma(model, envelope) is None


def test_almost_no_training_point_reads_edge() -> None:
    """Predicting at a run you already did must not say "less certain here".

    The threshold is a percentile *of the training distribution*, so a value
    of ``p`` puts ``100 - p`` per cent of the training runs above it by
    construction — and a GP interpolates its own data, so those sigmas are
    nearly identical and the choice is arbitrary. At the old default of 90
    that meant one run in ten.
    """
    from matofx.studies.coverage import evaluate_support  # noqa: PLC0415
    from matofx.studies.coverage.gp import relative_sigma  # noqa: PLC0415

    rows = two_factor_rows(7, 7)  # 49 runs
    surrogate = fit(rows, [r["thickness"] * 2.0 + r["heat_load"] for r in rows])
    points, envelope = _setup(rows)
    (result,) = analyse_responses(
        {"y": ResponseModel.scalar("y", surrogate)}, envelope, points=points
    )

    sigmas = relative_sigma(surrogate, rows) or ()
    edged = sum(
        1
        for row, sigma in zip(rows, sigmas, strict=True)
        if evaluate_support(
            envelope, row, sigma=sigma, edge_threshold=result.edge_sigma_threshold
        ).status
        == "edge"
    )
    assert edged <= 2, f"{edged} of {len(rows)} training runs read edge"
