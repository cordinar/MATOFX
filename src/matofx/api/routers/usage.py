"""HTTP API for the Usage Statistics surface (``usage.view``).

Thin endpoints over :class:`~matofx.usage.UsageService`. Read-only and
aggregate-only — every route returns summary figures, never individual
job / user / audit rows. Gated by the ``usage.view`` permission so a
non-technical "manager" role can be granted usage analytics without the
audit log (``audit.read``) or the database explorer (``database.explore``).

All tabs share one filter selection (period + multi-select tool / team /
role / execution target), parsed once by :func:`get_job_filters` and
passed to each per-tab service method. Slice 1 shipped ``/overview`` +
``/filters``; slice 2 adds ``/compute`` (Compute & Throughput).
"""

from __future__ import annotations

import datetime as _dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.exceptions import HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import (
    get_current_user,
    get_db,
    get_digest_service,
    get_settings,
    get_usage_service,
)
from matofx.api.schemas import (
    UsageApprovalKindFunnel,
    UsageComputeResponse,
    UsageDayValue,
    UsageDigestFailure,
    UsageDigestResponse,
    UsageDigestSendRequest,
    UsageExitCodeCount,
    UsageFailingTool,
    UsageFiltersResponse,
    UsageFilterTeam,
    UsageFilterTool,
    UsageInfraResponse,
    UsageKpi,
    UsageLogonEventPoint,
    UsageNamedCount,
    UsageOverviewResponse,
    UsagePeopleResponse,
    UsageReliabilityResponse,
    UsageRuntimeDist,
    UsageSeatsResponse,
    UsageStatePoint,
    UsageTargetUsage,
    UsageTeamConsumption,
    UsageThroughputPoint,
    UsageToolConsumption,
    UsageToolCount,
)
from matofx.config import Settings
from matofx.usage import (
    ComputeStats,
    DigestService,
    InfraStats,
    JobFilters,
    Kpi,
    OverviewStats,
    PeopleStats,
    ReliabilityStats,
    SeatsStats,
    UsageService,
)
from matofx.usage import export as usage_export
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission

router = APIRouter()

# Default look-back when the caller doesn't pass an explicit window.
_DEFAULT_WINDOW_DAYS = 30


async def _require_usage_view(session: AsyncSession, actor_user_id: int) -> None:
    """Raise 403 unless the actor holds ``usage.view``."""
    if not await user_has_permission(session, actor_user_id, Permission.USAGE_VIEW):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You don't have permission to view usage statistics "
                "(usage.view). Ask an administrator if you need access."
            ),
        )


def _as_utc(value: _dt.datetime) -> _dt.datetime:
    """Return ``value`` as a UTC-aware datetime (naive ⇒ assume UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=_dt.UTC)
    return value.astimezone(_dt.UTC)


def get_job_filters(
    since: Annotated[_dt.datetime | None, Query()] = None,
    until: Annotated[_dt.datetime | None, Query()] = None,
    tool_alias: Annotated[list[str] | None, Query()] = None,
    team_id: Annotated[list[int] | None, Query()] = None,
    role: Annotated[list[str] | None, Query()] = None,
    execution_target: Annotated[list[str] | None, Query()] = None,
) -> JobFilters:
    """Parse the shared filter query params into a :class:`JobFilters`.

    Each multi-select param is repeatable (``?tool_alias=a&tool_alias=b``);
    an absent param means "all". The window applies UTC-coercion +
    defaults: ``until`` defaults to now, ``since`` to ``until`` minus the
    default look-back, so naive inputs compare cleanly against the
    timezone-aware ``job.created_at``.
    """
    resolved_until = _as_utc(until) if until is not None else _dt.datetime.now(tz=_dt.UTC)
    resolved_since = (
        _as_utc(since)
        if since is not None
        else resolved_until - _dt.timedelta(days=_DEFAULT_WINDOW_DAYS)
    )
    return JobFilters(
        since=resolved_since,
        until=resolved_until,
        tool_aliases=tuple(tool_alias or ()),
        team_ids=tuple(team_id or ()),
        roles=tuple(role or ()),
        execution_targets=tuple(execution_target or ()),
    )


def _kpi(kpi: Kpi) -> UsageKpi:
    """Map a service :class:`Kpi` to its wire schema."""
    return UsageKpi(value=kpi.value, previous=kpi.previous, delta_pct=kpi.delta_pct)


def _overview_response(stats: OverviewStats, *, filters: JobFilters) -> UsageOverviewResponse:
    """Map the service :class:`OverviewStats` to the response schema."""
    return UsageOverviewResponse(
        since=filters.since,
        until=filters.until,
        jobs_total=_kpi(stats.jobs_total),
        active_users=_kpi(stats.active_users),
        success_rate=_kpi(stats.success_rate),
        core_hours=_kpi(stats.core_hours),
        gpu_hours=_kpi(stats.gpu_hours),
        registered_users=stats.registered_users,
        by_state=stats.by_state,
        series=[UsageStatePoint(date=p.date, state=p.state, count=p.count) for p in stats.series],
        top_tools=[
            UsageToolCount(tool_alias=t.tool_alias, tool_name=t.tool_name, count=t.count)
            for t in stats.top_tools
        ],
    )


def _compute_response(stats: ComputeStats, *, filters: JobFilters) -> UsageComputeResponse:
    """Map the service :class:`ComputeStats` to the response schema."""
    return UsageComputeResponse(
        since=filters.since,
        until=filters.until,
        by_tool=[
            UsageToolConsumption(
                tool_alias=c.tool_alias,
                tool_name=c.tool_name,
                core_hours=c.core_hours,
                gpu_hours=c.gpu_hours,
                jobs=c.jobs,
            )
            for c in stats.by_tool
        ],
        by_team=[
            UsageTeamConsumption(
                team_id=c.team_id,
                team_name=c.team_name,
                core_hours=c.core_hours,
                gpu_hours=c.gpu_hours,
                jobs=c.jobs,
            )
            for c in stats.by_team
        ],
        runtime=[
            UsageRuntimeDist(
                tool_alias=r.tool_alias,
                tool_name=r.tool_name,
                p50_hours=r.p50_hours,
                p95_hours=r.p95_hours,
                max_hours=r.max_hours,
                runs=r.runs,
            )
            for r in stats.runtime
        ],
        queue_wait=[UsageDayValue(date=d.date, value=d.value) for d in stats.queue_wait],
        throughput=[
            UsageThroughputPoint(date=t.date, submitted=t.submitted, completed=t.completed)
            for t in stats.throughput
        ],
    )


@router.get("/admin/usage/overview", response_model=UsageOverviewResponse)
async def get_usage_overview(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[UsageService, Depends(get_usage_service)],
    filters: Annotated[JobFilters, Depends(get_job_filters)],
) -> UsageOverviewResponse:
    """Return the Overview tab's KPIs, time series, and tool leaderboard."""
    await _require_usage_view(db, current_user.id)
    stats = await service.overview(db, filters=filters)
    return _overview_response(stats, filters=filters)


@router.get("/admin/usage/compute", response_model=UsageComputeResponse)
async def get_usage_compute(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[UsageService, Depends(get_usage_service)],
    filters: Annotated[JobFilters, Depends(get_job_filters)],
) -> UsageComputeResponse:
    """Return the Compute & Throughput tab's consumption + runtime figures."""
    await _require_usage_view(db, current_user.id)
    stats = await service.compute(db, filters=filters)
    return _compute_response(stats, filters=filters)


def _reliability_response(
    stats: ReliabilityStats, *, filters: JobFilters
) -> UsageReliabilityResponse:
    """Map the service :class:`ReliabilityStats` to the response schema."""
    return UsageReliabilityResponse(
        since=filters.since,
        until=filters.until,
        success_rate=_kpi(stats.success_rate),
        success_trend=[UsageDayValue(date=d.date, value=d.value) for d in stats.success_trend],
        top_failing=[
            UsageFailingTool(
                tool_alias=f.tool_alias,
                tool_name=f.tool_name,
                failures=f.failures,
                failure_rate=f.failure_rate,
                last_failed_at=f.last_failed_at,
            )
            for f in stats.top_failing
        ],
        exit_codes=[
            UsageExitCodeCount(exit_code=e.exit_code, count=e.count) for e in stats.exit_codes
        ],
        approval_funnel=[
            UsageApprovalKindFunnel(
                kind=a.kind,
                requested=a.requested,
                approved=a.approved,
                denied=a.denied,
                pending=a.pending,
            )
            for a in stats.approval_funnel
        ],
    )


@router.get("/admin/usage/reliability", response_model=UsageReliabilityResponse)
async def get_usage_reliability(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[UsageService, Depends(get_usage_service)],
    filters: Annotated[JobFilters, Depends(get_job_filters)],
) -> UsageReliabilityResponse:
    """Return the Reliability tab's success / failure / approval figures."""
    await _require_usage_view(db, current_user.id)
    stats = await service.reliability(db, filters=filters)
    return _reliability_response(stats, filters=filters)


def _people_response(stats: PeopleStats, *, filters: JobFilters) -> UsagePeopleResponse:
    """Map the service :class:`PeopleStats` to the response schema."""
    return UsagePeopleResponse(
        since=filters.since,
        until=filters.until,
        active_users=_kpi(stats.active_users),
        new_users=_kpi(stats.new_users),
        peak_concurrency=stats.peak_concurrency,
        registered_users=stats.registered_users,
        active_trend=[UsageDayValue(date=d.date, value=d.value) for d in stats.active_trend],
        by_user=[UsageNamedCount(name=n.name, count=n.count) for n in stats.by_user],
        by_team=[UsageNamedCount(name=n.name, count=n.count) for n in stats.by_team],
        by_role=[UsageNamedCount(name=n.name, count=n.count) for n in stats.by_role],
    )


def _seats_response(stats: SeatsStats, *, filters: JobFilters) -> UsageSeatsResponse:
    """Map the service :class:`SeatsStats` to the response schema."""
    return UsageSeatsResponse(
        since=filters.since,
        until=filters.until,
        registered_users=stats.registered_users,
        enabled_seats=stats.enabled_seats,
        peak_concurrency=stats.peak_concurrency,
        cap=stats.cap,
        licence_type=stats.licence_type,
        licence_state=stats.licence_state,
        logon_events=[
            UsageLogonEventPoint(date=e.date, granted=e.granted, revoked=e.revoked)
            for e in stats.logon_events
        ],
        concurrency_trend=[
            UsageDayValue(date=d.date, value=d.value) for d in stats.concurrency_trend
        ],
    )


def _infra_response(stats: InfraStats, *, filters: JobFilters) -> UsageInfraResponse:
    """Map the service :class:`InfraStats` to the response schema."""
    return UsageInfraResponse(
        since=filters.since,
        until=filters.until,
        by_target=[
            UsageTargetUsage(
                target=t.target,
                jobs=t.jobs,
                core_hours=t.core_hours,
                gpu_hours=t.gpu_hours,
            )
            for t in stats.by_target
        ],
        cpu_jobs=stats.cpu_jobs,
        gpu_jobs=stats.gpu_jobs,
        cpu_core_hours=stats.cpu_core_hours,
        gpu_core_hours=stats.gpu_core_hours,
    )


@router.get("/admin/usage/people", response_model=UsagePeopleResponse)
async def get_usage_people(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[UsageService, Depends(get_usage_service)],
    filters: Annotated[JobFilters, Depends(get_job_filters)],
) -> UsagePeopleResponse:
    """Return the People & Adoption tab's figures."""
    await _require_usage_view(db, current_user.id)
    stats = await service.people(db, filters=filters)
    return _people_response(stats, filters=filters)


@router.get("/admin/usage/seats", response_model=UsageSeatsResponse)
async def get_usage_seats(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[UsageService, Depends(get_usage_service)],
    filters: Annotated[JobFilters, Depends(get_job_filters)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UsageSeatsResponse:
    """Return the Seats & Licence tab's figures, incl. the licensed cap."""
    await _require_usage_view(db, current_user.id)
    stats = await service.seats(db, filters=filters, settings=settings)
    return _seats_response(stats, filters=filters)


@router.get("/admin/usage/infra", response_model=UsageInfraResponse)
async def get_usage_infra(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[UsageService, Depends(get_usage_service)],
    filters: Annotated[JobFilters, Depends(get_job_filters)],
) -> UsageInfraResponse:
    """Return the Infrastructure tab's figures."""
    await _require_usage_view(db, current_user.id)
    stats = await service.infra(db, filters=filters)
    return _infra_response(stats, filters=filters)


async def _tab_tables(
    service: UsageService,
    db: AsyncSession,
    tab: str,
    filters: JobFilters,
) -> list[usage_export.RenderedTable]:
    """Compute one tab's stats and turn them into export tables."""
    if tab == "overview":
        return usage_export.build_overview_tables(await service.overview(db, filters=filters))
    if tab == "compute":
        return usage_export.build_compute_tables(await service.compute(db, filters=filters))
    if tab == "reliability":
        return usage_export.build_reliability_tables(await service.reliability(db, filters=filters))
    if tab == "people":
        return usage_export.build_people_tables(await service.people(db, filters=filters))
    if tab == "seats":
        return usage_export.build_seats_tables(await service.seats(db, filters=filters))
    return usage_export.build_infra_tables(await service.infra(db, filters=filters))


def _period_label(filters: JobFilters) -> str:
    """Human "since → until" label for the report header."""
    return f"{filters.since.strftime('%Y-%m-%d')} → {filters.until.strftime('%Y-%m-%d')}"


def _filters_label(filters: JobFilters) -> str:
    """Human summary of the active narrowings, or "All"."""
    parts = []
    if filters.tool_aliases:
        parts.append(f"Tools: {', '.join(filters.tool_aliases)}")
    if filters.team_ids:
        parts.append(f"Teams: {', '.join(str(t) for t in filters.team_ids)}")
    if filters.roles:
        parts.append(f"Roles: {', '.join(filters.roles)}")
    if filters.execution_targets:
        parts.append(f"Targets: {', '.join(filters.execution_targets)}")
    return " · ".join(parts) if parts else "All"


@router.get("/admin/usage/export")
async def get_usage_export(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[UsageService, Depends(get_usage_service)],
    filters: Annotated[JobFilters, Depends(get_job_filters)],
    tab: Annotated[list[str] | None, Query()] = None,
    fmt: Annotated[str, Query(alias="format")] = "csv",
) -> Response:
    """Export the selected tabs as a CSV ZIP (``csv``) or printable HTML (``html``).

    ``tab`` is repeatable and chooses which tabs to include (omitted ⇒
    all). Only the chosen tabs' stats are computed.
    """
    await _require_usage_view(db, current_user.id)
    if fmt not in ("csv", "html"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported export format; expected 'csv' or 'html'.",
        )
    selected = [t for t in (tab or list(usage_export.TAB_LABELS)) if usage_export.is_known_tab(t)]
    if not selected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select at least one valid tab to export.",
        )

    tables: list[usage_export.RenderedTable] = []
    for tab_id in selected:
        tables.extend(await _tab_tables(service, db, tab_id, filters))

    stamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%d-%H%M")
    if fmt == "html":
        body = usage_export.render_report_html(
            tables,
            title="MATOFX — Usage Statistics",
            period_label=_period_label(filters),
            filters_label=_filters_label(filters),
            generated_at=_dt.datetime.now(tz=_dt.UTC),
        )
        return Response(
            content=body,
            media_type="text/html",
            headers={"Content-Disposition": f'attachment; filename="usage-{stamp}.html"'},
        )
    data = usage_export.tables_to_zip(tables)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="usage-{stamp}.zip"'},
    )


@router.post("/admin/usage/digest/send", response_model=UsageDigestResponse)
async def post_usage_digest_send(
    body: UsageDigestSendRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[UsageService, Depends(get_usage_service)],
    digest: Annotated[DigestService, Depends(get_digest_service)],
    filters: Annotated[JobFilters, Depends(get_job_filters)],
) -> UsageDigestResponse:
    """Email the selected tabs' report to ``recipients`` now.

    The report content is identical to the page's HTML export. Recurring
    delivery is Phase 2 (no cron worker yet); this is the on-demand send.
    """
    await _require_usage_view(db, current_user.id)
    selected = [
        t for t in (body.tabs or list(usage_export.TAB_LABELS)) if usage_export.is_known_tab(t)
    ]
    if not selected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select at least one valid tab to include in the digest.",
        )
    recipients = [r.strip() for r in body.recipients if "@" in r]
    if not recipients:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one valid email address.",
        )

    tables: list[usage_export.RenderedTable] = []
    for tab_id in selected:
        tables.extend(await _tab_tables(service, db, tab_id, filters))
    period = _period_label(filters)
    html_body = usage_export.render_report_html(
        tables,
        title="MATOFX — Usage Statistics",
        period_label=period,
        filters_label=_filters_label(filters),
        generated_at=_dt.datetime.now(tz=_dt.UTC),
    )
    text_body = (
        f"MATOFX Usage Statistics report for {period}.\n"
        "Open this email in an HTML-capable client to view the report."
    )
    result = await digest.send(
        db,
        actor_user_id=current_user.id,
        recipients=recipients,
        subject=f"MATOFX Usage Statistics — {period}",
        html_body=html_body,
        text_body=text_body,
    )
    return UsageDigestResponse(
        disabled=result.disabled,
        attempted=result.attempted,
        sent=result.sent,
        failed=[UsageDigestFailure(email=f["email"], reason=f["reason"]) for f in result.failed],
    )


@router.get("/admin/usage/filters", response_model=UsageFiltersResponse)
async def get_usage_filters(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[UsageService, Depends(get_usage_service)],
) -> UsageFiltersResponse:
    """Return the filter-bar dropdown options (tools / teams / roles / targets)."""
    await _require_usage_view(db, current_user.id)
    options = await service.filter_options(db)
    return UsageFiltersResponse(
        tools=[UsageFilterTool(alias=t.alias, name=t.name) for t in options.tools],
        teams=[UsageFilterTeam(id=t.id, name=t.name) for t in options.teams],
        roles=options.roles,
        execution_targets=options.execution_targets,
    )
