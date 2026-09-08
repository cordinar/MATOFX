"""Slice 4 (+ enrichment pass) — the optimisation collector (DB-free).

Drives ``collect_optimisation_facts`` with an unpersisted study carrying a
realistic ``config_json`` / ``current_best_json`` / ``mop_state_json`` and
``run_results``. Pins the two bug fixes — constraints read ``op``/``value`` (not
``operator``/``limit``) and the best objective value comes from
``current_best_json`` keyed by the **response name** — plus the new richness
(best design, constraint activity, CoP sensitivity, Pareto spread).
"""

from __future__ import annotations

from typing import Any

import pytest

from matofx.studies.optimisation.orm import OptimisationStudy
from matofx.summaries import NonTerminalResultError
from matofx.summaries.collectors import collect_optimisation_facts

pytestmark = pytest.mark.asyncio

# Maximise heat dissipation `q` subject to peak temperature `t_max <= 100`.
_SINGLE_CONFIG = {
    "strategy": "bo",
    "run_mode": "real",
    "target": {"kind": "tool"},
    "factors": [
        {"name": "fin_height", "type": "float", "min": 5.0, "max": 20.0},
        {"name": "fin_spacing", "type": "float", "min": 1.0, "max": 5.0},
    ],
    "responses": [
        {"name": "q", "goal": "maximise", "spec": {"kind": "scalar"}},
        {"name": "t_max", "goal": "track"},
    ],
    "constraints": [
        {"kind": "output", "response": "t_max", "op": "<=", "value": 100.0}
    ],
}

_TRACE = [
    {"batch": 0, "metric": 31.0, "n_feasible": 6, "n_total": 8, "wall_clock_s": 10.0},
    {"batch": 1, "metric": 45.2, "n_feasible": 34, "n_total": 32, "wall_clock_s": 90.0},
]

_MOP = {
    "q2": 0.88,
    "cop": [
        {"factor": "fin_spacing", "cop": 0.61, "total_effect": 0.69},
        {"factor": "fin_height", "cop": 0.24, "total_effect": 0.28},
    ],
    "frozen_factors": ["fin_count"],
}


def _study(
    *,
    config: dict[str, Any],
    best: dict[str, Any] | None,
    state: str = "complete",
    mop: dict[str, Any] | None = None,
) -> OptimisationStudy:
    return OptimisationStudy(
        name="Heat-sink cooling",
        state=state,
        target_kind="tool",
        tool_version_id=None,  # → no descriptor lookup; labels fall back to names
        config_json=config,
        convergence_trace=_TRACE,
        current_best_json=best,
        mop_state_json=mop,
        stop_reason="budget_exhausted",
        last_batch_number=1,
    )


def _runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"job_id": i, "batch": 1, "state": "complete", "inputs": r[0], "scalars": r[1]}
        for i, r in enumerate(rows)
    ]


async def _facts(study: OptimisationStudy, runs: list[dict[str, Any]]) -> Any:
    return await collect_optimisation_facts(None, study=study, run_results=runs)  # type: ignore[arg-type]


async def test_non_terminal_is_refused() -> None:
    study = _study(config=_SINGLE_CONFIG, best={"q": 45.2}, state="running")
    with pytest.raises(NonTerminalResultError):
        await _facts(study, [])


async def test_best_value_from_response_name_key() -> None:
    # current_best_json is keyed by RESPONSE NAME — the objective value is
    # best["q"], not best["objective"]. (This was the dropped-value bug.)
    study = _study(config=_SINGLE_CONFIG, best={"q": 45.2, "t_max": 92.0})
    facts = await _facts(study, [])
    assert facts.details["best_value"] == 45.2
    assert facts.headline[0].value == 45.2


async def test_constraints_use_op_and_value_keys() -> None:
    study = _study(config=_SINGLE_CONFIG, best={"q": 45.2, "t_max": 92.0})
    facts = await _facts(study, [])
    constraints = facts.details["constraints"]
    assert constraints[0]["op"] == "<="
    assert constraints[0]["value"] == 100.0


async def test_constraint_activity_at_optimum() -> None:
    # t_max at the optimum (92) is well under the 100 limit → not binding.
    study = _study(config=_SINGLE_CONFIG, best={"q": 45.2, "t_max": 92.0})
    facts = await _facts(study, [])
    best_c = facts.details["best_constraints"][0]
    assert best_c["value"] == 92.0
    assert best_c["limit"] == 100.0
    assert best_c["binding"] is False


async def test_binding_when_optimum_hugs_the_limit() -> None:
    study = _study(config=_SINGLE_CONFIG, best={"q": 45.2, "t_max": 99.5})
    facts = await _facts(study, [])
    assert facts.details["best_constraints"][0]["binding"] is True


async def test_best_design_from_run_results() -> None:
    runs = _runs(
        [
            ({"fin_height": 8.0, "fin_spacing": 4.0}, {"q": 30.0, "t_max": 80.0}),
            ({"fin_height": 12.0, "fin_spacing": 3.0}, {"q": 45.2, "t_max": 92.0}),
        ]
    )
    study = _study(config=_SINGLE_CONFIG, best={"q": 45.2, "t_max": 92.0})
    facts = await _facts(study, runs)
    design = {d["label"]: d["value"] for d in facts.details["best_design"]}
    assert design == {"fin_height": 12.0, "fin_spacing": 3.0}


async def test_sensitivity_drivers_from_mop_cop() -> None:
    study = _study(config=_SINGLE_CONFIG, best={"q": 45.2}, mop=_MOP)
    facts = await _facts(study, [])
    sens = facts.details["sensitivity"]
    assert sens["drivers"][0]["label"] == "fin_spacing"
    assert sens["drivers"][0]["cop"] == 0.61
    assert sens["q2"] == 0.88
    assert facts.details["frozen_factors"] == ["fin_count"]


async def test_convergence_first_and_best() -> None:
    facts = await _facts(_study(config=_SINGLE_CONFIG, best={"q": 45.2}), [])
    conv = facts.details["convergence"]
    assert conv["first"] == 31.0
    assert conv["best"] == 45.2


async def test_n_feasible_summed_from_trace() -> None:
    facts = await _facts(_study(config=_SINGLE_CONFIG, best={"q": 45.2}), [])
    assert facts.details["n_feasible"] == 40  # 6 + 34


async def test_multi_objective_pareto_ranges() -> None:
    config = dict(_SINGLE_CONFIG)
    config["responses"] = [
        {"name": "mass", "goal": "minimise", "spec": {"kind": "scalar"}},
        {"name": "t_max", "goal": "minimise", "spec": {"kind": "scalar"}},
    ]
    best = {
        "pareto_front": [
            {"mass": 0.12, "t_max": 99.0},
            {"mass": 0.21, "t_max": 78.0},
            {"mass": 0.16, "t_max": 88.0},
        ]
    }
    facts = await _facts(_study(config=config, best=best), [])
    assert facts.details["multi_objective"] is True
    assert facts.details["pareto_size"] == 3
    ranges = {r["label"]: (r["min"], r["max"]) for r in facts.details["pareto_ranges"]}
    assert ranges["mass"] == (0.12, 0.21)
    assert ranges["t_max"] == (78.0, 99.0)
