"""GP-variance coverage and the runs it asks for (ADR-0020 §5).

For fixed hyperparameters the GP posterior variance
``sigma^2(x) = k(x,x) - k(x,X)K^-1 k(X,x)`` **contains no `y` term** — it
depends only on *where* the design was sampled. Posterior sigma therefore
*is* a coverage measure: distance-to-data warped by the kernel. Its advantage
over a plain geometric criterion is **ARD length scales** — the GP has learned
which factors the response actually responds to, so it spends an expensive
simulation budget on the axes that matter.

**One correction the design did not anticipate.** This codebase's GP fits with
``normalize_y=True``, so ``predict`` multiplies its standard deviation back up
by the response's own spread before returning it. Raw sigma is therefore *not*
`y`-free: rescale a response by 3 and every sigma triples. Everything here
uses **relative sigma** — sigma divided by that spread — which is `y`-free
under a rescaling, comparable across responses in different physical units,
and the only form in which a configured threshold means the same thing twice.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

from matofx.config import CoverageSettings
from matofx.studies._intsnap import snap_int
from matofx.studies.coverage.report import CoverageWarning
from matofx.studies.surrogate import Surrogate
from matofx.studies.surrogate.field import FieldSurrogate

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from matofx.studies.coverage.envelope import Envelope
    from matofx.studies.coverage.points import PointSet

ResponseKind = Literal["scalar", "field"]


@dataclass(frozen=True, slots=True)
class ResponseModel:
    """One response of a model version, whatever shape its fit took (§2.2.1).

    Deliberately not a :class:`Surrogate`. A **scalar** response is one fitted
    surrogate; a **field** response is fitted *per POD mode*, so there is no
    single GP to interrogate and its sigma is the energy-weighted aggregate
    over the mode surrogates.
    """

    name: str
    kind: ResponseKind
    surrogate: Surrogate | None = None
    field_surrogate: FieldSurrogate | None = None

    @classmethod
    def scalar(cls, name: str, surrogate: Surrogate) -> ResponseModel:
        """A scalar response backed by one fitted surrogate."""
        return cls(name=name, kind="scalar", surrogate=surrogate)

    @classmethod
    def field(cls, name: str, field_surrogate: FieldSurrogate) -> ResponseModel:
        """A field response backed by its per-mode surrogates."""
        return cls(name=name, kind="field", field_surrogate=field_surrogate)

    @property
    def parts(self) -> list[Surrogate]:
        """The constituent surrogates — one for a scalar, k for a field."""
        if self.kind == "field" and self.field_surrogate is not None:
            return list(self.field_surrogate.per_mode_surrogates)
        return [self.surrogate] if self.surrogate is not None else []

    @property
    def method(self) -> str:
        """The fitting method (``gp`` / ``gp_matern`` / ``poly`` / ``gbm``)."""
        parts = self.parts
        return str(parts[0].kind) if parts else "gp"

    @property
    def has_variance(self) -> bool:
        """Whether **every** constituent fit carries a posterior sigma.

        A field response with any non-GP mode falls back wholesale rather
        than aggregating over a partial set, which would misreport.
        """
        parts = self.parts
        return bool(parts) and all(p.has_variance for p in parts)

    @property
    def factors(self) -> list[str]:
        """The numeric factors the underlying fit was trained over."""
        parts = self.parts
        return list(parts[0].factors) if parts else []

    @property
    def n_training_points(self) -> int:
        """Complete rows the fit used — the numerator of the cold-start ratio."""
        parts = self.parts
        return len(parts[0].quality.actual) if parts else 0

    @property
    def mode_weights(self) -> list[float]:
        """Energy weights over the retained modes, renormalised to sum to 1.

        ``mode_energy_spectrum`` holds each retained mode's share of the
        *total* energy, so it sums to the retention cutoff (~0.99), not to 1.
        A mode carrying 1 % of the field must not drive the verdict as hard
        as the dominant one — but the weights still have to be a mean.
        """
        if self.kind != "field" or self.field_surrogate is None:
            return [1.0]
        spectrum = np.asarray(
            self.field_surrogate.mode_energy_spectrum[: self.field_surrogate.n_modes], dtype=float
        )
        total = float(spectrum.sum())
        if total <= 0.0:
            return [1.0 / max(len(spectrum), 1)] * len(spectrum)
        return [float(v) for v in spectrum / total]

    @property
    def ard_relevance(self) -> dict[str, float] | None:
        """Per-factor relevance from the ARD length scales, ``None`` if no GP.

        Reported as ``1 / length_scale`` normalised to sum to 1: a short
        length scale means the response varies quickly along that axis, so
        it deserves more of the budget. This is exactly what a model-free
        criterion cannot know.
        """
        parts = self.parts
        if not parts or not self.has_variance:
            return None
        scales = parts[0].ard_length_scales
        if not scales:
            return None
        inverse = np.asarray([1.0 / s if s > 0 else 0.0 for s in scales], dtype=float)
        total = float(inverse.sum())
        if total <= 0.0:
            return None
        return {name: float(v) for name, v in zip(parts[0].factors, inverse / total, strict=False)}


@dataclass(frozen=True, slots=True)
class VarianceField:
    """Relative posterior sigma sampled over the declared envelope."""

    points: tuple[Mapping[str, Any], ...] = ()
    """Candidate points in raw units — each a submittable input set."""
    sigma: tuple[float, ...] = ()
    """Relative sigma at each candidate, aligned with :attr:`points`."""
    normalised: np.ndarray = field(default_factory=lambda: np.zeros((0, 0)))
    """The same candidates in normalised coordinates (for the metrics)."""


@dataclass(frozen=True, slots=True)
class SuggestionBatch:
    """Proposed runs, and anything the user should know about the batch."""

    response_name: str
    points: tuple[Mapping[str, Any], ...] = ()
    warnings: tuple[CoverageWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class SuggestionOption:
    """What committing ``n`` of the proposed runs would buy."""

    n: int
    verdict: str
    """The verdict the archive would read once these runs land."""
    coverage_score: float
    covering_radius: float
    gap_after: float
    """Biggest untried stretch afterwards, as a fraction of the space -- the
    same quantity the per-factor rows quote, so the two are comparable."""
    reaches_good: bool
    """Whether this is the *smallest* offered batch that reaches ``good``.

    The one row worth recommending: past it the user is buying diminishing
    returns, and nothing else in the table says where that point is."""

    levels_short: int = 0
    """Option levels still under-run after this batch (§4.6 imbalance).

    The gap above is a **covering radius** — numeric geometry, which a
    categorical axis contributes nothing to. So once a held-constant choice
    can be opened up, a batch of twenty runs sweeping two materials moves the
    gap barely at all while doing exactly what was asked, and a table showing
    only the gap reads as though the runs bought nothing.

    Reported as its own quantity rather than folded into the gap. Blending
    them would put a calibrated-looking number on categorical balance, which
    is the overclaim the pairwise pass deliberately refuses to make."""


@dataclass(frozen=True, slots=True)
class SuggestionPlan:
    """The cost/benefit table behind "suggest runs to fill the gaps" (§11.5).

    **The question is not "how many runs do you want".** Nobody knows that --
    it is the thing they opened the panel to find out. The answerable question
    is "what would N runs buy me?", asked for a spread of N so the user can
    see where the curve flattens and then choose. A spinner asked the user for
    the answer instead of giving it to them.

    Cheap to compute despite appearances: the suggestion walk is greedy, so
    the first ``k`` picks of an ``n``-point batch *are* the ``k``-point batch.
    One walk at the largest size, then a covering radius per prefix.
    """

    options: tuple[SuggestionOption, ...] = ()
    points: tuple[Mapping[str, Any], ...] = ()
    """The full batch at the largest offered size; option ``n`` is its first
    ``n`` rows, exactly (the greedy prefix property above)."""
    current_verdict: str = "insufficient"
    current_score: float = 0.0
    current_gap: float = 0.0
    current_levels_short: int = 0
    """Option levels under-run *before* any of these runs land, so the
    table can show the categorical price moving as well as the gap."""

    already_good: bool = False
    """Whether the archive **already** reads ``good``.

    Changes what the table is for. With a target to hit, the useful row is the
    smallest batch that hits it; with no target, every row is simply "tighter
    still", and flagging one of them "enough" would invent a threshold the
    user had already cleared."""

    measured_axes: int = 0
    """How many **numeric** axes the figures above were measured over.

    Zero for a space made only of choices, and that is not a footnote: the
    gap is a covering radius over numeric geometry, so with no numeric axis
    it is ``0.0`` — a figure that reads as "no gap at all", which is the most
    flattering thing the table could say about an archive that has never run
    half its options. The reader is told the gap does not apply here and
    pointed at the shortfall count instead, which is the column that does."""

    warnings: tuple[CoverageWarning, ...] = ()


# --- relative sigma -------------------------------------------------------


def relative_sigma(
    model: ResponseModel | Surrogate, points: Sequence[Mapping[str, Any]]
) -> tuple[float, ...] | None:
    """Posterior sigma at ``points``, divided by the response's own spread.

    ``None`` when the method carries no posterior variance (``poly`` /
    ``gbm``), or the fit never happened. For a field response this is the
    **energy-weighted** aggregate across the mode surrogates (§2.2.1) — and
    it has to be the *relative* sigma that is weighted, not the raw one:
    mode 1's coefficients are the largest, so weighting raw sigma by energy
    would count that mode's magnitude twice.
    """
    if isinstance(model, Surrogate):
        return _relative_sigma_one(model, points)
    if not model.has_variance:
        return None
    parts, weights = model.parts, model.mode_weights
    per_part = [_relative_sigma_one(p, points) for p in parts]
    if any(s is None for s in per_part):
        return None
    stacked = np.asarray(per_part, dtype=float)
    w = np.asarray(weights[: stacked.shape[0]], dtype=float)
    w = w / w.sum() if w.sum() > 0 else np.full(stacked.shape[0], 1.0 / stacked.shape[0])
    return tuple(float(v) for v in (w[:, None] * stacked).sum(axis=0))


def _relative_sigma_one(
    surrogate: Surrogate, points: Sequence[Mapping[str, Any]]
) -> tuple[float, ...] | None:
    """One surrogate's relative sigma, or ``None`` if it has no variance."""
    if not surrogate.has_variance or not points:
        return () if surrogate.has_variance else None
    _, sigma = surrogate.predict([_numeric_only(p) for p in points])
    if sigma is None:
        return None
    scale = surrogate.y_scale or 1.0
    return tuple(float(s) / scale for s in sigma)


def _numeric_only(point: Mapping[str, Any]) -> dict[str, float]:
    """The numeric half of a point — what ``Surrogate.predict`` accepts.

    Categorical and pinned coordinates ride along on a suggestion so it is a
    submittable input set, but the fit never saw them (``factor_columns`` is
    numeric-only). Factors the point omits — a ``derived`` axis, which no
    design candidate carries — fall back to their training mean inside
    ``predict``, which is the middle of the trained span rather than its edge.
    """
    return {
        k: float(v)
        for k, v in point.items()
        if not isinstance(v, bool) and isinstance(v, int | float)
    }


# --- the variance field ---------------------------------------------------


def variance_field(
    model: ResponseModel,
    envelope: Envelope,
    *,
    n_candidates: int,
    seed: int,
) -> VarianceField:
    """Evaluate relative sigma over a candidate sample of the envelope.

    Candidates come from ``envelope.sample``, so integer axes are already on
    their lattice. Returns an empty field for a model with no posterior
    variance — callers fall back to the model-free criterion rather than
    treating an empty field as "perfectly covered".
    """
    normalised = envelope.sample(n_candidates, seed=seed)
    points = tuple(envelope.denormalise(normalised))
    sigma = relative_sigma(model, points) if points else ()
    if sigma is None:
        return VarianceField(points=points, sigma=(), normalised=normalised)
    return VarianceField(points=points, sigma=tuple(sigma), normalised=normalised)


# --- suggested runs -------------------------------------------------------


def suggest_points(
    model: ResponseModel,
    envelope: Envelope,
    *,
    n: int,
    seed: int,
    settings: CoverageSettings | None = None,
) -> SuggestionBatch:
    """Plan a batch of runs that would most improve this response (§5).

    **Greedy sequential**, never top-*N*: take the max-sigma candidate,
    condition on it as a pseudo-observation, recompute sigma, repeat. Top-*N*
    would return *N* adjacent points in the same hole. Because sigma is
    `y`-free the whole batch is planned with **zero solver calls**.

    Falls back to a max-distance walk over the same candidates when the
    response has no posterior variance — a ``poly`` or ``gbm`` response loses
    ARD weighting, not the feature.

    Suggestions obey the integer invariant of
    ``designs/integer-parameter-handling.md`` (§5.1): every emitted integer
    value is integral, already-run lattice points are never re-proposed, the
    batch shrinks with a warning when a small lattice is exhausted, ``pinned``
    factors are emitted at their pinned value, and ``derived`` axes are
    skipped entirely — nobody can run a job at a snapshot index.

    **Already-run points come from the model's own training data**, never
    from a caller-supplied list: a second source of truth for "what has been
    run" is one that can fall out of step with the fit it is meant to
    describe.
    """
    settings = settings if settings is not None else CoverageSettings()
    warnings_out: list[CoverageWarning] = []

    if n <= 0:
        return SuggestionBatch(response_name=model.name)
    if not envelope.metric_axes:
        return SuggestionBatch(
            response_name=model.name, warnings=(_nothing_to_suggest(None, envelope),)
        )

    field_ = variance_field(model, envelope, n_candidates=settings.n_candidates, seed=seed)
    keep = _admissible(field_.points, envelope, _already_run(model), settings, warnings_out)
    if not keep:
        return SuggestionBatch(response_name=model.name, warnings=tuple(warnings_out))

    chosen = (
        _greedy_by_variance(model, field_, keep, n)
        if model.has_variance
        else _greedy_by_distance(field_, keep, n, envelope)
    )
    if len(chosen) < n:
        warnings_out.append(_shrunk_warning(envelope, len(chosen), n))
    return SuggestionBatch(
        response_name=model.name,
        points=tuple(field_.points[i] for i in chosen),
        warnings=tuple(warnings_out),
    )


def _already_run(model: ResponseModel) -> list[Mapping[str, Any]]:
    """The design points this response was actually fitted on.

    Read off the surrogate's own ``training_inputs`` (ADR-0020 §5) so the
    dedup can never disagree with the model it is proposing runs for.
    """
    parts = model.parts
    if not parts or not parts[0].training_inputs:
        return []
    columns = parts[0].training_inputs
    n = len(next(iter(columns.values())))
    return [{name: values[i] for name, values in columns.items()} for i in range(n)]


def prior_sigma(model: ResponseModel, envelope: Envelope) -> float | None:
    """Relative sigma where the fit knows **nothing** — its no-data ceiling.

    The yardstick a per-response coverage score has to be measured against,
    and not a constant: it is the kernel's amplitude relative to the response's
    own spread, so it differs per fit. Measured at ~33 on a real bracket GP,
    against ~0.0014 at the training points — which is why scoring
    ``threshold / worst_sigma`` made **every** GP response read ``poor``
    however good the design was.

    Evaluated far outside the envelope, where the posterior has decayed to the
    prior. ``None`` for a method with no posterior variance.
    """
    axes = envelope.metric_axes
    if not axes or not model.has_variance:
        return None
    far: dict[str, Any] = {}
    for name in axes:
        factor = envelope.by_name(name)
        lo = factor.lo if factor.lo is not None else 0.0
        hi = factor.hi if factor.hi is not None else 1.0
        span = max(abs(hi - lo), 1.0)
        far[name] = hi + span * _PRIOR_SPANS
    values = relative_sigma(model, [far])
    return float(values[0]) if values else None


_PRIOR_SPANS = 1000.0
"""How many envelope-widths out counts as "no data". Far enough that any
stationary kernel has decayed to its prior; the value is not sensitive because
the decay is exponential."""


def suggest_design_points(
    points: PointSet,
    envelope: Envelope,
    *,
    n: int,
    seed: int,
    settings: CoverageSettings | None = None,
) -> SuggestionBatch:
    """Plan runs from the **design alone** — no model, no response (§5 / §11.5).

    §11.5 puts "suggest runs to fill the gaps" on the cohort-preview panel,
    where by construction nothing has been fitted and no response has been
    chosen (§2.1). :func:`suggest_points` needs a fitted ``ResponseModel``;
    this path needs only the geometry the design pass already measured.

    A max-min-distance walk over the same candidate set: each pick is the
    point furthest from everything chosen so far *and* from the runs that
    already exist. It loses ARD weighting — the same trade §2.3 describes for
    every model-free case — and keeps every §5.1 invariant: integers stay
    integral, already-run lattice points are never re-proposed, the batch
    shrinks with a warning when a small lattice is exhausted, ``pinned``
    factors ride along at their value, and ``derived`` axes never appear.

    The batch carries **no** ``response_name``: attributing it to a response
    would be a claim this path cannot make.

    **A space of nothing but choices is still a space.** The walk below is
    geometric, so with no numeric axis to move along there is nothing for it
    to do — and the guard that spotted this refused the batch outright, which
    made "untick every number, leave the material ticked" return nothing at
    all. Sweeping the options is exactly what a forty-runs-on-one-material
    archive needs, so that case has its own filler
    (:func:`_fill_option_gaps`) rather than a refusal.
    """
    settings = settings if settings is not None else CoverageSettings()
    warnings_out: list[CoverageWarning] = []

    if n <= 0:
        return SuggestionBatch(response_name="")
    if not envelope.metric_axes:
        if not envelope.categorical_axes:
            return SuggestionBatch(
                response_name="", warnings=(_nothing_to_suggest(points, envelope),)
            )
        return _fill_option_gaps(points, envelope, n=n, warnings_out=warnings_out)

    # The *same* candidate sample the report's covering radius is measured
    # over. Planning against one sample and measuring against another leaves
    # the greedy filling holes nobody is looking at — which is not a subtle
    # effect: with a sparse candidate set the two sets barely overlap.
    normalised = envelope.sample(settings.n_candidates, seed=seed)
    candidates = tuple(envelope.denormalise(normalised))
    field_ = VarianceField(points=candidates, sigma=(), normalised=normalised)
    keep = _admissible(candidates, envelope, list(points.rows), settings, warnings_out)
    if not keep:
        return SuggestionBatch(response_name="", warnings=tuple(warnings_out))

    existing = envelope.normalise(points)
    # **Ask for more than we need, because the tool's own rules will reject
    # some.** Sampling each axis inside its own bounds says nothing about
    # sum-checks, formula bounds or validity rules (§5.2), and on a
    # sum-checked tool *every* independently-sampled point is infeasible.
    # Over-drawing lets the feasible ones still fill the batch.
    over = min(len(keep), max(n, n * _FEASIBILITY_OVERDRAW))
    chosen = _greedy_by_distance(field_, keep, over, envelope, existing=existing)
    # `chosen` is indices into `candidates`; the rules pass works on whole
    # points, and returns them *resolved* (forces applied), so it replaces the
    # points rather than filtering the index list.
    proposed = _feasible([candidates[i] for i in chosen], points, envelope, n, warnings_out)
    if len(proposed) < n:
        warnings_out.append(_shrunk_warning(envelope, len(proposed), n))
    return SuggestionBatch(
        response_name="",
        points=tuple(proposed),
        warnings=tuple(warnings_out),
    )


def _fill_option_gaps(
    points: PointSet,
    envelope: Envelope,
    *,
    n: int,
    warnings_out: list[CoverageWarning],
) -> SuggestionBatch:
    """A batch for a space made only of choices — emptiest combination first.

    With no numeric axis there is no volume and no covering radius; the
    "gaps" are the **combinations of options nobody has run**, and filling
    them is a counting job rather than a geometric one. Each cell of the
    option grid is scored by how many existing runs sit in it, and the batch
    takes the emptiest first, updating the count as it goes so a batch larger
    than the grid levels the cells up rather than piling onto one.

    Repeating a combination is allowed here, and only here. Everywhere else a
    proposal that duplicates an existing run whole is waste; on a grid whose
    cells are all occupied, a repeat is the *only* move that improves the
    option balance — which is the disease this space can actually have.
    """
    from matofx.studies.coverage.envelope import level_label  # noqa: PLC0415

    axes = list(envelope.categorical_axes)
    grids = [list(envelope.by_name(name).levels or ()) for name in axes]
    if any(not levels for levels in grids):
        return SuggestionBatch(response_name="", warnings=(_nothing_to_suggest(points, envelope),))

    cells = [dict(zip(axes, combo, strict=True)) for combo in itertools.product(*grids)]
    # Read each axis's column once. `points.values` falls back to `constants`
    # for an input that never varied, so it is the right reader — but it is
    # also the expensive one to call inside a per-cell loop.
    columns = {name: [level_label(v) for v in points.values(name)] for name in axes}
    run: dict[tuple[str, ...], int] = {}
    for i in range(len(points.rows)):
        key = tuple(columns[name][i] for name in axes)
        run[key] = run.get(key, 0) + 1
    counts = [run.get(tuple(cell[name] for name in axes), 0) for cell in cells]
    # **Whether the *archive* already fills the grid**, not whether the batch
    # picks a cell twice. A batch of six into a three-option grid repeats by
    # arithmetic, and warning about that would fire on the very case the
    # feature is for: an option nobody has run getting the runs it needs.
    grid_exhausted = all(count > 0 for count in counts)

    pinned = {f.name: f.pinned_value for f in envelope.factors if f.kind == "pinned"}
    chosen: list[dict[str, Any]] = []
    for _ in range(n):
        # **Re-chosen every pick, not once per pass over the grid.** A pass
        # gives every cell one run, which is the wrong answer when one option
        # has never been run and the others have ten each: the budget belongs
        # to the empty cell until it catches up. Taking the current minimum
        # each time concentrates while a cell is behind and spreads once the
        # counts level, with no separate rule for either case.
        i = min(range(len(cells)), key=lambda k: (counts[k], k))
        chosen.append({**pinned, **cells[i]})
        counts[i] += 1
    if grid_exhausted:
        warnings_out.append(
            CoverageWarning(
                code="options_repeated",
                message=(
                    "These inputs only take a fixed set of options, and every "
                    "combination of them has been run at least once. The runs below "
                    "repeat combinations on purpose — with nothing else left to vary, "
                    "evening out how often each option is used is the only thing more "
                    "runs can buy here."
                ),
            )
        )
    proposed = _feasible(chosen, points, envelope, n, warnings_out)
    if len(proposed) < n:
        warnings_out.append(_shrunk_warning(envelope, len(proposed), n))
    return SuggestionBatch(response_name="", points=tuple(proposed), warnings=tuple(warnings_out))


def _nothing_to_suggest(points: PointSet | None, envelope: Envelope) -> CoverageWarning:
    """Nothing left for a batch to move — and *why* decides the wording.

    The single message read "they are either held constant across every run
    or come from the solver rather than the design", which is a statement
    about the archive. After the user has just unticked every input it is
    simply untrue, and it sends them looking at their data for a problem they
    created one control higher up.

    The two cases are told apart by whether an axis the archive **varied** is
    pinned in this envelope: only a hold can do that.
    """
    varied = set(points.factor_names) if points is not None else set()
    held_by_hand = sorted(
        envelope.by_name(name).display_name
        for name in varied
        if any(f.name == name and f.kind == "pinned" for f in envelope.factors)
    )
    if held_by_hand:
        return CoverageWarning(
            code="all_inputs_held",
            message=(
                "You have unticked every input a new run could vary, so there is "
                "nothing left for a batch to explore. Tick at least one of them to "
                "see what more runs would buy."
            ),
        )
    return CoverageWarning(
        code="nothing_to_suggest",
        message=(
            "There are no inputs here that a new run could vary — they are either "
            "held constant across every run or come from the solver rather than "
            "the design."
        ),
    )


_FEASIBILITY_OVERDRAW = 6
"""How many extra candidates to plan before the tool's rules are applied.

A sum-check rejects almost every independently sampled point, so the batch is
drawn wide and trimmed. Six is enough to fill a batch on the descriptors in the
example set without making the greedy walk noticeably slower."""


def _feasible(
    chosen: Sequence[Mapping[str, Any]],
    points: PointSet,
    envelope: Envelope,
    n: int,
    warnings_out: list[CoverageWarning],
) -> Sequence[Mapping[str, Any]]:
    """Keep the proposed runs the tool would actually accept (§5.2).

    Order matters: the greedy has already ranked these by how much each fills
    the space, so taking the **first** ``n`` survivors keeps the best of what
    remains rather than a fresh arbitrary slice.

    A rule that removes everything is reported in the tool's own words --
    ``resolve_for_generation`` returns the message the form would show -- so a
    short batch says *why* it is short instead of appearing to run out of ideas.
    """
    from matofx.studies.coverage.rules import feasible_points  # noqa: PLC0415

    descriptor = points.descriptor
    if descriptor is None or not chosen:
        # Still trimmed: the caller over-draws so the rules pass has slack, and
        # a cohort whose tool has no descriptor would otherwise be handed the
        # whole over-draw as its batch.
        return list(chosen)[:n]

    kept, dropped = feasible_points(descriptor, list(chosen), varying=list(envelope.metric_axes))
    if dropped and len(kept) < n:
        reason, count = max(dropped.items(), key=lambda kv: kv[1])
        warnings_out.append(
            CoverageWarning(
                code="rules_rejected",
                message=(
                    f"{count} proposed run{'' if count == 1 else 's'} had to be "
                    f"discarded because of a rule this tool sets on its inputs: "
                    f"{reason}"
                ),
            )
        )
    return kept[:n]


def _admissible(
    candidates: Sequence[Mapping[str, Any]],
    envelope: Envelope,
    existing: Sequence[Mapping[str, Any]],
    settings: CoverageSettings,
    warnings_out: list[CoverageWarning],
) -> list[int]:
    """Candidate indices worth proposing, deduplicated on the lattice.

    A candidate whose small-lattice integer coordinates match a run that
    already exists is dropped: proposing a duplicate wastes a solver run and
    adds no information.

    **Only when the lattice is the whole design.** The lattice key was treated
    as the identity of a point, so a candidate was thrown away for repeating an
    integer value even when its continuous coordinates were completely
    different — two runs at eighteen fins but different fin height, base
    thickness and fan speed are not the same design point. On a cohort whose
    seven attainable fin counts had all been run that rejected *every*
    candidate, and the panel reported there was nothing to suggest at all,
    naming one input as though the other three had no room left either.

    With a continuous axis present an exact duplicate has measure zero and
    needs no filter; the greedy max-min walk is what keeps the batch spread.
    """
    lattice_axes = [
        f.name
        for f in envelope.factors
        if f.kind == "integer"
        and (f.lattice() or ()) != ()
        and len(f.lattice() or ()) <= settings.max_lattice_cardinality
    ]
    if any(envelope.by_name(name).kind != "integer" for name in envelope.metric_axes):
        lattice_axes = []
    run: set[tuple[int, ...]] = set()
    for point in existing:
        key = _lattice_key(point, lattice_axes)
        if key is not None:
            run.add(key)

    keep: list[int] = []
    seen: set[tuple[int, ...]] = set(run)
    for i, point in enumerate(candidates):
        key = _lattice_key(point, lattice_axes)
        if key is not None and lattice_axes:
            if key in seen:
                continue
            seen.add(key)
        keep.append(i)
    if lattice_axes and not keep:
        # Labels, never internal names ([[feedback_use_labels_not_names]]).
        labels = [envelope.by_name(name).display_name for name in lattice_axes]
        if len(labels) == 1:
            detail = f"Every value of {labels[0]} inside the range you set has already been run."
        else:
            # With several axes it is the *combination* that is used up, not
            # each one — "every value of A, B and C" claims something stronger,
            # and probably false, about each of them separately.
            detail = (
                "Every combination of "
                + ", ".join(labels[:-1])
                + f" and {labels[-1]} inside the range you set has already been run."
            )
        warnings_out.append(
            CoverageWarning(
                code="lattice_exhausted",
                factor=lattice_axes[0],
                message=(
                    f"{detail} Widen that range, or switch to the full range the tool "
                    f"allows, to give the batch somewhere new to go."
                ),
            )
        )
    return keep


def _lattice_key(point: Mapping[str, Any], axes: Sequence[str]) -> tuple[int, ...] | None:
    """A point's coordinates on the small integer axes, or ``None``."""
    if not axes:
        return None
    out: list[int] = []
    for name in axes:
        value = point.get(name)
        if not isinstance(value, int | float) or isinstance(value, bool):
            return None
        out.append(snap_int(float(value)))
    return tuple(out)


def _greedy_by_variance(
    model: ResponseModel, field_: VarianceField, keep: list[int], n: int
) -> list[int]:
    """Max-sigma, condition, repeat (§5) — the batch costs no solver calls."""
    sigma = list(field_.sigma)
    if not sigma:
        return keep[:n]
    working = model.parts[0]
    chosen: list[int] = []
    available = list(keep)
    for _ in range(min(n, len(available))):
        best = max(available, key=lambda i: sigma[i])
        chosen.append(best)
        available.remove(best)
        if not available:
            break
        working = working.conditioned_on([_numeric_only(field_.points[best])])
        refreshed = _relative_sigma_one(working, [field_.points[i] for i in available])
        if refreshed is None:
            break
        for i, value in zip(available, refreshed, strict=False):
            sigma[i] = value
    return chosen


_MAX_SELECTION_CANDIDATES = 1024
"""Candidates considered per greedy pick. The selection is O(candidates x
probe points) per pick, so the pool is capped while the *probe* set stays the
full sample — a coarser search over the right objective beats a finer search
over the wrong one."""


def _greedy_by_distance(
    field_: VarianceField,
    keep: list[int],
    n: int,
    envelope: Envelope,
    *,
    existing: np.ndarray | None = None,
) -> list[int]:
    """Greedy **minimax**: each pick is the one that shrinks the worst gap most.

    The obvious walk — repeatedly take the candidate furthest from everything
    chosen so far — is a *maximin* criterion, and it is the wrong one. The
    number this feature reports is the **covering radius**: the furthest any
    point of the design space sits from a run, which is a *minimax* quantity.
    The two nearly coincide in two or three dimensions and come apart badly
    above about six, because maximin drives every pick onto a corner —
    maximally distant from everything, and covering almost none of the
    interior that the covering radius is dominated by.

    Measured against 200 random batches of the same size, the maximin walk
    beat 100% of them at 2-4 factors and **0%** at twelve: worse than picking
    at random, on the very metric the panel headlines.

    So each pick here is the candidate that **minimises the resulting covering
    radius** over a probe sample of the envelope — the textbook greedy for the
    k-centre problem, and the one whose objective is the number the user is
    shown. ``existing`` seeds the probe distances with the runs that already
    exist, so the first pick lands in a real hole rather than at a corner.
    """
    _ = envelope
    x = field_.normalised
    if x.shape[0] == 0 or x.shape[1] == 0 or not keep:
        return keep[:n]

    probe = x.astype(np.float64, copy=False)
    # Distance from every probe point to the nearest run that already exists.
    covered = np.full(probe.shape[0], np.inf)
    if existing is not None and existing.shape[0] and existing.shape[1] == probe.shape[1]:
        inside = existing[((existing >= 0.0) & (existing <= 1.0)).all(axis=1)]
        if inside.shape[0]:
            covered = _cdist_rows(probe, inside).min(axis=1)

    pool = keep[:_MAX_SELECTION_CANDIDATES]
    to_probe = _cdist_rows(x[pool], probe)  # (candidates, probe points)

    chosen: list[int] = []
    remaining = list(range(len(pool)))
    for _ in range(min(n, len(remaining))):
        # Worst remaining gap if each candidate were run next.
        resulting = np.minimum(covered[None, :], to_probe[remaining]).max(axis=1)
        best_local = int(remaining[int(np.argmin(resulting))])
        chosen.append(pool[best_local])
        covered = np.minimum(covered, to_probe[best_local])
        remaining.remove(best_local)
        if not remaining:
            break
    return chosen


def _cdist_rows(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Euclidean distances between every row of ``a`` and every row of ``b``."""
    diff = a[:, None, :] - b[None, :, :]
    out: np.ndarray = np.sqrt((diff * diff).sum(axis=2))
    return out


def _shrunk_warning(envelope: Envelope, produced: int, asked: int) -> CoverageWarning:
    """The batch could not be filled — say why, in the user's terms."""
    lattice_axes = [f.name for f in envelope.factors if f.kind == "integer" and f.lattice()]
    factor = lattice_axes[0] if lattice_axes else None
    where = f" for {', '.join(lattice_axes)}" if lattice_axes else ""
    return CoverageWarning(
        code="lattice_exhausted",
        factor=factor,
        message=(
            f"You asked for {asked} runs but only {produced} could be suggested: the "
            f"remaining whole-number values{where} inside your range have already been "
            f"run. That is the correct answer — that input is finished."
        ),
    )


__all__ = [
    "ResponseKind",
    "ResponseModel",
    "SuggestionBatch",
    "VarianceField",
    "prior_sigma",
    "relative_sigma",
    "suggest_points",
    "variance_field",
]


# --- how many runs, and what they buy (§11.5) -----------------------------

RUNS_PER_INPUT_LADDER: tuple[int, ...] = (1, 2, 5, 10, 20)
"""The ladder, in **runs per varying input** rather than absolute counts.

A fixed ``(3, 5, 8, 12, 20)`` was wrong in both directions at once: for two
inputs it wasted its top rungs, and for twelve it offered twenty runs to fill
a space where twenty is a rounding error. How many runs a design space needs
scales with how wide it is, so the ladder has to as well.

Runs-per-input is the unit engineers already reason in -- the user guide's own
rule of thumb is "at least ten runs per varied input" -- so the rungs land on
numbers that mean something rather than on arbitrary round figures."""

DEFAULT_PLAN_SIZES: tuple[int, ...] = (3, 5, 8, 12, 20)
"""Fallback ladder where the axis count is unknown. Prefer
:func:`default_plan_sizes`."""

_MIN_RUNGS = 2
"""Fewest rows the table needs to read as a curve rather than a single quote."""

_MIN_PLAN_SIZE = 3
_MAX_PLAN_SIZE = 250
"""Ceiling on an offered batch. Past this the honest answer is not a bigger
batch but a designed study, and pricing a 2000-run batch costs real time."""


def default_plan_sizes(n_axes: int, *, n_existing: int = 0) -> tuple[int, ...]:
    """Batch sizes worth offering, for ``n_axes`` inputs over ``n_existing`` runs.

    See :data:`RUNS_PER_INPUT_LADDER` for why this scales with the axis count
    rather than being a constant.

    **It scales with the archive too.** The ladder was a pure function of the
    axis count, so the same five numbers came back whether the cohort held
    twenty runs or five hundred — reported as "why do I always get the same
    number of runs suggested". A rung far smaller than what is already there
    cannot move a measure computed over the union of the two, so offering it
    spends a row of the table on a batch that provably buys nothing. Rungs
    below a tenth of the existing archive are dropped, and at least two are
    always kept so the table can still be read as a curve.
    """
    if n_axes <= 0:
        return DEFAULT_PLAN_SIZES
    sizes = sorted(
        {min(_MAX_PLAN_SIZE, max(_MIN_PLAN_SIZE, per * n_axes)) for per in RUNS_PER_INPUT_LADDER}
    )
    floor = max(_MIN_PLAN_SIZE, n_existing // 10)
    kept = [n for n in sizes if n >= floor]
    return tuple(kept) if len(kept) >= _MIN_RUNGS else tuple(sizes[-_MIN_RUNGS:])


"""Batch sizes offered by default -- a log-ish spread, not a linear one.

The interesting part of the curve is its knee, and a linear ladder either
misses it or spends every evaluation bracketing it.
"""

_PLAN_SEED = 20_020
"""The report's own candidate seed. Planning against a different probe than
the one the verdict is measured over would quote a gain the panel then
contradicts."""


def suggestion_plan(
    points: PointSet,
    envelope: Envelope,
    *,
    seed: int,
    sizes: Sequence[int] | None = None,
    settings: CoverageSettings | None = None,
    measure_envelope: Envelope | None = None,
) -> SuggestionPlan:
    """What each candidate batch size would do to this archive's coverage.

    Answers the user's actual question -- *how many runs do I need?* -- by
    pricing several batches against the same measurement the verdict uses.
    See :class:`SuggestionPlan` for why the greedy prefix makes this cheap.

    Every number comes from the report's own rules (:func:`score_for` /
    :func:`verdict_for`, :func:`covering_radius`), never a private copy: a
    planner promising ``good`` and a report then saying ``thin`` would be
    worse than no planner at all.

    ``measure_envelope`` separates **what the batch may move** from **what the
    figures describe**. Leaving an input out of a batch is expressed by pinning
    it in ``envelope``, which is what stops the walk moving it; but pinning it
    also removes it from every measurement, and a user who left it out of the
    batch has not necessarily asked to stop being told about its gaps. Passing
    the un-pinned envelope here keeps the batch narrow and the assessment
    whole. Defaults to ``envelope``, which is the narrowed reading.
    """
    from dataclasses import replace as _replace  # noqa: PLC0415

    from matofx.studies.coverage.report import analyse_design  # noqa: PLC0415

    settings = settings if settings is not None else CoverageSettings()
    # A space of nothing but choices still has gaps — the combinations nobody
    # has run — so only a space with *neither* kind of axis is unplannable.
    if not envelope.metric_axes and not envelope.categorical_axes:
        return SuggestionPlan(warnings=(_nothing_to_suggest(points, envelope),))
    # `sizes=None` means "you decide" -- the ladder depends on how wide the
    # design space is, which the caller has no business working out.
    if sizes is None:
        # **Every axis the batch can move**, not only the numeric ones.
        # Counting ``metric_axes`` alone meant opening up a held-constant
        # choice changed nothing about the ladder, so the one control the
        # user had just used appeared to do nothing to the numbers.
        sizes = default_plan_sizes(
            len(envelope.metric_axes) + len(envelope.categorical_axes),
            n_existing=len(points.rows),
        )
    wanted = sorted({int(n) for n in sizes if int(n) > 0})
    if not wanted:
        return SuggestionPlan()

    batch = suggest_design_points(points, envelope, n=wanted[-1], seed=seed, settings=settings)
    if not batch.points:
        return SuggestionPlan(warnings=batch.warnings)

    # **The space the radius was measured over**, not the one the batch may
    # move in. `gap_fraction` divides a covering radius by the diagonal of
    # the space it was taken over, and every radius below comes from
    # `measure`, which uses `measure_envelope`. Reading the axis count off
    # the batch envelope instead divided a radius taken over N axes by the
    # diagonal of a smaller M-axis space, so unticking one input out of
    # three reported a 25% gap where the honest figure was 20% — inflating
    # in the alarming direction, and clamping towards 100% as more inputs
    # were left out. It is the number this whole card is built around.
    n_axes = len((measure_envelope or envelope).metric_axes)

    def measure(rows: tuple[Mapping[str, Any], ...], *, pairs_may_vote: bool | None = None) -> Any:
        """The **whole** verdict for a projected point set, not a private score.

        Calls :func:`analyse_design` rather than recomputing a radius and a
        ratio, so what the table promises is by construction what the panel
        will say once the runs land. An earlier version scored only the joint
        radius, and once the pairwise pass (§4.6) could move the verdict the
        two immediately disagreed: the table offered "Well covered" above a
        card reading "Poorly covered", about the same archive. Cheap enough
        to be the right call -- 0.64s on the 531-run demo archive.
        """
        return analyse_design(
            _replace(points, rows=rows),
            measure_envelope or envelope,
            settings=settings,
            pairs_may_vote=pairs_may_vote,
        )

    current = measure(tuple(points.rows))
    # **The pairs' entitlement to vote is settled here, on the archive as it
    # stands, and every rung is then judged by it.** Left to each rung, the
    # pairwise term is ignored at one size and counted at the next — and since
    # it can only subtract, crossing that line makes the verdict worse for
    # adding runs. Reported twice now; the second time as "4, 8, 40 and 80
    # runs = Poorly covered, but 20 runs = Patchy". The readings themselves
    # are all monotone, so pinning the gate is what makes the ladder monotone.
    pairs_may_vote = any(p.discriminating for p in current.per_pair)
    already_good = current.verdict == "good"

    options: list[SuggestionOption] = []
    # An archive already reading `good` has no threshold left to cross, so
    # nothing is "the batch that gets you there" -- start as though it were
    # already reached and no row claims the knee.
    reached = already_good
    for n in wanted:
        prefix = tuple(batch.points[:n])
        if len(prefix) < n and options:
            # The lattice ran out -- a larger number would repeat the batch
            # already offered and promise a gain it cannot deliver.
            break
        design = measure(tuple(points.rows) + prefix, pairs_may_vote=pairs_may_vote)
        first_good = (not reached) and design.verdict == "good"
        reached = reached or design.verdict == "good"
        options.append(
            SuggestionOption(
                n=len(prefix),
                verdict=design.verdict,
                coverage_score=design.coverage_score,
                covering_radius=design.covering_radius,
                gap_after=gap_fraction(design.covering_radius, n_axes),
                reaches_good=first_good,
                levels_short=len(design.diagnostics.underpopulated_levels),
            )
        )

    extra = list(batch.warnings)
    # **Say when the projections stop being comparable to the panel.** The
    # gate above is pinned so the ladder is monotone, which means a rung big
    # enough to unlock the pairwise check is still judged without it. That is
    # the right call for comparing rungs and the wrong one to leave unsaid:
    # once the runs land the panel *will* apply it, and a plan that promised
    # one verdict while the report gives another is worse than no plan.
    if not pairs_may_vote and options:
        biggest = max(len(points.rows) + o.n for o in options)
        floor = settings.min_discriminating_pair_runs
        if biggest >= floor:
            extra.append(
                CoverageWarning(
                    code="pairs_unlock_later",
                    message=(
                        f"These projections leave the pairwise check out, because this "
                        f"cohort is below the {floor} runs a pair it needs and every row "
                        f"has to be judged the same way. The larger batches would take you "
                        f"past that — so once those runs land, the panel will start "
                        f"checking how well each pair of inputs was varied together, and "
                        f"that check can find something these figures cannot see."
                    ),
                )
            )

    return SuggestionPlan(
        options=tuple(options),
        points=tuple(batch.points),
        current_verdict=current.verdict,
        current_score=current.coverage_score,
        current_gap=gap_fraction(current.covering_radius, n_axes),
        current_levels_short=len(current.diagnostics.underpopulated_levels),
        already_good=already_good,
        measured_axes=n_axes,
        warnings=tuple(extra),
    )


def gap_fraction(radius: float, n_axes: int) -> float:
    """A covering radius as a fraction of the space's own diagonal.

    The radius is in normalised units whose magnitude grows with the number
    of axes, which makes the bare number meaningless to a reader -- 0.78 is
    excellent at twelve factors and dreadful at two. Divided by the largest
    it could be, it becomes "how big the worst hole is, as a share of the
    whole space": a number that means the same thing at any width.
    """
    if n_axes <= 0:
        return 0.0
    span = float(n_axes) ** 0.5
    return float(max(0.0, min(1.0, radius / span))) if span > 0 else 0.0
