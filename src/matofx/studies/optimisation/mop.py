"""MOP / CoP — Metamodel of Optimal Prognosis + Coefficient of Prognosis (ADR-0009 §5.7).

optiSLang's signature capability, built entirely on the surrogate family
(ADR-0008) — no new dependency. Three ideas, all pure functions over the
study's accumulated ``(factors, response)`` data:

1. **Auto-best-metamodel** — fit every available surrogate kind and keep
   the one with the best cross-validated **Q²** (``SurrogateQuality.q2``).
   This is the "optimal prognosis" choice and the concrete form of the
   amendment's reserved "use best model" hook.
2. **Coefficient of Prognosis (CoP) per factor** — the share of response
   variance the metamodel explains, attributed to each input: the fitted
   metamodel's Sobol **total-effect** index (``SobolIndex.st``) scaled by
   the model's overall Q². ``CoP_i = ST_i · max(0, Q²)``.
3. **Sensitivity-driven variable reduction** — factors whose CoP falls
   below ``cop_cutoff`` carry almost no explained variance, so they can be
   frozen at their nominal value and dropped from the active search space
   (fewer dimensions → faster convergence). :meth:`MopResult.prunable`
   lists them; the caller (or the user, per-factor) decides.

This module is run-mode-agnostic — it just analyses data. Where the
result plugs into the loop (auto-kind for ``bo``; the frozen-factor search
reduction) is the integration layer's concern.
"""

from __future__ import annotations

from dataclasses import dataclass

from matofx.studies.surrogate import SurrogateKind, fit_surrogate, sobol_indices

ALL_SURROGATE_KINDS: tuple[SurrogateKind, ...] = ("gp", "gp_matern", "poly", "gbm")
"""Every metamodel kind the auto-best selection fits + ranks by Q²."""

DEFAULT_COP_CUTOFF = 0.01
"""A factor explaining <1% of response variance is a variable-reduction
candidate (§5.7, operator-tunable)."""


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass(frozen=True, slots=True)
class FactorCoP:
    """One factor's Coefficient of Prognosis + the raw Sobol total effect."""

    factor: str
    cop: float
    """``ST · max(0, Q²)`` — explained-variance share attributed to this factor, in ``[0, 1]``."""
    total_effect: float
    """The metamodel's Sobol total-effect index ``ST`` for this factor."""


@dataclass(frozen=True, slots=True)
class MopResult:
    """The outcome of an auto-best-metamodel + CoP analysis (§5.7)."""

    best_kind: SurrogateKind | None
    """The surrogate kind with the best cross-validated Q², or ``None`` if none fit."""
    q2: float | None
    """The best metamodel's cross-validated Q² (the prognosis quality)."""
    cop: tuple[FactorCoP, ...]
    """Per-factor CoP, highest first."""
    kind_q2: dict[str, float | None]
    """Per-kind Q² (``None`` when that kind couldn't fit) — the selection trail."""
    message: str | None = None
    """Set (with ``best_kind=None``) when no kind could be fit on the data."""

    def prunable(self, cutoff: float = DEFAULT_COP_CUTOFF) -> list[str]:
        """Factors whose CoP is below ``cutoff`` — the variable-reduction set (§5.7)."""
        return [c.factor for c in self.cop if c.cop < cutoff]


def fit_best_metamodel(
    factor_columns: dict[str, list[float | None]],
    response: list[float | None],
    *,
    kinds: tuple[SurrogateKind, ...] = ALL_SURROGATE_KINDS,
) -> MopResult:
    """Fit every ``kinds`` metamodel, keep the best Q², and attribute CoP (§5.7).

    Returns a :class:`MopResult`. When no kind fits (too few runs, no
    varied factor), ``best_kind`` is ``None`` and ``message`` explains why.
    """
    kind_q2: dict[str, float | None] = {}
    fits: list[tuple[SurrogateKind, object, float]] = []
    for kind in kinds:
        surrogate = fit_surrogate(factor_columns, response, kind=kind)
        q2 = surrogate.quality.q2 if surrogate.fitted else None
        kind_q2[kind] = q2
        if surrogate.fitted and q2 is not None:
            fits.append((kind, surrogate, q2))

    if not fits:
        return MopResult(
            best_kind=None,
            q2=None,
            cop=(),
            kind_q2=kind_q2,
            message="No metamodel could be fit — need more complete runs and a varied factor.",
        )

    best_kind, best_surrogate, best_q2 = max(fits, key=lambda t: t[2])
    weight = _clamp01(best_q2)  # a useless (≤0 Q²) model attributes no CoP
    sobol = sobol_indices(best_surrogate) or []  # type: ignore[arg-type]
    cop = tuple(
        sorted(
            (FactorCoP(si.factor, _clamp01(si.st * weight), si.st) for si in sobol),
            key=lambda c: c.cop,
            reverse=True,
        )
    )
    return MopResult(best_kind=best_kind, q2=best_q2, cop=cop, kind_q2=kind_q2)
