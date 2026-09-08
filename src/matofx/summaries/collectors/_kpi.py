"""Shared headline-KPI loading for the job + workflow collectors.

Reads the ADR-0012 KPI view (`list_kpis_for_tool` + `JobKpiValue`) for one job,
in descriptor declaration order, returning `HeadlineNumber`s with labels + units.
Returns ``[]`` when the KPI feature is off or none are declared, so callers
degrade silently.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.config import Settings
from matofx.features import Feature, feature_enabled
from matofx.hierarchy.orm import Job
from matofx.kpi import JobKpiValue, list_kpis_for_tool
from matofx.summaries.facts import HeadlineNumber


async def headline_kpis(
    session: AsyncSession, *, job: Job, settings: Settings
) -> list[HeadlineNumber]:
    """The declared KPIs for ``job``'s tool version, in declaration order."""
    if not feature_enabled(settings.features, Feature.KPI_EXTRACTION):
        return []
    if job.tool_version_id is None:
        return []
    catalog = await list_kpis_for_tool(
        session, tool_alias=job.tool_alias, tool_version_id=job.tool_version_id
    )
    if not catalog:
        return []
    rows = (
        await session.execute(select(JobKpiValue).where(JobKpiValue.job_id == job.id))
    ).scalars()
    values = {
        r.kpi_name: (float(r.value_numeric) if r.value_numeric is not None else None)
        for r in rows
    }
    return [
        HeadlineNumber(
            label=entry.kpi_label or entry.kpi_name,
            value=values.get(entry.kpi_name),
            unit=entry.kpi_unit,
        )
        for entry in catalog
    ]
