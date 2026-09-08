"""Self-contained printable HTML for the KPI warehouse (ADR-0012 §8.1).

The warehouse "Download PDF" button hands the browser a URL into
:func:`matofx.api.routers.kpi.get_tool_results_pdf` — this module
renders the HTML the endpoint streams. Browser-print is the actual
PDF generation path (same pattern the legacy app + ADR-0004 per-job
report follow): one print-on-load script + a print stylesheet, and
the operator picks "Save as PDF" in the print dialog.

The full ADR-0004 branding integration (Plotly figures inline,
descriptor-grouped sections, etc.) is a v1.x polish. v1.0 ships a
clean table-only HTML — operators export plots from the browser
window's screenshot tool when they need them. The trade-off is
documented in design §8.1 ("self-contained per ADR-0004 pattern" —
satisfied by the table-only shape; figure embed lands later).

Render contract is pure: input is a :class:`WarehouseReportContext`,
output is a UTF-8 HTML string. No filesystem reads, no network — the
endpoint passes everything in. Keeps the builder unit-testable
without app fixtures.
"""

from __future__ import annotations

import datetime as _dt
import html
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WarehouseReportContext:
    """Inputs to one :func:`build_warehouse_report` call.

    Frozen so call sites can grow extra fields later without breaking
    existing call sites (same convention as
    :class:`matofx.reports.builder.ReportContext`).
    """

    tool_alias: str
    tool_name: str
    generated_at: _dt.datetime
    kpi_entries: list[dict[str, Any]]
    """One entry per declared KPI: ``{kpi_name, kpi_label, kpi_unit, kpi_category}``."""

    rows: list[dict[str, Any]]
    """Warehouse rows in the API response shape — the builder pivots
    ``kpi_values`` + ``kpi_units`` per row, identity columns by name."""

    total: int
    """Unfiltered-by-pagination total — surfaces in the footer."""

    hidden_by_acl: int = 0
    """How many rows were excluded by per-project ACL — surfaces in
    the footer when nonzero."""

    app_display_title: str = "MATOFX"
    """Branded app title; falls back to MATOFX."""

    filter_summary: str = ""
    """Plain-English summary of the active filters
    (e.g. "version=1.2.0, completed_from=2026-01-01"). Empty when
    no filters applied."""

    auto_print: bool = True
    """When ``True`` the rendered HTML runs ``window.print()`` on load
    so the browser jumps straight into the Save-as-PDF dialog. Set
    ``False`` for the test harness so HTML round-trips don't trigger
    print dialogs."""


_STYLESHEET = """
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  color: #1a1a1a;
  margin: 24px;
  background: #ffffff;
}
.header { display: flex; justify-content: space-between; align-items: baseline;
          border-bottom: 2px solid #333; padding-bottom: 12px; margin-bottom: 16px; }
.header .app { font-size: 1.0rem; color: #666; }
.header .title { font-size: 1.4rem; font-weight: 600; }
.meta { font-size: 0.85rem; color: #555; margin-bottom: 16px; }
.meta div { margin: 2px 0; }
table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
th, td { border: 1px solid #ccc; padding: 6px 10px; text-align: left;
         vertical-align: top; }
th { background: #f4f4f4; font-weight: 600; }
td.numeric { text-align: right; font-variant-numeric: tabular-nums; }
.footer { margin-top: 16px; font-size: 0.8rem; color: #666; }
@media print {
  body { margin: 12mm; }
  .no-print { display: none !important; }
}
"""


_AUTO_PRINT_SCRIPT = """
<script>
window.addEventListener('load', function () {
  setTimeout(function () { window.print(); }, 200);
});
</script>
""".strip()


def build_warehouse_report(context: WarehouseReportContext) -> str:
    """Render the warehouse export as a self-contained printable HTML document.

    Returns a UTF-8 string ready to stream as ``text/html``. The
    output carries:

    - An inline ``<style>`` block — print stylesheet + screen
      stylesheet in one.
    - An app-title + tool-name header + filter summary.
    - The warehouse table — identity columns first, one column per
      declared KPI (header is ``kpi_label`` when set, else ``kpi_name``,
      with the captured unit in parentheses if available).
    - A footer with total / hidden-by-acl / generated-at.
    - When :attr:`WarehouseReportContext.auto_print` is ``True``, a
      ``window.print()`` script so the browser opens the print
      dialog on load. Set ``False`` in tests.
    """
    e = html.escape

    header_html = (
        f'<div class="header">'
        f'<div class="title">Results — {e(context.tool_name)}</div>'
        f'<div class="app">{e(context.app_display_title)}</div>'
        f"</div>"
    )

    meta_lines: list[str] = [
        f"<div><strong>Tool:</strong> {e(context.tool_alias)}</div>",
        f"<div><strong>Generated:</strong> {e(_format_dt(context.generated_at))}</div>",
    ]
    if context.filter_summary:
        meta_lines.append(
            f"<div><strong>Filters:</strong> {e(context.filter_summary)}</div>"
        )
    meta_html = '<div class="meta">' + "".join(meta_lines) + "</div>"

    table_html = _render_table(context)

    footer_parts: list[str] = []
    footer_parts.append(
        f"<strong>Total:</strong> {context.total} row(s) matched these filters."
    )
    if context.hidden_by_acl:
        footer_parts.append(
            f" {context.hidden_by_acl} additional row(s) "
            "hidden by access controls."
        )
    footer_html = f'<div class="footer">{"".join(footer_parts)}</div>'

    script_html = _AUTO_PRINT_SCRIPT if context.auto_print else ""

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head>'
        f"<title>Results — {e(context.tool_name)}</title>"
        '<meta charset="utf-8">'
        f"<style>{_STYLESHEET}</style>"
        "</head><body>"
        f"{header_html}"
        f"{meta_html}"
        f"{table_html}"
        f"{footer_html}"
        f"{script_html}"
        "</body></html>"
    )


def _render_table(context: WarehouseReportContext) -> str:
    """Build the table — identity cols + one numeric col per KPI."""
    e = html.escape

    if not context.rows:
        return '<div class="footer"><em>No rows matched.</em></div>'

    identity_headers = [
        "Job",
        "Project",
        "Scenario",
        "Requestor",
        "Completed",
        "Tool version",
    ]
    kpi_headers: list[str] = []
    for entry in context.kpi_entries:
        header = entry.get("kpi_label") or entry.get("kpi_name", "")
        unit = entry.get("kpi_unit")
        kpi_headers.append(f"{header} ({unit})" if unit else header)

    head = "<tr>"
    for h in identity_headers + kpi_headers:
        head += f"<th>{e(h)}</th>"
    head += "</tr>"

    body_rows: list[str] = []
    for row in context.rows:
        cells = [
            _cell(row.get("job_id")),
            _cell(row.get("project_name") or row.get("project_id")),
            _cell(row.get("scenario_name") or row.get("scenario_id")),
            _cell(row.get("requestor_name") or row.get("requestor_user_id")),
            _cell(_format_dt(row.get("completed_at"))),
            _cell(row.get("tool_version_label") or row.get("tool_version_id")),
        ]
        kpi_values = row.get("kpi_values", {}) or {}
        for entry in context.kpi_entries:
            v = kpi_values.get(entry["kpi_name"])
            cells.append(_cell(v, numeric=True))
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    return f"<table>{head}{''.join(body_rows)}</table>"


def _cell(value: Any, *, numeric: bool = False) -> str:
    """One ``<td>`` with HTML-escaped value; empty string for ``None``."""
    e = html.escape
    text = "" if value is None else str(value)
    cls = ' class="numeric"' if numeric else ""
    return f"<td{cls}>{e(text)}</td>"


def _format_dt(value: Any) -> str:
    """Lenient ISO-string formatter for ``datetime`` / ``str`` / ``None``."""
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = _dt.datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, _dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    return str(value)
