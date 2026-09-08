"""Unit tests for the pure export builders / renderers in :mod:`matofx.usage.export`."""

from __future__ import annotations

import datetime as _dt
import io
import zipfile

from matofx.usage import export
from matofx.usage.service import Kpi, OverviewStats, StatePoint, ToolCount


def _overview() -> OverviewStats:
    return OverviewStats(
        jobs_total=Kpi(value=3.0, previous=1.0, delta_pct=200.0),
        active_users=Kpi(value=1.0),
        success_rate=Kpi(value=66.5),
        core_hours=Kpi(value=4.0),
        gpu_hours=Kpi(value=0.0),
        registered_users=5,
        by_state={"complete": 2, "failed": 1},
        series=[StatePoint(date=_dt.date(2026, 5, 20), state="pending_approval", count=2)],
        top_tools=[ToolCount(tool_alias="alpha", tool_name="Alpha Tool", count=3)],
    )


def test_build_overview_tables_shape() -> None:
    tables = export.build_overview_tables(_overview())
    names = {t.name for t in tables}
    assert names == {"overview_kpis", "overview_jobs_by_day", "overview_top_tools"}
    # States are humanised in the export, not left as snake_case.
    jobs_by_day = next(t for t in tables if t.name == "overview_jobs_by_day")
    assert jobs_by_day.rows[0][1] == "Pending Approval"


def test_tables_to_zip_contains_one_csv_per_table() -> None:
    tables = export.build_overview_tables(_overview())
    data = export.tables_to_zip(tables)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        assert names == {
            "overview_kpis.csv",
            "overview_jobs_by_day.csv",
            "overview_top_tools.csv",
        }
        top = archive.read("overview_top_tools.csv").decode()
    assert "Tool,Jobs" in top
    assert "Alpha Tool,3" in top


def test_render_report_html_is_self_contained() -> None:
    tables = export.build_overview_tables(_overview())
    html = export.render_report_html(
        tables,
        title="MATOFX — Usage Statistics",
        period_label="2026-05-01 → 2026-05-22",
        filters_label="All",
        generated_at=_dt.datetime(2026, 5, 22, 12, 0, tzinfo=_dt.UTC),
    )
    assert "MATOFX — Usage Statistics" in html
    assert "Overview — KPIs" in html
    assert "Alpha Tool" in html
    # Air-gap guarantee: no external/CDN asset references.
    assert "http://" not in html
    assert "https://" not in html


def test_display_cell_formats() -> None:
    assert export._display_cell(None) == "—"
    assert export._display_cell(4.0) == "4"
    assert export._display_cell(1234) == "1,234"
    assert export._display_cell("x") == "x"


def test_is_known_tab() -> None:
    assert export.is_known_tab("overview")
    assert not export.is_known_tab("bogus")
