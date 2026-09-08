"""The pair's two readings, and when either is entitled to vote (§4.6).

Reported as *"why are all pairwise combinations shown as 'Rarely varied
together'? The cohort was made from an LHS of 20 runs, so they should have
been varied together."* Correct, and the Runs column said 20 on every row.

One number was answering two questions. The pair's covering radius divided by
a **fixed 60-run** yardstick asks *are there enough runs here*; divided by
what the pair's **own** runs could achieve it asks *are those runs arranged
well*. A small Latin hypercube answers "no" and "yes", and only the "no" was
on screen, under a label that claimed the "yes".
"""

from __future__ import annotations

import numpy as np
import pytest

from matofx.config import CoverageSettings
from matofx.studies.coverage import build_envelope, resolve_points
from matofx.studies.coverage.pairs import (
    PairCoverage,
    analyse_pairs,
    discriminating_pairs,
    pair_verdict,
)
from tests.studies._coverage_fixtures import descriptor, num, training


def _design(u: np.ndarray) -> tuple:
    names = [f"x{j}" for j in range(u.shape[1])]
    rows = [{names[j]: 1.0 + 9.0 * u[i, j] for j in range(u.shape[1])} for i in range(u.shape[0])]
    spec = descriptor(*[num(n, 1.0, 10.0) for n in names])
    points = resolve_points(training(rows), descriptor=spec)
    return points, build_envelope(points, descriptor=spec)


def _lhs(n: int, d: int, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.column_stack([(rng.permutation(n) + rng.random(n)) / n for _ in range(d)])


def _cross(n: int, d: int) -> np.ndarray:
    """One-factor-at-a-time: vary one input, hold the rest at nominal."""
    per = max(2, n // d)
    blocks = []
    for i in range(d):
        block = np.full((per, d), 0.5)
        block[:, i] = np.linspace(0.0, 1.0, per)
        blocks.append(block)
    return np.vstack(blocks)


def _pairs(u: np.ndarray, settings: CoverageSettings | None = None):
    settings = settings or CoverageSettings()
    points, envelope = _design(u)
    return analyse_pairs(envelope.normalise(points), envelope, settings=settings)


# --- the two readings disagree, and that is the point ----------------------


def test_a_small_hypercube_is_short_of_runs_but_well_arranged() -> None:
    """The reported case. Every pair carries all twenty runs."""
    pairs = _pairs(_lhs(20, 4))

    assert all(p.n_points == 20 for p in pairs), "all runs are on every pair"
    # The worst pair, not every pair: a lucky draw can put one of them over
    # the line even against the 60-run bar, and the claim is about the shape
    # of the reading rather than about every row of one sample.
    assert min(p.coverage_score for p in pairs) < 0.7, "measured against 60 runs, it is short"
    assert min(p.spread_score or 0.0 for p in pairs) > 0.8, (
        "measured against what twenty runs could do, the arrangement is near optimal"
    )


def test_a_cross_is_badly_arranged_however_many_runs_it_has() -> None:
    """The reading that has to keep working, or the pair pass loses its job."""
    small = _pairs(_cross(20, 4))
    large = _pairs(_cross(120, 4))

    assert all(p.spread_score is not None and p.spread_score < 0.75 for p in small)
    assert all(p.spread_score is not None and p.spread_score < 0.5 for p in large), (
        "more runs of the same shape must not improve the arrangement reading"
    )


def test_the_density_reading_improves_with_runs_and_the_spread_one_need_not() -> None:
    """They are different questions; a good design gains on one and not the
    other, which is exactly why both are shown."""
    small = _pairs(_lhs(20, 3))
    large = _pairs(_lhs(120, 3))

    assert min(p.coverage_score for p in large) > max(p.coverage_score for p in small)
    assert min(p.spread_score or 0.0 for p in small) > 0.8


# --- who is allowed to vote ------------------------------------------------


def test_a_pair_below_the_floor_does_not_set_the_verdict() -> None:
    """Below it the readings do not order a good design against a bad one —
    at twenty they invert — so the pass abstains rather than guessing."""
    settings = CoverageSettings()
    pairs = _pairs(_cross(20, 4), settings)

    assert all(not p.discriminating for p in pairs)
    assert discriminating_pairs(pairs, settings=settings) == ()
    assert pair_verdict(pairs, settings=settings) == "good", "abstains, not condemns"


def test_a_pair_above_the_floor_does_set_the_verdict() -> None:
    settings = CoverageSettings()
    pairs = _pairs(_cross(160, 4), settings)

    assert all(p.discriminating for p in pairs)
    assert pair_verdict(pairs, settings=settings) == "poor"


def test_a_clean_hypercube_is_never_condemned_by_its_pairs() -> None:
    """The defect this floor exists for: a clean twenty-run LHS produced a
    ``poor`` pair in most draws, dragging the whole cohort to "Poorly
    covered" on a definition — an entire two-input face never explored —
    that was untrue of it."""
    settings = CoverageSettings()
    for seed in range(6):
        assert pair_verdict(_pairs(_lhs(20, 4, seed), settings), settings=settings) == "good"


def _row(**kw) -> PairCoverage:
    base = {
        "a": "a",
        "b": "b",
        "a_label": "A",
        "b_label": "B",
        "n_points": 999,
        "covering_radius": 0.5,
        "reference_radius": 0.1,
        "coverage_score": 0.9,
        "verdict": "good",
        "spread_score": 0.9,
        "spread_verdict": "good",
        "discriminating": True,
    }
    return PairCoverage(**{**base, **kw})


def test_the_spread_reading_never_condemns_a_pair() -> None:
    """It divides by a yardstick built from the archive's own run count, so it
    is an efficiency — and an efficiency legitimately *falls* as runs are
    added. §4.4 demoted the joint efficiency from the verdict for exactly
    this reason. Wiring the pair-level twin in reproduced the symptom it
    warns about: a ladder reading poor, poor, thin, poor, poor while the
    joint score climbed monotonically to 1.000.
    """
    condemned = _row(spread_score=0.1, spread_verdict="poor")
    assert pair_verdict((condemned,), settings=CoverageSettings()) == "good"


def test_the_density_reading_does_condemn_a_pair() -> None:
    """It is measured against a bar that depends only on the dimension, so it
    moves only when the radius does — and a covering radius never rises as
    points are added. That is what keeps the verdict monotone."""
    condemned = _row(coverage_score=0.1, verdict="poor")
    assert pair_verdict((condemned,), settings=CoverageSettings()) == "poor"


# --- what a stored report does ---------------------------------------------


def test_a_report_saved_before_the_second_reading_shows_no_answer() -> None:
    """Absent is not the same as bad. Defaulting to 0.0 would render every
    pair of every archived report as the worst arrangement there is."""
    row = PairCoverage.from_json_obj(
        {"a": "a", "b": "b", "n_points": 40, "coverage_score": 0.6, "verdict": "thin"}
    )
    assert row.spread_score is None
    assert row.spread_verdict is None


def test_a_report_saved_before_the_floor_keeps_the_verdict_it_was_given() -> None:
    """It was written when every pair voted; silently turning that into an
    abstention would change a stored verdict nobody re-measured."""
    assert PairCoverage.from_json_obj({"a": "a", "b": "b"}).discriminating is True


def test_both_readings_survive_a_round_trip() -> None:
    (row,) = _pairs(_lhs(80, 2))[:1]
    back = PairCoverage.from_json_obj(row.to_json_obj())

    assert back.spread_score == pytest.approx(row.spread_score)
    assert back.spread_verdict == row.spread_verdict
    assert back.discriminating == row.discriminating
