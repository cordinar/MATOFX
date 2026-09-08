"""Single-objective classical / evolutionary strategies (ADR-0009 §5, build Phase 9).

``cmaes`` / ``de`` / ``pso`` / ``ga`` are pymoo single-objective
algorithms driven through the **same external generational ask/tell**
wrapper as the multi-objective :class:`PymooStrategy` (``nsga2`` /
``moea_d`` / ``nsga3``) — only the pymoo ``Algorithm`` object differs
(§5.2). The wrapper already does the int/categorical ↔ real encoding, the
output-constraint matrix ``G``, and the selection-filter penalty, so each
strategy here is just a ``_make_algorithm`` plus the single-objective
result semantics (``best_so_far`` from the bookkeeping history, no Pareto
front).

These bootstrap their own first generation (pymoo samples the initial
population in-box), so they take **no external LHS seed** — only ``bo``
does (``config._SELF_SEEDING_STRATEGIES``). They are generational, so
``supports_async_tell=False`` (a generation must complete before the
next), matching the pymoo backend.

Zero new dependency: pymoo (already pulled for the multi-objective EAs)
and, for CMA-ES, the ``cma`` package pymoo vendors in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from matofx.studies.optimisation.strategies.base import BaseStrategy
from matofx.studies.optimisation.strategies.multi import PymooStrategy

if TYPE_CHECKING:
    from matofx.studies.optimisation.strategies.base import ResponseVector


class SingleObjectivePymooStrategy(PymooStrategy):
    """Single-objective base over pymoo's external ask/tell.

    Reuses every encode/decode/generation mechanic from
    :class:`PymooStrategy` and swaps the multi-objective result semantics
    back to the single-objective ones: the best feasible point from the
    bookkeeping history (the optimum *is* the result, not a Pareto front).
    """

    objective_shapes: ClassVar[frozenset[str]] = frozenset({"single"})
    name: ClassVar[str] = "soo_pymoo"

    def best_so_far(self) -> ResponseVector | None:
        """The best feasible point — single-objective optimum (§16.1)."""
        # The multi-objective base returns None; the bookkeeping logic on
        # BaseStrategy (history + "always minimise" scalarisation) is the
        # right answer for a single objective.
        return BaseStrategy.best_so_far(self)

    def pareto_front(self) -> list[ResponseVector]:
        """No Pareto front for a single objective (the result is one point)."""
        return []


class CMAESStrategy(SingleObjectivePymooStrategy):
    """Covariance-Matrix-Adaptation ES — the workhorse when BO stops scaling (§5)."""

    name: ClassVar[str] = "cmaes"

    def _repair_kw(self) -> dict[str, Any]:
        """No ``repair`` operator — CMA-ES (the ``cma`` wrapper) rejects it.

        It keeps relaxation + ``snap_int`` on decode: integer values are still
        emitted, the population just isn't lattice-constrained (the design
        exempts CMA-ES from Phase C).
        """
        return {}

    def _make_algorithm(self) -> Any:
        from pymoo.algorithms.soo.nonconvex.cmaes import CMAES  # noqa: PLC0415

        # CMA-ES sizes its own population from the problem dimension
        # (cma's 4 + 3·ln(n) default); the batch loop submits whatever the
        # generation contains, exactly as it does for the pop_size EAs.
        return CMAES()


class DEStrategy(SingleObjectivePymooStrategy):
    """Differential evolution — robust on multimodal / noisy responses (§5)."""

    name: ClassVar[str] = "de"

    def _make_algorithm(self) -> Any:
        from pymoo.algorithms.soo.nonconvex.de import DE  # noqa: PLC0415

        return DE(pop_size=max(4, self._ctx.batch_size), **self._repair_kw())


class PSOStrategy(SingleObjectivePymooStrategy):
    """Particle-swarm optimisation — familiar global search (§5)."""

    name: ClassVar[str] = "pso"

    def _make_algorithm(self) -> Any:
        from pymoo.algorithms.soo.nonconvex.pso import PSO  # noqa: PLC0415

        return PSO(pop_size=max(4, self._ctx.batch_size), **self._repair_kw())


class GAStrategy(SingleObjectivePymooStrategy):
    """Single-objective genetic algorithm (§5)."""

    name: ClassVar[str] = "ga"

    def _make_algorithm(self) -> Any:
        from pymoo.algorithms.soo.nonconvex.ga import GA  # noqa: PLC0415

        return GA(pop_size=max(4, self._ctx.batch_size), **self._repair_kw())
