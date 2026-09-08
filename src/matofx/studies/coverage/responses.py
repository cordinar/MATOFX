"""Per-response coverage (ADR-0020 §2.2, §2.3).

``SurrogateModelVersion.responses`` is a **list** and ``fit_surrogate`` fits
**one** response at a time, so a version carries N GPs each with its own ARD
length scales. Mass may depend almost entirely on thickness while peak
temperature depends on everything: over an **identical point set** those GPs
give **different verdicts**. There is no single "GP coverage" for a design,
which is why this pass exists alongside the response-agnostic one — and why
the report headlines the *worst*, a model being only as trustworthy as its
least-covered response.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from matofx.config import CoverageSettings
from matofx.studies.coverage.gp import (
    ResponseKind,
    ResponseModel,
    prior_sigma,
    relative_sigma,
    suggest_points,
    variance_field,
)
from matofx.studies.coverage.report import (
    CoverageWarning,
    Criterion,
    FactorCoverage,
    Verdict,
    _per_factor,
    _score,
    _verdict,
    _worst,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from matofx.studies.coverage.envelope import Envelope
    from matofx.studies.coverage.points import PointSet

_SUGGEST_SEED = 20_021
"""Fixed seed, so the same version proposes the same runs twice — a batch
that shuffled between page loads would be impossible to review."""

_DEFAULT_SUGGESTIONS = 5
"""How many runs a response proposes by default. The UI's spinner overrides
it; this is only the number the stored report carries."""


@dataclass(frozen=True, slots=True)
class ResponseCoverage:
    """One response's verdict, and the runs that would improve it."""

    response_name: str
    response_kind: ResponseKind
    surrogate_method: str
    criterion: Criterion
    verdict: Verdict
    message: str
    covering_radius: float
    coverage_score: float
    per_factor: tuple[FactorCoverage, ...] = ()
    suggested_points: tuple[Mapping[str, object], ...] = ()
    warnings: tuple[CoverageWarning, ...] = ()
    edge_sigma_threshold: float | None = None
    """The stored ``edge`` boundary (§8) — a percentile of relative sigma at
    the **training points**, computed once here and never recomputed at
    predict time. That reference set is the definition of "the trained
    region"; a percentile over an envelope-wide sample would move with the
    envelope. Storing it also keeps the guard working on a version whose
    ``keep_training_data`` was off, which could not recompute it at all."""
    ard_relevance: dict[str, float] | None = None
    """Which axes this response actually responds to — the thing a model-free
    criterion cannot know, and the reason a GP spends budget better."""

    @property
    def available_statuses(self) -> tuple[str, ...]:
        """Which guard verdicts this response can return (§8).

        A ``poly`` or ``gbm`` response has no posterior variance, so ``edge``
        is not computable. It says so, rather than silently never returning
        one — the envelope half of the guard, which catches the genuinely
        dangerous case, works for every method.
        """
        if self.edge_sigma_threshold is None:
            return ("inside", "outside")
        return ("inside", "edge", "outside")


def analyse_responses(
    per_response: Mapping[str, ResponseModel],
    envelope: Envelope,
    *,
    points: PointSet,
    settings: CoverageSettings | None = None,
) -> tuple[ResponseCoverage, ...]:
    """One :class:`ResponseCoverage` per response, in stable name order.

    ``points`` is needed for the per-factor strip — geometry is geometry, and
    duplicating it per response would be the wrong kind of independence. What
    genuinely differs per response is the *criterion*, the headline number it
    produces, the ARD relevance, and the runs proposed.
    """
    settings = settings if settings is not None else CoverageSettings()
    return tuple(
        _analyse_one(name, per_response[name], envelope, points=points, settings=settings)
        for name in sorted(per_response)
    )


def _analyse_one(
    name: str,
    model: ResponseModel,
    envelope: Envelope,
    *,
    points: PointSet,
    settings: CoverageSettings,
) -> ResponseCoverage:
    """Measure one response, by whichever criterion its fit supports."""
    criterion = _choose_criterion(model, settings)
    per_factor = _per_factor(points, envelope, radius=0.0, settings=settings)

    if criterion == "model_free":
        return _model_free_result(name, model, envelope, points, settings)
    return _gp_result(name, model, envelope, points, per_factor, settings)


def _choose_criterion(model: ResponseModel, settings: CoverageSettings) -> Criterion:
    """Which criterion this response's fit can actually support (§2.3).

    A GP-variance path that assumed every fit had a posterior variance would
    break on two of four user-selectable kinds — and MOP's auto-best
    metamodel picks by Q² without regard to whether the winner carries
    uncertainty. Below the cold-start ratio the ARD length scales are not yet
    trustworthy either: the analysis would be steering by a compass it
    calibrated with the compass.
    """
    if not model.has_variance:
        return "model_free"
    active = max(len(model.factors), 1)
    if model.n_training_points < settings.cold_start_ratio * active:
        return "model_free"
    return "gp_variance"


def _gp_result(
    name: str,
    model: ResponseModel,
    envelope: Envelope,
    points: PointSet,
    per_factor: tuple[FactorCoverage, ...],
    settings: CoverageSettings,
) -> ResponseCoverage:
    """The GP pass: sigma over the envelope, scored against the trained region."""
    field_ = variance_field(model, envelope, n_candidates=settings.n_candidates, seed=_SUGGEST_SEED)
    at_training = relative_sigma(model, list(points.rows)) or ()
    threshold = (
        float(np.percentile(np.asarray(at_training), settings.edge_percentile))
        if at_training
        else None
    )

    worst_sigma = max(field_.sigma) if field_.sigma else 0.0
    score = _support_score(worst_sigma, threshold, prior_sigma(model, envelope))
    verdict = _worst([_verdict(score, settings), *(f.verdict for f in per_factor if f.actionable)])
    batch = suggest_points(
        model,
        envelope,
        n=_DEFAULT_SUGGESTIONS,
        seed=_SUGGEST_SEED,
        settings=settings,
    )
    return ResponseCoverage(
        response_name=name,
        response_kind=model.kind,
        surrogate_method=model.method,
        criterion="gp_variance",
        verdict=verdict,
        message=_message(name, verdict, model),
        covering_radius=worst_sigma,
        coverage_score=score,
        per_factor=per_factor,
        suggested_points=batch.points,
        warnings=batch.warnings,
        edge_sigma_threshold=threshold,
        ard_relevance=model.ard_relevance,
    )


def _support_score(worst_sigma: float, trained: float | None, prior: float | None) -> float:
    """Where the emptiest corner sits between "as good as the data" and "no data".

    ``1.0`` — even the worst point in the envelope is as well-supported as the
    training runs. ``0.0`` — somewhere in the envelope the model knows as
    little as it would with no data at all.

    The scale between those two ends is what makes the number mean something.
    An earlier version scored ``trained / worst``, which for any GP is the
    ratio of sigma *at* the data (~0.001) to sigma at the emptiest corner
    (~1 to 30) — always ~0, so every GP response read ``poor`` regardless of the
    design. That is finding 44's always-red light reappearing in the
    per-response pass, and it was invisible until a real fit was scored.
    """
    if prior is None or trained is None:
        return 0.0
    span = prior - trained
    if span <= 0.0:
        return 1.0
    return float(max(0.0, min(1.0, 1.0 - (worst_sigma - trained) / span)))


def _model_free_result(
    name: str,
    model: ResponseModel,
    envelope: Envelope,
    points: PointSet,
    settings: CoverageSettings,
) -> ResponseCoverage:
    """The fallback: the same geometry the design pass uses, attributed here.

    A model-free response still gets a full verdict and usable suggestions —
    it loses ARD weighting, not the feature. And this is not an edge case:
    the customer with a scattered ad-hoc archive is the modal one, so the
    path has to work exactly when the data is worst.
    """
    from matofx.studies.coverage.metrics import covering_radius  # noqa: PLC0415

    radius = covering_radius(
        envelope.normalise(points),
        envelope,
        n_candidates=settings.n_candidates,
        seed=_SUGGEST_SEED,
    )
    score = _score(radius, len(envelope.metric_axes))
    geometric = _per_factor(points, envelope, radius=radius, settings=settings)
    verdict = _worst([_verdict(score, settings), *(f.verdict for f in geometric if f.actionable)])
    batch = suggest_points(
        model,
        envelope,
        n=_DEFAULT_SUGGESTIONS,
        seed=_SUGGEST_SEED,
        settings=settings,
    )
    return ResponseCoverage(
        response_name=name,
        response_kind=model.kind,
        surrogate_method=model.method,
        criterion="model_free",
        verdict=verdict,
        message=_message(name, verdict, model),
        covering_radius=radius,
        coverage_score=score,
        per_factor=geometric,
        suggested_points=batch.points,
        warnings=batch.warnings,
        edge_sigma_threshold=None,
        ard_relevance=None,
    )


def _message(name: str, verdict: Verdict, model: ResponseModel) -> str:
    """The response's one-line answer, in the user's words."""
    what = "this field" if model.kind == "field" else f"'{name}'"
    if verdict == "good":
        return f"The runs cover the range well for {what}."
    if verdict == "thin":
        return (
            f"There are parts of the range where the model would be guessing about "
            f"{what}. Adding a few runs there would firm it up."
        )
    if verdict == "insufficient":
        return f"There isn't enough here to judge how well {what} is covered."
    return (
        f"Large parts of the range are untried for {what}, so predictions there are "
        f"extrapolation rather than interpolation."
    )


__all__ = ["ResponseCoverage", "ResponseKind", "ResponseModel", "analyse_responses"]
