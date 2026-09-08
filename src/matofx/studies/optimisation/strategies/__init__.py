"""Strategy registry + the implemented strategies (ADR-0009 §5).

Importing this package registers every strategy with a runtime
implementation into the registry: the baselines (``random`` / ``grid``),
the MATOFX-surrogate ``bo`` (amendment 2026-06-08), the multi-objective
EAs (``nsga2`` / ``moea_d`` / ``nsga3``), and the single-objective
classical spectrum (``cmaes`` / ``de`` / ``pso`` / ``ga``; build Phase 9).
The scipy local/annealing strategies (``nelder_mead`` / ``slsqp`` /
``dual_annealing``) and MOP/CoP land in later phases and register the same
way.
"""

from __future__ import annotations

from matofx.studies.optimisation.strategies.base import (
    BaseStrategy,
    Candidate,
    FactorSpec,
    Objective,
    ResponseVector,
    Strategy,
    StrategyContext,
    StrategyState,
    build_context,
)
from matofx.studies.optimisation.strategies.grid import GridStrategy
from matofx.studies.optimisation.strategies.multi import (
    MOEADStrategy,
    NSGA2Strategy,
    NSGA3Strategy,
    PymooStrategy,
)
from matofx.studies.optimisation.strategies.random_search import RandomStrategy
from matofx.studies.optimisation.strategies.registry import (
    StrategyRegistryError,
    get,
    is_registered,
    register,
    registered_names,
)
from matofx.studies.optimisation.strategies.soo import (
    CMAESStrategy,
    DEStrategy,
    GAStrategy,
    PSOStrategy,
    SingleObjectivePymooStrategy,
)
from matofx.studies.optimisation.strategies.surrogate_bo import SurrogateBOStrategy

register("random", RandomStrategy)
register("grid", GridStrategy)
register("bo", SurrogateBOStrategy)
register("cmaes", CMAESStrategy)
register("de", DEStrategy)
register("pso", PSOStrategy)
register("ga", GAStrategy)
register("nsga2", NSGA2Strategy)
register("moea_d", MOEADStrategy)
register("nsga3", NSGA3Strategy)

__all__ = [
    "BaseStrategy",
    "CMAESStrategy",
    "Candidate",
    "DEStrategy",
    "FactorSpec",
    "GAStrategy",
    "GridStrategy",
    "MOEADStrategy",
    "NSGA2Strategy",
    "NSGA3Strategy",
    "Objective",
    "PSOStrategy",
    "PymooStrategy",
    "RandomStrategy",
    "ResponseVector",
    "SingleObjectivePymooStrategy",
    "Strategy",
    "StrategyContext",
    "StrategyRegistryError",
    "StrategyState",
    "SurrogateBOStrategy",
    "build_context",
    "get",
    "is_registered",
    "register",
    "registered_names",
]
