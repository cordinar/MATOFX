"""Pairwise coverage — the level between one input and all of them (§4.6).

**The level the engine did not have, and the one users actually look at.**
Coverage was measured at two scales: each input's own range (the per-factor
strip) and the whole joint space (the verdict). A real archive failed in
between, and neither scale could see it.

The demo heat-sink archive is the case. 531 runs, six varying inputs, every
per-factor row green, overall verdict ``good`` — and *Base width* against
*Material conductivity* plotted as a cross: one horizontal line of runs and
one vertical, with the entire rest of the square empty. Fourteen of its
fifteen pairs score ``thin`` or ``poor``; five score below 0.35. That is a
one-factor-at-a-time archive, and no model built on it can learn how two
inputs interact.

**Why the joint verdict cannot catch it.** The score asks "is this as well
spread as this many runs *could* be?", and in six dimensions 291 runs cannot
cover anything — an evenly spread reference leaves nearly as big a hole as the
cross does, so the ratio flatters everything. The measure is not wrong; it is
uninformative at that width. In **two** dimensions 291 points is plenty, the
reference is demanding, and the cross is exposed immediately.

**Why the per-factor rows cannot catch it either.** A cross has both inputs
varied across their full range. Each row reads ~98% covered, and each row is
right. Marginals are blind to structure by construction.

Calibrated against known-good and known-bad designs before being allowed near
the verdict — §3's F3 is a light that is always red, and a new criterion that
demotes healthy archives would be exactly that. Latin hypercubes and uniform
random designs produce **no** poor pair at 3, 6 or 10 factors; one-factor-at-
a-time and tight clusters produce poor on **every** pair. The separation is
wide (good designs bottom out near 0.58, bad ones top out near 0.36) and the
0.45 floor sits in the middle of it.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from matofx.studies.coverage.envelope import Envelope
from matofx.studies.coverage.metrics import covering_radius, reference_radius

if TYPE_CHECKING:
    from collections.abc import Mapping

    from matofx.config import CoverageSettings
    from matofx.studies.coverage.points import PointSet
    from matofx.studies.coverage.report import Verdict

_PAIR_AXES = 2
_PAIR_SEED = 20_020
"""The report's candidate seed, so a pair is probed like everything else."""


@dataclass(frozen=True, slots=True)
class PairCoverage:
    """How well two inputs were varied **against each other**."""

    a: str
    b: str
    a_label: str
    b_label: str
    n_points: int
    """Runs placeable on *both* axes. Different pairs legitimately differ:
    a run missing one input can still be placed on a pair that excludes it."""
    covering_radius: float
    reference_radius: float
    coverage_score: float
    verdict: Verdict

    spread_score: float | None = None
    spread_verdict: Verdict | None = None
    """The same radius against a design of **this pair's own run count**.

    Two questions were being answered by one number, and only the first of
    them was being asked. :attr:`coverage_score` divides by a fixed 60-run
    yardstick, so it says *do you have enough runs here* — and a clean Latin
    hypercube of twenty reads ``thin`` on every pair for that reason alone,
    under a label ("rarely varied together") that claims something about the
    *arrangement*. Reported as "the cohort was made from an LHS of 20 runs,
    so they should have been varied together".

    This one divides by what those same twenty runs could achieve at best, so
    it answers the arrangement question: an LHS scores near 1.0 whatever its
    size, and a one-factor-at-a-time cross scores low whatever its size. The
    two together separate "too few runs" from "badly arranged", which no
    single number can.

    ``None`` on a report stored before this existed — rendered as "—" rather
    than as a zero, which would read as the worst possible arrangement.
    """

    detail: str = ""
    """The finding in words, where the numbers alone would be dismissed.

    A number-against-choice row reading 0.000 on four inputs at once looks
    like noise and was read as noise — "excluding the ones for Plate
    material". It was not noise: on the demo cohort every Magnesium run sat
    in the middle third of *every* input, which is the confounding this pass
    exists to catch and the most serious thing on the panel. A red chip does
    not say that; a sentence naming the option and its span does."""

    discriminating: bool = True
    """Whether this pair carries enough runs for either reading to mean
    anything (:attr:`CoverageSettings.min_discriminating_pair_runs`).

    Decided here because this is where the settings are, and stored with the
    row so every reader — the verdict, the table, a report rebuilt from JSON
    years later — agrees about which pairs were entitled to vote. Defaults
    ``True`` so a report written before the floor existed keeps the verdict
    it was given rather than silently gaining an abstention."""

    @property
    def pair_label(self) -> str:
        """What to call this pair on screen — labels, never internal names."""
        return f"{self.a_label} and {self.b_label}"

    def to_json_obj(self) -> dict[str, Any]:
        """A JSON-safe view (§9 persists the report as JSON)."""
        return {
            "a": self.a,
            "b": self.b,
            "a_label": self.a_label,
            "b_label": self.b_label,
            "n_points": self.n_points,
            "covering_radius": self.covering_radius,
            "reference_radius": self.reference_radius,
            "coverage_score": self.coverage_score,
            "verdict": self.verdict,
            "spread_score": self.spread_score,
            "spread_verdict": self.spread_verdict,
            "discriminating": self.discriminating,
            "detail": self.detail,
        }

    @classmethod
    def from_json_obj(cls, raw: Mapping[str, Any]) -> PairCoverage:
        """Rebuild a pair row from its stored form, tolerating unknown keys.

        The spread pair stays ``None`` when absent rather than defaulting.
        A report persisted before §4.6's second reading existed genuinely has
        no answer, and ``0.0`` would render it as the worst arrangement there
        is — inventing a finding about an archive nobody re-measured.
        """
        from matofx.studies.coverage.report import _as_verdict  # noqa: PLC0415

        spread = raw.get("spread_score")
        spread_verdict = raw.get("spread_verdict")
        return cls(
            a=str(raw.get("a") or ""),
            b=str(raw.get("b") or ""),
            a_label=str(raw.get("a_label") or ""),
            b_label=str(raw.get("b_label") or ""),
            n_points=int(raw.get("n_points") or 0),
            covering_radius=float(raw.get("covering_radius") or 0.0),
            reference_radius=float(raw.get("reference_radius") or 0.0),
            coverage_score=float(raw.get("coverage_score") or 0.0),
            verdict=_as_verdict(raw.get("verdict")),
            spread_score=None if spread is None else float(spread),
            spread_verdict=None if spread_verdict is None else _as_verdict(spread_verdict),
            discriminating=bool(raw.get("discriminating", True)),
            detail=str(raw.get("detail") or ""),
        )


def analyse_pairs(
    x: np.ndarray,
    envelope: Envelope,
    *,
    settings: CoverageSettings,
    per_factor_scores: Mapping[str, float] | None = None,
    points: PointSet | None = None,
) -> tuple[PairCoverage, ...]:
    """Score every pair of movable axes, worst first.

    ``points`` unlocks the pairs involving a **choice**: those cannot be read
    off ``x``, which carries the numeric axes alone. Optional so a caller with
    only a matrix still gets the numeric sweep it always got.

    ``x`` is the **already normalised** point matrix — the same one the joint
    radius is measured over, so a pair cannot disagree with the whole about
    where a run sits.

    Pairs are capped by axis count rather than by pair count: at thirteen
    factors there are seventy-eight pairs, and the cost is real. When the cap
    bites, the axes kept are the ones whose *own* coverage is weakest, since a
    thin axis is the likelier half of a thin pair. The caller says so out loud
    rather than presenting a partial sweep as a complete one.
    """
    axes = [name for name in envelope.metric_axes if envelope.by_name(name).actionable]
    options = [name for name in envelope.categorical_axes if envelope.by_name(name).actionable]
    if len(axes) + len(options) < _PAIR_AXES or x.shape[0] == 0:
        return ()

    scores = per_factor_scores or {}
    axes = sorted(axes, key=lambda n: (scores.get(n, 1.0), n))[: settings.max_pair_axes]
    options = sorted(options)[: settings.max_pair_axes]

    rows: list[PairCoverage] = []
    for a, b in itertools.combinations(sorted(axes), 2):
        row = _pair(x, envelope, a, b, settings=settings)
        if row is not None:
            rows.append(row)
    # **Choices belong here too.** The sweep walked ``metric_axes`` alone, so
    # a varying choice contributed no pair at all — the Fourth Study runs 40
    # jobs across two materials and still showed exactly the six numeric
    # pairs. Nothing else covers it either: every categorical check in the
    # report is *marginal* (counts per option, "an option barely used"), so
    # an archive that ran Aluminium only at low fin heights and Magnesium
    # only at high ones passes all of them, which is precisely the
    # confounding a model cannot see through and the pairwise pass exists to
    # catch.
    if points is not None:
        for name in options:
            for other in axes:
                row = _number_by_option_pair(points, envelope, other, name, settings=settings)
                if row is not None:
                    rows.append(row)
        for a, b in itertools.combinations(options, 2):
            row = _option_pair(points, envelope, a, b, settings=settings)
            if row is not None:
                rows.append(row)
    return tuple(sorted(rows, key=lambda r: (r.coverage_score, r.a, r.b)))


def _pair(
    x: np.ndarray,
    envelope: Envelope,
    a: str,
    b: str,
    *,
    settings: CoverageSettings,
) -> PairCoverage | None:
    """One pair, or ``None`` when there is not enough of it to judge.

    Too few placeable runs is ``insufficient``, not ``poor``: "we cannot tell"
    and "we can tell, and it is bad" are different answers, and a two-point
    projection would report the second while meaning the first.
    """
    from matofx.studies.coverage.report import score_for, verdict_for  # noqa: PLC0415

    fa, fb = envelope.by_name(a), envelope.by_name(b)
    columns = x[:, [envelope.column_index(a), envelope.column_index(b)]]
    placed = columns[np.isfinite(columns).all(axis=1)]
    placed = placed[((placed >= 0.0) & (placed <= 1.0)).all(axis=1)]
    if placed.shape[0] < settings.min_points_per_pair:
        return None

    sub = Envelope(factors=(fa, fb))
    radius = covering_radius(placed, sub, n_candidates=settings.pair_n_candidates, seed=_PAIR_SEED)
    # Fixed to the standard, like the joint bar (§4.4) — a pair scored against
    # its own point count moves under the user for the same reason, and did:
    # `Fin thickness x Material conductivity` fell from 0.473 to 0.433 between
    # 60 and 120 added runs while its gap shrank from 0.179 to 0.154.
    ideal = reference_radius(
        sub,
        settings.standard_runs_per_input * _PAIR_AXES,
        n_candidates=settings.pair_n_candidates,
        seed=_PAIR_SEED,
        replicates=settings.reference_replicates,
    )
    score = score_for(radius, _PAIR_AXES, ideal)
    # **The same radius, against what this pair's own runs could achieve.**
    # The bar above is fixed so the density reading stays monotone; this one
    # moves with the run count on purpose, because "are these runs arranged
    # well" is a question whose answer must not improve simply because there
    # are more of them. See :attr:`PairCoverage.spread_score`.
    own = reference_radius(
        sub,
        int(placed.shape[0]),
        n_candidates=settings.pair_n_candidates,
        seed=_PAIR_SEED,
        replicates=settings.reference_replicates,
    )
    spread = score_for(radius, _PAIR_AXES, own)
    return PairCoverage(
        a=a,
        b=b,
        a_label=fa.display_name,
        b_label=fb.display_name,
        n_points=int(placed.shape[0]),
        covering_radius=radius,
        reference_radius=ideal,
        coverage_score=score,
        verdict=verdict_for(score, settings),
        spread_score=spread,
        spread_verdict=verdict_for(spread, settings),
        discriminating=int(placed.shape[0]) >= settings.min_discriminating_pair_runs,
    )


def discriminating_pairs(
    pairs: tuple[PairCoverage, ...], *, settings: CoverageSettings
) -> tuple[PairCoverage, ...]:
    """The pairs carrying enough runs for the criterion to mean anything.

    See :attr:`CoverageSettings.min_discriminating_pair_runs` for the
    measurement. Below that floor a well-designed pair and a badly-designed
    one are indistinguishable, and at the smallest sizes they invert.
    """
    del settings  # the rule was applied when the row was built
    return tuple(p for p in pairs if p.discriminating)


def _number_by_option_pair(
    points: PointSet,
    envelope: Envelope,
    number: str,
    option: str,
    *,
    settings: CoverageSettings,
) -> PairCoverage | None:
    """A number against a choice: is the number covered **within each option**?

    The plain-English question is "did we sweep fin height for *every*
    material, or only for aluminium?", and the worst option is the answer —
    a model asked about magnesium at a fin height nobody ran there is
    extrapolating however good the aluminium coverage was.

    Measured one dimension at a time rather than by a Gower-style mixed
    distance. Both would produce a number; only this one produces a number a
    reader can act on, because the failing *option* is named by construction.

    **The arrangement half uses the per-factor row's own formula**, not a
    radius against a reference of the same size. The first attempt did the
    latter and was wrong in a way that only showed on real data: a
    stratified reference puts one point in every ``1/n`` slice, while the
    1-D projection of a space-filling batch is closer to uniform-random,
    whose largest gap runs about ``ln(n)/n``. The ratio therefore sits near
    ``1/ln(n)`` however good the design is. It scored a batch covering both
    materials across the whole declared range — biggest gaps 0.115 and 0.138
    of the range — at **0.31, poor**, and dragged a cohort whose joint score
    was a perfect 1.000 down to "Poorly covered". ``1 - 2 * largest gap`` is
    what the per-input strip already uses for exactly this question.

    ``None`` where fewer than two options carry enough runs to measure — with
    one option there is no comparison to make, and "we cannot tell" must not
    render as "we can tell, and it is bad".
    """
    from matofx.studies.coverage.envelope import level_label  # noqa: PLC0415
    from matofx.studies.coverage.report import largest_gap, verdict_for  # noqa: PLC0415

    factor = envelope.by_name(number)
    choice = envelope.by_name(option)
    sub = Envelope(factors=(factor,))
    declared = [str(lv) for lv in (choice.levels or ())]

    by_level: dict[str, list[float]] = {lv: [] for lv in declared}
    for value, raw_level in zip(points.values(number), points.values(option), strict=False):
        level = level_label(raw_level)
        unit = _unit(sub, factor, value)
        if unit is None or level not in by_level:
            continue
        by_level[level].append(unit)

    placed = sum(len(v) for v in by_level.values())
    if placed < settings.min_points_per_pair:
        return None
    measurable = {lv: v for lv, v in by_level.items() if len(v) >= _MIN_LEVEL_POINTS}
    if len(measurable) < _PAIR_AXES:
        return None

    # **Enough runs** is about the *thinnest* option, and it is a count, so it
    # rises whenever a run lands anywhere and never falls — which is what lets
    # it set the verdict. A declared option nobody has run scores it zero,
    # which is the right answer and the same one the option-balance row gives.
    thinnest = min(len(by_level[lv]) for lv in declared) if declared else 0
    density = max(0.0, min(1.0, thinnest / settings.standard_runs_per_input))
    # **Well spread** is the worst option's own 1-D coverage.
    scored = {
        lv: max(0.0, 1.0 - largest_gap(sorted(values)) * 2.0) for lv, values in measurable.items()
    }
    spread = min(scored.values())
    worst = max(largest_gap(sorted(values)) for values in measurable.values())
    worst_level = min(scored, key=lambda lv: scored[lv])
    detail = ""
    if verdict_for(spread, settings) != "good":
        span = by_level[worst_level]
        lo, hi = min(span), max(span)
        detail = (
            f"{worst_level} was only run between "
            f"{_denorm(factor, lo):g} and {_denorm(factor, hi):g}, "
            f"out of {factor.lo:g} to {factor.hi:g}."
        )
    return PairCoverage(
        a=number,
        b=option,
        a_label=factor.display_name,
        b_label=choice.display_name,
        n_points=placed,
        covering_radius=worst,
        reference_radius=0.0,
        coverage_score=density,
        verdict=verdict_for(density, settings),
        spread_score=spread,
        spread_verdict=verdict_for(spread, settings),
        discriminating=placed >= settings.min_discriminating_pair_runs,
        detail=detail,
    )


def _denorm(factor: Any, unit: float) -> float:
    """A normalised coordinate back in the input's own units, for the prose."""
    lo, hi = factor.lo, factor.hi
    if lo is None or hi is None:
        return unit
    return float(lo) + unit * (float(hi) - float(lo))


def _option_pair(
    points: PointSet,
    envelope: Envelope,
    a: str,
    b: str,
    *,
    settings: CoverageSettings,
) -> PairCoverage | None:
    """Two choices: how much of their combination grid has been run?

    No distance exists between two option sets, so the covering radius has
    nothing to measure. What *can* go wrong is a cell nobody has tried —
    every aluminium run on the coarse mesh and every magnesium one on the
    fine — and that is a counting question, the same one
    ``_fill_option_gaps`` fills.

    Two readings, to the same pattern as everywhere else. **Enough runs**
    compares cells occupied against the whole grid; **well spread** compares
    against the most cells this many runs *could* have occupied, which is
    ``min(runs, cells)`` — so twelve runs over twenty combinations are judged
    on whether they landed in twelve different ones, not blamed for not being
    twenty.
    """
    from matofx.studies.coverage.envelope import level_label  # noqa: PLC0415
    from matofx.studies.coverage.report import verdict_for  # noqa: PLC0415

    fa, fb = envelope.by_name(a), envelope.by_name(b)
    levels_a = [str(lv) for lv in (fa.levels or ())]
    levels_b = [str(lv) for lv in (fb.levels or ())]
    if len(levels_a) < _PAIR_AXES or len(levels_b) < _PAIR_AXES:
        return None

    cells = len(levels_a) * len(levels_b)
    seen: set[tuple[str, str]] = set()
    placed = 0
    for va, vb in zip(points.values(a), points.values(b), strict=False):
        key = (level_label(va), level_label(vb))
        if key[0] not in levels_a or key[1] not in levels_b:
            continue
        placed += 1
        seen.add(key)
    if placed < settings.min_points_per_pair:
        return None

    # `score_for` divides a yardstick by a measurement; here the measurement
    # is already a fraction the right way up, so the ratio is formed directly
    # rather than pretending an occupancy is a radius.
    density = len(seen) / cells
    spread = len(seen) / max(1, min(placed, cells))
    return PairCoverage(
        a=a,
        b=b,
        a_label=fa.display_name,
        b_label=fb.display_name,
        n_points=placed,
        covering_radius=0.0,
        reference_radius=0.0,
        coverage_score=density,
        verdict=verdict_for(density, settings),
        spread_score=spread,
        spread_verdict=verdict_for(spread, settings),
        discriminating=placed >= settings.min_discriminating_pair_runs,
    )


def _unit(sub: Envelope, factor: Any, value: Any) -> float | None:
    """One value on one axis, normalised to ``[0,1]``, or ``None`` if unplaceable."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    unit = sub.unit_value(factor, value)
    return None if not np.isfinite(unit) else float(unit)


_MIN_LEVEL_POINTS = 2
"""Runs an option needs before its within-option coverage can be measured.

One run is a point, not a spread; measuring it would report a hole the width
of the axis and blame the design for a level nobody has explored yet — which
the option-balance row already says, in words that fit."""


def pair_verdict(
    pairs: tuple[PairCoverage, ...], *, settings: CoverageSettings | None = None
) -> Verdict:
    """The pairwise pass's contribution to the overall verdict.

    **Only ``poor`` pairs count.** A genuine Latin hypercube produces ``thin``
    pairs once there are enough factors — its worst pair bottoms out near 0.58
    at ten — so letting a thin pair demote the whole archive would mark good
    designs down for being wide, which is F3 wearing a new hat. A ``poor``
    pair is different: it means an entire two-input face of the design space
    was never explored, and no amount of width makes that expected.

    **And the *spread* reading never counts, however good it looks.** It
    divides by a yardstick built from the archive's own run count, so it is
    an efficiency — and an efficiency legitimately *falls* when runs are
    added, because a larger set is held to a higher standard. §4.4 demoted
    the joint efficiency from the verdict for precisely this reason, citing
    "a plan table reading ``good`` at 60 suggested runs and ``poor`` at 120".
    Wiring the pair-level twin into the verdict reproduced that symptom
    exactly: on cohort 5 over the full declared range the ladder read poor,
    poor, thin, poor, poor while the joint score climbed monotonically to
    1.000. The spread column is reported, and it is diagnostic.

    ``coverage_score`` is measured against a bar that depends only on the
    dimension, so it moves only when the radius does, and a covering radius
    never rises as points are added. That is what keeps the verdict monotone.

    **And only where there are enough runs for it to discriminate.** Below
    :attr:`~CoverageSettings.min_discriminating_pair_runs` the reading does
    not order a good design against a bad one, so the pass abstains rather
    than guessing. ``settings=None`` keeps the historical behaviour for
    callers that have none to hand.
    """
    judged = pairs if settings is None else discriminating_pairs(pairs, settings=settings)
    if any(p.verdict == "poor" for p in judged):
        return "poor"
    return "good"


def one_at_a_time(
    pairs: tuple[PairCoverage, ...],
    *,
    floor: float,
    settings: CoverageSettings | None = None,
) -> bool:
    """Whether the archive looks one-factor-at-a-time rather than merely thin.

    A cross fails *every* pair, not a couple: the runs lie along the axes and
    the interior is empty throughout. Distinguishing this from one awkward
    corner matters, because the remedy is different — an OFAT archive needs a
    designed set varying several inputs together, not a few gap-filling runs.

    **Reads the arrangement, not the run count.** "They were varied one at a
    time" is a claim about the *shape* of the design, and it was tested
    against ``coverage_score``, which divides by a fixed 60-run bar. On a
    small archive every pair sits near that floor for want of runs — a clean
    eight-input hypercube of 24 runs measures 0.443 against a floor of 0.45 —
    so the claim was one unlucky draw away from being made about a textbook
    design. This is a *diagnostic* rather than a verdict term, so the
    non-monotone reading is the right one to use here: the question is what
    shape the runs are in, not how many there are.

    Restricted to pairs carrying enough runs to discriminate for the same
    reason. Below that floor neither reading orders a good design against a
    bad one, and with nothing left to judge the answer is "no claim".
    """
    judged = [
        p
        for p in (pairs if settings is None else discriminating_pairs(pairs, settings=settings))
        if p.verdict != "insufficient"
    ]
    if len(judged) < _PAIR_AXES:
        return False
    return all(
        (p.spread_score if p.spread_score is not None else p.coverage_score) < floor for p in judged
    )


__all__ = [
    "PairCoverage",
    "analyse_pairs",
    "discriminating_pairs",
    "one_at_a_time",
    "pair_verdict",
]
