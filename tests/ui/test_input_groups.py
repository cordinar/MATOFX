"""Unit tests for the study/optimisation input-group sectioning helper."""

from __future__ import annotations

from matofx.ui.pages._input_groups import group_sections, has_groups

_GROUPS = [
    {"name": "mat", "label": "Material", "column": 2, "row": 1},
    {"name": "geo", "label": "Geometry", "column": 1, "row": 1},
]
_INPUTS = [
    {"name": "youngs", "group": "mat"},
    {"name": "length", "group": "geo"},
    {"name": "notes", "group": None},
    {"name": "width", "group": "geo"},
    {"name": "ghost", "group": "does_not_exist"},
]


def _shape(sections: list) -> list:
    return [(header, [i["name"] for i in members]) for header, members in sections]


def test_orders_by_column_then_row_and_preserves_input_order() -> None:
    sections = group_sections(_INPUTS, _GROUPS)
    assert _shape(sections) == [
        ("Geometry", ["length", "width"]),  # column 1 before column 2
        ("Material", ["youngs"]),
        (None, ["notes", "ghost"]),  # ungrouped + unknown-group inputs trail
    ]
    assert has_groups(sections) is True


def test_no_groups_declared_yields_flat_single_section() -> None:
    sections = group_sections(_INPUTS, [])
    assert _shape(sections) == [(None, [i["name"] for i in _INPUTS])]
    assert has_groups(sections) is False


def test_none_input_groups_is_treated_as_no_groups() -> None:
    sections = group_sections(_INPUTS, None)
    assert has_groups(sections) is False


def test_empty_group_dropped_after_filtering() -> None:
    # Only a Material input survives (e.g. optimisation's numeric-only filter):
    # the empty Geometry group must not render.
    sections = group_sections([{"name": "youngs", "group": "mat"}], _GROUPS)
    assert _shape(sections) == [("Material", ["youngs"])]


def test_label_falls_back_to_name_when_missing() -> None:
    groups = [{"name": "geo", "column": 1, "row": 1}]
    sections = group_sections([{"name": "length", "group": "geo"}], groups)
    assert sections[0][0] == "geo"


def test_group_defaults_when_column_row_absent() -> None:
    # Groups without explicit column/row default to (1, 1) and keep declaration
    # order relative to each other via the stable sort.
    groups = [
        {"name": "a", "label": "Alpha"},
        {"name": "b", "label": "Beta"},
    ]
    inputs = [{"name": "x", "group": "b"}, {"name": "y", "group": "a"}]
    sections = group_sections(inputs, groups)
    assert [h for h, _ in sections] == ["Alpha", "Beta"]
