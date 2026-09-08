"""Stopping-criteria evaluation (ADR-0009 §8).

``config.stopping_criteria`` is an ordered list; **any** kind firing
stops the study, evaluated after every batch in declared order.
:func:`evaluate_stopping` returns the first matching reason (a
``stop_reason`` value) or ``None`` to keep going.

Implements ``max_runs`` (mandatory), ``target``, ``wall_clock``, and
``convergence`` over either the single-objective best-so-far or the
multi-objective hypervolume (the runtime writes whichever is meaningful
into the trace ``metric``; the convergence test is the same trailing-
window check for both).
"""

from __future__ import annotations

import operator
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from matofx.studies.optimisation.strategies import ResponseVector

_OPS = {
    "<=": operator.le,
    "<": operator.lt,
    ">=": operator.ge,
    ">": operator.gt,
    "==": operator.eq,
    "!=": operator.ne,
}


def evaluate_stopping(
    config: Mapping[str, Any],
    *,
    total: int,
    best: ResponseVector | None,
    trace: Sequence[Mapping[str, Any]],
    elapsed_s: float,
) -> str | None:
    """Return the first firing stop reason (§8), or ``None`` to continue.

    ``total`` is the count of terminal children so far; ``best`` is the
    strategy's best-so-far response vector (``None`` before any feasible
    result); ``trace`` is the appended convergence trace; ``elapsed_s``
    is wall-clock since the first batch.
    """
    for criterion in config.get("stopping_criteria") or []:
        if not isinstance(criterion, Mapping):
            continue
        kind = criterion.get("kind")
        if kind == "max_runs":
            if total >= int(criterion["value"]):
                return "budget_exhausted"
        elif kind == "target":
            if best is not None and _target_met(best, criterion):
                return "target_met"
        elif kind == "wall_clock":
            if elapsed_s >= float(criterion["hours"]) * 3600.0:
                return "wall_clock"
        elif (
            kind == "convergence"
            and criterion.get("metric") in {"best_so_far", "hypervolume"}
            and _converged(trace, criterion)
        ):
            return "converged"
    return None


def _target_met(best: ResponseVector, criterion: Mapping[str, Any]) -> bool:
    """Whether the feasible best satisfies a ``target`` criterion."""
    response = criterion.get("response")
    value = best.get(response) if isinstance(response, str) else None
    op = _OPS.get(str(criterion.get("op")))
    if value is None or op is None:
        return False
    return bool(op(float(value), float(criterion["value"])))


def _converged(trace: Sequence[Mapping[str, Any]], criterion: Mapping[str, Any]) -> bool:
    """Single-objective best-so-far convergence over a trailing window (§8).

    Fires when the best-so-far metric's absolute improvement across the
    last ``window`` batches is below ``epsilon``. Needs ``window + 1``
    non-null metric points before it can fire.
    """
    window = int(criterion.get("window", 0))
    epsilon = float(criterion.get("epsilon", 0.0))
    if window < 1:
        return False
    values = [p["metric"] for p in trace if p.get("metric") is not None]
    if len(values) < window + 1:
        return False
    recent = values[-(window + 1) :]
    return abs(float(recent[0]) - float(recent[-1])) < epsilon
