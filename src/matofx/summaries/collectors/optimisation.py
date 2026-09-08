"""Fact collection for an optimisation study.

Reads the frozen ``config_json`` (objectives, constraints, strategy, run mode),
the outcome (``current_best_json`` — a **response-vector keyed by response
name**, ``convergence_trace``, ``stop_reason``), the MOP/CoP sensitivity
(``mop_state_json``), and the per-run results into a rich :class:`SummaryFacts`.

The narrative built on these surfaces the *insight*, not just the setup: the
best design + its objective and constraint values (with whether each constraint
is binding), the Pareto-front spread for multi-objective studies, the CoP
sensitivity drivers, and how far the search improved. Factor / response names
resolve to descriptor labels — a tool target from its pinned version, a workflow
target from its member tools' descriptors (member-qualified ``Tool · field``).
"""

from __future__ import annotations

import math
import operator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from matofx.studies.optimisation.orm import OptimisationStudy
from matofx.summaries.collectors._descriptor import (
    DescriptorMaps,
    descriptor_maps,
    workflow_descriptor_maps,
)
from matofx.summaries.errors import NonTerminalResultError
from matofx.summaries.facts import HeadlineNumber, SummaryFacts, is_terminal

_OPTIMISED_GOALS = ("minimise", "maximise")
_MULTI_OBJECTIVE = 2
_TOP_DRIVERS = 3
_TOP_DESIGN_INPUTS = 4
# A constraint counts as "binding" when the optimum sits within this relative
# band of its limit — the engineering-meaningful "the limit drove the answer".
_BINDING_BAND = 0.03

_OPS = {
    "<=": operator.le,
    "<": operator.lt,
    ">=": operator.ge,
    ">": operator.gt,
    "==": operator.eq,
    "!=": operator.ne,
}


async def collect_optimisation_facts(
    session: AsyncSession,
    *,
    study: OptimisationStudy,
    run_results: list[dict[str, Any]],
) -> SummaryFacts:
    """Build :class:`SummaryFacts` for a terminal optimisation study."""
    if not is_terminal(study.state):
        raise NonTerminalResultError(f"study {study.id} is in state {study.state!r}")

    config = dict(study.config_json or {})
    maps = (
        await workflow_descriptor_maps(session, study.workflow_alias)
        if study.target_kind == "workflow"
        else await descriptor_maps(session, study.tool_version_id)
    )

    objectives = _objectives(config, maps)
    constraints = _constraints(config, maps)
    factor_names = _factor_names(config)
    multi = len(objectives) >= _MULTI_OBJECTIVE
    best = study.current_best_json if isinstance(study.current_best_json, dict) else {}

    trace = list(study.convergence_trace or [])
    feasible = sum(int(t.get("n_feasible", 0)) for t in trace) if trace else None

    details: dict[str, Any] = {
        "strategy": config.get("strategy"),
        "run_mode": config.get("run_mode"),
        "evaluations": len(run_results),
        "n_feasible": feasible,
        "objectives": objectives,
        "constraints": [
            {"label": c["label"], "op": c["op"], "value": c["value"], "unit": c["unit"]}
            for c in constraints
        ],
        "multi_objective": multi,
        "stop_reason": study.stop_reason,
        "frozen_factors": [maps.input_label(n) for n in _frozen(study)],
        "sensitivity": _sensitivity(study, maps),
        "convergence": _convergence(trace),
    }

    headline: tuple[HeadlineNumber, ...] = ()
    if multi:
        details["pareto_size"], details["pareto_ranges"] = _pareto(best, objectives)
    elif objectives:
        outcome = _single_outcome(
            objectives[0], constraints, best, run_results, factor_names, maps
        )
        details.update(outcome)
        if outcome.get("best_value") is not None:
            headline = (
                HeadlineNumber(
                    label=objectives[0]["label"],
                    value=outcome["best_value"],
                    unit=objectives[0]["unit"],
                ),
            )

    return SummaryFacts(
        surface_kind="optimisation_study",
        surface_id=study.id,
        title=study.name or f"Optimisation study {study.id}",
        state=study.state,
        headline=headline,
        submitted_by=None,
        started_at=study.created_at.isoformat() if study.created_at else None,
        finished_at=(
            study.state_changed_at.isoformat() if study.state_changed_at else None
        ),
        duration_seconds=_duration(study),
        details=details,
    )


# --- config parsing ----------------------------------------------------------


def _objectives(config: dict[str, Any], maps: DescriptorMaps) -> list[dict[str, Any]]:
    out = []
    for response in config.get("responses", []):
        if not isinstance(response, dict) or response.get("goal") not in _OPTIMISED_GOALS:
            continue
        name = str(response.get("name", ""))
        out.append(
            {
                "name": name,
                "label": maps.output_label(name) if name else name,
                "sense": response.get("goal"),
                "unit": maps.output_unit(name) if name else None,
            }
        )
    return out


def _constraints(config: dict[str, Any], maps: DescriptorMaps) -> list[dict[str, Any]]:
    out = []
    for constraint in config.get("constraints", []):
        if not isinstance(constraint, dict) or constraint.get("kind") != "output":
            continue
        name = str(constraint.get("response", ""))
        out.append(
            {
                "response": name,
                "label": maps.output_label(name) if name else name,
                "op": constraint.get("op"),
                "value": constraint.get("value"),
                "unit": maps.output_unit(name) if name else None,
            }
        )
    return out


def _factor_names(config: dict[str, Any]) -> list[str]:
    names = []
    for factor in config.get("factors", []):
        if isinstance(factor, dict) and isinstance(factor.get("name"), str):
            names.append(factor["name"])
    return names


# --- single-objective outcome (best design + constraint activity) ------------


def _single_outcome(
    objective: dict[str, Any],
    constraints: list[dict[str, Any]],
    best: dict[str, Any],
    run_results: list[dict[str, Any]],
    factor_names: list[str],
    maps: DescriptorMaps,
) -> dict[str, Any]:
    name = objective["name"]
    # current_best_json is the best-so-far response vector, keyed by response
    # NAME — so the objective value is best[name], and every constraint's value
    # at the optimum is best[constraint.response].
    best_value = _round(best.get(name)) if name in best else None

    best_constraints = []
    for c in constraints:
        at_best = best.get(c["response"])
        if at_best is None:
            continue
        best_constraints.append(
            {
                "label": c["label"],
                "value": _round(at_best),
                "unit": c["unit"],
                "op": c["op"],
                "limit": _round(c["value"]),
                "binding": _is_binding(at_best, c["value"]),
            }
        )

    return {
        "best_value": best_value,
        "best_constraints": best_constraints,
        "best_design": _best_design(
            objective, best_value, run_results, factor_names, maps
        ),
    }


def _best_design(
    objective: dict[str, Any],
    best_value: float | None,
    run_results: list[dict[str, Any]],
    factor_names: list[str],
    maps: DescriptorMaps,
) -> list[dict[str, Any]]:
    """The varied-factor values of the run that achieved the best objective.

    Matched from ``run_results`` (empty in pure-surrogate mode, so this degrades
    to no design — the best value + constraint activity still carry the story).
    """
    if best_value is None or not run_results or not factor_names:
        return []
    name = objective["name"]
    best_row = None
    best_gap = math.inf
    for row in run_results:
        if row.get("state") != "complete":
            continue
        value = row.get("scalars", {}).get(name)
        if not isinstance(value, (int, float)):
            continue
        gap = abs(float(value) - best_value)
        if gap < best_gap:
            best_gap = gap
            best_row = row
    if best_row is None:
        return []
    inputs = best_row.get("inputs", {})
    design = []
    for factor in factor_names[:_TOP_DESIGN_INPUTS]:
        if factor in inputs and isinstance(inputs[factor], (int, float)):
            design.append(
                {
                    "label": maps.input_label(factor),
                    "value": _round(inputs[factor]),
                    "unit": maps.input_unit(factor),
                }
            )
    return design


# --- multi-objective Pareto spread -------------------------------------------


def _pareto(
    best: dict[str, Any], objectives: list[dict[str, Any]]
) -> tuple[int | None, list[dict[str, Any]]]:
    front = best.get("pareto_front")
    if not isinstance(front, list) or not front:
        return None, []
    ranges = []
    for obj in objectives:
        values = [
            float(p[obj["name"]])
            for p in front
            if isinstance(p, dict) and isinstance(p.get(obj["name"]), (int, float))
        ]
        if values:
            ranges.append(
                {
                    "label": obj["label"],
                    "min": _round(min(values)),
                    "max": _round(max(values)),
                    "unit": obj["unit"],
                }
            )
    return len(front), ranges


# --- sensitivity (MOP / CoP) + convergence -----------------------------------


def _sensitivity(study: OptimisationStudy, maps: DescriptorMaps) -> dict[str, Any]:
    mop = study.mop_state_json if isinstance(study.mop_state_json, dict) else {}
    cop_rows = mop.get("cop")
    if not isinstance(cop_rows, list) or not cop_rows:
        return {}
    drivers = [
        {"label": maps.input_label(str(row.get("factor", ""))), "cop": _round(row.get("cop"))}
        for row in cop_rows
        if isinstance(row, dict) and row.get("cop") is not None
    ]
    drivers = [d for d in drivers if d["cop"] is not None][:_TOP_DRIVERS]
    if not drivers:
        return {}
    return {"drivers": drivers, "q2": _round(mop.get("q2"))}


def _convergence(trace: list[dict[str, Any]]) -> dict[str, Any]:
    points = [t for t in trace if isinstance(t, dict) and t.get("metric") is not None]
    if len(points) < _MULTI_OBJECTIVE:
        return {}
    return {
        "first": _round(points[0]["metric"]),
        "best": _round(points[-1]["metric"]),
        "batches": len(trace),
    }


def _frozen(study: OptimisationStudy) -> list[str]:
    mop = study.mop_state_json if isinstance(study.mop_state_json, dict) else {}
    frozen = mop.get("frozen_factors")
    return [str(f) for f in frozen] if isinstance(frozen, list) else []


# --- helpers -----------------------------------------------------------------


def _is_binding(value: Any, limit: Any) -> bool:
    try:
        v, lim = float(value), float(limit)
    except (TypeError, ValueError):
        return False
    scale = max(abs(v), abs(lim), 1.0)
    return abs(v - lim) <= _BINDING_BAND * scale


def _round(value: Any, sig: int = 4) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number == 0:
        return 0.0
    digits = -math.floor(math.log10(abs(number))) + (sig - 1)
    return round(number, digits)


def _duration(study: OptimisationStudy) -> float | None:
    if study.created_at is None or study.state_changed_at is None:
        return None
    return (study.state_changed_at - study.created_at).total_seconds()
