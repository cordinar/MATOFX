"""Unit tests for the sum-check derivation math (ADR-0009 §3.6).

Pure functions — no DB. A sum-checked group's *derived* (balance) member is
worked out from the others so the group totals its value exactly; an
out-of-range or non-integral residual makes the candidate infeasible (dropped,
never clamped into a wrong sum). The derived member's bounds live on the group
record, so the math needs no factor-spec map.
"""

from __future__ import annotations

from dataclasses import dataclass

from matofx.studies.optimisation.sumcheck import (
    apply_sum_derivation,
    derive_balance,
    pick_balance_heuristic,
    project_sum_groups,
    sum_check_notes,
)


@dataclass(frozen=True)
class _Spec:
    """Minimal stand-in for ``FactorSpec`` (only ``pick_balance_heuristic`` reads it)."""

    kind: str
    low: float | None
    high: float | None


def _group(
    balance: str = "c",
    value: float = 100.0,
    *,
    kind: str = "float",
    low: float = 0.0,
    high: float = 100.0,
) -> dict:
    return {
        "value": value,
        "members": [
            {"factor": "a", "role": "varied", "fixed_value": None},
            {"factor": "b", "role": "varied", "fixed_value": None},
            {"factor": "c", "role": "varied", "fixed_value": None},
        ],
        "balance": balance,
        "balance_kind": kind,
        "balance_low": low,
        "balance_high": high,
    }


def test_derive_sets_balance_to_residual() -> None:
    # Free members a, b given; c (the derived one) isn't in the candidate yet.
    cand = {"a": 30.0, "b": 25.0}
    new, ok = derive_balance(cand, _group(), frozen={})
    assert ok
    assert new["c"] == 45.0
    assert new["a"] + new["b"] + new["c"] == 100.0
    assert "c" not in cand  # input never mutated


def test_derive_infeasible_when_residual_below_zero() -> None:
    # a + b already exceed 100 → c would be negative, out of [0, 100].
    _new, ok = derive_balance({"a": 70.0, "b": 50.0}, _group(), frozen={})
    assert ok is False


def test_derive_counts_fixed_member() -> None:
    group = {
        "value": 100.0,
        "members": [
            {"factor": "a", "role": "varied", "fixed_value": None},
            {"factor": "b", "role": "fixed", "fixed_value": 20.0},
            {"factor": "c", "role": "varied", "fixed_value": None},
        ],
        "balance": "c",
        "balance_kind": "float",
        "balance_low": 0.0,
        "balance_high": 100.0,
    }
    new, ok = derive_balance({"a": 30.0}, group, frozen={})
    assert ok
    assert new["c"] == 50.0  # 100 - 30 (a) - 20 (fixed b)


def test_derive_frozen_value_wins_over_candidate() -> None:
    # b is MOP-frozen at 10 — that nominal counts, not the candidate's 25.
    new, ok = derive_balance({"a": 30.0, "b": 25.0}, _group(), frozen={"b": 10.0})
    assert ok
    assert new["c"] == 60.0  # 100 - 30 - 10


def test_derive_integer_balance_requires_integral_residual() -> None:
    group = _group(value=10.0, kind="int", low=0, high=10)
    ok_new, ok = derive_balance({"a": 3, "b": 4}, group, frozen={})
    assert ok
    assert ok_new["c"] == 3
    # A fractional residual can't be absorbed by an integer balance.
    _bad, bad_ok = derive_balance({"a": 3.5, "b": 4.0}, group, frozen={})
    assert bad_ok is False


def test_balance_none_feasible_only_if_fixed_members_total_value() -> None:
    group = {
        "value": 100.0,
        "members": [
            {"factor": "a", "role": "fixed", "fixed_value": 60.0},
            {"factor": "b", "role": "fixed", "fixed_value": 40.0},
        ],
        "balance": None,
    }
    _new, ok = derive_balance({}, group, frozen={})
    assert ok
    group["members"][1]["fixed_value"] = 30.0  # totals 90, not 100
    _new2, ok2 = derive_balance({}, group, frozen={})
    assert ok2 is False


def test_pick_balance_heuristic_prefers_widest_float() -> None:
    specs = {
        "a": _Spec("float", 0.0, 10.0),
        "b": _Spec("float", 0.0, 100.0),  # widest float
        "c": _Spec("int", 0, 1000),  # wider, but int → not preferred
    }
    assert pick_balance_heuristic(["a", "b", "c"], specs) == "b"
    # Determinism: equal-width floats break on the name.
    eq = {"x": _Spec("float", 0.0, 50.0), "y": _Spec("float", 0.0, 50.0)}
    assert pick_balance_heuristic(["y", "x"], eq) == "x"
    assert pick_balance_heuristic([], specs) is None


def test_apply_sum_derivation_drops_infeasible_keeps_feasible() -> None:
    cands = [
        {"a": 30.0, "b": 25.0},  # feasible → c=45
        {"a": 70.0, "b": 50.0},  # infeasible → dropped
    ]
    out = apply_sum_derivation(cands, [_group()], {})
    assert len(out) == 1
    assert out[0]["c"] == 45.0


def test_apply_sum_derivation_no_groups_is_passthrough() -> None:
    cands = [{"a": 1.0}, {"a": 2.0}]
    assert apply_sum_derivation(cands, [], {}) == cands


def test_project_sum_groups_fills_derived_member() -> None:
    # decode-side: a free-member candidate gets its derived member added.
    out = project_sum_groups({"a": 20.0, "b": 30.0}, [_group()])
    assert out["c"] == 50.0
    # An infeasible point is left without the derived member (runtime drops it).
    bad = project_sum_groups({"a": 80.0, "b": 80.0}, [_group()])
    assert "c" not in bad


def test_sum_check_notes_dedupes_and_labels() -> None:
    sc = {"value": 100.0, "sum_parameters": ["a", "b", "c"]}
    inputs = [
        {"name": "a", "label": "A %", "sum_check": sc},
        {"name": "b", "label": "B %", "sum_check": sc},  # same group declared again
        {"name": "c", "label": "C %", "sum_check": sc},
        {"name": "speed", "label": "Speed"},
    ]
    assert sum_check_notes(inputs) == [(100.0, ["A %", "B %", "C %"])]
    # A workflow member prefixes each label with the tool name.
    prefixed = sum_check_notes(inputs, prefix="Mix · ")
    assert prefixed[0][1] == ["Mix · A %", "Mix · B %", "Mix · C %"]


def test_sum_check_notes_empty_without_sum_check() -> None:
    assert sum_check_notes([{"name": "x", "label": "X"}]) == []
