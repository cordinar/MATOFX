"""Approval-gated members inside a workflow run (§16.15).

The bug: ``_tab_status_for`` recognised only the five original lifecycle
states. The four approval states have no branch, so they fell through
the ``created`` case and — because upstream *was* ready — came back as
``"ready"``. A member already submitted and sitting with an approver
re-rendered its **input form** and chipped itself *Ready*; a ``denied``
member, terminally refused, offered a form too. Nothing anywhere told
the submitter that approval was involved.

Standalone jobs were fine: the My Jobs right pane routes those states to
the approval notice. Only the workflow surface was blind, which is
exactly what the user reported.

Pure decision logic, so no app/browser context is needed (mirrors
``test_workflow_run_rerender.py``).
"""

from __future__ import annotations

import pytest

from matofx.hierarchy.orm import JOB_STATES
from matofx.ui.pages._approval_notice import APPROVAL_NOTICES, is_approval_state
from matofx.ui.pages.workflow_run import (
    _ACTIVE_CHILD_STATES,
    _UPSTREAM_DEAD_ENDS,
    _active_body_phase,
    _poll_can_rest,
    _tab_status_for,
    _tab_status_label,
)

_APPROVAL_STATES = ("pending_approval", "denied", "pending_results", "results_withheld")


# ---- tab status ------------------------------------------------------


@pytest.mark.parametrize("state", _APPROVAL_STATES)
def test_an_approval_state_is_never_ready(state: str) -> None:
    """The regression: these used to render the submit form again."""
    assert _tab_status_for(state, upstream_ready=True) != "ready"


@pytest.mark.parametrize("state", _APPROVAL_STATES)
def test_an_approval_state_maps_to_itself(state: str) -> None:
    """So the tab body can route it to the shared approval notice."""
    assert _tab_status_for(state, upstream_ready=True) == state
    assert is_approval_state(state)


@pytest.mark.parametrize("state", _APPROVAL_STATES)
def test_upstream_readiness_cannot_turn_an_approval_state_into_a_form(
    state: str,
) -> None:
    """The ``created`` split must not reach these at all, either way."""
    assert _tab_status_for(state, upstream_ready=False) == state


def test_created_still_splits_on_upstream_readiness() -> None:
    assert _tab_status_for("created", upstream_ready=True) == "ready"
    assert _tab_status_for("created", upstream_ready=False) == "waiting"


def test_every_job_state_has_a_tab_status() -> None:
    """No state may silently fall into the ``created`` branch again.

    ``created`` is the only state allowed to produce a status that isn't
    its own name — that split is the whole point of the function.
    """
    for state in JOB_STATES:
        status = _tab_status_for(state, upstream_ready=True)
        if state == "created":
            assert status == "ready"
            continue
        assert status == state, f"{state} was reclassified as {status!r}"


# ---- chip wording ----------------------------------------------------


@pytest.mark.parametrize("state", _APPROVAL_STATES)
def test_the_chip_never_shows_a_raw_snake_case_token(state: str) -> None:
    """``tab_status.title()`` would have read "Pending_Approval"."""
    label = _tab_status_label(state)
    assert "_" not in label
    assert label[:1].isupper()


def test_the_synthetic_statuses_keep_their_own_wording() -> None:
    assert _tab_status_label("ready") == "Ready"
    assert _tab_status_label("waiting") == "Waiting"


def test_pending_approval_chip_says_what_it_is_waiting_for() -> None:
    assert _tab_status_label("pending_approval") == "Pending approval"


# ---- notice coverage -------------------------------------------------


@pytest.mark.parametrize("state", _APPROVAL_STATES)
def test_every_approval_state_has_notice_copy(state: str) -> None:
    """The tab body renders this — a missing entry would fall to a stub."""
    title, body = APPROVAL_NOTICES[state]
    assert title
    assert body


# ---- polling ---------------------------------------------------------


@pytest.mark.parametrize("state", ["pending_approval", "pending_results"])
def test_poll_keeps_running_while_a_member_waits_on_an_approver(state: str) -> None:
    """An approver moves these, not the viewer — so the UI must keep looking.

    With the poll parked, the tab sat on "Awaiting approval" forever and
    only a manual refresh showed that the step had been signed off.
    """
    assert state in _ACTIVE_CHILD_STATES
    assert not _poll_can_rest({"children": [{"state": state}]})


@pytest.mark.parametrize("state", ["complete", "failed", "denied", "results_withheld"])
def test_poll_rests_once_every_member_is_terminal(state: str) -> None:
    assert _poll_can_rest({"children": [{"state": state}]})


def test_poll_rests_on_a_draft_awaiting_this_user() -> None:
    """``created`` waits on the viewer, who re-renders by submitting."""
    assert _poll_can_rest({"children": [{"state": "created"}]})


# ---- upstream dead ends ----------------------------------------------


def test_terminal_refusals_count_as_dead_ends_not_as_waiting() -> None:
    """ "Waiting for X to finish" is wrong when X can never finish."""
    assert "denied" in _UPSTREAM_DEAD_ENDS
    assert "results_withheld" in _UPSTREAM_DEAD_ENDS
    assert "failed" in _UPSTREAM_DEAD_ENDS


def test_a_held_upstream_is_still_merely_waiting() -> None:
    """``pending_approval`` upstream may yet be approved and complete."""
    assert "pending_approval" not in _UPSTREAM_DEAD_ENDS
    assert "pending_results" not in _UPSTREAM_DEAD_ENDS
    assert "queued" not in _UPSTREAM_DEAD_ENDS


# ---- body render phase -----------------------------------------------

_EMPTY_STATE: dict = {"run": {"children": []}, "descriptors": {}}


def _phase(child_state: str) -> str:
    return _active_body_phase(_EMPTY_STATE, {"state": child_state, "tool_alias": "x"})


def test_release_from_approval_is_a_real_phase_change() -> None:
    """``pending_approval → queued`` must move the body signature.

    Both used to collapse into ``"inflight"``, so when an approver
    released the run the signature never changed: the tab strip's badge
    flipped to *queued* while the body below it went on saying "Awaiting
    approval" until the user refreshed by hand.
    """
    assert _phase("pending_approval") != _phase("queued")


def test_release_of_held_results_is_a_real_phase_change() -> None:
    assert _phase("pending_results") != _phase("complete")


@pytest.mark.parametrize("state", _APPROVAL_STATES)
def test_each_approval_state_is_its_own_phase(state: str) -> None:
    """The notice copy differs per state, so they can't share a phase."""
    assert _phase(state) == state


def test_every_job_state_maps_to_exactly_one_body_phase() -> None:
    """Two states may share a phase only when they render the same body."""
    shared = {
        "queued": "inflight",
        "running": "inflight",
        "complete": "terminal",
        "failed": "terminal",
        "created": "ready",
    }
    for state in JOB_STATES:
        expected = shared.get(state, state)
        assert _phase(state) == expected, f"{state} rendered as {_phase(state)!r}"
