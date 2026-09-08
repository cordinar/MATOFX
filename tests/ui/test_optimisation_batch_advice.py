"""The runs-per-step field describes itself (2026-09-02).

One fixed label, "Runs per step", hid the case that matters most: every
evolutionary strategy builds its pymoo optimiser with
``pop_size=batch_size``, so for those the field **is** the population — and
the form's default of 1 gave a population of four, which cannot hold a Pareto
front however many generations it runs.

The field now re-labels and re-suggests itself per strategy, and says why.
"""

from __future__ import annotations

import pytest

from matofx.studies.optimisation.strategies import get
from matofx.ui.pages.optimisation import (
    _POPULATION_STRATEGIES,
    _STRATEGY_OPTIONS,
    batch_advice,
)


@pytest.mark.parametrize("strategy", sorted(_POPULATION_STRATEGIES))
def test_a_population_strategy_says_so_in_the_label(strategy: str) -> None:
    label, suggested, note = batch_advice(strategy, seed_n=20, max_runs=400)
    assert "Population" in label
    assert suggested >= 15, "a population this small cannot hold a front"
    assert "generation" in note


def test_the_population_follows_a_larger_seed() -> None:
    """A 40-run seed has already paid for 40 points; halving it wastes them."""
    assert batch_advice("nsga2", seed_n=40, max_runs=400)[1] == 40
    assert batch_advice("nsga2", seed_n=8, max_runs=400)[1] == 20  # floor still applies


def test_the_note_counts_generations_from_the_budget() -> None:
    _label, suggested, note = batch_advice("nsga2", seed_n=20, max_runs=400)
    assert f"{400 // suggested} generation" in note


def test_bayesian_optimisation_is_told_to_stay_sequential() -> None:
    """BO learns from every result, so a batch trades answer quality for
    parallelism — the opposite advice to a population method."""
    label, suggested, note = batch_advice("bo", seed_n=20, max_runs=400)
    assert suggested == 1
    assert "Population" not in label
    assert "without seeing each other" in note


def test_a_baseline_search_is_described_as_parallelism_only() -> None:
    label, _suggested, note = batch_advice("random", seed_n=20, max_runs=400)
    assert label == "Runs per step"
    assert "no model to update" in note


@pytest.mark.parametrize("strategy", sorted(_STRATEGY_OPTIONS))
def test_every_offered_strategy_gets_advice(strategy: str) -> None:
    """A strategy the form offers but the advice does not know would fall back
    to the parallelism wording, which is wrong for a population method."""
    label, suggested, note = batch_advice(strategy, seed_n=20, max_runs=400)
    assert label
    assert suggested >= 1
    assert note


@pytest.mark.parametrize("strategy", sorted(_POPULATION_STRATEGIES))
def test_the_population_list_matches_the_strategies_that_use_pop_size(
    strategy: str,
) -> None:
    """The list is a claim about the engine; check it against the engine.

    If a strategy stops being population-based (or a new one becomes so), this
    catches the advice drifting away from what the optimiser actually does.
    """
    assert strategy in _STRATEGY_OPTIONS, "advice for a strategy the form cannot pick"
    assert get(strategy) is not None


def test_no_offered_strategy_is_silently_missing_from_the_population_list() -> None:
    """The inverse: a pop_size strategy left out would be advised as if a
    batch were free parallelism, which is how the population-of-four happened.
    """
    known_sequential = {"bo", "random", "grid"}
    assert set(_STRATEGY_OPTIONS) - _POPULATION_STRATEGIES == known_sequential
