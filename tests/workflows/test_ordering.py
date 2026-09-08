"""Tests for the custom-workflow ordering algorithm.

Covers the pure-function ``compute_order`` plus the cycle detector.
Each test names the shape of the dependency graph it's exercising
(linear, fan-out, merge, disconnected, cycle) so a regression makes
the failure mode obvious.
"""

from __future__ import annotations

from typing import Any

import pytest

from matofx.workflows.ordering import CycleError, compute_order


def _forward(*targets: str) -> dict[str, Any]:
    """Descriptor stub that declares ``downstream_tools``."""
    return {"downstream_tools": list(targets)}


def _consumes(upstream: str, *, field_name: str = "input_1") -> dict[str, Any]:
    """Descriptor stub that declares one ``downstream_input`` field."""
    return {
        "inputs": [
            {
                "name": field_name,
                "downstream_input": True,
                "upstream_tool": upstream,
            }
        ]
    }


# ----- happy paths -------------------------------------------------------


def test_single_tool_returns_as_is() -> None:
    assert compute_order(["a"], {}) == ["a"]


def test_empty_input_returns_empty() -> None:
    assert compute_order([], {}) == []


def test_duplicate_aliases_dedupe_preserving_order() -> None:
    # No edges between them — both should stay in the order they
    # arrived after dedupe.
    assert compute_order(["a", "b", "a"], {}) == ["a", "b"]


def test_linear_chain_via_forward_edges() -> None:
    descriptors = {
        "a": _forward("b"),
        "b": _forward("c"),
        "c": {},
    }
    # Input order intentionally scrambled — algorithm should reorder.
    assert compute_order(["c", "a", "b"], descriptors) == ["a", "b", "c"]


def test_linear_chain_via_backward_edges() -> None:
    """A tool's input.upstream_tool alone is enough to imply an edge."""
    descriptors = {
        "a": {},
        "b": _consumes("a"),
        "c": _consumes("b"),
    }
    assert compute_order(["c", "a", "b"], descriptors) == ["a", "b", "c"]


def test_forward_and_backward_edges_agree() -> None:
    """Both signals pointing the same way collapse into one edge."""
    descriptors = {
        "a": _forward("b"),
        "b": _consumes("a"),
    }
    assert compute_order(["b", "a"], descriptors) == ["a", "b"]


def test_merge_node_runs_after_all_upstreams() -> None:
    """C consumes from both A and B; C must follow both."""
    descriptors = {
        "a": _forward("c"),
        "b": _forward("c"),
        "c": {},
    }
    result = compute_order(["a", "b", "c"], descriptors)
    assert result.index("c") > result.index("a")
    assert result.index("c") > result.index("b")


def test_fan_out_keeps_branches_after_root() -> None:
    """A → B, A → C: both B and C come after A."""
    descriptors = {
        "a": _forward("b", "c"),
        "b": {},
        "c": {},
    }
    result = compute_order(["a", "b", "c"], descriptors)
    assert result[0] == "a"
    assert set(result[1:]) == {"b", "c"}


def test_disconnected_tools_appear_in_alias_order() -> None:
    """No declared edges between any of them → alias-sorted output."""
    descriptors = {alias: {} for alias in ("beta", "alpha", "gamma")}
    assert compute_order(["gamma", "beta", "alpha"], descriptors) == [
        "alpha",
        "beta",
        "gamma",
    ]


def test_chain_contiguity_preserved_under_mixed_inputs() -> None:
    """Linear chain stays contiguous even when other tools are mixed in.

    Mirrors the design intent: a user picks "Material Properties →
    Heat Transfer" plus an unrelated "QA Check" — the chain shouldn't
    get split by the unrelated tool landing in the middle.
    """
    descriptors = {
        "material_properties": _forward("heat_transfer"),
        "heat_transfer": {},
        "qa_check": {},
    }
    result = compute_order(
        ["qa_check", "heat_transfer", "material_properties"], descriptors
    )
    assert result.index("heat_transfer") == result.index("material_properties") + 1


def test_unselected_upstream_ignored() -> None:
    """downstream_tools entries referring to tools the user didn't pick are dropped."""
    descriptors = {
        "a": _forward("absent_tool"),  # absent_tool not in selection
        "b": {},
    }
    # No edges → alias-sorted.
    assert compute_order(["b", "a"], descriptors) == ["a", "b"]


def test_input_without_downstream_flag_ignored() -> None:
    """``upstream_tool`` with ``downstream_input=False`` doesn't make an edge."""
    descriptors = {
        "a": {},
        "b": {
            "inputs": [
                {
                    "name": "manual_field",
                    "downstream_input": False,
                    "upstream_tool": "a",
                }
            ]
        },
    }
    # No edge inferred → alias order applies.
    assert compute_order(["b", "a"], descriptors) == ["a", "b"]


def test_missing_descriptor_treated_as_no_dependencies() -> None:
    """Tool with no entry in the descriptor map is fine — just no edges."""
    descriptors = {"a": _forward("b")}
    # "b" has no descriptor entry — still placed after "a".
    assert compute_order(["a", "b"], descriptors) == ["a", "b"]


def test_self_loop_in_descriptor_is_ignored() -> None:
    """A tool listing itself in downstream_tools is silently dropped."""
    descriptors = {"a": _forward("a", "b"), "b": {}}
    assert compute_order(["a", "b"], descriptors) == ["a", "b"]


# ----- cycle detection ---------------------------------------------------


def test_two_node_cycle_raises() -> None:
    descriptors = {
        "a": _forward("b"),
        "b": _forward("a"),
    }
    with pytest.raises(CycleError) as exc_info:
        compute_order(["a", "b"], descriptors)
    assert set(exc_info.value.cycle) == {"a", "b"}


def test_three_node_cycle_raises() -> None:
    descriptors = {
        "a": _forward("b"),
        "b": _forward("c"),
        "c": _forward("a"),
    }
    with pytest.raises(CycleError) as exc_info:
        compute_order(["a", "b", "c"], descriptors)
    assert set(exc_info.value.cycle) == {"a", "b", "c"}


def test_cycle_error_message_names_the_tools() -> None:
    """The CycleError message should help the user fix the problem."""
    descriptors = {"a": _forward("b"), "b": _forward("a")}
    with pytest.raises(CycleError, match="loop"):
        compute_order(["a", "b"], descriptors)


def test_cycle_in_subgraph_still_errors() -> None:
    """A cycle in part of the graph doesn't get masked by a clean DAG elsewhere."""
    descriptors = {
        "x": {},  # disconnected, clean
        "a": _forward("b"),
        "b": _forward("a"),
    }
    with pytest.raises(CycleError):
        compute_order(["x", "a", "b"], descriptors)
