"""Strategy contract + shared types for the optimisation engine (ADR-0009 §4.2/§5).

Every optimisation strategy implements one :class:`Strategy` Protocol so
the outer loop (§4.1) is strategy-agnostic: it ``ask``s for candidates,
runs them, and ``tell``s the strategy the results. The loop never knows
whether it is talking to a random baseline, a grid, or Optuna BO.

Phase 2 ships the three **foundational** strategies (``random`` / ``grid``
/ ``bo_ei``) and a single-objective :class:`BaseStrategy` that handles
the common bookkeeping — history, best-so-far tracking, the internal
"always minimise" scalarisation (a ``maximise`` goal is negated). Multi-
objective Pareto tracking is build Phase 4 (pymoo); ``pareto_front``
returns ``[]`` here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from matofx.studies._intsnap import snap_precision

Candidate = dict[str, Any]
"""A point in factor space — ``{factor_name: value}`` (float / int / str)."""

ResponseVector = dict[str, float]
"""A candidate's measured responses — ``{response_name: numeric_value}``."""

StrategyState = dict[str, Any]
"""JSON-serialisable optimiser state for ``dump`` / rehydrate (§10).

Durable persistence between runner restarts is build Phase 3; in Phase 2
``dump`` exists to satisfy the Protocol and to round-trip the history.
"""

FactorKind = Literal["float", "int", "categorical"]


@dataclass(frozen=True, slots=True)
class FactorSpec:
    """One search-space dimension, parsed from the validated config.

    For an ``int`` factor ``low``/``high`` are guaranteed whole numbers — the
    config validator (``_validate_numeric_factor``) rejects fractional integer
    bounds — and every value emitted for it is produced via
    ``matofx.studies._intsnap.snap_int`` (round half away from zero, clamped).

    ``precision`` is the descriptor's declared step for a ``float`` factor,
    frozen into the config at ``configure`` time. Values are snapped onto it on
    decode for the same reason integers are rounded: the tool has only the
    resolution it declares, so proposing ``10.055135393891423`` for an input
    that steps in tenths asks for a design the tool cannot actually build, and
    records a number the run never had.
    """

    name: str
    kind: FactorKind
    low: float | None = None
    high: float | None = None
    choices: tuple[str, ...] = ()
    precision: float | None = None

    def snap_float(self, value: float) -> float:
        """Clamp a raw continuous draw into the box, then onto the declared step.

        The single place a ``float`` factor's emitted value is produced, so every
        strategy resolves one the same way — the role :func:`snap_int` already
        plays for integers.
        """
        clamped = min(float(self.high), max(float(self.low), float(value)))  # type: ignore[arg-type]
        return snap_precision(clamped, self.precision, self.low, self.high)


@dataclass(frozen=True, slots=True)
class Objective:
    """One optimised response + its direction (§3.2).

    ``weight`` is the user-set relative importance (default 1.0). How it is
    used depends on the strategy: scalarising strategies weight a weighted-sum
    aggregation, Pareto strategies use it to normalise/bias. Ignored where not
    applicable.
    """

    name: str
    goal: Literal["minimise", "maximise"]
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy needs to operate, derived from the study config.

    Built by :func:`build_context` from the *already-validated*
    ``config_json`` (so the strategy never re-validates).
    """

    factors: tuple[FactorSpec, ...]
    objectives: tuple[Objective, ...]
    random_seed: int | None = None
    batch_size: int = 1
    """The study's batch size — pymoo population strategies size their
    generation to it (§9); the ask/tell strategies ignore it."""
    output_constraints: tuple[dict[str, Any], ...] = ()
    """The declared ``output`` constraint specs (§7) — the multi-objective
    strategies read these to size + fill pymoo's constraint matrix."""
    acquisition: str = "balanced"
    """How the ``bo`` strategy turns the surrogate into the next run(s):
    ``optimise`` (exploit), ``balanced``, or ``explore`` (improve the model)."""
    surrogate_kind: str = "gp"
    """Which MATOFX surrogate the ``bo`` strategy fits (``gp`` / ``gp_matern``
    / ``poly`` / ``gbm``); kernel/best-of selection is MOP/CoP (later phase)."""

    candidate_pool: int = 512
    """How many points the ``bo`` strategy scores per ``ask`` before choosing
    the next run(s). Larger = a finer acquisition search but slower proposals."""

    sum_groups: tuple[dict[str, Any], ...] = ()
    """Descriptor sum-check groups (§3.6) — a strategy projects each proposed
    candidate onto these in ``decode`` (one *balance* member absorbs the
    residual) so its points already satisfy the sum, matching the runtime
    repair and keeping the generational ask/tell (pymoo) consistent."""


@runtime_checkable
class Strategy(Protocol):
    """The ask/tell contract every strategy implements (ADR-0009 §4.2)."""

    objective_shapes: ClassVar[frozenset[str]]
    supports_async_tell: ClassVar[bool]
    needs_seed: ClassVar[bool]

    def bind(self, context: StrategyContext, state: StrategyState | None) -> Strategy:
        """Configure this strategy for a study; return the bound instance."""
        ...

    def ask(self, n: int) -> list[Candidate]:
        """Propose up to ``n`` next candidates (may return fewer / none)."""
        ...

    def tell(
        self,
        x: Sequence[Candidate],
        y: Sequence[ResponseVector],
        feasible: Sequence[bool],
    ) -> None:
        """Feed back the evaluated batch. Idempotent on identical ``(x, y)``."""
        ...

    def rationale(self, c: Candidate) -> dict[str, Any]:
        """Why this candidate was proposed (audit + convergence colouring)."""
        ...

    def dump(self) -> StrategyState:
        """Serialise internal state (§10)."""
        ...

    def best_so_far(self) -> ResponseVector | None:
        """Best feasible response vector seen, or ``None`` (single-objective)."""
        ...

    def pareto_front(self) -> list[ResponseVector]:
        """Non-dominated feasible set (multi-objective); ``[]`` for single."""
        ...


def build_context(config: Mapping[str, Any]) -> StrategyContext:
    """Parse a validated ``config_json`` into a :class:`StrategyContext`.

    Weighted-sum studies (a single-objective strategy over several objectives)
    present the strategy a single synthetic objective; the runtime scalarises
    each run's responses into it. Pareto studies keep their real objectives.
    """
    from matofx.studies.optimisation.config import (  # noqa: PLC0415
        WEIGHTED_SUM_KEY,
        is_weighted_sum,
    )

    objectives = (
        (Objective(name=WEIGHTED_SUM_KEY, goal="minimise", weight=1.0),)
        if is_weighted_sum(config)
        else parse_objectives(config)
    )
    return StrategyContext(
        factors=parse_factors(config),
        objectives=objectives,
        random_seed=config.get("random_seed"),
        batch_size=max(1, int(config.get("batch_size", 1))),
        output_constraints=tuple(
            dict(c)
            for c in (config.get("constraints") or [])
            if isinstance(c, Mapping) and c.get("kind") == "output"
        ),
        acquisition=str(config.get("acquisition", "balanced")),
        surrogate_kind=str(config.get("surrogate_kind", "gp")),
        candidate_pool=max(16, int(config.get("surrogate_candidate_pool") or 512)),
        sum_groups=tuple(
            dict(g) for g in (config.get("sum_groups") or []) if isinstance(g, Mapping)
        ),
    )


def parse_factors(config: Mapping[str, Any]) -> tuple[FactorSpec, ...]:
    """Build :class:`FactorSpec`s from the config's ``factors`` (post-validation)."""
    specs: list[FactorSpec] = []
    for factor in config.get("factors") or []:
        kind = factor["type"]
        if kind == "categorical":
            specs.append(
                FactorSpec(
                    name=factor["name"],
                    kind="categorical",
                    choices=tuple(str(c) for c in factor.get("choices") or ()),
                )
            )
        else:
            specs.append(
                FactorSpec(
                    name=factor["name"],
                    kind=kind,
                    low=float(factor["min"]),
                    high=float(factor["max"]),
                    precision=(
                        float(factor["precision"])
                        if isinstance(factor.get("precision"), int | float)
                        else None
                    ),
                )
            )
    return tuple(specs)


def parse_objectives(config: Mapping[str, Any]) -> tuple[Objective, ...]:
    """Build the optimised-:class:`Objective` tuple (``track`` responses excluded)."""
    objectives: list[Objective] = []
    for response in config.get("responses") or []:
        goal = response.get("goal")
        if goal in {"minimise", "maximise"}:
            weight = response.get("weight", 1.0)
            objectives.append(
                Objective(
                    name=response["name"],
                    goal=goal,
                    weight=float(weight) if isinstance(weight, int | float) else 1.0,
                )
            )
    return tuple(objectives)


class BaseStrategy:
    """Single-objective bookkeeping shared by the Phase 2 strategies.

    Holds the evaluated history, tracks the best feasible point, and
    exposes the "always minimise" scalarisation (negating a ``maximise``
    goal) so subclasses reason in one direction. Subclasses override
    :meth:`ask` (and may extend :meth:`tell`).
    """

    objective_shapes: ClassVar[frozenset[str]] = frozenset({"single"})
    supports_async_tell: ClassVar[bool] = True
    needs_seed: ClassVar[bool] = False
    name: ClassVar[str] = "base"

    def __init__(self) -> None:
        """Construct an unbound strategy (the registry instantiates it)."""
        self.context: StrategyContext | None = None
        self._history: list[tuple[Candidate, ResponseVector, bool]] = []
        self._seen: set[tuple[str, str]] = set()

    def bind(self, context: StrategyContext, state: StrategyState | None) -> Strategy:
        """Attach the context + restore history from a dumped state."""
        self.context = context
        self._history = []
        self._seen = set()
        if state:
            for entry in state.get("history", []):
                cand, responses, feas = entry["x"], entry["y"], bool(entry["feasible"])
                self._history.append((cand, responses, feas))
                self._seen.add(self._signature(cand, responses))
        return self

    @staticmethod
    def _signature(cand: Candidate, responses: Mapping[str, float]) -> tuple[str, str]:
        """A hashable identity for a (candidate, responses) pair (idempotency)."""
        return (
            json.dumps(cand, sort_keys=True, default=str),
            json.dumps({k: float(v) for k, v in responses.items()}, sort_keys=True),
        )

    def _record(self, cand: Candidate, responses: ResponseVector, feasible: bool) -> bool:
        """Append to history if unseen; return ``True`` when newly recorded.

        This is what makes :meth:`tell` idempotent on identical ``(x, y)``
        (non-negotiable #2) — re-feeding a crash-recovered point is a
        no-op, so a strategy never double-counts it.
        """
        sig = self._signature(cand, responses)
        if sig in self._seen:
            return False
        self._seen.add(sig)
        self._history.append((dict(cand), dict(responses), bool(feasible)))
        return True

    def ask(self, n: int) -> list[Candidate]:
        """Propose candidates — every concrete strategy overrides this."""
        raise NotImplementedError

    @property
    def _ctx(self) -> StrategyContext:
        if self.context is None:
            raise RuntimeError("Strategy used before bind().")
        return self.context

    @property
    def _objective(self) -> Objective:
        """The single optimised objective (multi-objective is build Phase 4)."""
        objectives = self._ctx.objectives
        if len(objectives) != 1:
            raise RuntimeError(
                f"{self.name!r} is single-objective but the study has "
                f"{len(objectives)} optimised responses."
            )
        return objectives[0]

    def _scalarise(self, y: ResponseVector) -> float:
        """Objective value as a quantity to **minimise** (maximise → negated)."""
        objective = self._objective
        value = float(y[objective.name])
        return value if objective.goal == "minimise" else -value

    def tell(
        self,
        x: Sequence[Candidate],
        y: Sequence[ResponseVector],
        feasible: Sequence[bool],
    ) -> None:
        """Append the batch to history (idempotent on identical entries)."""
        for cand, responses, feas in zip(x, y, feasible, strict=True):
            self._record(cand, responses, feas)

    def rationale(self, c: Candidate) -> dict[str, Any]:  # noqa: ARG002 — Protocol arg
        """Default rationale — just the proposing strategy."""
        return {"proposed_by": self.name}

    def dump(self) -> StrategyState:
        """Round-trip the history (the durable source of truth is study_result)."""
        return {
            "history": [
                {"x": x, "y": y, "feasible": feasible} for (x, y, feasible) in self._history
            ]
        }

    def best_so_far(self) -> ResponseVector | None:
        """The feasible history entry with the smallest scalarised objective."""
        feasible = [(x, y) for (x, y, ok) in self._history if ok]
        if not feasible:
            return None
        best = min(feasible, key=lambda pair: self._scalarise(pair[1]))
        return best[1]

    def pareto_front(self) -> list[ResponseVector]:
        """Multi-objective Pareto set — build Phase 4; ``[]`` for single-objective."""
        return []
