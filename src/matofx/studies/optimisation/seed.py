"""Latin-Hypercube initial seed batch (ADR-0009 §11).

Pure BO on <5 points is unstable, so every seeded strategy starts with a
space-filling LHS batch the runtime runs as batch 0 before the strategy
takes over. The seed is scaled into each factor's box and snapped for
integer / categorical factors. ``scipy.stats.qmc.LatinHypercube`` is
already in the stack (the parametric-studies sampler).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from matofx.studies.optimisation.strategies.base import Candidate, FactorSpec


def default_seed_size(dim: int) -> int:
    """The §11 default seed size — ``max(10, 3 * dim)`` (operator-tunable)."""
    return max(10, 3 * dim)


def lhs_seed(
    factors: Sequence[FactorSpec], n: int, *, random_seed: int | None = None
) -> list[Candidate]:
    """Generate ``n`` LHS points over the ``factors`` box (§11).

    Floats scale linearly into ``[low, high]``; integers snap to the
    nearest whole value in range; categoricals map the unit interval onto
    the choice list. Raises ``ValueError`` for an empty factor set (a
    validated study always has ≥1 factor).
    """
    if not factors:
        raise ValueError("Cannot seed a study with no factors.")
    if n < 1:
        return []
    from scipy.stats.qmc import LatinHypercube  # noqa: PLC0415

    sampler = LatinHypercube(d=len(factors), seed=random_seed)
    unit = sampler.random(n)  # (n, d) in [0, 1)
    out: list[Candidate] = []
    for row in unit:
        cand: Candidate = {}
        for u, f in zip(row, factors, strict=True):
            cand[f.name] = _scale(float(u), f)
        out.append(cand)
    return out


def _scale(u: float, f: FactorSpec) -> object:
    """Map a unit-interval draw ``u`` onto factor ``f``'s domain."""
    if f.kind == "categorical":
        idx = min(len(f.choices) - 1, int(u * len(f.choices)))
        return f.choices[idx]
    lo, hi = float(f.low), float(f.high)  # type: ignore[arg-type]
    if f.kind == "int":
        # Spread the unit interval over hi-lo+1 integer cells, clamped.
        value = math.floor(lo + u * (hi - lo + 1))
        return max(int(lo), min(int(hi), value))
    return lo + u * (hi - lo)
