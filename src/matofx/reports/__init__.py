"""Self-contained per-job HTML reports.

In-process generator behind ``GET /api/jobs/{job_id}/report``. Builds
a single standalone HTML document that mirrors the dashboard's
descriptor-grouped Inputs / Outputs view, embeds branding from the
chrome's :mod:`matofx.ui.branding_cache`, and inlines every asset so
the file can be archived or emailed and opened on an air-gapped
machine. Design: ``decisions/0004-per-job-html-report.md``.

Slice 1 ships the minimal skeleton (branded header + metadata only).
Later slices grow the inputs / outputs sections, interactive Plotly
figures, embedded media, and workflow context — extending
:func:`build_report`'s body, not its signature.
"""

from __future__ import annotations

from matofx.reports.builder import (
    ReportContext,
    SiblingJob,
    WorkflowContext,
    build_report,
)

__all__ = ["ReportContext", "SiblingJob", "WorkflowContext", "build_report"]
