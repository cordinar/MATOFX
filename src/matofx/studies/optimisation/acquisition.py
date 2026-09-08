"""Acquisition scoring for the surrogate-driven optimiser (ADR-0009 amendment 2026-06-08).

Given a surrogate's predicted mean (mu) and uncertainty (sigma) at a set
of candidate points, score each by how desirable it is to run **next** —
the higher the score, the better. One knob (the exploration weight kappa)
gives the three user-facing modes:

- **optimise** (kappa=0) — exploit: go where the surrogate predicts the
  best value (lowest for a minimise goal, highest for maximise);
- **balanced** (kappa~2) — trade predicted value off against uncertainty
  (an upper/lower-confidence-bound acquisition);
- **explore** (sigma only) — improve the model: go where it is least
  sure, ignoring the predicted value (active learning).

This is deliberately the simplest acquisition that maps the three modes
to one dial; Expected-Improvement / per-mode tuning can layer on later.
"""

from __future__ import annotations

from collections.abc import Sequence

ACQUISITION_MODES: frozenset[str] = frozenset({"optimise", "balanced", "explore"})
"""The user-selectable next-run strategies (the amendment's three buttons)."""

_BALANCED_KAPPA = 2.0
"""Exploration weight for ``balanced`` (mean +/- kappa*sigma)."""


def acquisition_scores(
    means: Sequence[float],
    sigmas: Sequence[float] | None,
    *,
    goal: str,
    mode: str,
) -> list[float]:
    """Score each candidate (higher = run next) from the surrogate's mean/std.

    ``goal`` is the objective's ``minimise`` / ``maximise`` direction;
    ``mode`` is one of :data:`ACQUISITION_MODES`. A ``None`` ``sigmas``
    (a non-GP surrogate with no uncertainty) collapses ``balanced`` /
    ``explore`` to pure exploitation.
    """
    sig = list(sigmas) if sigmas is not None else [0.0] * len(means)
    if mode == "explore":
        return [float(s or 0.0) for s in sig]
    sign = -1.0 if goal == "minimise" else 1.0
    kappa = 0.0 if mode == "optimise" else _BALANCED_KAPPA
    return [sign * float(m) + kappa * float(s or 0.0) for m, s in zip(means, sig, strict=True)]
