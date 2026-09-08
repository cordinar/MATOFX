"""Pure sensitivity analysis: a study's response vs its numeric factors (2-PS-b5).

Given a study's numeric factors and one chosen scalar response (the b0-5
response picker — a named scalar, or a ``csv_plot`` column reduced by an
aggregate), rank which factors drive the response. Three complementary
measures, each robust to the partial / failure-biased samples the D3
banner warns about:

- **Pearson** correlation — linear association per factor, computed
  pairwise-complete (``scipy.stats.pearsonr``).
- **Spearman** rank correlation — monotonic-but-not-necessarily-linear
  association (``scipy.stats.spearmanr``).
- **Standardized regression coefficients (SRC)** — each factor's
  multivariate effect with the others held at their mean, from an
  ordinary least-squares fit on z-scored inputs *and* a z-scored
  response (``statsmodels`` OLS, which also supplies the per-coefficient
  standard error / p-value / confidence interval and the model
  adjusted-R² and RMSE). Because both sides are standardized the
  coefficients are directly comparable across factors; ``|SRC|`` is the
  tornado-bar length and the model R² says how much of the response
  variance the linear model explains.

This module also carries the wider engineering-statistics surface
(2-PS-c-stats): per-response :func:`summarize`, multi-objective
:func:`pareto_indices`, designed-experiment :func:`main_effects` /
:func:`anova_contributions`, and the quadratic :func:`fit_rsm`.

Pure / no DB / no IO: the API layer assembles the aligned factor columns
+ response from a :class:`~matofx.studies.results_service.StudyResultsView`
and calls :func:`compute_sensitivity`.

Partial-data gating (design §10.0 / D3): every measure is computed only
over children that actually have a response value. Below
``_MIN_USABLE_POINTS`` such children the whole result is flagged
``sufficient=False`` and the UI greys the panel. The SRC fit additionally
needs more complete rows than active factors; when it cannot run the
correlations still stand and ``std_coef`` / ``r_squared`` come back
``None`` with a caveat in ``message``.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import statsmodels.api as sm
from scipy import stats

_MIN_USABLE_POINTS = 3
"""Below this many children with a response value, sensitivity is not
meaningful — the whole result is flagged insufficient (UI greys out)."""

_MIN_CORR_POINTS = 2
"""``pearsonr`` / ``spearmanr`` need at least this many paired points."""

_MIN_VARIED_DISTINCT = 2
"""A factor is "varied" only if it takes at least this many distinct values
across the children — a constant input is not a sensitivity axis."""

_EXTREME_MAGNITUDE = 1e12
"""Finite response values beyond this magnitude are treated as invalid
(non-physical / diverged solver) and excluded with a caveat — otherwise a
handful of ~1e285 blow-ups dominate / overflow the variance-based stats."""

_MAX_ANOVA_LEVELS = 12
"""A factor with more distinct values than this is treated as continuous
(not a categorical ANOVA factor) — keeps LHS sweeps out of the ANOVA."""

_SINGULAR_DET_TOL = 1e-12
"""Below this |determinant| the RSM Hessian is treated as singular (no
stationary point)."""


@dataclass(frozen=True, slots=True)
class FactorSensitivity:
    """One factor's association with the response, three ways."""

    factor: str
    pearson: float | None
    """Pearson r over pairwise-complete points; ``None`` if a side is
    constant or there are too few points."""
    spearman: float | None
    """Spearman rank correlation, same gating as :attr:`pearson`."""
    std_coef: float | None
    """Standardized regression coefficient (tornado magnitude is its
    absolute value); ``None`` when the multivariate fit could not run."""
    n: int
    """Pairwise-complete points behind the correlations."""
    pearson_p: float | None = None
    """Two-sided p-value for the Pearson correlation."""
    spearman_p: float | None = None
    """Two-sided p-value for the Spearman correlation."""
    std_coef_se: float | None = None
    """Standard error of the standardized regression coefficient."""
    std_coef_p: float | None = None
    """p-value for the standardized regression coefficient (H0: = 0)."""
    std_coef_ci_low: float | None = None
    """Low end of the 95% confidence interval for the SRC (tornado error bar)."""
    std_coef_ci_high: float | None = None
    """High end of the 95% confidence interval for the SRC."""


@dataclass(frozen=True, slots=True)
class SensitivityResult:
    """The full sensitivity result for one response axis."""

    response: str
    factors: list[FactorSensitivity]
    """Ordered for a tornado chart: by ``|std_coef|`` desc, falling back
    to ``|pearson|`` desc when the regression was unavailable."""
    r_squared: float | None
    """R² of the standardized linear model, or ``None`` if it did not run."""
    n_samples: int
    """Rows behind the headline number — complete rows for the SRC fit
    when it ran, else children with a response value."""
    sufficient: bool
    """``False`` when too few children have a response (UI greys out)."""
    message: str | None
    """Why the result is insufficient, or a caveat (e.g. SRC unavailable)."""
    adj_r_squared: float | None = None
    """Adjusted R² of the standardized linear model (penalises extra factors)."""
    rmse: float | None = None
    """Root-mean-square error of the standardized-model residuals."""


@dataclass(frozen=True, slots=True)
class _Corr:
    """A correlation coefficient with its two-sided p-value."""

    value: float | None
    pvalue: float | None


@dataclass(frozen=True, slots=True)
class _Fit:
    """Standardized OLS fit: per-factor inference + model quality."""

    coef: dict[str, float | None]
    se: dict[str, float | None]
    pvalue: dict[str, float | None]
    ci_low: dict[str, float | None]
    ci_high: dict[str, float | None]
    r_squared: float | None
    adj_r_squared: float | None
    rmse: float | None
    n: int


@dataclass(frozen=True, slots=True)
class ResponseSummary:
    """Descriptive statistics for one response across the children (2-PS-c-stats)."""

    count: int
    """Total values considered (every child)."""
    n_missing: int
    """Children with no value for this response (``None``)."""
    n_nonfinite: int
    """Children whose value was NaN / inf (likely a diverged run)."""
    n: int
    """Finite values the statistics are computed over."""
    minimum: float | None
    maximum: float | None
    mean: float | None
    median: float | None
    std: float | None
    """Sample standard deviation (ddof=1); ``None`` for n<2 or overflow."""
    cv: float | None
    """Coefficient of variation (std / |mean|); ``None`` when undefined."""
    p5: float | None
    p25: float | None
    p75: float | None
    p95: float | None


@dataclass(frozen=True, slots=True)
class AnovaTerm:
    """One ANOVA term's share of the response variance (2-PS-c-stats #5)."""

    term: str
    """Factor name, or ``"a x b"`` for a two-factor interaction."""
    contribution: float
    """Percent of the total sum of squares attributed to this term."""


@dataclass(frozen=True, slots=True)
class AnovaResult:
    """Sequential (Type I) ANOVA % contribution for a designed study."""

    terms: list[AnovaTerm]
    residual: float
    """Percent of the total sum of squares left unexplained."""
    n: int
    message: str | None


@dataclass(frozen=True, slots=True)
class RsmFit:
    """Quadratic response-surface fit (2-PS-c-stats #6)."""

    factors: list[str]
    coef: list[float]
    """OLS coefficients in term order: intercept, linear, squares, interactions."""
    means: dict[str, float]
    ranges: dict[str, tuple[float, float]]
    r_squared: float | None
    adj_r_squared: float | None
    rmse: float | None
    n: int
    stationary: dict[str, float] | None
    """Factor values at the surface's critical point, or ``None`` if singular."""
    stationary_kind: str | None
    """``"minimum"`` / ``"maximum"`` / ``"saddle"`` from the Hessian."""
    stationary_value: float | None
    message: str | None


# --- pure helpers --------------------------------------------------------


def summarize(values: Sequence[float | None]) -> ResponseSummary:
    """Descriptive stats over one response column, robust to missing / non-finite.

    ``None`` is counted as missing and NaN/inf as non-finite (both excluded
    from the statistics); ``std`` / ``cv`` come back ``None`` rather than
    ``inf`` when a diverged value overflows the variance.
    """
    count = len(values)
    n_missing = sum(1 for v in values if v is None)
    finite = [float(v) for v in values if v is not None and math.isfinite(v)]
    n = len(finite)
    n_nonfinite = count - n_missing - n
    if n == 0:
        return ResponseSummary(
            count=count,
            n_missing=n_missing,
            n_nonfinite=n_nonfinite,
            n=0,
            minimum=None,
            maximum=None,
            mean=None,
            median=None,
            std=None,
            cv=None,
            p5=None,
            p25=None,
            p75=None,
            p95=None,
        )
    arr = np.asarray(finite, dtype=float)

    def _ok(x: float) -> float | None:
        return float(x) if math.isfinite(x) else None

    mean = _ok(float(arr.mean()))
    std = _ok(float(arr.std(ddof=1))) if n > 1 else None
    p5, p25, p50, p75, p95 = (float(p) for p in np.percentile(arr, [5, 25, 50, 75, 95]))
    cv = std / abs(mean) if (std is not None and mean is not None and mean != 0.0) else None
    return ResponseSummary(
        count=count,
        n_missing=n_missing,
        n_nonfinite=n_nonfinite,
        n=n,
        minimum=_ok(float(arr.min())),
        maximum=_ok(float(arr.max())),
        mean=mean,
        median=_ok(p50),
        std=std,
        cv=cv,
        p5=_ok(p5),
        p25=_ok(p25),
        p75=_ok(p75),
        p95=_ok(p95),
    )


def pareto_indices(rows: Sequence[Sequence[float | None]], maximize: Sequence[bool]) -> list[int]:
    """Indices of the non-dominated (Pareto-optimal) rows (2-PS-c-stats #4).

    ``rows[i]`` is the objective vector for run ``i`` and ``maximize[j]``
    the desired direction of objective ``j``. Rows with any missing /
    non-finite objective are skipped. Row *a* dominates *b* when it is at
    least as good on every objective and strictly better on one; a row
    kept here is dominated by nobody. Identical rows are all kept.
    """
    sign = [(-1.0 if m else 1.0) for m in maximize]  # fold every objective to "minimise"
    pts: list[tuple[int, list[float]]] = []
    for i, row in enumerate(rows):
        vals = [v for v in row if v is not None and math.isfinite(v)]
        if len(vals) == len(sign):  # every objective present + finite
            pts.append((i, [sign[j] * vals[j] for j in range(len(sign))]))
    out: list[int] = []
    for idx, p in pts:
        dominated = any(
            all(q[j] <= p[j] for j in range(len(p))) and any(q[j] < p[j] for j in range(len(p)))
            for other, q in pts
            if other != idx
        )
        if not dominated:
            out.append(idx)
    return out


def _complete_float_rows(
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
    names: Sequence[str],
) -> tuple[list[float], dict[str, list[float]]]:
    """Rows where the response and every named factor are finite, as float lists."""
    y_list: list[float] = []
    cols: dict[str, list[float]] = {name: [] for name in names}
    for i, yv in enumerate(response):
        if yv is None or not math.isfinite(yv):
            continue
        row: dict[str, float] = {}
        complete = True
        for name in names:
            col = factor_columns[name]
            v = col[i] if i < len(col) else None
            if v is None or not math.isfinite(v):
                complete = False
                break
            row[name] = float(v)
        if complete:
            y_list.append(float(yv))
            for name in names:
                cols[name].append(row[name])
    return y_list, cols


def main_effects(
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
) -> dict[str, list[tuple[float, float]]]:
    """Mean response at each level of each factor (the main-effects plot data).

    ``{factor: [(level, mean_response), ...]}`` sorted by level, over rows
    where both the factor value and the response are finite.
    """
    out: dict[str, list[tuple[float, float]]] = {}
    for name, col in factor_columns.items():
        groups: dict[float, list[float]] = {}
        for v, y in zip(col, response, strict=False):
            if v is not None and y is not None and math.isfinite(v) and math.isfinite(y):
                groups.setdefault(float(v), []).append(float(y))
        if groups:
            out[name] = [(lvl, sum(ys) / len(ys)) for lvl, ys in sorted(groups.items())]
    return out


def anova_contributions(
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
) -> AnovaResult:
    """Sequential (Type I) ANOVA % contribution: main effects + 2-way interactions.

    Treats each factor with 2..``_MAX_ANOVA_LEVELS`` distinct levels as
    categorical (so LHS sweeps are excluded), treatment-codes them, and
    decomposes the total sum of squares by adding terms in order. Balanced
    designs (full-factorial grids) make the order immaterial. Returns a
    message instead of terms when the data don't suit an ANOVA.
    """
    names = sorted(factor_columns)
    y_list, cols = _complete_float_rows(factor_columns, response, names)

    n = len(y_list)
    if n < _MIN_USABLE_POINTS:
        return AnovaResult([], 100.0, n, "Not enough complete runs for an ANOVA.")

    levels: dict[str, list[float]] = {}
    for name in names:
        distinct = sorted(set(cols[name]))
        if _MIN_VARIED_DISTINCT <= len(distinct) <= _MAX_ANOVA_LEVELS:
            levels[name] = distinct
    cat = list(levels)
    if not cat:
        return AnovaResult(
            [], 100.0, n, "Main-effects / ANOVA suit designed (grid/factorial) studies."
        )

    y = np.asarray(y_list)
    sst = float(np.sum((y - y.mean()) ** 2))
    if sst == 0.0:
        return AnovaResult([], 100.0, n, "Response is constant across these runs.")

    coded: dict[str, Any] = {}
    for name in cat:
        ref_then = levels[name][1:]  # treatment coding: first level is the reference
        coded[name] = np.asarray(
            [[1.0 if cols[name][r] == lvl else 0.0 for lvl in ref_then] for r in range(n)]
        )

    blocks: list[tuple[str, Any]] = [(name, coded[name]) for name in cat]
    for a, b in itertools.combinations(cat, 2):
        ca, cb = coded[a], coded[b]
        if ca.shape[1] and cb.shape[1]:
            inter = np.column_stack(
                [ca[:, ia] * cb[:, ib] for ia in range(ca.shape[1]) for ib in range(cb.shape[1])]
            )
            blocks.append((f"{a} x {b}", inter))

    design = np.ones((n, 1))
    sse_prev = sst  # intercept-only residual SS equals SST
    terms: list[AnovaTerm] = []
    for label, block in blocks:
        if block.shape[1] == 0:
            continue
        design = np.column_stack([design, block])
        sse = float(sm.OLS(y, design).fit().ssr)
        terms.append(AnovaTerm(label, 100.0 * max(0.0, sse_prev - sse) / sst))
        sse_prev = sse
    return AnovaResult(terms, 100.0 * max(0.0, sse_prev) / sst, n, None)


def _rsm_design_row(x: Sequence[float]) -> list[float]:
    """Quadratic design row: ``[1, x_i..., x_i^2..., x_i*x_j (i<j)...]``."""
    k = len(x)
    row = [1.0, *x, *[v * v for v in x]]
    row += [x[i] * x[j] for i in range(k) for j in range(i + 1, k)]
    return row


def _rsm_eval(coef: Sequence[float], x: Sequence[float]) -> float:
    row = _rsm_design_row(x)
    return float(sum(row[i] * coef[i] for i in range(len(coef))))


def _rsm_stationary(
    names: Sequence[str], coef: Sequence[float]
) -> tuple[dict[str, float] | None, str | None, float | None]:
    """Critical point of the fitted quadratic, classified by its Hessian."""
    k = len(names)
    linear = np.asarray(coef[1 : 1 + k], dtype=float)
    square = coef[1 + k : 1 + 2 * k]
    inter = coef[1 + 2 * k :]
    hess = np.zeros((k, k))
    for m in range(k):
        hess[m, m] = 2.0 * square[m]
    idx = 0
    for i in range(k):
        for j in range(i + 1, k):
            hess[i, j] = hess[j, i] = inter[idx]
            idx += 1
    if not np.all(np.isfinite(hess)) or abs(float(np.linalg.det(hess))) < _SINGULAR_DET_TOL:
        return None, None, None
    x_star = np.linalg.solve(hess, -linear)
    if not np.all(np.isfinite(x_star)):
        return None, None, None
    eig = np.linalg.eigvals(hess)
    if np.all(eig > 0):
        kind = "minimum"
    elif np.all(eig < 0):
        kind = "maximum"
    else:
        kind = "saddle"
    stationary = {names[m]: float(x_star[m]) for m in range(k)}
    return stationary, kind, _rsm_eval(coef, [float(v) for v in x_star])


def fit_rsm(
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
) -> RsmFit:
    """Fit a quadratic response surface (main + squares + 2-way interactions).

    Works on any sample with enough complete runs (``n`` greater than the
    number of quadratic terms). Returns fit quality (R²/adj-R²/RMSE), the
    coefficients (for prediction / contouring), and the surface's
    stationary point classified via the Hessian, or a ``message`` when it
    cannot be fitted.
    """
    names = sorted(factor_columns)
    y_list, cols = _complete_float_rows(factor_columns, response, names)
    n = len(y_list)
    k = len(names)
    n_terms = 1 + 2 * k + k * (k - 1) // 2
    if k == 0 or n <= n_terms:
        return RsmFit(
            names,
            [],
            {},
            {},
            None,
            None,
            None,
            n,
            None,
            None,
            None,
            f"Need more than {n_terms} complete runs for a quadratic surface in {k} factor(s).",
        )
    design = np.asarray([_rsm_design_row([cols[name][r] for name in names]) for r in range(n)])
    y = np.asarray(y_list)
    model = sm.OLS(y, design).fit()
    coef = [float(p) for p in model.params]
    stationary, kind, value = _rsm_stationary(names, coef)
    return RsmFit(
        factors=names,
        coef=coef,
        means={name: float(np.mean(cols[name])) for name in names},
        ranges={name: (min(cols[name]), max(cols[name])) for name in names},
        r_squared=_finite_or_none(model.rsquared),
        adj_r_squared=_finite_or_none(model.rsquared_adj),
        rmse=_finite_or_none(np.sqrt(model.mse_resid)),
        n=n,
        stationary=stationary,
        stationary_kind=kind,
        stationary_value=value,
        message=None,
    )


def rsm_predict(fit: RsmFit, values: Mapping[str, float]) -> float:
    """Predicted response at ``values`` (factors absent default to their mean)."""
    x = [float(values.get(name, fit.means.get(name, 0.0))) for name in fit.factors]
    return _rsm_eval(fit.coef, x)


def varied_columns(factor_columns: Mapping[str, Sequence[float | None]]) -> list[str]:
    """Names of the columns that actually vary (≥ 2 distinct non-null values).

    The "varied inputs" a sensitivity should rank — a factor held constant
    across the run is not an axis. Sorted for a stable row order.
    """
    out: list[str] = []
    for name, col in factor_columns.items():
        distinct = {v for v in col if v is not None and math.isfinite(v)}
        if len(distinct) >= _MIN_VARIED_DISTINCT:
            out.append(name)
    return sorted(out)


def _mask_extreme(response: Sequence[float | None]) -> tuple[list[float | None], int]:
    """None-out finite-but-extreme response values (likely solver divergence)."""
    out: list[float | None] = list(response)
    n = 0
    for i, v in enumerate(out):
        if v is not None and math.isfinite(v) and abs(v) > _EXTREME_MAGNITUDE:
            out[i] = None
            n += 1
    return out, n


def _pair(
    column: Sequence[float | None], response: Sequence[float | None]
) -> tuple[list[float], list[float]]:
    """``(x, y)`` over rows where both the factor and the response exist."""
    xs: list[float] = []
    ys: list[float] = []
    for xv, yv in zip(column, response, strict=False):
        if xv is not None and yv is not None and math.isfinite(xv) and math.isfinite(yv):
            xs.append(float(xv))
            ys.append(float(yv))
    return xs, ys


def _const(values: Sequence[float]) -> bool:
    """True for an empty or zero-variance series (correlation undefined)."""
    return len(values) == 0 or min(values) == max(values)


def _finite_or_none(x: float) -> float | None:
    """``float(x)`` if finite, else ``None`` (keeps NaN/inf out of the payload)."""
    return float(x) if math.isfinite(float(x)) else None


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> _Corr:
    if len(xs) < _MIN_CORR_POINTS or _const(xs) or _const(ys):
        return _Corr(None, None)
    res = stats.pearsonr(xs, ys)
    return _Corr(_finite_or_none(res.statistic), _finite_or_none(res.pvalue))


def _spearman(xs: Sequence[float], ys: Sequence[float]) -> _Corr:
    if len(xs) < _MIN_CORR_POINTS or _const(xs) or _const(ys):
        return _Corr(None, None)
    res = stats.spearmanr(xs, ys)
    return _Corr(_finite_or_none(res.statistic), _finite_or_none(res.pvalue))


def _zscore(a: Any) -> Any:
    """Standardize each column to zero mean / unit variance — overflow-safe.

    A plain ``(a - mean) / std`` overflows ``float64`` when values are huge
    (e.g. a diverged solver writing ~1e285): squaring for the variance
    hits ``inf`` and the standardized column collapses to 0. Normalising the
    deviations by their max magnitude before squaring keeps the variance
    finite, so the coefficients stay meaningful for large-but-valid data.
    """
    mu = a.mean(axis=0)
    dev = a - mu
    scale = np.abs(dev).max(axis=0)
    scale = np.where(scale > 0.0, scale, 1.0)
    std = np.sqrt(np.mean((dev / scale) ** 2, axis=0)) * scale
    std = np.where(std > 0.0, std, 1.0)
    return dev / std


def _standardized_regression(
    names: Sequence[str],
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
) -> _Fit | None:
    """Standardized OLS over rows complete across the response and every factor.

    Returns a :class:`_Fit` with per-factor coefficient / SE / p-value /
    95% CI plus model R², adjusted R² and RMSE, or ``None`` when the fit
    cannot run: too few complete rows, no varying factor, a constant
    response, or fewer observations than parameters. Constant factors over
    the complete rows are dropped (singular). Both sides are z-scored, so
    the coefficients are standardized (directly comparable) and the
    intercept is ~0; statsmodels OLS supplies the inference.
    """
    cols = {name: list(factor_columns.get(name, ())) for name in names}
    y_vals: list[float] = []
    x_rows: list[list[float]] = []
    for i, yv in enumerate(response):
        if yv is None or not math.isfinite(yv):
            continue
        row: list[float] = []
        complete = True
        for name in names:
            column = cols[name]
            cell = column[i] if i < len(column) else None
            if cell is None or not math.isfinite(cell):
                complete = False
                break
            row.append(float(cell))
        if complete:
            y_vals.append(float(yv))
            x_rows.append(row)

    n_complete = len(y_vals)
    if not names or n_complete < _MIN_USABLE_POINTS or _const(y_vals):
        return None

    x_all = np.asarray(x_rows, dtype=float)
    active: list[str] = []
    keep: list[int] = []
    for j, name in enumerate(names):
        if not _const(list(x_all[:, j])):
            active.append(name)
            keep.append(j)
    # Need more observations than parameters for any residual signal.
    if not active or n_complete <= len(active) + 1:
        return None

    xz = _zscore(x_all[:, keep])
    yz = _zscore(np.asarray(y_vals, dtype=float))
    model = sm.OLS(yz, sm.add_constant(xz, has_constant="add")).fit()
    ci = np.asarray(model.conf_int())
    coef: dict[str, float | None] = {}
    se: dict[str, float | None] = {}
    pvalue: dict[str, float | None] = {}
    ci_low: dict[str, float | None] = {}
    ci_high: dict[str, float | None] = {}
    for idx, name in enumerate(active, start=1):  # 0 is the intercept
        coef[name] = _finite_or_none(model.params[idx])
        se[name] = _finite_or_none(model.bse[idx])
        pvalue[name] = _finite_or_none(model.pvalues[idx])
        ci_low[name] = _finite_or_none(ci[idx, 0])
        ci_high[name] = _finite_or_none(ci[idx, 1])
    return _Fit(
        coef=coef,
        se=se,
        pvalue=pvalue,
        ci_low=ci_low,
        ci_high=ci_high,
        r_squared=_finite_or_none(model.rsquared),
        adj_r_squared=_finite_or_none(model.rsquared_adj),
        rmse=_finite_or_none(np.sqrt(model.mse_resid)),
        n=n_complete,
    )


def _tornado_key(f: FactorSensitivity) -> float:
    """Sort key for tornado order: ``|SRC|``, else ``|pearson|``, else last."""
    if f.std_coef is not None:
        return abs(f.std_coef)
    if f.pearson is not None:
        return abs(f.pearson)
    return -1.0


def compute_sensitivity(
    response_label: str,
    factor_names: Sequence[str],
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
) -> SensitivityResult:
    """Rank ``factor_names`` by their influence on ``response``.

    ``factor_columns`` maps each factor to its per-child values and
    ``response`` is the per-child response, all aligned by index (use
    ``None`` for a missing value). Names absent from ``factor_columns``
    are ignored. See the module docstring for the measures and gating.
    """
    names = [n for n in factor_names if n in factor_columns]
    response, n_extreme = _mask_extreme(response)
    n_response = sum(1 for v in response if v is not None and math.isfinite(v))
    if n_response < _MIN_USABLE_POINTS:
        note = f" ({n_extreme} excluded as extreme / diverged)" if n_extreme else ""
        return SensitivityResult(
            response=response_label,
            factors=[],
            r_squared=None,
            n_samples=n_response,
            sufficient=False,
            message=(
                f"Only {n_response} child(ren) have a usable response value{note}; "
                f"need at least {_MIN_USABLE_POINTS} for sensitivity."
            ),
        )

    fit = _standardized_regression(names, factor_columns, response)

    factors: list[FactorSensitivity] = []
    for name in names:
        xs, ys = _pair(factor_columns[name], response)
        pc = _pearson(xs, ys)
        sc = _spearman(xs, ys)
        factors.append(
            FactorSensitivity(
                factor=name,
                pearson=pc.value,
                spearman=sc.value,
                std_coef=fit.coef.get(name) if fit else None,
                n=len(xs),
                pearson_p=pc.pvalue,
                spearman_p=sc.pvalue,
                std_coef_se=fit.se.get(name) if fit else None,
                std_coef_p=fit.pvalue.get(name) if fit else None,
                std_coef_ci_low=fit.ci_low.get(name) if fit else None,
                std_coef_ci_high=fit.ci_high.get(name) if fit else None,
            )
        )
    factors.sort(key=_tornado_key, reverse=True)

    parts: list[str] = []
    if n_extreme:
        parts.append(
            f"{n_extreme} run(s) had extreme values (|value| > 1e12, likely "
            "solver divergence) and were excluded"
        )
    if fit is None:
        parts.append(
            "standardized regression needs more complete runs than factors "
            "(and a varying response); showing correlations only"
        )
    message = ("; ".join(parts) + ".") if parts else None
    return SensitivityResult(
        response=response_label,
        factors=factors,
        r_squared=fit.r_squared if fit else None,
        n_samples=fit.n if fit else n_response,
        sufficient=True,
        message=message,
        adj_r_squared=fit.adj_r_squared if fit else None,
        rmse=fit.rmse if fit else None,
    )


__all__ = [
    "AnovaResult",
    "AnovaTerm",
    "FactorSensitivity",
    "ResponseSummary",
    "RsmFit",
    "SensitivityResult",
    "anova_contributions",
    "compute_sensitivity",
    "fit_rsm",
    "main_effects",
    "pareto_indices",
    "rsm_predict",
    "summarize",
    "varied_columns",
]
