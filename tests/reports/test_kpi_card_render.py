"""Tests for the ADR-0004 report headline KPI block (slice 8)."""

from __future__ import annotations

import datetime as _dt

from matofx.reports.builder import ReportContext, build_report


def _ctx(**overrides):  # type: ignore[no-untyped-def]
    base = dict(
        job_id=42,
        tool_alias="heat_transfer",
        tool_name="Heat Transfer",
        tool_version="1.0.0",
        submitted_by_username="anika",
        submitted_at=_dt.datetime(2026, 5, 29, 9, 0, tzinfo=_dt.UTC),
        completed_at=_dt.datetime(2026, 5, 29, 10, 0, tzinfo=_dt.UTC),
        report_generated_at=_dt.datetime(2026, 5, 29, 12, 0, tzinfo=_dt.UTC),
    )
    base.update(overrides)
    return ReportContext(**base)  # type: ignore[arg-type]


def test_report_with_kpi_cards_renders_headline_block() -> None:
    """Cards land under "Headline KPIs" with label + value + unit."""
    ctx = _ctx(
        kpi_cards=[
            {
                "kpi_name": "max_temperature",
                "kpi_label": "Peak temp",
                "kpi_unit": "K",
                "value": 372.15,
            },
            {
                "kpi_name": "max_pressure",
                "kpi_label": "Peak pressure",
                "kpi_unit": "Pa",
                "value": 101325.0,
            },
        ],
    )
    html = build_report(ctx)
    assert "Headline KPIs" in html
    assert "Peak temp" in html
    assert "372.15" in html
    assert "Peak pressure" in html
    assert "101325.0" in html
    assert "kpi-card" in html


def test_report_with_no_kpi_cards_omits_block() -> None:
    """Empty ``kpi_cards`` → no "Headline KPIs" section block.

    Only checks the rendered HTML structure — the stylesheet always
    carries the ``.kpi-card-*`` CSS rules regardless of whether any
    cards are emitted, so we can't assert on the slug in the bytes.
    """
    html = build_report(_ctx(kpi_cards=[]))
    assert "Headline KPIs" not in html
    assert 'class="card kpi-summary"' not in html
    assert 'class="kpi-card-grid"' not in html


def test_report_kpi_cards_default_to_empty_list() -> None:
    """Existing callers that don't pass ``kpi_cards`` still render cleanly."""
    html = build_report(_ctx())
    assert "Headline KPIs" not in html


def test_report_kpi_card_with_null_value_renders_dash() -> None:
    """A NULL value (extraction sentinel) renders as an em-dash, not 'None'."""
    ctx = _ctx(
        kpi_cards=[
            {
                "kpi_name": "max_temperature",
                "kpi_label": "Peak temp",
                "kpi_unit": "K",
                "value": None,
            }
        ],
    )
    html = build_report(ctx)
    assert "&mdash;" in html
    assert ">None<" not in html


def test_report_kpi_card_uses_kpi_name_when_label_absent() -> None:
    """Missing ``kpi_label`` falls back to the raw ``kpi_name``."""
    ctx = _ctx(
        kpi_cards=[
            {
                "kpi_name": "iteration_count",
                "kpi_label": None,
                "kpi_unit": None,
                "value": 42,
            }
        ],
    )
    html = build_report(ctx)
    assert "iteration_count" in html


def test_report_stylesheet_includes_kpi_card_rules() -> None:
    """The stylesheet carries the KPI-card grid styles."""
    html = build_report(_ctx(kpi_cards=[{"kpi_name": "x", "kpi_label": "X", "kpi_unit": None, "value": 1}]))
    assert ".kpi-card-grid" in html
    assert ".kpi-card-value" in html
