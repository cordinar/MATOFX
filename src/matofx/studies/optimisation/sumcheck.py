"""Sum-check-aware optimisation — keep a descriptor sum-check exact (ADR-0009 §3.6).

A tool descriptor can declare a cross-field **sum-check**: a set of numeric
inputs that must total a fixed value (e.g. three component percentages = 100).
When those inputs are *varied* by the optimiser they must still satisfy the
sum on every evaluated candidate — but a sum is an **equality** constraint, and
the optimiser's constraint mechanism is reject-and-retry, which can never hit
an equality exactly.

The fix is to search the group's **true degrees of freedom**: one member (the
*balance*) is **derived**, not searched — the strategy varies the other members
and the balance is worked out as ``value - sum(others)``. The balance is removed
from the search space at study load (like MOP variable-freezing), so the
optimiser never wastes effort exploring the redundant direction the rule
removes. Which member is derived is chosen once, by a heuristic (the
widest-range member, so it can always absorb the residual) — the choice doesn't
affect *which* blends are reachable, only how cleanly they sample.

This module is pure (no DB, no NiceGUI): the group records it operates on are
built at ``configure()`` time and stored in ``config_json["sum_groups"]``; the
runtime fills in the derived member on the seed (and each batch) before a
candidate is evaluated, and strategies do the same in ``decode``.

A *group record* (the stored shape) is::

    {
        "value": 100.0,
        "members": [
            {"factor": "<axis key>", "input_key": "<member.input|name>",
             "role": "varied" | "fixed", "fixed_value": <number|None>},
            ...
        ],
        "balance": "<axis key>",        # the derived member
        "balance_kind": "float"|"int",  # its bounds, so the derive needs no
        "balance_low": <number|None>,   #   factor-spec map at runtime
        "balance_high": <number|None>,
    }

The *axis key* is the strategy's factor key: the input name for a tool target,
``member.input`` for a workflow target.
"""

from __future__ import annotations

import math
from collections.abc import Mapping  # runtime: used in isinstance checks
from typing import TYPE_CHECKING, Any, Protocol

from matofx.studies._intsnap import snap_int

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# Tolerance for "the residual lands exactly on a bound / an integer". Factor
# values are O(1) to O(1e3) here, so 1e-9 is comfortably below any real step.
_EPS = 1e-9


class _Bounded(Protocol):
    """The factor facts the repair needs — satisfied by ``FactorSpec`` / ``_Bounds``.

    Read-only (``@property``) so frozen dataclasses satisfy it structurally.
    """

    @property
    def kind(self) -> str: ...
    @property
    def low(self) -> float | None: ...
    @property
    def high(self) -> float | None: ...


def pick_balance_heuristic(varied_keys: Sequence[str], specs: Mapping[str, _Bounded]) -> str | None:
    """Default balance member: the widest-range varied member (deterministic).

    A wide range gives the balance the most room to absorb the residual without
    hitting a clamp. A float is preferred over an integer (it can absorb a
    fractional residual exactly); ties break on the widest range, then the name
    for stability. ``None`` when there are no varied members to balance.
    """

    def _sort_key(key: str) -> tuple[int, float, str]:
        spec = specs.get(key)
        width = (
            float(spec.high) - float(spec.low)
            if spec is not None and spec.low is not None and spec.high is not None
            else 0.0
        )
        float_first = 0 if (spec is not None and spec.kind == "float") else 1
        return (float_first, -width, key)

    return sorted(varied_keys, key=_sort_key)[0] if varied_keys else None


def derive_balance(  # noqa: PLR0911 — one tightly-coupled feasibility decision
    candidate: Mapping[str, Any],
    group: Mapping[str, Any],
    *,
    frozen: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Compute the group's *derived* (balance) member so the sum is exact (§3.6).

    The derived member is **not** a search variable — the strategy varies the
    other members and this works it out as ``value - sum(others)``, where an
    "other" is its ``fixed_value`` (held members), its MOP-frozen nominal
    (``frozen`` wins), or the candidate's own value (varied members). The
    derived value is accepted only when it lands inside its own ``[low, high]``
    box (and is integral for an integer member); otherwise the candidate is
    **infeasible** (the caller drops it) — clamping would silently break the
    sum. The member's bounds are stored on the group record
    (``balance_kind`` / ``balance_low`` / ``balance_high``) so this needs no
    factor-spec map.

    Returns ``(new_candidate, feasible)``; ``new_candidate`` is always a fresh
    dict (never mutates the input).
    """
    new = dict(candidate)
    balance = group.get("balance")
    if balance is None:
        # Nothing varies in this group — the held/frozen members alone must
        # already total the value, else there's no way to satisfy it.
        total = _sum_members(group, candidate, frozen, skip=None)
        return new, abs(total - float(group["value"])) <= _EPS

    residual = float(group["value"]) - _sum_members(group, candidate, frozen, skip=balance)
    if math.isnan(residual):
        return new, False
    lo = group.get("balance_low")
    hi = group.get("balance_high")
    lo = float(lo) if lo is not None else None
    hi = float(hi) if hi is not None else None

    if group.get("balance_kind") == "int":
        # Must be an exact integer already; rounding would break the sum.
        if abs(residual - round(residual)) > _EPS:
            return new, False
        value = snap_int(residual, lo, hi)
        if abs(value - residual) > _EPS:  # the clamp moved it → sum broken
            return new, False
        new[balance] = value
        return new, True

    if (lo is not None and residual < lo - _EPS) or (hi is not None and residual > hi + _EPS):
        return new, False
    new[balance] = residual
    return new, True


def _sum_members(
    group: Mapping[str, Any],
    candidate: Mapping[str, Any],
    frozen: Mapping[str, Any],
    *,
    skip: str | None,
) -> float:
    """Σ of a group's members except ``skip`` (the balance), or NaN if a value is missing."""
    total = 0.0
    for member in group.get("members") or []:
        key = member.get("factor")
        if key is None or key == skip:
            continue
        if member.get("role") == "fixed":
            total += float(member.get("fixed_value") or 0.0)
            continue
        raw = frozen.get(key, candidate.get(key))
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            return math.nan
        total += float(raw)
    return total


def apply_sum_derivation(
    candidates: Iterable[Mapping[str, Any]],
    sum_groups: Sequence[Mapping[str, Any]],
    frozen: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Add every group's derived member to each candidate; drop the infeasible ones.

    Used on the seed (and as an idempotent safety net on each batch): the
    strategy supplies the free members and this fills in the derived one.
    Groups are assumed disjoint and applied in order.
    """
    out: list[dict[str, Any]] = []
    for candidate in candidates:
        derived = dict(candidate)
        feasible = True
        for group in sum_groups:
            derived, ok = derive_balance(derived, group, frozen=frozen)
            if not ok:
                feasible = False
                break
        if feasible:
            out.append(derived)
    return out


def sum_check_notes(
    inputs: Sequence[Mapping[str, Any]], *, prefix: str = ""
) -> list[tuple[float, list[str]]]:
    """Distinct sum-check groups in a descriptor's inputs → ``(value, [labels])``.

    For the wizard's informational banner: a group is declared on every member
    input, so de-duplicate by ``(value, members)``. Each member is labelled by
    its field label; ``prefix`` (e.g. ``"Heat Transfer · "``) qualifies it for a
    workflow member.
    """
    by_name = {str(i.get("name")): i for i in inputs if i.get("name")}
    seen: set[tuple[Any, ...]] = set()
    notes: list[tuple[float, list[str]]] = []
    for field in inputs:
        sc = field.get("sum_check")
        if not isinstance(sc, Mapping):
            continue
        params = tuple(str(p) for p in (sc.get("sum_parameters") or []))
        signature = (sc.get("value"), params)
        if signature in seen:
            continue
        seen.add(signature)
        labels = [f"{prefix}{by_name[p].get('label') or p}" for p in params if p in by_name]
        if labels:
            notes.append((float(sc.get("value") or 0.0), labels))
    return notes


def project_sum_groups(
    candidate: Mapping[str, Any],
    sum_groups: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Add each group's derived member to a single candidate (never drops).

    Used inside a strategy's ``decode``: the strategy varies only the free
    members, so its decoded candidate gets the derived one filled in here. The
    point it then proposes already satisfies every sum, so it matches what the
    runtime evaluates and tells back (load-bearing for the pymoo generational
    ask/tell). An infeasible group leaves the candidate without the derived
    member; the runtime then drops it (the intended steer away from that
    corner).
    """
    out = dict(candidate)
    for group in sum_groups:
        out, _feasible = derive_balance(out, group, frozen={})
    return out


__all__ = [
    "apply_sum_derivation",
    "derive_balance",
    "pick_balance_heuristic",
    "project_sum_groups",
    "sum_check_notes",
]
