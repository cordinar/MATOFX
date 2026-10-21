"""Unit tests for the per-job Headline KPIs card helpers (slice 10).

The card itself is a NiceGUI surface and isn't render-tested (matches
the V&V card convention); the pure-logic helpers — value formatting +
sparkline SVG construction — are. The last group reaches into the card
proper but stops before any NiceGUI call, to pin down which catalog it
asks the API for.
"""

from __future__ import annotations

from matofx.ui.api_client import ApiError
from matofx.ui.pages.job import (
    _build_sparkline_svg,
    _format_kpi_value,
    _render_job_kpi_card,
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


# ---- Catalog is pinned to the job's own tool version -----------------


async def _capture_catalog_call(job: dict[str, object]) -> dict[str, object]:
    """Drive the card far enough to record the catalog call it makes.

    The stub raises :class:`ApiError` on the first call, which the card
    swallows and returns from — so no NiceGUI slot is needed to observe
    the arguments. That first call is the whole point of the test.
    """
    seen: dict[str, object] = {}

    class _StubClient:
        async def list_tool_kpis(
            self, alias: str, *, version_id: int | None = None
        ) -> dict[str, object]:
            seen["alias"] = alias
            seen["version_id"] = version_id
            raise ApiError(503, "stop here")

    await _render_job_kpi_card(_StubClient(), job, 7)  # type: ignore[arg-type]
    return seen


async def test_kpi_card_pins_catalog_to_the_jobs_tool_version() -> None:
    """The card asks for the KPIs of the version the job actually ran.

    Without the pin it drew the tool-wide catalog, so a run whose pinned
    descriptor declared no KPI still got tiles — each reading "—" for a
    KPI that run was never asked to produce.
    """
    seen = await _capture_catalog_call({"tool_alias": "bracket_stress", "tool_version_id": 65})
    assert seen == {"alias": "bracket_stress", "version_id": 65}


async def test_kpi_card_falls_back_to_tool_catalog_without_a_version() -> None:
    """An endpoint that didn't resolve the version degrades, not crashes."""
    seen = await _capture_catalog_call({"tool_alias": "bracket_stress"})
    assert seen == {"alias": "bracket_stress", "version_id": None}


async def test_kpi_card_skips_a_job_with_no_tool_alias() -> None:
    """No alias → no call at all (the card returns before touching the API)."""
    assert await _capture_catalog_call({}) == {}
