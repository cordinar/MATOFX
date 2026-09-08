"""Regression tests for the workflow-run page's poll-driven rerender.

The bug: while a workflow run was still in flight, the poll loop tore
down the *whole* shell on every tick — the tab strip and the active-tab
body together. So a downstream member transitioning rebuilt the body the
user was reading, wiping an in-progress form and re-fetching the embedded
job needlessly; and the poll never stopped, so the page kept hitting the
API for the life of the tab even after the run completed.

The fix (1) deactivates the poll once the run reaches a terminal state,
and (2) gates the tab strip and the active body on *separate* signatures
so a poll rebuilds only what actually changed.

These exercise the pure decision logic by stubbing the NiceGUI rebuild
helpers and feeding fake containers, so no app/browser context is needed
(mirrors ``test_studies_detail_rerender.py``).
"""

from __future__ import annotations

from typing import Any

import pytest

from matofx.ui.pages import workflow_run


class _FakeBox:
    """Stand-in for a NiceGUI container: supports ``.clear()`` + ``with``."""

    def __init__(self, tally: dict[str, int], key: str) -> None:
        self._tally = tally
        self._key = key

    def clear(self) -> None:
        self._tally[f"{self._key}_clear"] += 1

    def __enter__(self) -> _FakeBox:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Replace the three render helpers with counters; return the tally."""
    tally = {"header": 0, "tabs": 0, "body": 0}

    def _bump_header(_client: Any, _run: Any) -> None:
        tally["header"] += 1

    def _bump_tabs(_state: dict[str, Any]) -> None:
        tally["tabs"] += 1

    def _bump_body(_state: dict[str, Any]) -> None:
        tally["body"] += 1

    monkeypatch.setattr(workflow_run, "_render_header", _bump_header)
    monkeypatch.setattr(workflow_run, "_render_tabs_strip", _bump_tabs)
    monkeypatch.setattr(workflow_run, "_render_active_tab", _bump_body)
    return tally


# Two-member workflow: ``downstream`` (pos 2) consumes an output of
# ``upstream`` (pos 1), so it stays "waiting" until upstream completes.
_DESCRIPTORS = {
    "upstream": {"inputs": []},
    "downstream": {
        "inputs": [{"name": "x", "downstream_input": True, "upstream_tool": "upstream"}]
    },
}


def _state(
    *,
    active_position: int,
    upstream_state: str = "running",
    downstream_state: str = "created",
    run_state: str = "running",
) -> dict[str, Any]:
    tally: dict[str, int] = {"header_clear": 0, "tabs_clear": 0, "body_clear": 0}
    return {
        "run": {
            "id": 1,
            "state": run_state,
            "cancel_requested_at": None,
            "children": [
                {
                    "id": 11,
                    "tool_alias": "upstream",
                    "workflow_position": 1,
                    "state": upstream_state,
                },
                {
                    "id": 12,
                    "tool_alias": "downstream",
                    "workflow_position": 2,
                    "state": downstream_state,
                },
            ],
        },
        "descriptors": _DESCRIPTORS,
        "active_position": active_position,
        "view_kind": "tool",
        "header": _FakeBox(tally, "header"),
        "tabs_box": _FakeBox(tally, "tabs"),
        "body_box": _FakeBox(tally, "body"),
        "client": object(),
        "run_id": 1,
        "last_tabs_sig": None,
        "last_body_sig": None,
    }


def _baseline(state: dict[str, Any], calls: dict[str, int]) -> None:
    """First render establishes both signatures; reset the tally after."""
    workflow_run._rerender_all(state, force=True)
    calls.update(header=0, tabs=0, body=0)


# ---------- fix #2: independent strip / body gating ---------------------


def test_force_rebuilds_strip_and_body(calls: dict[str, int]) -> None:
    state = _state(active_position=1)
    workflow_run._rerender_all(state, force=True)
    assert (calls["header"], calls["tabs"], calls["body"]) == (1, 1, 1)


def test_downstream_transition_rebuilds_strip_not_body(calls: dict[str, int]) -> None:
    # User is on the upstream tab; a *downstream* member changes state.
    state = _state(active_position=1)
    _baseline(state, calls)
    state["run"]["children"][1]["state"] = "queued"
    workflow_run._rerender_all(state)
    assert calls["tabs"] == 1  # badges refresh
    assert calls["body"] == 0  # body the user is on is left intact


def test_active_tab_queued_to_running_does_not_rebuild_body(
    calls: dict[str, int],
) -> None:
    # The active tab is in flight (queued). Its queued → running move is
    # handled in place by the embedded standalone renderer (status line +
    # logs), so the workflow page must NOT re-embed it — re-embedding
    # while it's still working only re-fetches the job needlessly.
    state = _state(active_position=1, upstream_state="queued")
    _baseline(state, calls)
    state["run"]["children"][0]["state"] = "running"
    workflow_run._rerender_all(state)
    assert calls["body"] == 0  # still in flight → embedded renderer owns it
    assert calls["tabs"] == 1  # strip badge still updates


def test_active_tab_running_to_complete_rebuilds_body(calls: dict[str, int]) -> None:
    # The active tab finishing (running → complete) is the moment the
    # results must appear. The page's own poll re-embeds the body exactly
    # once — it does NOT rely on the embedded renderer's inner timer,
    # which can be torn down mid-tick and leave the tab stuck on
    # "running" until a manual refresh (the reported bug).
    state = _state(active_position=1, upstream_state="running")
    _baseline(state, calls)
    state["run"]["children"][0]["state"] = "complete"
    workflow_run._rerender_all(state)
    assert calls["body"] == 1  # results re-embedded once
    assert calls["tabs"] == 1  # strip badge still updates


def test_active_tab_running_to_failed_rebuilds_body(calls: dict[str, int]) -> None:
    # Failure is terminal too — the body must re-embed so the failed
    # banner + logs replace the "running" view without a refresh.
    state = _state(active_position=1, upstream_state="running")
    _baseline(state, calls)
    state["run"]["children"][0]["state"] = "failed"
    workflow_run._rerender_all(state)
    assert calls["body"] == 1
    assert calls["tabs"] == 1


def test_created_active_tab_submitted_rebuilds_body(calls: dict[str, int]) -> None:
    # A ready ``created`` tab entering the in-flight phase (created →
    # queued) flips the body from the form to the embedded job view, so
    # it must rebuild exactly once.
    state = _state(active_position=1, upstream_state="created")
    _baseline(state, calls)
    state["run"]["children"][0]["state"] = "queued"
    workflow_run._rerender_all(state)
    assert calls["body"] == 1


def test_created_active_tab_flips_to_ready_rebuilds_body(calls: dict[str, int]) -> None:
    # The active downstream tab stays "created", but its upstream
    # completing flips it waiting → form; the body must rebuild even
    # though the active child's own (id, state) is unchanged.
    state = _state(active_position=2, upstream_state="running")
    _baseline(state, calls)
    state["run"]["children"][0]["state"] = "complete"
    workflow_run._rerender_all(state)
    assert calls["body"] == 1


def test_unchanged_poll_is_a_noop(calls: dict[str, int]) -> None:
    state = _state(active_position=1)
    _baseline(state, calls)
    workflow_run._rerender_all(state)
    assert (calls["header"], calls["tabs"], calls["body"]) == (0, 0, 0)


def test_cancel_flag_rebuilds_strip_and_body(calls: dict[str, int]) -> None:
    state = _state(active_position=1)
    _baseline(state, calls)
    state["run"]["cancel_requested_at"] = "2026-06-04T00:00:00Z"
    workflow_run._rerender_all(state)
    assert calls["tabs"] == 1
    assert calls["body"] == 1


# ---------- fix #1: poll rests when nothing is in flight ----------------


def _run_with_child_states(*states: str) -> dict[str, Any]:
    return {"children": [{"id": i, "state": s} for i, s in enumerate(states)]}


@pytest.mark.parametrize("child_state", ["queued", "running"])
def test_poll_keeps_running_while_a_child_is_in_flight(child_state: str) -> None:
    run = _run_with_child_states("complete", child_state)
    assert workflow_run._poll_can_rest(run) is False


def test_poll_rests_when_all_children_terminal() -> None:
    run = _run_with_child_states("complete", "failed", "cancelled")
    assert workflow_run._poll_can_rest(run) is True


def test_poll_rests_with_finished_tool_and_waiting_drafts() -> None:
    # The reported bug: one tool finished, the rest are ``created`` drafts
    # awaiting the user. The run-level state never goes terminal, but
    # nothing is in flight, so the poll must still rest.
    run = _run_with_child_states("complete", "created", "created")
    assert workflow_run._poll_can_rest(run) is True


def test_poll_rests_on_brand_new_draft_run() -> None:
    run = _run_with_child_states("created", "created")
    assert workflow_run._poll_can_rest(run) is True


def test_poll_rests_with_no_children() -> None:
    assert workflow_run._poll_can_rest({"children": []}) is True
    assert workflow_run._poll_can_rest({}) is True


# ---------- fix #3: run-level summary fills in on completion ------------
#
# The bug: the run-level Summary card (the workflow's rolled-up "outputs")
# was drawn exactly once at page load. While the run is in flight the
# summary endpoint 404s, so the card starts blank; when the run later
# completed the poll refreshed the tab strip + body but never the summary
# card, so it only appeared after a manual page refresh.


def _summary_state(run_state: str, *, done: bool = False) -> tuple[dict[str, Any], dict[str, int]]:
    tally: dict[str, int] = {"summary_clear": 0}
    state = {
        "run": {"id": 7, "state": run_state, "children": []},
        "summary_box": _FakeBox(tally, "summary"),
        "summary_done": done,
        "client": object(),
        "run_id": 7,
    }
    return state, tally


async def test_summary_not_fetched_while_run_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched: list[Any] = []

    async def _spy(_client: Any, _kind: str, _sid: int) -> bool:
        fetched.append((_kind, _sid))
        return True

    monkeypatch.setattr("matofx.ui._summary_card.render_summary_card", _spy)
    state, tally = _summary_state("running")
    await workflow_run._refresh_summary_card(state)
    assert fetched == []  # never hit the endpoint while non-terminal
    assert tally["summary_clear"] == 0
    assert state["summary_done"] is False


async def test_summary_renders_once_when_run_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched: list[Any] = []

    async def _spy(_client: Any, kind: str, sid: int) -> bool:
        fetched.append((kind, sid))
        return True

    monkeypatch.setattr("matofx.ui._summary_card.render_summary_card", _spy)
    state, tally = _summary_state("complete")
    await workflow_run._refresh_summary_card(state)
    assert fetched == [("workflow_run", 7)]
    assert tally["summary_clear"] == 1
    assert state["summary_done"] is True

    # A second poll tick is a no-op once latched — no re-fetch, no re-clear.
    await workflow_run._refresh_summary_card(state)
    assert fetched == [("workflow_run", 7)]
    assert tally["summary_clear"] == 1


@pytest.mark.parametrize("run_state", ["complete", "failed", "partial", "cancelled"])
async def test_summary_renders_for_every_terminal_state(
    monkeypatch: pytest.MonkeyPatch, run_state: str
) -> None:
    async def _spy(_client: Any, _kind: str, _sid: int) -> bool:
        return True

    monkeypatch.setattr("matofx.ui._summary_card.render_summary_card", _spy)
    state, _tally = _summary_state(run_state)
    await workflow_run._refresh_summary_card(state)
    assert state["summary_done"] is True


async def test_summary_retries_when_first_fetch_comes_back_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the summary isn't ready on the tick we first see "complete", the
    # card render returns False; we must NOT latch, so the next tick retries.
    results = iter([False, True])

    async def _spy(_client: Any, _kind: str, _sid: int) -> bool:
        return next(results)

    monkeypatch.setattr("matofx.ui._summary_card.render_summary_card", _spy)
    state, tally = _summary_state("complete")

    await workflow_run._refresh_summary_card(state)
    assert state["summary_done"] is False  # first attempt empty → not latched
    assert tally["summary_clear"] == 1

    await workflow_run._refresh_summary_card(state)
    assert state["summary_done"] is True  # retry succeeded
    assert tally["summary_clear"] == 2
