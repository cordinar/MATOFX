"""Pairs involving a choice (§4.6).

Reported as *"why is Plate material not included in this table? Shouldn't
categorical and boolean parameters also be in this table?"* — and the Fourth
Study, 40 runs across two materials, showed exactly the six numeric pairs.

Nothing else covered it. Every categorical check in the report is **marginal**
— counts per option, "an option barely used" — so an archive that ran
aluminium only at low fin heights and magnesium only at high ones passes all
of them. That is the confounding a model cannot see through, and catching it
between two inputs is what the pairwise pass is for.
"""

from __future__ import annotations

import pytest

from matofx.config import CoverageSettings
from matofx.studies.coverage import build_envelope, resolve_points
from matofx.studies.coverage.pairs import PairCoverage, analyse_pairs
from tests.studies._coverage_fixtures import descriptor, num, select, training

_SPEC = descriptor(num("fin_height", 1.0, 10.0), select("material", "Al", "Mg"))
_N = 40


def _pairs(rows, spec=_SPEC):
    points = resolve_points(training(rows), descriptor=spec)
    envelope = build_envelope(points, descriptor=spec)
    return analyse_pairs(
        envelope.normalise(points), envelope, settings=CoverageSettings(), points=points
    )


def _sweep(material: str, lo: float, hi: float, n: int) -> list[dict]:
    """``n`` runs of one material, evenly spanning ``lo``..``hi`` inclusive."""
    step = (hi - lo) / max(1, n - 1)
    return [{"fin_height": lo + step * i, "material": material} for i in range(n)]


# --- a choice earns a row at all -------------------------------------------


def test_a_varying_choice_is_paired_with_every_number() -> None:
    (row,) = _pairs(_sweep("Al", 1.0, 10.0, 20) + _sweep("Mg", 1.0, 10.0, 20))
    assert {row.a, row.b} == {"fin_height", "material"}
    assert row.n_points == _N


def test_the_pair_is_named_by_labels_not_internal_names() -> None:
    (row,) = _pairs(_sweep("Al", 1.0, 10.0, 20) + _sweep("Mg", 1.0, 10.0, 20))
    assert row.pair_label == "Fin Height and Material"


def test_a_choice_pair_needs_the_points_to_be_measured_at_all() -> None:
    """It cannot be read off the normalised matrix, which carries the numeric
    axes alone — so a caller with only a matrix gets the sweep it always got."""
    rows = _sweep("Al", 1.0, 10.0, 20) + _sweep("Mg", 1.0, 10.0, 20)
    points = resolve_points(training(rows), descriptor=_SPEC)
    envelope = build_envelope(points, descriptor=_SPEC)

    without = analyse_pairs(envelope.normalise(points), envelope, settings=CoverageSettings())
    assert without == ()


# --- what it measures ------------------------------------------------------


def test_a_number_swept_within_every_option_reads_well() -> None:
    (row,) = _pairs(_sweep("Al", 1.0, 10.0, 25) + _sweep("Mg", 1.0, 10.0, 25))
    assert row.verdict == "good", "twenty-five of each is close to the standard"
    assert row.spread_verdict == "good", "and each sweeps the whole range"


def test_a_number_confounded_with_the_option_reads_badly_on_both() -> None:
    """The finding this exists for: aluminium only at low fin heights and
    magnesium only at high ones. No amount of further runs *of the same
    shape* fixes it, which is why both readings condemn it."""
    (row,) = _pairs(_sweep("Al", 1.0, 5.0, 25) + _sweep("Mg", 6.0, 10.0, 25))
    assert row.spread_verdict == "poor", "each material saw less than half the range"


def test_an_option_with_few_but_well_placed_runs_splits_the_two_readings() -> None:
    """The case that earns the second column. Thirty-five aluminium runs and
    five magnesium ones, both swept across the whole range: you need more
    magnesium runs, and the five you have are in the right places."""
    (row,) = _pairs(_sweep("Al", 1.0, 10.0, 35) + _sweep("Mg", 1.0, 10.0, 5))
    assert row.verdict == "poor", "five runs is far short of the standard"
    assert row.spread_verdict == "good", "but they span the whole range"


def test_the_worst_option_sets_the_pair() -> None:
    """A model asked about magnesium at a fin height nobody ran there is
    extrapolating however good the aluminium coverage is."""
    good_al = _sweep("Al", 1.0, 10.0, 25)
    clumped_mg = [{"fin_height": 9.5, "material": "Mg"} for _ in range(25)]
    (row,) = _pairs(good_al + clumped_mg)
    assert row.spread_verdict == "poor"


# --- two choices against each other ----------------------------------------


_GRID = descriptor(select("material", "Al", "Mg"), select("mesh", "coarse", "fine"))


def test_two_choices_are_scored_on_their_combination_grid() -> None:
    rows = [
        {"material": m, "mesh": g}
        for m in ("Al", "Mg")
        for g in ("coarse", "fine")
        for _ in range(10)
    ]
    (row,) = _pairs(rows, _GRID)
    assert row.verdict == "good", "every combination has been run"
    assert row.spread_verdict == "good"


def test_a_missing_combination_is_the_finding() -> None:
    """Every aluminium run coarse and every magnesium one fine — each option
    is perfectly balanced on its own, and half the grid has never been tried."""
    rows = [{"material": "Al", "mesh": "coarse"} for _ in range(20)] + [
        {"material": "Mg", "mesh": "fine"} for _ in range(20)
    ]
    (row,) = _pairs(rows, _GRID)
    assert row.coverage_score == pytest.approx(0.5), "two cells of four"
    assert row.verdict == "thin"


def test_a_grid_larger_than_the_batch_is_judged_on_what_was_possible() -> None:
    """Twelve runs over twenty combinations are judged on whether they landed
    in twelve different ones, not blamed for not being twenty."""
    spec = descriptor(
        select("material", "Al", "Mg", "Ti", "Cu"), select("mesh", "c", "m", "f", "vf", "x")
    )
    materials = ("Al", "Mg", "Ti", "Cu")
    meshes = ("c", "m", "f", "vf", "x")
    rows = [{"material": materials[i % 4], "mesh": meshes[i % 5]} for i in range(20)]
    (row,) = _pairs(rows, spec)
    assert row.spread_score == pytest.approx(1.0), "every run landed in a fresh cell"
    assert row.coverage_score == pytest.approx(1.0)


# --- when it declines to judge ---------------------------------------------


def test_a_single_option_is_not_a_pair() -> None:
    """With one level there is no comparison to make, and "we cannot tell"
    must not render as "we can tell, and it is bad"."""
    assert _pairs(_sweep("Al", 1.0, 10.0, 40)) == ()


def test_an_option_with_one_run_is_not_measured_against_the_number() -> None:
    """One run is a point, not a spread. Measuring it would report a hole the
    width of the axis and blame the design for a level nobody has explored —
    which the option-balance row already says, in words that fit."""
    rows = [*_sweep("Al", 1.0, 10.0, 39), {"fin_height": 5.0, "material": "Mg"}]
    assert _pairs(rows) == ()


# --- saying what was found (2026-09-04) ------------------------------------


def test_a_confounded_pair_names_the_option_and_its_span() -> None:
    """Reported as *"why is Base thickness x Fan speed Unevenly spread but all
    others (excluding the ones for Plate material) Well spread?"* — the four
    material rows, all reading 0.000, had been set aside as noise.

    They were the most serious finding on the panel: on the demo cohort every
    Magnesium run sat in the middle third of every input. A red chip does not
    say that. The row now does.
    """
    (row,) = _pairs(_sweep("Al", 1.0, 10.0, 30) + _sweep("Mg", 4.0, 6.0, 20))

    assert row.spread_verdict == "poor"
    assert "Mg was only run between 4 and 6" in row.detail
    assert "out of 1 to 10" in row.detail


def test_a_healthy_pair_says_nothing_extra() -> None:
    """The sentence is a finding, not a caption — it appears only where there
    is something to report."""
    (row,) = _pairs(_sweep("Al", 1.0, 10.0, 25) + _sweep("Mg", 1.0, 10.0, 25))
    assert row.detail == ""


def test_the_finding_survives_a_round_trip() -> None:
    (row,) = _pairs(_sweep("Al", 1.0, 10.0, 30) + _sweep("Mg", 4.0, 6.0, 20))
    assert PairCoverage.from_json_obj(row.to_json_obj()).detail == row.detail
    assert PairCoverage.from_json_obj({"a": "a", "b": "b"}).detail == ""
