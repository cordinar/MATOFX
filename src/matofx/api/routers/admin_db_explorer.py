"""Admin → Explore Database router.

Read-only DB inspection surface for the legacy ``Explore Database``
admin page port. Endpoints:

- ``GET /admin/db/tables`` — list of allowed tables with row counts and
  visible/redacted column metadata.
- ``GET /admin/db/tables/{name}`` — one page of rows, with optional
  per-column substring filters, sort, and pagination.
- ``GET /admin/users/{user_id}/activity`` — the per-user dashboard
  that powers the User Activity tab: owned projects (with scenario
  counts), jobs created or cancelled, audit events, and logon-grant /
  revoke history. Top-N per section.

All endpoints require the ``database.explore`` permission, which is
seeded onto the ``administrator`` role and is implicit for
``super_user``.

The actual SQL lives in :mod:`matofx.db.explorer`; this router only
maps HTTP query params to that helper, validates inputs, and turns
domain errors into the right HTTP status codes.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import get_current_user, get_db, get_settings
from matofx.api.schemas import (
    AdminAuditFiltersResponse,
    AdminAuditLogEntry,
    AdminAuditLogPage,
    AdminAuditVerifyFailure,
    AdminAuditVerifyResponse,
    AdminDbColumnDistinctResponse,
    AdminDbTableInfo,
    AdminDbTableListResponse,
    AdminDbTablePageResponse,
    AdminDiagnosticsFailedJobExample,
    AdminDiagnosticsFailedJobs,
    AdminDiagnosticsOrphanedDir,
    AdminDiagnosticsOrphanedDirs,
    AdminDiagnosticsQueueDepth,
    AdminDiagnosticsRecentUser,
    AdminDiagnosticsResponse,
    AdminUserActivityAuditEvent,
    AdminUserActivityJob,
    AdminUserActivityLogonEvent,
    AdminUserActivityNamedRecord,
    AdminUserActivityProject,
    AdminUserActivityResponse,
    AdminUserActivityScenario,
    AdminUserActivitySubject,
    AdminUserActivityWorkflow,
)
from matofx.audit.orm import AuditLog
from matofx.config import Settings
from matofx.db.explorer import (
    FilterError,
    FilterTooComplexError,
    TableNotFoundError,
    UnknownColumnError,
    column_distinct_values,
    list_visible_tables,
    read_table_page,
)
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.models.orm import SurrogateModel
from matofx.studies.cohort.orm import JobCohort
from matofx.studies.optimisation.orm import OptimisationStudy
from matofx.studies.orm import ParametricStudy
from matofx.tools.exec_orm import StorageLocation
from matofx.users.orm import User, UserLogonEvent
from matofx.users.permissions import Permission, user_has_permission
from matofx.users.role_orm import Role
from matofx.workflows.orm import Workflow, WorkflowRun

_log = logging.getLogger(__name__)

router = APIRouter()


async def _require_explore(session: AsyncSession, actor_user_id: int) -> None:
    """403 unless the caller holds ``database.explore``."""
    if not await user_has_permission(session, actor_user_id, Permission.DATABASE_EXPLORE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You don't have permission to explore the database "
                "(database.explore). Ask an administrator if you need access."
            ),
        )


async def _require_audit_read(session: AsyncSession, actor_user_id: int) -> None:
    """403 unless the caller holds ``audit.read``.

    Separate from ``database.explore`` so the audit-log surface can be
    granted to a compliance reviewer without unlocking the wider DB
    browser, and vice-versa.
    """
    if not await user_has_permission(session, actor_user_id, Permission.AUDIT_READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You don't have permission to read the audit log "
                "(audit.read). Ask an administrator if you need access."
            ),
        )


@router.get("/admin/db/tables", response_model=AdminDbTableListResponse)
async def list_tables_endpoint(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminDbTableListResponse:
    """Return every table the explorer is willing to surface."""
    await _require_explore(db, current_user.id)
    rows = await list_visible_tables(db)
    return AdminDbTableListResponse(
        tables=[
            AdminDbTableInfo(
                name=row.name,
                row_count=row.row_count,
                columns=list(row.columns),
                redacted_columns=list(row.redacted_columns),
            )
            for row in rows
        ]
    )


@router.get("/admin/db/tables/{name}", response_model=AdminDbTablePageResponse)
async def read_table_endpoint(
    name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=1000)] = 100,
    sort_by: Annotated[str | None, Query()] = None,
    sort_order: Annotated[str, Query(pattern="^(?i)(asc|desc)$")] = "asc",
    filters: Annotated[str | None, Query(description="JSON filter tree (root group)")] = None,
) -> AdminDbTablePageResponse:
    """Return one page of rows for ``name`` (an allowed table).

    Query parameters:

    - ``page`` / ``page_size`` — pagination, 1-based.
    - ``sort_by`` / ``sort_order`` — column to sort on (must be a
      non-redacted column) and direction.
    - ``filters`` — JSON-encoded recursive boolean filter tree whose
      root is a group ``{"op": "and"|"or", "children": [...]}``. Leaves
      are ``{"column", "match": "values"|"text", ...}``. See
      :func:`matofx.db.explorer.read_table_page` for the full shape.
      Leaf columns must be non-redacted.
    """
    await _require_explore(db, current_user.id)

    parsed_filters: dict[str, Any] | None = None
    if filters:
        try:
            raw = json.loads(filters)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The 'filters' parameter must be a JSON-encoded filter tree.",
            ) from exc
        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The 'filters' parameter must decode to a JSON object (the root group).",
            )
        parsed_filters = raw

    try:
        page_data = await read_table_page(
            db,
            table_name=name,
            sort_by=sort_by,
            sort_order=sort_order,
            filters=parsed_filters,
            page=page,
            page_size=page_size,
        )
    except TableNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (UnknownColumnError, FilterError, FilterTooComplexError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return AdminDbTablePageResponse(
        columns=list(page_data.columns),
        redacted_columns=list(page_data.redacted_columns),
        rows=page_data.rows,
        total_rows=page_data.total_rows,
        page=page_data.page,
        page_size=page_data.page_size,
    )


@router.get(
    "/admin/db/tables/{name}/columns/{column}/distinct",
    response_model=AdminDbColumnDistinctResponse,
)
async def column_distinct_endpoint(
    name: str,
    column: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminDbColumnDistinctResponse:
    """Distinct-value summary for one column, driving the value-picker.

    Returns up to the explorer's distinct-value limit of values (as
    text). Above the limit, ``too_many`` is true and the UI uses
    free-text search only. ``has_null`` reports whether an "(empty)"
    choice should be offered.
    """
    await _require_explore(db, current_user.id)
    try:
        result = await column_distinct_values(db, table_name=name, column=column)
    except TableNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except UnknownColumnError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return AdminDbColumnDistinctResponse(
        column=result.column,
        too_many=result.too_many,
        values=list(result.values),
        has_null=result.has_null,
        limit=result.limit,
    )


@router.get("/admin/users/{user_id}/activity", response_model=AdminUserActivityResponse)
async def get_user_activity(
    user_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> AdminUserActivityResponse:
    """Bundle every per-user data slice powering the User Activity tab.

    Each section is capped at ``limit`` rows, newest first: owned
    projects, scenarios created, jobs submitted, workflows owned or run
    (admin-released and custom), parametric studies, optimisation
    studies, surrogate models and job cohorts the user created, audit
    events where the actor was this user, and logon grant/revoke
    history.
    """
    await _require_explore(db, current_user.id)

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No user with id {user_id}.",
        )

    role_name = ""
    if user.role_id is not None:
        role_row = await db.get(Role, user.role_id)
        if role_row is not None:
            role_name = role_row.name

    subject = AdminUserActivitySubject(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        role=role_name,
        is_active=user.is_active,
        is_approver=user.is_approver,
    )

    projects = await _user_projects(db, user_id, limit)
    scenarios = await _user_scenarios(db, user_id, limit)
    jobs = await _user_jobs(db, user_id, limit)
    workflows = await _user_workflows(db, user_id, limit)
    parametric_studies = await _owned_named_records(
        db, ParametricStudy, ParametricStudy.created_by_user_id, user_id, limit
    )
    optimisations = await _owned_named_records(
        db, OptimisationStudy, OptimisationStudy.created_by_user_id, user_id, limit
    )
    surrogate_models = await _owned_named_records(
        db, SurrogateModel, SurrogateModel.owner_user_id, user_id, limit
    )
    cohorts = await _owned_named_records(
        db, JobCohort, JobCohort.created_by_user_id, user_id, limit
    )
    audit_events = await _user_audit_events(db, user_id, limit)
    logon_events = await _user_logon_events(db, user_id, limit)

    return AdminUserActivityResponse(
        subject=subject,
        projects=projects,
        scenarios=scenarios,
        jobs=jobs,
        workflows=workflows,
        parametric_studies=parametric_studies,
        optimisations=optimisations,
        surrogate_models=surrogate_models,
        cohorts=cohorts,
        audit_events=audit_events,
        logon_events=logon_events,
    )


async def _user_projects(
    db: AsyncSession, user_id: int, limit: int
) -> list[AdminUserActivityProject]:
    """Owned projects + cheap scenario count, newest first."""
    project_stmt = (
        sa.select(
            Project.id,
            Project.name,
            Project.description,
            Project.created_at,
            sa.func.count(Scenario.id).label("scenario_count"),
        )
        .outerjoin(Scenario, Scenario.project_id == Project.id)
        .where(Project.creator_user_id == user_id)
        .group_by(Project.id, Project.name, Project.description, Project.created_at)
        .order_by(Project.created_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(project_stmt)).all()
    return [
        AdminUserActivityProject(
            id=row.id,
            name=row.name,
            description=row.description or "",
            created_at=row.created_at,
            scenario_count=int(row.scenario_count or 0),
        )
        for row in rows
    ]


async def _user_jobs(db: AsyncSession, user_id: int, limit: int) -> list[AdminUserActivityJob]:
    """Jobs the user submitted, newest first.

    Strict creator filter — cancellations the user requested on
    someone else's job are recorded in the audit trail, not surfaced
    here, so this list reads cleanly as "what they ran".
    """
    stmt = (
        sa.select(Job)
        .where(Job.created_by_user_id == user_id)
        .order_by(Job.created_at.desc())
        .limit(limit)
    )
    out: list[AdminUserActivityJob] = []
    for row in (await db.execute(stmt)).scalars().all():
        out.append(
            AdminUserActivityJob(
                id=row.id,
                name=row.name,
                tool_alias=row.tool_alias,
                state=row.state,
                created_at=row.created_at,
                completed_at=row.completed_at,
                scenario_id=row.scenario_id,
            )
        )
    return out


async def _user_workflows(
    db: AsyncSession, user_id: int, limit: int
) -> list[AdminUserActivityWorkflow]:
    """Workflows the user owns or has run — admin-released and custom.

    Two sets are merged into one row per distinct workflow:

    * **Owns** — the user is the current ``owner_id`` of a custom
      workflow, or the ``installed_by_user_id`` of record (covers an
      admin who installed an admin-released workflow).
    * **Has run** — the user submitted at least one
      :class:`~matofx.workflows.orm.WorkflowRun` of it.

    Each row carries *this user's* run count and most-recent run, then
    the merged list is ordered by last activity (newest run, falling
    back to the workflow's creation) and capped at ``limit``. Both
    contributing queries are individually capped at ``limit`` so the
    merge stays bounded to about twice ``limit`` rows.
    """
    # Workflows the user owns / installed.
    owned_stmt = (
        sa.select(Workflow)
        .where(
            sa.or_(
                Workflow.owner_id == user_id,
                Workflow.installed_by_user_id == user_id,
            )
        )
        .order_by(Workflow.created_at.desc())
        .limit(limit)
    )
    loaded: dict[int, Workflow] = {
        wf.id: wf for wf in (await db.execute(owned_stmt)).scalars().all()
    }

    # Per-workflow tally of the runs this user submitted.
    run_stmt = (
        sa.select(
            WorkflowRun.workflow_id,
            sa.func.count(WorkflowRun.id).label("run_count"),
            sa.func.max(WorkflowRun.created_at).label("last_run_at"),
        )
        .where(WorkflowRun.submitted_by_user_id == user_id)
        .group_by(WorkflowRun.workflow_id)
        .order_by(sa.func.max(WorkflowRun.created_at).desc())
        .limit(limit)
    )
    runs_by_wf: dict[int, tuple[int, _dt.datetime | None]] = {
        int(r.workflow_id): (int(r.run_count or 0), r.last_run_at)
        for r in (await db.execute(run_stmt)).all()
    }

    # Pull in any run-only workflows not already loaded from the owned set.
    missing_ids = [wid for wid in runs_by_wf if wid not in loaded]
    if missing_ids:
        extra_stmt = sa.select(Workflow).where(Workflow.id.in_(missing_ids))
        for wf in (await db.execute(extra_stmt)).scalars().all():
            loaded[wf.id] = wf

    items: list[AdminUserActivityWorkflow] = []
    for wf in loaded.values():
        run_count, last_run_at = runs_by_wf.get(wf.id, (0, None))
        items.append(
            AdminUserActivityWorkflow(
                id=wf.id,
                alias=wf.alias,
                name=wf.name,
                kind=wf.kind,
                state=wf.state,
                owned=(user_id in (wf.owner_id, wf.installed_by_user_id)),
                archived=wf.archived_at is not None,
                run_count=run_count,
                last_run_at=last_run_at,
                created_at=wf.created_at,
            )
        )

    # Newest activity first: most recent run if any, else workflow creation.
    items.sort(key=lambda it: it.last_run_at or it.created_at, reverse=True)
    return items[:limit]


async def _owned_named_records(
    db: AsyncSession,
    model: Any,
    owner_column: Any,
    user_id: int,
    limit: int,
) -> list[AdminUserActivityNamedRecord]:
    """Records of ``model`` the user owns / created, newest first.

    Shared by the parametric-studies, optimisations, surrogate-models
    and job-cohorts sections — every one of those models exposes
    ``id`` / ``name`` / ``state`` / ``created_at`` and a single
    owner FK column (passed as ``owner_column``), so one query shape
    serves all four.
    """
    stmt = (
        sa.select(model.id, model.name, model.state, model.created_at)
        .where(owner_column == user_id)
        .order_by(model.created_at.desc())
        .limit(limit)
    )
    return [
        AdminUserActivityNamedRecord(
            id=int(row.id),
            name=row.name,
            state=row.state,
            created_at=row.created_at,
        )
        for row in (await db.execute(stmt)).all()
    ]


async def _user_scenarios(
    db: AsyncSession, user_id: int, limit: int
) -> list[AdminUserActivityScenario]:
    """Scenarios the user created.

    ``scenario`` has no ``created_by_user_id`` column, so we attribute
    creation by the audit log: rows with
    ``action='hierarchy.scenario.create'`` and ``actor='user:{id}'``
    and ``outcome='success'``. The ``resource_id`` strings are then
    joined back to the live ``scenario`` table — scenarios that have
    been deleted since the audit row was written drop out silently
    rather than rendering as ghost rows.
    """
    actor = f"user:{user_id}"
    audit_stmt = (
        sa.select(AuditLog.resource_id)
        .where(
            AuditLog.actor == actor,
            AuditLog.action == "hierarchy.scenario.create",
            AuditLog.outcome == "success",
        )
        .order_by(AuditLog.timestamp.desc())
        .limit(limit)
    )
    audit_rows = (await db.execute(audit_stmt)).all()
    scenario_ids: list[int] = []
    for (resource_id,) in audit_rows:
        if isinstance(resource_id, str) and resource_id.isdigit():
            scenario_ids.append(int(resource_id))
    if not scenario_ids:
        return []

    stmt = (
        sa.select(
            Scenario.id,
            Scenario.name,
            Scenario.project_id,
            Scenario.created_at,
            Project.name.label("project_name"),
        )
        .join(Project, Project.id == Scenario.project_id)
        .where(Scenario.id.in_(scenario_ids))
    )
    rows_by_id = {row.id: row for row in (await db.execute(stmt)).all()}

    # Re-emit in audit-log (newest-first) order, dropping any that no
    # longer exist.
    return [
        AdminUserActivityScenario(
            id=rows_by_id[sid].id,
            name=rows_by_id[sid].name,
            project_id=rows_by_id[sid].project_id,
            project_name=rows_by_id[sid].project_name,
            created_at=rows_by_id[sid].created_at,
        )
        for sid in scenario_ids
        if sid in rows_by_id
    ]


async def _user_audit_events(
    db: AsyncSession, user_id: int, limit: int
) -> list[AdminUserActivityAuditEvent]:
    """Audit rows whose ``actor`` matches the user — newest first.

    Services write user-driven actions with ``actor=f"user:{user_id}"``
    so the lookup is exact-match on a single string. Audit rows whose
    actor is ``"system"`` or ``"anonymous"`` are not returned here even
    if they touched data the user owns.
    """
    actor = f"user:{user_id}"
    stmt = (
        sa.select(AuditLog)
        .where(AuditLog.actor == actor)
        .order_by(AuditLog.timestamp.desc())
        .limit(limit)
    )
    return [
        AdminUserActivityAuditEvent(
            id=row.id,
            timestamp=row.timestamp,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            outcome=row.outcome,
            reason=row.reason,
        )
        for row in (await db.execute(stmt)).scalars().all()
    ]


async def _user_logon_events(
    db: AsyncSession, user_id: int, limit: int
) -> list[AdminUserActivityLogonEvent]:
    """Grant / revoke history for the user, newest first."""
    stmt = (
        sa.select(UserLogonEvent)
        .where(UserLogonEvent.user_id == user_id)
        .order_by(UserLogonEvent.at.desc())
        .limit(limit)
    )
    return [
        AdminUserActivityLogonEvent(
            id=row.id,
            event=row.event,
            at=row.at,
            actor_user_id=row.actor_user_id,
            reason=row.reason,
        )
        for row in (await db.execute(stmt)).scalars().all()
    ]


# ---------------------------------------------------------------------------
# Diagnostics tab
# ---------------------------------------------------------------------------

# Cap how much each diagnostics card can return so a runaway DB or
# directory listing can't blow up the response. Tuned for "operational
# dashboard": enough rows to be useful, few enough to render snappily.
_FAILED_JOBS_EXAMPLE_LIMIT = 10
_RECENT_USERS_LIMIT = 10
_ORPHAN_DIRS_LIMIT = 100

_USER_ACTOR_PATTERN = re.compile(r"^user:(\d+)$")
_JOB_DIR_PATTERN = re.compile(r"^job_(\d+)$")


@router.get("/admin/db/diagnostics", response_model=AdminDiagnosticsResponse)
async def get_diagnostics(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminDiagnosticsResponse:
    """Curated operational snapshot for the Diagnostics tab.

    Four small cards — failed jobs in the last 7 days, current runner
    queue depth, recently active users, and orphaned working
    directories. Each card answers an "is something wrong right now?"
    question without the operator having to write SQL.
    """
    await _require_explore(db, current_user.id)
    failed = await _diag_failed_jobs(db)
    queue = await _diag_queue_depth(db)
    recent_users = await _diag_recent_users(db)
    orphans = await _diag_orphaned_working_dirs(db)
    return AdminDiagnosticsResponse(
        failed_jobs_7d=failed,
        queue_depth=queue,
        recent_users=recent_users,
        orphaned_working_dirs=orphans,
    )


async def _diag_failed_jobs(db: AsyncSession) -> AdminDiagnosticsFailedJobs:
    """Count + newest examples of failed jobs in the past 7 days.

    The 7-day window is taken on ``Job.updated_at`` — failed jobs
    always bump it on the state transition, and the column is
    NOT NULL, so it works even when ``completed_at`` is null (which
    can happen when the runner promotes ``failed`` before the
    heartbeat reports a completion timestamp).
    """
    cutoff = _dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(days=7)
    count_stmt = sa.select(sa.func.count(Job.id)).where(
        Job.state == "failed",
        Job.updated_at >= cutoff,
    )
    count = int((await db.execute(count_stmt)).scalar_one() or 0)

    examples: list[AdminDiagnosticsFailedJobExample] = []
    if count:
        ex_stmt = (
            sa.select(Job)
            .where(Job.state == "failed", Job.updated_at >= cutoff)
            .order_by(Job.updated_at.desc())
            .limit(_FAILED_JOBS_EXAMPLE_LIMIT)
        )
        for row in (await db.execute(ex_stmt)).scalars().all():
            examples.append(
                AdminDiagnosticsFailedJobExample(
                    id=row.id,
                    name=row.name,
                    tool_alias=row.tool_alias,
                    scenario_id=row.scenario_id,
                    completed_at=row.completed_at,
                    updated_at=row.updated_at,
                )
            )
    return AdminDiagnosticsFailedJobs(count=count, examples=examples)


async def _diag_queue_depth(db: AsyncSession) -> AdminDiagnosticsQueueDepth:
    """Live counts of jobs in the queued / running states."""
    stmt = (
        sa.select(Job.state, sa.func.count(Job.id))
        .where(Job.state.in_(("queued", "running")))
        .group_by(Job.state)
    )
    counts: dict[str, int] = {row[0]: int(row[1]) for row in (await db.execute(stmt)).all()}
    return AdminDiagnosticsQueueDepth(
        queued=counts.get("queued", 0),
        running=counts.get("running", 0),
    )


async def _diag_recent_users(db: AsyncSession) -> list[AdminDiagnosticsRecentUser]:
    """Top-N users by latest audit-event timestamp.

    Queries the audit log for distinct ``actor`` values matching
    ``user:{id}`` and takes the max timestamp per actor. Then resolves
    those ids back to the live ``user`` table — users that have been
    deleted (rare in v1, but possible in future) drop out silently.
    """
    actor_stmt = (
        sa.select(AuditLog.actor, sa.func.max(AuditLog.timestamp).label("last_at"))
        .where(AuditLog.actor.like("user:%"))
        .group_by(AuditLog.actor)
        .order_by(sa.func.max(AuditLog.timestamp).desc())
        .limit(_RECENT_USERS_LIMIT)
    )
    rows = (await db.execute(actor_stmt)).all()
    if not rows:
        return []

    last_seen: dict[int, _dt.datetime] = {}
    for actor, last_at in rows:
        match = _USER_ACTOR_PATTERN.match(str(actor))
        if not match:
            continue
        last_seen[int(match.group(1))] = last_at

    if not last_seen:
        return []

    user_stmt = sa.select(User.id, User.username, User.full_name).where(
        User.id.in_(list(last_seen.keys()))
    )
    user_rows = {row.id: row for row in (await db.execute(user_stmt)).all()}

    out: list[AdminDiagnosticsRecentUser] = []
    # Re-emit in audit-log (newest-first) order, dropping ids whose
    # user row is gone.
    for actor, last_at in rows:
        match = _USER_ACTOR_PATTERN.match(str(actor))
        if not match:
            continue
        uid = int(match.group(1))
        urow = user_rows.get(uid)
        if urow is None:
            continue
        out.append(
            AdminDiagnosticsRecentUser(
                user_id=uid,
                username=urow.username,
                full_name=urow.full_name,
                last_active_at=last_at,
            )
        )
    return out


async def _diag_orphaned_working_dirs(db: AsyncSession) -> AdminDiagnosticsOrphanedDirs:
    """Find ``jobs/job_NNNNNNN`` dirs whose job_id has no live row.

    Iterates every ``storage_location`` (active *and* retired — old
    mounts are exactly where stale dirs accumulate) and lists
    ``<mount_path>/jobs/``. Any subdirectory matching ``job_<digits>``
    whose id isn't in the ``job`` table is an orphan candidate.

    Best-effort: a missing or unreadable mount path doesn't fail the
    whole diagnostics page — the card just reports ``checked=False``
    with a friendly note. Items are capped at ``_ORPHAN_DIRS_LIMIT`` so
    a directory full of stale dirs doesn't blow up the response.
    """
    job_ids: set[int] = {row[0] for row in (await db.execute(sa.select(Job.id))).all()}

    locations = (
        (await db.execute(sa.select(StorageLocation).order_by(StorageLocation.id))).scalars().all()
    )

    items: list[AdminDiagnosticsOrphanedDir] = []
    unreachable: list[str] = []

    for sl in locations:
        if len(items) >= _ORPHAN_DIRS_LIMIT:
            break
        try:
            jobs_dir = Path(sl.mount_path) / "jobs"
            if not jobs_dir.exists():
                continue
            entries = list(os.scandir(jobs_dir))
        except OSError as exc:
            _log.warning(
                "diagnostics: couldn't list orphan candidates under storage_location %s: %s",
                sl.id,
                exc,
            )
            unreachable.append(sl.name)
            continue

        for entry in entries:
            if len(items) >= _ORPHAN_DIRS_LIMIT:
                break
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            match = _JOB_DIR_PATTERN.match(entry.name)
            if not match:
                continue
            job_id = int(match.group(1))
            if job_id in job_ids:
                continue
            items.append(
                AdminDiagnosticsOrphanedDir(
                    storage_location_id=sl.id,
                    storage_location_name=sl.name,
                    relative_path=f"jobs/{entry.name}",
                )
            )

    note = ""
    if unreachable:
        names = ", ".join(unreachable)
        note = (
            f"Couldn't read the working-dir tree on: {names}. "
            "The API host may not have the share mounted; orphans on "
            "those locations weren't checked."
        )
    # ``checked`` is only False when *every* configured location was
    # unreachable — partial coverage still counts as "we ran the check"
    # and the UI relies on ``note`` to surface the caveat.
    fully_failed = bool(locations) and len(unreachable) == len(locations)
    return AdminDiagnosticsOrphanedDirs(
        checked=not fully_failed,
        note=note,
        items=items,
    )


# --- audit log browser -------------------------------------------------------


_AUDIT_OUTCOMES: frozenset[str] = frozenset({"success", "failure", "denied"})
_AUDIT_ACTOR_TYPES: frozenset[str] = frozenset({"user", "service", "system", "anonymous"})
# Hard ceiling on a single CSV export so a filter that matches the whole
# log can't stream an unbounded response. Surfaced in the export headers.
_AUDIT_EXPORT_CAP = 50_000


def _parse_iso(value: str | None, *, field_name: str) -> _dt.datetime | None:
    """Parse a caller-supplied ISO-8601 timestamp; HTTP-400 on malformed input."""
    if value is None:
        return None
    try:
        # ``fromisoformat`` accepts the ``Z`` suffix on 3.11+.
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{field_name}' must be an ISO-8601 timestamp.",
        ) from exc


async def _resolve_user_actors(db: AsyncSession, term: str) -> list[str]:
    """Map a free-text user query to matching ``user:<id>`` actor strings.

    Matches ``users`` on numeric id (exact), full name (case-insensitive
    substring), or username (case-insensitive substring) — so an operator
    can type ``42``, ``Robert``, or ``rcordina`` interchangeably. Returns
    the ``user:<id>`` actor tokens; an empty list means "no such user",
    which the caller turns into a match-nothing condition.
    """
    like = f"%{term}%"
    conds: list[sa.ColumnElement[bool]] = [
        User.full_name.ilike(like),
        User.username.ilike(like),
    ]
    if term.isdigit():
        conds.append(User.id == int(term))
    stmt = sa.select(User.id).where(sa.or_(*conds))
    ids = (await db.execute(stmt)).scalars().all()
    return [f"user:{uid}" for uid in ids]


async def _build_audit_conditions(
    db: AsyncSession,
    *,
    user: str | None,
    actor: str | None,
    actor_type: str | None,
    actions: list[str] | None,
    resource_types: list[str] | None,
    resource_id: str | None,
    outcome: str | None,
    correlation_id: str | None,
    since: str | None,
    until: str | None,
) -> list[sa.ColumnElement[bool]]:
    """Build the AND-combined WHERE conditions shared by list + export.

    Validation lives here so both endpoints reject bad input identically.
    ``actions`` / ``resource_types`` are exact ``IN`` matches (picked from
    real values); ``user`` resolves to ``user:<id>`` actors; ``actor`` is
    an exact fallback used by internal callers / deep-links.
    """
    if outcome is not None and outcome not in _AUDIT_OUTCOMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'outcome' must be one of {sorted(_AUDIT_OUTCOMES)} — got {outcome!r}.",
        )
    if actor_type is not None and actor_type not in _AUDIT_ACTOR_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"'actor_type' must be one of {sorted(_AUDIT_ACTOR_TYPES)} — got {actor_type!r}."
            ),
        )

    since_dt = _parse_iso(since, field_name="since")
    until_dt = _parse_iso(until, field_name="until")

    conditions: list[sa.ColumnElement[bool]] = []
    if user:
        matched = await _resolve_user_actors(db, user.strip())
        # No matching user ⇒ the filter can't match any row. ``false()``
        # short-circuits the query rather than silently ignoring the term.
        conditions.append(AuditLog.actor.in_(matched) if matched else sa.false())
    if actor:
        conditions.append(AuditLog.actor == actor)
    if actor_type:
        conditions.append(AuditLog.actor_type == actor_type)
    if actions:
        conditions.append(AuditLog.action.in_(actions))
    if resource_types:
        conditions.append(AuditLog.resource_type.in_(resource_types))
    if resource_id:
        conditions.append(AuditLog.resource_id == resource_id)
    if outcome:
        conditions.append(AuditLog.outcome == outcome)
    if correlation_id:
        conditions.append(AuditLog.correlation_id == correlation_id)
    if since_dt is not None:
        conditions.append(AuditLog.timestamp >= since_dt)
    if until_dt is not None:
        conditions.append(AuditLog.timestamp <= until_dt)
    return conditions


async def _actor_labels(db: AsyncSession, actors: set[str]) -> dict[str, str]:
    """Resolve ``user:<id>`` actor strings to ``Full Name (username)`` labels.

    Non-user actors (``system``, ``service:*``, ``anonymous``) aren't looked
    up — the caller falls back to echoing the raw actor for those. Returns a
    map keyed by the raw actor string, only for users that still exist.
    """
    user_ids: dict[int, str] = {}
    for a in actors:
        if a.startswith("user:"):
            suffix = a.split(":", 1)[1]
            if suffix.isdigit():
                user_ids[int(suffix)] = a
    if not user_ids:
        return {}
    stmt = sa.select(User.id, User.full_name, User.username).where(User.id.in_(user_ids))
    labels: dict[str, str] = {}
    for uid, full_name, username in (await db.execute(stmt)).all():
        full = (full_name or "").strip()
        username_str = str(username or "")
        label = f"{full} ({username_str})" if full else username_str
        labels[user_ids[int(uid)]] = label or user_ids[int(uid)]
    return labels


@router.get("/admin/audit/log", response_model=AdminAuditLogPage)
async def list_audit_log(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
    user: Annotated[str | None, Query(max_length=255)] = None,
    actor: Annotated[str | None, Query(max_length=255)] = None,
    actor_type: Annotated[str | None, Query(max_length=32)] = None,
    action: Annotated[list[str] | None, Query()] = None,
    resource_type: Annotated[list[str] | None, Query()] = None,
    resource_id: Annotated[str | None, Query(max_length=255)] = None,
    outcome: Annotated[str | None, Query()] = None,
    correlation_id: Annotated[str | None, Query(max_length=64)] = None,
    since: Annotated[str | None, Query(description="ISO-8601 lower bound")] = None,
    until: Annotated[str | None, Query(description="ISO-8601 upper bound")] = None,
) -> AdminAuditLogPage:
    """Return one page of audit-log rows, newest first.

    All filters are optional and AND-combined. ``user`` matches the actor
    by numeric id / full name / username (see :func:`_resolve_user_actors`);
    ``actor_type`` narrows to a class of actor; ``action`` and
    ``resource_type`` are repeatable exact-match filters (OR within each,
    picked from the values actually present via ``/admin/audit/filters``).
    """
    await _require_audit_read(db, current_user.id)

    conditions = await _build_audit_conditions(
        db,
        user=user,
        actor=actor,
        actor_type=actor_type,
        actions=action,
        resource_types=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        correlation_id=correlation_id,
        since=since,
        until=until,
    )

    count_stmt = sa.select(sa.func.count()).select_from(AuditLog)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total_rows = int((await db.execute(count_stmt)).scalar_one())

    page_stmt = sa.select(AuditLog).order_by(AuditLog.id.desc())
    if conditions:
        page_stmt = page_stmt.where(*conditions)
    page_stmt = page_stmt.limit(page_size).offset((page - 1) * page_size)
    rows = (await db.execute(page_stmt)).scalars().all()

    labels = await _actor_labels(db, {row.actor for row in rows})

    entries = [
        AdminAuditLogEntry(
            id=row.id,
            timestamp=row.timestamp,
            actor=row.actor,
            actor_label=labels.get(row.actor, row.actor),
            actor_type=row.actor_type,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            outcome=row.outcome,
            request_id=row.request_id,
            session_id=row.session_id,
            source_ip=row.source_ip,
            prior_hash=row.prior_hash,
            hash=row.hash,
            signature=row.signature,
            key_id=row.key_id,
            schema_version=row.schema_version,
            payload=dict(row.payload or {}),
            reason=row.reason,
            correlation_id=row.correlation_id,
        )
        for row in rows
    ]

    return AdminAuditLogPage(
        rows=entries,
        total_rows=total_rows,
        page=page,
        page_size=page_size,
    )


@router.get("/admin/audit/filters", response_model=AdminAuditFiltersResponse)
async def audit_log_filters(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminAuditFiltersResponse:
    """Distinct ``action`` / ``resource_type`` / ``actor_type`` values in the log.

    Powers the filter-bar dropdowns so an operator only ever sees choices
    that exist. ``DISTINCT`` scans the column; fine at current scale, and
    ``action`` / ``resource_type`` carry indexes for the common lookups.
    """
    await _require_audit_read(db, current_user.id)

    actions = (
        (await db.execute(sa.select(AuditLog.action).distinct().order_by(AuditLog.action)))
        .scalars()
        .all()
    )
    resource_types = (
        (
            await db.execute(
                sa.select(AuditLog.resource_type).distinct().order_by(AuditLog.resource_type)
            )
        )
        .scalars()
        .all()
    )
    actor_types = (
        (await db.execute(sa.select(AuditLog.actor_type).distinct().order_by(AuditLog.actor_type)))
        .scalars()
        .all()
    )

    return AdminAuditFiltersResponse(
        actions=list(actions),
        resource_types=list(resource_types),
        actor_types=list(actor_types),
    )


_AUDIT_EXPORT_COLUMNS: tuple[str, ...] = (
    "id",
    "timestamp",
    "actor",
    "actor_label",
    "actor_type",
    "action",
    "resource_type",
    "resource_id",
    "outcome",
    "reason",
    "correlation_id",
    "request_id",
    "session_id",
    "source_ip",
    "hash",
    "prior_hash",
    "signature",
    "key_id",
    "schema_version",
    "payload",
)


@router.get("/admin/audit/export")
async def export_audit_log(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    user: Annotated[str | None, Query(max_length=255)] = None,
    actor: Annotated[str | None, Query(max_length=255)] = None,
    actor_type: Annotated[str | None, Query(max_length=32)] = None,
    action: Annotated[list[str] | None, Query()] = None,
    resource_type: Annotated[list[str] | None, Query()] = None,
    resource_id: Annotated[str | None, Query(max_length=255)] = None,
    outcome: Annotated[str | None, Query()] = None,
    correlation_id: Annotated[str | None, Query(max_length=64)] = None,
    since: Annotated[str | None, Query(description="ISO-8601 lower bound")] = None,
    until: Annotated[str | None, Query(description="ISO-8601 upper bound")] = None,
) -> Response:
    """Download the current filtered view as CSV (newest first).

    Same filters as :func:`list_audit_log`, but unpaginated and capped at
    ``_AUDIT_EXPORT_CAP`` rows. When the cap truncates the result the
    ``X-Audit-Export-Truncated`` header is set so the caller can warn.
    """
    await _require_audit_read(db, current_user.id)

    conditions = await _build_audit_conditions(
        db,
        user=user,
        actor=actor,
        actor_type=actor_type,
        actions=action,
        resource_types=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        correlation_id=correlation_id,
        since=since,
        until=until,
    )

    stmt = sa.select(AuditLog).order_by(AuditLog.id.desc())
    if conditions:
        stmt = stmt.where(*conditions)
    # Fetch one past the cap so we can tell "exactly at the cap" from
    # "truncated" without a second COUNT query.
    stmt = stmt.limit(_AUDIT_EXPORT_CAP + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    truncated = len(rows) > _AUDIT_EXPORT_CAP
    rows = rows[:_AUDIT_EXPORT_CAP]

    labels = await _actor_labels(db, {row.actor for row in rows})

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_AUDIT_EXPORT_COLUMNS)
    for row in rows:
        writer.writerow(
            [
                row.id,
                row.timestamp.isoformat() if row.timestamp else "",
                row.actor,
                labels.get(row.actor, row.actor),
                row.actor_type,
                row.action,
                row.resource_type,
                row.resource_id,
                row.outcome,
                row.reason or "",
                row.correlation_id or "",
                row.request_id or "",
                row.session_id or "",
                row.source_ip or "",
                row.hash,
                row.prior_hash,
                row.signature or "",
                row.key_id or "",
                row.schema_version,
                json.dumps(row.payload or {}, sort_keys=True),
            ]
        )

    headers = {"Content-Disposition": 'attachment; filename="audit-log.csv"'}
    if truncated:
        headers["X-Audit-Export-Truncated"] = str(_AUDIT_EXPORT_CAP)
    return Response(content=buf.getvalue(), media_type="text/csv", headers=headers)


@router.post("/admin/audit/verify", response_model=AdminAuditVerifyResponse)
async def verify_audit_chain(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    start_id: Annotated[int | None, Query(ge=1)] = None,
    end_id: Annotated[int | None, Query(ge=1)] = None,
) -> AdminAuditVerifyResponse:
    """Verify the audit chain — same primitive as ``matofx audit verify``.

    Walks the chain (or a sub-range), recomputes each record's hash,
    checks links, and validates Ed25519 signatures when the install has
    a signing key configured. Returns the verifier's failure list
    verbatim so the UI can show exactly which record failed and why.
    """
    await _require_audit_read(db, current_user.id)

    from matofx.audit import load_signing_key, verify  # noqa: PLC0415
    from matofx.audit.keys import compute_key_id  # noqa: PLC0415

    public_keys = {}
    if settings.audit.signing_key_path is not None:
        priv = load_signing_key(settings.audit.signing_key_path)
        public_keys[compute_key_id(priv.public_key())] = priv.public_key()

    result = await verify(
        db,
        public_keys=public_keys or None,
        start_id=start_id,
        end_id=end_id,
    )

    return AdminAuditVerifyResponse(
        ok=result.ok,
        records_checked=result.records_checked,
        failures=[
            AdminAuditVerifyFailure(record_id=f.record_id, reason=f.reason) for f in result.failures
        ],
        signing_key_configured=bool(public_keys),
    )
