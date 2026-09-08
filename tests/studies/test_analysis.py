"""Tests for the pure sensitivity analysis (2-PS-b5).

Exercises :func:`compute_sensitivity` and its helpers directly with
hand-built factor columns + responses — no DB, no server. The DB/ACL
orchestration (the ``/sensitivity`` endpoint) is thin glue over this,
covered by the b-6 regression.
"""

from __future__ import annotations

import itertools
import math

import pytest

from matofx.studies.analysis import (
    _const,
    _pair,
    _pearson,
    _spearman,
    anova_contributions,
    compute_sensitivity,
    fit_rsm,
    main_effects,
    pareto_indices,
    rsm_predict,
    summarize,
    varied_columns,
)


def _crossed(a_vals: list[float], b_vals: list[float]) -> tuple[list[float], list[float]]:
    """Fully-crossed (orthogonal) grid of two factors."""
    a: list[float] = []
    b: list[float] = []
    for av, bv in itertools.product(a_vals, b_vals):
        a.append(av)
        b.append(bv)
    return a, b


# --- helpers -------------------------------------------------------------


def test_pair_drops_rows_missing_either_side() -> None:
    xs, ys = _pair([1.0, None, 3.0, 4.0], [10.0, 20.0, None, 40.0])
    assert xs == [1.0, 4.0]
    assert ys == [10.0, 40.0]


def test_const_detects_zero_variance() -> None:
    assert _const([2.0, 2.0, 2.0]) is True
    assert _const([]) is True
    assert _const([1.0, 2.0]) is False


def test_varied_columns_keeps_only_varying_factors() -> None:
    cols: dict[str, list[float | None]] = {
        "a": [1.0, 2.0, 3.0],  # varies
        "held": [5.0, 5.0, 5.0],  # constant → dropped
        "b": [1.0, None, 1.0],  # one distinct value → dropped
        "c": [1.0, None, 9.0],  # two distinct (None ignored) → kept
    }
    assert varied_columns(cols) == ["a", "c"]


def test_pearson_perfect_line() -> None:
    assert _pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]).value == pytest.approx(1.0)
    assert _pearson([1.0, 2.0, 3.0], [-2.0, -4.0, -6.0]).value == pytest.approx(-1.0)


def test_pearson_none_when_constant_or_too_few() -> None:
    assert _pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]).value is None
    assert _pearson([1.0], [2.0]).value is None


def test_spearman_captures_monotonic_nonlinear() -> None:
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [x**3 for x in xs]
    assert _spearman(xs, ys).value == pytest.approx(1.0)
    # Spearman is a perfect 1.0 here while Pearson is strictly below it.
    pearson = _pearson(xs, ys)
    assert pearson.value is not None
    assert pearson.value < 1.0


def test_correlations_carry_pvalues() -> None:
    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    ys = [2.1, 3.9, 6.2, 7.8, 10.1, 12.0]
    pc = _pearson(xs, ys)
    assert pc.value == pytest.approx(1.0, abs=0.01)
    assert pc.pvalue is not None
    assert pc.pvalue < 0.05  # strong, significant correlation


# --- compute_sensitivity -------------------------------------------------


def test_single_factor_linear_response() -> None:
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    res = compute_sensitivity("y", ["x"], {"x": xs}, [2.0 * x for x in xs])
    assert res.sufficient is True
    assert res.r_squared == pytest.approx(1.0)
    assert res.n_samples == 5
    assert len(res.factors) == 1
    f = res.factors[0]
    assert f.factor == "x"
    assert f.pearson == pytest.approx(1.0)
    assert f.spearman == pytest.approx(1.0)
    assert f.std_coef == pytest.approx(1.0)
    assert f.n == 5


def test_single_factor_negative_response() -> None:
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    res = compute_sensitivity("y", ["x"], {"x": xs}, [-3.0 * x for x in xs])
    f = res.factors[0]
    assert f.pearson == pytest.approx(-1.0)
    assert f.std_coef == pytest.approx(-1.0)


def test_two_orthogonal_factors_rank_by_magnitude() -> None:
    a, b = _crossed([0.0, 1.0, 2.0, 3.0], [0.0, 1.0, 2.0, 3.0])
    response = [3.0 * av + bv for av, bv in zip(a, b, strict=True)]
    res = compute_sensitivity("y", ["a", "b"], {"a": a, "b": b}, response)
    assert res.sufficient is True
    assert res.r_squared == pytest.approx(1.0)
    # Tornado order: the stronger driver first.
    assert [f.factor for f in res.factors] == ["a", "b"]
    by = {f.factor: f for f in res.factors}
    coef_a = by["a"].std_coef
    coef_b = by["b"].std_coef
    assert coef_a is not None
    assert coef_b is not None
    # Orthogonal standardized predictors → SRC equals the simple correlation,
    # and the 3:1 raw-coefficient ratio shows up as 3/sqrt(10) : 1/sqrt(10).
    assert coef_a == pytest.approx(3.0 / math.sqrt(10.0))
    assert coef_b == pytest.approx(1.0 / math.sqrt(10.0))
    assert coef_a == pytest.approx(by["a"].pearson)
    assert abs(coef_a) > abs(coef_b) > 0.0


def test_negative_multivariate_coefficient_keeps_sign() -> None:
    a, b = _crossed([0.0, 1.0, 2.0, 3.0], [0.0, 1.0, 2.0, 3.0])
    response = [2.0 * av - 4.0 * bv for av, bv in zip(a, b, strict=True)]
    res = compute_sensitivity("y", ["a", "b"], {"a": a, "b": b}, response)
    by = {f.factor: f for f in res.factors}
    coef_a = by["a"].std_coef
    coef_b = by["b"].std_coef
    assert coef_a is not None
    assert coef_a > 0.0
    assert coef_b is not None
    assert coef_b < 0.0
    # b is the bigger driver despite the sign.
    assert res.factors[0].factor == "b"


def test_insufficient_when_too_few_responses() -> None:
    res = compute_sensitivity("y", ["x"], {"x": [1.0, 2.0, 3.0]}, [10.0, None, None])
    assert res.sufficient is False
    assert res.factors == []
    assert res.r_squared is None
    assert res.n_samples == 1
    assert res.message is not None
    assert "at least 3" in res.message


def test_constant_factor_is_dropped_from_src_but_listed() -> None:
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    const = [7.0, 7.0, 7.0, 7.0, 7.0]
    response = [2.0 * x for x in a]
    res = compute_sensitivity("y", ["a", "c"], {"a": a, "c": const}, response)
    by = {f.factor: f for f in res.factors}
    assert set(by) == {"a", "c"}
    assert by["a"].std_coef == pytest.approx(1.0)
    # Constant factor: no correlation, no coefficient, but still a row.
    assert by["c"].pearson is None
    assert by["c"].spearman is None
    assert by["c"].std_coef is None


def test_constant_response_yields_no_associations() -> None:
    xs = [1.0, 2.0, 3.0, 4.0]
    res = compute_sensitivity("y", ["x"], {"x": xs}, [5.0, 5.0, 5.0, 5.0])
    assert res.sufficient is True  # enough rows, but nothing varies
    assert res.r_squared is None
    f = res.factors[0]
    assert f.pearson is None
    assert f.std_coef is None
    assert res.message is not None


def test_src_unavailable_when_underdetermined_but_correlations_stand() -> None:
    # 3 complete rows, 2 factors → observations <= params + 1, so the fit
    # cannot run; correlations are still reported and drive the ordering.
    a = [1.0, 2.0, 3.0]
    b = [1.0, 3.0, 2.0]
    response = [1.0, 2.0, 3.0]
    res = compute_sensitivity("y", ["a", "b"], {"a": a, "b": b}, response)
    assert res.sufficient is True
    assert res.r_squared is None
    assert all(f.std_coef is None for f in res.factors)
    assert all(f.pearson is not None for f in res.factors)
    assert res.message is not None
    # Fallback ordering by |pearson|: a is perfectly correlated, b is not.
    assert res.factors[0].factor == "a"


def test_partial_factor_coverage_limits_src_not_correlations() -> None:
    # response present for all 4; b present for only 2 → complete rows = 2,
    # below the SRC floor, but a's correlation uses all 4 points.
    a = [1.0, 2.0, 3.0, 4.0]
    b = [1.0, None, None, 4.0]
    response = [2.0, 4.0, 6.0, 8.0]
    res = compute_sensitivity("y", ["a", "b"], {"a": a, "b": b}, response)
    by = {f.factor: f for f in res.factors}
    assert by["a"].n == 4
    assert by["b"].n == 2
    assert all(f.std_coef is None for f in res.factors)
    assert by["a"].pearson == pytest.approx(1.0)


def test_nonfinite_responses_treated_as_missing() -> None:
    # NaN / inf responses are dropped (they would otherwise crash the
    # sklearn fit with "Input y contains NaN"); only the finite rows count.
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    response = [2.0, float("nan"), 6.0, float("inf"), 10.0]
    res = compute_sensitivity("y", ["x"], {"x": xs}, response)
    assert res.sufficient is True
    assert res.n_samples == 3
    f = res.factors[0]
    assert f.pearson == pytest.approx(1.0)
    assert f.std_coef == pytest.approx(1.0)


def test_extreme_values_excluded_with_caveat() -> None:
    # Finite-but-huge values (a diverged solver) are excluded so they don't
    # overflow / dominate the variance-based stats; a caveat is reported.
    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    response = [2.0, 4.0, 6.0, 8.0, 1e285, -1e290]
    res = compute_sensitivity("y", ["x"], {"x": xs}, response)
    assert res.sufficient is True
    assert res.n_samples == 4
    assert res.message is not None
    assert "extreme" in res.message
    assert res.factors[0].std_coef == pytest.approx(1.0)


def test_varied_columns_ignores_nonfinite() -> None:
    cols: dict[str, list[float | None]] = {
        "a": [1.0, float("nan"), 1.0],  # one finite value → dropped
        "b": [1.0, float("nan"), 9.0],  # two finite values → kept
    }
    assert varied_columns(cols) == ["b"]


def test_src_inference_fields_populated() -> None:
    a, b = _crossed([0.0, 1.0, 2.0, 3.0], [0.0, 1.0, 2.0, 3.0])
    # A tiny alternating perturbation keeps residuals non-zero so the
    # p-values / CIs are well-defined (a perfect fit gives SE=0).
    response = [
        3.0 * av + bv + (0.01 if i % 2 else -0.01)
        for i, (av, bv) in enumerate(zip(a, b, strict=True))
    ]
    res = compute_sensitivity("y", ["a", "b"], {"a": a, "b": b}, response)
    by = {f.factor: f for f in res.factors}
    fa = by["a"]
    assert fa.std_coef is not None
    assert fa.std_coef_p is not None
    assert fa.std_coef_p < 0.05
    assert fa.std_coef_ci_low is not None
    assert fa.std_coef_ci_high is not None
    assert fa.std_coef_ci_low <= fa.std_coef <= fa.std_coef_ci_high
    assert res.adj_r_squared is not None
    assert res.adj_r_squared > 0.9
    assert res.rmse is not None
    assert fa.pearson_p is not None


def test_pareto_indices_minimise_both() -> None:
    # minimise both objectives; (3,3) is dominated by (2,2).
    rows = [[1.0, 4.0], [2.0, 2.0], [4.0, 1.0], [3.0, 3.0]]
    assert sorted(pareto_indices(rows, [False, False])) == [0, 1, 2]


def test_pareto_indices_maximise_direction() -> None:
    # maximise both: the corner points dominate; (1,1) is dominated.
    rows = [[1.0, 1.0], [3.0, 1.0], [1.0, 3.0], [2.0, 2.0]]
    assert sorted(pareto_indices(rows, [True, True])) == [1, 2, 3]


def test_pareto_indices_skips_missing_and_keeps_ties() -> None:
    rows: list[list[float | None]] = [[1.0, 1.0], [1.0, 1.0], [2.0, 2.0], [None, 5.0]]
    # identical optimal points both kept; the row with a missing objective is skipped.
    assert sorted(pareto_indices(rows, [False, False])) == [0, 1]


def test_main_effects_means_per_level() -> None:
    a = [0.0, 0.0, 1.0, 1.0, 2.0, 2.0]
    y = [10.0, 12.0, 20.0, 22.0, 30.0, 32.0]
    me = main_effects({"a": a}, y)
    assert me["a"] == [(0.0, 11.0), (1.0, 21.0), (2.0, 31.0)]


def test_anova_contributions_balanced_grid() -> None:
    a, b = _crossed([0.0, 1.0, 2.0], [0.0, 1.0])
    response = [10.0 * av + 1.0 * bv for av, bv in zip(a, b, strict=True)]
    res = anova_contributions({"a": a, "b": b}, response)
    assert res.message is None
    terms = {t.term: t.contribution for t in res.terms}
    assert "a" in terms
    assert "b" in terms
    assert terms["a"] > terms["b"]
    total = sum(t.contribution for t in res.terms) + res.residual
    assert total == pytest.approx(100.0, abs=1e-6)


def test_anova_message_for_continuous_factors() -> None:
    a = [float(i) for i in range(20)]  # 20 distinct levels -> treated as continuous
    res = anova_contributions({"a": a}, [2.0 * x for x in a])
    assert res.terms == []
    assert res.message is not None


def test_fit_rsm_one_factor_parabola() -> None:
    xs = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    y = [(x - 3.0) ** 2 for x in xs]  # minimum at x=3, value 0
    fit = fit_rsm({"x": xs}, y)
    assert fit.message is None
    assert fit.r_squared == pytest.approx(1.0)
    assert fit.stationary is not None
    assert fit.stationary["x"] == pytest.approx(3.0)
    assert fit.stationary_kind == "minimum"
    assert fit.stationary_value == pytest.approx(0.0, abs=1e-6)
    assert rsm_predict(fit, {"x": 5.0}) == pytest.approx(4.0)


def test_fit_rsm_two_factor_bowl() -> None:
    a, b = _crossed([0.0, 1.0, 2.0, 3.0], [0.0, 1.0, 2.0, 3.0])
    y = [(av - 1.0) ** 2 + (bv - 2.0) ** 2 for av, bv in zip(a, b, strict=True)]
    fit = fit_rsm({"a": a, "b": b}, y)
    assert fit.stationary is not None
    assert fit.stationary["a"] == pytest.approx(1.0, abs=1e-6)
    assert fit.stationary["b"] == pytest.approx(2.0, abs=1e-6)
    assert fit.stationary_kind == "minimum"


def test_fit_rsm_too_few_runs() -> None:
    fit = fit_rsm({"x": [0.0, 1.0]}, [1.0, 2.0])  # 2 runs < quadratic terms
    assert fit.coef == []
    assert fit.message is not None


def test_summarize_basic_stats() -> None:
    s = summarize([1.0, 2.0, 3.0, 4.0, 5.0])
    assert (s.count, s.n, s.n_missing, s.n_nonfinite) == (5, 5, 0, 0)
    assert s.minimum == 1.0
    assert s.maximum == 5.0
    assert s.mean == pytest.approx(3.0)
    assert s.median == pytest.approx(3.0)
    assert s.std == pytest.approx(2.5**0.5)  # sample std (ddof=1)


def test_summarize_counts_missing_and_nonfinite() -> None:
    s = summarize([1.0, None, float("nan"), float("inf"), 3.0])
    assert s.count == 5
    assert s.n_missing == 1
    assert s.n_nonfinite == 2
    assert s.n == 2
    assert s.minimum == 1.0
    assert s.maximum == 3.0


def test_summarize_empty_is_all_none() -> None:
    s = summarize([None, float("nan")])
    assert s.n == 0
    assert s.mean is None
    assert s.std is None
    assert s.median is None
    assert s.minimum is None


def test_summarize_single_value_has_no_std() -> None:
    s = summarize([7.0])
    assert s.n == 1
    assert s.mean == 7.0
    assert s.std is None
    assert s.cv is None


def test_unknown_factor_names_are_ignored() -> None:
    xs = [1.0, 2.0, 3.0, 4.0]
    res = compute_sensitivity("y", ["x", "ghost"], {"x": xs}, [1.0, 2.0, 3.0, 4.0])
    assert [f.factor for f in res.factors] == ["x"]
