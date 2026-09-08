"""The pairwise pass (ADR-0020 §4.3), and the calibration that licenses it.

A user opened the cohort panel on the demo heat-sink archive, looked at *Base
width* against *Material conductivity*, and saw a cross: one horizontal line
of runs, one vertical, the rest of the square empty. The panel above it read
**Well covered**, with six green per-factor rows and no warning of any kind.

Both neighbouring measures were right and both were blind:

- each row measures one input's own range, and in a cross both inputs *are*
  varied across their full range — the rows read ~98% and are correct;
- the joint score asks "as well spread as this many runs could be?", and in
  six dimensions 291 runs cannot cover anything, so an evenly spread reference
  leaves nearly as large a hole as the cross does and the ratio flatters it.

The failure lived at the level in between, which nothing measured. In two
dimensions 291 points is plentiful, the reference is demanding, and the cross
is exposed at once.

A new criterion that can push the verdict down has to be shown not to be F3 —
a light that is always red — *before* it is trusted, which is what the
calibration tests here are for.
"""

from __future__ import annotations

import itertools

import numpy as np
import pytest

from matofx.config import CoverageSettings
from matofx.studies.coverage import analyse_design, build_envelope, resolve_points
from matofx.studies.coverage.pairs import (
    PairCoverage,
    analyse_pairs,
    one_at_a_time,
    pair_verdict,
)
from matofx.studies.coverage.report import DesignCoverage
from tests.studies._coverage_fixtures import training

_SETTINGS = CoverageSettings()


def _points(rows: list[dict[str, float]]) -> tuple:
    points = resolve_points(training(rows))
    envelope = build_envelope(points)
    return points, envelope, envelope.normalise(points)


def _latin_hypercube(n: int, d: int, seed: int = 3) -> list[dict[str, float]]:
    rng = np.random.default_rng(seed)
    cols = np.column_stack([(rng.permutation(n) + rng.random(n)) / n for _ in range(d)])
    return [{f"x{i}": float(v) for i, v in enumerate(row)} for row in cols]


def _one_at_a_time(n_per: int, d: int) -> list[dict[str, float]]:
    """Vary one input, hold the rest at their midpoint. The cross."""
    rows: list[dict[str, float]] = []
    for i in range(d):
        for step in np.linspace(0.0, 1.0, n_per):
            row = {f"x{j}": 0.5 for j in range(d)}
            row[f"x{i}"] = float(step)
            rows.append(row)
    return rows


# --- calibration: it must not become a light that is always red ----------


@pytest.mark.parametrize("d", [3, 6])
def test_a_latin_hypercube_has_no_poor_pair(d: int) -> None:
    """The known-good design. If this fails, the floor is wrong, not the design.

    §3's F3 three times over was a verdict that read `poor` whatever it was
    shown. A criterion allowed to demote the whole archive earns that right by
    passing a design that genuinely is well spread.
    """
    _, envelope, x = _points(_latin_hypercube(120, d))
    pairs = analyse_pairs(x, envelope, settings=_SETTINGS)
    assert pairs
    assert not [p for p in pairs if p.verdict == "poor"]
    assert pair_verdict(pairs) == "good"


@pytest.mark.parametrize("d", [3, 6])
def test_uniform_random_runs_are_not_condemned(d: int) -> None:
    """The honest middle: scattered ad-hoc jobs are the modal archive."""
    rng = np.random.default_rng(9)
    rows = [{f"x{i}": float(v) for i, v in enumerate(row)} for row in rng.random((150, d))]
    _, envelope, x = _points(rows)
    assert not [p for p in analyse_pairs(x, envelope, settings=_SETTINGS) if p.verdict == "poor"]


# --- detection: the failure the user actually saw ------------------------


@pytest.mark.parametrize("d", [3, 6])
def test_one_factor_at_a_time_fails_every_pair(d: int) -> None:
    """The cross. Every pair, not one awkward corner."""
    _, envelope, x = _points(_one_at_a_time(40, d))
    pairs = analyse_pairs(x, envelope, settings=_SETTINGS)
    assert pairs
    assert all(p.verdict == "poor" for p in pairs)
    assert pair_verdict(pairs) == "poor"
    assert one_at_a_time(pairs, floor=_SETTINGS.one_at_a_time_floor)


def test_the_cross_is_caught_even_though_every_row_is_green() -> None:
    """The whole point: the two neighbouring measures both pass it.

    This is the exact shape the user was looking at — full marginal spread,
    empty interior — and before §4.3 the panel called it well covered.
    """
    points, envelope, _ = _points(_one_at_a_time(40, 4))
    design = analyse_design(points, envelope, settings=_SETTINGS)

    # Each input on its own: varied right across its range.
    assert all(f.verdict == "good" for f in design.per_factor if f.actionable)
    # Together: not at all.
    assert design.verdict == "poor"
    assert design.per_pair
    assert "one at a time" in design.message or "combinations" in design.message


def test_the_warning_names_the_remedy_that_applies() -> None:
    """A cross needs a designed set, not a few gap-filling runs."""
    points, envelope, _ = _points(_one_at_a_time(40, 4))
    design = analyse_design(points, envelope, settings=_SETTINGS)
    codes = {w.code for w in design.warnings}
    assert "one_at_a_time" in codes
    text = next(w.message for w in design.warnings if w.code == "one_at_a_time")
    assert "several inputs together" in text


# --- honesty about what was and was not measured -------------------------


def test_a_pair_with_too_few_placeable_runs_is_not_judged() -> None:
    """ "We cannot tell" is not "we can tell, and it is bad"."""
    rows = [{"a": float(i), "b": float(i)} for i in range(5)]
    _, envelope, x = _points(rows)
    assert analyse_pairs(x, envelope, settings=_SETTINGS) == ()


def test_the_sweep_is_capped_on_axes_and_the_count_is_reported() -> None:
    """A capped sweep presented as a full one is silent truncation (F2)."""
    settings = CoverageSettings(max_pair_axes=4)
    points, envelope, x = _points(_latin_hypercube(120, 7))
    pairs = analyse_pairs(x, envelope, settings=settings)
    assert len(pairs) == 6  # 4 axes -> 6 pairs, not 21

    design = analyse_design(points, envelope, settings=settings)
    assert design.n_pairs_examined == 6
    assert design.n_pairs_total == 21


def test_pairs_come_back_worst_first() -> None:
    """The panel and the plots both read the head of this list."""
    _, envelope, x = _points(_latin_hypercube(120, 5))
    scores = [p.coverage_score for p in analyse_pairs(x, envelope, settings=_SETTINGS)]
    assert scores == sorted(scores)


def test_a_pair_carries_labels_for_the_surfaces_that_render_it() -> None:
    _, envelope, x = _points(_latin_hypercube(60, 3))
    pair = analyse_pairs(x, envelope, settings=_SETTINGS)[0]
    # No descriptor in this fixture, so labels fall back to names — never blank.
    assert pair.a_label
    assert pair.b_label
    assert pair.pair_label == f"{pair.a_label} and {pair.b_label}"


def test_pairs_survive_a_round_trip_through_storage() -> None:
    """A stored report outlives the release that wrote it (§9)."""
    _, envelope, x = _points(_latin_hypercube(60, 3))
    original = analyse_pairs(x, envelope, settings=_SETTINGS)[0]
    assert PairCoverage.from_json_obj(original.to_json_obj()) == original


def test_a_report_written_before_pairs_existed_still_reads() -> None:
    """An older row has no `per_pair`; it must not become an error."""
    restored = DesignCoverage.from_json_obj(
        {"criterion": "model_free", "verdict": "good", "message": "x"}
    )
    assert restored.per_pair == ()
    assert restored.n_pairs_total == 0


# --- the aggregation rule ------------------------------------------------


def test_a_thin_pair_alone_does_not_demote_the_archive() -> None:
    """A wide Latin hypercube legitimately produces thin pairs.

    Letting one demote the whole archive would mark good designs down for
    being wide — F3 wearing a different hat.
    """
    thin = PairCoverage(
        a="a",
        b="b",
        a_label="A",
        b_label="B",
        n_points=100,
        covering_radius=0.2,
        reference_radius=0.1,
        coverage_score=0.5,
        verdict="thin",
    )
    assert pair_verdict((thin,)) == "good"


def test_one_bad_corner_is_not_called_one_at_a_time() -> None:
    """Different diagnosis, different remedy — so it must not over-claim."""

    def pair(name: str, score: float, verdict: str) -> PairCoverage:
        return PairCoverage(
            a=name,
            b="z",
            a_label=name,
            b_label="Z",
            n_points=100,
            covering_radius=0.2,
            reference_radius=0.1,
            coverage_score=score,
            verdict=verdict,  # type: ignore[arg-type]
        )

    mixed = (pair("a", 0.2, "poor"), pair("b", 0.9, "good"))
    assert pair_verdict(mixed) == "poor"
    assert not one_at_a_time(mixed, floor=_SETTINGS.one_at_a_time_floor)


def test_every_pair_is_scored_when_the_cap_does_not_bite() -> None:
    _, envelope, x = _points(_latin_hypercube(80, 4))
    assert len(analyse_pairs(x, envelope, settings=_SETTINGS)) == len(
        list(itertools.combinations(range(4), 2))
    )
