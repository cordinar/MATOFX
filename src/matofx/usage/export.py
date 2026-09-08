"""Export the Usage Statistics tabs as CSV bundles or a printable report.

The operator picks *which* tabs to export (not all-or-nothing); the
router computes only the chosen tabs' stats and hands them here. This
module is pure — it turns the stats dataclasses into flat
:class:`RenderedTable` rows and then into either:

- a **ZIP of CSVs** (one ``<tab>_<table>.csv`` per table), built with the
  stdlib ``csv`` + ``zipfile`` — no third-party dependency; or
- a **self-contained printable HTML** report (Jinja2, inline CSS, no
  JavaScript and no external/CDN asset) the operator prints to PDF from
  the browser. This keeps the air-gap guarantee (no PDF engine, no
  outbound fetch) while still giving a paginated "PDF" deliverable.

The same builders feed the email digest (:mod:`matofx.usage.digest`),
so the report a recipient gets is exactly what the page exports.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape

from matofx.usage.service import (
    ComputeStats,
    InfraStats,
    OverviewStats,
    PeopleStats,
    ReliabilityStats,
    SeatsStats,
)

# Tab id → human label, in display order. The export dialog and the
# digest config both validate selections against these keys.
TAB_LABELS: dict[str, str] = {
    "overview": "Overview",
    "compute": "Compute & Throughput",
    "reliability": "Reliability",
    "people": "People & Adoption",
    "seats": "Seats & Licence",
    "infra": "Infrastructure",
}

_APPROVAL_KINDS: dict[str, str] = {"pre": "Pre-run", "post": "Post-results"}
_TARGET_LABELS: dict[str, str] = {"local": "Local", "aos": "AOS", "hpc": "HPC"}


@dataclass(frozen=True)
class RenderedTable:
    """One flat table ready for CSV or HTML rendering."""

    name: str
    """File-stem id, unique within an export (e.g. ``overview_top_tools``)."""
    label: str
    """Human heading shown in the HTML report."""
    headers: list[str]
    rows: list[list[Any]]


def is_known_tab(tab: str) -> bool:
    """Whether ``tab`` is an exportable tab id."""
    return tab in TAB_LABELS


# ---- per-tab table builders ------------------------------------------------


def _humanize(value: str) -> str:
    return value.replace("_", " ").title()


def _iso(moment: _dt.datetime | None) -> str:
    return moment.isoformat() if moment is not None else ""


def _kpi_rows(stats: OverviewStats) -> list[list[Any]]:
    return [
        [label, kpi.value, kpi.previous, kpi.delta_pct]
        for label, kpi in (
            ("Jobs run", stats.jobs_total),
            ("Active users", stats.active_users),
            ("Success rate %", stats.success_rate),
            ("Core-hours", stats.core_hours),
            ("GPU-hours", stats.gpu_hours),
        )
    ]


def build_overview_tables(stats: OverviewStats) -> list[RenderedTable]:
    """Tables for the Overview tab."""
    return [
        RenderedTable(
            name="overview_kpis",
            label="Overview — KPIs",
            headers=["Metric", "Value", "Previous", "Delta %"],
            rows=[*_kpi_rows(stats), ["Registered users", stats.registered_users, None, None]],
        ),
        RenderedTable(
            name="overview_jobs_by_day",
            label="Overview — jobs over time (by state)",
            headers=["Date", "State", "Jobs"],
            rows=[[p.date.isoformat(), _humanize(p.state), p.count] for p in stats.series],
        ),
        RenderedTable(
            name="overview_top_tools",
            label="Overview — top tools",
            headers=["Tool", "Jobs"],
            rows=[[t.tool_name, t.count] for t in stats.top_tools],
        ),
    ]


def build_compute_tables(stats: ComputeStats) -> list[RenderedTable]:
    """Tables for the Compute & Throughput tab."""
    return [
        RenderedTable(
            name="compute_by_tool",
            label="Compute — consumption by tool",
            headers=["Tool", "Core-hours", "GPU-hours", "Jobs"],
            rows=[[c.tool_name, c.core_hours, c.gpu_hours, c.jobs] for c in stats.by_tool],
        ),
        RenderedTable(
            name="compute_by_team",
            label="Compute — consumption by team",
            headers=["Team", "Core-hours", "GPU-hours", "Jobs"],
            rows=[[c.team_name, c.core_hours, c.gpu_hours, c.jobs] for c in stats.by_team],
        ),
        RenderedTable(
            name="compute_runtime",
            label="Compute — runtime distribution (hours)",
            headers=["Tool", "p50", "p95", "Max", "Runs"],
            rows=[
                [r.tool_name, r.p50_hours, r.p95_hours, r.max_hours, r.runs] for r in stats.runtime
            ],
        ),
        RenderedTable(
            name="compute_queue_wait",
            label="Compute — average queue wait (hours)",
            headers=["Date", "Avg wait (hours)"],
            rows=[[p.date.isoformat(), p.value] for p in stats.queue_wait],
        ),
        RenderedTable(
            name="compute_throughput",
            label="Compute — throughput",
            headers=["Date", "Submitted", "Completed"],
            rows=[[p.date.isoformat(), p.submitted, p.completed] for p in stats.throughput],
        ),
    ]


def build_reliability_tables(stats: ReliabilityStats) -> list[RenderedTable]:
    """Tables for the Reliability tab."""
    rate = stats.success_rate
    return [
        RenderedTable(
            name="reliability_success",
            label="Reliability — success rate",
            headers=["Metric", "Value", "Previous", "Delta %"],
            rows=[["Success rate %", rate.value, rate.previous, rate.delta_pct]],
        ),
        RenderedTable(
            name="reliability_success_trend",
            label="Reliability — success rate over time",
            headers=["Date", "Success %"],
            rows=[[p.date.isoformat(), p.value] for p in stats.success_trend],
        ),
        RenderedTable(
            name="reliability_top_failing",
            label="Reliability — top failing tools",
            headers=["Tool", "Failures", "Failure rate %", "Last failed"],
            rows=[
                [f.tool_name, f.failures, f.failure_rate, _iso(f.last_failed_at)]
                for f in stats.top_failing
            ],
        ),
        RenderedTable(
            name="reliability_exit_codes",
            label="Reliability — exit codes",
            headers=["Exit code", "Jobs"],
            rows=[[e.exit_code, e.count] for e in stats.exit_codes],
        ),
        RenderedTable(
            name="reliability_approval_funnel",
            label="Reliability — approval funnel",
            headers=["Gate", "Requested", "Approved", "Denied", "Pending"],
            rows=[
                [
                    _APPROVAL_KINDS.get(a.kind, _humanize(a.kind)),
                    a.requested,
                    a.approved,
                    a.denied,
                    a.pending,
                ]
                for a in stats.approval_funnel
            ],
        ),
    ]


def build_people_tables(stats: PeopleStats) -> list[RenderedTable]:
    """Tables for the People & Adoption tab."""
    return [
        RenderedTable(
            name="people_summary",
            label="People — summary",
            headers=["Metric", "Value"],
            rows=[
                ["Active users", stats.active_users.value],
                ["New users", stats.new_users.value],
                ["Peak concurrent sessions", stats.peak_concurrency],
                ["Registered users", stats.registered_users],
            ],
        ),
        RenderedTable(
            name="people_active_trend",
            label="People — active users over time",
            headers=["Date", "Active users"],
            rows=[[p.date.isoformat(), p.value] for p in stats.active_trend],
        ),
        RenderedTable(
            name="people_by_user",
            label="People — jobs by user",
            headers=["User", "Jobs"],
            rows=[[n.name, n.count] for n in stats.by_user],
        ),
        RenderedTable(
            name="people_by_team",
            label="People — jobs by team",
            headers=["Team", "Jobs"],
            rows=[[n.name, n.count] for n in stats.by_team],
        ),
        RenderedTable(
            name="people_by_role",
            label="People — jobs by role",
            headers=["Role", "Jobs"],
            rows=[[n.name, n.count] for n in stats.by_role],
        ),
    ]


def build_seats_tables(stats: SeatsStats) -> list[RenderedTable]:
    """Tables for the Seats & Licence tab."""
    return [
        RenderedTable(
            name="seats_summary",
            label="Seats — summary",
            headers=["Metric", "Value"],
            rows=[
                ["Enabled seats", stats.enabled_seats],
                ["Peak concurrent sessions", stats.peak_concurrency],
                ["Registered users", stats.registered_users],
                ["Licensed cap", stats.cap if stats.cap is not None else "n/a (Phase 2)"],
            ],
        ),
        RenderedTable(
            name="seats_concurrency_trend",
            label="Seats — peak concurrency over time",
            headers=["Date", "Peak sessions"],
            rows=[[p.date.isoformat(), p.value] for p in stats.concurrency_trend],
        ),
        RenderedTable(
            name="seats_logon_events",
            label="Seats — logon grants vs revocations",
            headers=["Date", "Granted", "Revoked"],
            rows=[[e.date.isoformat(), e.granted, e.revoked] for e in stats.logon_events],
        ),
    ]


def build_infra_tables(stats: InfraStats) -> list[RenderedTable]:
    """Tables for the Infrastructure tab."""
    return [
        RenderedTable(
            name="infra_by_target",
            label="Infrastructure — by execution target",
            headers=["Target", "Jobs", "Core-hours", "GPU-hours"],
            rows=[
                [
                    _TARGET_LABELS.get(t.target, _humanize(t.target)),
                    t.jobs,
                    t.core_hours,
                    t.gpu_hours,
                ]
                for t in stats.by_target
            ],
        ),
        RenderedTable(
            name="infra_cpu_gpu",
            label="Infrastructure — CPU vs GPU",
            headers=["Category", "Jobs", "Core-hours"],
            rows=[
                ["CPU", stats.cpu_jobs, stats.cpu_core_hours],
                ["GPU", stats.gpu_jobs, stats.gpu_core_hours],
            ],
        ),
    ]


# ---- output rendering ------------------------------------------------------


def tables_to_zip(tables: Sequence[RenderedTable]) -> bytes:
    """Bundle each table as ``<name>.csv`` inside a single ZIP archive."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for table in tables:
            archive.writestr(f"{table.name}.csv", _table_to_csv(table))
    return buffer.getvalue()


def _table_to_csv(table: RenderedTable) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(table.headers)
    for row in table.rows:
        writer.writerow(["" if cell is None else cell for cell in row])
    return out.getvalue()


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """Process-wide Jinja2 environment for the usage report template."""
    return Environment(
        loader=PackageLoader("matofx.usage", "templates"),
        autoescape=select_autoescape(["html"]),
    )


def render_report_html(
    tables: Sequence[RenderedTable],
    *,
    title: str,
    period_label: str,
    filters_label: str,
    generated_at: _dt.datetime,
) -> str:
    """Render the self-contained printable HTML report."""
    template = _environment().get_template("usage_report.html.j2")
    return template.render(
        title=title,
        period_label=period_label,
        filters_label=filters_label,
        generated_at=generated_at.strftime("%Y-%m-%d %H:%M UTC"),
        tables=[
            {"label": t.label, "headers": t.headers, "rows": _display_rows(t.rows)} for t in tables
        ],
    )


def _display_rows(rows: list[list[Any]]) -> list[list[str]]:
    """Stringify cells for HTML (numbers trimmed, ``None`` → em dash)."""
    return [[_display_cell(cell) for cell in row] for row in rows]


def _display_cell(cell: Any) -> str:
    if cell is None:
        return "—"
    if isinstance(cell, float):
        return f"{cell:,.2f}".rstrip("0").rstrip(".")
    if isinstance(cell, int):
        return f"{cell:,}"
    return str(cell)
