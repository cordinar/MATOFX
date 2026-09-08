"""The coverage verdict and the report it travels in (ADR-0020 §2.1, §7).

**Coverage is a property of the design, not of the fit.** Q² is computed by
leave-out cross-validation on the points that exist, so a tightly clustered
cohort scores well precisely because every held-out point has a near
neighbour — high Q², no coverage, false confidence. This module measures the
geometry instead, before any response is chosen and without fitting anything.

The verdict **never blocks a fit**. It is recorded and travels with the model.
A user may knowingly build on thin data; they may not do so unknowingly.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

from matofx.config import CoverageSettings
from matofx.studies.coverage.diagnose import CoverageDiagnostics, diagnose
from matofx.studies.coverage.envelope import Envelope, level_label
from matofx.studies.coverage.metrics import (
    candidate_resolution,
    centered_l2_discrepancy,
    covering_radius,
    lattice_occupancy,
    maximin_distance,
    measured_rows,
    nn_distance_distribution,
    reference_radius,
    trained_span,
)
from matofx.studies.coverage.pairs import (
    PairCoverage,
    analyse_pairs,
    discriminating_pairs,
    one_at_a_time,
    pair_verdict,
)

if TYPE_CHECKING:
    import datetime as dt

    from matofx.studies.coverage.envelope import FactorEnvelope, FactorKind, Scale
    from matofx.studies.coverage.points import PointSet

Verdict = Literal["good", "thin", "poor", "insufficient"]
_VERDICTS: frozenset[str] = frozenset({"good", "thin", "poor", "insufficient"})
Measure = Literal["covering_radius", "lattice_occupancy", "level_balance", "pinned", "span"]
Criterion = Literal["model_free", "gp_variance"]

_CANDIDATE_SEED = 20_020
"""Fixed seed for the candidate sample. Coverage must be reproducible — the
same cohort and envelope have to give the same verdict twice, or the cached
report of §9.1 would drift under an unchanged cache key."""

_VERDICT_ORDER: dict[str, int] = {"good": 0, "thin": 1, "poor": 2, "insufficient": 3}


@dataclass(frozen=True, slots=True)
class CoverageWarning:
    """One plain-English thing the user should know.

    Per ``feedback_users_are_not_modellers`` the text is written for an
    engineer who is not a modeller — "two of your inputs move together, so the
    model can't tell their effects apart", never "VIF = 8.4". ``factor`` names
    the axis where there is one, so the per-factor strip can put the remedy
    against the right row.
    """

    code: str
    message: str
    factor: str | None = None
    factor_label: str | None = None
    """The axis's display label, so a surface can highlight the right row
    without re-resolving a descriptor ([[feedback_use_labels_not_names]]).
    ``message`` is already written in labels; this is for matching."""

    def to_json_obj(self) -> dict[str, Any]:
        """A JSON-safe view of this warning."""
        return {
            "code": self.code,
            "message": self.message,
            "factor": self.factor,
            "factor_label": self.factor_label,
        }

    @classmethod
    def from_json_obj(cls, raw: Mapping[str, Any]) -> CoverageWarning:
        """Rebuild a warning from its stored form."""
        return cls(
            code=str(raw.get("code") or ""),
            message=str(raw.get("message") or ""),
            factor=raw.get("factor"),
            factor_label=raw.get("factor_label"),
        )


def _as_verdict(value: object) -> Verdict:
    """A stored verdict, clamped to one this release understands.

    A stored report outlives the release that wrote it in **both** directions:
    an older reader can meet a newer row. An unrecognised verdict becomes
    ``insufficient`` -- "we cannot tell" -- because that is the only answer
    that cannot overclaim, and because letting the raw string through put a
    value outside the API's ``Literal`` into a response model, turning a
    forward-compatible read into a 500.
    """
    return value if value in _VERDICTS else "insufficient"  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class FactorCoverage:
    """One row of the per-factor strip — a measurement, not prose."""

    name: str
    kind: FactorKind
    measure: Measure
    scale: Scale
    value: float
    verdict: Verdict
    missing_values: tuple[str, ...] = ()
    actionable: bool = True
    label: str = ""
    """Copied from the axis's :attr:`FactorEnvelope.label` when the row is
    built — the envelope stays the one place a label is *resolved*."""

    simulated_lo: float | None = None
    simulated_hi: float | None = None
    """The lowest and highest values these runs actually used."""

    declared_lo: float | None = None
    declared_hi: float | None = None
    """The bounds the tool itself allows, or ``None`` where it sets none."""

    declared_value: float = 0.0
    declared_verdict: Verdict = "insufficient"
    """Coverage of the **tool's own** range, rather than of the range these
    runs happened to span (§4.5).

    Every figure the panel had was measured against the observed spread, so
    "98% covered" always meant *98% of the range you chose to run* — a
    self-referential reading that cannot report the one thing a reader most
    wants to know: how much of what this tool accepts has anybody tried? On
    the demo heat-sink archive ``base_temperature_rise`` is held at 60 out of
    a declared 10-120, which every existing reading called fine.

    ``insufficient`` where the descriptor declares no bounds — there is no
    denominator, and a percentage of an unknown range would be invented."""

    @property
    def display_name(self) -> str:
        """What a user should see for this row — never the internal name."""
        return self.label or self.name

    def to_json_obj(self) -> dict[str, Any]:
        """A JSON-safe view of this row."""
        return {
            "label": self.label,
            "name": self.name,
            "kind": self.kind,
            "measure": self.measure,
            "scale": self.scale,
            "value": self.value,
            "verdict": self.verdict,
            "missing_values": list(self.missing_values),
            "actionable": self.actionable,
            "simulated_lo": self.simulated_lo,
            "simulated_hi": self.simulated_hi,
            "declared_lo": self.declared_lo,
            "declared_hi": self.declared_hi,
            "declared_value": self.declared_value,
            "declared_verdict": self.declared_verdict,
        }

    @classmethod
    def from_json_obj(cls, raw: Mapping[str, Any]) -> FactorCoverage:
        """Rebuild a strip row from its stored form."""
        return cls(
            name=str(raw.get("name") or ""),
            kind=raw.get("kind", "continuous"),
            measure=raw.get("measure", "covering_radius"),
            scale=raw.get("scale", "linear"),
            value=float(raw.get("value") or 0.0),
            verdict=_as_verdict(raw.get("verdict")),
            missing_values=tuple(str(v) for v in (raw.get("missing_values") or [])),
            actionable=bool(raw.get("actionable", True)),
            label=str(raw.get("label") or ""),
            simulated_lo=_opt_float(raw.get("simulated_lo")),
            simulated_hi=_opt_float(raw.get("simulated_hi")),
            declared_lo=_opt_float(raw.get("declared_lo")),
            declared_hi=_opt_float(raw.get("declared_hi")),
            declared_value=float(raw.get("declared_value") or 0.0),
            declared_verdict=_as_verdict(raw.get("declared_verdict")),
        )


@dataclass(frozen=True, slots=True)
class DesignCoverage:
    """The response-agnostic verdict: is this design fit to learn from?"""

    criterion: Criterion
    verdict: Verdict
    message: str
    covering_radius: float
    coverage_score: float
    n_candidates: int = 0
    """How many probe points the radius was estimated over.

    Recorded because ``covering_radius`` is a **sampled, low-biased**
    estimate, not a measurement, and a stored report outlives the setting that
    produced it. Without this, two reports written months apart under
    different ``n_candidates`` look directly comparable and are not."""

    efficiency_score: float = 0.0
    """Are these runs arranged well **for how many there are**? (§4.4)

    The old headline number, kept because it answers a real question -- did
    you get your money's worth out of the runs you made? -- and demoted
    because it is not the question the panel asks. Scored against a bar that
    moves with the archive's own size, so it can fall when runs are added:
    that is meaningful about *efficiency* and nonsense about *coverage*."""

    discriminating: bool = True
    """Whether the joint score is meaningful at this width (§4.4).

    ``False`` above ``max_discriminating_axes``, where a covering radius over
    the whole space cannot separate a clustered design from a Latin hypercube.
    The number is still reported; it just does not set the light, and the
    surface must say why rather than showing a figure that looks like a
    verdict."""

    reference_radius: float = 0.0
    """The radius an evenly-spread design of the *standard* size would achieve.

    The yardstick ``coverage_score`` is measured against, recorded because the
    score is a **ratio** and a ratio alone is not comparable between two
    reports. Adding runs raises this bar, so an archive topped up with badly
    placed runs can score lower while genuinely covering more — the covering
    radius is the monotone number, the score is the efficiency one. Without
    the yardstick stored, nobody reading two reports a month apart can tell
    which of the two moved."""
    per_factor: tuple[FactorCoverage, ...] = ()
    per_pair: tuple[PairCoverage, ...] = ()
    """How well each pair of inputs was varied **against each other** (§4.6).

    The level between one input and all of them, and the one a real archive
    failed at while both neighbours read healthy. Ordered worst first."""
    n_pairs_examined: int = 0
    n_pairs_total: int = 0
    """Pairs actually scored, and pairs that exist. They differ when the axis
    cap bites; the surface says so rather than implying a full sweep."""
    min_points_per_pair: int = 0
    sampling_modes: tuple[str, ...] = ()
    """How the runs feeding this archive were sampled, where that is known.

    **Provenance, not measurement — and at low density it is the only thing
    that can answer the question.** Below about twenty runs per input no
    geometric reading can tell a Latin hypercube from a cluster (see
    :attr:`density_discriminating`), so the panel cannot *measure* that a
    small archive is well arranged. But it can often simply know: a cohort
    whose runs all came from an ``lhs`` study is space-filling by
    construction, whatever twenty points look like to a covering radius.

    Empty where it cannot be established — ad-hoc jobs, a mix of studies and
    one-offs, or a cohort assembled by hand. Empty is "we do not know", never
    "it was not designed", and nothing may read it the other way.
    """

    density_discriminating: bool = True
    """Whether there are enough runs **per input** for the figures to order a
    well-arranged design against a badly-arranged one.

    Distinct from :attr:`discriminating`, which is about width. That one
    stands the joint score down, because above six inputs the blind spot is
    *flattering* — a cluster scores 0.79 and reads ``good``. This one changes
    no verdict at all: at six inputs or fewer the density blind spot is
    *conservative*, with best-bad plateauing in the 0.50-0.65 band, so a
    hypercube and a cluster both read ``thin`` and nothing overclaims.

    What it gates is any claim about the **arrangement** of the runs, which
    at low density is not supportable in either direction."""

    min_discriminating_runs_per_input: int = 0
    """The threshold behind :attr:`density_discriminating`, carried so a
    surface can quote it without re-reading config."""

    min_discriminating_pair_runs: int = 0
    """Runs on a pair below which it is shown but does not set the verdict.

    Carried on the report for the same reason as
    :attr:`min_points_per_pair`: the panel has to say *why* a row is not
    counting, and re-reading config in the UI is a second copy of a number
    that can drift from the one the engine applied."""
    """Runs needed on both axes before a pair is judged at all (§4.6).

    Recorded so a surface can explain an **empty** sweep. Zero pairs scored and
    every pair healthy look identical otherwise, which is the confusion this
    whole feature exists to prevent: the reader needs to know the difference
    between "nothing to report" and "not enough runs to tell", and how many
    more runs would change that."""
    warnings: tuple[CoverageWarning, ...] = ()
    diagnostics: CoverageDiagnostics = field(default_factory=CoverageDiagnostics)
    maximin_distance: float | None = None
    discrepancy: float | None = None
    nn_distances: tuple[float, ...] = ()
    thresholds: dict[str, float] = field(default_factory=dict)
    """The verdict floors this pass was measured against.

    Config-tunable (§16), so a bare "poor" means nothing a year later without
    them. Recorded here rather than passed to :func:`build_report` separately:
    one source means a stored report cannot claim floors the verdict was not
    actually reached under."""

    def to_json_obj(self) -> dict[str, Any]:
        """A JSON-safe view of the design pass.

        ``nn_distances`` is deliberately dropped: it drives a histogram of the
        *live* point set, and a stored copy of one float per training run
        would dominate the row while going stale the moment the cohort grows.
        """
        return {
            "criterion": self.criterion,
            "verdict": self.verdict,
            "message": self.message,
            "covering_radius": self.covering_radius,
            "coverage_score": self.coverage_score,
            "reference_radius": self.reference_radius,
            "efficiency_score": self.efficiency_score,
            "discriminating": self.discriminating,
            "n_candidates": self.n_candidates,
            "per_factor": [f.to_json_obj() for f in self.per_factor],
            "per_pair": [p.to_json_obj() for p in self.per_pair],
            "n_pairs_examined": self.n_pairs_examined,
            "n_pairs_total": self.n_pairs_total,
            "min_points_per_pair": self.min_points_per_pair,
            "min_discriminating_pair_runs": self.min_discriminating_pair_runs,
            "density_discriminating": self.density_discriminating,
            "sampling_modes": list(self.sampling_modes),
            "min_discriminating_runs_per_input": self.min_discriminating_runs_per_input,
            "warnings": [w.to_json_obj() for w in self.warnings],
            "diagnostics": self.diagnostics.to_json_obj(),
            "maximin_distance": self.maximin_distance,
            "discrepancy": self.discrepancy,
            "thresholds": dict(self.thresholds),
        }

    @classmethod
    def from_json_obj(cls, raw: Mapping[str, Any]) -> DesignCoverage:
        """Rebuild the design pass from its stored form."""
        return cls(
            criterion=raw.get("criterion", "model_free"),
            verdict=_as_verdict(raw.get("verdict")),
            message=str(raw.get("message") or ""),
            covering_radius=float(raw.get("covering_radius") or 0.0),
            coverage_score=float(raw.get("coverage_score") or 0.0),
            reference_radius=float(raw.get("reference_radius") or 0.0),
            efficiency_score=float(raw.get("efficiency_score") or 0.0),
            discriminating=bool(raw.get("discriminating", True)),
            n_candidates=int(raw.get("n_candidates") or 0),
            per_factor=tuple(
                FactorCoverage.from_json_obj(f) for f in (raw.get("per_factor") or [])
            ),
            per_pair=tuple(PairCoverage.from_json_obj(p) for p in (raw.get("per_pair") or [])),
            n_pairs_examined=int(raw.get("n_pairs_examined") or 0),
            n_pairs_total=int(raw.get("n_pairs_total") or 0),
            min_points_per_pair=int(raw.get("min_points_per_pair") or 0),
            min_discriminating_pair_runs=int(raw.get("min_discriminating_pair_runs") or 0),
            density_discriminating=bool(raw.get("density_discriminating", True)),
            sampling_modes=tuple(str(m) for m in (raw.get("sampling_modes") or ())),
            min_discriminating_runs_per_input=int(
                raw.get("min_discriminating_runs_per_input") or 0
            ),
            warnings=tuple(CoverageWarning.from_json_obj(w) for w in (raw.get("warnings") or [])),
            diagnostics=CoverageDiagnostics.from_json_obj(raw.get("diagnostics") or {}),
            maximin_distance=_opt_float(raw.get("maximin_distance")),
            discrepancy=_opt_float(raw.get("discrepancy")),
            thresholds={str(k): float(v) for k, v in (raw.get("thresholds") or {}).items()},
        )


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """The whole assessment, as persisted and as rendered (§7 / §9)."""

    verdict: Verdict
    design: DesignCoverage
    envelope: Envelope
    source_kind: str
    computed_at: dt.datetime
    per_response: tuple[Any, ...] = ()
    warnings: tuple[CoverageWarning, ...] = ()
    design_points: tuple[Mapping[str, Any], ...] = ()
    """The training points themselves (§11.4).

    The pairwise scatter matrix is the visual payoff of the whole panel —
    existing points per factor pair, empty regions shaded — and it cannot be
    reconstructed from a verdict and a radius. Carried here so one call
    answers the page.

    **Not stored** (§9): the points belong to the cohort, which the version
    already references. A copy on every version would bloat the row and go
    stale the moment the cohort grows."""

    def to_json_obj(self) -> dict[str, Any]:
        """The stored form (ADR-0020 §9) — a few kB, JSON-safe throughout."""
        return {
            "schema": 1,
            "verdict": self.verdict,
            "source_kind": self.source_kind,
            "computed_at": self.computed_at.isoformat(),
            "design": self.design.to_json_obj(),
            "envelope": self.envelope.to_json_obj(),
            "per_response": [_response_json(r) for r in self.per_response],
            "warnings": [w.to_json_obj() for w in self.warnings],
            "thresholds": dict(self.design.thresholds),
        }

    @classmethod
    def from_json_obj(cls, raw: Mapping[str, Any]) -> CoverageReport:
        """Rebuild a stored report.

        Unknown keys are ignored and missing optional sections default, so a
        report written by a later release stays readable by an earlier one —
        the row outlives the release that wrote it.
        """
        import datetime as _dt  # noqa: PLC0415

        stamp = raw.get("computed_at")
        computed_at = (
            _dt.datetime.fromisoformat(str(stamp))
            if stamp
            else _dt.datetime.fromtimestamp(0, tz=_dt.UTC)
        )
        return cls(
            verdict=_as_verdict(raw.get("verdict")),
            design=DesignCoverage.from_json_obj(raw.get("design") or {}),
            envelope=Envelope.from_json_obj(raw.get("envelope") or []),
            source_kind=str(raw.get("source_kind") or "cohort"),
            computed_at=computed_at,
            per_response=tuple(raw.get("per_response") or ()),
            warnings=tuple(CoverageWarning.from_json_obj(w) for w in (raw.get("warnings") or [])),
        )


# --- the design pass ------------------------------------------------------


def analyse_design(
    points: PointSet,
    envelope: Envelope,
    *,
    settings: CoverageSettings | None = None,
    sampling_modes: tuple[str, ...] = (),
    pairs_may_vote: bool | None = None,
) -> DesignCoverage:
    """Measure the design's geometry — no GP, no response (§2.1).

    This is what cohort preview runs, and it is meaningful precisely because
    it makes no claim about any particular output. It also resolves the
    sequencing problem: asking "is this fit to learn from?" before a response
    exists cannot require a fitted model, or deciding whether to fit would
    require fitting.

    Thresholds arrive in ``settings``; ``None`` resolves to the config model's
    own defaults, so nothing here hardcodes a number
    (``feedback_no_hardcoded_defaults_outside_config``).
    """
    settings = settings if settings is not None else CoverageSettings()
    diagnostics = diagnose(points, envelope, settings=settings)

    degenerate = _degenerate_message(points, envelope, settings=settings)
    if degenerate is not None:
        return DesignCoverage(
            criterion="model_free",
            verdict="insufficient",
            message=degenerate,
            covering_radius=0.0,
            coverage_score=0.0,
            per_factor=_per_factor(points, envelope, radius=None, settings=settings),
            min_points_per_pair=settings.min_points_per_pair,
            min_discriminating_pair_runs=settings.min_discriminating_pair_runs,
            density_discriminating=False,
            min_discriminating_runs_per_input=settings.min_discriminating_runs_per_input,
            warnings=_warnings(points, envelope, diagnostics, per_pair=(), settings=settings),
            diagnostics=diagnostics,
            thresholds=dict(settings.verdict_thresholds),
        )

    x = envelope.normalise(points)
    radius = covering_radius(x, envelope, n_candidates=settings.n_candidates, seed=_CANDIDATE_SEED)
    # **The yardstick must be built for the points the radius was measured
    # over, not for every row the cohort holds.** ``covering_radius`` drops
    # rows it cannot place (a missing coordinate) and rows outside the
    # declared envelope; passing ``points.n_points`` then asked "how well
    # would 531 evenly-spread runs do?" of a radius achieved by 291 — a
    # yardstick for a design nearly twice the size, so the score came out
    # pessimistic in direct proportion to how much data was missing. On the
    # demo heat-sink archive that alone moved 0.73 -> 0.68 and flipped the
    # verdict from `good` to `thin`, while every per-factor row stayed green:
    # the user-visible contradiction that found this. Same disease as §3's
    # F2 — data disappearing quietly — one level up.
    n_measured = measured_rows(x, envelope)
    n_axes = len(envelope.metric_axes)

    # **The bar is fixed to a standard, not to this archive's own size**
    # (§4.4). Scored against itself the number is an efficiency, and an
    # efficiency legitimately falls when runs are added — which produced a
    # plan table reading `good` at 60 suggested runs and `poor` at 120 while
    # the gap it was measuring had genuinely shrunk. Pinned to
    # `standard_runs_per_input` the bar depends only on the dimension, so the
    # score moves only when the radius does, and a covering radius never
    # rises as points are added. Monotone by arithmetic.
    ideal = reference_radius(
        envelope,
        settings.standard_runs_per_input * n_axes,
        n_candidates=settings.n_candidates,
        seed=_CANDIDATE_SEED,
        replicates=settings.reference_replicates,
    )
    score = _score(radius, n_axes, ideal)

    # The old number, kept as a labelled diagnostic rather than a verdict.
    # Its reference moves with the archive's size, so it would miss the cache
    # on every call — and a suggestion plan asks for six analyses whose point
    # counts differ only by the batch. The count is bucketed to 2% so those
    # share one layout: the reference is smooth in `n`, the shift is far below
    # the sampling noise the median is already smoothing out, and it is a
    # diagnostic rather than a verdict either way.
    efficiency = _score(
        radius,
        n_axes,
        reference_radius(
            envelope,
            _bucket(n_measured),
            n_candidates=settings.n_candidates,
            seed=_CANDIDATE_SEED,
            replicates=settings.reference_replicates,
        ),
    )
    # Above the measured boundary the joint radius cannot tell a cluster from
    # a hypercube — and errs *flattering*, scoring a cluster 0.79. Reported,
    # never the light.
    discriminating = n_axes <= settings.max_discriminating_axes
    # **Width and density are different blind spots.** The one above is
    # flattering and stands the joint score down; this one is conservative
    # and changes no verdict — it only gates claims about *arrangement*.
    density_discriminating = _density_discriminating(n_measured, n_axes, settings=settings)
    per_factor = _per_factor(points, envelope, radius=radius, settings=settings, normalised=x)

    # **The level between one input and all of them** (§4.6). Neither
    # neighbour can see a one-factor-at-a-time archive: the per-factor rows
    # are blind to structure by construction, and in six dimensions 291 runs
    # cannot cover anything, so the joint score flatters a cross as readily as
    # a Latin hypercube. In two dimensions the same runs are plentiful and the
    # cross is obvious. Only a *poor* pair counts towards the verdict --
    # see :func:`pair_verdict` for why a thin one must not.
    movable = {f.name: f.value for f in per_factor if f.actionable}
    per_pair = analyse_pairs(
        x, envelope, settings=settings, per_factor_scores=movable, points=points
    )
    # Choices are pairable now (§4.6), so the denominator has to count them
    # or the "N of M pairs examined" note under-reports what a full sweep
    # would be and a capped sweep looks complete.
    n_pairable = len(movable) + len(
        [f for f in envelope.categorical_axes if envelope.by_name(f).actionable]
    )
    n_pairs_total = n_pairable * (n_pairable - 1) // 2

    # **Two things can stand down, and if both do, nothing left can see
    # structure.** The joint score abstains above six inputs; the pairwise
    # pass now abstains below the run count where its readings order a good
    # design against a bad one at all. What remains is the per-factor strip,
    # which is marginal and passes a one-factor-at-a-time cross every time —
    # so a green badge there would be the flattering answer on exactly the
    # cohort least able to justify one. "We cannot tell" is the honest
    # verdict, and it is already a first-class one.
    # **Whether the pairs vote is fixed by the caller, or read from the data.**
    # A plan projects the same archive at several sizes, and a term that is
    # ignored at one rung and counted at the next can only ever make the
    # verdict worse — which broke the ladder for a second time: untick an
    # input and it read poor, poor, thin, poor, poor, because at 60 total runs
    # the pairs crossed the floor and started voting while still below the
    # poor line. Every underlying measure is monotone; the *gate* was not.
    # `suggestion_plan` therefore settles the entitlement once, on the archive
    # as it stands, and applies it to every rung — the same principle §4.4
    # applies to the bar, which is pinned rather than moving with the archive.
    judged_pairs = (
        discriminating_pairs(per_pair, settings=settings) if pairs_may_vote is not False else ()
    )
    structural = discriminating or bool(judged_pairs)
    verdict = (
        _worst(
            ([_verdict(score, settings)] if discriminating else [])
            + [f.verdict for f in per_factor if f.actionable]
            + [pair_verdict(judged_pairs, settings=None)]
        )
        if structural
        else "insufficient"
    )

    return DesignCoverage(
        criterion="model_free",
        verdict=verdict,
        message=_headline(verdict, radius, per_factor, per_pair, settings=settings),
        covering_radius=radius,
        coverage_score=score,
        efficiency_score=efficiency,
        discriminating=discriminating,
        reference_radius=ideal,
        n_candidates=settings.n_candidates,
        per_factor=per_factor,
        per_pair=per_pair,
        min_points_per_pair=settings.min_points_per_pair,
        min_discriminating_pair_runs=settings.min_discriminating_pair_runs,
        density_discriminating=density_discriminating,
        sampling_modes=sampling_modes,
        min_discriminating_runs_per_input=settings.min_discriminating_runs_per_input,
        n_pairs_examined=len(per_pair),
        n_pairs_total=n_pairs_total,
        warnings=_warnings(
            points,
            envelope,
            diagnostics,
            per_pair=per_pair,
            n_measured=n_measured,
            verdict=verdict,
            settings=settings,
        ),
        diagnostics=diagnostics,
        maximin_distance=maximin_distance(x),
        discrepancy=centered_l2_discrepancy(x),
        nn_distances=nn_distance_distribution(x),
        thresholds=dict(settings.verdict_thresholds),
    )


def build_report(
    design: DesignCoverage,
    *,
    envelope: Envelope,
    source_kind: str,
    computed_at: dt.datetime,
    per_response: tuple[Any, ...] = (),
    points: PointSet | None = None,
) -> CoverageReport:
    """Assemble the persisted report, headlining the **worst** verdict.

    A model is only as trustworthy as its least-covered response, so the
    headline is the worst of the design pass and every response — never an
    average, which would let one well-covered output mask another.
    """
    verdict = _worst([design.verdict] + [r.verdict for r in per_response])
    warnings = list(design.warnings)
    framing = _source_framing(source_kind, verdict)
    if framing is not None:
        warnings.append(framing)
    return CoverageReport(
        verdict=verdict,
        design=design,
        envelope=envelope,
        source_kind=source_kind,
        computed_at=computed_at,
        per_response=per_response,
        warnings=tuple(warnings),
        design_points=tuple(points.rows) if points is not None else (),
    )


# --- verdict machinery ----------------------------------------------------


def edge_threshold_from(
    stored: Mapping[str, Any] | None, *, response_name: str | None = None
) -> float | None:
    """The stored ``edge`` boundary for a response, from a stored report (§8).

    **Read, never recomputed.** Recomputing would need the training set, which
    a version fitted with ``keep_training_data=False`` does not have.

    ``response_name`` picks that response's own threshold; without it the
    first response carrying one stands for the point, which is what an
    exported tool does (the factor space is shared, so distance-to-data is the
    same for all of them). Falls back to the first available threshold if the
    named response has none, because a threshold from a sibling response is a
    better answer than refusing to distinguish ``edge`` from ``inside`` at
    all.

    ``None`` means this fit cannot produce ``edge`` — an honest answer, not a
    degraded one: the envelope half of the guard needs no model and catches
    the genuinely dangerous case.
    """
    if not isinstance(stored, Mapping):
        return None
    responses = stored.get("per_response")
    if not isinstance(responses, list | tuple):
        return None
    fallback: float | None = None
    for response in responses:
        if not isinstance(response, Mapping):
            continue
        value = response.get("edge_sigma_threshold")
        if value is None:
            continue
        try:
            threshold = float(value)
        except (TypeError, ValueError):
            continue
        if response_name is not None and response.get("response_name") == response_name:
            return threshold
        if fallback is None:
            fallback = threshold
    return fallback


def _response_json(response: Any) -> dict[str, Any]:
    """One :class:`ResponseCoverage` as JSON (slice 2's per-response verdict).

    Kept deliberately flat and read back as plain dicts: the guard (§8) needs
    the method, the criterion and ``edge_sigma_threshold``, and nothing that
    reads a stored report needs to reconstitute a live ``ResponseModel``.
    """
    return {
        "response_name": getattr(response, "response_name", ""),
        "response_kind": getattr(response, "response_kind", "scalar"),
        "surrogate_method": getattr(response, "surrogate_method", ""),
        "criterion": getattr(response, "criterion", "model_free"),
        "verdict": getattr(response, "verdict", "insufficient"),
        "message": getattr(response, "message", ""),
        "coverage_score": getattr(response, "coverage_score", 0.0),
        "covering_radius": getattr(response, "covering_radius", 0.0),
        "edge_sigma_threshold": getattr(response, "edge_sigma_threshold", None),
        "available_statuses": list(getattr(response, "available_statuses", ())),
        "ard_relevance": getattr(response, "ard_relevance", None),
        "suggested_points": [dict(p) for p in getattr(response, "suggested_points", ())],
    }


def _opt_float(value: Any) -> float | None:
    """``float(value)`` or ``None`` — a stored ``null`` means "not computed"."""
    return None if value is None else float(value)


def _degenerate_message(
    points: PointSet, envelope: Envelope, *, settings: CoverageSettings
) -> str | None:
    """Why this point set cannot be judged, or ``None`` if it can (§1.4).

    ``insufficient`` is a first-class verdict, distinct from ``poor``.
    Conflating "we can tell, and it is bad" with "we cannot tell" would be its
    own species of false confidence.
    """
    if not points.factor_names:
        return (
            "These runs are all the same design point — nothing was varied, so "
            "there's no design space to measure coverage over."
        )
    if points.n_points < settings.min_points_for_analysis:
        return (
            f"Too few runs to judge coverage — at least "
            f"{settings.min_points_for_analysis} are needed, and this set has "
            f"{points.n_points}."
        )
    if not envelope.metric_axes and not envelope.categorical_axes:
        return (
            "None of these inputs can be measured for coverage — they're all held "
            "constant or come from the solver rather than the design."
        )
    return None


def _density_discriminating(n_points: int, n_axes: int, *, settings: CoverageSettings) -> bool:
    """Whether there are enough runs per input to judge the *arrangement*.

    See :attr:`CoverageSettings.min_discriminating_runs_per_input`. Below the
    threshold a clean Latin hypercube and a tight cluster of the same size
    score within noise of each other, so any statement about how well the
    runs are arranged is unsupportable in either direction.
    """
    if n_axes <= 0:
        return False
    return n_points >= settings.min_discriminating_runs_per_input * n_axes


def _score(radius: float, n_axes: int, reference: float | None = None) -> float:
    """``covering_radius`` as a 0..1 score, higher is better.

    Referenced against what an evenly-spread design of the **same size** would
    achieve (:func:`reference_radius`), so the score answers "is this as well
    spread as this many runs could be?" — a question whose answer depends on
    the design rather than on how many factors it happens to have.

    ``reference=None`` falls back to the hypercube half-diagonal for callers
    with no point count to hand; that fallback is dimension-*naive* and is
    only used where no better yardstick exists.
    """
    if n_axes <= 0:
        return 1.0
    yardstick = reference if reference is not None else (n_axes**0.5) / 2.0
    if yardstick <= 0.0:
        return 1.0
    if radius <= 0.0:
        return 1.0
    return float(max(0.0, min(1.0, yardstick / radius)))


def _bucket(n: int, *, granularity: float = 0.02) -> int:
    """``n`` rounded to the nearest 2%, so near-identical counts share a cache.

    Never below 1: a bucket of zero would ask for a reference over no points
    and get 0.0, which divides into a score of 1.0 — a perfect verdict from an
    empty design.
    """
    if n <= 0:
        return 0
    step = max(1, int(n * granularity))
    return max(1, round(n / step) * step)


def _verdict(score: float, settings: CoverageSettings) -> Verdict:
    """Score → traffic light, using the configured floors."""
    if score >= settings.verdict_thresholds["good"]:
        return "good"
    return "thin" if score >= settings.verdict_thresholds["thin"] else "poor"


def score_for(radius: float, n_axes: int, reference: float | None = None) -> float:
    """Public alias for the score rule -- see :func:`_score`.

    Exported so the suggestion planner scores a *projected* design with the
    identical rule the report scores the real one. A planner carrying its own
    copy of the arithmetic is the drift §3's F1 is about: it would happily
    promise a verdict the report then disagrees with.
    """
    return _score(radius, n_axes, reference)


def verdict_for(score: float, settings: CoverageSettings) -> Verdict:
    """Public alias for the verdict rule -- see :func:`_verdict`."""
    return _verdict(score, settings)


def _worst(verdicts: list[Verdict]) -> Verdict:
    """The least reassuring of several verdicts."""
    return max(verdicts, key=lambda v: _VERDICT_ORDER[v]) if verdicts else "insufficient"


# --- the per-factor strip -------------------------------------------------


def _per_factor(
    points: PointSet,
    envelope: Envelope,
    *,
    radius: float | None,
    settings: CoverageSettings,
    normalised: np.ndarray | None = None,
) -> tuple[FactorCoverage, ...]:
    """One row per axis, each with the measure that suits its kind.

    ``normalised`` is the matrix the joint pass has already built. Without it
    every continuous row re-normalised the **whole** archive to read one
    column -- six factors over 531 runs meant six full passes for one matrix's
    worth of information, and it was the largest single cost in the report
    ([[feedback_performance_and_security_equal]]).
    """
    x = envelope.normalise(points) if normalised is None else normalised
    return tuple(
        _factor_row(points, envelope, factor, radius=radius, settings=settings, normalised=x)
        for factor in envelope.factors
    )


def _factor_row(
    points: PointSet,
    envelope: Envelope,
    factor: FactorEnvelope,
    *,
    radius: float | None,
    settings: CoverageSettings,
    normalised: np.ndarray,
) -> FactorCoverage:
    """One row of the strip."""
    if factor.kind == "pinned":
        # A pinned axis has no coverage of its own — but it may still have
        # explored none of the range the tool allows, which is the only
        # reading that says anything useful about it.
        pinned_declared, pinned_verdict = _declared_coverage(points, factor, settings=settings)
        value = _as_number(factor.pinned_value)
        return FactorCoverage(
            name=factor.name,
            label=factor.label,
            kind=factor.kind,
            measure="pinned",
            scale=factor.scale,
            value=1.0,
            verdict="good",
            actionable=False,
            simulated_lo=value,
            simulated_hi=value,
            declared_lo=factor.declared_lo,
            declared_hi=factor.declared_hi,
            declared_value=pinned_declared,
            declared_verdict=pinned_verdict,
        )
    if factor.kind == "derived":
        span, _ = trained_span(points, factor)
        return FactorCoverage(
            name=factor.name,
            label=factor.label,
            kind=factor.kind,
            measure="span",
            scale=factor.scale,
            value=span,
            verdict=_verdict(span, settings),
            actionable=False,
        )
    if factor.kind == "categorical":
        return _categorical_row(points, envelope, factor, settings=settings)
    if factor.kind == "integer" and _uses_occupancy(factor, settings):
        occupancy, missing = lattice_occupancy(points, factor)
        lattice_lo, lattice_hi = _observed_span(points, factor.name)
        lattice_declared, lattice_verdict = _declared_coverage(points, factor, settings=settings)
        return FactorCoverage(
            name=factor.name,
            label=factor.label,
            kind=factor.kind,
            measure="lattice_occupancy",
            scale=factor.scale,
            value=occupancy,
            verdict=_verdict(occupancy, settings),
            missing_values=missing,
            simulated_lo=lattice_lo,
            simulated_hi=lattice_hi,
            declared_lo=factor.declared_lo,
            declared_hi=factor.declared_hi,
            declared_value=lattice_declared,
            declared_verdict=lattice_verdict,
        )
    return _distance_row(
        points, envelope, factor, radius=radius, settings=settings, normalised=normalised
    )


def _as_number(value: Any) -> float | None:
    """``float(value)`` for a real number, else ``None`` (a pinned string)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _uses_occupancy(factor: FactorEnvelope, settings: CoverageSettings) -> bool:
    """Whether an integer axis is small enough to measure by occupancy (§3.3).

    At or below the configured cardinality the lattice is the domain; above it
    — a mesh seed over ``[1,1000]`` — it is dense enough to behave
    continuously and distance metrics are the honest measure.
    """
    lattice = factor.lattice()
    return bool(lattice) and len(lattice or ()) <= settings.max_lattice_cardinality


def _categorical_row(
    points: PointSet,
    envelope: Envelope,
    factor: FactorEnvelope,
    *,
    settings: CoverageSettings,
) -> FactorCoverage:
    """Level balance: the share held by the *least* populated level."""
    levels = factor.levels or ()
    counts = dict.fromkeys(levels, 0)
    # A value that is not one of this axis's levels is **not a new level**.
    # ``counts.get(key, 0) + 1`` invented one, which did two things at once:
    # it inflated ``len(counts)`` so the even share shrank, and it entered the
    # ``min()`` below. A missing value therefore became a phantom material
    # whose fixed count dragged the balance *down* as real runs were added.
    #
    # Dropped instead, exactly as ``_unit`` drops a row with no numeric
    # coordinate: a run that cannot be placed on this axis is not evidence
    # about how its levels are balanced.
    unplaceable = 0
    for value in points.values(factor.name):
        key = level_label(value)
        if key in counts:
            counts[key] += 1
        else:
            unplaceable += 1
    if not counts:
        return FactorCoverage(
            name=factor.name,
            label=factor.label,
            kind=factor.kind,
            measure="level_balance",
            scale=factor.scale,
            value=0.0,
            verdict="insufficient",
        )
    total = sum(counts.values()) or 1
    even_share = 1.0 / len(counts)
    balance = (min(counts.values()) / total) / even_share
    thin = [level for level, count in counts.items() if count < settings.min_level_points]
    if unplaceable:
        # Not folded into the balance — it is a different fact. Runs that
        # carry no value for this axis say nothing about how its levels are
        # shared out, but a reader is entitled to know they were set aside.
        thin = [*thin, f"({unplaceable} run(s) with no value recorded)"]
    verdict: Verdict = _verdict(min(balance, 1.0), settings)
    if thin:
        verdict = _worst([verdict, "thin"])
    _ = envelope  # levels come from the factor; the envelope is context only
    return FactorCoverage(
        name=factor.name,
        label=factor.label,
        kind=factor.kind,
        measure="level_balance",
        scale=factor.scale,
        value=min(balance, 1.0),
        verdict=verdict,
        missing_values=tuple(sorted(thin)),
    )


def _distance_row(
    points: PointSet,
    envelope: Envelope,
    factor: FactorEnvelope,
    *,
    radius: float | None,
    settings: CoverageSettings,
    normalised: np.ndarray,
) -> FactorCoverage:
    """A continuous (or dense-integer) axis, measured by its own 1-D gap."""
    lo, hi = _observed_span(points, factor.name)
    declared_value, declared_verdict = _declared_coverage(points, factor, settings=settings)

    if radius is None:
        score: float = 0.0
        verdict: Verdict = "insufficient"
    else:
        column = envelope.column_index(factor.name)
        values = sorted(v for v in normalised[:, column].tolist() if 0.0 <= v <= 1.0)
        score = max(0.0, 1.0 - _largest_gap(values) * 2.0)
        verdict = _verdict(score, settings)

    return FactorCoverage(
        name=factor.name,
        label=factor.label,
        kind=factor.kind,
        measure="covering_radius",
        scale=factor.scale,
        value=score,
        verdict=verdict,
        simulated_lo=lo,
        simulated_hi=hi,
        declared_lo=factor.declared_lo,
        declared_hi=factor.declared_hi,
        declared_value=declared_value,
        declared_verdict=declared_verdict,
    )


def _observed_span(points: PointSet, name: str) -> tuple[float | None, float | None]:
    """The lowest and highest values actually run for ``name``."""
    numbers = [
        float(v)
        for v in points.values(name)
        if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(float(v))
    ]
    return (min(numbers), max(numbers)) if numbers else (None, None)


def _declared_coverage(
    points: PointSet, factor: FactorEnvelope, *, settings: CoverageSettings
) -> tuple[float, Verdict]:
    """How much of the **tool's own** allowed range these runs explored (§4.5).

    The same largest-gap measure as the main reading, but normalised over the
    descriptor's bounds instead of the observed spread. That makes it answer
    a question no other figure in the report could: an input pinned at 60 out
    of an allowed 10-120 reads 0 here and "the same in every run" everywhere
    else, and only one of those tells a user they have explored none of what
    the tool accepts.

    Values **outside** the declared bounds are kept and clamped rather than
    dropped: a run at 406 against a declared maximum of 400 still demonstrates
    the top of the range, and discarding it would report *less* coverage
    because of a run that proves *more*. It is the descriptor that is out of
    date there, and §11 flags it separately.
    """
    lo, hi = factor.declared_lo, factor.declared_hi
    if lo is None or hi is None or hi <= lo:
        return 0.0, "insufficient"
    numbers = [
        float(v)
        for v in points.values(factor.name)
        if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(float(v))
    ]
    if not numbers:
        return 0.0, "poor"
    scaled = sorted(min(1.0, max(0.0, (v - lo) / (hi - lo))) for v in numbers)
    score = max(0.0, 1.0 - _largest_gap(scaled) * 2.0)
    return score, _verdict(score, settings)


def largest_gap(values: list[float]) -> float:
    """Public alias for the 1-D gap measure -- see :func:`_largest_gap`.

    Exported because §4.6's within-option coverage measures one numeric axis
    inside one level of a choice, which is the same question the per-factor
    row asks; a second implementation of "how big is the biggest gap" is the
    drift §3's F1 is about.
    """
    return _largest_gap(values)


def _largest_gap(values: list[float]) -> float:
    """Biggest distance from anywhere in ``[0,1]`` to the nearest value."""
    if not values:
        return 1.0
    gaps = [values[0], 1.0 - values[-1]]
    gaps += [(b - a) / 2.0 for a, b in itertools.pairwise(values)]
    return max(gaps)


# --- warnings -------------------------------------------------------------


def _warnings(
    points: PointSet,
    envelope: Envelope,
    diagnostics: CoverageDiagnostics,
    *,
    per_pair: tuple[PairCoverage, ...] = (),
    n_measured: int | None = None,
    verdict: Verdict | None = None,
    settings: CoverageSettings,
) -> tuple[CoverageWarning, ...]:
    """Everything the user should be told, in their own language.

    ``n_measured`` is the count the radius was actually taken over, handed in
    rather than recomputed: it is one normalisation of the whole archive, and
    a second copy of "which rows count" is the drift §3's F1 is about. ``None``
    on the degenerate path, where nothing was measured and the density caveat
    would have nothing to say.
    """
    out: list[CoverageWarning] = []
    out.extend(_pair_warnings(per_pair, settings=settings))

    if diagnostics.confounded:
        out.append(
            CoverageWarning(
                code="confounding",
                message=(
                    "Two or more of your inputs move together across these runs, so a "
                    "model can't tell their effects apart. Coverage alone won't fix "
                    "this — it needs runs that break the pattern, varying one input "
                    "while holding the other."
                ),
            )
        )

    for name, level, count in diagnostics.underpopulated_levels:
        shown = envelope.by_name(name).display_name
        out.append(
            CoverageWarning(
                code="categorical_imbalance",
                factor=name,
                factor_label=shown,
                message=(
                    f"Only {count} run{'' if count == 1 else 's'} used "
                    f"'{level}' for {shown}. A model built from this will still give "
                    f"confident answers for '{level}', but it has barely seen it — "
                    f"aim for at least {settings.min_level_points}."
                ),
            )
        )

    n_axes = len(envelope.metric_axes)
    if n_axes > settings.max_discriminating_axes:
        out.append(
            CoverageWarning(
                code="joint_not_discriminating",
                message=(
                    f"With {n_axes} inputs varying, no realistic number of runs can "
                    f"fill the whole space, so the overall figure can't tell a "
                    f"well-designed set from a badly-designed one — measured, it "
                    f"rates a tight cluster about as highly as an even spread. The "
                    f"verdict here comes from the per-input and per-pair readings "
                    f"instead, which stay meaningful however many inputs there are."
                ),
            )
        )

    # The **density** caveat, which is a qualification and not a stand-down.
    # Deliberately says what is and is not at risk: below this many runs per
    # input the figure cannot separate a good design from a bad one, but it
    # never reads *better* for a bad one either, so nothing is being hidden.
    # **Not once the figure has already cleared the bar.** The density blind
    # spot is that a good design cannot be *praised* at low density — a
    # hypercube and a cluster both read thin. It cannot make a bad design read
    # good: best-bad plateaus in the 0.50-0.65 band at six inputs or fewer. So
    # a `good` verdict is trustworthy however few runs earned it, and warning
    # there would undermine a reading that is sound.
    if (
        verdict != "good"
        and n_measured is not None
        and n_axes
        and envelope.metric_axes
        and n_axes <= settings.max_discriminating_axes
        and not _density_discriminating(n_measured, n_axes, settings=settings)
    ):
        want = settings.min_discriminating_runs_per_input * n_axes
        out.append(
            CoverageWarning(
                code="low_density",
                message=(
                    f"With {n_measured} runs across {n_axes} varying inputs, the overall "
                    f"figure is mostly reporting how few runs there are. It would take "
                    f"around {want} before it could tell a well-arranged set from a badly "
                    f"arranged one — below that a clustered set would read about the same. "
                    f"It never reads better than this for a bad design, so nothing is "
                    f"being hidden; it just cannot praise a good one yet."
                ),
            )
        )

    resolution = candidate_resolution(settings.n_candidates, n_axes)
    if resolution < settings.min_candidate_resolution and envelope.metric_axes:
        out.append(
            CoverageWarning(
                code="coarse_estimate",
                message=(
                    f"With {len(envelope.metric_axes)} inputs varying, the overall "
                    f"figure is a rough estimate — there are too many combinations to "
                    f"check them all, so treat it as approximate rather than exact. "
                    f"It still answers a different question from the per-input rows "
                    f"below: those measure each input's own range, this one measures "
                    f"the combinations."
                ),
            )
        )

    out.extend(_axis_warnings(points, envelope))

    descriptor_labels = {
        f.name: f.label for f in (points.descriptor.inputs if points.descriptor else ())
    }
    for name in points.unusable_names:
        # Neither a factor nor a constant: there was nothing in the column to
        # measure or to pin, so the input has no row anywhere in the report.
        # Saying nothing would leave a user hunting a row that is not there,
        # and this is exactly the input a model will later be asked to
        # extrapolate along.
        out.append(
            CoverageWarning(
                code="unusable_input",
                message=(
                    f"'{descriptor_labels.get(name) or name}' has no value recorded "
                    f"in any of these runs, so it isn't part of this assessment and "
                    f"a model built from them can't account for it."
                ),
                factor=name,
                factor_label=descriptor_labels.get(name) or name,
            )
        )

    if diagnostics.n_excluded_invalid or diagnostics.n_excluded_filtered:
        out.append(
            CoverageWarning(
                code="excluded_runs",
                message=_exclusion_message(diagnostics),
            )
        )
    if diagnostics.n_outside_envelope:
        out.append(
            CoverageWarning(
                code="outside_envelope",
                message=(
                    f"{diagnostics.n_outside_envelope} of these runs sit outside the "
                    "range you've said the model should be trusted over. They still "
                    "help train it, but they aren't counted towards coverage of that "
                    "range."
                ),
            )
        )
    if diagnostics.n_near_duplicates:
        out.append(
            CoverageWarning(
                code="near_duplicates",
                message=(
                    f"{diagnostics.n_near_duplicates} of these runs repeat a design "
                    "point that's already in the set, so they add no new information."
                ),
            )
        )
    return tuple(out)


def _pair_warnings(
    per_pair: tuple[PairCoverage, ...], *, settings: CoverageSettings
) -> list[CoverageWarning]:
    """Name the input pairs that were never really varied together (§4.6).

    **The warning that was missing entirely.** A user looking at the pairwise
    plots could see a cross and read a ``good`` verdict beside it, with
    nothing in the panel acknowledging the empty space. The plot was the only
    place the problem appeared, which makes it a finding the panel left to
    chance.

    One sentence for the whole archive when *every* pair fails -- a dozen
    near-identical lines differing only in two names is a wall of text nobody
    reads, and the remedy is the same one for all of them.
    """
    bad = [p for p in per_pair if p.verdict == "poor"]
    if not bad:
        return []

    if one_at_a_time(per_pair, floor=settings.one_at_a_time_floor, settings=settings):
        return [
            CoverageWarning(
                code="one_at_a_time",
                message=(
                    "These runs look like they were made one input at a time: every "
                    "pair of inputs leaves most of its combinations untried, even "
                    "though each input on its own covers its range. A model built "
                    "from them can predict along the lines that were run and will be "
                    "guessing everywhere between. Covering it needs a designed set "
                    "that varies several inputs together — a Latin hypercube study "
                    "over the ranges you care about is the usual answer."
                ),
            )
        ]

    listed = ", ".join(p.pair_label for p in bad[:3])
    more = len(bad) - 3
    tail = f", and {more} other pair{'' if more == 1 else 's'}" if more > 0 else ""
    return [
        CoverageWarning(
            code="pair_uncovered",
            factor=bad[0].a,
            factor_label=bad[0].a_label,
            message=(
                f"Some inputs were rarely varied together: {listed}{tail}. Each of "
                f"those inputs covers its own range, but most combinations of them "
                f"have never been run, so a model can't tell how they interact. "
                f"Runs that change several inputs at once are what fill this — a "
                f"wider range on any one of them will not."
            ),
        )
    ]


def _axis_warnings(points: PointSet, envelope: Envelope) -> list[CoverageWarning]:
    """Remedies for the axes no design point can fix (§11).

    A derived axis is thin because the runs were short or their snapshots
    sparse — proposing a design point at a snapshot index would be meaningless
    output, and naming the wrong remedy is worse than staying silent.

    Held-constant inputs get **one** warning between them, not one each. A
    real model routinely holds a dozen inputs fixed (a real 250-run bracket
    fit holds twelve), and a dozen near-identical sentences differing only in
    a name is a wall of text nobody reads — while the per-factor strip already
    lists each one individually, which is the right place for the detail.
    """
    out: list[CoverageWarning] = []
    for factor in envelope.factors:
        if factor.kind == "derived":
            _, (lo, hi) = trained_span(points, factor)
            out.append(
                CoverageWarning(
                    code="derived_axis_thin",
                    factor=factor.name,
                    factor_label=factor.display_name,
                    message=(
                        f"This model was trained on snapshots {_fmt(lo)} to {_fmt(hi)} "
                        f"of each run. To cover more, the fix is a longer run or "
                        f"keeping more snapshots — it isn't something a new design "
                        f"point can add."
                    ),
                )
            )
    pinned = [f for f in envelope.factors if f.kind == "pinned"]
    if pinned:
        out.append(_pinned_warning(pinned))
    return out


def _pinned_warning(pinned: list[FactorEnvelope]) -> CoverageWarning:
    """One sentence covering every held-constant input.

    It used to end *"needs a new cohort that varies it"*, which was true only
    while a held-constant input could not be swept from here. Promotable
    choices (§11.5) made it false and, worse, self-contradicting: the panel
    said *new cohort* directly above a card offering to sweep the very same
    input into this one.

    It now states the finding and stops. Naming the tick would be wrong in the
    other direction — a pinned input is promotable only when the descriptor
    declares a domain for it, so a `material` or `multiselect` held constant
    has no tick to point at. The *Fill the gaps* card renders the offer exactly
    when the offer exists, which is where that sentence belongs.
    """
    if len(pinned) == 1:
        only = pinned[0]
        return CoverageWarning(
            code="pinned_factor",
            factor=only.name,
            factor_label=only.display_name,
            message=(
                f"'{only.display_name}' was the same ({only.pinned_value!r}) in every "
                f"run, so the model can't say anything about changing it. Covering it "
                f"needs runs that vary it."
            ),
        )
    names = ", ".join(sorted(f.display_name for f in pinned))
    return CoverageWarning(
        code="pinned_factor",
        message=(
            f"{len(pinned)} inputs were the same in every run ({names}), so the model "
            f"can't say anything about changing them. Covering any of them needs runs "
            f"that vary it."
        ),
    )


def _source_framing(source_kind: str, verdict: Verdict) -> CoverageWarning | None:
    """Explain a discouraging verdict that the data source fully accounts for.

    An optimisation study concentrates its runs near the optimum — that is the
    algorithm working correctly, not a design flaw. A cohort drawn from one
    reads ``poor`` every time, and a red light that is always red is one users
    learn to ignore. Same measurement, honest framing, and it points at the
    real remedy instead of implying someone did something wrong.
    """
    if source_kind != "optimisation" or verdict not in ("thin", "poor"):
        return None
    return CoverageWarning(
        code="source_framing",
        message=(
            "These runs came from an optimisation study, so they cluster near the "
            "best design found. That is expected — it just means this model should "
            "be trusted near there, not across the whole range. To cover the rest, "
            "add a spread of runs across the range rather than more optimisation."
        ),
    )


def _exclusion_message(diagnostics: CoverageDiagnostics) -> str:
    """The "measured on N of M runs" note, naming which gate took which."""
    dropped = diagnostics.n_excluded_invalid + diagnostics.n_excluded_filtered
    total = diagnostics.n_points + dropped
    parts: list[str] = []
    if diagnostics.n_excluded_invalid:
        parts.append(f"{diagnostics.n_excluded_invalid} had results flagged as not valid")
    if diagnostics.n_excluded_filtered:
        parts.append(f"{diagnostics.n_excluded_filtered} were excluded by your filters")
    return f"Coverage is measured on {diagnostics.n_points} of {total} runs: {' and '.join(parts)}."


def _headline(
    verdict: Verdict,
    radius: float,
    per_factor: tuple[FactorCoverage, ...],
    per_pair: tuple[PairCoverage, ...] = (),
    *,
    settings: CoverageSettings | None = None,
) -> str:
    """The one-line answer, naming a weak axis **only when there is one**.

    The naming used to be unconditional — ``min(per_factor)`` was always
    called "the biggest gap", even at 0.90 with a ``good`` verdict and a green
    row of its own. On the demo heat-sink archive that produced "the biggest
    gap is in thermal_conductivity" directly above a row reading "90%
    covered", which reads as the panel contradicting itself and is the fastest
    way to lose a user's trust in a number they cannot check by eye.

    So an axis is named only when its **own** verdict is not ``good``. When
    every axis is individually fine and the overall verdict still is not, the
    cause is not any one input — it is the **combinations**, which is a real
    and separate thing to say. Each row measures one input's own range; the
    verdict measures the joint space, where 531 runs spread over six inputs
    leave holes no single axis can show. Saying that plainly is the difference
    between a contradiction and an explanation.
    """
    settings = settings if settings is not None else CoverageSettings()
    weak = [f for f in per_factor if f.actionable and f.verdict != "good"]
    weakest = min(weak, key=lambda f: f.value, default=None)
    where = f" The thinnest input on its own is {weakest.display_name}." if weakest else ""

    # **A failed pair outranks everything else in the sentence**, because it
    # is the finding neither neighbouring measure could make and the one with
    # a different remedy. Naming the pair is the whole point: "coverage is
    # patchy" is not actionable, "Base width and Material conductivity were
    # never varied together" is.
    bad_pairs = [p for p in per_pair if p.verdict == "poor"]
    if bad_pairs:
        worst = bad_pairs[0]
        if one_at_a_time(per_pair, floor=settings.one_at_a_time_floor, settings=settings):
            return (
                f"Each input on its own was varied across its range, but they were "
                f"varied **one at a time** — every pair of inputs leaves most of its "
                f"combinations untried, {worst.pair_label} worst of all. A model built "
                f"from these runs cannot learn how any two inputs behave together. "
                f"Fixing it needs a set that varies several inputs at once."
            ).replace("**", "")
        others = len(bad_pairs) - 1
        also = f" ({others} other pair{'' if others == 1 else 's'} too.)" if others else ""
        return (
            f"Each input on its own is well covered, but their combinations are not: "
            f"{worst.pair_label} were rarely varied together, so most of that "
            f"combination has never been run and a model would be guessing across "
            f"it.{also} Filling this needs runs that vary several inputs at once, "
            f"not a wider range on any one of them."
        )

    if verdict == "good":
        return f"These runs cover the range well.{where}"

    # Every input individually fine, verdict still not — say why, or the user
    # is left holding two answers that look like a contradiction.
    if not weak and any(f.actionable for f in per_factor):
        shortfall = (
            "unevenly — there are noticeable gaps"
            if verdict == "thin"
            else "poorly — large parts are untried"
        )
        return (
            f"Each input on its own is well covered, but their combinations "
            f"are not: taken together these runs cover the space {shortfall} a "
            f"model would have to guess across. Filling this needs runs that vary "
            f"several inputs at once, not a wider range on any one of them."
        )

    if verdict == "thin":
        return (
            f"These runs cover the range unevenly — there are noticeable gaps a "
            f"model would have to guess across.{where}"
        )
    return (
        f"These runs leave large parts of the range untried, so a model built from "
        f"them would be guessing outside the area they cover (biggest gap "
        f"{radius:.2f} of the range).{where}"
    )


def _fmt(value: float | None) -> str:
    """A bound as short user-facing text."""
    if value is None:
        return "?"
    return str(int(value)) if float(value).is_integer() else f"{value:.3g}"


__all__ = [
    "CoverageDiagnostics",
    "CoverageReport",
    "CoverageWarning",
    "Criterion",
    "DesignCoverage",
    "FactorCoverage",
    "Measure",
    "Verdict",
    "analyse_design",
    "build_report",
]
