"""A categorical factor is plottable in the design-space explorer (2026-09-02).

Every axis in the explorer went through ``_num``, which returns ``None`` for a
string. A factor with options — a material, a solver — therefore had no number,
and the two surfaces failed differently:

- as the **colour**, ``_num(...) or 0.0`` gave *every* run the value 0, so two
  materials drew as one flat colour on a continuous bar;
- as an **axis**, every point was skipped and the panel reported "No numeric
  runs to plot yet", which was true about what it did and wrong about why.

Levels now take evenly-spaced positions with the level's *name* on the tick,
and a categorical colour gets a stepped bar labelled per level. These tests
cover the placement maths and then render the real panel, because the
positions only matter if they reach the figure.
"""

from __future__ import annotations

from typing import Any

import pytest
from nicegui import Client, ui
from nicegui.page import page as page_decorator

import matofx.ui.theme as theme_module
from matofx.ui.pages.optimisation import (
    UNSET_LEVEL,
    _factors_from_config,
    _render_objective_scatter,
    axis_categories,
    category_axis,
    category_colours,
    category_positions,
    stepped_colorscale,
)

_MATERIALS = ["Aluminium", "Magnesium"]


# --- ordering the columns --------------------------------------------------


def test_declared_order_survives_alphabetical_sorting() -> None:
    """``Low / Medium / High`` must not become ``High / Low / Medium``."""
    ordered = ["Low", "Medium", "High"]
    assert axis_categories(ordered, ["High", "Low"]) == ordered


def test_an_option_the_search_never_tried_still_gets_a_column() -> None:
    """The empty column is the point — it says the optimiser never went there."""
    assert axis_categories(_MATERIALS, ["Aluminium"] * 5) == _MATERIALS


def test_a_level_that_was_run_but_never_declared_is_kept() -> None:
    """A run that exists has to be plottable; dropping it is how "nothing is
    here" got reported for an archive that was full."""
    assert axis_categories(_MATERIALS, ["Titanium"]) == [*_MATERIALS, "Titanium"]


def test_categories_are_not_duplicated_by_repeated_runs() -> None:
    assert axis_categories(_MATERIALS, ["Aluminium", "Aluminium", "Magnesium"]) == _MATERIALS


# --- placing the runs ------------------------------------------------------


def test_a_level_maps_to_its_own_column() -> None:
    assert category_positions(_MATERIALS, ["Magnesium", "Aluminium"]) == [1.0, 0.0]


def test_positions_are_exact_without_a_nudge() -> None:
    """Off by default: the explorer is read run-by-run (hover, Pareto rings),
    so a nudged marker would sit where it does not belong."""
    assert category_positions(_MATERIALS, ["Aluminium"] * 4) == [0.0, 0.0, 0.0, 0.0]


def test_a_nudge_separates_runs_but_keeps_them_in_their_column() -> None:
    positions = category_positions(_MATERIALS, ["Aluminium"] * 4, jitter=True)
    assert len({round(p, 9) for p in positions}) == 4  # type: ignore[arg-type]
    assert all(abs(p) < 0.5 for p in positions)  # type: ignore[arg-type]


def test_a_nudge_is_deterministic() -> None:
    """A screenshot taken today and one taken in October have to match."""
    values = ["Aluminium", "Magnesium"] * 6
    assert category_positions(_MATERIALS, values, jitter=True) == category_positions(
        _MATERIALS, values, jitter=True
    )


def test_a_value_with_no_column_is_reported_not_guessed() -> None:
    assert category_positions(_MATERIALS, ["Titanium"]) == [None]


# --- the stepped colour bar ------------------------------------------------


def test_each_level_lands_inside_its_own_flat_band() -> None:
    """The property that makes the bar readable: with ``cmin=-0.5`` and
    ``cmax=n-0.5``, level *i* must normalise into band *i*, not straddle two.
    """
    colours = category_colours(len(_MATERIALS))
    scale = stepped_colorscale(colours)
    n = len(colours)
    for i in range(n):
        position = (i + 0.5) / n  # where cmin / cmax put level i
        band = {c for stop, c in scale if i / n <= stop <= (i + 1) / n}
        assert colours[i] in band
        assert i / n < position < (i + 1) / n


def test_a_band_is_flat_so_the_eye_cannot_interpolate() -> None:
    """A gradient between two materials would imply a halfway material."""
    scale = stepped_colorscale(["#111111", "#222222"])
    assert scale == [[0.0, "#111111"], [0.5, "#111111"], [0.5, "#222222"], [1.0, "#222222"]]


def test_one_level_still_gets_a_colour() -> None:
    assert len(category_colours(1)) == 1
    assert category_colours(0) == []


def test_the_axis_ticks_carry_names_not_indices() -> None:
    axis = category_axis(_MATERIALS)
    assert axis["tickvals"] == [0, 1]
    assert axis["ticktext"] == _MATERIALS
    assert axis["range"] == [-0.5, 1.5]


# --- the config actually carries the levels --------------------------------


def test_the_declared_choices_reach_the_plot() -> None:
    """The options were always in the config; they were discarded one function
    short of the plot, which is the whole defect."""
    facs = _factors_from_config(
        {
            "factors": [
                {"name": "fin_height", "type": "float", "min": 5.0, "max": 45.0},
                {"name": "material", "type": "categorical", "choices": _MATERIALS},
            ]
        },
        {"material": "Plate material"},
        {"fin_height": "mm"},
    )
    assert facs[0]["levels"] == []
    assert facs[1]["levels"] == _MATERIALS
    assert facs[1]["label"] == "Plate material"


def test_a_numeric_factor_is_never_given_levels() -> None:
    """A numeric factor with a stray ``choices`` must stay numeric — treating
    it as categorical would squash a continuous sweep onto two ticks."""
    facs = _factors_from_config(
        {"factors": [{"name": "x", "type": "float", "min": 0.0, "max": 1.0, "choices": [1, 2]}]},
        {},
        {},
    )
    assert facs[0]["levels"] == []


# --- the panel itself ------------------------------------------------------


@pytest.fixture(autouse=True)
def _plain_theme(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the theme lookup, which reads per-user storage a bare Client has not."""
    monkeypatch.setattr(theme_module, "apply_theme_to_plotly", lambda fig, _theme=None: fig)


@pytest.fixture
def slot() -> Any:
    def _unused() -> None:
        """Never called — `page` only needs a target to decorate."""

    client = Client(page_decorator("/__explorer_categorical_probe__")(_unused), request=None)
    with client, ui.column() as column:
        yield column


_OBJECTIVES: list[dict[str, Any]] = [
    {"name": "mass", "label": "Plate + fin mass", "unit": "g", "direction": "min"},
    {"name": "t_source", "label": "Peak component temperature", "unit": "degC", "direction": "min"},
]
_FACTORS: list[dict[str, Any]] = [
    {"key": "fin_height", "label": "Fin height", "unit": "mm", "levels": []},
    {"key": "material", "label": "Plate material", "unit": "", "levels": _MATERIALS},
    {"key": "coolant", "label": "Coolant", "unit": "", "levels": ["Air", "Water"]},
]
# Axis indices: 0 mass, 1 peak temp, 2 fin height, 3 material, 4 coolant.
_MATERIAL_AXIS, _COOLANT_AXIS = 3, 4


def _derived(materials: list[str | None] | None = None) -> list[dict[str, Any]]:
    chosen = materials or ["Aluminium", "Aluminium", "Magnesium", "Magnesium"]
    rows: list[dict[str, Any]] = []
    for i, material in enumerate(chosen):
        inputs: dict[str, Any] = {"fin_height": 10.0 + 5 * i, "coolant": "Air"}
        if material is not None:
            inputs["material"] = material
        rows.append(
            {
                "job_id": 100 + i,
                "batch": 1,
                "obj_vals": [300.0 - 25 * i, 70.0 + 3 * i],
                "inputs": inputs,
                "scalars": {"mass": 300.0 - 25 * i},
                "feasible": True,
                "on_front": i == 0,
            }
        )
    return rows


def _figure(container: Any) -> dict[str, Any]:
    plots = [e for e in container.descendants() if isinstance(e, ui.plotly)]
    assert plots, "the panel drew no chart at all"
    figure = plots[0].figure
    return figure if isinstance(figure, dict) else figure.to_plotly_json()


def _render(slot: Any, prefs: dict[str, Any], rows: list[dict[str, Any]] | None = None) -> Any:
    with slot:
        _render_objective_scatter(
            _derived() if rows is None else rows,
            _OBJECTIVES,
            _FACTORS,
            {"mass": "Plate + fin mass"},
            {"mass": "g"},
            prefs=prefs,
        )
    return slot


def test_colouring_by_a_material_gives_two_colours_not_two_zeros(slot: Any) -> None:
    """The reported symptom, checked at the figure: every point was 0, one colour."""
    figure = _figure(_render(slot, {"x": 0, "y": 1, "z": -1, "c": f"a{_MATERIAL_AXIS}"}))
    marker = figure["data"][0]["marker"]

    assert sorted(set(marker["color"])) == [0.0, 1.0], "both materials must be distinguishable"
    assert (marker["cmin"], marker["cmax"]) == (-0.5, 1.5)
    assert list(marker["colorbar"]["ticktext"]) == _MATERIALS, "the bar must name the levels"
    assert list(marker["colorbar"]["tickvals"]) == [0, 1]


def test_a_material_on_an_axis_plots_every_run(slot: Any) -> None:
    """It used to plot none of them and then blame the data."""
    rows = _derived()
    figure = _figure(_render(slot, {"x": _MATERIAL_AXIS, "y": 1, "z": -1, "c": "_batch"}, rows))

    assert len(figure["data"][0]["x"]) == len(rows)
    assert set(figure["data"][0]["x"]) == {0.0, 1.0}
    assert list(figure["layout"]["xaxis"]["ticktext"]) == _MATERIALS
    assert list(figure["layout"]["xaxis"]["range"]) == [-0.5, 1.5]


def test_an_untried_option_still_shows_as_an_empty_column(slot: Any) -> None:
    """Nobody ran magnesium — the gap in the search is what the column says."""
    rows = _derived(["Aluminium"] * 4)
    figure = _figure(_render(slot, {"x": _MATERIAL_AXIS, "y": 1, "z": -1, "c": "_batch"}, rows))

    assert list(figure["layout"]["xaxis"]["ticktext"]) == _MATERIALS
    assert set(figure["data"][0]["x"]) == {0.0}


def test_a_continuous_partner_axis_leaves_positions_exact(slot: Any) -> None:
    """No nudge here: the temperature axis already spreads the cloud, and an
    exact column is what the hover and the Pareto rings get read against."""
    figure = _figure(_render(slot, {"x": _MATERIAL_AXIS, "y": 1, "z": -1, "c": "_batch"}))
    assert all(float(x).is_integer() for x in figure["data"][0]["x"])


def test_two_option_axes_are_nudged_apart(slot: Any) -> None:
    """Otherwise four runs — or four hundred — draw as one dot per combination."""
    figure = _figure(
        _render(slot, {"x": _MATERIAL_AXIS, "y": _COOLANT_AXIS, "z": -1, "c": "_batch"})
    )
    xs = figure["data"][0]["x"]
    assert len(xs) == 4
    assert not all(float(x).is_integer() for x in xs)
    assert len(set(xs)) > 1, "runs of one material must not stack"


def test_a_run_missing_the_factor_gets_a_named_column(slot: Any) -> None:
    """It has to stay visible; silently dropping it is the defect in miniature."""
    rows = _derived(["Aluminium", None, "Magnesium", "Magnesium"])
    figure = _figure(_render(slot, {"x": _MATERIAL_AXIS, "y": 1, "z": -1, "c": "_batch"}, rows))

    assert list(figure["layout"]["xaxis"]["ticktext"]) == [*_MATERIALS, UNSET_LEVEL]
    assert len(figure["data"][0]["x"]) == len(rows)


def test_a_material_on_the_third_axis_is_named_too(slot: Any) -> None:
    """3-D builds its axes through ``scene`` — a separate code path from 2-D."""
    figure = _figure(_render(slot, {"x": 0, "y": 1, "z": _MATERIAL_AXIS, "c": "_batch"}))
    zaxis = figure["layout"]["scene"]["zaxis"]

    assert list(zaxis["ticktext"]) == _MATERIALS
    assert zaxis["title"]["text"] == "Plate material"
    assert len(figure["data"][0]["z"]) == 4


def test_numeric_axes_are_untouched(slot: Any) -> None:
    """The regression guard for every study that has no categorical factor."""
    figure = _figure(_render(slot, {"x": 0, "y": 1, "z": -1, "c": "_batch"}))

    assert list(figure["data"][0]["x"]) == [300.0, 275.0, 250.0, 225.0]
    assert "ticktext" not in (figure["layout"].get("xaxis") or {})
    marker = figure["data"][0]["marker"]
    # Plotly expands the named scale, so check the *shape*: a continuous ramp
    # has one colour per stop, while a stepped one repeats each interior stop.
    stops = [stop for stop, _colour in marker["colorscale"]]
    assert len(set(stops)) == len(stops), "a numeric colour must not be banded"
    assert "cmin" not in marker
    assert "cmax" not in marker
