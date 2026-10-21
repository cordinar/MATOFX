"""Scalar surrogate models for a parametric study (2-PS-c).

A surrogate is a cheap model fitted to the completed runs that predicts one
scalar response at **any** input combination (including unsampled points),
so an engineer can explore the design space, read off a prediction with its
uncertainty, and optimise — the model behind the JMP-style Prediction
Profiler. Four user-selectable backends (see :data:`SurrogateKind`):

- **Gaussian process** (default, RBF kernel) — interpolates the runs and
  returns a prediction **and a 1-sigma uncertainty**, so the UI can show
  where the surrogate is extrapolating versus confident. Auto-fits the
  kernel length-scales; ``O(n^3)`` so it suits up to ~1-2k runs.
- **Gaussian process (Matérn)** — same, with a Matérn(nu=2.5) kernel that
  tolerates a rougher response surface; also gives uncertainty.
- **Polynomial** — a degree-2 (quadratic) least-squares fit: cheap and
  interpretable, the fallback for very large studies.
- **Gradient boosting** — boosted trees for non-smooth / discontinuous
  responses a GP fits poorly; mean prediction only (no uncertainty band).

Quality is reported as **cross-validated R² (Q²)** plus CV-RMSE and the
held-out predicted-vs-actual pairs — *not* in-sample R², which a GP
interpolator trivially drives to 1. Pure compute (scikit-learn); the API /
UI assemble the aligned factor columns + response and call
:func:`fit_surrogate`, hold the returned :class:`Surrogate` for the
session, and call :meth:`Surrogate.predict` for each profiler move.
"""

from __future__ import annotations

import contextlib
import math
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from scipy.stats import qmc
from sklearn.compose import TransformedTargetRegressor
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.exceptions import ConvergenceWarning
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF, ConstantKernel, Matern, WhiteKernel
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import KFold, LeaveOneOut, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import PolynomialFeatures, StandardScaler

from matofx.studies.analysis import (
    _MIN_VARIED_DISTINCT,
    _complete_float_rows,
    _mask_extreme,
    varied_columns,
)

SurrogateKind = Literal["gp", "gp_matern", "poly", "gbm"]
"""Scalar fitting strategies (ADR-0008 §4):

- ``gp`` — Gaussian process, squared-exponential (RBF) kernel. Smooth
  responses; gives an analytic 1-sigma uncertainty band (the default).
- ``gp_matern`` — Gaussian process, Matérn(nu=2.5) kernel. Like ``gp``
  but tolerates a rougher response surface; also gives uncertainty.
- ``poly`` — degree-2 polynomial + linear regression. Fast classical RSM.
- ``gbm`` — gradient-boosted trees. Handles nonlinearity / discontinuities
  GPs fit poorly; mean prediction only (no uncertainty band).
"""

GP_KINDS: frozenset[str] = frozenset({"gp", "gp_matern"})
"""Strategies that produce a per-point 1-sigma uncertainty (drive the ± band)."""

OutputScale = Literal["as_measured", "log"]
"""The scale a scalar result is learned on (ADR-0021).

- ``as_measured`` — the model learns ``y`` itself (every fit before ADR-0021).
- ``log`` — the model learns ``log(y)`` and predictions come back through
  ``exp``. Suits results that behave like a product of powers of the inputs
  (temperature rise, resistance, stress, power); needs every ``y > 0``.
"""

_LOG_CLIP = math.log(np.finfo(float).max) - 1.0
"""Largest latent value ``exp`` is allowed to see: a far extrapolation on the log
scale must come back as a huge number, not ``inf``."""


LOG_NOT_POSSIBLE = "Log scale isn't possible: some runs are zero or negative."
"""Why a result stays as measured whatever was asked for (ADR-0021 §6)."""


def _safe_exp(z: Any) -> Any:
    return np.exp(np.clip(z, -_LOG_CLIP, _LOG_CLIP))


_MIN_SURROGATE_POINTS = 5
"""Below this many complete runs a surrogate isn't worth fitting / validating."""

_MAX_CV_FOLDS = 5
"""k-fold count for cross-validated Q² once there are enough runs for it;
fewer runs use leave-one-out."""

_SOBOL_BASE_SAMPLES = 512
"""Base sample size N for the Saltelli scheme (power of two for the Sobol
sequence); total surrogate evaluations are ``N * (k + 2)``."""


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Where a surrogate's training data came from (ADR-0008 §4.2).

    Set when a surrogate is fitted from ingested ``study_result`` rows
    (``matofx.studies.surrogate.ingest``); ``None`` for the current-style
    "fit from raw columns" parametric callers, which stay unchanged.
    """

    source_kind: str
    """``parametric`` / ``optimisation`` / ``cohort`` (the discriminator)."""
    source_id: int
    n_points: int
    """Complete rows fed into the fit."""


@dataclass(frozen=True, slots=True)
class SurrogateQuality:
    """Cross-validated quality of a surrogate fit (the honest trust metric)."""

    q2: float | None
    """Cross-validated R² (leave-one-out / k-fold); ``None`` if not computable."""
    rmse_cv: float | None
    n: int
    actual: list[float]
    predicted: list[float]
    """Held-out predictions aligned with :attr:`actual` (predicted-vs-actual plot)."""
    message: str | None


class Surrogate:
    """A fitted scalar surrogate over the study's varied numeric inputs."""

    def __init__(
        self,
        *,
        factors: list[str],
        kind: SurrogateKind,
        ranges: dict[str, tuple[float, float]],
        means: dict[str, float],
        quality: SurrogateQuality,
        scaler: Any = None,
        model: Any = None,
        training_inputs: dict[str, list[float]] | None = None,
        source_ref: SourceRef | None = None,
        y_scale_override: float | None = None,
        categorical_levels: Mapping[str, Sequence[str]] | None = None,
        training_categoricals: Mapping[str, Sequence[str]] | None = None,
        output_scale: OutputScale = "as_measured",
        log_factors: Sequence[str] = (),
    ) -> None:
        """Hold a fitted (or, when ``model`` is ``None``, unfitted) surrogate."""
        self.factors = factors
        self.kind = kind
        self.output_scale: OutputScale = output_scale
        """What the model learned (ADR-0021). ``quality`` is always reported in
        the result's own units, whichever scale this is."""
        self.log_factors: tuple[str, ...] = tuple(str(f) for f in log_factors)
        """Numeric factors the model sees as ``log(x)`` (ADR-0022). Callers always
        pass real units — :meth:`_encode` takes the log — and ``training_inputs``,
        ``ranges`` and ``means`` stay in real units."""
        self.ranges = ranges
        self.means = means
        self.quality = quality
        self._scaler = scaler
        self._model = model
        self.training_inputs: dict[str, list[float]] = training_inputs or {}
        """The values each factor took during fitting (ADR-0008 §4.2) —
        drives the in-form coverage gauge. Empty for an unfitted surrogate."""
        self.source_ref: SourceRef | None = source_ref
        """Where the training data came from, or ``None`` for raw-column
        (parametric) callers."""
        self.categorical_levels: dict[str, tuple[str, ...]] = {
            str(name): tuple(str(v) for v in levels)
            for name, levels in (categorical_levels or {}).items()
        }
        """Varied non-numeric factors and the levels seen while fitting.

        A ``select`` input (a material, a solver choice) is a real design axis
        and the model is fitted on it, one-hot encoded onto the end of the
        numeric matrix. It is kept **out** of :attr:`factors` / :attr:`ranges`
        / :attr:`means`, which stay numeric so the coverage gauge and the
        profiler are unchanged; :meth:`predict` re-encodes each point.

        Before this, a varied categorical was silently dropped: two runs with
        identical numbers and different materials became the same point with
        two different answers, so the fit carried irreducible error and the
        model returned one blended prediction for both.
        """
        self.training_categoricals: dict[str, list[str]] = {
            str(name): [str(v) for v in values]
            for name, values in (training_categoricals or {}).items()
        }
        """The level each training row used, row-aligned with
        :attr:`training_inputs`.

        The numeric half alone cannot rebuild the model matrix once a
        categorical is encoded onto it, and :meth:`conditioned_on` has to."""
        self._y_scale_override = y_scale_override
        """Set to 1.0 by :meth:`conditioned_on`, whose frozen GP is fitted on
        an already-standardised response — its ``predict`` sigma is therefore
        relative already and must not be divided by the response spread a
        second time (ADR-0020 §5)."""

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Restore a pickled surrogate, defaulting attributes added since.

        **A bundle is a joblib pickle of this object, and bundles outlive the
        class.** Every surrogate exported before a categorical became a real
        factor was written without ``categorical_levels``, so the first
        prediction from any of them raised ``AttributeError`` — the tool would
        have failed at run time, not at load time, in the one surface that runs
        unattended. Defaulting here rather than at each use site means the next
        attribute added is handled too.
        """
        state.setdefault("categorical_levels", {})
        state.setdefault("training_categoricals", {})
        # Every bundle written before ADR-0021 learned the result as measured.
        state.setdefault("output_scale", "as_measured")
        # ...and every input as measured before ADR-0022.
        state.setdefault("log_factors", ())
        self.__dict__.update(state)

    @property
    def fitted(self) -> bool:
        """Whether a model was fitted (``False`` carries a ``quality.message``)."""
        return self._model is not None

    def predict(
        self, points: Sequence[Mapping[str, float]]
    ) -> tuple[list[float], list[float] | None]:
        """Predict the response at each point; second list is 1-sigma (GP) or ``None``.

        Factors absent from a point default to their training mean, so a
        profiler can vary one factor while holding the rest at a setpoint.

        Always in the result's own units. A ``log`` fit returns ``exp(μ)`` — the
        median, which is also what its cross-validated quality was scored on —
        and the delta-method 1-sigma ``exp(mu) * sigma``.
        """
        mean, std = self._predict_latent(points)
        if self.output_scale != "log" or not mean:
            return mean, std
        values = _safe_exp(np.asarray(mean))
        out = [float(v) for v in values]
        if std is None:
            return out, None
        return out, [float(v * s) for v, s in zip(values, std, strict=True)]

    def _predict_latent(
        self, points: Sequence[Mapping[str, float]]
    ) -> tuple[list[float], list[float] | None]:
        """The model's own output: ``y`` for as-measured, ``log(y)`` for a log fit."""
        if self._model is None or not points:
            return [], None
        x = np.asarray([self._encode(p) for p in points])
        xs = self._scaler.transform(x)
        if self.kind in GP_KINDS:
            mean, std = self._model.predict(xs, return_std=True)
            return [float(m) for m in mean], [float(s) for s in std]
        return [float(m) for m in self._model.predict(xs)], None

    def relative_sigma(self, points: Sequence[Mapping[str, float]]) -> list[float] | None:
        """1-sigma as a fraction of the training response's spread (ADR-0020 §5).

        Unit-free, so comparable across responses. Computed on the scale the
        model learned: for a ``log`` fit that is ``sigma / std(log y)``, which keeps
        the coverage thresholds calibrated — dividing the delta-method sigma
        by ``std(y)`` would instead make hot corners look less supported.
        ``None`` for a method with no variance.
        """
        if not self.has_variance:
            return None
        _, sigma = self._predict_latent(points)
        if sigma is None:
            return None
        scale = self.y_scale or 1.0
        return [float(s) / scale for s in sigma]

    def _encode(self, point: Mapping[str, Any]) -> list[float]:
        """One model row for ``point``: numeric factors, then the one-hots.

        A missing numeric factor falls back to its training mean, so a
        profiler can vary one axis and hold the rest. A missing **categorical**
        falls back to the level the training set used most, for the same
        reason. An *unrecognised* level is refused rather than encoded as all
        zeros — all-zeros is a point the model never saw, and returning a
        confident number for it is the failure this whole encoding exists to
        stop.
        """
        row = [float(point.get(name, self.means.get(name, 0.0))) for name in self.factors]
        if self.log_factors:
            logged = set(self.log_factors)
            for i, name in enumerate(self.factors):
                if name not in logged:
                    continue
                if row[i] <= 0.0:
                    # A silent NaN here would reach an exported tool's outputs.json
                    # unnoticed; the form guards it, this is the backstop.
                    raise ValueError(
                        f"{name!r} must be above 0 for this model, which learned it on a "
                        f"log scale; got {row[i]:g}."
                    )
                row[i] = math.log(row[i])
        for name, levels in self.categorical_levels.items():
            raw = point.get(name)
            value = str(raw) if raw is not None else levels[0]
            if value not in levels:
                raise ValueError(
                    f"{name!r} was {value!r}, which this model was not trained on. "
                    f"It saw: {', '.join(levels)}."
                )
            row.extend(1.0 if value == level else 0.0 for level in levels)
        return row

    def _training_point(self, index: int) -> dict[str, Any]:
        """Training row ``index`` as a ``{factor: value}`` point, levels included."""
        point: dict[str, Any] = {name: self.training_inputs[name][index] for name in self.factors}
        for name, values in self.training_categoricals.items():
            if index < len(values):
                point[name] = values[index]
        return point

    # --- coverage support (ADR-0020 §5) ---------------------------------

    @property
    def has_variance(self) -> bool:
        """Whether this fit carries a per-point posterior sigma.

        Only the two GP kinds do, which is why the exporter's ± gate is
        already GP-only. ``poly`` / ``gbm`` are user-selectable, and MOP's
        auto-best-metamodel may pick one on Q² alone — so callers must ask
        rather than assume.
        """
        return self.fitted and self.kind in GP_KINDS

    @property
    def y_scale(self) -> float | None:
        """The spread of the training response — the unit ``sigma`` comes in.

        The GP fits with ``normalize_y=True``, so ``predict`` multiplies its
        standard deviation back up by this before returning it. Dividing by
        it again gives a **relative** sigma that is genuinely free of the
        response's magnitude and therefore comparable across responses in
        different physical units (ADR-0020 §5).

        On the scale the model learned: ``std(log y)`` for a ``log`` fit.
        """
        if self._y_scale_override is not None:
            return self._y_scale_override
        latent = self._latent_actual()
        if latent is None or latent.size == 0:
            return None
        spread = float(np.std(latent))
        return spread if spread > 0.0 else None

    def _latent_actual(self) -> Any:
        """The training response on the scale the model learned, or ``None``."""
        actual = np.asarray(self.quality.actual, dtype=float)
        if self.output_scale != "log":
            return actual
        if actual.size and np.any(actual <= 0.0):
            return None
        return np.log(actual)

    @property
    def ard_length_scales(self) -> list[float] | None:
        """Per-factor ARD length scales of the fitted kernel, or ``None``.

        Short means the response changes quickly along that axis — the
        thing that lets a GP-based criterion spend an expensive simulation
        budget on the factors the response actually responds to.
        """
        if not self.has_variance:
            return None
        for part in self._kernel_parts():
            length_scale = getattr(part, "length_scale", None)
            if length_scale is not None:
                scales = np.atleast_1d(np.asarray(length_scale, dtype=float))
                # An ARD kernel is fitted over the encoded matrix, whose leading
                # columns are the numeric factors — so slice rather than fall
                # back to a single shared scale, which would throw away exactly
                # the per-axis signal a budget-steering criterion needs.
                if scales.size >= len(self.factors):
                    return [float(v) for v in scales[: len(self.factors)]]
                return [float(scales[0])] * len(self.factors)
        return None

    def _kernel_parts(self) -> list[Any]:
        """Flatten the fitted kernel's sum/product tree into its leaves."""
        kernel = getattr(self._model, "kernel_", None)
        out: list[Any] = []
        stack = [kernel] if kernel is not None else []
        while stack:
            node = stack.pop()
            k1, k2 = getattr(node, "k1", None), getattr(node, "k2", None)
            if k1 is not None or k2 is not None:
                stack.extend(k for k in (k1, k2) if k is not None)
            else:
                out.append(node)
        return out

    def conditioned_on(
        self,
        points: Sequence[Mapping[str, float]],
        *,
        pseudo_response: Sequence[float] | None = None,
    ) -> Surrogate:
        """A copy whose posterior also accounts for ``points`` (ADR-0020 §5).

        The mechanism greedy sequential selection needs: treat a candidate
        as though it had already returned a value, so the *next* pick moves
        to a different hole instead of crowding the same one.

        **The hyperparameters are frozen** at the values this fit found —
        re-optimising them on a made-up response would be steering by a
        compass recalibrated mid-turn. With them frozen the resulting sigma
        contains no ``y`` term at all, which is what lets a whole batch of
        runs be planned with **zero solver calls**: ``pseudo_response`` can
        be anything and the sigma field is identical. It defaults to the
        current posterior mean, the least disruptive choice for the *mean*
        predictions this copy also carries.

        Raises ``ValueError`` for a method with no posterior variance —
        there is nothing to condition.
        """
        if not self.has_variance:
            raise ValueError(
                f"A '{self.kind}' surrogate has no uncertainty to condition on; only "
                "the Gaussian-process methods do."
            )
        if not points:
            return self

        rows = [self._encode(p) for p in points]
        extra_y = (
            [float(v) for v in pseudo_response]
            if pseudo_response is not None
            else self.predict(points)[0]
        )
        base_x = [self._encode(self._training_point(i)) for i in range(len(self.quality.actual))]
        x = np.asarray(base_x + rows)
        y = np.asarray([*self.quality.actual, *extra_y], dtype=float)
        xs = self._scaler.transform(x)
        base = np.asarray(self.quality.actual, dtype=float)
        if self.output_scale == "log":
            # The kernel was fitted on log(y), so the frozen copy must be too. A
            # made-up pseudo-response at or below zero is floored: sigma is
            # y-free, so it only nudges the copy's mean.
            tiny = np.finfo(float).tiny
            y = np.log(np.maximum(y, tiny))
            base = np.log(np.maximum(base, tiny))

        # normalize_y=False + the already-fitted kernel: no re-optimisation,
        # no y-driven rescaling, so sigma is exactly y-free.
        scale = self.y_scale or 1.0
        frozen = GaussianProcessRegressor(kernel=self._model.kernel_, optimizer=None)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=ConvergenceWarning)
            frozen.fit(xs, (y - float(np.mean(base))) / scale)

        # The copy is already on the latent, standardised scale (y_scale 1.0),
        # so it is "as measured" from here on — including for a log parent.
        return Surrogate(
            factors=list(self.factors),
            kind=self.kind,
            categorical_levels=dict(self.categorical_levels),
            training_categoricals={k: list(v) for k, v in self.training_categoricals.items()},
            ranges=dict(self.ranges),
            means=dict(self.means),
            quality=SurrogateQuality(
                self.quality.q2,
                self.quality.rmse_cv,
                len(y),
                [float(v) for v in y],
                [],
                self.quality.message,
            ),
            scaler=self._scaler,
            model=frozen,
            # Real units, like every other surrogate: rebuilt from the points
            # rather than the encoded matrix, which is logged for a log-input fit.
            training_inputs={
                name: [
                    *(float(v) for v in self.training_inputs.get(name, [])),
                    *(float(p.get(name, self.means.get(name, 0.0))) for p in points),
                ]
                for name in self.factors
            },
            source_ref=self.source_ref,
            y_scale_override=1.0,
            log_factors=self.log_factors,
        )


def _finite_or_none(x: float) -> float | None:
    return float(x) if math.isfinite(float(x)) else None


def _make_estimator(kind: SurrogateKind, k: int) -> Any:
    if kind == "poly":
        return Pipeline(
            [
                ("poly", PolynomialFeatures(degree=2, include_bias=False)),
                ("lin", LinearRegression()),
            ]
        )
    if kind == "gbm":
        # Trees handle non-smooth / discontinuous surfaces a GP can't; the
        # conservative defaults keep small-DOE fits from over-growing.
        return GradientBoostingRegressor(
            n_estimators=200, max_depth=3, learning_rate=0.05, random_state=0
        )
    noise = WhiteKernel(noise_level=1e-3, noise_level_bounds=(1e-6, 1e1))
    if kind == "gp_matern":
        kernel = (
            ConstantKernel(1.0)
            * Matern(length_scale=[1.0] * k, length_scale_bounds=(1e-2, 1e2), nu=2.5)
            + noise
        )
    else:  # gp (RBF, the smooth default)
        kernel = (
            ConstantKernel(1.0) * RBF(length_scale=[1.0] * k, length_scale_bounds=(1e-2, 1e2))
            + noise
        )
    return GaussianProcessRegressor(
        kernel=kernel, normalize_y=True, n_restarts_optimizer=2, random_state=0
    )


def _on_scale(estimator: Any, output_scale: OutputScale) -> Any:
    """``estimator`` learning ``log(y)`` but predicting ``y`` for a log fit.

    Cross-validation goes through this so held-out predictions, Q² and RMSE
    are always in the result's own units — the only way the two scales can
    be compared like for like (ADR-0021).
    """
    if output_scale != "log":
        return estimator
    return TransformedTargetRegressor(
        regressor=estimator, func=np.log, inverse_func=_safe_exp, check_inverse=False
    )


def _cross_val_quality(
    estimator: Any, xs: Any, y: Any, n: int, output_scale: OutputScale = "as_measured"
) -> SurrogateQuality:
    actual = [float(v) for v in y]
    cv = (
        LeaveOneOut()
        if n <= _MAX_CV_FOLDS * 2
        else KFold(n_splits=_MAX_CV_FOLDS, shuffle=True, random_state=0)
    )
    pred: Any = None
    with (
        contextlib.suppress(Exception),
        warnings.catch_warnings(),
        np.errstate(over="ignore", invalid="ignore"),
    ):
        warnings.simplefilter("ignore", category=ConvergenceWarning)
        pred = cross_val_predict(_on_scale(estimator, output_scale), xs, y, cv=cv)
    if pred is None:
        return SurrogateQuality(None, None, n, actual, [], "Cross-validation failed.")
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    q2 = _finite_or_none(1.0 - ss_res / ss_tot) if ss_tot > 0 else None
    rmse = _finite_or_none(float(np.sqrt(np.mean((y - pred) ** 2))))
    return SurrogateQuality(q2, rmse, n, actual, [float(v) for v in pred], None)


@dataclass(frozen=True, slots=True)
class _Design:
    """The model matrix a scalar fit is built from, before scaling."""

    names: list[str]
    """Varied numeric factors."""
    levels: dict[str, tuple[str, ...]]
    cols: dict[str, list[float]]
    n: int
    k: int
    """Model columns: numeric factors plus one-hots."""
    x: Any
    """Model matrix; the ``logged`` columns already hold ``log(x)``."""
    y: Any
    logged: tuple[str, ...] = ()
    log_problem: str | None = None

    def problem(self, kind: SurrogateKind) -> str | None:
        """Why ``kind`` can't be fitted on this design, or ``None``."""
        if self.log_problem is not None:
            return self.log_problem
        if self.k == 0 or self.n < _MIN_SURROGATE_POINTS:
            return (
                f"Need at least {_MIN_SURROGATE_POINTS} complete runs and a varied factor "
                "to fit a surrogate."
            )
        if kind == "poly":
            n_terms = 1 + 2 * self.k + self.k * (self.k - 1) // 2
            if self.n <= n_terms:
                return (
                    f"A degree-2 polynomial surrogate needs more than {n_terms} complete "
                    f"runs for {self.k} factor(s); try the Gaussian process."
                )
        return None


def _design(
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
    categorical_columns: Mapping[str, Sequence[Any]] | None,
    log_factors: Sequence[str] = (),
) -> _Design:
    names = varied_columns(factor_columns)
    levels = _categorical_levels(categorical_columns)
    encoded = _one_hot_columns(categorical_columns, levels)
    # One-hots are ordinary numeric columns from here on, so row completeness,
    # scaling and cross-validation all work unchanged.
    all_columns = {**factor_columns, **encoded}
    model_names = [*names, *encoded]
    masked, _ = _mask_extreme(response)
    y_list, cols = _complete_float_rows(all_columns, masked, model_names)
    n = len(y_list)
    x = np.asarray([[cols[name][r] for name in model_names] for r in range(n)], dtype=float)
    wanted = set(log_factors)
    logged = tuple(name for name in names if name in wanted)
    log_problem = None
    for name in logged:
        j = names.index(name)
        if x.size and bool(np.any(x[:, j] <= 0.0)):
            log_problem = f"Log inputs aren't possible: {name!r} has runs at or below zero."
            break
        if x.size:
            x[:, j] = np.log(x[:, j])
    return _Design(
        names,
        levels,
        cols,
        n,
        len(model_names),
        x,
        np.asarray(y_list, dtype=float),
        logged,
        log_problem,
    )


def repeated_cv_rmse(
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
    *,
    kind: SurrogateKind,
    output_scale: OutputScale,
    seeds: Sequence[int],
    categorical_columns: Mapping[str, Sequence[Any]] | None = None,
    log_factors: Sequence[str] = (),
) -> list[float]:
    """Shuffled 5-fold CV RMSE (result's own units) once per seed.

    The evidence the automatic output-scale choice is made on (ADR-0021 §3).
    A repeat that cannot be scored — too few rows per fold for a polynomial, a
    non-finite prediction, ``log`` on a non-positive response — scores ``inf``,
    so it can never win.
    """
    design = _design(factor_columns, response, categorical_columns, log_factors)
    if design.problem(kind) is not None or design.n < _MAX_CV_FOLDS:
        return [math.inf for _ in seeds]
    y = design.y
    if output_scale == "log" and bool(np.any(y <= 0.0)):
        return [math.inf for _ in seeds]
    xs = StandardScaler().fit_transform(design.x)
    out: list[float] = []
    for seed in seeds:
        estimator = _on_scale(_make_estimator(kind, design.k), output_scale)
        cv = KFold(n_splits=_MAX_CV_FOLDS, shuffle=True, random_state=seed)
        pred: Any = None
        with (
            contextlib.suppress(Exception),
            warnings.catch_warnings(),
            np.errstate(over="ignore", invalid="ignore"),
        ):
            warnings.simplefilter("ignore", category=ConvergenceWarning)
            pred = cross_val_predict(estimator, xs, y, cv=cv)
        rmse = math.inf if pred is None else float(np.sqrt(np.mean((y - pred) ** 2)))
        out.append(rmse if math.isfinite(rmse) else math.inf)
    return out


def fit_surrogate(
    factor_columns: Mapping[str, Sequence[float | None]],
    response: Sequence[float | None],
    *,
    kind: SurrogateKind = "gp",
    source_ref: SourceRef | None = None,
    categorical_columns: Mapping[str, Sequence[Any]] | None = None,
    output_scale: OutputScale = "as_measured",
    log_factors: Sequence[str] = (),
) -> Surrogate:
    """Fit a scalar surrogate over the varied inputs (2-PS-c).

    Excludes NaN / non-finite and extreme (diverged) response values, fits
    on standardized inputs, and reports cross-validated Q². Returns an
    unfitted :class:`Surrogate` carrying a ``quality.message`` when there
    are too few complete runs (or, for ``poly``, fewer runs than terms).

    ``source_ref`` is an *additive* tag recording where the data came from
    (set by the ingestion layer, ADR-0008 §4.2); raw-column callers omit
    it and behave exactly as before (non-negotiable #10).

    ``categorical_columns`` are the varied **non-numeric** inputs, row-aligned
    with ``factor_columns``. They are one-hot encoded onto the end of the model
    matrix, so a material or a solver choice is a real axis of the fit. Omitting
    them (the raw-column callers) behaves exactly as before.

    ``output_scale='log'`` learns ``log(y)`` (ADR-0021). It needs every kept
    response value to be positive; otherwise the result is unfitted with a
    message saying so. Quality is reported in the result's own units either way.

    ``log_factors`` are numeric factors learned as ``log(x)`` (ADR-0022); every
    value must be positive. Names that aren't varied factors are ignored.
    """
    design = _design(factor_columns, response, categorical_columns, log_factors)
    names, levels, cols, n, k = design.names, design.levels, design.cols, design.n, design.k

    def _empty(message: str) -> Surrogate:
        return Surrogate(
            factors=names,
            kind=kind,
            ranges={},
            means={},
            quality=SurrogateQuality(None, None, n, [], [], message),
            categorical_levels=levels,
            output_scale=output_scale,
            log_factors=design.logged,
        )

    problem = design.problem(kind)
    if problem is not None:
        return _empty(problem)
    x, y = design.x, design.y
    if output_scale == "log" and bool(np.any(y <= 0.0)):
        return _empty(LOG_NOT_POSSIBLE)

    scaler = StandardScaler().fit(x)
    xs = scaler.transform(x)
    estimator = _make_estimator(kind, k)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=ConvergenceWarning)
        estimator.fit(xs, np.log(y) if output_scale == "log" else y)
    quality = _cross_val_quality(estimator, xs, y, n, output_scale)
    return Surrogate(
        output_scale=output_scale,
        log_factors=design.logged,
        factors=names,
        kind=kind,
        # Numeric-only, deliberately: the coverage gauge, the profiler and the
        # exported form all read these and none of them wants a one-hot column.
        ranges={name: (min(cols[name]), max(cols[name])) for name in names},
        means={name: float(np.mean(cols[name])) for name in names},
        quality=quality,
        scaler=scaler,
        model=estimator,
        training_inputs={name: [float(v) for v in cols[name]] for name in names},
        source_ref=source_ref,
        categorical_levels=levels,
        # Recovered from the one-hot columns of the rows that survived the
        # completeness filter, so it is row-aligned with ``training_inputs``
        # without threading the raw column through the row filter.
        training_categoricals=_levels_per_row(cols, levels, n),
    )


def _levels_per_row(
    cols: Mapping[str, Sequence[float]],
    levels: Mapping[str, tuple[str, ...]],
    n_rows: int,
) -> dict[str, list[str]]:
    """The level each kept row used, read back off its one-hot columns."""
    out: dict[str, list[str]] = {}
    for name, names_levels in levels.items():
        picked: list[str] = []
        for row in range(n_rows):
            hot = next(
                (lv for lv in names_levels if cols[f"{name}={lv}"][row] == 1.0),
                names_levels[0],
            )
            picked.append(hot)
        out[name] = picked
    return out


def _categorical_levels(
    categorical_columns: Mapping[str, Sequence[Any]] | None,
) -> dict[str, tuple[str, ...]]:
    """The distinct levels of each **varied** categorical, in sorted order.

    A column with one level is not an axis — it is a held constant, and
    one-hot encoding it would add a column of ones the scaler cannot scale.
    """
    out: dict[str, tuple[str, ...]] = {}
    for name, column in (categorical_columns or {}).items():
        seen = sorted({str(v) for v in column if v is not None and str(v) != ""})
        if len(seen) >= _MIN_VARIED_DISTINCT:
            out[str(name)] = tuple(seen)
    return out


def _one_hot_columns(
    categorical_columns: Mapping[str, Sequence[Any]] | None,
    levels: Mapping[str, tuple[str, ...]],
) -> dict[str, list[float | None]]:
    """``{"material=Copper": [0.0, 1.0, …]}`` — one column per level.

    Every level gets a column rather than dropping a baseline: with a
    distance-based kernel, an implicit baseline sits at the origin and is not
    equidistant from the rest, which would make one material special for no
    physical reason. A row whose value is missing is ``None`` in every column
    of that factor, so ``_complete_float_rows`` drops it exactly as it drops a
    row with a missing number.
    """
    out: dict[str, list[float | None]] = {}
    for name, names_levels in levels.items():
        column = list((categorical_columns or {}).get(name) or [])
        for level in names_levels:
            out[f"{name}={level}"] = [
                None if v is None else (1.0 if str(v) == level else 0.0) for v in column
            ]
    return out


@dataclass(frozen=True, slots=True)
class SobolIndex:
    """Variance-based global sensitivity of the response to one factor."""

    factor: str
    s1: float
    """First-order index — variance share from this factor alone."""
    st: float
    """Total-effect index — share including all interactions (``st - s1``)."""


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def sobol_indices(surrogate: Surrogate, *, n: int = _SOBOL_BASE_SAMPLES) -> list[SobolIndex] | None:
    """Variance-based (Sobol) global sensitivity, evaluated on the surrogate.

    Saltelli scheme over each factor's sampled box (Sobol low-discrepancy
    sequence): first-order ``S1`` and total-effect ``ST`` per factor
    (Jansen estimators), clamped to ``[0, 1]``. Captures nonlinearity and
    interactions the linear standardized-regression sensitivity cannot.
    Returns ``None`` if the surrogate isn't fitted.
    """
    if not surrogate.fitted or not surrogate.factors:
        return None
    factors = surrogate.factors
    k = len(factors)
    bounds = np.asarray([surrogate.ranges[f] for f in factors], dtype=float)
    base = qmc.Sobol(d=2 * k, scramble=True, seed=0).random(n)
    span = bounds[:, 1] - bounds[:, 0]
    a = bounds[:, 0] + base[:, :k] * span
    b = bounds[:, 0] + base[:, k:] * span

    def _f(matrix: Any) -> Any:
        points = [{factors[j]: float(matrix[i, j]) for j in range(k)} for i in range(len(matrix))]
        means, _ = surrogate.predict(points)
        return np.asarray(means)

    y_a = _f(a)
    y_b = _f(b)
    var = float(np.var(np.concatenate([y_a, y_b])))
    if var <= 0.0:
        return [SobolIndex(factors[j], 0.0, 0.0) for j in range(k)]
    out: list[SobolIndex] = []
    for i in range(k):
        ab = a.copy()
        ab[:, i] = b[:, i]
        y_ab = _f(ab)
        s1 = float(np.mean(y_b * (y_ab - y_a)) / var)
        st = float(0.5 * np.mean((y_a - y_ab) ** 2) / var)
        out.append(SobolIndex(factors[i], _clamp01(s1), _clamp01(st)))
    return out


def optimize_surrogate(surrogate: Surrogate, *, maximize: bool = True) -> dict[str, float] | None:
    """Factor settings that maximise (or minimise) the surrogate over its box.

    Global search (differential evolution) over each factor's sampled range,
    evaluated on the cheap surrogate. Returns the best operating point as a
    ``{factor: value}`` dict, or ``None`` if the surrogate isn't fitted.
    """
    if not surrogate.fitted or not surrogate.factors:
        return None
    from scipy.optimize import differential_evolution  # noqa: PLC0415

    factors = surrogate.factors
    bounds = [surrogate.ranges[f] for f in factors]
    sign = -1.0 if maximize else 1.0

    def objective(x: Any) -> Any:
        # vectorised: x is (n_factors, n_candidates); predict all at once.
        points = [{f: float(x[i, j]) for i, f in enumerate(factors)} for j in range(x.shape[1])]
        means, _ = surrogate.predict(points)
        return sign * np.asarray(means)

    result = differential_evolution(
        objective, bounds, vectorized=True, seed=0, maxiter=60, tol=1e-3, polish=True
    )
    return {f: float(result.x[i]) for i, f in enumerate(factors)}


# Imported at the *bottom*, after the engine (Surrogate / fit_surrogate)
# is defined: ``field`` imports those from this package, so this must run
# once the names exist — re-exporting the field engine from the package
# root without a circular import.
from matofx.studies.surrogate.field import (  # noqa: E402
    FieldPrediction,
    FieldQuality,
    FieldSurrogate,
    FieldSurrogateError,
    fit_field_surrogate,
    load_field_snapshot,
)

__all__ = [
    "GP_KINDS",
    "LOG_NOT_POSSIBLE",
    "FieldPrediction",
    "FieldQuality",
    "FieldSurrogate",
    "FieldSurrogateError",
    "OutputScale",
    "SobolIndex",
    "SourceRef",
    "Surrogate",
    "SurrogateKind",
    "SurrogateQuality",
    "fit_field_surrogate",
    "fit_surrogate",
    "load_field_snapshot",
    "optimize_surrogate",
    "repeated_cv_rmse",
    "sobol_indices",
]
