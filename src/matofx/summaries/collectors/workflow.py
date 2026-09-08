"""Fact collection for a workflow run.

Composes the run row, its child jobs (in tab order), and the rolled-up state
into a :class:`SummaryFacts`. The caller resolves the run + children through
:class:`~matofx.workflows.run_service.WorkflowRunService` (which applies the
project-VIEW ACL) and hands them in, so this collector does no access control
of its own — only the cheap tool-name lookups and the tally.
"""

from __future__ import annotations

import math
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
from matofx.workflows.orm import Workflow, WorkflowRun
from matofx.workflows.run_service import compute_state


async def collect_workflow_facts(
    session: AsyncSession,
    *,
    run: WorkflowRun,
    children: list[Job],
    settings: Settings,
) -> SummaryFacts:
    """Build :class:`SummaryFacts` for a terminal workflow run.

    ``children`` must be in tab order (``workflow_position`` ASC), as
    :meth:`WorkflowRunService.list_children` returns them. Raises
    :class:`NonTerminalResultError` when the rolled-up state is not terminal.
    """
    state = compute_state(run, children)
    if not is_terminal(state):
        raise NonTerminalResultError(f"workflow run {run.id} is in state {state!r}")

    labels = await _tool_labels(session, [c.tool_alias for c in children])
    members = [
        {"label": labels.get(c.tool_alias, c.tool_alias), "state": c.state} for c in children
    ]
    completed = sum(1 for c in children if c.state == "complete")
    failed = sum(1 for c in children if c.state == "failed")
    first_failed = next((m["label"] for m in members if m["state"] == "failed"), None)
    first_failed_position = next(
        (i + 1 for i, m in enumerate(members) if m["state"] == "failed"), None
    )

    workflow_name = await _workflow_name(session, run.workflow_id)
    finished = _latest_completion(children)
    duration = (
        (finished - run.created_at).total_seconds()
        if finished is not None and run.created_at is not None
        else None
    )

    details: dict[str, Any] = {
        "workflow_name": workflow_name,
        "members": members,
        "member_count": len(members),
        "completed_count": completed,
        "failed_count": failed,
        "headless": bool(run.auto_advance),
    }
    if first_failed is not None:
        details["first_failed_label"] = first_failed
        details["first_failed_position"] = first_failed_position

    # The workflow's "result" is what its final completed step produced: surface
    # that step's headline KPIs so the summary carries numbers, not just a tally.
    final = next((c for c in reversed(children) if c.state == "complete"), None)
    if final is not None:
        final_kpis = await headline_kpis(session, job=final, settings=settings)
        final_results = [
            {"label": h.label, "value": _round(h.value), "unit": h.unit}
            for h in final_kpis
            if h.value is not None
        ]
        if final_results:
            details["final_step_label"] = labels.get(final.tool_alias, final.tool_alias)
            details["final_results"] = final_results

    return SummaryFacts(
        surface_kind="workflow_run",
        surface_id=run.id,
        title=run.name or workflow_name,
        state=state,
        submitted_by=await _submitter(session, run.submitted_by_user_id),
        started_at=run.created_at.isoformat() if run.created_at is not None else None,
        finished_at=finished.isoformat() if finished is not None else None,
        duration_seconds=duration,
        details=details,
    )


async def _tool_labels(session: AsyncSession, aliases: list[str]) -> dict[str, str]:
    distinct = sorted(set(aliases))
    if not distinct:
        return {}
    rows = (await session.execute(select(Tool).where(Tool.alias.in_(distinct)))).scalars()
    return {row.alias: row.name for row in rows}


async def _workflow_name(session: AsyncSession, workflow_id: int | None) -> str:
    if workflow_id is None:
        return "workflow"
    wf = await session.get(Workflow, workflow_id)
    return wf.name if wf is not None else "workflow"


async def _submitter(session: AsyncSession, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    return user.username if user is not None else None


def _latest_completion(children: list[Job]) -> Any:
    stamps = [c.completed_at for c in children if c.completed_at is not None]
    return max(stamps) if stamps else None


def _round(value: Any, sig: int = 4) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number == 0:
        return 0.0
    digits = -math.floor(math.log10(abs(number))) + (sig - 1)
    return round(number, digits)
