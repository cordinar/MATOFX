"""Slice 4 (+ enrichment pass) — the templated optimisation narrative (pure).

Covers the richer output: best design + constraint activity, Pareto spread, CoP
sensitivity, and convergence. Constraint facts use the real ``op`` symbols.
"""

from __future__ import annotations

from matofx.summaries import SummaryFacts, render_optimisation


def _facts(state: str = "complete", **details_overrides: object) -> SummaryFacts:
    details: dict[str, object] = {
        "strategy": "bo",
        "run_mode": "real",
        "evaluations": 40,
        "n_feasible": 40,
        "objectives": [
            {"name": "q", "label": "Heat dissipation", "sense": "maximise", "unit": "W"}
        ],
        "constraints": [{"label": "Peak temperature", "op": "<=", "value": 100.0, "unit": "°C"}],
        "multi_objective": False,
        "stop_reason": "budget_exhausted",
        "frozen_factors": ["Number of fins"],
        "best_value": 45.2,
        "best_constraints": [
            {
                "label": "Peak temperature",
                "value": 92.0,
                "unit": "°C",
                "op": "<=",
                "limit": 100.0,
                "binding": False,
            }
        ],
        "best_design": [
            {"label": "Fin height", "value": 12.0, "unit": "mm"},
            {"label": "Fin spacing", "value": 3.0, "unit": "mm"},
        ],
        "sensitivity": {
            "drivers": [
                {"label": "Fin spacing", "cop": 0.61},
                {"label": "Fin height", "cop": 0.24},
            ],
            "q2": 0.88,
        },
        "convergence": {"first": 31.0, "best": 45.2, "batches": 8},
    }
    details.update(details_overrides)
    return SummaryFacts(
        surface_kind="optimisation_study",
        surface_id=312,
        title="Heat-sink cooling",
        state=state,
        details=details,
    )


def test_setup_sentence_includes_constraint_and_budget() -> None:
    joined = " ".join(render_optimisation(_facts()).paragraphs)
    assert (
        "This Bayesian-optimisation study maximised heat dissipation subject to "
        "peak temperature at most 100 °C, over 40 evaluations in real mode, "
        "stopping on the evaluation budget being reached." in joined
    )


def test_best_design_value_and_constraint_activity() -> None:
    # The bug this fixes: best value comes from current_best_json keyed by the
    # response name, not a literal "objective" key — so it must now appear.
    joined = " ".join(render_optimisation(_facts()).paragraphs)
    assert "The best design reaches heat dissipation 45.2 W" in joined
    assert "peak temperature comfortably within its 100 °C limit at 92 °C" in joined
    assert "It sits at fin height 12 mm and fin spacing 3 mm." in joined
    assert "40 of 40 evaluations were feasible" in joined


def test_binding_constraint_is_flagged() -> None:
    facts = _facts(
        best_constraints=[
            {
                "label": "Peak temperature",
                "value": 99.5,
                "unit": "°C",
                "op": "<=",
                "limit": 100.0,
                "binding": True,
            }
        ]
    )
    joined = " ".join(render_optimisation(facts).paragraphs)
    assert "peak temperature binding at 99.5 °C (limit 100 °C)" in joined


def test_sensitivity_paragraph_lists_cop_drivers() -> None:
    joined = " ".join(render_optimisation(_facts()).paragraphs)
    assert "attributes most of the response to fin spacing (CoP 0.61)" in joined
    assert "fin height (CoP 0.24)" in joined
    assert "Q-squared 0.88" in joined
    # frozen folds into the sensitivity sentence, not a standalone one
    assert "Variable-reduction analysis froze" not in joined
    assert "Low-influence factors (Number of fins) were frozen" in joined


def test_convergence_paragraph() -> None:
    joined = " ".join(render_optimisation(_facts()).paragraphs)
    assert "the best objective value moved from 31 to 45.2" in joined


def test_multi_objective_pareto_spread() -> None:
    facts = _facts(
        strategy="nsga2",
        objectives=[
            {"name": "mass", "label": "Mass", "sense": "minimise", "unit": "kg"},
            {"name": "t", "label": "Peak temperature", "sense": "minimise", "unit": "°C"},
        ],
        multi_objective=True,
        pareto_size=9,
        pareto_ranges=[
            {"label": "Mass", "min": 0.12, "max": 0.21, "unit": "kg"},
            {"label": "Peak temperature", "min": 78.0, "max": 99.0, "unit": "°C"},
        ],
        best_value=None,
    )
    joined = " ".join(render_optimisation(facts).paragraphs)
    assert "This NSGA-II study minimised mass and peak temperature" in joined
    assert "returned a 9-point Pareto front" in joined
    assert "mass spans 0.12 to 0.21 kg" in joined
    assert "peak temperature spans 78 to 99 °C" in joined


def test_failed_study() -> None:
    joined = " ".join(render_optimisation(_facts(state="failed")).paragraphs)
    assert "failed before producing a result" in joined


def test_paragraph_count_stays_within_four() -> None:
    # setup + outcome + sensitivity + convergence = 4 (the format-guard ceiling).
    assert len(render_optimisation(_facts()).paragraphs) <= 4
