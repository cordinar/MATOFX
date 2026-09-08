"""Coverage-panel helpers (ADR-0020 §11, slice 3).

Per house style the live NiceGUI rendering is exercised through the API
client path; these unit tests pin the pure-Python functions that decide what
the panel *says* — which is where the plain-English requirement of
``feedback_users_are_not_modellers`` actually lives.
"""

from __future__ import annotations

import pytest

from matofx.ui._coverage_panel import (
    MODES_EXPLAINER,
    envelope_rows,
    factor_row_text,
    factor_table_rows,
    headline,
    modes_count_note,
    pairs_note,
    plan_gap_note,
    plan_today_line,
    scatter_axes,
    scatter_pairs,
    suggestion_columns,
    suggestion_summary,
    support_chip,
    support_is_reportable,
    support_note,
    unmovable_note,
    verdict_chip,
)

_GOOD = {
    "verdict": "good",
    "message": "These runs cover the range well.",
    "criterion": "model_free",
    "coverage_score": 0.81,
    "diagnostics": {
        "n_points": 25,
        "n_excluded_invalid": 0,
        "n_excluded_filtered": 0,
        "n_outside_envelope": 0,
        "grouping_mode": "n/a",
    },
    "per_factor": [],
    "warnings": [],
    "envelope": [],
    "points": [],
}


def _report(**overrides: object) -> dict:
    out = {k: (v.copy() if isinstance(v, dict | list) else v) for k, v in _GOOD.items()}
    out.update(overrides)
    return out


# --- the verdict chip -----------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "expected_label"),
    [
        ("good", "Well covered"),
        ("thin", "Patchy"),
        ("poor", "Poorly covered"),
        ("insufficient", "Not enough to tell"),
    ],
)
def test_every_verdict_has_a_plain_english_label(verdict: str, expected_label: str) -> None:
    label, _colour = verdict_chip(verdict)
    assert label == expected_label


def test_insufficient_is_not_shown_as_a_failure() -> None:
    # "We cannot tell" and "we can tell, and it is bad" must not look alike.
    _, insufficient = verdict_chip("insufficient")
    _, poor = verdict_chip("poor")
    assert insufficient != poor


def test_an_unknown_verdict_degrades_rather_than_raising() -> None:
    label, colour = verdict_chip("something_new")
    assert label
    assert colour


# --- the headline ---------------------------------------------------------


def test_the_headline_is_the_engine_message() -> None:
    assert headline(_report()) == "These runs cover the range well."


def test_the_headline_names_the_excluded_runs_when_there_are_any() -> None:
    # "62 of 100" is never a surprise (§11.2).
    text = headline(
        _report(
            diagnostics={
                "n_points": 62,
                "n_excluded_invalid": 30,
                "n_excluded_filtered": 8,
                "n_outside_envelope": 0,
                "grouping_mode": "n/a",
            }
        )
    )
    assert "62" in text
    assert "100" in text


def test_the_headline_stays_silent_when_nothing_was_excluded() -> None:
    assert "of" not in headline(_report()).split(".")[-1]


def test_a_report_missing_its_message_still_produces_text() -> None:
    assert headline(_report(message="")).strip()


# --- the per-factor strip -------------------------------------------------


def test_a_continuous_axis_reports_its_gap() -> None:
    text = factor_row_text(
        {
            "name": "thickness",
            "kind": "continuous",
            "measure": "covering_radius",
            "value": 0.4,
            "verdict": "thin",
            "missing_values": [],
            "actionable": True,
            "scale": "linear",
        }
    )
    assert "thickness" in text
    assert "%" in text


def test_an_integer_axis_names_its_missing_values() -> None:
    text = factor_row_text(
        {
            "name": "ply_count",
            "kind": "integer",
            "measure": "lattice_occupancy",
            "value": 0.6,
            "verdict": "thin",
            "missing_values": ["2", "4"],
            "actionable": True,
            "scale": "linear",
        }
    )
    assert "2" in text
    assert "4" in text


def test_a_complete_lattice_says_so_rather_than_naming_nothing() -> None:
    text = factor_row_text(
        {
            "name": "ply_count",
            "kind": "integer",
            "measure": "lattice_occupancy",
            "value": 1.0,
            "verdict": "good",
            "missing_values": [],
            "actionable": True,
            "scale": "linear",
        }
    )
    assert "every" in text.lower() or "all" in text.lower()


def test_a_pinned_axis_says_it_cannot_be_varied() -> None:
    text = factor_row_text(
        {
            "name": "mesh_density",
            "kind": "pinned",
            "measure": "pinned",
            "value": 1.0,
            "verdict": "good",
            "missing_values": [],
            "actionable": False,
            "scale": "linear",
        }
    )
    assert "same" in text.lower() or "held" in text.lower()


def test_a_derived_axis_speaks_in_snapshots_not_seconds() -> None:
    # The column is a 0-based snapshot index; the platform has no seconds
    # to quote, so it must not pretend otherwise.
    text = factor_row_text(
        {
            "name": "time",
            "kind": "derived",
            "measure": "span",
            "value": 0.5,
            "verdict": "thin",
            "missing_values": [],
            "actionable": False,
            "scale": "linear",
        }
    )
    assert "snapshot" in text.lower()
    assert " s" not in text.lower().replace("snapshots", "")


def test_a_log_axis_says_it_is_measured_on_a_log_scale() -> None:
    text = factor_row_text(
        {
            "name": "conductivity",
            "kind": "continuous",
            "measure": "covering_radius",
            "value": 0.9,
            "verdict": "good",
            "missing_values": [],
            "actionable": True,
            "scale": "log",
        }
    )
    assert "log" in text.lower()


# --- the envelope form ----------------------------------------------------


def test_envelope_rows_split_editable_from_read_only() -> None:
    rows = envelope_rows(
        [
            {
                "name": "thickness",
                "kind": "continuous",
                "lo": 1.0,
                "hi": 9.0,
                "scale": "linear",
                "levels": None,
                "pinned_value": None,
                "actionable": True,
            },
            {
                "name": "material",
                "kind": "categorical",
                "lo": None,
                "hi": None,
                "scale": "linear",
                "levels": ["cfrp", "gfrp"],
                "pinned_value": None,
                "actionable": True,
            },
            {
                "name": "mesh",
                "kind": "pinned",
                "lo": None,
                "hi": None,
                "scale": "linear",
                "levels": None,
                "pinned_value": "fine",
                "actionable": False,
            },
            {
                "name": "time",
                "kind": "derived",
                "lo": 0.0,
                "hi": 23.0,
                "scale": "linear",
                "levels": None,
                "pinned_value": None,
                "actionable": False,
            },
        ]
    )
    by_name = {r.name: r for r in rows}
    assert by_name["thickness"].editable is True
    assert by_name["material"].editable is False  # levels are shown, not typed
    assert by_name["mesh"].editable is False
    assert by_name["time"].editable is False


def test_a_pinned_row_explains_why_it_is_read_only() -> None:
    (row,) = envelope_rows(
        [
            {
                "name": "mesh",
                "kind": "pinned",
                "lo": None,
                "hi": None,
                "scale": "linear",
                "levels": None,
                "pinned_value": "fine",
                "actionable": False,
            },
        ]
    )
    assert "fine" in row.note
    assert "vary" in row.note.lower() or "change" in row.note.lower()


def test_a_categorical_row_lists_its_levels() -> None:
    (row,) = envelope_rows(
        [
            {
                "name": "material",
                "kind": "categorical",
                "lo": None,
                "hi": None,
                "scale": "linear",
                "levels": ["cfrp", "gfrp"],
                "pinned_value": None,
                "actionable": True,
            },
        ]
    )
    assert "cfrp" in row.note
    assert "gfrp" in row.note


def test_envelope_rows_are_ordered_editable_first() -> None:
    # The form asks a question; the things the user can answer come first.
    rows = envelope_rows(
        [
            {
                "name": "mesh",
                "kind": "pinned",
                "lo": None,
                "hi": None,
                "scale": "linear",
                "levels": None,
                "pinned_value": "fine",
                "actionable": False,
            },
            {
                "name": "thickness",
                "kind": "continuous",
                "lo": 1.0,
                "hi": 9.0,
                "scale": "linear",
                "levels": None,
                "pinned_value": None,
                "actionable": True,
            },
        ]
    )
    assert [r.name for r in rows] == ["thickness", "mesh"]


# --- the scatter matrix ---------------------------------------------------


def test_scatter_pairs_are_every_combination_of_the_measured_axes() -> None:
    envelope = [{"name": n, "kind": "continuous", "actionable": True} for n in ("a", "b", "c")]
    assert scatter_pairs(envelope, cap=8) == [("a", "b"), ("a", "c"), ("b", "c")]


def test_scatter_pairs_exclude_axes_no_point_can_move() -> None:
    envelope = [
        {"name": "a", "kind": "continuous", "actionable": True},
        {"name": "b", "kind": "continuous", "actionable": True},
        {"name": "mesh", "kind": "pinned", "actionable": False},
        {"name": "time", "kind": "derived", "actionable": False},
    ]
    assert scatter_pairs(envelope, cap=8) == [("a", "b")]


def test_scatter_pairs_are_capped_because_eleven_factors_is_unreadable() -> None:
    envelope = [{"name": f"f{i}", "kind": "continuous", "actionable": True} for i in range(11)]
    pairs = scatter_pairs(envelope, cap=4)
    names = {n for pair in pairs for n in pair}
    assert len(names) == 4
    assert len(pairs) == 6


def test_the_axes_plotted_are_the_least_covered_ones() -> None:
    """The panel names the thinnest axis; it has to be one of the ones drawn.

    The old rule took the first `cap` axes in envelope order — sorted by
    internal name — so on the real heat-sink archive it never plotted
    thermal_conductivity, the very axis the headline called out.
    """
    envelope = [
        {"name": n, "label": n.title(), "kind": "continuous", "actionable": True}
        for n in ("alpha", "beta", "gamma", "delta", "omega")
    ]
    per_factor = [
        {"name": "alpha", "value": 0.99},
        {"name": "beta", "value": 0.98},
        {"name": "gamma", "value": 0.97},
        {"name": "delta", "value": 0.96},
        {"name": "omega", "value": 0.20},  # the thin one, last alphabetically
    ]
    names = {n for pair in scatter_pairs(envelope, per_factor, cap=2) for n in pair}
    assert names == {"omega", "delta"}


def test_scatter_axis_order_is_stable_when_scores_tie() -> None:
    """A matrix that reshuffles between reloads is its own bug."""
    envelope = [
        {"name": n, "label": label, "kind": "continuous", "actionable": True}
        for n, label in (("b", "Second"), ("a", "First"), ("c", "Third"))
    ]
    per_factor = [{"name": n, "value": 0.5} for n in ("a", "b", "c")]
    assert scatter_axes(envelope, per_factor, cap=2) == ["a", "b"]


def test_a_single_measured_axis_yields_no_pairs() -> None:
    assert scatter_pairs([{"name": "a", "kind": "continuous", "actionable": True}], cap=8) == []


def test_scatter_pairs_of_an_empty_envelope_is_empty() -> None:
    assert scatter_pairs([], cap=8) == []


# --- §11.5 the suggestion summary ----------------------------------------


def test_a_summary_names_the_response_when_one_drove_the_batch() -> None:
    text = suggestion_summary({"response_name": "peak_temperature", "points": [{"a": 1}, {"a": 2}]})
    assert "peak_temperature" in text
    assert "2 runs" in text


def test_a_summary_claims_no_response_at_cohort_preview() -> None:
    # Nothing has been fitted, so attributing the batch to a response would
    # be a claim this path cannot make.
    text = suggestion_summary({"response_name": "", "points": [{"a": 1}]})
    assert "for " not in text
    assert "1 run" in text


def test_an_empty_batch_says_so_rather_than_showing_a_bare_table() -> None:
    assert "no runs" in suggestion_summary({"response_name": "", "points": []}).lower()


def test_suggested_columns_put_what_actually_varies_first() -> None:
    # The varying inputs are what the user is being asked to approve; the
    # shared ones are context.
    points = [
        {"mesh": "fine", "thickness": 1.0, "load": 5.0},
        {"mesh": "fine", "thickness": 4.0, "load": 5.0},
    ]
    assert suggestion_columns(points)[0] == "thickness"
    assert set(suggestion_columns(points)) == {"mesh", "thickness", "load"}


def test_suggested_columns_of_an_empty_batch_is_empty() -> None:
    assert suggestion_columns([]) == []


# --- §8 the predict-time support badge -----------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("inside", "Well supported"),
        ("edge", "Near the edge"),
        ("outside", "Outside the trained range"),
    ],
)
def test_every_support_status_has_a_plain_english_label(status: str, expected: str) -> None:
    label, _colour = support_chip(status)
    assert label == expected


def test_an_unknown_support_status_degrades_rather_than_raising() -> None:
    label, colour = support_chip("something_new")
    assert label
    assert colour


def test_the_note_carries_the_reason_that_names_the_input() -> None:
    note = support_note(
        {"status": "outside", "reason": "'thickness' is 40, outside the 1 to 9 range..."}
    )
    assert "thickness" in note


def test_a_missing_reason_still_says_something_useful() -> None:
    assert support_note({"status": "outside"}).strip()


def test_no_badge_is_shown_when_there_is_no_verdict() -> None:
    # An empty badge invites the reader to assume the blank means fine.
    assert support_is_reportable(None) is False
    assert support_is_reportable({}) is False
    assert support_is_reportable({"status": "inside"}) is True


# --- an empty pairwise sweep must say why ---------------------------------


def test_an_unscorable_sweep_explains_itself() -> None:
    """Zero pairs scored is a result, not silence.

    A cohort too small to judge its pairs rendered the section blank, which
    reads as "no problems found" — the exact confusion coverage exists to
    remove. The note now says none could be scored, how many runs a pair needs,
    and that this is not a clean bill of health.
    """
    note = pairs_note({"n_pairs_examined": 0, "n_pairs_total": 6, "min_points_per_pair": 20})
    assert "6" in note
    assert "20" in note
    assert "not a clean bill of health" in note


def test_no_pairs_to_examine_stays_silent() -> None:
    """A single-factor cohort has no pairs; there is nothing to explain."""
    assert pairs_note({"n_pairs_examined": 0, "n_pairs_total": 0}) == ""


def test_a_scored_sweep_keeps_its_original_note() -> None:
    note = pairs_note({"n_pairs_examined": 6, "n_pairs_total": 6})
    assert "All 6 pairs were checked." in note


# --- a categorical axis has options, not an interval -----------------------


def _categorical_report(observed, declared, *, kind="pinned"):
    axis = {"name": "material", "kind": kind, "declared_levels": list(declared)}
    if kind == "pinned":
        axis["pinned_value"] = observed[0] if observed else None
        axis["levels"] = None
    else:
        axis["levels"] = list(observed)
    return {
        "per_factor": [
            {
                "name": "material",
                "label": "Plate material",
                "kind": kind,
                "precision": None,
                "simulated_lo": None,
                "simulated_hi": None,
                "declared_lo": None,
                "declared_hi": None,
                "verdict": "good",
                "declared_verdict": "insufficient",
                "measure": "pinned",
            }
        ],
        "envelope": [axis],
    }


def test_a_categorical_states_its_options_not_a_dash() -> None:
    """ "No stated range" was wrong: an option list *is* the range.

    The descriptor enumerates the options, so quoting an em dash read as
    "unknown" for something perfectly well known — and hid that only one of
    two materials had ever been run.
    """
    (row,) = factor_table_rows(_categorical_report(["Aluminium"], ["Aluminium", "Magnesium"]))
    assert row["simulated_range"] == "Aluminium"
    assert row["declared_range"] == "Aluminium, Magnesium"
    assert row["declared_chip"] == "1 of 2 options tried"
    assert row["declared_colour"] == "negative"


def test_a_fully_explored_categorical_says_so() -> None:
    (row,) = factor_table_rows(
        _categorical_report(["Air", "Water"], ["Air", "Water"], kind="categorical")
    )
    assert row["simulated_range"] == "Air, Water"
    assert row["declared_chip"] == "All 2 options tried"
    assert row["declared_colour"] == "positive"


def test_a_categorical_with_no_declared_options_still_says_so() -> None:
    """A free-text categorical genuinely has no option list to quote."""
    (row,) = factor_table_rows(_categorical_report(["Custom"], []))
    assert row["declared_range"] == "—"
    assert row["declared_chip"] == "No stated range"


def test_a_numeric_axis_is_unaffected() -> None:
    report = {
        "per_factor": [
            {
                "name": "fin_height",
                "label": "Fin height",
                "kind": "continuous",
                "precision": 0.1,
                "simulated_lo": 15.2,
                "simulated_hi": 24.6,
                "declared_lo": 5.0,
                "declared_hi": 45.0,
                "verdict": "good",
                "declared_verdict": "thin",
                "measure": "covering_radius",
            }
        ],
        "envelope": [{"name": "fin_height", "kind": "continuous", "declared_levels": []}],
    }
    (row,) = factor_table_rows(report)
    assert "15.2" in row["simulated_range"]
    assert "45.0" in row["declared_range"]
    assert row["declared_chip"] == "Partly covered"


def test_a_pinned_numeric_keeps_its_declared_bounds() -> None:
    """``pinned`` is not a synonym for categorical.

    A numeric input held at one value is pinned too, and it still has real
    declared bounds. Reading ``pinned`` as "categorical" threw them away and
    reported "No stated range" for an input with a perfectly good 10-120 range
    — losing the very "Barely covered" verdict that says only one point of it
    was ever run.
    """
    report = {
        "per_factor": [
            {
                "name": "base_temperature_rise",
                "label": "Base temperature rise",
                "kind": "pinned",
                "measure": "pinned",
                "precision": 1.0,
                "simulated_lo": 60.0,
                "simulated_hi": 60.0,
                "declared_lo": 10.0,
                "declared_hi": 120.0,
                "verdict": "good",
                "declared_verdict": "poor",
            }
        ],
        "envelope": [
            {
                "name": "base_temperature_rise",
                "kind": "pinned",
                "pinned_value": 60.0,
                "declared_levels": [],
            }
        ],
    }
    (row,) = factor_table_rows(report)
    assert row["simulated_range"] == "60"
    assert "10" in row["declared_range"]
    assert "120" in row["declared_range"]
    assert row["declared_chip"] == "Barely covered"


# --- modes have to be explained to be worth showing ------------------------


def test_the_modes_explainer_avoids_jargon_and_markdown() -> None:
    """Two charts axed "POD mode" tell an unprepared reader nothing.

    Plain text, not markdown: these panels render through ``ui.label``, which
    would show the asterisks verbatim.
    """
    assert "pattern" in MODES_EXPLAINER
    assert "POD" not in MODES_EXPLAINER
    assert "**" not in MODES_EXPLAINER


def test_one_mode_is_described_as_a_shape_that_did_not_change() -> None:
    note = modes_count_note(1, 0.994)
    assert "One pattern" in note
    assert "magnitude" in note
    assert "99%" in note
    assert "Together" not in note  # singular must not read as plural


def test_several_modes_are_described_as_a_changing_shape() -> None:
    note = modes_count_note(3, 0.991)
    assert "3 patterns" in note
    assert "shape" in note
    assert "Together they account for 99%" in note


def test_no_modes_says_nothing() -> None:
    """A scalar-only fit has no modes; silence is the right answer."""
    assert modes_count_note(None) == ""
    assert modes_count_note(0) == ""


# --- what the batch-size table says (2026-09-03) ---------------------------


def test_the_gap_leads_when_there_is_geometry_to_measure() -> None:
    line = plan_today_line({"measured_axes": 3, "current_gap": 0.24})
    assert "24% of the space" in line


def test_a_choice_only_space_does_not_quote_a_zero_gap() -> None:
    """The covering radius is 0.0 there because there is no distance to
    measure — and "biggest untried gap is 0%" is the most flattering thing
    the table could say about an archive that has never run half its options.
    """
    line = plan_today_line({"measured_axes": 0, "current_levels_short": 2})
    assert "0%" not in line
    assert "2 options" in line


def test_a_choice_only_space_with_nothing_short_says_so() -> None:
    line = plan_today_line({"measured_axes": 0, "current_levels_short": 0})
    assert "every option" in line


def test_the_gap_explainer_says_the_column_is_silent_for_choices() -> None:
    note = plan_gap_note({"measured_axes": 0})
    assert "no distance to measure" in note
    assert "option counts" in note


def test_nothing_is_said_when_the_batch_can_move_every_measured_input() -> None:
    assert unmovable_note([]) == ""


def test_a_flat_table_explains_itself_rather_than_looking_broken() -> None:
    """Reported as rows that do not improve at any size. They are right: the
    figures cover an input the batch cannot reach, so its gap cannot close.
    Without this the obvious wrong conclusion is "ask for more runs".
    """
    note = unmovable_note(["Fin height"])
    assert "Fin height" in note
    assert "unticked it" in note
    assert "Only the ones I ticked" in note


def test_several_left_out_inputs_are_named_as_a_list() -> None:
    note = unmovable_note(["Fin height", "Fin count", "Fan speed"])
    assert "Fin height, Fin count and Fan speed" in note
    assert "unticked them" in note
