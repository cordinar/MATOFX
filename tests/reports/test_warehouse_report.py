"""Unit tests for the warehouse printable-HTML builder (ADR-0012 §8.1)."""

from __future__ import annotations

import datetime as _dt

from matofx.reports.warehouse_report import (
    WarehouseReportContext,
    build_warehouse_report,
)


def _ctx(**overrides: object) -> WarehouseReportContext:
    base = {
        "tool_alias": "heat_transfer",
        "tool_name": "Heat Transfer",
        "generated_at": _dt.datetime(2026, 5, 29, 12, 0, tzinfo=_dt.UTC),
        "kpi_entries": [
            {
                "kpi_name": "max_temperature",
                "kpi_label": "Peak temp",
                "kpi_unit": "K",
                "kpi_category": "thermal",
            }
        ],
        "rows": [
            {
                "job_id": 1,
                "project_id": 2,
                "scenario_id": 3,
                "requestor_user_id": 4,
                "completed_at": _dt.datetime(2026, 5, 29, 10, 0, tzinfo=_dt.UTC),
                "tool_version_id": 5,
                "kpi_values": {"max_temperature": 372.15},
                "kpi_units": {"max_temperature": "K"},
            }
        ],
        "total": 1,
        "hidden_by_acl": 0,
        "auto_print": False,
    }
    base.update(overrides)
    return WarehouseReportContext(**base)  # type: ignore[arg-type]


def test_report_renders_table_header_and_row() -> None:
    html = build_warehouse_report(_ctx())
    assert "<table>" in html
    assert "Peak temp (K)" in html
    assert "372.15" in html
    assert ">1<" in html  # job_id cell


def test_report_renders_tool_name_in_header() -> None:
    html = build_warehouse_report(_ctx())
    assert "Heat Transfer" in html


def test_report_prefers_identity_names_over_ids() -> None:
    """Identity columns show names when present, ids only as fallback."""
    rows = [
        {
            "job_id": 1,
            "project_id": 2,
            "project_name": "Rocket nozzle",
            "scenario_id": 3,
            "scenario_name": "Hot case",
            "requestor_user_id": 4,
            "requestor_name": "Ada Lovelace",
            "completed_at": _dt.datetime(2026, 5, 29, 10, 0, tzinfo=_dt.UTC),
            "tool_version_id": 5,
            "tool_version_label": "v1.2.0",
            "kpi_values": {"max_temperature": 372.15},
            "kpi_units": {"max_temperature": "K"},
        }
    ]
    html = build_warehouse_report(_ctx(rows=rows))
    assert "Rocket nozzle" in html
    assert "Hot case" in html
    assert "Ada Lovelace" in html
    assert "v1.2.0" in html


def test_report_falls_back_to_ids_when_names_absent() -> None:
    """A row with no resolved names still renders the raw ids (traceable)."""
    html = build_warehouse_report(_ctx())  # rows carry only ids
    assert ">2<" in html  # project_id
    assert ">5<" in html  # tool_version_id


def test_report_renders_empty_state_when_no_rows() -> None:
    html = build_warehouse_report(_ctx(rows=[]))
    assert "No rows matched" in html
    assert "<table>" not in html


def test_report_renders_acl_hidden_count_in_footer() -> None:
    html = build_warehouse_report(_ctx(hidden_by_acl=12))
    assert "12 additional row(s)" in html
    assert "hidden by access controls" in html


def test_report_escapes_user_supplied_strings() -> None:
    """Tool name carrying HTML special chars is escaped, not injected."""
    html = build_warehouse_report(_ctx(tool_name="<script>boom</script>"))
    assert "<script>boom</script>" not in html
    assert "&lt;script&gt;" in html


def test_report_auto_print_can_be_disabled_for_tests() -> None:
    no_print = build_warehouse_report(_ctx(auto_print=False))
    assert "window.print" not in no_print
    yes_print = build_warehouse_report(_ctx(auto_print=True))
    assert "window.print" in yes_print


def test_report_renders_filter_summary_when_present() -> None:
    html = build_warehouse_report(_ctx(filter_summary="version_id=7, completed_from=2026-01-01"))
    assert "version_id=7" in html
    assert "Filters" in html


def test_report_includes_print_stylesheet() -> None:
    html = build_warehouse_report(_ctx())
    assert "@media print" in html
