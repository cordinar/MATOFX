"""Unit tests for the structured approval review builders.

The HTTP wiring lives in ``test_approvals.py``; here we pin the *pure*
payload builders that turn a frozen study config / factor spec / surrogate
quality dict into the labelled, unit-carrying review blocks the approver
sees (``designs/approval-detail-completeness.md``). No DB / HTTP — these
are deterministic functions over plain dicts.
"""

from __future__ import annotations

from types import SimpleNamespace

from matofx.api.routers.approvals import (
    _build_optimisation_review,
    _optimisation_footprint,
    _study_factor_variable,
    _surrogate_metrics,
)
from matofx.summaries.collectors._descriptor import DescriptorMaps

_MAPS = DescriptorMaps(
    input_labels={"width": "Fin width", "temp": "Inlet temperature"},
    input_units={"width": "mm", "temp": "K"},
    output_labels={"drop": "Pressure drop", "mass": "Mass"},
    output_units={"drop": "Pa", "mass": "kg"},
    output_order=["drop", "mass"],
)

_CONFIG = {
    "factors": [
        {"name": "width", "type": "float", "min": 1.0, "max": 5.0},
        {"name": "mode", "type": "categorical", "choices": ["a", "b"]},
    ],
    "responses": [
        {"name": "drop", "goal": "minimise", "spec": {"kind": "scalar", "name": "drop"}},
        {"name": "mass", "goal": "track", "spec": {"kind": "scalar", "name": "mass"}},
    ],
    "constraints": [
        {"kind": "output", "response": "mass", "op": "<=", "value": 2.0},
        {"kind": "cross_field", "expr": "width < 4"},
    ],
    "strategy": "bo",
    "acquisition": "balanced",
    "run_mode": "real",
    "batch_size": 3,
    "stopping_criteria": [
        {"kind": "max_runs", "value": 50},
        {"kind": "convergence", "metric": "best_so_far"},
    ],
    "seed": {"kind": "lhs", "n": 10},
    "mop": {"enabled": True, "cop_cutoff": 0.1},
}

_SNAPSHOT = {
    "resources": {"cpu_cores": 4, "ram_mb": 2048, "gpu_memory_mb": 0},
    "licences_required": [{"name": "ansys", "count": 2}],
}


def test_optimisation_review_resolves_labels_units_ranges() -> None:
    review = _build_optimisation_review(_CONFIG, _MAPS, _SNAPSHOT, is_workflow=False)

    # Variables: descriptor labels + units, numeric range, categorical levels.
    width, mode = review.variables
    assert width.label == "Fin width"
    assert width.unit == "mm"
    assert (width.lower, width.upper) == (1.0, 5.0)
    assert width.var_type == "continuous"
    assert mode.var_type == "categorical"
    assert mode.levels == ["a", "b"]

    # Objectives carry the resolved output label + sense.
    senses = {o.label: o.sense for o in review.objectives}
    assert senses == {"Pressure drop": "minimise", "Mass": "track"}

    # Constraints: an output constraint resolves to the response's output
    # label + unit; a cross-field constraint keeps its expression.
    out_con = next(c for c in review.constraints if c.kind == "output")
    assert out_con.label == "Mass"
    assert out_con.op == "<="
    assert out_con.value == 2.0
    assert out_con.unit == "kg"
    cross = next(c for c in review.constraints if c.kind == "cross_field")
    assert cross.expression == "width < 4"


def test_optimisation_review_strategy_and_budget_are_human() -> None:
    review = _build_optimisation_review(_CONFIG, _MAPS, _SNAPSHOT, is_workflow=False)
    assert review.strategy is not None
    assert review.strategy.algorithm == "Bayesian optimisation"  # not "bo"
    assert review.strategy.run_mode == "Real evaluations"
    assert review.strategy.batch_size == 3
    assert review.strategy.variable_reduction  # MOP/CoP enabled
    assert review.stopping is not None
    assert "Max 50 evaluations" in review.stopping.criteria
    assert review.stopping.seeding == "10 LHS seed points"


def test_optimisation_footprint_run_range_and_resources() -> None:
    review = _build_optimisation_review(_CONFIG, _MAPS, _SNAPSHOT, is_workflow=False)
    fp = review.footprint
    assert fp is not None
    # Firm ceiling = max_runs + seed; a convergence criterion can stop
    # earlier, so the floor is seed + one batch.
    assert fp.est_runs_max == 60
    assert fp.est_runs_min == 13
    assert fp.cpu_cores_per_run == 4
    assert fp.ram_mb_per_run == 2048
    assert fp.est_core_hours is None  # no walltime basis → never a guess
    assert [(lic.name, lic.count) for lic in fp.licences] == [("ansys", 2)]


def test_optimisation_footprint_no_convergence_is_firm() -> None:
    config = {**_CONFIG, "stopping_criteria": [{"kind": "max_runs", "value": 50}]}
    fp = _optimisation_footprint(config, _SNAPSHOT, has_convergence=False, is_workflow=False)
    assert fp.est_runs_min == fp.est_runs_max == 60
    assert fp.approximate is False


def test_optimisation_footprint_workflow_is_approximate() -> None:
    fp = _optimisation_footprint(_CONFIG, _SNAPSHOT, has_convergence=False, is_workflow=True)
    assert fp.approximate is True
    assert fp.note


def test_study_factor_variable_range_list_subset_constant() -> None:
    maps = DescriptorMaps({"k": "Conductivity"}, {"k": "W/mK"}, {}, {}, [])

    rng = _study_factor_variable(
        "k",
        {"role": "vary", "type": "numeric", "vary": {"kind": "range", "min": 0.1, "max": 2.5}},
        maps,
    )
    assert rng.label == "Conductivity"
    assert rng.unit == "W/mK"
    assert (rng.lower, rng.upper) == (0.1, 2.5)
    assert rng.role == "vary"

    lst = _study_factor_variable(
        "k",
        {"role": "vary", "type": "numeric", "vary": {"kind": "list", "values": [1, 2, 3]}},
        maps,
    )
    assert lst.levels == [1, 2, 3]

    sub = _study_factor_variable(
        "k",
        {"role": "vary", "type": "select", "vary": {"kind": "subset", "options": ["x", "y"]}},
        maps,
    )
    assert sub.var_type == "categorical"
    assert sub.levels == ["x", "y"]

    const = _study_factor_variable("k", {"role": "constant", "value": 20.0}, maps)
    assert const.role == "constant"
    assert const.value == 20.0


def test_surrogate_metrics_from_quality() -> None:
    version = SimpleNamespace(quality={"drop": {"q2": 0.95, "rmse_cv": 1.2}})
    metrics = _surrogate_metrics(version, _MAPS)  # type: ignore[arg-type]
    by_metric = {m.metric: m for m in metrics}
    assert by_metric["Q² (cross-validated)"].value == 0.95
    assert by_metric["Q² (cross-validated)"].response_label == "Pressure drop"
    assert by_metric["RMSE (CV)"].value == 1.2
    assert by_metric["RMSE (CV)"].unit == "Pa"
