"""The predict-time extrapolation guard (ADR-0020 §8).

**The highest-value half of the feature.** A build-time coverage check fires
once, when somebody is looking. This fires every time anyone leans on the
model — including inside an exported surrogate-tool running unattended in a
workflow, where nobody is watching a badge and the only thing that can catch a
runaway extrapolation is a number a validity rule can test.

Three statuses, and which of them a given fit can return is not uniform:

- ``outside`` — the point fails :meth:`Envelope.contains`. Deterministic and
  cheap, **needs no model at all**, and therefore works for every one of the
  four surrogate kinds. This is the half that catches the genuinely dangerous
  case: a pinned input supplied at a different value, a snapshot index past
  the trained span, a factor simply out of range.
- ``edge`` — inside the envelope, but the local sigma exceeds the threshold
  stored at fit. Requires a posterior variance, so ``poly`` and ``gbm``
  responses cannot return it — and say so through ``available_statuses``
  rather than being silently unable to.
- ``inside`` — the model is interpolating.

The threshold is **read**, never recomputed. Recomputing per prediction would
cost an evaluation over the whole training set on every call, and is flatly
impossible on a version fitted with ``keep_training_data=False``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from matofx.studies.coverage.envelope import _axis_contains, level_label

if TYPE_CHECKING:
    from collections.abc import Mapping

    from matofx.studies.coverage.envelope import Envelope, FactorEnvelope

SupportStatus = Literal["inside", "edge", "outside"]


@dataclass(frozen=True, slots=True)
class Support:
    """Whether a prediction sits inside what the model was trained on."""

    status: SupportStatus
    reason: str
    sigma: float | None = None
    """Relative posterior sigma at the point, or ``None`` for a method with no
    uncertainty. **Numeric on purpose** (§8.1): ADR-0017 output validity
    operates on numeric scalars and field arrays, never on status strings, so
    only a number can carry a declared maximum and flag a runaway
    extrapolation in a workflow nobody is watching."""
    available_statuses: tuple[str, ...] = ("inside", "outside")
    """Which verdicts this fit can actually produce.

    A response with no posterior variance never returns ``edge``, and saying
    so is better than leaving a caller to wonder why it never appears.

    **Empty** means none of them can — the model records no envelope, so
    ``status`` is a conservative placeholder rather than a measurement. A
    surface showing this to a person should show *nothing*; a validity rule
    should still act on the conservative answer."""

    def to_json_obj(self) -> dict[str, Any]:
        """The ``support`` block of a predict response."""
        return {
            "status": self.status,
            "sigma": self.sigma,
            "reason": self.reason,
            "available_statuses": list(self.available_statuses),
        }


def evaluate_support(
    envelope: Envelope,
    point: Mapping[str, Any],
    *,
    sigma: float | None = None,
    edge_threshold: float | None = None,
) -> Support:
    """Classify one prediction against the model's declared support (§8).

    ``sigma`` is the relative posterior sigma at ``point`` (see
    ``coverage.gp.relative_sigma``) and ``edge_threshold`` the value stored on
    the version at fit. Both ``None`` for a method with no posterior variance,
    in which case only ``inside`` / ``outside`` are reachable — which is the
    honest answer, not a degraded one: the envelope half is the half that
    catches the dangerous case.

    Never raises. A prediction guard that could throw would turn a caveat into
    an outage, so a malformed point is simply unsupportable — ``outside``.
    """
    available: tuple[str, ...] = (
        ("inside", "edge", "outside") if edge_threshold is not None else ("inside", "outside")
    )
    if not envelope.factors:
        # `outside` is the conservative answer, and the one an ADR-0017
        # validity rule should act on. But it is standing in for **"we cannot
        # tell"**, not for "your point is out of range" — finding 67's shape,
        # where "cannot be placed" was being counted as "outside the declared
        # range". Saying so structurally, on the field that already exists for
        # it, lets a surface with the full verdict draw nothing rather than
        # assert a red "Outside the trained range" nobody checked.
        return Support(
            status="outside",
            reason=(
                "This model doesn't record what range it was trained over, so it can't "
                "vouch for this prediction."
            ),
            sigma=sigma,
            available_statuses=(),
        )

    offending = _first_violation(envelope, point)
    if offending is not None:
        return Support(
            status="outside",
            reason=offending,
            sigma=sigma,
            available_statuses=available,
        )

    if edge_threshold is not None and sigma is not None and sigma > edge_threshold:
        return Support(
            status="edge",
            reason=(
                "This point is inside the range the model was trained over, but it sits "
                "further from the actual runs than most of them do — the model is less "
                "certain here than usual."
            ),
            sigma=sigma,
            available_statuses=available,
        )

    return Support(
        status="inside",
        reason="This point sits within the runs the model was trained on.",
        sigma=sigma,
        available_statuses=available,
    )


def _first_violation(envelope: Envelope, point: Mapping[str, Any]) -> str | None:
    """A plain-English reason the point is outside, or ``None``.

    Names the offending input. "Outside the range the model was trained on"
    without saying *which* input is not something a user can act on.
    """
    for factor in envelope.factors:
        if factor.name not in point:
            return (
                f"No value was given for '{factor.name}', which this model needs in "
                f"order to say whether the prediction is supported."
            )
        reason = _axis_violation(factor, point[factor.name])
        if reason is not None:
            return reason
    return None


def _axis_violation(factor: FactorEnvelope, value: Any) -> str | None:
    """Why ``value`` is outside ``factor``, or ``None`` if it is inside.

    **The decision is delegated, and only the wording lives here.** This
    function once re-implemented containment, and the two copies drifted: the
    envelope keyed categorical levels by :func:`level_label` (so a material
    record is the level ``"Copper"``) while this one keyed them by ``str()``
    (so the level was the whole dict). The guard then rejected 138 of 143 real
    training runs as values "this model never saw" — a model refusing to
    vouch for the very runs it was fitted on. One rule, one implementation.
    """
    if _axis_contains(factor, value):
        return None

    shown = level_label(value)
    if factor.kind == "pinned":
        return (
            f"'{factor.name}' was {level_label(factor.pinned_value)!r} in every run this "
            f"model learned from, so it can't predict for {shown!r}."
        )
    if factor.kind == "categorical":
        levels = factor.levels or ()
        return (
            f"This model never saw {shown!r} for '{factor.name}' "
            f"(it was trained on: {', '.join(levels) or 'nothing'})."
        )

    number = _as_float(value)
    if number is None:
        return f"'{factor.name}' needs to be a number to check against the trained range."
    return _out_of_range(factor, number)


def _out_of_range(factor: FactorEnvelope, number: float) -> str:
    """The out-of-range message, in the axis's own language."""
    span = f"{_fmt(factor.lo)} to {_fmt(factor.hi)}"
    if factor.kind == "derived":
        # A snapshot index, never seconds — the platform has no seconds to
        # quote, so it must not imply it does.
        return (
            f"This model was trained on snapshots {span}; {_fmt(number)} is outside "
            f"that, so predicting there is extrapolation."
        )
    return (
        f"'{factor.name}' is {_fmt(number)}, outside the {span} range this model was "
        f"trained to be trusted over."
    )


def _fmt(value: float | None) -> str:
    """A bound as short user-facing text."""
    if value is None:
        return "?"
    return str(int(value)) if float(value).is_integer() else f"{value:.4g}"


def _as_float(value: Any) -> float | None:
    """``float(value)`` for a real number (not bool / string), else ``None``."""
    import math  # noqa: PLC0415

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


__all__ = ["Support", "SupportStatus", "evaluate_support"]
