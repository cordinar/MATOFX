"""Slice 1 — the Summary block in the per-job HTML report.

Pure: feed a :class:`ReportContext` with / without a summary and assert the
block renders (and is absent / self-contained) accordingly.
"""

from __future__ import annotations

import datetime as _dt

from matofx.reports import ReportContext, build_report
from matofx.summaries import SummaryText

_NOW = _dt.datetime(2026, 6, 10, 12, 0, 0, tzinfo=_dt.UTC)


def _context(**overrides: object) -> ReportContext:
    base: dict[str, object] = {
        "job_id": 7,
        "tool_alias": "heat_sink",
        "tool_name": "Heat sink",
        "tool_version": "1.0.0",
        "submitted_by_username": "alice",
        "submitted_at": _NOW,
        "completed_at": _NOW,
        "report_generated_at": _NOW,
        "app_display_title": "MATOFX",
    }
    base.update(overrides)
    return ReportContext(**base)  # type: ignore[arg-type]


def test_summary_block_renders_paragraphs() -> None:
    summary = SummaryText(
        title="Summary",
        paragraphs=("The Heat sink job completed successfully in 4 min 12 s.",),
    )
    html = build_report(_context(summary=summary))
    assert "result-summary" in html
    assert "The Heat sink job completed successfully in 4 min 12 s." in html


def test_no_summary_suppresses_the_block() -> None:
    html = build_report(_context(summary=None))
    assert "result-summary" not in html


def test_summary_paragraphs_are_html_escaped() -> None:
    """User-supplied content in a summary is escaped — no injection."""
    summary = SummaryText(title="Summary", paragraphs=("<script>alert(1)</script>",))
    html = build_report(_context(summary=summary))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
