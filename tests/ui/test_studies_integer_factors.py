"""Parametric-study factor builders keep integer inputs whole (Phase D parity).

Pure — no NiceGUI runtime, no DB. The build/collect helpers only read ``.value``
off their widgets, so ``SimpleNamespace`` stands in. An integer descriptor input
(``ftype == "integer"``) must never carry a fractional bound / value through to the
study spec, even if the form somehow held one. See
``designs/integer-parameter-handling.md``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from matofx.ui.pages.studies import (
    _build_design_vary,
    _build_factor_spec,
    _build_grid_vary,
    _build_sampling_vary,
    _dedupe_numeric,
    _snap_int_value,
)


def _w(value: Any) -> SimpleNamespace:
    return SimpleNamespace(value=value)


def test_snap_int_value_only_snaps_integer_factors() -> None:
    assert _snap_int_value(10.6, {"ftype": "integer"}) == 11
    assert _snap_int_value(-2.5, {"ftype": "integer"}) == -3  # half away from zero
    assert _snap_int_value(10.6, {"ftype": "float"}) == 10.6  # untouched
    assert _snap_int_value("cu", {"ftype": "select"}) == "cu"  # non-numeric untouched


def test_dedupe_numeric_preserves_order() -> None:
    assert _dedupe_numeric([1, 2, 2, 3, 1]) == [1, 2, 3]


def test_grid_range_snaps_integer_bounds() -> None:
    st = {
        "ftype": "integer",
        "type": "numeric",
        "by_select": _w("By range"),
        "min": _w(1.0),
        "max": _w(10.6),  # a fractional max must not survive
        "levels": _w(4),
        "spacing": _w("linear"),
    }
    spec = _build_grid_vary("n", "numeric", st)
    assert spec["vary"]["min"] == 1
    assert spec["vary"]["max"] == 11
    assert isinstance(spec["vary"]["max"], int)


def test_grid_range_leaves_float_bounds_alone() -> None:
    st = {
        "ftype": "float",
        "type": "numeric",
        "by_select": _w("By range"),
        "min": _w(1.0),
        "max": _w(10.6),
        "levels": _w(4),
        "spacing": _w("linear"),
    }
    spec = _build_grid_vary("x", "numeric", st)
    assert spec["vary"]["max"] == 10.6


def test_grid_by_values_snaps_and_dedupes_integers() -> None:
    st = {
        "ftype": "integer",
        "type": "numeric",
        "by_select": _w("By values"),
        "values_text": _w("2, 2.6, 3"),  # 2.6 → 3, collides with 3
    }
    spec = _build_grid_vary("n", "numeric", st)
    assert spec["vary"]["values"] == [2, 3]


def test_design_and_sampling_snap_integer_bounds() -> None:
    design = _build_design_vary(
        "n", "numeric", {"ftype": "integer", "type": "numeric", "min": _w(2.2), "max": _w(8.9)}
    )
    assert design["vary"]["min"] == 2
    assert design["vary"]["max"] == 9

    sampling = _build_sampling_vary(
        "n",
        "numeric",
        {
            "ftype": "integer",
            "type": "numeric",
            "dist_kind": _w("uniform"),
            "min": _w(2.2),
            "max": _w(8.9),
        },
    )
    assert sampling["vary"]["min"] == 2
    assert sampling["vary"]["max"] == 9


def test_constant_integer_value_is_snapped() -> None:
    factors = {
        "n": {"role": "Constant", "ftype": "integer", "type": "numeric", "const": _w(3.7)},
        "mat": {"role": "Constant", "ftype": "select", "type": "select", "const": _w("cu")},
    }
    spec = _build_factor_spec(factors, "grid")
    assert spec["n"]["value"] == 4
    assert spec["mat"]["value"] == "cu"  # non-numeric constant untouched
