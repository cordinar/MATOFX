"""Unit tests for the workflow-target config gate (ADR-0009 §3.3 rule 9 / §3.4).

Pure-function tests — no DB. A two-member chain (``mat`` → ``heat``) with
``heat.youngs_modulus`` wired from ``mat``; factors are member-qualified
open inputs and responses name a real ``{member, output}``.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from matofx.studies.optimisation import OptimisationConfigError
from matofx.studies.optimisation.config import validate_workflow_config
from matofx.tools.descriptor import InputField, OutputField, SumCheck, ToolDescriptor


def _members() -> dict[str, ToolDescriptor]:
    mat = ToolDescriptor(
        alias="mat",
        name="Material",
        version="1.0.0",
        description="material props",
        main_script="static/main.py",
        inputs=[InputField(name="alloy", label="Alloy", type="select", options=["al", "cu"])],
        outputs=[OutputField(name="youngs_modulus", label="E", type="number")],
    )
    heat = ToolDescriptor(
        alias="heat",
        name="Heat",
        version="1.0.0",
        description="thermal",
        main_script="static/main.py",
        inputs=[
            InputField(name="mesh_size", label="Mesh", type="float", minimum=0.1, maximum=5.0),
            InputField(
                name="youngs_modulus",
                label="E",
                type="float",
                downstream_input=True,
                upstream_tool="mat",
            ),
        ],
        outputs=[OutputField(name="max_temp", label="Max temp", type="number")],
    )
    return {"mat": mat, "heat": heat}


def _good_config() -> dict[str, Any]:
    return {
        "target": {"kind": "workflow", "workflow_alias": "mh", "workflow_version_id": 1},
        "factors": [
            {
                "name": "mat.alloy",
                "member": "mat",
                "input": "alloy",
                "type": "categorical",
                "choices": ["al", "cu"],
            },
            {
                "name": "heat.mesh",
                "member": "heat",
                "input": "mesh_size",
                "type": "float",
                "min": 0.5,
                "max": 5.0,
            },
        ],
        "responses": [
            {
                "name": "temp",
                "goal": "minimise",
                "spec": {"kind": "scalar", "member": "heat", "name": "max_temp"},
            },
        ],
        "strategy": "bo",
        "run_mode": "real",
        "batch_size": 2,
        "seed": {"kind": "lhs", "n": 6},
        "stopping_criteria": [{"kind": "max_runs", "value": 30}],
    }


def _rejects(config: dict[str, Any], fragment: str) -> None:
    with pytest.raises(OptimisationConfigError) as exc:
        validate_workflow_config(config, _members())
    assert fragment.lower() in str(exc.value).lower()


def test_good_workflow_config_validates() -> None:
    validate_workflow_config(_good_config(), _members())


def test_factor_on_wired_input_rejected() -> None:
    cfg = _good_config()
    # youngs_modulus on heat is wired from mat → not an open input.
    cfg["factors"].append(
        {
            "name": "heat.E",
            "member": "heat",
            "input": "youngs_modulus",
            "type": "float",
            "min": 1.0,
            "max": 2.0,
        }
    )
    _rejects(cfg, "isn't an *open* input")


def test_factor_on_unknown_member_rejected() -> None:
    cfg = _good_config()
    cfg["factors"][0]["member"] = "nope"
    _rejects(cfg, "isn't a tool in this workflow")


def test_factor_type_mismatch_rejected() -> None:
    cfg = _good_config()
    cfg["factors"][1]["type"] = "categorical"  # mesh_size is a float input
    _rejects(cfg, "is a 'float' field")


def test_response_on_unknown_output_rejected() -> None:
    cfg = _good_config()
    cfg["responses"][0]["spec"]["name"] = "not_an_output"
    _rejects(cfg, "doesn't produce")


def test_response_on_unknown_member_rejected() -> None:
    cfg = _good_config()
    cfg["responses"][0]["spec"]["member"] = "ghost"
    _rejects(cfg, "isn't a tool in this workflow")


def test_no_factors_rejected() -> None:
    cfg = _good_config()
    cfg["factors"] = []
    _rejects(cfg, "at least one factor")


def test_good_config_not_mutated() -> None:
    cfg = _good_config()
    before = copy.deepcopy(cfg)
    validate_workflow_config(cfg, _members())
    assert cfg == before


# --- fixed inputs (§3.5) ----------------------------------------------------


def _members_with_extra_required() -> dict[str, ToolDescriptor]:
    members = _members()
    heat = members["heat"]
    members["heat"] = heat.model_copy(
        update={
            "inputs": [
                *heat.inputs,
                InputField(
                    name="coolant",
                    label="Coolant",
                    type="material",
                    required=True,
                    material_class="Fluid",
                ),
            ]
        }
    )
    return members


def test_required_open_input_needs_fixed_value() -> None:
    # An open required member input the search isn't varying must be held.
    members = _members_with_extra_required()
    cfg = _good_config()  # varies mat.alloy + heat.mesh_size, not heat.coolant
    with pytest.raises(OptimisationConfigError) as exc:
        validate_workflow_config(cfg, members)
    assert "fixed value" in str(exc.value).lower()
    assert "coolant" in str(exc.value).lower()
    # Held constant under its member-qualified key → clean.
    cfg["fixed_inputs"] = {"heat.coolant": "7"}
    validate_workflow_config(cfg, members)


def test_wired_input_never_needs_a_fixed_value() -> None:
    # ``heat.youngs_modulus`` is required but wired from ``mat`` — it's filled
    # from upstream, so the good config (no fixed_inputs) already validates.
    # Make that exemption explicit.
    validate_workflow_config(_good_config(), _members())


# --- sum checks (§3.6) ------------------------------------------------------


def _members_with_sum() -> dict[str, ToolDescriptor]:
    """``mat`` gains two percentages summing to 100; ``heat`` is reused as-is."""
    sc = SumCheck(value=100.0, sum_parameters=["p1", "p2"])
    pct = {"type": "float", "minimum": 0.0, "maximum": 100.0, "sum_check": sc}
    mat = ToolDescriptor(
        alias="mat",
        name="Material",
        version="1.0.0",
        description="material props",
        main_script="static/main.py",
        inputs=[
            InputField(name="p1", label="P1", **pct),
            InputField(name="p2", label="P2", **pct),
        ],
        outputs=[OutputField(name="youngs_modulus", label="E", type="number")],
    )
    return {"mat": mat, "heat": _members()["heat"]}


def _sum_workflow_config() -> dict[str, Any]:
    return {
        "target": {"kind": "workflow", "workflow_alias": "mh", "workflow_version_id": 1},
        "factors": [
            {
                "name": "mat.p1",
                "member": "mat",
                "input": "p1",
                "type": "float",
                "min": 0,
                "max": 100,
            },  # noqa: E501
            {
                "name": "mat.p2",
                "member": "mat",
                "input": "p2",
                "type": "float",
                "min": 0,
                "max": 100,
            },  # noqa: E501
            {
                "name": "heat.mesh",
                "member": "heat",
                "input": "mesh_size",
                "type": "float",
                "min": 0.5,
                "max": 5.0,
            },
        ],
        "responses": [
            {
                "name": "temp",
                "goal": "minimise",
                "spec": {"kind": "scalar", "member": "heat", "name": "max_temp"},
            }
        ],
        "strategy": "bo",
        "run_mode": "real",
        "batch_size": 1,
        "seed": {"kind": "lhs", "n": 4},
        "stopping_criteria": [{"kind": "max_runs", "value": 20}],
    }


def test_workflow_sum_group_uses_member_qualified_keys() -> None:
    groups = validate_workflow_config(_sum_workflow_config(), _members_with_sum())
    assert len(groups) == 1
    grp = groups[0]
    assert grp["value"] == 100.0
    assert {m["factor"] for m in grp["members"]} == {"mat.p1", "mat.p2"}
    assert all(m["role"] == "varied" for m in grp["members"])
    assert grp["balance"] in {"mat.p1", "mat.p2"}


def test_workflow_sum_group_infeasible_rejected() -> None:
    cfg = _sum_workflow_config()
    for factor in cfg["factors"][:2]:  # cap both percentages at 10 → can't reach 100
        factor["max"] = 10.0
    with pytest.raises(OptimisationConfigError, match="sum to 100"):
        validate_workflow_config(cfg, _members_with_sum())
