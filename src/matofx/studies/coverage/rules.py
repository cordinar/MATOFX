"""A suggested run has to be one the tool would accept (ADR-0020 §5.2).

**The suggestion path knew nothing about the tool's own input rules.** It
sampled each varying axis inside its declared bounds, independently, and
proposed the result. A descriptor can say a great deal more than "each input
lies in this box": ``set_value`` forces, formula-driven bounds over sibling
inputs, ``require`` validity rules, cross-field constraints, and **sum-checks**
— a set of inputs that must total a fixed value.

Measured on ``material_properties``, whose three percentages must total 100:
**0 of 8** proposed runs were submittable. Every one summed to something else
(83.8, 117.1, 71.0, …). The batch was shown to the user run by run, priced, and
committed — and the generator then dropped every point as invalid.

Two rules, applied in this order, because they are different kinds of thing.

**A sum-check is repaired, not rejected.** It is an *equality*: filtering random
candidates against it rejects essentially all of them, which would turn a broken
feature into an empty one. The fix is the one ADR-0009 §3.6 already made for the
optimiser — search the group's **true degrees of freedom**, deriving one member
(the *balance*) as ``value - sum(others)`` instead of sampling it. This module
reuses that machinery rather than restating it; a second implementation of "what
does this sum-check mean" is the drift §3's F1 is about.

**Everything else is rejected.** Bounds, forces and validity rules are not
equalities and a candidate either satisfies them or does not.
:func:`resolve_for_generation` is the check, and it is the **same call the study
generator makes** on the same point — so a batch that passes here cannot be
dropped there, which is the failure this module exists to close.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from matofx.tools.descriptor import ToolDescriptor

_MIN_VARIED_MEMBERS = 2
"""Members a sum group needs before deriving one of them makes sense.

With one varied member the sum fixes it outright — there is no design freedom
left, and the axis should not have been sampled in the first place."""


def sum_groups_for(
    descriptor: ToolDescriptor | None, varying: Sequence[str]
) -> list[dict[str, Any]]:
    """Sum-check groups over the axes currently being varied.

    Built in the shape ``matofx.studies.optimisation.sumcheck`` already
    consumes, so its ``derive_balance`` / ``apply_sum_derivation`` can be used
    unchanged. Members that are *not* varying are carried as ``fixed`` at their
    own value where the descriptor pins one, since they still count towards the
    total.

    Empty for a descriptor with no sum-check, which is the overwhelming
    majority — this whole pass then costs one attribute lookup.
    """
    if descriptor is None:
        return []

    varied = set(varying)
    fields = {f.name: f for f in descriptor.inputs}
    groups: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()

    for field in descriptor.inputs:
        check = getattr(field, "sum_check", None)
        if check is None:
            continue
        members = [str(m) for m in (getattr(check, "sum_parameters", None) or ())]
        key = tuple(sorted(members))
        if not members or key in seen:
            continue
        seen.add(key)

        varied_members = [m for m in members if m in varied]
        if len(varied_members) < _MIN_VARIED_MEMBERS:
            # Nothing to balance: with one free member the sum determines it,
            # and with none the group is already satisfied by the constants.
            continue

        balance = _pick_balance(varied_members, fields)
        if balance is None:
            continue
        spec = fields.get(balance)
        groups.append(
            {
                "value": float(getattr(check, "value", 0.0) or 0.0),
                "members": [
                    {
                        "factor": m,
                        "input_key": m,
                        "role": "varied" if m in varied else "fixed",
                        "fixed_value": None if m in varied else _default_of(fields.get(m)),
                    }
                    for m in members
                ],
                "balance": balance,
                "balance_kind": "int" if _kind_of(spec) == "integer" else "float",
                "balance_low": _bound_of(spec, "minimum"),
                "balance_high": _bound_of(spec, "maximum"),
            }
        )
    return groups


def feasible_points(
    descriptor: ToolDescriptor | None,
    points: Sequence[Mapping[str, Any]],
    *,
    varying: Sequence[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """``(points the tool would accept, {reason: how many were dropped})``.

    Sum-checks repaired first, then every other rule enforced by the same call
    the generator makes. A point that survives is returned **resolved** — with
    any ``set_value`` forces written in — because that is what would actually
    run, and showing the user something else would be showing them a run they
    are not about to make.
    """
    if descriptor is None or not points:
        return [dict(p) for p in points], {}

    from matofx.studies.optimisation.sumcheck import (  # noqa: PLC0415
        apply_sum_derivation,
    )
    from matofx.tools.forms.model import resolve_for_generation  # noqa: PLC0415

    dropped: dict[str, int] = {}
    groups = sum_groups_for(descriptor, varying)

    candidates: list[Mapping[str, Any]] = [dict(p) for p in points]
    if groups:
        before = len(candidates)
        candidates = list(apply_sum_derivation(candidates, groups, frozen={}))
        if before > len(candidates):
            dropped["The balancing input would fall outside its own range."] = before - len(
                candidates
            )

    kept: list[dict[str, Any]] = []
    for candidate in candidates:
        resolved, errors = resolve_for_generation(descriptor, candidate)
        if errors:
            reason = errors[0].message
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        kept.append(dict(resolved))
    return kept, dropped


def _pick_balance(varied: Sequence[str], fields: Mapping[str, Any]) -> str | None:
    """Which member to derive — the optimiser's own heuristic, reused."""
    from matofx.studies.optimisation.sumcheck import (  # noqa: PLC0415
        pick_balance_heuristic,
    )

    specs = {
        name: _BoundedSpec(
            kind=_kind_of(fields.get(name)),
            low=_bound_of(fields.get(name), "minimum"),
            high=_bound_of(fields.get(name), "maximum"),
        )
        for name in varied
    }
    return pick_balance_heuristic(list(varied), specs)


class _BoundedSpec:
    """The three attributes ``pick_balance_heuristic`` reads, and no more."""

    __slots__ = ("high", "kind", "low")

    def __init__(self, *, kind: str, low: float | None, high: float | None) -> None:
        self.kind = "float" if kind != "integer" else "int"
        self.low = low
        self.high = high


def _kind_of(field: Any) -> str:
    """``"integer"`` or ``"float"`` for a descriptor input."""
    return "integer" if str(getattr(field, "type", "")) == "integer" else "float"


def _bound_of(field: Any, which: str) -> float | None:
    """A literal ``minimum`` / ``maximum``, or ``None``."""
    value = getattr(field, which, None)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _default_of(field: Any) -> float | None:
    """A non-varying member's own value, so it still counts towards the total."""
    value = getattr(field, "default", None)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["feasible_points", "sum_groups_for"]
