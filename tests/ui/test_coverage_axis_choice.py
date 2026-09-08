"""Choosing which inputs a batch explores, and what that is measured against.

Three controls now decide the scope of a suggested batch: where to fill across,
which inputs to explore, and which space the figures describe. The last two are
new (2026-09-03), and the interaction between them is the part worth pinning:
leaving an input out of the batch must not quietly change what "well covered"
means, because a verdict that shifts meaning without saying so is the
false-confidence failure the whole coverage feature exists to prevent.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from nicegui import Client, ui
from nicegui.page import page as page_decorator

import matofx.ui.theme as theme_module
from matofx.studies.coverage.envelope import (
    Envelope,
    FactorEnvelope,
    FactorOverride,
    _apply_override,
)
from matofx.studies.coverage.gp import default_plan_sizes
from matofx.ui.pages import cohorts as page
from matofx.ui.pages.cohorts import (
    explorable_axes,
    held_rows,
    hold_overrides,
    hold_value,
    measurement_envelope,
)

_MATERIALS = ["Aluminium", "Magnesium"]


def _report() -> dict[str, Any]:
    return {
        "envelope": [
            {
                "name": "fin_height",
                "kind": "continuous",
                "lo": 5.0,
                "hi": 45.0,
                "label": "Fin height",
            },
            {"name": "fins", "kind": "integer", "lo": 6.0, "hi": 29.0, "label": "Fin count"},
            {
                "name": "material",
                "kind": "pinned",
                "pinned_value": "Aluminium",
                "varying_kind": "categorical",
                "declared_levels": _MATERIALS,
                "label": "Plate material",
            },
            {"name": "alloy", "kind": "pinned", "pinned_value": "6061", "varying_kind": None},
            {"name": "time", "kind": "derived"},
        ]
    }


# --- which axes are offered ------------------------------------------------


def test_varying_axes_are_always_offered() -> None:
    names = [f["name"] for f in explorable_axes(_report())]
    assert "fin_height" in names
    assert "fins" in names


def test_a_promotable_held_axis_is_offered() -> None:
    """This is what was missing: the control never appeared for a cohort whose
    only held-constant input was a choice."""
    assert "material" in [f["name"] for f in explorable_axes(_report())]


def test_a_held_axis_with_no_domain_is_not_offered() -> None:
    """Nothing to open it into, so offering it would promise a sweep that
    cannot happen."""
    assert "alloy" not in [f["name"] for f in explorable_axes(_report())]


def test_a_derived_axis_is_never_offered() -> None:
    """Nobody can run a job at a snapshot index."""
    assert "time" not in [f["name"] for f in explorable_axes(_report())]


# --- the value a held axis takes ------------------------------------------


def test_a_number_is_held_at_its_midpoint() -> None:
    assert hold_value({"kind": "continuous", "lo": 5.0, "hi": 45.0}) == 25.0


def test_a_whole_number_stays_whole() -> None:
    """A held integer must still be a value the form would accept."""
    held = hold_value({"kind": "integer", "lo": 6.0, "hi": 29.0})
    assert held == 18
    assert isinstance(held, int)


def test_a_choice_is_held_at_its_first_option() -> None:
    assert hold_value({"kind": "categorical", "levels": _MATERIALS}) == "Aluminium"


def test_a_held_axis_falls_back_to_the_value_it_already_had() -> None:
    assert hold_value({"kind": "pinned", "pinned_value": "Aluminium"}) == "Aluminium"


# --- what unticking an input does to the measurement ----------------------


def test_unticking_always_keeps_the_batch_off_that_input() -> None:
    """Reported: "I unticked Fin height … and I get suggestions for Fin height".

    The pins were gated on the narrowed scope, so under the default the untick
    did nothing whatever. Which inputs the batch moves and which the figures
    describe are two questions, and this answers only the first.
    """
    rows = hold_overrides(_report(), {"fins"})
    assert [r["name"] for r in rows] == ["fins"]
    assert rows[0]["hold"] == 18


def test_only_the_unticked_axes_are_held() -> None:
    rows = hold_overrides(_report(), {"fins", "fin_height"})
    assert {r["name"] for r in rows} == {"fins", "fin_height"}


def test_an_already_held_axis_is_not_held_again() -> None:
    """It is already out of the design space; a second override would only
    restate that, and would overwrite the value the archive actually used."""
    assert hold_overrides(_report(), {"material", "alloy"}) == []


def test_holding_nothing_produces_nothing() -> None:
    assert hold_overrides(_report(), set()) == []


def test_the_default_scope_still_measures_the_unticked_input() -> None:
    """The load-bearing one. Leaving an input out of the batch must not remove
    it from the figures, or a batch exploring two of eight axes could report
    "well covered" while most of the space went unexamined.
    """
    rows = [{"name": "fins", "hold": 18}, {"name": "fin_height", "lo": 5.0, "hi": 45.0}]
    measured = measurement_envelope(rows, narrowed=False)

    assert measured is not None
    assert [r["name"] for r in measured] == ["fin_height"], "the pin must not be measured"


def test_the_narrowed_scope_measures_the_same_space_the_batch_moves_in() -> None:
    """The two coincide, so there is nothing to send."""
    rows = [{"name": "fins", "hold": 18}]
    assert measurement_envelope(rows, narrowed=True) is None


def test_widenings_survive_into_the_measurement() -> None:
    """Only the pins are stripped. A range the user widened describes the
    space in both readings."""
    rows = [{"name": "fin_height", "lo": 5.0, "hi": 45.0}, {"name": "fins", "hold": 18}]
    measured = measurement_envelope(rows, narrowed=False)

    assert measured == [{"name": "fin_height", "lo": 5.0, "hi": 45.0}]


# --- and the engine honours it --------------------------------------------


def test_a_held_axis_leaves_the_design_space() -> None:
    varying = FactorEnvelope(name="fins", kind="integer", lo=6.0, hi=29.0)
    held = _apply_override(varying, FactorOverride(hold=18))

    assert held.kind == "pinned"
    assert held.pinned_value == 18
    assert not held.actionable, "a held axis is one no suggestion may move"
    envelope = Envelope(
        factors=(FactorEnvelope(name="fin_height", kind="continuous", lo=5.0, hi=45.0), held)
    )
    assert "fins" not in envelope.metric_axes


def test_a_held_axis_still_rides_along_in_every_suggested_run() -> None:
    """It leaves the *design* space, not the input set: a suggested run is
    still a complete, submittable set of inputs."""
    envelope = Envelope(
        factors=(
            FactorEnvelope(name="fin_height", kind="continuous", lo=5.0, hi=45.0),
            _apply_override(
                FactorEnvelope(name="fins", kind="integer", lo=6.0, hi=29.0),
                FactorOverride(hold=18),
            ),
        )
    )
    points = envelope.denormalise(envelope.sample(4, seed=0))
    assert {p["fins"] for p in points} == {18}


def test_a_held_axis_can_be_opened_up_again() -> None:
    """Holding is a scoping decision for one batch, not a permanent demotion —
    ``varying_kind`` has to survive it."""
    held = _apply_override(
        FactorEnvelope(name="fins", kind="integer", lo=6.0, hi=29.0), FactorOverride(hold=18)
    )
    assert held.varying_kind == "integer"


@pytest.mark.parametrize("kind", ["pinned", "derived"])
def test_holding_an_axis_that_cannot_move_is_a_no_op(kind: str) -> None:
    factor = FactorEnvelope(name="x", kind=kind, pinned_value=3.0)
    assert _apply_override(factor, FactorOverride(hold=9.0)).kind == kind


# --- how many runs the ladder offers (2026-09-03) --------------------------


def test_the_ladder_grows_with_the_number_of_inputs_explored() -> None:
    """Opening up a held-constant choice has to change the numbers offered.

    The ladder counted ``metric_axes`` only, so ticking a categorical — the
    one control the user had just used — left every rung identical.
    """
    four = default_plan_sizes(4, n_existing=20)
    five = default_plan_sizes(5, n_existing=20)
    assert five != four
    assert max(five) > max(four)


def test_the_ladder_drops_rungs_too_small_to_matter() -> None:
    """Reported as "why do I always get the same number of runs".

    A four-run batch cannot move a measure computed over five hundred runs
    plus four, so offering it spends a row on a batch that provably buys
    nothing.
    """
    small = default_plan_sizes(4, n_existing=20)
    large = default_plan_sizes(4, n_existing=500)
    assert min(large) > min(small)
    assert set(large) <= set(small), "rungs are dropped, not invented"


def test_a_ladder_always_has_at_least_two_rungs() -> None:
    """One row is a quote, not a curve — the table exists to show a knee."""
    assert len(default_plan_sizes(2, n_existing=100_000)) >= 2


def test_a_small_archive_is_unaffected() -> None:
    """The demo's own numbers must not move: 4 inputs, 20 runs."""
    assert default_plan_sizes(4, n_existing=20) == (4, 8, 20, 40, 80)


# --- the card as it renders ------------------------------------------------


@pytest.fixture
def card(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The fill-the-gaps card, built for a cohort with one held-constant choice."""
    monkeypatch.setattr(theme_module, "apply_theme_to_plotly", lambda fig, _t=None: fig)
    monkeypatch.setattr(page, "_client", lambda: None)

    def _unused() -> None:
        """Never called — `page` only needs a target to decorate."""

    report = {
        "envelope": [
            {
                "name": "fin_height",
                "label": "Fin height",
                "kind": "continuous",
                "lo": 15.0,
                "hi": 25.0,
                "declared_lo": 5.0,
                "declared_hi": 45.0,
                "actionable": True,
            },
            {
                "name": "fin_count",
                "label": "Fin count",
                "kind": "integer",
                "lo": 18.0,
                "hi": 24.0,
                "declared_lo": 6.0,
                "declared_hi": 28.0,
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
        ],
        "points": [{"fin_height": 15.0 + i, "fin_count": 18 + i % 6} for i in range(20)],
        "per_pair": [],
    }
    client = Client(page_decorator("/__fill_card_probe__")(_unused), request=None)
    with client, ui.column() as column:
        page._render_suggestions(report, {}, 5, {})
        yield column


def _labels(container: Any) -> list[str]:
    return [
        t
        for t in (getattr(e, "text", "") for e in container.descendants())
        if isinstance(t, str) and t.strip()
    ]


def _buttons(container: Any) -> dict[str, Any]:
    return {b.text: b for b in container.descendants() if type(b).__name__ == "Button" and b.text}


def _checkboxes(container: Any) -> dict[str, Any]:
    return {
        c.text: c
        for c in container.descendants()
        if type(c).__name__ == "Checkbox" and getattr(c, "text", "")
    }


def test_the_inputs_come_before_the_two_scope_controls(card: Any) -> None:
    """ "Which inputs?" is the question answered first; both scope controls are
    follow-ons from it. Stacked the other way they read as three unrelated
    settings, which is what made the third one confusing."""
    labels = _labels(card)
    assert labels.index("Inputs to explore") < labels.index("Fill across")
    assert labels.index("Inputs to explore") < labels.index("Coverage figures describe")


def test_the_second_scope_names_the_ticks_above_it(card: Any) -> None:
    """It was "Measure coverage against / The whole design space", which reads
    as an independent third scope and collides with "Fill across"."""
    labels = _labels(card)
    assert "Coverage figures describe" in labels
    assert "Only the ones I ticked" in labels
    assert "Measure coverage against" not in labels


def test_it_reads_every_explored_input_while_a_choice_is_still_held(card: Any) -> None:
    """The opening state, and the wording matters.

    A promotable input that is still unticked is outside the design space
    either way, so the figures do not describe it. Calling that "every input"
    would claim a completeness the numbers do not have.
    """
    buttons = _buttons(card)
    assert "Every explored input" in buttons
    assert "Every input" not in buttons
    assert buttons["Every explored input"].enabled is False
    assert any("Every explored input so far is ticked" in t for t in _labels(card))


def test_it_reads_every_input_once_they_are_all_ticked(card: Any) -> None:
    """Now they really are all in play, so the plainer word is the honest one —
    and there is still nothing to leave out, so it stays inert."""
    boxes = _checkboxes(card)
    held = next(k for k in boxes if "held so far" in k)
    boxes[held].set_value(True)

    buttons = _buttons(card)
    assert "Every input" in buttons
    assert buttons["Every input"].enabled is False
    assert any("nothing to leave out" in t for t in _labels(card))


def test_it_is_inert_whenever_nothing_is_left_out(card: Any) -> None:
    """With nothing left out the two options describe the same space, so a
    live control would invite a hunt for a difference that is not there."""
    assert _buttons(card)["Only the ones I ticked"].enabled is False


def test_unticking_a_varying_input_brings_it_to_life(card: Any) -> None:
    boxes = _checkboxes(card)
    boxes["Fin count"].set_value(False)

    assert _buttons(card)["Only the ones I ticked"].enabled is True


def test_unticking_a_held_input_does_not(card: Any) -> None:
    """It is already outside the design space, so unticking leaves nothing
    behind for the figures to keep reporting."""
    boxes = _checkboxes(card)
    held = next(k for k in boxes if "held so far" in k)
    boxes[held].set_value(False)

    assert _buttons(card)["Only the ones I ticked"].enabled is False


def test_the_preview_says_it_can_be_opened() -> None:
    """It read as a heading and went unclicked, so the label now says so.

    Read from the source: the expansion is built inside the fill preview,
    which only renders once a batch has been priced.
    """
    source = pathlib.Path(page.__file__).read_text(encoding="utf-8")
    assert "See where these runs would go — click to open" in source


# --- options get a before/after bar too ------------------------------------


@pytest.fixture
def levels_slot(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A slot to draw one option bar into, with the theme lookup stubbed."""
    monkeypatch.setattr(theme_module, "apply_theme_to_plotly", lambda fig, _t=None: fig)

    def _unused() -> None:
        """Never called — `page` only needs a target to decorate."""

    client = Client(page_decorator("/__levels_probe__")(_unused), request=None)
    with client, ui.column() as column:
        yield column


def _figure_of(container: Any) -> dict[str, Any]:
    plot = next(e for e in container.descendants() if isinstance(e, ui.plotly))
    figure = plot.figure
    return figure if isinstance(figure, dict) else figure.to_plotly_json()


def test_an_option_axis_gets_its_own_before_and_after_bar(levels_slot: Any) -> None:
    """Reported: the histograms showed only the numbers.

    An input you opened up is one of the inputs the batch explores, so it
    belongs in the same row. It cannot be binned — the counts per option are
    the whole story — but it answers the identical question.
    """
    with levels_slot:
        page._stacked_levels(
            "Plate material",
            ["Aluminium"] * 20,
            ["Aluminium"] * 5 + ["Magnesium"] * 5,
            ["Aluminium", "Magnesium"],
        )
    data = _figure_of(levels_slot)["data"]

    assert [t["name"] for t in data] == ["existing", "proposed"]
    assert list(data[0]["y"]) == [20, 0], "existing counts per option"
    assert list(data[1]["y"]) == [5, 5], "proposed counts per option"


def test_an_option_nobody_ran_still_gets_a_column(levels_slot: Any) -> None:
    """The empty column is the point, exactly as on the pairwise strips."""
    with levels_slot:
        page._stacked_levels("Plate material", ["Aluminium"] * 20, [], ["Aluminium", "Magnesium"])
    data = _figure_of(levels_slot)["data"]

    assert list(data[0]["x"]) == ["Aluminium", "Magnesium"]
    assert list(data[0]["y"]) == [20, 0]
    text = " ".join(
        str(getattr(e, "text", "")) for e in levels_slot.descendants() if getattr(e, "text", "")
    )
    assert "never run: Magnesium" in text


def test_the_histograms_and_the_matrix_use_the_same_axes() -> None:
    """An input plotted in one row and missing from the other is a reader
    hunting for a panel that is not there.

    They were built from separate lists — numbers only for the histograms,
    ``grid_axes`` for the matrix — so a held-constant choice drew a strip in
    the matrix and no bar above it. Now there is one list, and each axis is
    drawn the way its kind allows.
    """
    source = pathlib.Path(page.__file__).read_text(encoding="utf-8")
    body = source[source.index("def _build_fill_preview(") :]
    body = body[: body.index("\ndef ", 1)]

    assert "for name in matrix_axes:" in body, "the histograms must walk the matrix axes"
    assert "_build_scatter_grid(_FreshPanel(), matrix_axes" in body
    assert "for name in axes:" not in body, "a second axis list has come back"


# --- the value an unticked input is held at (2026-09-03) -------------------


def test_a_held_number_lands_on_the_input_s_own_step() -> None:
    """The one route into a proposed run that skipped the precision snap.

    ``Envelope.denormalise`` snaps the axes the batch moves and copies pinned
    values straight through, so an input stepping in halves over an observed
    2.0-3.5 put ``2.75`` on every run in the batch — the unsubmittable value
    ``precision`` exists to prevent, arrived at by the one path nobody could
    see.
    """
    held = hold_value({"kind": "continuous", "lo": 2.0, "hi": 3.5, "precision": 0.5})
    assert held == 3.0


def test_a_held_number_without_a_step_keeps_its_midpoint() -> None:
    """No declared increment is a real answer, not zero: the raw value stands."""
    assert hold_value({"kind": "continuous", "lo": 5.0, "hi": 45.0, "precision": None}) == 25.0


def test_a_snapped_hold_stays_inside_the_axis() -> None:
    """Rounding near a bound can step outside it, and a hold reaches every
    proposed run untouched."""
    held = hold_value({"kind": "continuous", "lo": 2.0, "hi": 2.9, "precision": 1.0})
    assert 2.0 <= held <= 2.9


def test_the_user_s_own_held_value_wins_over_the_midpoint() -> None:
    """The midpoint is a default, not a decision — "hold it at the value we
    actually ship" is the common case and there was no way to say it."""
    rows = hold_overrides(_report(), {"fins"}, values={"fins": 24})
    assert rows == [{"name": "fins", "hold": 24}]


def test_a_held_value_for_an_input_that_is_not_held_is_ignored() -> None:
    """Stale entries survive a re-tick so the user's choice comes back; they
    must not put a pin back on an input that is being explored again."""
    rows = hold_overrides(_report(), {"fins"}, values={"fin_height": 12.0})
    assert [r["name"] for r in rows] == ["fins"]


def test_only_axes_the_batch_could_move_are_offered_a_held_value() -> None:
    """An axis the archive already held constant is pinned whatever the user
    ticks; offering to choose its value would imply the batch could move it."""
    assert [f["name"] for f in held_rows(_report(), {"fins", "material", "alloy"})] == ["fins"]


# --- when the card is drawn at all (2026-09-03) ----------------------------


def test_the_card_is_drawn_for_a_choice_only_design() -> None:
    """The engine now plans a batch of nothing but options, so gating the card
    on a numeric axis made that unreachable."""
    report = {"envelope": [{"name": "material", "kind": "categorical", "levels": _MATERIALS}]}
    assert page._has_actionable_axis(report)


def test_the_card_is_drawn_when_every_number_was_held_constant() -> None:
    """The archive where the tick list matters most: nothing varied, but the
    descriptor says what the inputs could sweep."""
    report = {
        "envelope": [
            {"name": "fins", "kind": "pinned", "pinned_value": 12, "varying_kind": "integer"}
        ]
    }
    assert page._has_actionable_axis(report)


def test_the_card_stays_away_when_nothing_could_ever_move() -> None:
    """A snapshot index comes from the solver, and a pinned input with no
    declared domain has nothing to sweep — there is no batch to offer."""
    report = {
        "envelope": [
            {"name": "time", "kind": "derived"},
            {"name": "alloy", "kind": "pinned", "pinned_value": "6061", "varying_kind": None},
        ]
    }
    assert not page._has_actionable_axis(report)
