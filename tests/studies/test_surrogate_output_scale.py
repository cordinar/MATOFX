"""Log output scale in the scalar surrogate core (ADR-0021).

The per-result choice built on it lives in ``test_surrogate_tuning.py`` (ADR-0022).

The benchmark behind the ADR found two shapes that matter: a result that is a
product of powers of the inputs (a temperature rise) fits far better on a log
scale, and an additive one (a mass) fits far worse. Both are reproduced here
with synthetic data, so the rule is pinned against the cases it exists for.
"""

from __future__ import annotations

import numpy as np
import pytest

from matofx.studies.surrogate import (
    LOG_NOT_POSSIBLE,
    Surrogate,
    fit_surrogate,
    repeated_cv_rmse,
)

_RNG = np.random.default_rng(0)
_N = 40
_FINS = _RNG.uniform(2.0, 12.0, _N)
_FAN = _RNG.uniform(1.0, 10.0, _N)
_COLS = {"fins": list(_FINS), "fan": list(_FAN)}


def _temperature(fins: float, fan: float) -> float:
    """Ambient + a power-law rise: steep in the few-fins, low-fan corner."""
    return 25.0 + 300.0 * fins**-1.2 * fan**-0.6


_TEMP = [_temperature(f, v) for f, v in zip(_FINS, _FAN, strict=True)]
_MASS = [100.0 + 20.0 * f + 3.0 * v for f, v in zip(_FINS, _FAN, strict=True)]


# --- the fit itself ---------------------------------------------------------


def test_log_fit_reports_quality_in_the_results_own_units() -> None:
    """Q², RMSE and held-out predictions are comparable with an as-measured fit."""
    surrogate = fit_surrogate(_COLS, _TEMP, kind="gp", output_scale="log")
    assert surrogate.fitted
    assert surrogate.output_scale == "log"
    assert surrogate.quality.actual == pytest.approx(_TEMP)
    # Held-out predictions sit near the data, not near log(data) (~3.6-4.9).
    assert min(surrogate.quality.predicted) > 25.0
    assert surrogate.quality.rmse_cv is not None
    assert surrogate.quality.rmse_cv < 5.0


def test_log_fit_predicts_in_the_results_own_units_with_delta_method_sigma() -> None:
    surrogate = fit_surrogate(_COLS, _TEMP, kind="gp", output_scale="log")
    point = {"fins": 6.0, "fan": 5.0}
    means, sigmas = surrogate.predict([point])
    assert means[0] == pytest.approx(_temperature(6.0, 5.0), rel=0.02)
    latent_mean, latent_sigma = surrogate._predict_latent([point])
    assert sigmas is not None
    assert latent_sigma is not None
    assert sigmas[0] == pytest.approx(np.exp(latent_mean[0]) * latent_sigma[0])


def test_log_scale_beats_as_measured_on_a_power_law() -> None:
    measured = fit_surrogate(_COLS, _TEMP, kind="gp")
    logged = fit_surrogate(_COLS, _TEMP, kind="gp", output_scale="log")
    assert logged.quality.rmse_cv < measured.quality.rmse_cv  # type: ignore[operator]


def test_log_scale_refused_when_a_run_is_not_positive() -> None:
    surrogate = fit_surrogate(_COLS, [m - 200.0 for m in _MASS], kind="gp", output_scale="log")
    assert not surrogate.fitted
    assert surrogate.quality.message == LOG_NOT_POSSIBLE


def test_far_extrapolation_on_a_log_fit_stays_finite() -> None:
    surrogate = fit_surrogate(_COLS, _TEMP, kind="poly", output_scale="log")
    means, _ = surrogate.predict([{"fins": 1e6, "fan": -1e6}])
    assert np.isfinite(means[0])


def test_a_bundle_written_before_adr_0021_loads_as_measured() -> None:
    """Bundles are pickles of the object; an old one has no ``output_scale``."""
    surrogate = fit_surrogate(_COLS, _TEMP, kind="gp")
    before = surrogate.predict([{"fins": 6.0, "fan": 5.0}])
    state = dict(surrogate.__dict__)
    del state["output_scale"]
    restored = Surrogate.__new__(Surrogate)
    restored.__setstate__(state)
    assert restored.output_scale == "as_measured"
    assert restored.predict([{"fins": 6.0, "fan": 5.0}]) == before


def test_relative_sigma_unchanged_for_an_as_measured_fit() -> None:
    """The coverage guard's number is exactly what it was before ADR-0021."""
    surrogate = fit_surrogate(_COLS, _TEMP, kind="gp")
    points = [{"fins": 6.0, "fan": 5.0}, {"fins": 11.0, "fan": 9.0}]
    _, sigma = surrogate.predict(points)
    spread = float(np.std(surrogate.quality.actual))
    assert sigma is not None
    assert surrogate.relative_sigma(points) == pytest.approx([s / spread for s in sigma])


def test_relative_sigma_of_a_log_fit_is_on_the_log_scale() -> None:
    surrogate = fit_surrogate(_COLS, _TEMP, kind="gp", output_scale="log")
    point = [{"fins": 6.0, "fan": 5.0}]
    _, latent_sigma = surrogate._predict_latent(point)
    spread = float(np.std(np.log(surrogate.quality.actual)))
    assert latent_sigma is not None
    assert surrogate.relative_sigma(point) == pytest.approx([latent_sigma[0] / spread])


def test_relative_sigma_is_none_without_variance() -> None:
    surrogate = fit_surrogate(_COLS, _TEMP, kind="poly", output_scale="log")
    assert surrogate.relative_sigma([{"fins": 6.0, "fan": 5.0}]) is None


def test_conditioning_a_log_fit_shrinks_sigma_at_the_new_point() -> None:
    surrogate = fit_surrogate(_COLS, _TEMP, kind="gp", output_scale="log")
    gap = [{"fins": 2.5, "fan": 9.5}]
    before = surrogate.relative_sigma(gap)
    after = surrogate.conditioned_on(gap).relative_sigma(gap)
    assert before is not None
    assert after is not None
    assert after[0] < before[0]


def test_repeated_cv_scores_an_impossible_log_as_infinite() -> None:
    scores = repeated_cv_rmse(
        _COLS,
        [m - 200.0 for m in _MASS],
        kind="gp",
        output_scale="log",
        seeds=(1, 2),
    )
    assert scores == [float("inf"), float("inf")]
