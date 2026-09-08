"""Tests for the shared integer-snap helper (ADR-0009 amendment 2026-06-15).

Pins the round-half-**away-from-zero** rule and the ceil/floor clamp that every
automated sampler funnels integer values through. See
``designs/integer-parameter-handling.md``.
"""

from __future__ import annotations

import pytest

from matofx.studies._intsnap import snap_int, snap_precision


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (2.0, 2),
        (2.4, 2),
        (2.5, 3),  # half away from zero (not banker's 2)
        (2.6, 3),
        (3.5, 4),
        (-2.4, -2),
        (-2.5, -3),  # symmetric: away from zero
        (-3.5, -4),
        (0.5, 1),
        (-0.5, -1),
        (0.0, 0),
    ],
)
def test_round_half_away_from_zero(value: float, expected: int) -> None:
    assert snap_int(value) == expected
    assert isinstance(snap_int(value), int)


def test_clamps_into_bounds() -> None:
    assert snap_int(12.0, 0, 10) == 10
    assert snap_int(-5.0, 0, 10) == 0
    assert snap_int(5.0, 0, 10) == 5


def test_unbounded_sides() -> None:
    assert snap_int(7.5, lo=0) == 8  # no upper bound
    assert snap_int(7.5, hi=100) == 8  # no lower bound
    assert snap_int(7.5) == 8  # neither


def test_fractional_bounds_are_defended() -> None:
    # ceil(lo)/floor(hi) keep the result a whole number strictly inside a
    # fractional bound (factor bounds are validated integral, but descriptor
    # bounds reach _coerce and must not leak a fraction).
    assert snap_int(1.4, 1.9, 10.5) == 2  # raised to ceil(1.9)
    assert snap_int(11.0, 1.9, 10.5) == 10  # lowered to floor(10.5)


def test_already_integer_passthrough() -> None:
    for v in (-3, 0, 7, 100):
        assert snap_int(float(v), -1000, 1000) == v


# --- float inputs snap onto their declared precision -----------------------


def test_snap_precision_honours_the_declared_step() -> None:
    """A tool declaring one decimal must not be handed fifteen."""
    assert snap_precision(24.606065714271846, 0.1) == 24.6
    assert snap_precision(7.925333126177183, 0.1) == 7.9
    assert snap_precision(0.123456, 0.01) == 0.12
    assert snap_precision(59.57064555417492, 1.0) == 60.0


def test_snap_precision_uses_the_step_not_just_decimals() -> None:
    """``0.5`` snaps to halves, not to one decimal place."""
    assert snap_precision(3.7, 0.5) == 3.5
    assert snap_precision(3.8, 0.5) == 4.0


def test_snap_precision_sheds_binary_float_dust() -> None:
    """The product of a step and a count must not carry 0.30000000000000004 tails."""
    for i in range(1, 40):
        value = snap_precision(i * 0.1 + 1e-12, 0.1)
        assert value == round(value, 1)


def test_snap_precision_steps_inward_at_a_bound() -> None:
    """Snapping must never land outside the declared range."""
    assert snap_precision(2.04, 0.1, 2.1, 15.0) == 2.1  # up to the first legal step
    assert snap_precision(15.06, 0.1, 2.0, 15.0) == 15.0  # down to the last one


def test_snap_precision_passes_through_without_a_declared_step() -> None:
    """No precision declared means the tool has not told us its resolution."""
    assert snap_precision(5.123456789, None) == 5.123456789
    assert snap_precision(5.123456789, 0.0) == 5.123456789
