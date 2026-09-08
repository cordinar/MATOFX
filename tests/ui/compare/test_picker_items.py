"""Unit tests for the Select-tab picker item model + search haystack.

The picker UI itself is interactive (NiceGUI), but the item builders and
the search ``haystack`` are pure functions, so we test those directly:
they're what guarantees the search box matches a job/scenario/run by
name, number, or context.
"""

from __future__ import annotations

from matofx.ui.pages.compare.page import (
    _job_items,
    _PickerItem,
    _run_items,
    _scenario_items,
)


def _matches(item: _PickerItem, query: str) -> bool:
    """Mirror :meth:`_PickerList._filtered` for one item."""
    return query.strip().lower() in item.haystack


def test_job_items_carry_name_number_and_context() -> None:
    items = _job_items(
        [
            {
                "id": 2020,
                "name": "bracket_stress",
                "project_name": "Demo testing",
                "scenario_name": "Scenario 1",
            }
        ]
    )
    assert len(items) == 1
    item = items[0]
    assert item.id == 2020
    assert item.primary == "bracket_stress"
    assert "#0002020" in item.meta
    assert "Demo testing" in item.meta
    assert "Scenario 1" in item.meta


def test_job_search_matches_name_number_and_context() -> None:
    (item,) = _job_items(
        [
            {
                "id": 2020,
                "name": "bracket_stress",
                "project_name": "Demo testing",
                "scenario_name": "Scenario 1",
            }
        ]
    )
    assert _matches(item, "bracket")  # by name
    assert _matches(item, "BRACKET")  # case-insensitive
    assert _matches(item, "2020")  # by number
    assert _matches(item, "0002020")  # by padded number
    assert _matches(item, "demo testing")  # by project
    assert _matches(item, "scenario 1")  # by scenario
    assert not _matches(item, "nonexistent")


def test_job_items_fall_back_to_placeholder_name() -> None:
    (item,) = _job_items([{"id": 5, "name": None}])
    assert item.primary == "(no name)"


def test_scenario_items_use_name_or_id_and_description() -> None:
    named, unnamed = _scenario_items(
        [
            {"id": 7, "name": "Cold soak", "description": "-40C hold"},
            {"id": 9, "name": None, "description": ""},
        ]
    )
    assert named.primary == "Cold soak"
    assert named.meta == "-40C hold"
    assert _matches(named, "cold")
    assert _matches(named, "7")
    # No name → the id stands in as the primary label.
    assert unnamed.primary == "9"


def test_run_items_include_step_count_number_and_context() -> None:
    (item,) = _run_items(
        [
            {
                "id": 185,
                "name": "nominal",
                "children": [{}, {}, {}],
                "project_name": "Demo testing",
                "scenario_name": "Scenario 1",
            }
        ]
    )
    assert item.id == 185
    assert item.primary == "nominal"
    assert "#0000185" in item.meta
    assert "3 steps" in item.meta
    assert "Demo testing" in item.meta
    assert "Scenario 1" in item.meta
    assert _matches(item, "185")
    assert _matches(item, "nominal")
    assert _matches(item, "demo testing")  # by project
    assert _matches(item, "scenario 1")  # by scenario


def test_run_items_omit_context_when_absent() -> None:
    # Older payloads (or runs missing names) still render number + steps.
    (item,) = _run_items([{"id": 7, "name": "r", "children": [{}]}])
    assert item.meta == "#0000007 · 1 steps"
