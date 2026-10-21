"""Exhaustive unit tests for the optimisation ``validate_config`` gate (ADR-0009 §3.3).

Pure-function tests — no DB. Each builds a known-good config against a
fixed heat-sink-style descriptor, mutates one rule's worth of it, and
asserts the precise :class:`OptimisationConfigError` fires. The
happy-path case proves the good config validates clean.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from matofx.studies.optimisation import OptimisationConfigError, validate_config
from matofx.studies.optimisation.config import is_weighted_sum, weighted_sum_value
from matofx.tools.descriptor import InputField, OutputField, SumCheck, ToolDescriptor


def _descriptor() -> ToolDescriptor:
    """A heat-sink-style tool: 3 factors (float/int/select) + 2 outputs."""
    return ToolDescriptor(
        alias="heat_sink",
        name="Heat Sink",
        version="1.0.0",
        description="thermal study",
        main_script="static/main.py",
        inputs=[
            InputField(
                name="fin_height", label="Fin height", type="float", minimum=5.0, maximum=40.0
            ),
            InputField(name="fin_count", label="Fin count", type="integer", minimum=4, maximum=60),
            InputField(
                name="material", label="Material", type="select", options=["al", "cu", "steel"]
            ),
        ],
        outputs=[
            OutputField(name="mass", label="Mass", type="number"),
            OutputField(name="max_temperature", label="Max temperature", type="number"),
        ],
    )


def _good_config() -> dict[str, Any]:
    """A launch-ready single-objective constrained config."""
    return {
        "target": {"kind": "tool", "tool_alias": "heat_sink", "tool_version_id": 1},
        "factors": [
            {"name": "fin_height", "type": "float", "min": 10.0, "max": 30.0},
            {"name": "fin_count", "type": "int", "min": 8, "max": 40},
            {"name": "material", "type": "categorical", "choices": ["al", "cu"]},
        ],
        "responses": [
            {"name": "mass", "spec": {"kind": "scalar"}, "goal": "minimise"},
            {"name": "max_temperature", "spec": {"kind": "scalar"}, "goal": "track"},
        ],
        "constraints": [
            {"kind": "cross_field", "expr": "fin_height * fin_count <= 1200"},
            {"kind": "output", "response": "max_temperature", "op": "<=", "value": 85.0},
        ],
        "strategy": "bo",
        "run_mode": "real",
        "batch_size": 4,
        "concurrency_cap": 4,
        "seed": {"kind": "lhs", "n": 25},
        "stopping_criteria": [
            {"kind": "max_runs", "value": 200},
            {"kind": "convergence", "metric": "best_so_far", "window": 15, "epsilon": 1e-3},
        ],
    }


def test_good_config_validates_clean() -> None:
    validate_config(_good_config(), _descriptor())


def _assert_rejects(config: dict[str, Any], fragment: str) -> None:
    with pytest.raises(OptimisationConfigError) as exc:
        validate_config(config, _descriptor())
    assert fragment.lower() in str(exc.value).lower()


def test_workflow_target_rejected() -> None:
    cfg = _good_config()
    cfg["target"] = {"kind": "workflow", "workflow_alias": "chain", "workflow_version_id": 1}
    _assert_rejects(cfg, "workflow-target optimisation isn't available")


def test_no_factors_rejected() -> None:
    cfg = _good_config()
    cfg["factors"] = []
    _assert_rejects(cfg, "at least one factor")


def test_unknown_factor_rejected() -> None:
    cfg = _good_config()
    cfg["factors"][0]["name"] = "not_an_input"
    _assert_rejects(cfg, "isn't an input")


def test_duplicate_factor_rejected() -> None:
    cfg = _good_config()
    cfg["factors"].append({"name": "fin_height", "type": "float", "min": 11.0, "max": 12.0})
    _assert_rejects(cfg, "more than once")


def test_factor_type_mismatch_rejected() -> None:
    cfg = _good_config()
    # fin_height is a float input, declared int here.
    cfg["factors"][0]["type"] = "int"
    _assert_rejects(cfg, "but the tool input is")


def test_factor_bounds_widen_below_min_rejected() -> None:
    cfg = _good_config()
    cfg["factors"][0]["min"] = 1.0  # below the descriptor minimum 5.0
    _assert_rejects(cfg, "below the tool's minimum")


def test_factor_bounds_widen_above_max_rejected() -> None:
    cfg = _good_config()
    cfg["factors"][0]["max"] = 99.0  # above the descriptor maximum 40.0
    _assert_rejects(cfg, "exceeds the tool's maximum")


def test_factor_max_less_than_min_rejected() -> None:
    cfg = _good_config()
    cfg["factors"][1]["min"], cfg["factors"][1]["max"] = 40, 8
    _assert_rejects(cfg, "max < min")


def test_integer_factor_fractional_min_rejected() -> None:
    cfg = _good_config()
    cfg["factors"][1]["min"] = 8.5  # fin_count is an integer input
    _assert_rejects(cfg, "must be a whole number")


def test_integer_factor_fractional_max_rejected() -> None:
    cfg = _good_config()
    cfg["factors"][1]["max"] = 39.9  # fin_count is an integer input
    _assert_rejects(cfg, "must be a whole number")


def test_integer_factor_whole_float_bounds_accepted() -> None:
    # 8.0 / 40.0 are whole numbers expressed as floats — fine.
    cfg = _good_config()
    cfg["factors"][1]["min"], cfg["factors"][1]["max"] = 8.0, 40.0
    validate_config(cfg, _descriptor())


def test_categorical_choice_not_offered_rejected() -> None:
    cfg = _good_config()
    cfg["factors"][2]["choices"] = ["al", "titanium"]
    _assert_rejects(cfg, "doesn't offer")


def test_no_responses_rejected() -> None:
    cfg = _good_config()
    cfg["responses"] = []
    _assert_rejects(cfg, "at least one response")


def test_no_optimised_response_rejected() -> None:
    cfg = _good_config()
    for response in cfg["responses"]:
        response["goal"] = "track"
    _assert_rejects(cfg, "minimise or maximise")


def test_bad_response_goal_rejected() -> None:
    cfg = _good_config()
    cfg["responses"][0]["goal"] = "optimise"
    _assert_rejects(cfg, "use 'minimise'")


def test_scalar_response_unknown_output_rejected() -> None:
    cfg = _good_config()
    cfg["responses"][0]["spec"] = {"kind": "scalar", "name": "no_such_output"}
    _assert_rejects(cfg, "the tool doesn't produce")


def test_csv_column_response_missing_column_rejected() -> None:
    cfg = _good_config()
    cfg["responses"][0]["spec"] = {"kind": "csv_column", "file": "dp.csv"}
    _assert_rejects(cfg, "missing 'file' or 'column'")


def test_unknown_strategy_rejected() -> None:
    cfg = _good_config()
    cfg["strategy"] = "magic"
    _assert_rejects(cfg, "isn't one matofx offers")


def test_multi_objective_strategy_on_single_objective_rejected() -> None:
    cfg = _good_config()
    cfg["strategy"] = "nsga2"  # multi-only, but only one optimised response
    _assert_rejects(cfg, "doesn't support a single objective")


def test_single_objective_strategy_on_multi_objective_is_weighted_sum() -> None:
    # A single-objective strategy with two or more optimised responses is now a
    # weighted-sum search (scalarised via the per-objective weights), not an
    # error — the objectives are combined into one scalar the search drives.
    cfg = _good_config()
    cfg["responses"][1]["goal"] = "minimise"  # now two optimised responses
    cfg["responses"][0]["weight"] = 2.0
    cfg["strategy"] = "bo"  # single-only
    validate_config(cfg, _descriptor())  # no raise
    assert is_weighted_sum(cfg) is True


def test_weighted_sum_value_combines_with_signs_and_weights() -> None:
    cfg = _good_config()
    cfg["responses"][1]["goal"] = "minimise"
    cfg["responses"][0]["weight"] = 2.0
    cfg["strategy"] = "bo"
    r0 = str(cfg["responses"][0]["spec"].get("name") or cfg["responses"][0]["name"])
    r1 = str(cfg["responses"][1]["spec"].get("name") or cfg["responses"][1]["name"])
    # both minimise → Σ wᵢ·fᵢ = 2·10 + 1·5 = 25
    assert weighted_sum_value({r0: 10.0, r1: 5.0}, cfg) == 25.0
    # a missing objective → None (failed run, skipped by the optimiser)
    assert weighted_sum_value({r0: 10.0}, cfg) is None


def test_bad_acquisition_mode_rejected() -> None:
    cfg = _good_config()
    cfg["acquisition"] = "teleport"
    _assert_rejects(cfg, "acquisition mode")


def test_explore_acquisition_accepted() -> None:
    cfg = _good_config()
    cfg["acquisition"] = "explore"
    validate_config(cfg, _descriptor())


def test_cross_field_unknown_name_rejected() -> None:
    cfg = _good_config()
    cfg["constraints"][0]["expr"] = "fin_height * gizmo <= 1200"
    _assert_rejects(cfg, "aren't factors")


def test_cross_field_uncompilable_rejected() -> None:
    cfg = _good_config()
    cfg["constraints"][0]["expr"] = "fin_height *"
    _assert_rejects(cfg, "won't compile")


def test_output_constraint_unknown_response_rejected() -> None:
    cfg = _good_config()
    cfg["constraints"][1]["response"] = "pressure_drop"
    _assert_rejects(cfg, "undeclared response")


def test_missing_max_runs_rejected() -> None:
    cfg = _good_config()
    cfg["stopping_criteria"] = [
        {"kind": "convergence", "metric": "best_so_far", "window": 15, "epsilon": 1e-3}
    ]
    _assert_rejects(cfg, "max_runs")


def test_hypervolume_on_single_objective_rejected() -> None:
    cfg = _good_config()
    cfg["stopping_criteria"][1] = {"kind": "convergence", "metric": "hypervolume", "window": 5}
    _assert_rejects(cfg, "multi-objective")


def test_batch_size_zero_rejected() -> None:
    cfg = _good_config()
    cfg["batch_size"] = 0
    _assert_rejects(cfg, "batch_size")


def test_seed_n_zero_rejected() -> None:
    cfg = _good_config()
    cfg["seed"]["n"] = 0
    _assert_rejects(cfg, "seed batch size")


def test_cohort_seed_needs_no_n() -> None:
    """A cohort seed imports prior runs, so it carries no batch size."""
    cfg = _good_config()
    cfg["seed"] = {"kind": "cohort", "cohort_id": 1}
    validate_config(cfg, _descriptor())


def test_cohort_seed_missing_cohort_id_rejected() -> None:
    cfg = _good_config()
    cfg["seed"] = {"kind": "cohort"}
    _assert_rejects(cfg, "cohort_id")


def test_seeded_strategy_missing_seed_rejected() -> None:
    cfg = _good_config()
    del cfg["seed"]
    _assert_rejects(cfg, "needs an initial seed")


def test_surrogate_mode_without_model_rejected() -> None:
    cfg = _good_config()
    cfg["run_mode"] = "surrogate"
    _assert_rejects(cfg, "attached surrogate model")


def test_surrogate_assisted_real_deferred() -> None:
    cfg = _good_config()
    cfg["run_mode"] = "surrogate_assisted_real"
    cfg["surrogate_model_version_id"] = 1
    _assert_rejects(cfg, "isn't available yet")


def test_bad_run_mode_rejected() -> None:
    cfg = _good_config()
    cfg["run_mode"] = "telepathy"
    _assert_rejects(cfg, "run mode")


def test_grid_strategy_needs_no_seed() -> None:
    """Space-filling strategies (grid/random) are exempt from the seed rule."""
    cfg = _good_config()
    cfg["strategy"] = "grid"
    del cfg["seed"]
    validate_config(cfg, _descriptor())


def test_multi_objective_nsga2_validates() -> None:
    cfg = _good_config()
    cfg["responses"][1]["goal"] = "minimise"  # two optimised responses
    cfg["strategy"] = "nsga2"
    cfg["stopping_criteria"][1] = {
        "kind": "convergence",
        "metric": "hypervolume",
        "window": 10,
        "epsilon": 1e-3,
    }
    validate_config(cfg, _descriptor())


def test_mop_enabled_with_bo_validates() -> None:
    cfg = _good_config()  # strategy is "bo"
    cfg["mop"] = {"enabled": True, "cop_cutoff": 0.02}
    validate_config(cfg, _descriptor())


def test_mop_enabled_with_non_bo_rejected() -> None:
    cfg = _good_config()
    cfg["strategy"] = "cmaes"  # single-objective, but not surrogate-search
    cfg["mop"] = {"enabled": True}
    _assert_rejects(cfg, "surrogate-search ('bo')")


def test_mop_bad_cutoff_rejected() -> None:
    cfg = _good_config()
    cfg["mop"] = {"enabled": True, "cop_cutoff": 1.5}
    _assert_rejects(cfg, "cop_cutoff")


def test_mop_disabled_is_ignored() -> None:
    cfg = _good_config()
    cfg["strategy"] = "cmaes"
    cfg["mop"] = {"enabled": False}  # off → no bo requirement
    validate_config(cfg, _descriptor())


def test_good_config_not_mutated_by_validation() -> None:
    """Defensive: validation must not mutate the caller's config."""
    cfg = _good_config()
    before = copy.deepcopy(cfg)
    validate_config(cfg, _descriptor())
    assert cfg == before


# --- fixed inputs (§3.5) ----------------------------------------------------


def _descriptor_with_required(extra: InputField) -> ToolDescriptor:
    base = _descriptor()
    return base.model_copy(update={"inputs": [*base.inputs, extra]})


def test_required_non_factor_input_needs_fixed_value() -> None:
    # A required input the search isn't varying must be held constant; without
    # a value the study can't start (the real bug: it produced only failed runs).
    desc = _descriptor_with_required(
        InputField(
            name="coolant", label="Coolant", type="material", required=True, material_class="Fluid"
        )
    )
    cfg = _good_config()  # varies fin_height / fin_count / material — not coolant
    with pytest.raises(OptimisationConfigError) as exc:
        validate_config(cfg, desc)
    assert "fixed value" in str(exc.value).lower()
    assert "coolant" in str(exc.value).lower()
    # Holding it constant clears the gate.
    cfg["fixed_inputs"] = {"coolant": "7"}
    validate_config(cfg, desc)


def test_optional_non_factor_input_needs_no_fixed_value() -> None:
    # Only *required* unvaried inputs must be held — optional ones fall back
    # to the tool's own default.
    desc = _descriptor_with_required(
        InputField(name="note", label="Note", type="string", required=False)
    )
    validate_config(_good_config(), desc)


def test_blank_fixed_value_is_treated_as_unset() -> None:
    desc = _descriptor_with_required(
        InputField(
            name="coolant", label="Coolant", type="material", required=True, material_class="Fluid"
        )
    )
    cfg = _good_config()
    cfg["fixed_inputs"] = {"coolant": ""}  # blank ≠ a real choice
    with pytest.raises(OptimisationConfigError, match="fixed value"):
        validate_config(cfg, desc)


# --- sum checks (§3.6) ------------------------------------------------------


def _mix_descriptor() -> ToolDescriptor:
    """Three percentages summing to 100 (each declares the shared sum-check) + a free input."""
    sc = SumCheck(value=100.0, sum_parameters=["a_pct", "b_pct", "c_pct"])
    pct = {"type": "float", "minimum": 0.0, "maximum": 100.0, "sum_check": sc}
    return ToolDescriptor(
        alias="mix",
        name="Mix",
        version="1.0.0",
        description="blend",
        main_script="static/main.py",
        inputs=[
            InputField(name="a_pct", label="A %", **pct),
            InputField(name="b_pct", label="B %", **pct),
            InputField(name="c_pct", label="C %", **pct),
            InputField(
                name="speed", label="Speed", type="float", minimum=0.0, maximum=10.0, required=False
            ),
        ],
        outputs=[OutputField(name="y", label="Y", type="number")],
    )


_MIX_BOUNDS = {
    "a_pct": (0.0, 100.0),
    "b_pct": (0.0, 100.0),
    "c_pct": (0.0, 100.0),
    "speed": (0.0, 10.0),
}


def _mix_config(*factor_names: str) -> dict[str, Any]:
    names = factor_names or ("a_pct", "b_pct", "c_pct")
    return {
        "target": {"kind": "tool", "tool_alias": "mix", "tool_version_id": 1},
        "factors": [
            {"name": n, "type": "float", "min": _MIX_BOUNDS[n][0], "max": _MIX_BOUNDS[n][1]}
            for n in names
        ],
        "responses": [{"name": "y", "spec": {"kind": "scalar"}, "goal": "minimise"}],
        "strategy": "bo",
        "run_mode": "real",
        "batch_size": 1,
        "seed": {"kind": "lhs", "n": 4},
        "stopping_criteria": [{"kind": "max_runs", "value": 20}],
    }


def test_sum_group_derived_when_all_members_varied() -> None:
    groups = validate_config(_mix_config(), _mix_descriptor())
    assert len(groups) == 1
    grp = groups[0]
    assert grp["value"] == 100.0
    assert {m["factor"] for m in grp["members"]} == {"a_pct", "b_pct", "c_pct"}
    assert all(m["role"] == "varied" for m in grp["members"])
    # Equal ranges → name tie-break makes 'a_pct' the balance.
    assert grp["balance"] == "a_pct"


def test_sum_group_dedupes_to_one_record() -> None:
    # The sum-check is declared on all three inputs; only one group emerges.
    groups = validate_config(_mix_config(), _mix_descriptor())
    assert len(groups) == 1


def test_sum_group_with_a_fixed_member() -> None:
    cfg = _mix_config("a_pct", "b_pct")  # vary two, hold the third
    cfg["fixed_inputs"] = {"c_pct": 20.0}
    grp = validate_config(cfg, _mix_descriptor())[0]
    roles = {m["factor"]: m["role"] for m in grp["members"]}
    assert roles == {"a_pct": "varied", "b_pct": "varied", "c_pct": "fixed"}
    held = next(m for m in grp["members"] if m["factor"] == "c_pct")
    assert held["fixed_value"] == 20.0
    assert grp["balance"] in {"a_pct", "b_pct"}


def test_sum_group_infeasible_ranges_rejected() -> None:
    cfg = _mix_config()
    for factor in cfg["factors"]:
        factor["max"] = 10.0  # three values each ≤ 10 can never reach 100
    with pytest.raises(OptimisationConfigError, match="sum to 100"):
        validate_config(cfg, _mix_descriptor())


def test_sum_group_all_fixed_must_total_value() -> None:
    cfg = _mix_config("speed")  # vary the free input; hold all three percentages
    cfg["fixed_inputs"] = {"a_pct": 30.0, "b_pct": 30.0, "c_pct": 30.0}  # totals 90
    with pytest.raises(OptimisationConfigError, match="total 90"):
        validate_config(cfg, _mix_descriptor())


def test_no_sum_check_yields_no_groups() -> None:
    assert validate_config(_good_config(), _descriptor()) == []
