"""Smoke tests for the descriptor wizard's unit catalogue.

The wizard's per-input and per-output unit picker draws from
:data:`matofx.ui.units.UNIT_OPTIONS`. These tests pin down its
shape so a future edit can't accidentally drop a sentinel or
introduce a duplicate value (which would break the edit-mode
round-trip — :func:`state_from_descriptor` looks up the dropdown
value by stored unit string).
"""

from __future__ import annotations

from matofx.ui.units import (
    UNIT_CUSTOM_VALUE,
    UNIT_NONE_VALUE,
    UNIT_OPTIONS,
    is_known_unit,
)


def test_sentinels_are_present_and_first() -> None:
    """``No unit`` and ``Custom unit…`` appear before the catalogue entries."""
    keys = list(UNIT_OPTIONS.keys())
    assert keys[0] == UNIT_NONE_VALUE
    assert keys[1] == UNIT_CUSTOM_VALUE
    assert UNIT_OPTIONS[UNIT_NONE_VALUE] == "No unit"
    assert "Custom" in UNIT_OPTIONS[UNIT_CUSTOM_VALUE]


def test_no_duplicate_values() -> None:
    """Every dropdown value is unique — round-trip lookup needs that."""
    values = list(UNIT_OPTIONS.keys())
    assert len(values) == len(set(values))


def test_known_engineering_units_present() -> None:
    """A handful of canonical units must appear; safety net against accidental drops."""
    canonicals = [
        "m",
        "mm",
        "kg",
        "K",
        "°C",
        "MPa",
        "Pa·s",
        "N·m",
        "J/(kg·K)",
        "W/(m·K)",
        "mol/L (M)",
        "rpm",
        "m/s²",
        "Bq",
        "dB",
    ]
    missing = [u for u in canonicals if u not in UNIT_OPTIONS]
    assert not missing, f"missing canonical units: {missing}"


def test_labels_carry_category_prefix_for_real_units() -> None:
    """Catalogue entries have ``Category: unit`` labels; sentinels are plain."""
    for value, label in UNIT_OPTIONS.items():
        if value in (UNIT_NONE_VALUE, UNIT_CUSTOM_VALUE):
            continue
        assert ":" in label, f"unit {value!r} label missing category prefix: {label!r}"


def test_is_known_unit_filters_sentinels() -> None:
    """``is_known_unit`` rejects sentinels + ``None`` but accepts catalogue entries."""
    assert is_known_unit("MPa") is True
    assert is_known_unit("m") is True
    assert is_known_unit(UNIT_NONE_VALUE) is False
    assert is_known_unit(UNIT_CUSTOM_VALUE) is False
    assert is_known_unit(None) is False
    assert is_known_unit("furlongs/fortnight") is False
