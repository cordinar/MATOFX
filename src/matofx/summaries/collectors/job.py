"""Fact collection for a single job.

Composes the same data the per-job report already resolves — the ``Job`` row,
the tool's display name, and the ADR-0012 headline-KPI view — into a
:class:`SummaryFacts`. Reusable from the report endpoint, the job page, and
(later) ``SummaryService`` without any of them needing the others' state.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.config import Settings
from matofx.hierarchy.orm import Job
from matofx.summaries.collectors._kpi import headline_kpis
from matofx.summaries.errors import NonTerminalResultError
from matofx.summaries.facts import SummaryFacts, is_terminal
from matofx.tools.orm import Tool
from matofx.users.orm import User

_SCALAR_OUTPUT_TYPES = frozenset({"number", "integer", "float", "string", "boolean"})


async def collect_job_facts(
    session: AsyncSession,
    *,
    job: Job,
    settings: Settings,
    tool_name: str | None = None,
    outputs_doc: Mapping[str, Any] | None = None,
) -> SummaryFacts:
    """Build :class:`SummaryFacts` for a terminal job.

    ``tool_name`` and ``outputs_doc`` may be supplied by a caller that has
    already resolved them (the report endpoint) to avoid re-querying;
    otherwise the tool's display name is looked up and the output-count
    sentence is omitted. Raises :class:`NonTerminalResultError` if the job is
    not in a terminal state.
    """
    if not is_terminal(job.state):
        raise NonTerminalResultError(f"job {job.id} is in state {job.state!r}")

    label = tool_name or await _tool_name(session, job.tool_alias)
    submitter = await _submitter_username(session, job.created_by_user_id)
    headline = await headline_kpis(session, job=job, settings=settings)

    details: dict[str, Any] = {
        "tool_label": label,
        "exit_code": job.exit_code,
    }
    if job.workflow_run_id is not None:
        details["workflow_run_id"] = job.workflow_run_id
    counts = _output_counts(outputs_doc)
    if counts is not None:
        details["output_file_count"], details["output_scalar_count"] = counts

    return SummaryFacts(
        surface_kind="job",
        surface_id=job.id,
        title=job.name or label,
        state=job.state,
        headline=tuple(headline),
        submitted_by=submitter,
        started_at=_iso(job.started_at),
        finished_at=_iso(job.completed_at),
        duration_seconds=_duration(job),
        details=details,
    )


async def _tool_name(session: AsyncSession, tool_alias: str) -> str:
    row = (
        await session.execute(select(Tool).where(Tool.alias == tool_alias))
    ).scalar_one_or_none()
    return row.name if row is not None else tool_alias


async def _submitter_username(session: AsyncSession, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    return user.username if user is not None else None


def _output_counts(outputs_doc: Mapping[str, Any] | None) -> tuple[int, int] | None:
    """Return ``(file_count, scalar_count)`` from a parsed outputs doc.

    ``None`` when no document was supplied — the narrative then omits the
    "produced N files / M scalars" sentence rather than guessing.
    """
    if not outputs_doc:
        return None
    outputs = outputs_doc.get("outputs")
    if not isinstance(outputs, list):
        return None
    files = 0
    scalars = 0
    for entry in outputs:
        if not isinstance(entry, dict):
            continue
        out_type = str(entry.get("type", "")).lower()
        if entry.get("filename") or (out_type and out_type not in _SCALAR_OUTPUT_TYPES):
            files += 1
        else:
            scalars += 1
    return files, scalars


def _duration(job: Job) -> float | None:
    if job.started_at is None or job.completed_at is None:
        return None
    return (job.completed_at - job.started_at).total_seconds()


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None
