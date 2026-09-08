"""Unit tests for the Navigator's strategy recommendation (ADR-0009 §5.6).

Pure heuristic — no DB. Each branch of ``recommend_strategy`` maps a
problem shape (objective count, factor count + kinds, run mode) to a
strategy + rationale; the wizard pre-selects it but the user can override.
"""

from __future__ import annotations

from typing import Any

from matofx.studies.optimisation.navigator import recommend_strategy


def _cfg(factors: list[dict[str, Any]], goals: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "factors": factors,
        "responses": [
            {"name": f"r{i}", "spec": {"kind": "scalar"}, "goal": g} for i, g in enumerate(goals)
        ],
        **extra,
    }


def _floats(n: int) -> list[dict[str, Any]]:
    return [{"name": f"x{i}", "type": "float", "min": 0.0, "max": 1.0} for i in range(n)]


def test_single_objective_modest_continuous_recommends_bo() -> None:
    rec = recommend_strategy(_cfg(_floats(3), ["minimise"]))
    assert rec.strategy == "bo"
    assert rec.acquisition == "balanced"
    assert rec.run_mode == "real"
    assert rec.batch_size >= 1
    assert "Bayesian" in rec.rationale or "surrogate" in rec.rationale.lower()


def test_high_dimensional_recommends_cmaes() -> None:
    rec = recommend_strategy(_cfg(_floats(25), ["maximise"]))
    assert rec.strategy == "cmaes"
    assert "25 factors" in rec.rationale


def test_integer_dominated_recommends_cmaes() -> None:
    factors = [
        {"name": "a", "type": "int", "min": 0, "max": 10},
        {"name": "b", "type": "categorical", "choices": ["x", "y"]},
        {"name": "c", "type": "float", "min": 0.0, "max": 1.0},
    ]
    rec = recommend_strategy(_cfg(factors, ["minimise"]))
    assert rec.strategy == "cmaes"  # 2 discrete > 1 continuous


def test_two_objectives_recommends_nsga2() -> None:
    rec = recommend_strategy(_cfg(_floats(2), ["minimise", "minimise"]))
    assert rec.strategy == "nsga2"
    assert rec.acquisition is None


def test_many_objectives_recommends_nsga3() -> None:
    rec = recommend_strategy(_cfg(_floats(2), ["minimise", "maximise", "minimise", "minimise"]))
    assert rec.strategy == "nsga3"


def test_surrogate_mode_recommends_de() -> None:
    rec = recommend_strategy(_cfg(_floats(3), ["minimise"], run_mode="surrogate"))
    assert rec.strategy == "de"
    assert rec.run_mode == "surrogate"


def test_track_only_responses_are_not_objectives() -> None:
    # A 'track' goal isn't optimised, so two tracks + one minimise = single.
    rec = recommend_strategy(_cfg(_floats(3), ["track", "minimise", "track"]))
    assert rec.strategy == "bo"


def test_empty_config_falls_through_to_bo() -> None:
    rec = recommend_strategy({})
    assert rec.strategy == "bo"  # the safe single-objective default
