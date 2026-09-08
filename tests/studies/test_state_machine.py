"""Exhaustive tests for the study state-machine rollup (H40, design §6/§8).

Pure — ``compute_state`` only reads ``.state`` / ``.cancel_requested_at``
on the study and ``.state`` on each child, so lightweight stubs cover
the whole truth table without a DB.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

import pytest

from matofx.studies import compute_state


@dataclass
class _Study:
    state: str
    cancel_requested_at: _dt.datetime | None = None


@dataclass
class _Child:
    state: str


def _kids(*states: str) -> list[_Child]:
    return [_Child(s) for s in states]


_NOW = _dt.datetime(2026, 5, 18, tzinfo=_dt.UTC)


def test_rejected_is_sticky_over_everything() -> None:
    s = _Study("rejected", cancel_requested_at=_NOW)
    assert compute_state(s, _kids("complete", "complete")) == "rejected"  # type: ignore[arg-type]


def test_cancel_requested_beats_child_rollup() -> None:
    s = _Study("running", cancel_requested_at=_NOW)
    assert compute_state(s, _kids("complete", "running")) == "cancelled"  # type: ignore[arg-type]


def test_draft_with_no_children() -> None:
    assert compute_state(_Study("draft"), []) == "draft"  # type: ignore[arg-type]


def test_pending_approval_passthrough() -> None:
    assert compute_state(_Study("pending_approval"), []) == "pending_approval"  # type: ignore[arg-type]


def test_submitted_no_children_returns_stored() -> None:
    assert compute_state(_Study("running"), []) == "running"  # type: ignore[arg-type]


def test_all_complete() -> None:
    assert compute_state(_Study("running"), _kids("complete", "complete")) == "complete"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "inflight", ["created", "queued", "running", "pending_approval", "pending_results"]
)
def test_any_inflight_is_running(inflight: str) -> None:
    s = _Study("running")
    assert compute_state(s, _kids("complete", inflight, "failed")) == "running"  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["failed", "denied", "results_withheld"])
def test_complete_plus_terminal_bad_is_partial(bad: str) -> None:
    s = _Study("running")
    assert compute_state(s, _kids("complete", bad)) == "partial"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "states",
    [("failed",), ("failed", "failed"), ("failed", "denied"), ("denied", "results_withheld")],
)
def test_all_terminal_bad_is_failed(states: tuple[str, ...]) -> None:
    assert compute_state(_Study("running"), _kids(*states)) == "failed"  # type: ignore[arg-type]


def test_single_complete() -> None:
    assert compute_state(_Study("running"), _kids("complete")) == "complete"  # type: ignore[arg-type]
