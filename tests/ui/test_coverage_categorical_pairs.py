"""Categorical axes in the pairwise matrix (2026-09-02).

Axis selection filtered to ``continuous`` / ``integer``, so an input with a
fixed set of options appeared in **no** panel. That is the one failure mode
that is invisible everywhere else: a material run only in one corner of the
space looks perfectly healthy on every continuous row, which is exactly why
categorical imbalance is the silent one of coverage's three diseases.

Two shapes are added — a strip (choice against number) and a tally grid
(choice against choice) — and these tests cover the maths behind both, plus a
render pass proving the panels actually draw.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from nicegui import Client, ui
from nicegui.page import page as page_decorator

import matofx.ui.pages.cohorts as cohorts_module
import matofx.ui.theme as theme_module
from matofx.ui._coverage_panel import (
    axis_levels,
    grid_axes,
    level_counts,
    strip_positions,
    with_pinned_values,
)
from matofx.ui.pages.cohorts import _build_scatter_grid

_MATERIALS = ["Aluminium", "Magnesium"]


@pytest.fixture(autouse=True)
def _plain_theme(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the theme lookup, which reads per-user storage a bare Client has not.

    The panels are what is under test; how they are coloured is not. The stub
    keeps the real signature — the grid now builds its cells through
    ``themed_plotly``, which passes the palette name through as a second
    argument (and is what carries ``displaylogo: False`` to the browser).
    """
    monkeypatch.setattr(theme_module, "apply_theme_to_plotly", lambda figure, _theme=None: figure)


@pytest.fixture
def slot() -> Any:
    """A container the render functions can build into, with no browser."""

    def _unused() -> None:
        """Never called — `page` only needs a target to decorate."""

    client = Client(page_decorator("/__categorical_pairs_probe__")(_unused), request=None)
    with client, ui.column() as column:
        yield column


# --- strip positions -------------------------------------------------------


def test_runs_of_one_level_are_nudged_apart() -> None:
    """Without a nudge, twenty runs and two draw the same single dot."""
    positions = strip_positions(_MATERIALS, ["Aluminium"] * 4)
    assert len({round(p, 6) for p in positions}) == 4  # type: ignore[arg-type]
    assert all(abs(p) < 0.5 for p in positions)  # type: ignore[arg-type]


def test_each_level_sits_over_its_own_tick() -> None:
    positions = strip_positions(_MATERIALS, ["Aluminium", "Magnesium"])
    assert positions[0] == pytest.approx(0.0, abs=0.3)
    assert positions[1] == pytest.approx(1.0, abs=0.3)


def test_positions_are_deterministic() -> None:
    """A screenshot taken today and one taken in October have to match."""
    values = ["Aluminium", "Magnesium"] * 6
    assert strip_positions(_MATERIALS, values) == strip_positions(_MATERIALS, values)


def test_an_unknown_level_is_dropped_not_placed() -> None:
    """A run of a level the envelope does not declare has no column to sit in."""
    positions = strip_positions(_MATERIALS, ["Titanium"])
    assert positions == [None]


# --- the tally grid --------------------------------------------------------


def test_the_grid_counts_each_combination() -> None:
    points = [
        {"m": "Aluminium", "c": "Air"},
        {"m": "Aluminium", "c": "Air"},
        {"m": "Magnesium", "c": "Water"},
    ]
    grid = level_counts(points, ["Air", "Water"], _MATERIALS, "c", "m")
    assert grid == [[2, 0], [0, 1]]


def test_an_untried_combination_reads_zero() -> None:
    """The categorical form of the empty corner the numeric panels hunt for."""
    grid = level_counts([{"m": "Aluminium", "c": "Air"}], ["Air", "Water"], _MATERIALS, "c", "m")
    assert grid[1] == [0, 0]


# --- which axes get columns ------------------------------------------------


def test_a_declared_option_nobody_ran_still_gets_a_column() -> None:
    """An empty column is the point — it says the model has never seen it."""
    levels = axis_levels(
        [
            {
                "name": "material",
                "kind": "categorical",
                "levels": ["Aluminium"],
                "declared_levels": _MATERIALS,
            }
        ]
    )
    assert levels == {"material": _MATERIALS}


def test_numeric_axes_are_not_given_levels() -> None:
    assert axis_levels([{"name": "fin_height", "kind": "continuous"}]) == {}


# --- the panels actually draw ---------------------------------------------


def _report() -> dict[str, Any]:
    return {
        "envelope": [
            {
                "name": "fin_height",
                "label": "Fin height",
                "kind": "continuous",
                "declared_lo": 5.0,
                "declared_hi": 45.0,
                "actionable": True,
            },
            {
                "name": "fan_duty",
                "label": "Fan speed",
                "kind": "continuous",
                "declared_lo": 20.0,
                "declared_hi": 100.0,
                "actionable": True,
            },
            {
                "name": "material",
                "label": "Plate material",
                "kind": "categorical",
                "declared_levels": _MATERIALS,
                "actionable": True,
            },
            {
                "name": "coolant",
                "label": "Coolant",
                "kind": "categorical",
                "declared_levels": ["Air", "Water"],
                "actionable": True,
            },
        ],
        "points": [
            {"fin_height": 10.0, "fan_duty": 40.0, "material": "Aluminium", "coolant": "Air"},
            {"fin_height": 30.0, "fan_duty": 80.0, "material": "Aluminium", "coolant": "Air"},
            {"fin_height": 12.0, "fan_duty": 45.0, "material": "Magnesium", "coolant": "Water"},
        ],
        "per_pair": [],
    }


def _text_of(element: Any) -> str:
    out: list[str] = []
    for child in element.descendants():
        text = getattr(child, "text", None)
        if isinstance(text, str) and text:
            out.append(text)
    return " | ".join(out)


def test_the_matrix_draws_with_categorical_axes(slot: Any) -> None:
    """Every panel shape must build — strip, tally and scatter in one grid.

    A render pass, because two render-time defects have shipped past a green
    suite on this panel already.
    """
    report = _report()
    labels = {str(f["name"]): str(f.get("label") or f["name"]) for f in report["envelope"]}
    with slot:
        _build_scatter_grid(
            slot,
            ["fin_height", "fan_duty", "material", "coolant"],
            labels,
            report["points"],
            report,
        )

    text = _text_of(slot)
    assert "option" in text, "the caption must explain the new panel shapes"


def test_a_categorical_only_pair_is_reachable(slot: Any) -> None:
    """Two option pickers and nothing else still produce a grid."""
    report = _report()
    report["envelope"] = [f for f in report["envelope"] if f["kind"] == "categorical"]
    with slot:
        _build_scatter_grid(
            slot,
            ["material", "coolant"],
            {"material": "Plate material", "coolant": "Coolant"},
            report["points"],
            report,
        )
    assert _text_of(slot)


# --- a choice that never varied (2026-09-02) -------------------------------


def test_a_pinned_choice_still_gets_its_levels() -> None:
    """A material every run shared is recorded ``pinned``, not ``categorical``.

    ``axis_levels`` tested the kind, so it handed back nothing and the grid
    tried to plot a material on a number line. Reported as "I can't see the
    plate material plots" on a cohort whose 60 runs were all aluminium.
    """
    levels = axis_levels(
        [
            {
                "name": "material",
                "kind": "pinned",
                "levels": ["Aluminium"],
                "declared_levels": _MATERIALS,
            }
        ]
    )
    assert levels == {"material": _MATERIALS}


def test_a_pinned_number_is_still_not_an_option_axis() -> None:
    """The trap on the other side. ``base_temperature_rise`` held at one value
    is pinned too, and it has bounds rather than levels — reading ``pinned`` as
    categorical threw those bounds away once already."""
    pinned_number = {"name": "rise", "kind": "pinned", "declared_lo": 10, "declared_hi": 120}
    assert axis_levels([pinned_number]) == {}


def test_a_pinned_choice_earns_a_column_in_the_grid(slot: Any) -> None:
    """The whole point: 100 % of runs on one level and 0 % on the other is the
    most extreme categorical imbalance there is, and it was the one case the
    panel could not draw."""
    report = _report()
    for factor in report["envelope"]:
        if factor["name"] == "material":
            factor["kind"] = "pinned"
            factor["actionable"] = False
            factor["levels"] = ["Aluminium"]
    for point in report["points"]:
        point["material"] = "Aluminium"

    labels = {str(f["name"]): str(f.get("label") or f["name"]) for f in report["envelope"]}
    with slot:
        _build_scatter_grid(
            slot,
            ["fin_height", "fan_duty", "material", "coolant"],
            labels,
            report["points"],
            report,
        )
    assert _text_of(slot)


def test_a_held_constant_axis_gets_its_value_back_for_plotting() -> None:
    """``resolve_points`` builds rows from the factors that *vary*, so a pinned
    material is on no point at all — the column drew and nothing landed on it.
    """
    envelope = [{"name": "material", "kind": "pinned", "pinned_value": "Aluminium"}]
    filled = with_pinned_values([{"fin_height": 10.0}, {"fin_height": 20.0}], envelope)

    assert [p["material"] for p in filled] == ["Aluminium", "Aluminium"]
    assert [p["fin_height"] for p in filled] == [10.0, 20.0]


def test_a_value_the_point_already_carries_wins() -> None:
    """A varying axis must never be overwritten by a stale envelope constant."""
    envelope = [{"name": "material", "kind": "pinned", "pinned_value": "Aluminium"}]
    filled = with_pinned_values([{"material": "Magnesium"}], envelope)
    assert filled[0]["material"] == "Magnesium"


def test_points_are_copied_not_mutated() -> None:
    """The caller's rows feed the metrics as well; filling in for a chart must
    not change what anything else reads."""
    original = {"fin_height": 10.0}
    with_pinned_values([original], [{"name": "m", "pinned_value": "Al"}])
    assert original == {"fin_height": 10.0}


def test_nothing_pinned_is_a_plain_copy() -> None:
    points = [{"fin_height": 10.0}]
    assert with_pinned_values(points, [{"name": "fin_height", "kind": "continuous"}]) == points


def test_a_pinned_axis_with_no_recorded_value_is_skipped() -> None:
    """``pinned_value`` is None for an axis with nothing usable in it; inventing
    a ``None`` column would put every run on a level that does not exist."""
    filled = with_pinned_values([{"x": 1.0}], [{"name": "m", "pinned_value": None}])
    assert "m" not in filled[0]


def test_the_strip_lands_its_runs_once_the_value_is_back(slot: Any) -> None:
    """End to end: 20 aluminium runs, magnesium never tried. Every run has to
    sit on the aluminium column and the magnesium column has to be empty —
    which is the whole picture that argument depends on.
    """
    envelope = [
        {
            "name": "fin_height",
            "label": "Fin height",
            "kind": "continuous",
            "declared_lo": 5.0,
            "declared_hi": 45.0,
            "actionable": True,
        },
        {
            "name": "material",
            "label": "Plate material",
            "kind": "pinned",
            "actionable": False,
            "pinned_value": "Aluminium",
            "levels": ["Aluminium"],
            "declared_levels": _MATERIALS,
        },
    ]
    points = [{"fin_height": 10.0 + i} for i in range(20)]
    filled = with_pinned_values(points, envelope)
    positions = strip_positions(_MATERIALS, [p["material"] for p in filled])

    assert all(pos is not None for pos in positions), "every run must land"
    assert all(abs(pos) < 0.5 for pos in positions)  # type: ignore[arg-type]

    with slot:
        _build_scatter_grid(
            slot,
            ["fin_height", "material"],
            {"fin_height": "Fin height", "material": "Plate material"},
            points,
            {"envelope": envelope, "points": points, "per_pair": []},
        )
    assert _text_of(slot)


# --- one definition of "deserves a pairwise panel" -------------------------


def test_grid_axes_keeps_a_varying_choice() -> None:
    envelope = [
        {"name": "fin_height", "kind": "continuous", "actionable": True},
        {"name": "material", "kind": "categorical", "actionable": True},
    ]
    assert grid_axes(envelope) == ["fin_height", "material"]


def test_grid_axes_keeps_a_pinned_choice() -> None:
    """Not actionable, but 100% on one level is the imbalance worth seeing."""
    envelope = [
        {"name": "fin_height", "kind": "continuous", "actionable": True},
        {"name": "material", "kind": "pinned", "actionable": False, "declared_levels": _MATERIALS},
    ]
    assert grid_axes(envelope) == ["fin_height", "material"]


def test_grid_axes_keeps_a_pinned_number() -> None:
    """Reported: untick Fin height and it vanishes from both plots, while
    Plate material — left unticked — stays in both.

    The old rule admitted a pinned choice and refused a pinned number, which
    was defensible while the only pinned axes were ones the archive held
    constant. §11.5's tick list lets a user pin a number themselves, and that
    is precisely the axis whose spike they need to see.
    """
    envelope = [
        {"name": "fin_height", "kind": "continuous", "actionable": True},
        {
            "name": "rise",
            "kind": "pinned",
            "actionable": False,
            "declared_lo": 10,
            "declared_hi": 120,
        },
    ]
    assert grid_axes(envelope) == ["fin_height", "rise"]


def test_grid_axes_treats_both_kinds_of_pinned_axis_alike() -> None:
    """The load-bearing one: whichever way the rule goes, an input the user
    unticked and an input the archive held constant must go the same way."""
    envelope = [
        {"name": "fin_height", "kind": "pinned", "actionable": False, "declared_lo": 5},
        {"name": "material", "kind": "pinned", "actionable": False, "declared_levels": _MATERIALS},
    ]
    assert grid_axes(envelope) == ["fin_height", "material"]


def test_grid_axes_drops_a_derived_axis() -> None:
    """``time`` on a time-resolved field predicts but cannot be commissioned."""
    envelope = [
        {"name": "fin_height", "kind": "continuous", "actionable": True},
        {"name": "time", "kind": "derived", "actionable": False},
    ]
    assert grid_axes(envelope) == ["fin_height"]


def test_both_matrices_are_built_from_the_same_list() -> None:
    """The reported drift: the cohort's coverage grid showed the material and
    the fill-preview's existing-against-proposed grid did not, because the
    second still filtered to ``continuous`` / ``integer``.

    Reading the source is crude, but it is the only way to assert that neither
    call site has quietly grown its own list comprehension again.
    """
    source = pathlib.Path(cohorts_module.__file__).read_text(encoding="utf-8")
    assert source.count("grid_axes(envelope)") == 2, "both matrices must share it"

    # The bare tuple is forbidden anywhere in the page. It appeared twice
    # within a day, which is how the two matrices drifted apart; the kinds now
    # live in ``VARYING_KINDS`` and the matrices in ``grid_axes``.
    assert source.count('"continuous", "integer", "categorical"') == 1, (
        "the kinds may be written out once, where VARYING_KINDS is defined; "
        "a second occurrence is an inline axis list coming back"
    )
