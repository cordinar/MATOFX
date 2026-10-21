"""Outcome wording for a submitted batch of approval decisions.

The page itself is a NiceGUI surface and isn't render-tested; the
decision the *user* sees — what got applied, and what didn't and why —
is pure logic and lives here.

Background: ``_submit`` used to end with ``ui.navigate.reload()``. The
queue did refresh, but the document reloaded on the same tick as the
``ui.notify`` calls above it, so every message was destroyed before it
rendered. A batch where items failed came back looking untouched with
no explanation — "I clicked Approve and nothing happened". The page now
rebuilds its list in place and keeps the messages; these tests pin down
the messages themselves.
"""

from __future__ import annotations

from matofx.ui.pages.approvals import (
    request_display_name,
    summarise_decision_results,
)

# ---- request_display_name -------------------------------------------


def test_display_name_is_job_number_and_name() -> None:
    name = request_display_name({"job_id": 2977, "job_name": "Bracket sweep"}, 23)
    assert name == "0002977 · Bracket sweep"


def test_display_name_falls_back_to_tool_name_for_a_tool_less_gate() -> None:
    """Library / material gates have no job — they still get a label."""
    assert request_display_name({"tool_name": "Laminate table"}, 23) == "Laminate table"


def test_display_name_drops_the_separator_when_the_name_is_blank() -> None:
    assert request_display_name({"job_id": 2977, "job_name": ""}, 23) == "0002977"


def test_display_name_last_resort_is_the_request_id() -> None:
    """A row we no longer hold still produces a message, not a blank."""
    assert request_display_name({}, 23) == "Request 23"


# ---- summarise_decision_results --------------------------------------


_ITEMS = {
    22: {"job_id": 2978, "job_name": "Soak run"},
    23: {"job_id": 2977, "job_name": "Bracket sweep"},
}


def test_all_applied_reports_the_count_and_no_failures() -> None:
    success, failures = summarise_decision_results(
        [{"request_id": 22, "ok": True}, {"request_id": 23, "ok": True}], _ITEMS
    )
    assert success == "2 decision(s) applied."
    assert failures == []


def test_a_rejected_decision_is_named_and_explained() -> None:
    """The message the reload used to destroy."""
    success, failures = summarise_decision_results(
        [{"request_id": 23, "ok": False, "error": "already approved"}], _ITEMS
    )
    assert success is None
    assert failures == ["0002977 · Bracket sweep — not applied: already approved"]


def test_a_mixed_batch_reports_both_halves() -> None:
    success, failures = summarise_decision_results(
        [
            {"request_id": 22, "ok": True},
            {"request_id": 23, "ok": False, "error": "already approved"},
        ],
        _ITEMS,
    )
    assert success == "1 decision(s) applied."
    assert failures == ["0002977 · Bracket sweep — not applied: already approved"]


def test_a_failure_with_no_error_text_still_says_it_did_not_apply() -> None:
    _success, failures = summarise_decision_results([{"request_id": 22, "ok": False}], _ITEMS)
    assert failures == ["0002978 · Soak run — not applied: failed"]


def test_a_failure_for_an_unknown_request_is_still_reported() -> None:
    """Never silently drop a failure just because its row has gone."""
    _success, failures = summarise_decision_results(
        [{"request_id": 99, "ok": False, "error": "not permitted"}], _ITEMS
    )
    assert failures == ["Request 99 — not applied: not permitted"]


def test_an_empty_result_set_says_nothing() -> None:
    assert summarise_decision_results([], _ITEMS) == (None, [])
