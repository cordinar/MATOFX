"""The sampling-plan generator (designs/parametric-studies.md §4).

``generate(...)`` is a **pure function** — no DB, no IO. It turns a
:class:`~matofx.studies.factors.FactorSpec` + a mode + mode-specific
params + a seed into the list of input dictionaries the study will fan
out as child jobs, after dropping points that violate the tool's
cross-field rules and refusing if the survivors exceed the frozen run
cap. It is on the H40 must-be-exhaustively-tested list.

Pipeline, in order (design §4):

1. Generate the raw matrix in factor space (per-mode engine below).
2. Map coded RSM/screening points to real input values via each
   factor's range.
3. Apply the descriptor's §16.16 rules to every completed point via the
   *exact* :func:`matofx.tools.forms.resolve_for_generation` the real
   submit path uses: ``set_value`` forces are written in; formula-driven
   bounds, ``require`` validity rules, §F30 cross-field constraints and
   sum-checks are enforced. Drop invalid points; accumulate the skip
   count + the plain-English reason.
4. Enforce the run cap: survivors over ``run_cap`` ⇒
   :class:`RunCapExceededError`.

| mode | engine |
|---|---|
| ``list`` | ``itertools`` cross-product of explicit value sets |
| ``grid`` | ``itertools`` full-factorial (ranges discretised) |
| ``frac_factorial`` / ``plackett_burman`` | ``pyDOE3`` (2-level) |
| ``lhs`` / ``sobol`` / ``random`` | ``scipy.stats.qmc`` / numpy |
| ``ccd`` / ``box_behnken`` | ``pyDOE3`` coded design → real |

Determinism: the stochastic modes (``lhs`` / ``sobol`` / ``random``)
take ``seed``; the rest are deterministic and ignore it.
"""

from __future__ import annotations

import itertools
import math
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pyDOE3
from pydantic import BaseModel, ConfigDict, Field
from scipy.stats import qmc, truncnorm

from matofx.studies._intsnap import snap_int, snap_precision
from matofx.studies.errors import FactorSpecError, RunCapExceededError, StudyGenerationError
from matofx.studies.factors import (
    DistributionVary,
    Factor,
    FactorSpec,
    FileListVary,
    GridSpacing,
    ListVary,
    RangeVary,
    SubsetVary,
)
from matofx.studies.orm import StudyMode
from matofx.tools import forms
from matofx.tools.descriptor import InputField, ToolDescriptor

_BOX_BEHNKEN_MIN_FACTORS = 3
# A unit-cube coordinate splits a boolean factor at the midpoint.
_BOOL_SPLIT = 0.5

# Grid level placement (factors.GridSpacing). Each entry is a monotonic
# warp g(t) of the normalised position t in [0, 1]; _placed() affine-
# maps g(t) into [min, max]. The log/cosine acts on the POSITION, never
# on the values, so there is no sign / min>0 constraint (design §3,
# 2026-05-19 amendment): every spacing works on any range.
_LOG_BASE = 10.0
_WARP: dict[GridSpacing, Callable[[float], float]] = {
    "linear": lambda t: t,
    "log_min": lambda t: (_LOG_BASE**t - 1.0) / (_LOG_BASE - 1.0),
    "log_max": lambda t: 1.0 - (_LOG_BASE ** (1.0 - t) - 1.0) / (_LOG_BASE - 1.0),
    "edges": lambda t: (1.0 - math.cos(math.pi * t)) / 2.0,
    "center": lambda t: 0.5 + math.asin(2.0 * t - 1.0) / math.pi,
}


def _req(x: float | None) -> float:
    """Narrow a distribution parameter the model already guarantees.

    ``DistributionVary``'s validator enforces that the right parameters
    are present for each ``dist``; this is the type-narrowing companion
    (no ``assert`` — library code shouldn't rely on ``-O``-strippable
    assertions for an invariant).
    """
    if x is None:  # pragma: no cover - guaranteed by DistributionVary
        raise StudyGenerationError("distribution parameter missing")
    return x


class SamplingParams(BaseModel):
    """Mode-specific knobs (design §2 ``sampling_params``).

    Every field defaults sensibly; only the ones the chosen mode reads
    are used.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    points_per_factor: int = Field(default=3, ge=2, le=1000)
    """``grid``: how many evenly-spaced levels a numeric range becomes."""

    n_samples: int = Field(default=20, ge=1, le=1_000_000)
    """``lhs`` / ``sobol`` / ``random``: how many points to draw."""

    frac_gen: str | None = None
    """``frac_factorial``: explicit pyDOE3 generator string (e.g.
    ``"a b ab"``). When ``None`` a resolution-``frac_resolution``
    design is built automatically."""

    frac_resolution: int = Field(default=3, ge=3, le=7)
    """``frac_factorial``: target design resolution when ``frac_gen``
    is not given."""

    ccd_face: Literal["circumscribed", "inscribed", "faced"] = "faced"
    """``ccd``: axial-point placement. ``faced`` (default) keeps every
    point inside the declared range; the others extrapolate / shrink."""

    ccd_alpha: Literal["orthogonal", "rotatable"] = "orthogonal"
    """``ccd``: axial distance rule (ignored when ``ccd_face='faced'``)."""

    ccd_center: tuple[int, int] = (1, 1)
    """``ccd``: (factorial-block, axial-block) centre-point replicate counts."""

    bb_center: int | None = None
    """``box_behnken``: centre-point replicate count (pyDOE3 default
    when ``None``)."""

    runs: list[dict[str, Any]] | None = None
    """``table`` mode: the user-authored run rows — varied columns
    only; constants are merged in by the generator. ``None`` for every
    other mode (design §3/§4, 2026-05-19 amendment)."""


@dataclass(frozen=True, slots=True)
class GenerateResult:
    """The outcome of a generation pass."""

    points: list[dict[str, Any]]
    """Surviving, cross-field-valid input dictionaries (one per child)."""

    generated_count: int
    """``len(points)`` — what the study would actually run."""

    skipped_invalid_count: int
    """Raw points dropped by cross-field validation (design §4 step 3)."""

    raw_count: int
    """Points the mode engine produced before filtering."""

    skip_reasons: dict[str, int] = field(default_factory=dict)
    """``{plain_english_reason: count}`` for the pre-submit summary
    (design §9: "352 of 400 grid points were skipped because
    'components must sum to 100%'")."""

    level_warnings: tuple[str, ...] = ()
    """Plain-English warnings raised during level placement — e.g. an
    integer input whose ``grid`` levels collapsed to fewer distinct
    values than requested. Empty when there are none (design §3)."""


def generate(
    descriptor: ToolDescriptor,
    factor_spec: FactorSpec,
    mode: StudyMode,
    sampling_params: SamplingParams,
    *,
    run_cap: int,
    seed: int | None = None,
) -> GenerateResult:
    """Produce the study's input matrix. Pure — no DB/IO.

    Raises :class:`FactorSpecError` (names that aren't tool inputs,
    structurally impossible designs), :class:`ModeNotApplicableError`
    (mode incompatible with the factors), or
    :class:`RunCapExceededError` (survivors exceed ``run_cap``).
    """
    factor_spec.validate_for_mode(mode)

    fields_by_name = {f.name: f for f in descriptor.inputs}
    unknown = sorted(set(factor_spec.factors) - set(fields_by_name))
    if unknown:
        raise FactorSpecError(
            f"The factor specification names inputs the tool doesn't have: {', '.join(unknown)}."
        )

    varied = factor_spec.varied
    constants = factor_spec.constants
    varied_names = list(varied)

    _validate_domain_bounds(varied, fields_by_name)

    raw_rows, level_warnings = _raw_matrix(varied, mode, sampling_params, seed, fields_by_name)
    raw_count = len(raw_rows)

    points: list[dict[str, Any]] = []
    skip_reasons: dict[str, int] = {}
    for row in raw_rows:
        point: dict[str, Any] = dict(constants)
        for name in varied_names:
            point[name] = _coerce(fields_by_name.get(name), row[name])
        # Apply the tool's §16.16 rules to the generated point: set_value
        # forces are written in, formula-driven bounds and require/validity
        # rules (plus §F30 cross-field constraints and sum-checks) are
        # enforced. A violating point is dropped with a plain-English
        # reason; a feasible one is stored with its forces applied.
        point, errors = forms.resolve_for_generation(descriptor, point)
        if errors:
            reason = errors[0].message
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            continue
        points.append(point)

    generated = len(points)
    if generated > run_cap:
        raise RunCapExceededError(generated, run_cap)

    return GenerateResult(
        points=points,
        generated_count=generated,
        skipped_invalid_count=raw_count - generated,
        raw_count=raw_count,
        skip_reasons=skip_reasons,
        level_warnings=tuple(level_warnings),
    )


# --- domain-bounds validation (design §3, bounds rule) -------------------


def _field_bounds(field_def: InputField | None) -> tuple[float | None, float | None]:
    """The input's declared ``(minimum, maximum)`` — either side may be ``None``."""
    if field_def is None:
        return None, None
    return field_def.minimum, field_def.maximum


def _fmt_bounds(lo: float | None, hi: float | None) -> str:
    """Plain-English rendering of a possibly one-sided ``[lo, hi]`` limit."""
    if lo is not None and hi is not None:
        return f"between {lo:g} and {hi:g}"
    if lo is not None:
        return f"at or above {lo:g}"
    return f"at or below {hi:g}"


def _domain_values(vary: Any) -> list[float]:
    """The numeric domain endpoints that must sit within the input's bounds.

    Returns the values a mode would actually try to reach: a range's
    ``min``/``max``, every explicit list value, or a uniform/log-uniform
    distribution's ``low``/``high``. A **normal** distribution is exempt —
    it is *truncated* to the input's bounds at sample time
    (:func:`_inverse_cdf`), so it can never step outside them.
    """
    if isinstance(vary, RangeVary):
        return [vary.min, vary.max]
    if isinstance(vary, ListVary):
        return list(vary.values)
    if isinstance(vary, DistributionVary) and vary.dist in ("uniform", "loguniform"):
        return [_req(vary.low), _req(vary.high)]
    return []


def _validate_domain_bounds(
    varied: dict[str, Factor], fields_by_name: dict[str, InputField]
) -> None:
    """Reject a varied numeric domain that steps outside the input's bounds.

    A range / value-list / uniform / log-uniform domain wider than the
    tool input's declared ``[minimum, maximum]`` would otherwise be
    silently clamped point-by-point by :func:`_coerce`, piling samples
    onto the limit and distorting the distribution (the "LHS points all
    at one edge" symptom). We surface it as a plain-English
    :class:`FactorSpecError` the user can act on instead. Inputs with no
    declared bound impose no constraint; normal distributions are
    truncated at sample time and so are never rejected here.
    """
    for name, f in varied.items():
        field_def = fields_by_name.get(name)
        lo, hi = _field_bounds(field_def)
        if lo is None and hi is None:
            continue
        for v in _domain_values(f.vary):
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                label = field_def.label if field_def is not None else name
                raise FactorSpecError(
                    f"'{label}' is varied outside the values the tool allows "
                    f"({_fmt_bounds(lo, hi)}). Bring the range in so every "
                    f"point stays within the input's limits."
                )


# --- per-mode raw-matrix engines -----------------------------------------


def _raw_matrix(
    varied: dict[str, Factor],
    mode: StudyMode,
    sp: SamplingParams,
    seed: int | None,
    fields_by_name: dict[str, InputField],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Dispatch to the mode engine; returns ``(rows, level_warnings)``."""
    if mode == "table":
        return _table_rows(varied, sp), []
    if mode == "list":
        return _enumerated(varied, sp, fields_by_name, ranges_as_levels=False)
    if mode == "grid":
        return _enumerated(varied, sp, fields_by_name, ranges_as_levels=True)
    if mode in ("frac_factorial", "plackett_burman"):
        return _screening(varied, mode, sp), []
    if mode in ("ccd", "box_behnken"):
        return _rsm(varied, mode, sp), []
    if mode in ("lhs", "sobol", "random"):
        return _space_filling(varied, mode, sp, seed, fields_by_name), []
    raise StudyGenerationError(f"Unsupported sampling mode {mode!r}.")  # pragma: no cover


def _table_rows(varied: dict[str, Factor], sp: SamplingParams) -> list[dict[str, Any]]:
    """The user-authored run matrix (``table`` mode), validated.

    Rows live in ``sampling_params.runs`` and carry only the varied
    columns (constants are merged by :func:`generate`). Every row must
    provide exactly the varied columns — no missing or unexpected keys
    (a plain-English ``FactorSpecError`` otherwise, per F30).
    """
    rows = sp.runs or []
    if not rows:
        raise FactorSpecError("A run table needs at least one run.")
    expected = set(varied)
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        keys = set(row)
        missing = expected - keys
        if missing:
            raise FactorSpecError(f"Run {idx} is missing a value for {', '.join(sorted(missing))}.")
        extra = keys - expected
        if extra:
            raise FactorSpecError(
                f"Run {idx} has an unexpected column: {', '.join(sorted(extra))}."
            )
        out.append({k: row[k] for k in varied})
    return out


def _placed(lo: float, hi: float, n: int, spacing: GridSpacing) -> list[float]:
    """``n`` points across ``[lo, hi]`` placed by the ``spacing`` warp."""
    g = _WARP[spacing]
    span = hi - lo
    return [lo + g(i / (n - 1)) * span for i in range(n)]


def _range_levels(
    name: str, vary: RangeVary, sp: SamplingParams, field_def: InputField | None
) -> tuple[list[Any], str | None]:
    """Placed levels for a ``grid`` numeric range.

    Integer inputs round each placed level and de-duplicate, returning
    a plain-English warning when distinct levels fall below the
    requested count (design §3, 2026-05-19 amendment).
    """
    n = vary.levels if vary.levels is not None else sp.points_per_factor
    placed = _placed(vary.min, vary.max, n, vary.spacing)
    if field_def is None or field_def.type != "integer":
        return placed, None
    seen: list[Any] = []
    for v in placed:
        iv = snap_int(v)
        if iv not in seen:
            seen.append(iv)
    if len(seen) < n:
        return seen, (
            f"'{name}': {n} levels requested but only {len(seen)} distinct "
            f"integer values exist between {vary.min:g} and {vary.max:g}; "
            f"running {len(seen)}."
        )
    return seen, None


def _levels(
    name: str,
    f: Factor,
    sp: SamplingParams,
    field_def: InputField | None,
    *,
    ranges_as_levels: bool,
) -> tuple[list[Any], str | None]:
    """Explicit level set for an enumerable (list / grid) factor.

    Returns the levels and an optional plain-English warning (see
    :func:`_range_levels`).
    """
    if f.type == "bool":
        return [False, True], None
    vary = f.vary
    if isinstance(vary, ListVary):
        return list(vary.values), None
    if isinstance(vary, SubsetVary):
        return list(vary.options), None
    if isinstance(vary, FileListVary):
        return list(vary.cas_hashes), None
    if isinstance(vary, RangeVary) and ranges_as_levels:
        return _range_levels(name, vary, sp, field_def)
    raise FactorSpecError(  # pragma: no cover - guarded by validate_for_mode
        f"'{name}' cannot be enumerated for this mode."
    )


def _enumerated(
    varied: dict[str, Factor],
    sp: SamplingParams,
    fields_by_name: dict[str, InputField],
    *,
    ranges_as_levels: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Cross-product of each factor's level set (``list`` / ``grid``)."""
    names = list(varied)
    level_sets: list[list[Any]] = []
    warnings: list[str] = []
    for nm in names:
        levels, warn = _levels(
            nm, varied[nm], sp, fields_by_name.get(nm), ranges_as_levels=ranges_as_levels
        )
        level_sets.append(levels)
        if warn is not None:
            warnings.append(warn)
    rows = [dict(zip(names, combo, strict=True)) for combo in itertools.product(*level_sets)]
    return rows, warnings


def _ranges(varied: dict[str, Factor]) -> tuple[list[str], list[RangeVary]]:
    names = list(varied)
    spans: list[RangeVary] = []
    for n in names:
        v = varied[n].vary
        if not isinstance(v, RangeVary):  # pragma: no cover - guarded upstream
            raise FactorSpecError(f"'{n}' must be a numeric range for this mode.")
        spans.append(v)
    return names, spans


def _coded_to_real(coded: float, span: RangeVary) -> float:
    """Map a coded design level (centre 0, faces -/+1, axial -/+alpha) to real."""
    centre = (span.min + span.max) / 2.0
    half = (span.max - span.min) / 2.0
    return centre + coded * half


def _design(build: Any, *, not_possible: str) -> Any:
    """Run a pyDOE3 builder, translating its errors to plain English.

    pyDOE3 raises bare ``ValueError`` (e.g. "design not possible") for
    factor/parameter combinations that have no constructible design.
    F30: the user must never see that — surface a plain-English reason
    they can act on instead.
    """
    try:
        return np.asarray(build(), dtype=float)
    except ValueError as exc:
        raise FactorSpecError(not_possible) from exc


def _screening(
    varied: dict[str, Factor], mode: StudyMode, sp: SamplingParams
) -> list[dict[str, Any]]:
    names, spans = _ranges(varied)
    n = len(names)
    if mode == "plackett_burman":
        matrix = _design(
            lambda: pyDOE3.pbdesign(n),
            not_possible=(
                f"A Plackett-Burman design isn't constructible for {n} "
                f"factors. Pick another screening or grid mode."
            ),
        )[:, :n]
    elif sp.frac_gen is not None:
        matrix = _design(
            lambda: pyDOE3.fracfact(sp.frac_gen),
            not_possible=(
                "That fractional-factorial generator string isn't valid. "
                "Use the form 'a b ab c' (one term per factor)."
            ),
        )
        if matrix.shape[1] != n:
            raise FactorSpecError(
                f"The fractional-factorial generator defines "
                f"{matrix.shape[1]} factors but {n} are varied."
            )
    else:
        res = sp.frac_resolution
        matrix = _design(
            lambda: pyDOE3.fracfact_by_res(n, res),
            not_possible=(
                f"A resolution-{res} fractional-factorial design isn't "
                f"possible with {n} factors. Lower the resolution, add "
                f"factors, or choose another mode."
            ),
        )
    return _rows_from_coded(matrix, names, spans)


def _rsm(varied: dict[str, Factor], mode: StudyMode, sp: SamplingParams) -> list[dict[str, Any]]:
    names, spans = _ranges(varied)
    n = len(names)
    if mode == "box_behnken":
        if n < _BOX_BEHNKEN_MIN_FACTORS:
            raise FactorSpecError(
                f"Box-Behnken designs need at least "
                f"{_BOX_BEHNKEN_MIN_FACTORS} varied numeric factors; "
                f"{n} are varied."
            )
        matrix = _design(
            lambda: pyDOE3.bbdesign(n, center=sp.bb_center),
            not_possible=(
                f"A Box-Behnken design isn't constructible for {n} "
                f"factors. Try a central-composite or grid mode."
            ),
        )
    else:  # ccd
        matrix = _design(
            lambda: pyDOE3.ccdesign(n, center=sp.ccd_center, alpha=sp.ccd_alpha, face=sp.ccd_face),
            not_possible=(
                f"A central-composite design isn't constructible for {n} "
                f"factors with these settings."
            ),
        )
    return _rows_from_coded(matrix, names, spans)


def _rows_from_coded(
    matrix: Any, names: Sequence[str], spans: Sequence[RangeVary]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for coded_row in matrix.tolist():
        rows.append({name: _coded_to_real(coded_row[i], spans[i]) for i, name in enumerate(names)})
    return rows


def _space_filling(
    varied: dict[str, Factor],
    mode: StudyMode,
    sp: SamplingParams,
    seed: int | None,
    fields_by_name: dict[str, InputField],
) -> list[dict[str, Any]]:
    names = list(varied)
    d = len(names)
    rng = np.random.default_rng(seed)
    if mode == "lhs":
        unit = qmc.LatinHypercube(d=d, rng=rng).random(sp.n_samples)
    elif mode == "sobol":
        with warnings.catch_warnings():
            # Sobol is a power-of-2 sequence; an arbitrary n is a
            # deliberate, documented trade-off, not a bug to surface.
            warnings.simplefilter("ignore")
            unit = qmc.Sobol(d=d, rng=rng).random(sp.n_samples)
    else:  # random
        unit = rng.random((sp.n_samples, d))

    rows: list[dict[str, Any]] = []
    for u_row in unit.tolist():
        row: dict[str, Any] = {}
        for i, name in enumerate(names):
            row[name] = _scale_unit(u_row[i], varied[name], fields_by_name.get(name))
        rows.append(row)
    return rows


def _scale_unit(u: float, f: Factor, field_def: InputField | None) -> Any:
    """Map a unit-cube coordinate ``u`` ∈ [0, 1) to a factor value."""
    if f.type == "bool":
        return u >= _BOOL_SPLIT
    vary = f.vary
    if isinstance(vary, RangeVary):
        return vary.min + u * (vary.max - vary.min)
    if isinstance(vary, SubsetVary):
        return vary.options[_bin_index(u, len(vary.options))]
    if isinstance(vary, ListVary):
        return vary.values[_bin_index(u, len(vary.values))]
    if isinstance(vary, DistributionVary):
        return _inverse_cdf(u, vary, field_def)
    raise FactorSpecError(  # pragma: no cover - guarded by validate_for_mode
        "File factors cannot be space-filling sampled."
    )


def _bin_index(u: float, k: int) -> int:
    return min(int(u * k), k - 1)


def _inverse_cdf(u: float, d: DistributionVary, field_def: InputField | None) -> float:
    """Inverse-CDF sample a distribution at quantile ``u``.

    A **normal** distribution is *truncated* to the input's declared
    ``[minimum, maximum]`` (via :func:`scipy.stats.truncnorm`) so its
    unbounded tails cannot land outside the input's limits and pile up
    on a bound; an input with no declared bound reduces truncnorm to a
    plain normal.
    """
    if d.dist == "uniform":
        low, high = _req(d.low), _req(d.high)
        return low + u * (high - low)
    if d.dist == "loguniform":
        low, high = _req(d.low), _req(d.high)
        return math.exp(math.log(low) + u * (math.log(high) - math.log(low)))
    mean, std = _req(d.mean), _req(d.std)
    lo, hi = _field_bounds(field_def)
    a = -np.inf if lo is None else (lo - mean) / std
    b = np.inf if hi is None else (hi - mean) / std
    return float(truncnorm.ppf(u, a, b, loc=mean, scale=std))


# --- numeric coercion to the descriptor's declared type ------------------


def _coerce(spec: InputField | None, value: Any) -> Any:
    """Coerce a generated value to the input's declared type/bounds.

    Integer inputs are rounded; numeric inputs are clamped into the
    descriptor's declared ``[minimum, maximum]`` so a coded/space-
    filling point can't land just outside a bound and fail per-field
    validation downstream. Non-numeric values pass through untouched.
    """
    if (
        spec is None
        or value is None
        or not isinstance(value, (int, float))
        or isinstance(value, bool)
    ):
        return value
    v = float(value)
    if spec.minimum is not None:
        v = max(v, spec.minimum)
    if spec.maximum is not None:
        v = min(v, spec.maximum)
    if spec.type == "integer":
        return snap_int(v, spec.minimum, spec.maximum)
    # A float input is snapped onto its declared step for the same reason an
    # integer is rounded: the tool has only the resolution it declares, so a
    # sampler that emits more digits is inventing precision the run never had.
    return snap_precision(v, spec.precision, spec.minimum, spec.maximum)


__all__ = ["GenerateResult", "SamplingParams", "generate"]
