"""Pure-helper tests for ``matofx.ui.pages.job_search``.

The page itself is a NiceGUI surface (hard to drive without a client),
but the small helper functions it exports are pure dict-in / string-out
and test cleanly in isolation. Covers:

- :func:`_tool_label` — pretty ``name vX.Y.Z`` formatting.
- :func:`_input_label_with_unit` — descriptor label + unit suffix.
- :func:`_is_empty_filter_value` — empty-filter detection (the rule
  that decides whether a widget contributes to the API payload).

(The read-only inputs view used to live in this module behind a
``_format_input_value`` helper; it now delegates to
``matofx.ui.pages.job._render_body`` for byte-for-byte parity with the
My Jobs workspace, so the helper and its tests have been retired.)
"""

from __future__ import annotations

from matofx.ui.pages.job_search import (
    _closeness_label,
    _input_label_with_unit,
    _is_empty_filter_value,
    _tool_label,
    _weighting_note,
)

# --- _tool_label ---------------------------------------------------------


def test_tool_label_includes_version_when_present() -> None:
    assert _tool_label({"name": "Heat Transfer", "version": "1.0.0"}) == (
        "Heat Transfer v1.0.0"
    )


def test_tool_label_falls_back_to_alias_when_name_missing() -> None:
    assert _tool_label({"alias": "ht", "version": "0.9"}) == "ht v0.9"


def test_tool_label_omits_version_suffix_when_blank() -> None:
    assert _tool_label({"name": "Just A Tool"}) == "Just A Tool"


def test_tool_label_handles_none() -> None:
    assert _tool_label(None) == ""


# --- _input_label_with_unit ----------------------------------------------


def test_input_label_appends_unit_in_parens() -> None:
    assert _input_label_with_unit({"label": "Temperature", "unit": "degC"}) == (
        "Temperature (degC)"
    )


def test_input_label_omits_unit_when_blank() -> None:
    assert _input_label_with_unit({"label": "Material"}) == "Material"


def test_input_label_falls_back_to_name() -> None:
    assert _input_label_with_unit({"name": "k", "unit": "W/m/K"}) == "k (W/m/K)"


# --- _is_empty_filter_value ----------------------------------------------


def test_empty_filter_none_string_list_are_empty() -> None:
    assert _is_empty_filter_value(None) is True
    assert _is_empty_filter_value("") is True
    assert _is_empty_filter_value([]) is True


def test_empty_filter_zero_is_not_empty() -> None:
    """Numeric zero is a real filter value (e.g. percentage = 0%)."""
    assert _is_empty_filter_value(0) is False
    assert _is_empty_filter_value(0.0) is False


def test_empty_filter_false_is_not_empty() -> None:
    """Boolean ``False`` is a real filter value, not 'no constraint'."""
    assert _is_empty_filter_value(False) is False


def test_empty_filter_populated_string_or_list_is_not_empty() -> None:
    assert _is_empty_filter_value("abc") is False
    assert _is_empty_filter_value(["a"]) is False


# --- _closeness_label (Similar mode) -------------------------------------


def test_closeness_label_rounds_to_whole_percent() -> None:
    assert _closeness_label(87.4) == "87%"
    assert _closeness_label(99.6) == "100%"
    assert _closeness_label(0) == "0%"


def test_closeness_label_handles_missing_or_nonnumeric() -> None:
    assert _closeness_label(None) == "—"
    assert _closeness_label("nope") == "—"
    # bool is an int subclass but is not a real closeness value
    assert _closeness_label(True) == "—"


# --- _weighting_note (Similar mode) --------------------------------------


def test_weighting_note_sensitivity_with_source() -> None:
    note = _weighting_note(
        {"weighting": "sensitivity", "weighting_source": "Study 42 — Heat Sink sweep"}
    )
    assert note == "Ranked by input sensitivity from Study 42 — Heat Sink sweep."


def test_weighting_note_sensitivity_without_source() -> None:
    note = _weighting_note({"weighting": "sensitivity", "weighting_source": None})
    assert note == "Ranked by input sensitivity."


def test_weighting_note_uniform() -> None:
    note = _weighting_note({"weighting": "uniform"})
    assert "equal weighting" in note
