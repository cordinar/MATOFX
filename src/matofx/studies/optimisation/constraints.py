"""Selection-time constraint filtering (ADR-0009 §7).

Two of the three constraint kinds are knowable *before* a candidate runs:

- **box** (`factor min/max`) — guaranteed by construction (strategies +
  the LHS seed never propose outside the factor box), so there is
  nothing to re-check here.
- **cross_field** (`a + b <= 100`) — evaluated over the candidate's
  factor values via the descriptor wizard's ``safe_expr`` engine; a
  violator is dropped before it is ever queued.

The third kind — **output** constraints (`max_temperature <= 85`) — can't
be known until the job runs, so it is evaluated at ``tell`` time, not
here; that lands with the multi-objective build (Phase 4). A candidate
whose ``cross_field`` predicate references a value it doesn't carry is
treated as "not selectable" (``False``), never an error (mirrors the
cohort membership filter).
"""

from __future__ import annotations

import operator
from collections.abc import Mapping
from typing import Any

from matofx.studies.optimisation.strategies.base import Candidate, ResponseVector

_OPS = {
    "<=": operator.le,
    "<": operator.lt,
    ">=": operator.ge,
    ">": operator.gt,
    "==": operator.eq,
    "!=": operator.ne,
}

_INFEASIBLE_G = 1.0e9
"""Large positive constraint value for an output whose response is missing
(a failed child) — pymoo reads ``g > 0`` as infeasible."""


def constraints_ok(candidate: Candidate, config: Mapping[str, Any]) -> bool:
    """Whether ``candidate`` passes every selection-time constraint (§7)."""
    from matofx.tools.forms import safe_expr  # noqa: PLC0415

    for constraint in config.get("constraints") or []:
        if not isinstance(constraint, Mapping) or constraint.get("kind") != "cross_field":
            continue  # box: by construction; output: evaluated at tell time
        expr = constraint.get("expr")
        if not isinstance(expr, str):
            continue
        try:
            if not bool(safe_expr.evaluate_condition(expr, candidate)):
                return False
        except safe_expr.UnsafeExpressionError:
            # A predicate referencing a value this candidate lacks is
            # "not selectable", not an error.
            return False
    return True


def output_constraints(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The declared ``output`` constraints, in order (§7, tell-time)."""
    return [
        c
        for c in config.get("constraints") or []
        if isinstance(c, Mapping) and c.get("kind") == "output"
    ]


def output_feasible(responses: ResponseVector, config: Mapping[str, Any]) -> bool:
    """Whether ``responses`` satisfy every ``output`` constraint (§7).

    A response a constraint names but the run didn't produce (a failed
    child) counts as a violation.
    """
    for c in output_constraints(config):
        op = _OPS.get(str(c.get("op")))
        value = responses.get(str(c.get("response")))
        if op is None:
            continue
        if value is None or not op(float(value), float(c["value"])):
            return False
    return True


def output_constraint_g(responses: ResponseVector, config: Mapping[str, Any]) -> list[float]:
    """Per-output constraint values in pymoo's ``g(x) ≤ 0`` convention (§7).

    Order matches :func:`output_constraints`. ``<=`` / ``<`` give
    ``response - limit``; ``>=`` / ``>`` give ``limit - response``; ``==``
    gives ``abs(response - limit)``; ``!=`` gives ``-abs(response - limit)``.
    A missing response yields a large positive (infeasible) value.
    """
    out: list[float] = []
    for c in output_constraints(config):
        value = responses.get(str(c.get("response")))
        if value is None:
            out.append(_INFEASIBLE_G)
            continue
        r = float(value)
        limit = float(c["value"])
        op = str(c.get("op"))
        if op in ("<=", "<"):
            out.append(r - limit)
        elif op in (">=", ">"):
            out.append(limit - r)
        elif op == "==":
            out.append(abs(r - limit))
        else:  # "!="
            out.append(-abs(r - limit))
    return out
