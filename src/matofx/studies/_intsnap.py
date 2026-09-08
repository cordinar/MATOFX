"""Value snapping for automated sampling (ADR-0009 amendment 2026-06-15).

The single place an integer-typed parameter's value is produced from a
continuous draw, so every sampler — parametric sweep, LHS/cohort seed, and
every optimisation strategy — snaps the same way. The rule is **round half
away from zero** (``2.5 -> 3``, ``-2.5 -> -3``): what a user expects reading a
result, and symmetric about zero so a range straddling it has no directional
bias. See ``designs/integer-parameter-handling.md``.
"""

from __future__ import annotations

import math


def snap_int(value: float, lo: float | None = None, hi: float | None = None) -> int:
    """Round ``value`` half away from zero, then clamp into ``[lo, hi]``.

    The clamp uses ``ceil(lo)`` / ``floor(hi)`` so the result is always a whole
    number inside the inclusive bound even if the bound itself is fractional — a
    defensive belt; optimisation factor bounds are validated integral upstream
    (``_validate_numeric_factor``). Either bound may be ``None`` for an
    unbounded side.
    """
    v = float(value)
    nearest = math.floor(v + 0.5) if v >= 0 else math.ceil(v - 0.5)
    iv = int(nearest)
    if lo is not None:
        iv = max(iv, math.ceil(lo))
    if hi is not None:
        iv = min(iv, math.floor(hi))
    return iv


def snap_precision(
    value: float, precision: float | None, lo: float | None = None, hi: float | None = None
) -> float:
    """Snap ``value`` onto the input's declared ``precision`` step, inside bounds.

    A tool that declares ``precision = 0.1`` is telling us it has one decimal of
    resolution. Generating ``24.606065714271846`` for it is noise: the tool cannot
    act on the extra digits, and every downstream surface — the study matrix, the
    coverage plots, the job record — then quotes a number the tool never really
    had. So the sampler snaps to the step, exactly as an integer input is rounded.

    The step is honoured for any positive ``precision`` (``0.5`` snaps to halves,
    not to one decimal), and the product is re-rounded to the step's own decimal
    places to shed binary-float dust (``0.1 * 246`` is not ``24.6``).

    Snapping can push a value a fraction outside a bound, so the clamp steps
    *inward* to the nearest representable multiple — ``ceil`` at the bottom,
    ``floor`` at the top — mirroring :func:`snap_int`. ``None`` precision (or a
    non-positive one) returns the value untouched.
    """
    v = float(value)
    if precision is None or precision <= 0.0 or not math.isfinite(v):
        return v
    digits = max(0, min(12, -math.floor(math.log10(precision)))) if precision < 1.0 else 0
    snapped = round(round(v / precision) * precision, digits)
    if lo is not None and snapped < lo:
        snapped = round(math.ceil(lo / precision) * precision, digits)
    if hi is not None and snapped > hi:
        snapped = round(math.floor(hi / precision) * precision, digits)
    return snapped


__all__ = ["snap_int", "snap_precision"]
