"""Multi-objective evolutionary strategies — ``nsga2`` / ``moea_d`` (ADR-0009 §5).

These wrap pymoo's generational ask/tell over an encoded real vector:

- Each factor maps to one bounded real dimension — ``float`` directly,
  ``int`` and ``categorical`` (an index into its choice list) kept on their
  lattice by a pymoo ``Repair`` so the population only holds realisable designs
  (Phase C); decode snaps once more as a belt (round half away from zero).
- ``ask`` draws a whole generation (sized to the study's ``batch_size``)
  and returns the decoded candidates; ``tell`` fills the objective matrix
  ``F`` (signed so pymoo always minimises) and the constraint matrix ``G``
  (pymoo's ``g ≤ 0`` convention) for the generation and advances it.
  Candidates the selection-time filter dropped get a penalty so the
  generation stays whole.

Crucially, the Pareto front / hypervolume are read from an **external
archive** rebuilt from the told history — *not* from pymoo's internal
population — so crash recovery and rehydrate (Phase 3) work even though
pymoo's generational state itself doesn't round-trip: a resumed study
keeps every completed result (and its Pareto front) and simply starts a
fresh generation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np

from matofx.studies._intsnap import snap_int
from matofx.studies.optimisation.strategies.base import (
    BaseStrategy,
    Candidate,
    FactorSpec,
    Objective,
    ResponseVector,
    Strategy,
    StrategyContext,
    StrategyState,
)
from matofx.studies.optimisation.sumcheck import project_sum_groups

if TYPE_CHECKING:
    from collections.abc import Sequence

_PENALTY = 1.0e12
"""Objective/constraint value for a generation slot the selection filter
dropped — keeps pymoo's population whole without rewarding the gap."""


class PymooStrategy(BaseStrategy):
    """Generational multi-objective base over pymoo ask/tell."""

    objective_shapes: ClassVar[frozenset[str]] = frozenset({"multi"})
    supports_async_tell: ClassVar[bool] = False
    needs_seed: ClassVar[bool] = False
    name: ClassVar[str] = "pymoo"

    def bind(self, context: StrategyContext, state: StrategyState | None) -> Strategy:
        """Build the encoded problem + pymoo algorithm; rebuild the archive."""
        super().bind(context, state)
        self._archive: list[tuple[Candidate, ResponseVector]] = [
            (x, y) for (x, y, feasible) in self._history if feasible
        ]
        self._xl, self._xu = self._bounds()
        # Keep the population on the integer / categorical lattice (Phase C):
        # a continuous EA's offspring are snapped after variation so it searches
        # only realisable designs, not a fictitious continuous interior. ``None``
        # when every factor is continuous (the repair would be a no-op).
        self._lattice_repair = _make_lattice_repair(context.factors)
        self._algorithm = self._make_algorithm()
        self._algorithm.setup(
            self._problem(), termination=("n_gen", 10**9), seed=context.random_seed, verbose=False
        )
        self._pending_pop: Any = None
        self._pending_cands: list[Candidate] = []
        return self

    # --- ask / tell ------------------------------------------------------

    def ask(self, n: int) -> list[Candidate]:  # noqa: ARG002 — pymoo sizes its own generation
        """Draw the next generation; return its decoded candidates.

        ``atleast_2d`` normalises the population matrix because the
        steady-state algorithms (MOEA/D) return a single offspring per
        ask after the initial generation, where ``X`` is 1-D.
        """
        pop = self._algorithm.ask()
        self._pending_pop = pop
        rows = np.atleast_2d(pop.get("X"))
        self._pending_cands = [self._decode(row) for row in rows]
        return [dict(c) for c in self._pending_cands]

    def tell(
        self,
        x: Sequence[Candidate],
        y: Sequence[ResponseVector],
        feasible: Sequence[bool],
    ) -> None:
        """Archive the results and advance the pending generation (idempotent)."""
        for cand, responses, feas in zip(x, y, feasible, strict=True):
            if self._record(cand, responses, feas) and feas:
                self._archive.append((dict(cand), dict(responses)))
        if self._pending_pop is not None:
            self._advance_generation(x, y)
            self._pending_pop = None
            self._pending_cands = []

    def best_so_far(self) -> ResponseVector | None:
        """Undefined for multi-objective — the Pareto front is the result."""
        return None

    def pareto_front(self) -> list[ResponseVector]:
        """Non-dominated feasible response vectors from the archive (§16.1)."""
        return _pareto_front(self._archive, self._ctx.objectives)

    # --- internals -------------------------------------------------------

    def _make_algorithm(self) -> Any:
        raise NotImplementedError

    def _repair_kw(self) -> dict[str, Any]:
        """``{"repair": ...}`` when the space has a lattice, else ``{}``.

        pymoo's GA/DE/PSO/NSGA-II/NSGA-III/MOEA-D accept a ``repair`` operator;
        CMA-ES does not (it wraps the ``cma`` package), so that strategy keeps
        relaxation + ``snap_int`` on decode and overrides this to ``{}``.
        """
        return {"repair": self._lattice_repair} if self._lattice_repair is not None else {}

    def _advance_generation(self, x: Sequence[Candidate], y: Sequence[ResponseVector]) -> None:
        from matofx.studies.optimisation.constraints import output_constraint_g  # noqa: PLC0415

        told = {self._signature(c, r): r for c, r in zip(x, y, strict=True)}
        objectives = self._ctx.objectives
        constraint_config = {"constraints": list(self._ctx.output_constraints)}
        n_constr = len(self._ctx.output_constraints)

        f_rows: list[list[float]] = []
        g_rows: list[list[float]] = []
        for cand in self._pending_cands:
            responses = _match(told, cand)
            if responses is None:
                f_rows.append([_PENALTY] * len(objectives))
                g_rows.append([_PENALTY] * n_constr)
            else:
                f_rows.append([_signed(o, responses) for o in objectives])
                g_rows.append(output_constraint_g(responses, constraint_config))

        self._pending_pop.set("F", np.array(f_rows, dtype=float))
        if n_constr:
            self._pending_pop.set("G", np.array(g_rows, dtype=float))
        self._algorithm.tell(infills=self._pending_pop)

    def _problem(self) -> Any:
        from pymoo.core.problem import Problem  # noqa: PLC0415

        n_var = len(self._ctx.factors)
        n_obj = len(self._ctx.objectives)
        n_constr = len(self._ctx.output_constraints)
        xl, xu = self._xl, self._xu

        class _AskTellProblem(Problem):  # type: ignore[misc]
            def __init__(self) -> None:
                super().__init__(n_var=n_var, n_obj=n_obj, n_ieq_constr=n_constr, xl=xl, xu=xu)

            def _evaluate(self, _x: Any, _out: Any, *_a: Any, **_k: Any) -> None:
                # ask/tell drives evaluation externally; never auto-called.
                raise NotImplementedError

        return _AskTellProblem()

    def _bounds(self) -> tuple[np.ndarray, np.ndarray]:
        lows: list[float] = []
        highs: list[float] = []
        for f in self._ctx.factors:
            if f.kind == "categorical":
                lows.append(0.0)
                highs.append(float(max(len(f.choices) - 1, 0)))
            else:
                lows.append(float(f.low))  # type: ignore[arg-type]
                highs.append(float(f.high))  # type: ignore[arg-type]
        return np.array(lows, dtype=float), np.array(highs, dtype=float)

    def _decode(self, row: Any) -> Candidate:
        cand: Candidate = {}
        for value, f in zip(row, self._ctx.factors, strict=True):
            v = float(value)
            if f.kind == "float":
                cand[f.name] = f.snap_float(v)
            elif f.kind == "int":
                cand[f.name] = snap_int(v, f.low, f.high)
            else:
                idx = max(0, min(len(f.choices) - 1, round(v)))
                cand[f.name] = f.choices[idx]
        if self._ctx.sum_groups:
            cand = project_sum_groups(cand, self._ctx.sum_groups)
        return cand


class NSGA2Strategy(PymooStrategy):
    """Non-dominated Sorting GA II — 2-3 objectives (ADR-0009 §5)."""

    name: ClassVar[str] = "nsga2"

    def _make_algorithm(self) -> Any:
        from pymoo.algorithms.moo.nsga2 import NSGA2  # noqa: PLC0415

        return NSGA2(pop_size=max(4, self._ctx.batch_size), **self._repair_kw())


class MOEADStrategy(PymooStrategy):
    """MOEA/D decomposition — many objectives (ADR-0009 §5).

    Uses pymoo's **generational** ``ParallelMOEAD`` (the whole population
    is asked + told each generation) rather than the steady-state
    ``MOEAD`` (one offspring per ask) so it fits the batch ask/tell loop.
    """

    name: ClassVar[str] = "moea_d"

    def _make_algorithm(self) -> Any:
        from pymoo.algorithms.moo.moead import ParallelMOEAD  # noqa: PLC0415

        ref_dirs = _reference_directions(len(self._ctx.objectives), self._ctx)
        return ParallelMOEAD(
            ref_dirs=ref_dirs, n_neighbors=min(15, len(ref_dirs)), **self._repair_kw()
        )


class NSGA3Strategy(PymooStrategy):
    """Reference-direction NSGA-III — many objectives, >3 (ADR-0009 §5).

    Same external generational ask/tell as :class:`NSGA2Strategy`; the
    difference is reference-direction-based survival, which keeps a
    well-spread front as the objective count climbs past three (where
    NSGA-II's crowding distance thins out).
    """

    name: ClassVar[str] = "nsga3"

    def _make_algorithm(self) -> Any:
        from pymoo.algorithms.moo.nsga3 import NSGA3  # noqa: PLC0415

        ref_dirs = _reference_directions(len(self._ctx.objectives), self._ctx)
        return NSGA3(
            ref_dirs=ref_dirs,
            pop_size=max(len(ref_dirs), self._ctx.batch_size),
            **self._repair_kw(),
        )


def _reference_directions(n_obj: int, ctx: StrategyContext) -> Any:
    """Das-Dennis-style reference directions sized to the batch (shared by MOEA/D + NSGA-III)."""
    from pymoo.util.ref_dirs import get_reference_directions  # noqa: PLC0415

    n_points = max(ctx.batch_size, n_obj + 1)
    return get_reference_directions("energy", n_obj, n_points, seed=ctx.random_seed)


# --- module helpers ---------------------------------------------------------


def _make_lattice_repair(factors: Sequence[FactorSpec]) -> Any:
    """Build a pymoo ``Repair`` that snaps the population onto its lattice.

    Int and categorical-index columns are rounded (half away from zero) and
    clamped; float columns pass through. Returns ``None`` when every factor is
    continuous (the repair would be a no-op). Built lazily so importing this
    module doesn't import pymoo.
    """
    if not any(f.kind in ("int", "categorical") for f in factors):
        return None
    from pymoo.core.repair import Repair  # noqa: PLC0415

    specs = tuple(factors)

    class _LatticeRepair(Repair):  # type: ignore[misc]
        def _do(self, _problem: Any, x: Any, **_kwargs: Any) -> Any:
            out = np.array(x, dtype=float)
            for j, f in enumerate(specs):
                if f.kind == "int":
                    lo, hi = float(int(f.low)), float(int(f.high))  # type: ignore[arg-type]
                elif f.kind == "categorical":
                    lo, hi = 0.0, float(max(len(f.choices) - 1, 0))
                else:
                    continue
                col = out[:, j]
                out[:, j] = np.clip(np.sign(col) * np.floor(np.abs(col) + 0.5), lo, hi)
            return out

    return _LatticeRepair()


def _signed(objective: Objective, responses: ResponseVector) -> float:
    """Objective value as a quantity to minimise (maximise → negated)."""
    value = float(responses[objective.name])
    return value if objective.goal == "minimise" else -value


def _match(told: dict[tuple[str, str], ResponseVector], cand: Candidate) -> ResponseVector | None:
    for (sig_x, _sig_y), responses in told.items():
        if sig_x == BaseStrategy._signature(cand, {})[0]:
            return responses
    return None


def _dominates(a: Sequence[float], b: Sequence[float]) -> bool:
    """``a`` Pareto-dominates ``b`` (minimisation): no worse in all, better in one."""
    return all(ai <= bi for ai, bi in zip(a, b, strict=True)) and any(
        ai < bi for ai, bi in zip(a, b, strict=True)
    )


def _pareto_front(
    archive: Sequence[tuple[Candidate, ResponseVector]], objectives: Sequence[Objective]
) -> list[ResponseVector]:
    """Non-dominated feasible response vectors (raw, un-signed)."""
    vectors = [[_signed(o, responses) for o in objectives] for (_, responses) in archive]
    front: list[ResponseVector] = []
    for i, vi in enumerate(vectors):
        if not any(j != i and _dominates(vj, vi) for j, vj in enumerate(vectors)):
            front.append(archive[i][1])
    return front


def hypervolume(
    pareto: Sequence[ResponseVector],
    objectives: Sequence[Objective],
    ref_point: Sequence[float],
) -> float:
    """Hypervolume of a Pareto set vs ``ref_point`` (signed-minimisation space)."""
    if not pareto:
        return 0.0
    from pymoo.indicators.hv import HV  # noqa: PLC0415

    points = np.array([[_signed(o, r) for o in objectives] for r in pareto], dtype=float)
    return float(HV(ref_point=np.array(ref_point, dtype=float))(points))


def reference_point(
    pareto: Sequence[ResponseVector], objectives: Sequence[Objective]
) -> list[float]:
    """A hypervolume reference point — per-objective worst, nudged out 10% (§10)."""
    if not pareto:
        return [1.0 for _ in objectives]
    matrix = np.array([[_signed(o, r) for o in objectives] for r in pareto], dtype=float)
    worst = matrix.max(axis=0)
    spread = np.where(np.abs(worst) > 0, np.abs(worst) * 0.1, 1.0)
    return list(worst + spread)
