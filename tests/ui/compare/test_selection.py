"""URL-state parsing for the Compare workspace.

Pin the parser's behaviour so deep links and the My-Jobs multi-select
floating bar route the same query strings into the same selections.
Friendly-error copy is asserted by substring (not full text) so a
later wording tweak doesn't churn the tests.
"""

from __future__ import annotations

from matofx.ui.pages.compare.selection import (
    CROSS_TOOL_MESSAGE,
    MAX_SELECTION,
    MIN_SELECTION,
    Selection,
    SelectionError,
    build_query,
    detect_cross_tool,
    parse_selection,
)


def test_empty_query_lands_on_blank_tool_mode() -> None:
    result = parse_selection({})
    assert isinstance(result, Selection)
    assert result.mode == "tool"
    assert result.ids == ()


def test_explicit_mode_no_ids_is_not_an_error() -> None:
    """User landing on the Select tab without picks is the canonical entry path."""
    result = parse_selection({"mode": "scenario"})
    assert isinstance(result, Selection)
    assert result.mode == "scenario"
    assert result.ids == ()


def test_unknown_mode_returns_friendly_error() -> None:
    result = parse_selection({"mode": "wat"})
    assert isinstance(result, Selection) is False
    assert isinstance(result, SelectionError)
    assert "doesn't recognise" in result.message


def test_tool_mode_reads_jobs_param() -> None:
    result = parse_selection({"mode": "tool", "jobs": "1,2,3"})
    assert isinstance(result, Selection)
    assert result.mode == "tool"
    assert result.ids == (1, 2, 3)


def test_scenario_mode_reads_scenarios_param() -> None:
    result = parse_selection({"mode": "scenario", "scenarios": "10,20"})
    assert isinstance(result, Selection)
    assert result.mode == "scenario"
    assert result.ids == (10, 20)


def test_non_numeric_id_returns_friendly_error() -> None:
    result = parse_selection({"mode": "tool", "jobs": "1,banana,3"})
    assert isinstance(result, SelectionError)
    assert "whole number" in result.message


def test_below_minimum_after_dedupe_is_an_error() -> None:
    """A single-id selection isn't compareable; the user needs at least two."""
    result = parse_selection({"mode": "tool", "jobs": "5"})
    assert isinstance(result, SelectionError)
    assert str(MIN_SELECTION) in result.message


def test_duplicate_ids_dedupe_silently() -> None:
    """The set sense of selection dominates; the diff would be trivial otherwise."""
    result = parse_selection({"mode": "tool", "jobs": "1,1,2,2,3"})
    assert isinstance(result, Selection)
    assert result.ids == (1, 2, 3)


def test_above_maximum_is_an_error() -> None:
    too_many = ",".join(str(i) for i in range(1, MAX_SELECTION + 2))
    result = parse_selection({"mode": "tool", "jobs": too_many})
    assert isinstance(result, SelectionError)
    assert str(MAX_SELECTION) in result.message


def test_build_query_round_trips() -> None:
    selection = Selection(mode="tool", ids=(1, 2, 3))
    rendered = build_query(selection)
    assert rendered == {"mode": "tool", "jobs": "1,2,3"}
    parsed = parse_selection(rendered)
    assert isinstance(parsed, Selection)
    assert parsed == selection


def test_build_query_for_empty_selection_keeps_mode() -> None:
    """An empty selection still serialises its mode so the URL stays
    self-describing — opening it again lands on the right Select tab."""
    rendered = build_query(Selection(mode="scenario", ids=()))
    assert rendered == {"mode": "scenario"}


def test_detect_cross_tool_homogeneous() -> None:
    assert detect_cross_tool(["heat_transfer", "heat_transfer", "heat_transfer"]) is False


def test_detect_cross_tool_mixed() -> None:
    assert detect_cross_tool(["heat_transfer", "material_properties"]) is True


def test_detect_cross_tool_ignores_blanks() -> None:
    """Empty aliases are noise (e.g. malformed metadata) — don't trip the guard."""
    assert detect_cross_tool(["heat_transfer", "", "heat_transfer"]) is False


def test_cross_tool_message_is_user_facing() -> None:
    """Sanity check: the constant the workspace renders is plain English."""
    assert "tool" in CROSS_TOOL_MESSAGE.lower()
    assert "support" in CROSS_TOOL_MESSAGE.lower()
