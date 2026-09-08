"""Pure-Python unit tests for the V&V lifecycle helpers (no DB).

The state machine lives in :mod:`matofx.vv.orm` as a transition table +
two helper functions. Fast tests so the lifecycle invariants get a
direct, schema-free assertion surface — the DB-side ``ck_*_state`` CHECK
catches stored-value violations, but only after a write attempt.
"""

from __future__ import annotations

import pytest

from matofx.vv.orm import (
    VV_STATES,
    can_transition,
    is_terminal,
)


def test_vv_states_closed_vocabulary() -> None:
    """The vocabulary is the canonical five — no more, no less."""
    assert set(VV_STATES) == {"draft", "pending", "approved", "rejected", "retired"}


@pytest.mark.parametrize("state", ["approved", "rejected", "retired"])
def test_terminal_states(state: str) -> None:
    """``approved`` / ``rejected`` / ``retired`` are terminal."""
    assert is_terminal(state)


@pytest.mark.parametrize("state", ["draft", "pending"])
def test_non_terminal_states(state: str) -> None:
    """``draft`` / ``pending`` are mutable."""
    assert not is_terminal(state)


def test_unknown_state_is_not_terminal() -> None:
    """An out-of-vocabulary state isn't treated as terminal (defensive)."""
    assert not is_terminal("frobbed")


# ---- Transition table ----------------------------------------------


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        ("draft", "pending"),
        ("draft", "retired"),
        ("pending", "approved"),
        ("pending", "rejected"),
        ("pending", "draft"),  # recall before decision
        ("pending", "retired"),  # curator withdraws a record still in review
        ("approved", "retired"),
        ("rejected", "retired"),
    ],
)
def test_allowed_transitions(from_state: str, to_state: str) -> None:
    """The documented happy-path + recall + retire transitions are allowed."""
    assert can_transition(from_state, to_state)


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        # Once terminal, no forward moves (except retire from a decision state).
        ("retired", "draft"),
        ("retired", "pending"),
        ("retired", "approved"),
        ("retired", "rejected"),
        # Decisions cannot be reversed by state flip — re-record instead.
        ("approved", "pending"),
        ("approved", "rejected"),
        ("rejected", "approved"),
        ("rejected", "pending"),
        # No self-loops are listed as forward moves.
        ("draft", "draft"),
        ("pending", "pending"),
        ("approved", "approved"),
        # Skipping pending isn't allowed — every decision goes through review.
        ("draft", "approved"),
        ("draft", "rejected"),
    ],
)
def test_forbidden_transitions(from_state: str, to_state: str) -> None:
    """Decision reversal, skip-review, and post-retire moves are all forbidden."""
    assert not can_transition(from_state, to_state)


def test_unknown_from_state_rejects() -> None:
    """A state outside the vocabulary can't be the source of any transition."""
    assert not can_transition("frobbed", "draft")
    assert not can_transition("frobbed", "approved")
