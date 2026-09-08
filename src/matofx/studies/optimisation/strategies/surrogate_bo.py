"""The ``bo`` strategy — MATOFX-surrogate Bayesian optimisation (ADR-0009 amendment 2026-06-08).

Single-objective adaptive optimisation built on **MATOFX's own surrogate
engine** (not a third-party library). Each ``ask``:

1. fit a surrogate (``fit_surrogate``, kind from the config) on the runs
   told so far — this is the same model the study keeps, can inspect, and
   can publish;
2. draw a pool of candidate points across the factor box;
3. score them with the chosen **acquisition mode** (optimise / balanced /
   explore) from the surrogate's predicted mean + uncertainty;
4. return the top ``n`` distinct points to run next.

The surrogate engine is numeric, so factors are worked in an encoded
vector — ``float`` direct, ``int`` drawn **on the integer lattice**,
``categorical`` as an ordinal index into its choice list — and decoded back
into real candidates at the boundary (the same encoding the multi-objective
strategies use). Because the candidate pool is sampled on the lattice, the
acquisition argmax is always a realisable design; ``ask`` returns the top
**distinct** points so a small integer space doesn't spend batch slots on
identical proposals (ADR-0009 amendment 2026-06-15, Phase C). Proper
non-ordinal categorical handling + kernel choice / best-of are the later
MOP/CoP work; ``surrogate_kind`` reserves the hook.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from matofx.studies._intsnap import snap_int
from matofx.studies.optimisation.acquisition import acquisition_scores
from matofx.studies.optimisation.strategies.base import (
    BaseStrategy,
    Candidate,
    FactorSpec,
    Strategy,
    StrategyContext,
    StrategyState,
)
from matofx.studies.optimisation.sumcheck import project_sum_groups

_MIN_FIT = 2
"""Below this many told points there's nothing to fit — fall back to random."""


class SurrogateBOStrategy(BaseStrategy):
    """Surrogate-driven single-objective optimiser (the ``bo`` strategy)."""

    objective_shapes: ClassVar[frozenset[str]] = frozenset({"single"})
    supports_async_tell: ClassVar[bool] = True
    needs_seed: ClassVar[bool] = True
    name: ClassVar[str] = "bo"

    def bind(self, context: StrategyContext, state: StrategyState | None) -> Strategy:
        """Seed the RNG; the surrogate is refit from history on each ask."""
        super().bind(context, state)
        self._rng = np.random.default_rng(context.random_seed)
        return self

    def ask(self, n: int) -> list[Candidate]:
        """Propose the next ``n`` runs by maximising the acquisition (§ amendment)."""
        usable = [(x, y) for (x, y, ok) in self._history if ok and self._objective.name in y]
        if len(usable) < _MIN_FIT:
            return [self._random_candidate() for _ in range(n)]

        from matofx.studies.surrogate import fit_surrogate  # noqa: PLC0415

        objective = self._objective
        factor_columns = {
            f.name: [self._encode(h[0].get(f.name), f) for h in usable] for f in self._ctx.factors
        }
        response = [float(h[1][objective.name]) for h in usable]
        surrogate = fit_surrogate(factor_columns, response, kind=self._ctx.surrogate_kind)  # type: ignore[arg-type]

        pool = [self._random_encoded() for _ in range(self._ctx.candidate_pool)]
        means, sigmas = surrogate.predict(pool)
        if not means:
            # The surrogate couldn't fit (too few complete rows) — explore the
            # pool in order, still returning distinct designs.
            return self._top_distinct(pool, list(range(len(pool))), n)
        scores = acquisition_scores(means, sigmas, goal=objective.goal, mode=self._ctx.acquisition)
        order = sorted(range(len(pool)), key=lambda i: scores[i], reverse=True)
        return self._top_distinct(pool, order, n)

    def _top_distinct(
        self, pool: list[dict[str, float]], order: list[int], n: int
    ) -> list[Candidate]:
        """Decode pool points in score order, keeping the first ``n`` distinct.

        Two pool points can round to the same integer / categorical design, and
        proposing it twice only wastes a batch slot.
        """
        out: list[Candidate] = []
        seen: set[str] = set()
        for i in order:
            cand = self._decode(pool[i])
            key = repr(sorted(cand.items()))
            if key in seen:
                continue
            seen.add(key)
            out.append(cand)
            if len(out) >= n:
                break
        return out

    def rationale(self, c: Candidate) -> dict[str, Any]:  # noqa: ARG002 — Protocol arg
        """Tag the proposal with the acquisition mode + data size."""
        return {
            "proposed_by": self.name,
            "acquisition": self._ctx.acquisition,
            "n_points": len(self._history),
        }

    # --- encode / decode -------------------------------------------------

    def _encode(self, value: Any, f: FactorSpec) -> float:
        if f.kind == "categorical":
            try:
                return float(f.choices.index(str(value)))
            except ValueError:
                return 0.0
        return float(value)

    def _random_encoded(self) -> dict[str, float]:
        point: dict[str, float] = {}
        for f in self._ctx.factors:
            if f.kind == "categorical":
                point[f.name] = float(self._rng.integers(0, max(len(f.choices), 1)))
            elif f.kind == "int":
                point[f.name] = float(self._rng.integers(int(f.low), int(f.high) + 1))  # type: ignore[arg-type]
            else:
                point[f.name] = float(self._rng.uniform(f.low, f.high))  # type: ignore[arg-type]
        return point

    def _decode(self, point: dict[str, float]) -> Candidate:
        cand: Candidate = {}
        for f in self._ctx.factors:
            v = float(point[f.name])
            if f.kind == "categorical":
                idx = max(0, min(len(f.choices) - 1, round(v)))
                cand[f.name] = f.choices[idx]
            elif f.kind == "int":
                cand[f.name] = snap_int(v, f.low, f.high)
            else:
                cand[f.name] = f.snap_float(v)
        if self._ctx.sum_groups:
            cand = project_sum_groups(cand, self._ctx.sum_groups)
        return cand

    def _random_candidate(self) -> Candidate:
        return self._decode(self._random_encoded())
