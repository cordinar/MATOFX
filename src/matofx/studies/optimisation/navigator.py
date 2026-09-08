"""Algorithm-recommendation wizard — the "Navigator" (ADR-0009 §5.6).

`recommend_strategy(config)` proposes a strategy + run mode + batch size
from the problem's shape (objective count, factor count + kinds, run
mode). The create wizard shows a **"Recommended"** badge on the suggested
strategy with the one-line rationale; the user can always override — the
full dropdown stays available (§5.6: "the wizard never *forces* a
choice").

The heuristics are deterministic, documented, and read only the
(possibly partial) draft config — no DB, no pymoo, no surrogate engine.
They are tuned to the *implemented* strategy set (ADR-0009 amendment
2026-06-08 + build Phase 9): the dropped Optuna `hyperband` /
`active_*` branches are folded into ``bo`` (the ``explore`` acquisition
mode covers "improve the model, not the optimum"), and the scipy
local/annealing polish (`nelder_mead` / `slsqp`) is a later phase, so the
cheap/surrogate branch recommends ``de`` alone for now.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_HIGH_DIM = 20
"""Factor count above which surrogate BO starts to struggle and CMA-ES
scales better (§5.6 "~20 factors")."""

_MULTI_OBJECTIVE = 2
"""Optimised-response count at/above which a study is multi-objective."""

_MANY_OBJECTIVES = 3
"""Objective count above which NSGA-III's reference directions beat
NSGA-II's crowding distance (§5.6)."""

_OPTIMISED_GOALS = frozenset({"minimise", "maximise"})


@dataclass(frozen=True, slots=True)
class StrategyRecommendation:
    """A proposed strategy + run mode + batch size, with a plain-English why."""

    strategy: str
    run_mode: str
    batch_size: int
    rationale: str
    acquisition: str | None = None
    """Set only when ``strategy == 'bo'`` — the suggested acquisition mode."""


def recommend_strategy(config: Mapping[str, Any]) -> StrategyRecommendation:
    """Propose a strategy for a (possibly partial) draft config (§5.6).

    Reads ``factors`` / ``responses`` / ``run_mode`` defensively — a draft
    mid-configuration is fine; missing pieces fall through to the
    single-objective default. The result is a *suggestion*: the wizard
    pre-selects it and shows the rationale, never locks it in.
    """
    factors = [f for f in (config.get("factors") or []) if isinstance(f, Mapping)]
    n_factors = len(factors)
    n_continuous = sum(1 for f in factors if f.get("type") == "float")
    n_discrete = n_factors - n_continuous
    optimised = sum(
        1
        for r in (config.get("responses") or [])
        if isinstance(r, Mapping) and r.get("goal") in _OPTIMISED_GOALS
    )
    run_mode = str(config.get("run_mode") or "real")

    # --- multiple objectives → evolutionary Pareto search ---
    if optimised >= _MULTI_OBJECTIVE:
        if optimised > _MANY_OBJECTIVES:
            return StrategyRecommendation(
                strategy="nsga3",
                run_mode=run_mode,
                batch_size=12,
                rationale=(
                    f"{optimised} objectives — NSGA-III's reference directions keep a "
                    "well-spread front past three goals, where NSGA-II thins out."
                ),
            )
        return StrategyRecommendation(
            strategy="nsga2",
            run_mode=run_mode,
            batch_size=12,
            rationale=(
                f"{optimised} objectives — NSGA-II maps the Pareto trade-off front for "
                "two or three goals."
            ),
        )

    # --- single objective (or none chosen yet) ---
    # Surrogate-mode evaluations are instant, so a global population search
    # costs nothing against the run budget; differential evolution is the
    # robust default there (the nelder_mead/slsqp polish step is a later phase).
    if run_mode == "surrogate":
        return StrategyRecommendation(
            strategy="de",
            run_mode="surrogate",
            batch_size=8,
            rationale=(
                "Surrogate evaluations are instant — differential evolution searches "
                "globally with no run-budget cost."
            ),
        )

    # High-dimensional or integer/categorical-dominated spaces outscale the
    # continuous GP that backs BO; CMA-ES is the workhorse there.
    if n_factors > _HIGH_DIM or (n_discrete > 0 and n_discrete > n_continuous):
        reason = (
            f"{n_factors} factors" if n_factors > _HIGH_DIM else "an integer/categorical space"
        )
        return StrategyRecommendation(
            strategy="cmaes",
            run_mode=run_mode,
            batch_size=8,
            rationale=(
                f"With {reason}, CMA-ES scales past surrogate search while staying robust."
            ),
        )

    # The sweet spot for the MATOFX surrogate: one objective, a modest
    # continuous space, expensive real runs → sample-efficient Bayesian search.
    return StrategyRecommendation(
        strategy="bo",
        run_mode=run_mode,
        batch_size=1,
        acquisition="balanced",
        rationale=(
            "One objective over a modest continuous space — surrogate search "
            "(Bayesian optimisation) makes the most of every expensive run."
        ),
    )
