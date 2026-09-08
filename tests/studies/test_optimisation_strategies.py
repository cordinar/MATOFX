"""Unit tests for the Phase 2 strategy engine (ADR-0009 §5/§7/§11).

Pure functions — no DB. Cover the three foundational strategies'
ask/tell contract + best tracking, the LHS seed, the selection-time
constraint filter, and the registry guards.
"""

from __future__ import annotations

from typing import Any

import pytest

from matofx.studies.optimisation.constraints import constraints_ok
from matofx.studies.optimisation.seed import default_seed_size, lhs_seed
from matofx.studies.optimisation.strategies import (
    StrategyRegistryError,
    build_context,
    get,
    is_registered,
    register,
    registered_names,
)


def _config(strategy: str = "random", **extra: Any) -> dict[str, Any]:
    config = {
        "factors": [
            {"name": "x", "type": "float", "min": 0.0, "max": 10.0},
            {"name": "n", "type": "int", "min": 1, "max": 4},
            {"name": "mat", "type": "categorical", "choices": ["al", "cu"]},
        ],
        "responses": [{"name": "mass", "spec": {"kind": "scalar"}, "goal": "minimise"}],
        "strategy": strategy,
        "random_seed": 7,
    }
    config.update(extra)
    return config


# --- registry ---------------------------------------------------------------


def test_foundational_strategies_registered() -> None:
    # The implemented set (Phase 9 added the pymoo classical spectrum); the
    # scipy local/annealing strategies (nelder_mead/slsqp/dual_annealing) land later.
    assert {
        "random",
        "grid",
        "bo",
        "cmaes",
        "de",
        "pso",
        "ga",
        "nsga2",
        "moea_d",
        "nsga3",
    } == registered_names()
    assert is_registered("random")
    assert is_registered("cmaes")  # Phase 9
    assert not is_registered("nelder_mead")  # later phase


def test_register_rejects_unknown_name() -> None:
    with pytest.raises(StrategyRegistryError, match="catalogue"):
        register("not_a_strategy", get("random").__class__)


# --- random -----------------------------------------------------------------


def test_random_ask_in_bounds_and_tell_best() -> None:
    ctx = build_context(_config("random"))
    strat = get("random").bind(ctx, None)
    cands = strat.ask(8)
    assert len(cands) == 8
    for c in cands:
        assert 0.0 <= c["x"] <= 10.0
        assert isinstance(c["n"], int)
        assert 1 <= c["n"] <= 4
        assert c["mat"] in {"al", "cu"}
    responses = [{"mass": float(c["x"] + c["n"])} for c in cands]
    strat.tell(cands, responses, [True] * len(cands))
    best = strat.best_so_far()
    assert best is not None
    assert best["mass"] == min(r["mass"] for r in responses)


def test_random_is_seed_reproducible() -> None:
    a = get("random").bind(build_context(_config("random")), None).ask(5)
    b = get("random").bind(build_context(_config("random")), None).ask(5)
    assert a == b  # same random_seed → same draws


# --- grid -------------------------------------------------------------------


def test_grid_enumerates_then_exhausts() -> None:
    ctx = build_context(_config("grid"))
    strat = get("grid").bind(ctx, None)
    seen: list[dict[str, Any]] = []
    while True:
        batch = strat.ask(4)
        if not batch:
            break
        seen.extend(batch)
    # 5 float levels x 4 int levels x 2 categories = 40 points.
    assert len(seen) == 5 * 4 * 2
    assert strat.ask(4) == []  # exhausted stays exhausted


# --- bo (MATOFX surrogate) --------------------------------------------------


def test_bo_ask_in_bounds_and_tracks_best() -> None:
    ctx = build_context(_config("bo"))
    strat = get("bo").bind(ctx, None)
    assert strat.needs_seed is True
    # Seed it with a handful of evaluations so the surrogate can fit.
    seed = [{"x": float(i), "n": i % 4, "mat": "al"} for i in range(6)]
    strat.tell(seed, [{"mass": float(s["x"] + s["n"])} for s in seed], [True] * len(seed))
    cands = strat.ask(3)
    assert len(cands) == 3
    for c in cands:  # proposals respect the factor box
        assert 0.0 <= c["x"] <= 10.0
        assert 1 <= c["n"] <= 4
        assert c["mat"] in {"al", "cu"}
    best = strat.best_so_far()
    assert best is not None
    assert best["mass"] == min(s["x"] + s["n"] for s in seed)


def test_bo_explore_acquisition_proposes() -> None:
    ctx = build_context(_config("bo", acquisition="explore"))
    strat = get("bo").bind(ctx, None)
    seed = [{"x": float(i), "n": i % 4, "mat": "al"} for i in range(6)]
    strat.tell(seed, [{"mass": float(s["x"] + s["n"])} for s in seed], [True] * len(seed))
    cands = strat.ask(2)
    assert len(cands) == 2  # explore still proposes runnable points


# --- single-objective pymoo spectrum (cmaes / de / pso / ga) ----------------


@pytest.mark.parametrize("strategy", ["cmaes", "de", "pso", "ga"])
def test_soo_pymoo_reaches_budget_and_tracks_best(strategy: str) -> None:
    """Each classical single-objective strategy drives a multi-generation
    ask/tell loop, stays in the factor box, and converges its best toward
    the analytic optimum of a simple bowl objective (§5)."""
    ctx = build_context(_config(strategy, batch_size=6))
    strat = get(strategy).bind(ctx, None)
    assert strat.needs_seed is False  # the EAs bootstrap their own population

    # mass = (x-3)^2 + n  → minimised near x=3, n=1.
    def _mass(c: dict[str, Any]) -> float:
        return float((c["x"] - 3.0) ** 2 + c["n"])

    evaluated = 0
    for _ in range(15):
        cands = strat.ask(6)
        assert cands  # the generation is non-empty
        for c in cands:  # proposals respect the factor box
            assert 0.0 <= c["x"] <= 10.0
            assert 1 <= c["n"] <= 4
            assert c["mat"] in {"al", "cu"}
        strat.tell(cands, [{"mass": _mass(c)} for c in cands], [True] * len(cands))
        evaluated += len(cands)

    assert evaluated >= 60  # reached a meaningful budget
    best = strat.best_so_far()
    assert best is not None
    assert strat.pareto_front() == []  # single-objective → no front
    # The bowl's floor is mass=1 (x=3, n=1); a real optimiser gets close.
    assert best["mass"] < 4.0


def test_soo_pymoo_idempotent_tell() -> None:
    """Re-telling an identical (x, y) batch is a no-op (crash-recovery safety)."""
    ctx = build_context(_config("de", batch_size=6))
    strat = get("de").bind(ctx, None)
    cands = strat.ask(6)
    responses = [{"mass": float(c["x"] + c["n"])} for c in cands]
    strat.tell(cands, responses, [True] * len(cands))
    best_after_first = strat.best_so_far()
    strat.tell(cands, responses, [True] * len(cands))  # replay
    assert strat.best_so_far() == best_after_first


# --- integer handling, Phase C (lattice repair + distinct bo pool) ----------


@pytest.mark.parametrize("strategy", ["de", "pso", "ga"])
def test_pymoo_soo_proposes_only_integers(strategy: str) -> None:
    """The lattice repair keeps an integer factor whole across generations —
    every decoded proposal for ``n`` is a Python ``int`` in range, never a
    relaxed float (ADR-0009 amendment 2026-06-15, Phase C)."""
    ctx = build_context(_config(strategy, batch_size=6))
    strat = get(strategy).bind(ctx, None)
    for _ in range(3):
        cands = strat.ask(6)
        for c in cands:
            assert isinstance(c["n"], int)
            assert 1 <= c["n"] <= 4
        strat.tell(cands, [{"mass": float(c["x"] + c["n"])} for c in cands], [True] * len(cands))


def _int_only_config(strategy: str, **extra: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "factors": [{"name": "n", "type": "int", "min": 0, "max": 2}],  # 3 designs
        "responses": [{"name": "mass", "spec": {"kind": "scalar"}, "goal": "minimise"}],
        "strategy": strategy,
        "random_seed": 7,
    }
    config.update(extra)
    return config


def test_bo_proposes_distinct_designs_in_small_int_space() -> None:
    """``bo`` returns the top *distinct* lattice points: a 3-point integer space
    can't yield 5 different proposals, so ask(5) gives ≤ 3 unique whole ``n``."""
    ctx = build_context(_int_only_config("bo"))
    strat = get("bo").bind(ctx, None)
    seed = [{"n": i} for i in range(3)]
    strat.tell(seed, [{"mass": float(s["n"])} for s in seed], [True] * 3)
    cands = strat.ask(5)
    values = [c["n"] for c in cands]
    assert all(isinstance(v, int) and 0 <= v <= 2 for v in values)
    assert len(values) == len(set(values))  # no identical proposals in a batch
    assert len(values) <= 3


# --- seed -------------------------------------------------------------------


def test_default_seed_size() -> None:
    assert default_seed_size(1) == 10
    assert default_seed_size(5) == 15


def test_lhs_seed_in_bounds() -> None:
    ctx = build_context(_config())
    points = lhs_seed(ctx.factors, 12, random_seed=3)
    assert len(points) == 12
    for p in points:
        assert 0.0 <= p["x"] <= 10.0
        assert isinstance(p["n"], int)
        assert 1 <= p["n"] <= 4
        assert p["mat"] in {"al", "cu"}


def test_lhs_seed_empty_factors_raises() -> None:
    with pytest.raises(ValueError, match="no factors"):
        lhs_seed([], 5)


# --- constraints ------------------------------------------------------------


def test_constraints_ok_cross_field() -> None:
    config = {"constraints": [{"kind": "cross_field", "expr": "a + b <= 10"}]}
    assert constraints_ok({"a": 4, "b": 5}, config) is True
    assert constraints_ok({"a": 8, "b": 5}, config) is False


def test_constraints_ok_no_constraints() -> None:
    assert constraints_ok({"a": 1}, {}) is True


def test_constraints_ok_output_kind_ignored_at_selection() -> None:
    # Output constraints are evaluated at tell time (Phase 4), not here.
    config = {"constraints": [{"kind": "output", "response": "t", "op": "<=", "value": 5}]}
    assert constraints_ok({"a": 1}, config) is True
