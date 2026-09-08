"""The ``random`` baseline strategy (ADR-0009 §5.3).

Uniform random sampling over the factor box. Useful as a control
(verifies the loop + instrumentation) and as a cheap space-filler. It is
space-filling on its own, so it needs no LHS seed batch.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np

from matofx.studies.optimisation.strategies.base import (
    BaseStrategy,
    Candidate,
    Strategy,
    StrategyContext,
    StrategyState,
)


class RandomStrategy(BaseStrategy):
    """Uniform random search over the factor box."""

    objective_shapes: ClassVar[frozenset[str]] = frozenset({"single", "multi"})
    supports_async_tell: ClassVar[bool] = True
    needs_seed: ClassVar[bool] = False
    name: ClassVar[str] = "random"

    def bind(self, context: StrategyContext, state: StrategyState | None) -> Strategy:
        """Seed the RNG from the study's reproducibility seed (§3.2)."""
        super().bind(context, state)
        self._rng = np.random.default_rng(context.random_seed)
        # Advance the RNG past points already drawn (rehydrate, build Phase 3).
        for _ in self._history:
            self._sample()
        return self

    def ask(self, n: int) -> list[Candidate]:
        """Draw ``n`` independent uniform points."""
        return [self._sample() for _ in range(n)]

    def _sample(self) -> Candidate:
        cand: Candidate = {}
        for f in self._ctx.factors:
            if f.kind == "categorical":
                cand[f.name] = str(self._rng.choice(list(f.choices)))
                continue
            assert f.low is not None and f.high is not None  # numeric  # noqa: S101, PT018
            if f.kind == "float":
                cand[f.name] = f.snap_float(self._rng.uniform(f.low, f.high))
            else:
                cand[f.name] = int(self._rng.integers(int(f.low), int(f.high) + 1))
        return cand
