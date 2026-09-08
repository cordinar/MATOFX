"""A swept choice is plottable on the study Explore plots (2026-09-02).

Companion to ``test_optimisation_categorical_axes``: the same blind spot, found
one surface along. Here it took a quieter form — ``_varied_inputs`` filters to
``_numeric_inputs``, so an input swept over two materials never appeared in the
X / Y / Z / colour pickers **at all**. Not a wrong plot; a missing option, for
the one variable the study was actually varying.

Choice-valued inputs are now offered, plot as one named column per option, and
carry their level name into the hover — the axis position is an index, and a
tooltip reading ``Plate material: 0.0`` would be the original defect wearing a
different hat.
"""

from __future__ import annotations

from typing import Any

import pytest
from nicegui import Client, ui
from nicegui.page import page as page_decorator

import matofx.ui.theme as theme_module
from matofx.ui._axis_picker import (
    SECTION_HEADER_PREFIX,
    guard_section_headers,
    is_section_header,
)
from matofx.ui._categorical_axis import UNSET_LEVEL
from matofx.ui.pages.studies import (
    _draw_parcoords,
    _draw_scatter,
    _input_levels,
    _option_inputs,
    _plot_levels,
    _plot_variables,
    _var_value,
    sectioned_variables,
    varying_keys,
)

_MATERIALS = ["Aluminium", "Magnesium"]


def _children(materials: list[str | None] | None = None) -> list[dict[str, Any]]:
    chosen = materials or ["Aluminium", "Aluminium", "Magnesium", "Magnesium"]
    out: list[dict[str, Any]] = []
    for i, material in enumerate(chosen):
        inputs: dict[str, Any] = {"fin_height": 10.0 + 5 * i}
        if material is not None:
            inputs["material"] = material
        out.append(
            {
                "job_id": 200 + i,
                "state": "complete",
                "inputs": inputs,
                "scalars": {"t_source": 70.0 + 3 * i},
            }
        )
    return out


def _state(
    materials: list[str | None] | None = None, *, declared: list[str] | None = None
) -> dict[str, Any]:
    return {
        "results": {
            "children": _children(materials),
            "input_labels": {"fin_height": "Fin height", "material": "Plate material"},
            "input_units": {"fin_height": "mm", "material": None},
            "input_options": {"material": declared if declared is not None else _MATERIALS},
            "response_sources": {
                "scalars": [{"name": "t_source", "label": "Peak component temperature"}]
            },
        }
    }


# --- which inputs are offered ----------------------------------------------


def test_a_swept_choice_is_offered_as_a_plot_variable() -> None:
    """The defect: it appeared in no picker, so the swept variable could not
    be plotted against anything."""
    variables = _plot_variables(_state())
    assert "in:material" in variables
    assert variables["in:material"] == "Plate material"


def test_a_single_material_study_can_still_plot_its_material() -> None:
    """Reported from the UI: a study run over one material had no way to plot
    the input it had selected.

    A single level is not a reason to hide it. The user chose that material,
    the run/response matrix shows it, and one column is a true statement about
    every run in the study. "No spread" disqualifies an input from a *ranking*,
    not from a *picture*.
    """
    assert _option_inputs(_children(["Aluminium"] * 4)) == ["material"]


def test_the_single_level_axis_actually_draws(slot: Any) -> None:
    """The end of that report: it has to reach the chart, not just the picker."""
    figure = _scatter(slot, _state(["Aluminium"] * 4), x="in:material", y="out:t_source")

    assert len(figure["data"][0]["x"]) == 4
    assert list(figure["layout"]["xaxis"]["ticktext"]) == _MATERIALS


def test_numbers_are_never_treated_as_choices() -> None:
    """Otherwise a continuous sweep would be squashed onto a handful of ticks."""
    assert "fin_height" not in _option_inputs(_children())


def test_a_non_text_input_is_not_made_an_axis() -> None:
    """Only real option values become columns. A list (a multiselect) or a
    mapping would stringify into a label nobody can read and a column nobody
    can interpret."""
    children = _children()
    children[0]["inputs"]["tags"] = ["a", "b"]
    children[0]["inputs"]["blank"] = ""
    assert _option_inputs(children) == ["material"]


def test_every_input_comes_before_every_output() -> None:
    """This dict also orders the parallel-coordinates axes.

    Appending the choice inputs after the outputs stranded them at the
    right-hand edge of the parallel plot, away from the inputs they belong
    with — reported from the UI.
    """
    keys = list(_plot_variables(_state()))
    last_input = max(i for i, k in enumerate(keys) if k.startswith("in:"))
    first_output = min(i for i, k in enumerate(keys) if k.startswith("out:"))
    assert last_input < first_output


def test_inputs_follow_the_tool_s_own_order() -> None:
    """So the parallel axes read left to right like the tool's form, rather
    than alphabetically or by whichever helper listed them first."""
    state = _state()
    state["results"]["input_labels"] = {"material": "Plate material", "fin_height": "Fin height"}
    keys = [k for k in _plot_variables(state) if k.startswith("in:")]
    assert keys == ["in:material", "in:fin_height"]


def test_an_input_the_descriptor_never_mentions_is_still_offered() -> None:
    """A run carrying an input the label map has lost must not vanish."""
    state = _state()
    state["results"]["input_labels"] = {}
    keys = [k for k in _plot_variables(state) if k.startswith("in:")]
    assert set(keys) == {"in:fin_height", "in:material"}


def test_a_numeric_input_held_constant_is_still_offered() -> None:
    """Same reasoning as the single-material case: the study chose that value,
    the matrix shows it, and one flat axis is a true statement about the runs.
    ``_varied_inputs`` keeps its filter because sensitivity ranks against a
    spread; a plot does not."""
    state = _state()
    for child in state["results"]["children"]:
        child["inputs"]["fan_duty"] = 70.0
    assert "in:fan_duty" in _plot_variables(state)


# --- section headings ------------------------------------------------------


def test_the_picker_is_split_into_inputs_and_outputs() -> None:
    options = sectioned_variables(_plot_variables(_state()))
    keys = list(options)
    headings = [i for i, k in enumerate(keys) if is_section_header(k)]

    assert len(headings) == 2
    assert headings[0] == 0
    assert options[keys[headings[0]]].startswith("INPUTS")
    assert options[keys[headings[1]]].startswith("OUTPUTS")
    assert all(k.startswith("in:") for k in keys[1 : headings[1]])
    assert all(k.startswith("out:") for k in keys[headings[1] + 1 :])


def test_a_heading_is_not_added_for_a_section_with_nothing_in_it() -> None:
    """A study whose runs produced no scalars yet must not show a bare
    "OUTPUTS" heading with nothing under it."""
    options = sectioned_variables({"in:x": "X"})
    assert [v for k, v in options.items() if is_section_header(k)] == [
        next(v for k, v in options.items() if is_section_header(k))
    ]
    assert not any("OUTPUTS" in v for k, v in options.items() if is_section_header(k))


def test_picking_a_heading_snaps_back_and_does_not_redraw(slot: Any) -> None:
    """Quasar can grey out an option, but only via a per-option flag NiceGUI
    normalises away. So a heading is selectable and reverts instead — picking
    one must be a no-op, never an axis with no data.
    """
    options = sectioned_variables(_plot_variables(_state()))
    redraws: list[int] = []
    with slot:
        select = ui.select(options, value="in:fin_height")
    changed = guard_section_headers(select, lambda: redraws.append(1))

    select.value = next(k for k in options if is_section_header(k))
    changed(None)

    assert select.value == "in:fin_height", "the previous axis must come back"
    assert redraws == [], "a heading must not trigger a redraw"


def test_picking_a_real_variable_redraws_and_is_remembered(slot: Any) -> None:
    """The guard must not swallow ordinary changes."""
    options = sectioned_variables(_plot_variables(_state()))
    redraws: list[str] = []
    with slot:
        select = ui.select(options, value="in:fin_height")
    changed = guard_section_headers(select, lambda: redraws.append(str(select.value)))

    select.value = "out:t_source"
    changed(None)

    assert redraws == ["out:t_source"]
    assert select.value == "out:t_source"


def test_a_heading_is_recognisable_without_knowing_the_key_format() -> None:
    assert is_section_header(f"{SECTION_HEADER_PREFIX}in")
    assert not is_section_header("in:material")
    assert not is_section_header("")
    assert not is_section_header(None)


def test_a_heading_carries_a_visible_rule() -> None:
    """It has to read as a divider in the list, not as another variable."""
    options = sectioned_variables({"in:x": "X", "out:y": "Y"})
    headings = [v for k, v in options.items() if is_section_header(k)]
    assert headings[0].startswith("INPUTS ")
    assert all("─" in h for h in headings)


def test_two_sections_never_share_a_heading_key() -> None:
    """Numbered keys, not slugs — two groups must not collide on a word."""
    options = sectioned_variables({"in:x": "X", "out:y": "Y"})
    assert len([k for k in options if is_section_header(k)]) == 2


def test_a_heading_left_in_a_stored_pick_draws_nothing_rather_than_lying(slot: Any) -> None:
    """Belt and braces: a heading reaching the chart must read as "no axis
    chosen", not as an axis with no data."""
    with slot:
        _draw_scatter(
            _state(),
            x_key=f"{SECTION_HEADER_PREFIX}in",
            y_key="out:t_source",
            z_key="",
            colour_key="",
        )
    text = " ".join(
        str(getattr(e, "text", "")) for e in slot.descendants() if getattr(e, "text", "")
    )
    assert "Pick X and Y" in text


def test_a_choice_has_no_numeric_value_by_design() -> None:
    """``_var_value`` still feeds sensitivity and ANOVA, which need numbers."""
    assert _var_value(_children()[0], "in:material") is None


# --- which levels get columns ----------------------------------------------


def test_declared_options_order_the_axis() -> None:
    results = _state(declared=["Magnesium", "Aluminium"])["results"]
    levels = _input_levels(results, results["children"], "material")
    assert levels == ["Magnesium", "Aluminium"]


def test_an_option_the_sweep_never_ran_keeps_its_column() -> None:
    """The empty column is the coverage gap, stated in the plot."""
    results = _state(["Aluminium"] * 4, declared=[*_MATERIALS, "Titanium"])["results"]
    assert _input_levels(results, results["children"], "material") == [*_MATERIALS, "Titanium"]


def test_a_run_with_no_value_gets_a_named_column() -> None:
    results = _state(["Aluminium", None, "Magnesium", "Magnesium"])["results"]
    assert _input_levels(results, results["children"], "material") == [*_MATERIALS, UNSET_LEVEL]


def test_levels_survive_a_tool_that_declares_none() -> None:
    """A descriptor with no enumerated options still has runs to place."""
    results = _state(declared=[])["results"]
    assert _input_levels(results, results["children"], "material") == _MATERIALS


def test_only_choice_variables_get_levels() -> None:
    assert set(_plot_levels(_state())) == {"in:material"}


# --- the charts ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _plain_theme(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the theme lookup, which reads per-user storage a bare Client has not."""
    monkeypatch.setattr(theme_module, "apply_theme_to_plotly", lambda fig, _theme=None: fig)


@pytest.fixture
def slot() -> Any:
    def _unused() -> None:
        """Never called — `page` only needs a target to decorate."""

    client = Client(page_decorator("/__studies_categorical_probe__")(_unused), request=None)
    with client, ui.column() as column:
        yield column


def _figure(container: Any) -> dict[str, Any]:
    plots = [e for e in container.descendants() if isinstance(e, ui.plotly)]
    assert plots, "the panel drew no chart at all"
    figure = plots[0].figure
    return figure if isinstance(figure, dict) else figure.to_plotly_json()


def _scatter(slot: Any, state: dict[str, Any], **keys: str) -> dict[str, Any]:
    with slot:
        _draw_scatter(
            state,
            x_key=keys.get("x", ""),
            y_key=keys.get("y", ""),
            z_key=keys.get("z", ""),
            colour_key=keys.get("colour", ""),
        )
    return _figure(slot)


def test_a_material_on_the_x_axis_plots_every_run(slot: Any) -> None:
    figure = _scatter(slot, _state(), x="in:material", y="out:t_source")

    assert len(figure["data"][0]["x"]) == 4
    assert set(figure["data"][0]["x"]) == {0.0, 1.0}
    assert list(figure["layout"]["xaxis"]["ticktext"]) == _MATERIALS
    assert list(figure["layout"]["xaxis"]["range"]) == [-0.5, 1.5]


def test_the_hover_names_the_material_rather_than_its_column(slot: Any) -> None:
    """``Plate material: 0.0`` would be the original defect in the tooltip."""
    figure = _scatter(slot, _state(), x="in:material", y="out:t_source")
    trace = figure["data"][0]

    assert "Plate material: %{customdata[1]}" in trace["hovertemplate"]
    assert "%{x:" not in trace["hovertemplate"]
    assert [row[1] for row in trace["customdata"]] == [
        "Aluminium",
        "Aluminium",
        "Magnesium",
        "Magnesium",
    ]


def test_a_numeric_axis_keeps_its_rounded_hover(slot: Any) -> None:
    """The regression guard: numeric axes must not lose their formatting."""
    figure = _scatter(slot, _state(), x="in:fin_height", y="out:t_source")
    trace = figure["data"][0]

    assert "%{x:." in trace["hovertemplate"]
    assert list(trace["x"]) == [10.0, 15.0, 20.0, 25.0]
    assert "ticktext" not in (figure["layout"].get("xaxis") or {})


def test_colouring_by_a_material_gives_a_named_stepped_bar(slot: Any) -> None:
    figure = _scatter(slot, _state(), x="in:fin_height", y="out:t_source", colour="in:material")
    marker = figure["data"][0]["marker"]

    assert sorted(set(marker["color"])) == [0.0, 1.0]
    assert (marker["cmin"], marker["cmax"]) == (-0.5, 1.5)
    assert list(marker["colorbar"]["ticktext"]) == _MATERIALS
    stops = [stop for stop, _colour in marker["colorscale"]]
    assert len(set(stops)) < len(stops), "the bar must be banded, not a ramp"


def test_the_colour_hover_names_the_material_too(slot: Any) -> None:
    figure = _scatter(slot, _state(), x="in:fin_height", y="out:t_source", colour="in:material")
    trace = figure["data"][0]

    assert "Plate material: %{customdata[1]}" in trace["hovertemplate"]
    assert trace["customdata"][0][1] == "Aluminium"


def test_a_numeric_colour_still_shows_its_value(slot: Any) -> None:
    figure = _scatter(slot, _state(), x="out:t_source", y="in:fin_height", colour="in:fin_height")
    trace = figure["data"][0]

    assert "Fin height (mm): %{customdata[1]:." in trace["hovertemplate"]
    assert "cmin" not in trace["marker"]


def test_a_material_on_the_third_axis_is_named(slot: Any) -> None:
    """3-D builds axes through ``scene`` — a separate path from the 2-D ones."""
    figure = _scatter(slot, _state(), x="in:fin_height", y="out:t_source", z="in:material")
    zaxis = figure["layout"]["scene"]["zaxis"]

    assert list(zaxis["ticktext"]) == _MATERIALS
    assert len(figure["data"][0]["z"]) == 4


def test_two_choice_axes_are_nudged_apart(slot: Any) -> None:
    """With nothing continuous left, every run would land on one of two dots."""
    state = _state()
    state["results"]["input_options"]["coolant"] = ["Air", "Water"]
    for i, child in enumerate(state["results"]["children"]):
        child["inputs"]["coolant"] = "Air" if i % 2 else "Water"

    figure = _scatter(slot, state, x="in:material", y="in:coolant")
    assert not all(float(x).is_integer() for x in figure["data"][0]["x"])


def test_a_choice_becomes_a_parallel_coordinates_axis(slot: Any) -> None:
    with slot:
        _draw_parcoords(_state(), colour_key="in:material")
    figure = _figure(slot)
    dims = {d["label"]: d for d in figure["data"][0]["dimensions"]}

    assert list(dims["Plate material"]["ticktext"]) == _MATERIALS
    assert set(dims["Plate material"]["values"]) == {0.0, 1.0}
    assert "ticktext" not in dims["Fin height (mm)"]


def test_the_parallel_coordinates_colour_bar_names_its_levels(slot: Any) -> None:
    with slot:
        _draw_parcoords(_state(), colour_key="in:material")
    line = _figure(slot)["data"][0]["line"]

    assert list(line["colorbar"]["ticktext"]) == _MATERIALS
    assert (line["cmin"], line["cmax"]) == (-0.5, 1.5)


# --- hiding the axes that do not vary --------------------------------------


def _held_fan_duty() -> dict[str, Any]:
    """A study with one numeric input held at a single value throughout.

    The label map has to know it, or "Fan duty not in labels" would pass
    vacuously against the raw-name fallback.
    """
    state = _state()
    state["results"]["input_labels"]["fan_duty"] = "Fan duty"
    for child in state["results"]["children"]:
        child["inputs"]["fan_duty"] = 70.0
    return state


def test_an_axis_every_run_shares_is_reported_as_unvarying() -> None:
    assert varying_keys({"a": [1.0, 1.0, 1.0], "b": [1.0, 2.0, 3.0]}) == ["b"]


def test_a_missing_value_does_not_make_an_axis_look_varied() -> None:
    """``None`` is "no value plotted", not a second level."""
    assert varying_keys({"a": [1.0, None, 1.0]}) == []


def test_the_parallel_plot_can_drop_the_flat_axes(slot: Any) -> None:
    """A tool with many held inputs otherwise draws a wall of flat lines
    beside the two that matter."""
    state = _held_fan_duty()

    with slot:
        _draw_parcoords(state, colour_key="", hide_constant=True)
    labels = {d["label"] for d in _figure(slot)["data"][0]["dimensions"]}

    assert "Fin height (mm)" in labels
    assert "Fan duty" not in labels
    assert "Plate material" in labels, "two materials ran — this one varies"


def test_hiding_is_off_unless_asked(slot: Any) -> None:
    """The default has to keep showing what the study chose."""
    state = _held_fan_duty()

    with slot:
        _draw_parcoords(state, colour_key="")
    labels = {d["label"] for d in _figure(slot)["data"][0]["dimensions"]}

    assert "Fan duty" in labels


def test_a_study_where_nothing_varies_still_draws(slot: Any) -> None:
    """Hiding everything would leave an empty frame and no explanation."""
    state = _state(["Aluminium"] * 4)
    for child in state["results"]["children"]:
        child["inputs"] = {"fin_height": 10.0, "material": "Aluminium"}
        child["scalars"] = {"t_source": 70.0}

    with slot:
        _draw_parcoords(state, colour_key="", hide_constant=True)
    assert _figure(slot)["data"][0]["dimensions"]
