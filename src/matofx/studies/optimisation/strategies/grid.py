"""The ``grid`` baseline strategy (ADR-0009 §5.3).

An exhaustive coarse full-factorial sweep: numeric factors are sampled at
``_DEFAULT_LEVELS`` evenly-spaced levels (fewer for a small integer
range), categorical factors at every choice. ``ask`` walks the
cartesian product in order and returns ``[]`` once exhausted — the
runtime reads the empty proposal as ``grid_exhausted`` and stops. Space-
filling on its own, so no LHS seed batch.
"""

from __future__ import annotations

from itertools import product
from typing import Any, ClassVar

import numpy as np

from matofx.studies._intsnap import snap_int
from matofx.studies.optimisation.strategies.base import (
    BaseStrategy,
    Candidate,
    FactorSpec,
    Strategy,
    StrategyContext,
    StrategyState,
)

_DEFAULT_LEVELS = 5
"""Evenly-spaced levels per numeric factor (the "coarse" in coarse grid)."""

_MAX_GRID_POINTS = 100_000
"""Hard ceiling on the materialised product — a guard against a factor
explosion building an unbounded list (the ``max_runs`` budget caps how
many are actually run)."""


class GridStrategy(BaseStrategy):
    """Exhaustive coarse full-factorial enumeration."""

    objective_shapes: ClassVar[frozenset[str]] = frozenset({"single", "multi"})
    supports_async_tell: ClassVar[bool] = True
    needs_seed: ClassVar[bool] = False
    name: ClassVar[str] = "grid"

    def bind(self, context: StrategyContext, state: StrategyState | None) -> Strategy:
        """Materialise the grid and restore the cursor (rehydrate)."""
        super().bind(context, state)
        self._points = self._build_grid()
        self._cursor = int(state.get("cursor", 0)) if state else 0
        return self

    def ask(self, n: int) -> list[Candidate]:
        """Return the next ``n`` un-visited grid points (fewer near the end)."""
        out = self._points[self._cursor : self._cursor + n]
        self._cursor += len(out)
        return [dict(p) for p in out]

    def dump(self) -> StrategyState:
        """History plus the grid cursor (so a resume doesn't re-run points)."""
        state = super().dump()
        state["cursor"] = self._cursor
        return state

    def _build_grid(self) -> list[Candidate]:
        axes: list[list[Any]] = [self._axis(f) for f in self._ctx.factors]
        total = 1
        for axis in axes:
            total *= max(len(axis), 1)
        if total > _MAX_GRID_POINTS:
            raise ValueError(
                f"A coarse grid over these factors is {total} points "
                f"(> {_MAX_GRID_POINTS}). Reduce factors or use a sampling strategy."
            )
        names = [f.name for f in self._ctx.factors]
        return [dict(zip(names, combo, strict=True)) for combo in product(*axes)]

    def _axis(self, f: FactorSpec) -> list[Any]:
        if f.kind == "categorical":
            return list(f.choices)
        assert f.low is not None and f.high is not None  # numeric  # noqa: S101, PT018
        if f.kind == "int":
            lo, hi = int(f.low), int(f.high)
            span = hi - lo + 1
            levels = min(_DEFAULT_LEVELS, span)
            return sorted({snap_int(v) for v in np.linspace(lo, hi, levels)})
        # De-duplicated: a coarse step can collapse neighbouring levels, and a
        # grid that visits the same design twice wastes half a budget.
        return sorted({f.snap_float(v) for v in np.linspace(f.low, f.high, _DEFAULT_LEVELS)})
