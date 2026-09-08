"""Tests for the scalar surrogate fit core (2-PS-c).

Pure — fits over hand-built factor columns + responses (no DB / server).
The session caching + Prediction Profiler are thin glue covered elsewhere.
"""

from __future__ import annotations

import itertools

import pytest

from matofx.studies.surrogate import (
    Surrogate,
    fit_surrogate,
    optimize_surrogate,
    sobol_indices,
)


def _crossed(a_vals: list[float], b_vals: list[float]) -> tuple[list[float], list[float]]:
    a: list[float] = []
    b: list[float] = []
    for av, bv in itertools.product(a_vals, b_vals):
        a.append(av)
        b.append(bv)
    return a, b


def test_poly_surrogate_fits_quadratic() -> None:
    a, b = _crossed([0.0, 1.0, 2.0, 3.0, 4.0], [0.0, 1.0, 2.0, 3.0, 4.0])
    y = [(av - 2.0) ** 2 + (bv - 1.0) ** 2 for av, bv in zip(a, b, strict=True)]
    s = fit_surrogate({"a": a, "b": b}, y, kind="poly")
    assert s.fitted
    assert s.quality.message is None
    assert s.quality.q2 is not None
    assert s.quality.q2 > 0.99  # a quadratic surrogate nails a quadratic truth
    means, std = s.predict([{"a": 2.0, "b": 1.0}])
    assert means[0] == pytest.approx(0.0, abs=1e-6)
    assert std is None  # polynomial has no per-point uncertainty


def test_gp_surrogate_predicts_and_has_uncertainty() -> None:
    xs = [float(i) for i in range(12)]
    y = [2.0 * x + 1.0 for x in xs]
    s = fit_surrogate({"x": xs}, y, kind="gp")
    assert s.fitted
    means, std = s.predict([{"x": 5.0}])
    assert means[0] == pytest.approx(11.0, abs=1.0)
    assert std is not None
    assert std[0] >= 0.0


def test_gp_matern_surrogate_predicts_and_has_uncertainty() -> None:
    xs = [float(i) for i in range(12)]
    y = [2.0 * x + 1.0 for x in xs]
    s = fit_surrogate({"x": xs}, y, kind="gp_matern")
    assert s.fitted
    means, std = s.predict([{"x": 5.0}])
    assert means[0] == pytest.approx(11.0, abs=1.5)
    assert std is not None  # Matérn GP still yields a 1-sigma band
    assert std[0] >= 0.0


def test_gbm_surrogate_fits_without_uncertainty() -> None:
    # Gradient boosting handles non-smooth responses; it has no analytic
    # uncertainty, so predict's second list is None.
    xs = [float(i) for i in range(20)]
    y = [(x - 10.0) ** 2 for x in xs]
    s = fit_surrogate({"x": xs}, y, kind="gbm")
    assert s.fitted
    means, std = s.predict([{"x": 0.0}])
    assert means[0] == pytest.approx(100.0, abs=30.0)
    assert std is None


def test_predict_defaults_absent_factor_to_mean() -> None:
    a, b = _crossed([0.0, 1.0, 2.0, 3.0], [0.0, 1.0, 2.0, 3.0])
    y = [av + bv for av, bv in zip(a, b, strict=True)]
    s = fit_surrogate({"a": a, "b": b}, y, kind="poly")
    # b omitted -> held at its mean (1.5); only one prediction returned.
    means, _ = s.predict([{"a": 2.0}])
    assert len(means) == 1


def test_too_few_runs_returns_unfitted_with_message() -> None:
    s = fit_surrogate({"x": [0.0, 1.0, 2.0]}, [1.0, 2.0, 3.0], kind="gp")
    assert not s.fitted
    assert s.quality.message is not None
    assert s.predict([{"x": 1.0}]) == ([], None)


def test_poly_underdetermined_returns_message() -> None:
    # 2 factors -> 1 + 2*2 + 1 = 6 quadratic terms; 5 runs is too few.
    a = [0.0, 1.0, 2.0, 3.0, 4.0]
    b = [0.0, 1.0, 0.0, 1.0, 0.0]
    s = fit_surrogate({"a": a, "b": b}, [1.0, 2.0, 3.0, 4.0, 5.0], kind="poly")
    assert not s.fitted
    assert s.quality.message is not None


def test_optimize_surrogate_finds_the_minimum() -> None:
    a, b = _crossed([0.0, 1.0, 2.0, 3.0, 4.0], [0.0, 1.0, 2.0, 3.0, 4.0])
    y = [(av - 3.0) ** 2 + (bv - 1.0) ** 2 for av, bv in zip(a, b, strict=True)]
    s = fit_surrogate({"a": a, "b": b}, y, kind="poly")
    opt = optimize_surrogate(s, maximize=False)
    assert opt is not None
    assert opt["a"] == pytest.approx(3.0, abs=0.2)
    assert opt["b"] == pytest.approx(1.0, abs=0.2)


def test_optimize_unfitted_returns_none() -> None:
    s = fit_surrogate({"x": [0.0, 1.0, 2.0]}, [1.0, 2.0, 3.0], kind="gp")
    assert optimize_surrogate(s) is None


def test_sobol_isolates_the_driving_factor() -> None:
    # response depends on a only; b is inert -> S1/ST high for a, ~0 for b.
    a, b = _crossed([0.0, 1.0, 2.0, 3.0, 4.0], [0.0, 1.0, 2.0, 3.0, 4.0])
    y = [3.0 * av for av, _ in zip(a, b, strict=True)]
    s = fit_surrogate({"a": a, "b": b}, y, kind="poly")
    idx = sobol_indices(s, n=128)
    assert idx is not None
    by = {i.factor: i for i in idx}
    assert by["a"].s1 > 0.8
    assert by["a"].st > 0.8
    assert by["b"].st < 0.2
    assert all(0.0 <= i.s1 <= 1.0 and 0.0 <= i.st <= 1.0 for i in idx)


def test_sobol_unfitted_returns_none() -> None:
    s = fit_surrogate({"x": [0.0, 1.0, 2.0]}, [1.0, 2.0, 3.0], kind="gp")
    assert sobol_indices(s) is None


def test_unfitted_surrogate_predict_is_empty() -> None:
    s = Surrogate(
        factors=["x"],
        kind="gp",
        ranges={},
        means={},
        quality=fit_surrogate({"x": [0.0]}, [1.0]).quality,
    )
    assert s.predict([{"x": 1.0}]) == ([], None)
