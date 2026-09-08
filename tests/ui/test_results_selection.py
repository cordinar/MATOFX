"""Unit tests for the warehouse-page selection model.

Pure-logic tests against ``matofx.ui.pages._results_selection`` — no
NiceGUI, no DB, no httpx. The page shell wires the live widgets to
this module but doesn't own the rules.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from matofx.ui.pages._results_selection import (
    COMPARE_MAX,
    COMPARE_MIN,
    CompareAction,
    SelectionState,
    build_compare_href,
    encode_job_ids_for_export,
)

# ---- Selection state mechanics ---------------------------------------


def test_empty_state_count_is_zero() -> None:
    assert SelectionState().count == 0


def test_tick_adds_to_set() -> None:
    s = SelectionState().tick(5).tick(7)
    assert s.count == 2
    assert s.ticked == frozenset({5, 7})


def test_untick_removes_from_set() -> None:
    s = SelectionState(ticked=frozenset({5, 7})).untick(5)
    assert s.ticked == frozenset({7})


def test_untick_missing_id_is_noop() -> None:
    s = SelectionState(ticked=frozenset({5})).untick(99)
    assert s.ticked == frozenset({5})


def test_clear_returns_empty() -> None:
    assert SelectionState(ticked=frozenset({1, 2, 3})).clear().count == 0


def test_tick_idempotent() -> None:
    s = SelectionState().tick(5).tick(5)
    assert s.ticked == frozenset({5})


# ---- Compare-button action ------------------------------------------


def test_empty_selection_compare_disabled() -> None:
    action = SelectionState().compare_action()
    assert isinstance(action, CompareAction)
    assert action.enabled is False
    assert action.label == "View in Compare"
    assert action.href is None
    assert "Tick at least" in action.tooltip


def test_single_selection_compare_disabled_with_count() -> None:
    action = SelectionState(ticked=frozenset({1})).compare_action()
    assert action.enabled is False
    assert action.label == "View in Compare (1)"
    assert "Tick at least" in action.tooltip


def test_two_selections_compare_enabled() -> None:
    action = SelectionState(ticked=frozenset({1, 2})).compare_action()
    assert action.enabled is True
    assert action.label == "View in Compare (2)"
    assert action.tooltip == ""
    assert action.href is not None
    assert action.href.startswith("/compare?")


def test_six_selections_compare_still_enabled() -> None:
    action = SelectionState(ticked=frozenset(range(1, 7))).compare_action()
    assert action.enabled is True
    assert action.label == f"View in Compare ({COMPARE_MAX})"


def test_seven_selections_compare_disabled_with_cap_tooltip() -> None:
    action = SelectionState(ticked=frozenset(range(1, 8))).compare_action()
    assert action.enabled is False
    assert action.label == "View in Compare (7)"
    assert "Compare is limited to 6" in action.tooltip
    assert "narrow your selection" in action.tooltip
    # No href when disabled — guards against accidental navigation.
    assert action.href is None


def test_compare_constants_match_design() -> None:
    """The shared limits are the same as Compare's own ceiling."""
    assert COMPARE_MIN == 2
    assert COMPARE_MAX == 6


# ---- Compare-handoff URL --------------------------------------------


def test_compare_href_encodes_jobs_in_ascending_order() -> None:
    """Job ids land in ascending order so the URL is stable across re-renders."""
    href = build_compare_href(frozenset({5, 1, 3}))
    parsed = urlparse(href)
    assert parsed.path == "/compare"
    query = parse_qs(parsed.query)
    assert query["mode"] == ["tool"]
    assert query["jobs"] == ["1,3,5"]


def test_compare_href_uses_tool_mode() -> None:
    """The warehouse hand-off always sends Compare its ``mode=tool`` URL."""
    href = build_compare_href(frozenset({1, 2}))
    assert "mode=tool" in href


# ---- CSV export job_ids encoding ------------------------------------


def test_export_encoding_empty_selection_returns_empty_string() -> None:
    assert encode_job_ids_for_export(frozenset()) == ""


def test_export_encoding_orders_ids_ascending() -> None:
    assert encode_job_ids_for_export(frozenset({3, 1, 2})) == "1,2,3"
