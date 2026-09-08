"""Does the coverage panel actually *draw*? (ADR-0020 §11)

**The gap every other test in this area shares.** `test_coverage_panel.py`
covers the wording helpers, which are pure and easy to test; the render
functions that consume them were covered by nothing, because "the suite
cannot drive NiceGUI". Two render-time defects shipped through that gap in
consecutive rounds, both invisible to 600 passing tests:

- `cell.style("cursor: pointer;", add=True)` — `Style.__call__`'s first
  positional parameter is itself named `add`, so it was passed twice. The
  panel rendered its first input row and then silently stopped, with the rest
  of the table simply absent rather than erroring on screen;
- before that, a `string` output branch that no page could render.

NiceGUI elements only need a *slot* to build into — not a browser, a server
or a socket — so a bare `Client` is enough to prove every element call in a
render path is well-formed, which is exactly the class of bug that got through.
Verified by reintroducing the `add=True` defect: four of these fail on it.

These are smoke tests: they assert that drawing succeeds and that the content
reaches the tree. They do not assert layout, and they are not a substitute for
looking at the page.
"""

from __future__ import annotations

from typing import Any

import pytest
from nicegui import Client, ui
from nicegui.page import page as page_decorator

from matofx.ui.pages import cohorts as cohorts_page
from matofx.ui.pages.cohorts import (
    _ChoiceButtons,
    _declared_overrides,
    _render_coverage_body,
    _render_create_study,
    _render_factor_strip,
    _render_pair_legend,
    _render_pair_strip,
    _render_verdict,
    default_study_name,
)


@pytest.fixture
def slot() -> Any:
    """A container the render functions can build into, with no browser.

    A bare :class:`Client` is enough: NiceGUI elements need a *slot* to build
    into, not a socket, a server or a page request. Nothing here is served, so
    the probe route is never reachable.
    """

    def _unused() -> None:
        """Never called — `page` only needs a target to decorate."""

    client = Client(page_decorator("/__coverage_panel_probe__")(_unused), request=None)
    with client, ui.column() as column:
        yield column


def _report() -> dict[str, Any]:
    """A report shaped like the one the demo heat-sink archive produces."""
    return {
        "verdict": "poor",
        "message": "Each input on its own is well covered, but their combinations are not.",
        "coverage_score": 0.83,
        "covering_radius": 0.78,
        "efficiency_score": 0.81,
        "discriminating": True,
        "diagnostics": {
            "n_points": 531,
            "n_unmeasurable": 240,
            "n_outside_envelope": 0,
            "grouping_mode": "n/a",
        },
        "envelope": [
            {
                "name": "air_velocity",
                "label": "Air velocity",
                "kind": "continuous",
                "lo": 0.6,
                "hi": 15.0,
                "declared_lo": 0.5,
                "declared_hi": 15.0,
                "precision": 0.1,
                "actionable": True,
            },
            {
                "name": "base_temperature_rise",
                "label": "Base temperature rise",
                "kind": "pinned",
                "pinned_value": 60.0,
                "declared_lo": 10.0,
                "declared_hi": 120.0,
                "precision": 1.0,
                "actionable": False,
            },
        ],
        "per_factor": [
            {
                "name": "air_velocity",
                "label": "Air velocity",
                "kind": "continuous",
                "measure": "covering_radius",
                "value": 0.982,
                "verdict": "good",
                "actionable": True,
                "simulated_lo": 0.6,
                "simulated_hi": 15.0,
                "declared_lo": 0.5,
                "declared_hi": 15.0,
                "declared_value": 0.98,
                "declared_verdict": "good",
                "precision": 0.1,
                "missing_values": [],
            },
            {
                # The row that used to render as a green "Well covered".
                "name": "base_temperature_rise",
                "label": "Base temperature rise",
                "kind": "pinned",
                "measure": "pinned",
                "value": 1.0,
                "verdict": "good",
                "actionable": False,
                "simulated_lo": 60.0,
                "simulated_hi": 60.0,
                "declared_lo": 10.0,
                "declared_hi": 120.0,
                "declared_value": 0.0,
                "declared_verdict": "poor",
                "precision": 1.0,
                "missing_values": [],
            },
        ],
        "per_pair": [
            {
                "a": "air_velocity",
                "b": "base_width",
                "a_label": "Air velocity",
                "b_label": "Base width",
                "n_points": 291,
                "coverage_score": 0.16,
                "verdict": "poor",
            }
        ],
        "n_pairs_examined": 1,
        "n_pairs_total": 15,
        "warnings": [{"code": "pair_uncovered", "message": "Some inputs were rarely varied."}],
        "points": [
            {"air_velocity": 1.0 + i * 0.1, "base_width": 40.0 + i, "base_temperature_rise": 60.0}
            for i in range(40)
        ],
    }


def _text_of(element: Any) -> str:
    """Every bit of text in a slot's subtree."""
    out: list[str] = []
    for child in element.descendants():
        text = getattr(child, "text", None)
        if isinstance(text, str) and text:
            out.append(text)
    return " | ".join(out)


def test_the_per_input_table_draws_every_row(slot: Any) -> None:
    """The regression. It used to stop after the first input."""
    with slot:
        _render_factor_strip(_report())

    text = _text_of(slot)
    assert "Air velocity" in text
    assert "Base temperature rise" in text, "the table stopped after the first row"


def test_a_pinned_input_is_badged_not_varied_on_screen(slot: Any) -> None:
    """Not merely in the helper — in the tree the user actually sees."""
    with slot:
        _render_factor_strip(_report())

    text = _text_of(slot)
    assert "Not varied" in text
    # ...and it must not claim the tool's range was explored, because it wasn't.
    assert "Barely covered" in text


def test_both_range_columns_reach_the_page(slot: Any) -> None:
    with slot:
        _render_factor_strip(_report())

    text = _text_of(slot)
    assert "Simulated range" in text
    assert "Input range" in text


def test_the_pair_table_draws_both_readings(slot: Any) -> None:
    """One number was answering two questions, and the panel showed only the
    answer that read badly. Each row now carries a run-count tag and an
    arrangement tag."""
    report = _report()
    report["per_pair"][0].update({"spread_score": 0.91, "spread_verdict": "good"})

    with slot:
        _render_pair_strip(report)

    text = _text_of(slot)
    assert "Air velocity vs Base width" in text
    assert "Very few" in text, "the run-count reading"
    assert "Well spread" in text, "the arrangement reading"


def test_a_pair_saved_before_the_second_reading_shows_a_dash(slot: Any) -> None:
    """Absent is not the same as bad — a zero would render every archived
    report as the worst arrangement there is."""
    with slot:
        _render_pair_strip(_report())

    assert "—" in _text_of(slot)


def test_the_legend_explains_every_tag(slot: Any) -> None:
    with slot:
        _render_pair_legend()

    text = _text_of(slot)
    for tag in (
        "Enough",
        "Not many",
        "Very few",
        "Well spread",
        "Unevenly spread",
        "Barely spread",
        "Not assessed",
    ):
        assert tag in text, f"legend is missing: {tag}"


def test_the_verdict_card_draws(slot: Any) -> None:
    with slot:
        _render_verdict(_report())

    text = _text_of(slot)
    assert "Poorly covered" in text
    assert "combinations" in text


def test_a_report_with_no_pairs_draws_nothing_rather_than_failing(slot: Any) -> None:
    """A stored report from before §4.3 has no `per_pair` key at all."""
    report = _report()
    report.pop("per_pair")
    with slot:
        _render_pair_strip(report)
    assert "vs" not in _text_of(slot)


def test_an_input_with_no_declared_bounds_still_draws(slot: Any) -> None:
    """A tool that states no minimum/maximum must not break the table."""
    report = _report()
    for factor in report["per_factor"]:
        factor["declared_lo"] = None
        factor["declared_hi"] = None
        factor["declared_verdict"] = "insufficient"
    with slot:
        _render_factor_strip(report)

    assert "No stated range" in _text_of(slot)


# --- the panel as a whole -------------------------------------------------


@pytest.fixture
def _no_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the API client away.

    The fill-the-gaps card builds one at render time, and these tests are
    about the panel's structure rather than anything it fetches — mounting a
    whole app to assert a heading order would be testing the wrong thing.
    """
    monkeypatch.setattr(cohorts_page, "_client", lambda: None)


@pytest.mark.usefixtures("_no_api")
def test_the_whole_panel_draws_as_sections(slot: Any) -> None:
    """The regression net for the layout, not just its parts.

    Each block was verified alone while the page that assembles them was
    verified by nobody — which is how a render-time error in one row took the
    rest of the table with it.
    """

    async def _reload() -> None:
        """Never called here."""

    with slot:
        _render_coverage_body(_report(), {}, _reload, cohort_id=1)

    text = _text_of(slot)
    for section in (
        "Each input on its own",
        "How the inputs combine",
        "Over what range should this model be trusted?",
        "Fill the gaps",
    ):
        assert section in text, f"section missing: {section}"


@pytest.mark.usefixtures("_no_api")
def test_the_verdict_comes_before_the_evidence(slot: Any) -> None:
    """Answer first. A form above the answer buried it."""

    async def _reload() -> None:
        """Never called here."""

    with slot:
        _render_coverage_body(_report(), {}, _reload, cohort_id=1)

    order = _text_of(slot)
    assert order.index("Poorly covered") < order.index("Each input on its own")
    assert order.index("Each input on its own") < order.index(
        "Over what range should this model be trusted?"
    )


# --- filling the gaps over one range or the other -------------------------


def test_the_fill_scope_offers_the_tools_own_range() -> None:
    """Widening a model's claims must be a choice, never a default."""
    overrides = _declared_overrides(_report())
    assert {o["name"] for o in overrides} == {"air_velocity"}
    assert overrides[0]["lo"] == 0.5
    assert overrides[0]["hi"] == 15.0


def test_an_axis_the_tool_leaves_unbounded_is_not_widened() -> None:
    """There is no wider range to claim, and inventing one proposes runs the
    form would reject."""
    report = _report()
    for factor in report["envelope"]:
        factor["declared_lo"] = None
        factor["declared_hi"] = None
    assert _declared_overrides(report) == []


def test_a_pinned_axis_is_never_offered_for_widening() -> None:
    """No design point can move it, whatever the tool allows."""
    names = {o["name"] for o in _declared_overrides(_report())}
    assert "base_temperature_rise" not in names


# --- the fill-scope buttons ----------------------------------------------


def _button_state(control: Any) -> dict[str, str]:
    """``{choice: "filled" | "outlined"}`` as Quasar will render it."""
    out: dict[str, str] = {}
    for key, button in control._buttons.items():
        props = button._props
        out[key] = "filled" if props.get("unelevated") else "outlined"
    return out


def test_exactly_one_choice_is_filled(slot: Any) -> None:
    """Filled means current; outlined means available. Never both, never neither."""
    with slot:
        control = _ChoiceButtons({"a": "The first", "b": "The second"}, value="a")

    assert _button_state(control) == {"a": "filled", "b": "outlined"}
    assert all(b._props.get("color") == "primary" for b in control._buttons.values())


def test_the_fill_and_the_outline_swap_on_selection(slot: Any) -> None:
    changed: list[str] = []
    with slot:
        control = _ChoiceButtons(
            {"a": "The first", "b": "The second"}, value="a", on_change=changed.append
        )

    control.select("b")
    assert _button_state(control) == {"a": "outlined", "b": "filled"}
    assert control.value == "b"
    assert changed == ["b"]


def test_selecting_an_unknown_choice_changes_nothing(slot: Any) -> None:
    """A stale key must not leave every button outlined and no value set."""
    changed: list[str] = []
    with slot:
        control = _ChoiceButtons({"a": "The first"}, value="a", on_change=changed.append)

    control.select("nonsense")
    assert control.value == "a"
    assert _button_state(control) == {"a": "filled"}
    assert not changed


# --- the create-study form ------------------------------------------------


def test_the_default_study_name_is_named_after_the_tool() -> None:
    """A project accumulates these; "Fill coverage gaps" three times says nothing.

    The label, never the alias ([[feedback_use_labels_not_names]]) — with the
    alias only as the fallback a cohort whose tool no longer resolves needs.
    """
    assert (
        default_study_name({"tool_name": "Finned Heat Sink", "tool_alias": "heat_sink"})
        == "Finned Heat Sink - Fill coverage gaps"
    )
    assert default_study_name({"tool_alias": "heat_sink"}) == "heat_sink - Fill coverage gaps"
    assert default_study_name({}) == "Fill coverage gaps"


@pytest.mark.usefixtures("_no_api")
def test_the_project_is_a_picker_not_a_typed_id(slot: Any) -> None:
    """A typed id asks the user to know a database key.

    It also accepts any integer, including a project they cannot see — which
    surfaces as a permission error only after the batch has been priced.
    """
    with slot:
        _render_create_study(
            None, 1, {"n": 3, "points": [], "envelope": []}, {"tool_name": "Heat sink"}
        )

    kinds = {type(d).__name__ for d in slot.descendants()}
    assert "Select" in kinds, "the project control is not a picker"
    assert "Number" not in kinds, "a numeric id input is still present"

    # Widget labels live in props, not in element text.
    labels = {str(d._props.get("label")) for d in slot.descendants() if d._props.get("label")}
    assert "Project" in labels
    assert "Project id" not in labels, "the old lowercase label survived"

    values = {str(d._props.get("value")) for d in slot.descendants() if d._props.get("value")}
    assert "Heat sink - Fill coverage gaps" in values


# --- the fill-the-gaps card itself (2026-09-03) ----------------------------


def _fill_report() -> dict[str, Any]:
    """A report with two numbers to explore and a choice held constant."""
    report = _report()
    report["envelope"] = [
        *report["envelope"],
        {
            "name": "base_width",
            "label": "Base width",
            "kind": "continuous",
            "lo": 40.0,
            "hi": 80.0,
            "declared_lo": 20.0,
            "declared_hi": 120.0,
            "precision": 0.5,
            "actionable": True,
        },
        {
            "name": "material",
            "label": "Plate material",
            "kind": "pinned",
            "pinned_value": "Aluminium",
            "varying_kind": "categorical",
            "declared_levels": ["Aluminium", "Magnesium"],
            "actionable": False,
        },
    ]
    return report


def _tick_labels(slot: Any) -> set[str]:
    """The tick list's labels — a Checkbox carries its label as `.text`."""
    return {str(d.text) for d in slot.descendants() if type(d).__name__ == "Checkbox" and d.text}


def _untick(slot: Any, label: str) -> None:
    """Clear one tick and let its handler run, as a click would."""
    box = next(d for d in slot.descendants() if type(d).__name__ == "Checkbox" and d.text == label)
    box.set_value(False)


@pytest.mark.usefixtures("_no_api")
def test_the_fill_card_draws_a_tick_per_explorable_input(slot: Any) -> None:
    with slot:
        cohorts_page._render_suggestions(_fill_report(), {}, 1, {})

    ticks = _tick_labels(slot)
    assert "Air velocity" in ticks
    assert "Base width" in ticks
    assert "Plate material  (held so far)" in ticks, "a promotable input must be offered"


@pytest.mark.usefixtures("_no_api")
def test_unticking_an_input_draws_a_box_for_the_value_it_is_held_at(slot: Any) -> None:
    """Every run in the batch shares that value, and it used to be invisible —
    the fill preview cannot show it either, because the preview is drawn over
    the batch's space where the axis has been pinned out of existence."""
    with slot:
        cohorts_page._render_suggestions(_fill_report(), {}, 1, {})
    _untick(slot, "Base width")

    labels = {str(d._props.get("label")) for d in slot.descendants() if d._props.get("label")}
    assert "Base width held at" in labels


@pytest.mark.usefixtures("_no_api")
def test_the_held_value_offered_is_one_the_tool_would_accept(slot: Any) -> None:
    """40.0-80.0 stepping in halves has a midpoint of 60.0; the point of the
    snap is that a range with an odd midpoint never reaches the box raw."""
    report = _fill_report()
    width = next(f for f in report["envelope"] if f["name"] == "base_width")
    width["hi"] = 80.7

    with slot:
        cohorts_page._render_suggestions(report, {}, 1, {})
    _untick(slot, "Base width")

    held = next(d for d in slot.descendants() if str(d._props.get("label")) == "Base width held at")
    assert (held.value / 0.5) == round(held.value / 0.5), held.value


@pytest.mark.usefixtures("_no_api")
def test_the_plan_table_draws_its_rows_and_its_caveats(slot: Any) -> None:
    plan = {
        "options": [
            {"n": 4, "verdict": "thin", "gap_after": 0.29, "levels_short": 1},
            {"n": 8, "verdict": "good", "gap_after": 0.21, "reaches_good": True},
        ],
        "current_verdict": "poor",
        "current_gap": 0.34,
        "current_levels_short": 2,
        "measured_axes": 3,
    }
    with slot:
        cohorts_page._render_plan_table(
            plan, lambda *_: None, ["Base width"], "Measured over the whole thing."
        )

    text = _text_of(slot)
    assert "4 runs" in text
    assert "8 runs" in text
    assert "34% of the space" in text
    assert "Base width" in text, "the input the batch cannot move must be named"
    assert "Measured over the whole thing." in text


@pytest.mark.usefixtures("_no_api")
def test_a_choice_only_plan_does_not_draw_a_gap_column(slot: Any) -> None:
    """Its covering radius is 0.0 because there is no distance to measure, and
    "gap closes to 0%" is the most flattering thing the table could say."""
    plan = {
        "options": [{"n": 4, "verdict": "thin", "gap_after": 0.0, "levels_short": 0}],
        "current_verdict": "poor",
        "current_gap": 0.0,
        "current_levels_short": 2,
        "measured_axes": 0,
    }
    with slot:
        cohorts_page._render_plan_table(plan, lambda *_: None)

    text = _text_of(slot)
    assert "of the space" not in text
    assert "2 options still have too few runs" in text
    assert "options under-run 2 → 0" in text


@pytest.fixture
def _no_theme(monkeypatch: Any) -> None:
    """Plotly figures are themed from the signed-in user's preference.

    These tests build elements without a request, so the lookup has no session
    to read. The theming is not what is under test — whether the traces are
    built at all is.
    """
    import matofx.ui.theme as theme_module  # noqa: PLC0415

    monkeypatch.setattr(theme_module, "themed_plotly", lambda figure: figure)


@pytest.mark.usefixtures("_no_api", "_no_theme")
def test_a_held_input_gets_a_histogram_showing_where_the_batch_puts_it(slot: Any) -> None:
    """The disclosure the "held at" box promises, drawn.

    Reported as Fin height vanishing from both plots when unticked while
    Plate material, left unticked, stayed in both. A pinned axis now earns a
    chart either way \u2014 and the existing runs have to appear on it, which
    means reading the pinned-filled rows rather than the raw ones.
    """
    report = _report()
    report["envelope"] = [
        f for f in report["envelope"] if f["name"] in {"air_velocity", "base_temperature_rise"}
    ]
    suggested = [{"air_velocity": 2.0 + i, "base_temperature_rise": 60.0} for i in range(3)]

    with slot:
        cohorts_page._build_fill_preview(
            _FillPanel(),
            cohorts_page.grid_axes(report["envelope"]),
            {f["name"]: f["label"] for f in report["envelope"]},
            {f["name"]: f.get("precision") for f in report["envelope"]},
            report["points"],
            suggested,
            report,
        )

    # The chart's title lives inside the Plotly layout; its footer is the
    # element text, and the footer is what carries the counts.
    text = _text_of(slot)
    assert text.count("40 existing + 3 proposed") == 2, (
        "both inputs must earn a chart \u2014 the held one included"
    )
    assert "40 existing + 3 proposed  (60)" in text, (
        "the existing runs must appear on the held axis at its held value; they "
        "are absent from the raw rows and have to be read through "
        "with_pinned_values, or that bar is empty"
    )


class _FillPanel:
    """A slot stand-in for the fill preview's own build latch."""

    _fill_built = False

    def __enter__(self) -> _FillPanel:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.mark.usefixtures("_no_api")
def test_the_finding_sits_on_one_line_beside_its_chip(slot: Any) -> None:
    """It was a block element under the badge, so the column ran to two lines
    and the chips stopped lining up down the table."""
    report = _report()
    report["per_pair"][0].update(
        {
            "spread_score": 0.0,
            "spread_verdict": "poor",
            "detail": "Magnesium was only run between 5.1 and 7.9, out of 2.2 to 14.1.",
        }
    )

    with slot:
        _render_pair_strip(report)

    detail = next(
        d for d in slot.descendants() if str(getattr(d, "text", "")).startswith("Magnesium")
    )
    badge = next(d for d in slot.descendants() if str(getattr(d, "text", "")) == "Barely spread")
    assert detail.parent_slot is badge.parent_slot, "chip and finding must share one row"
    row = badge.parent_slot.parent
    assert "nowrap" in row._style.get("flex-wrap", ""), "the row must not wrap"
    assert "block" not in detail._style.get("display", ""), "the finding must stay inline"
