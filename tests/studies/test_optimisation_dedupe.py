"""Unit tests for the candidate-dedupe helpers (ADR-0009 amendment 2026-06-15).

Pure — no DB. The integer-handling Phase B drops proposals whose *design*
repeats one already evaluated, so integer-rounding collisions don't re-run an
identical job. See ``designs/integer-parameter-handling.md`` §2.4.
"""

from __future__ import annotations

from matofx.studies.optimisation.runtime import (
    _candidate_key,
    _dedupe_unseen,
    _discrete_capacity,
)
from matofx.studies.optimisation.strategies.base import FactorSpec


def test_candidate_key_collapses_integral_floats() -> None:
    # A point produced as int 3 and one round-tripped from storage as 3.0 must
    # share a key, or a rounding collision slips past the dedupe.
    assert _candidate_key({"n": 3, "x": 1.5}) == _candidate_key({"n": 3.0, "x": 1.5})


def test_candidate_key_distinguishes_real_floats() -> None:
    assert _candidate_key({"x": 1.5}) != _candidate_key({"x": 1.6})


def test_candidate_key_is_order_independent() -> None:
    assert _candidate_key({"a": 1, "b": 2}) == _candidate_key({"b": 2, "a": 1})


def test_dedupe_drops_seen_and_in_batch_repeats() -> None:
    seen = {_candidate_key({"n": 1})}
    batch = [{"n": 1}, {"n": 2}, {"n": 2}, {"n": 3}]
    unique = _dedupe_unseen(batch, seen)
    assert unique == [{"n": 2}, {"n": 3}]


def test_dedupe_keeps_distinct_continuous_points() -> None:
    batch = [{"x": 1.10}, {"x": 1.11}, {"x": 1.12}]
    assert _dedupe_unseen(batch, set()) == batch


def test_discrete_capacity_counts_integer_and_categorical() -> None:
    factors = (
        FactorSpec(name="n", kind="int", low=0.0, high=5.0),  # 6 values
        FactorSpec(name="mode", kind="categorical", choices=("a", "b", "c")),  # 3
    )
    assert _discrete_capacity(factors) == 18


def test_discrete_capacity_none_when_any_continuous() -> None:
    factors = (
        FactorSpec(name="n", kind="int", low=0.0, high=5.0),
        FactorSpec(name="x", kind="float", low=0.0, high=1.0),
    )
    assert _discrete_capacity(factors) is None
