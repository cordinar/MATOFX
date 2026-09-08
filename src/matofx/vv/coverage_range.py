"""Coverage fills in the V&V applicable range (ADR-0020 §10).

``vv_record.applicable_range_json`` is authored by hand today and purely
documentary — a curator's assertion about what a validation covers, which
nothing checks. A surrogate model version already carries a **declared
envelope**: the same object the predict-time guard (§8) enforces on every
prediction. Offering it as the applicable range turns the field from an
assertion into the thing the platform already tests against, and closes the
loop with ``VVService.resolve_envelope_warning``, which soft-warns at submit
when a job's inputs fall outside an approved validation's envelope.

**A pre-fill, never a replacement.** The curator can widen it, narrow it or
ignore it; the point is that they start from what the model actually supports
rather than from an empty box.

**The two schemas do not match, and that is the interesting part.** The V&V
column is ``{factor: {"min", "max"}}`` — numeric bounds only. An envelope also
carries categorical levels and non-numeric pinned values, which those bounds
cannot express. Dropping them silently would leave the V&V record claiming a
**wider** applicability than the guard enforces — the exact inversion this
whole feature exists to prevent — so they are named in the note instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

_NUMERIC_KINDS = frozenset({"continuous", "integer", "derived"})
"""Envelope kinds a ``{"min", "max"}`` pair can honestly represent.

``derived`` is included deliberately: a field validated over snapshots 0-5
does not cover snapshot 500, and the V&V claim should not imply that it does.
"""


@dataclass(frozen=True, slots=True)
class SuggestedApplicableRange:
    """What coverage can offer the V&V form, and what it could not carry."""

    ranges: dict[str, dict[str, float]] = field(default_factory=dict)
    """``{factor: {"min", "max"}}`` — exactly the shape the column stores and
    ``resolve_envelope_warning`` already reads."""

    unexpressible: tuple[str, ...] = ()
    """Factors the model constrains but a min/max pair cannot express.

    Named rather than dropped: a validation that silently omits "cfrp or
    gfrp, and nothing else" claims to cover materials the model has never
    seen."""

    note: str = ""
    """Plain-English provenance, so a curator reading this a year later can
    tell a pre-filled range from one somebody typed — and can see what the
    pre-fill could not include."""

    source_model_version_id: int | None = None


def applicable_range_from_report(
    report: Mapping[str, Any] | None,
    *,
    model_version_id: int | None = None,
) -> SuggestedApplicableRange:
    """Offer a stored coverage report's envelope as a V&V applicable range.

    ``report`` is the ``coverage_report_json`` stored on a surrogate model
    version (§9). ``None`` or empty — a version fitted before coverage existed
    — offers nothing at all rather than an empty range, which would read as
    "validated everywhere".
    """
    if not report:
        return SuggestedApplicableRange()

    ranges: dict[str, dict[str, float]] = {}
    unexpressible: list[str] = []
    described: list[str] = []

    envelope = report.get("envelope")
    if not isinstance(envelope, list | tuple):
        # A malformed stored report offers nothing rather than raising on
        # a read path; the caller then pre-fills nothing, which is right.
        return SuggestedApplicableRange()
    for axis in envelope:
        if not isinstance(axis, dict):
            continue
        name = str(axis.get("name") or "")
        if not name:
            continue
        kind = str(axis.get("kind") or "continuous")
        bounds = _numeric_bounds(axis, kind)
        if bounds is not None:
            ranges[name] = bounds
            continue
        unexpressible.append(name)
        described.append(_describe(name, axis, kind))

    return SuggestedApplicableRange(
        ranges=ranges,
        unexpressible=tuple(unexpressible),
        note=_note(report, described),
        source_model_version_id=model_version_id,
    )


def _numeric_bounds(axis: Mapping[str, Any], kind: str) -> dict[str, float] | None:
    """The ``{"min", "max"}`` pair for one axis, or ``None`` if it has none.

    A **pinned numeric** input becomes a zero-width range. That is not a
    degenerate case to be skipped: the validation covers exactly that value
    and no other, and ``[v, v]`` says so in the only vocabulary the column
    has.
    """
    if kind == "pinned":
        value = _as_float(axis.get("pinned_value"))
        return None if value is None else {"min": value, "max": value}
    if kind not in _NUMERIC_KINDS:
        return None
    lo = _as_float(axis.get("lo"))
    hi = _as_float(axis.get("hi"))
    if lo is None or hi is None:
        return None
    return {"min": lo, "max": hi}


def _describe(name: str, axis: Mapping[str, Any], kind: str) -> str:
    """How to say, in the note, what this axis constrains."""
    if kind == "pinned":
        return f"{name} was always {axis.get('pinned_value')!r}"
    levels = [str(v) for v in (axis.get("levels") or [])]
    if levels:
        return f"{name} only covers {', '.join(levels)}"
    return f"{name} is limited but can't be written as a range"


def _note(report: Mapping[str, Any], described: list[str]) -> str:
    """The provenance line, plus whatever the min/max schema could not carry."""
    stamp = str(report.get("computed_at") or "")[:10]
    dated = f" on {stamp}" if stamp else ""
    verdict = str(report.get("verdict") or "")
    quality = f" (coverage read {verdict})" if verdict else ""
    note = (
        f"Filled in from the range this model was measured for coverage over{dated}"
        f"{quality}. Adjust it if this validation covers something narrower."
    )
    if described:
        count = len(described)
        lead = (
            " One thing a range can't capture, so check it yourself: "
            if count == 1
            else f" {count} things a range can't capture, so check them yourself: "
        )
        note += lead + "; ".join(described) + "."
    return note


def _as_float(value: Any) -> float | None:
    """``float(value)`` for a real number (not bool / string), else ``None``."""
    import math  # noqa: PLC0415

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


__all__ = ["SuggestedApplicableRange", "applicable_range_from_report"]
