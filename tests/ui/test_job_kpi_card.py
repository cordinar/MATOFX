"""Unit tests for the per-job Headline KPIs card helpers (slice 10).

The card itself is a NiceGUI surface and isn't render-tested (matches
the V&V card convention); the pure-logic helpers — value formatting +
sparkline SVG construction — are.
"""

from __future__ import annotations

from matofx.ui.pages.job import (
    _build_sparkline_svg,
    _format_kpi_value,
)

# ---- Value formatting -----------------------------------------------


def test_format_value_none_returns_dash() -> None:
    assert _format_kpi_value(None) == "—"


def test_format_value_integer_formats_compactly() -> None:
    assert _format_kpi_value(42) == "42"


def test_format_value_float_drops_trailing_zeros() -> None:
    assert _format_kpi_value(372.150000) == "372.15"


def test_format_value_string_passes_through() -> None:
    """Non-numeric falls back to ``str()`` rather than crashing."""
    assert _format_kpi_value("hot") == "hot"


# ---- Sparkline SVG --------------------------------------------------


def test_sparkline_with_no_points_returns_empty() -> None:
    assert _build_sparkline_svg([], label="x") == ""


def test_sparkline_produces_polyline_with_n_points() -> None:
    svg = _build_sparkline_svg([1.0, 2.0, 3.0, 4.0], label="Peak temp")
    assert "<svg" in svg
    assert "<polyline" in svg
    # 4 points → 4 coordinate pairs in the polyline.
    assert svg.count(",") == 4
    # The label is escaped into the aria-label.
    assert "Peak temp sparkline" in svg


def test_sparkline_normalises_zero_span_without_div_zero() -> None:
    """All-equal values render without exploding on a zero range."""
    svg = _build_sparkline_svg([5.0, 5.0, 5.0], label="flat")
    assert "<polyline" in svg
