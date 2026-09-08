"""GP-variance coverage (ADR-0020 §5, slice 2).

The load-bearing claim of the whole design is that GP posterior variance
*is* a coverage measure — distance-to-data warped by the kernel, with ARD
length scales telling it which axes deserve budget. These tests pin what
that claim actually means against **this** codebase's GP, which fits with
``normalize_y=True`` and optimises its hyperparameters.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from matofx.config import CoverageSettings
from matofx.studies.coverage import (
    ResponseModel,
    build_envelope,
    resolve_points,
)
from matofx.studies.coverage.gp import relative_sigma, variance_field
from tests.studies._coverage_fixtures import fit, training, two_factor_rows

# --- the y-free property, stated precisely -------------------------------


def test_relative_sigma_is_y_free_under_a_pure_rescaling_of_the_response() -> None:
    # ADR-0020 §5: "sigma contains no y term ... it depends only on *where*
    # the design was sampled". True of the *relative* field. Raw sigma
    # carries the response's own scale, because the GP fits with
    # normalize_y=True and multiplies its std back up by y's spread.
    rows = two_factor_rows(4, 4)
    y = [r["thickness"] * 2.0 + r["heat_load"] for r in rows]

    plain = fit(rows, y)
    scaled = fit(rows, [3.0 * v for v in y])
    probes = [{"thickness": 5.0, "heat_load": 5.0}, {"thickness": 20.0, "heat_load": 20.0}]

    # rel= rather than exact: the two fits optimise their hyperparameters
    # independently and land a few ULPs apart. The point is that the field
    # does not *scale* with the response, not that it is bit-identical.
    assert relative_sigma(plain, probes) == pytest.approx(relative_sigma(scaled, probes), rel=1e-4)

    # ...and the contrast that makes the fix necessary: raw sigma does not
    # survive the rescaling, so a threshold applied to it would move with
    # the units of the response.
    raw_plain = plain.predict(probes)[1]
    raw_scaled = scaled.predict(probes)[1]
    assert raw_plain is not None
    assert raw_scaled is not None
    assert raw_scaled[0] == pytest.approx(3.0 * raw_plain[0], rel=1e-6)


def test_relative_sigma_is_comparable_across_responses_in_different_units() -> None:
    # Mass in kg and peak temperature in K must be judged on one scale, or
    # the verdict is decided by whoever picked the units.
    rows = two_factor_rows(4, 4)
    kilograms = fit(rows, [0.001 * r["thickness"] for r in rows])
    kelvin = fit(rows, [800.0 * r["thickness"] for r in rows])
    probes = [{"thickness": 5.0, "heat_load": 5.0}]

    assert relative_sigma(kilograms, probes) == pytest.approx(
        relative_sigma(kelvin, probes), rel=1e-3
    )


def test_sigma_grows_with_distance_from_the_training_data() -> None:
    rows = two_factor_rows(4, 4)  # thickness / heat_load over [1, 9]
    model = fit(rows, [r["thickness"] * 2.0 + r["heat_load"] for r in rows])

    near = relative_sigma(model, [{"thickness": 5.0, "heat_load": 5.0}])
    far = relative_sigma(model, [{"thickness": 60.0, "heat_load": 60.0}])
    assert near is not None
    assert far is not None
    assert far[0] > near[0]


def test_relative_sigma_is_none_for_a_method_with_no_posterior_variance() -> None:
    rows = two_factor_rows(4, 4)
    y = [r["thickness"] * 2.0 + r["heat_load"] for r in rows]
    for kind in ("poly", "gbm"):
        model = fit(rows, y, kind=kind)
        assert relative_sigma(model, [{"thickness": 5.0, "heat_load": 5.0}]) is None


def test_ard_spends_attention_on_the_axis_the_response_responds_to() -> None:
    # The GP's advantage over a plain geometric criterion: it has learned
    # which factors matter, so an unused axis costs less coverage.
    rows = two_factor_rows(6, 6)
    only_thickness = fit(rows, [r["thickness"] ** 2 for r in rows])

    model = ResponseModel.scalar("peak_temperature", only_thickness)
    relevance = model.ard_relevance
    assert relevance is not None
    assert relevance["thickness"] > relevance["heat_load"]


# --- the variance field ---------------------------------------------------


def _envelope_for(rows):
    points = resolve_points(training(rows))
    return points, build_envelope(points)


def test_variance_field_evaluates_over_envelope_candidates() -> None:
    rows = two_factor_rows(4, 4)
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] for r in rows]))
    _, envelope = _envelope_for(rows)

    field = variance_field(model, envelope, n_candidates=256, seed=3)
    assert len(field.sigma) == 256
    assert len(field.points) == 256
    assert all(math.isfinite(s) for s in field.sigma)


def test_variance_field_is_deterministic_for_a_given_seed() -> None:
    rows = two_factor_rows(4, 4)
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] for r in rows]))
    _, envelope = _envelope_for(rows)

    a = variance_field(model, envelope, n_candidates=128, seed=11)
    b = variance_field(model, envelope, n_candidates=128, seed=11)
    assert a.sigma == b.sigma


def test_variance_field_candidate_points_are_valid_inputs() -> None:
    rows = two_factor_rows(4, 4)
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] for r in rows]))
    _, envelope = _envelope_for(rows)

    for point in variance_field(model, envelope, n_candidates=64, seed=5).points:
        assert envelope.contains(point)


def test_a_gap_in_the_design_shows_as_a_peak_in_the_variance_field() -> None:
    # Two clusters at opposite corners, nothing in the middle.
    rows = [
        {"thickness": 1.0 + i * 0.1, "heat_load": 1.0 + j * 0.1} for i in range(3) for j in range(3)
    ] + [
        {"thickness": 9.0 - i * 0.1, "heat_load": 9.0 - j * 0.1} for i in range(3) for j in range(3)
    ]
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] + r["heat_load"] for r in rows]))
    middle = relative_sigma(model.surrogate, [{"thickness": 5.0, "heat_load": 5.0}])
    corner = relative_sigma(model.surrogate, [{"thickness": 1.1, "heat_load": 1.1}])
    assert middle is not None
    assert corner is not None
    assert middle[0] > corner[0]


# --- conditioning, the mechanism greedy selection needs ------------------


def test_conditioning_on_a_pseudo_observation_collapses_sigma_there() -> None:
    rows = two_factor_rows(4, 4)
    model = fit(rows, [r["thickness"] * 2.0 + r["heat_load"] for r in rows])
    hole = [{"thickness": 30.0, "heat_load": 30.0}]

    before = relative_sigma(model, hole)
    after = relative_sigma(model.conditioned_on(hole), hole)
    assert before is not None
    assert after is not None
    assert after[0] < before[0] / 2.0


def test_conditioning_is_y_free_so_a_whole_batch_costs_zero_solver_calls() -> None:
    # The property the greedy planner rests on: what the pseudo-run would
    # have returned cannot change where the next one should go.
    rows = two_factor_rows(4, 4)
    model = fit(rows, [r["thickness"] * 2.0 + r["heat_load"] for r in rows])
    hole = [{"thickness": 30.0, "heat_load": 30.0}]
    elsewhere = [{"thickness": 2.0, "heat_load": 8.0}]

    modest = model.conditioned_on(hole, pseudo_response=[0.0])
    absurd = model.conditioned_on(hole, pseudo_response=[1e6])
    assert relative_sigma(modest, elsewhere) == pytest.approx(relative_sigma(absurd, elsewhere))


def test_conditioning_preserves_the_fitted_hyperparameters() -> None:
    # Re-optimising would be steering by a compass recalibrated mid-turn.
    rows = two_factor_rows(4, 4)
    model = fit(rows, [r["thickness"] * 2.0 + r["heat_load"] for r in rows])
    conditioned = model.conditioned_on([{"thickness": 30.0, "heat_load": 30.0}])
    assert conditioned.ard_length_scales == pytest.approx(model.ard_length_scales)


def test_conditioning_a_non_gp_surrogate_is_refused_clearly() -> None:
    from matofx.studies.coverage import CoverageError  # noqa: PLC0415

    rows = two_factor_rows(4, 4)
    model = fit(rows, [r["thickness"] for r in rows], kind="gbm")
    with pytest.raises((CoverageError, ValueError)):
        model.conditioned_on([{"thickness": 5.0, "heat_load": 5.0}])


# --- the cold-start ratio -------------------------------------------------


def test_below_the_cold_start_ratio_the_model_free_criterion_is_used() -> None:
    # The customer with forty scattered ad-hoc jobs is the modal case; the
    # feature has to work exactly when the ARD length scales are least
    # trustworthy, and say which criterion it used.
    from matofx.studies.coverage import analyse_responses  # noqa: PLC0415

    rows = two_factor_rows(3, 2)  # 6 points, 2 factors
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] for r in rows]))
    points, envelope = _envelope_for(rows)

    cold = analyse_responses(
        {"y": model}, envelope, points=points, settings=CoverageSettings(cold_start_ratio=10.0)
    )
    warm = analyse_responses(
        {"y": model}, envelope, points=points, settings=CoverageSettings(cold_start_ratio=1.5)
    )
    assert cold[0].criterion == "model_free"
    assert warm[0].criterion == "gp_variance"


def test_the_cold_start_fallback_still_returns_a_full_verdict() -> None:
    from matofx.studies.coverage import analyse_responses  # noqa: PLC0415

    rows = two_factor_rows(3, 2)
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] for r in rows]))
    points, envelope = _envelope_for(rows)

    (result,) = analyse_responses(
        {"y": model}, envelope, points=points, settings=CoverageSettings(cold_start_ratio=10.0)
    )
    assert result.criterion == "model_free"
    assert result.verdict in ("good", "thin", "poor", "insufficient")
    assert result.per_factor  # it loses ARD weighting, not the feature


def test_the_ratio_counts_active_dimensions_not_raw_points() -> None:
    from matofx.studies.coverage import analyse_responses  # noqa: PLC0415

    settings = CoverageSettings(cold_start_ratio=4.0)
    # 16 points over 2 factors = 8 per dimension: warm.
    wide = two_factor_rows(4, 4)
    wide_model = ResponseModel.scalar("y", fit(wide, [r["thickness"] for r in wide]))
    wide_points, wide_env = _envelope_for(wide)
    assert (
        analyse_responses({"y": wide_model}, wide_env, points=wide_points, settings=settings)[
            0
        ].criterion
        == "gp_variance"
    )

    # 6 points over 2 factors = 3 per dimension: cold.
    narrow = two_factor_rows(3, 2)
    narrow_model = ResponseModel.scalar("y", fit(narrow, [r["thickness"] for r in narrow]))
    narrow_points, narrow_env = _envelope_for(narrow)
    assert (
        analyse_responses({"y": narrow_model}, narrow_env, points=narrow_points, settings=settings)[
            0
        ].criterion
        == "model_free"
    )


def test_an_unfitted_surrogate_falls_back_rather_than_raising() -> None:
    from matofx.studies.coverage import analyse_responses  # noqa: PLC0415

    rows = two_factor_rows(2, 2)  # 4 points — below fit_surrogate's floor
    unfitted = fit(rows, [r["thickness"] for r in rows])
    assert not unfitted.fitted

    points, envelope = _envelope_for(rows)
    (result,) = analyse_responses(
        {"y": ResponseModel.scalar("y", unfitted)}, envelope, points=points
    )
    assert result.criterion == "model_free"


def test_variance_field_of_an_unfitted_model_is_empty_not_an_exception() -> None:
    rows = two_factor_rows(2, 2)
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] for r in rows]))
    _, envelope = _envelope_for(rows)
    assert variance_field(model, envelope, n_candidates=32, seed=1).sigma == ()


def test_sigma_is_evaluated_with_derived_axes_held_at_their_training_mean() -> None:
    # A design candidate has no snapshot index (§1.2.1), so the model is
    # asked at the middle of its trained span rather than at zero, which
    # would sit at the edge of it.
    rows = [
        {"thickness": float(t), "time": float(s)} for t in (1.0, 3.0, 5.0, 7.0) for s in range(4)
    ]
    model = ResponseModel.scalar("y", fit(rows, [r["thickness"] + r["time"] for r in rows]))
    assert "time" in model.surrogate.factors

    mean_time = float(np.mean(model.surrogate.training_inputs["time"]))
    at_mean = relative_sigma(model.surrogate, [{"thickness": 4.0, "time": mean_time}])
    implied = relative_sigma(model.surrogate, [{"thickness": 4.0}])
    assert implied == pytest.approx(at_mean)
