"""Workflows router — admin CRUD + user-facing visibility list.

Two surfaces ride this router under different prefixes:

- ``/api/admin/workflows*`` — gated on ``workflows.manage``. Create,
  edit, promote-state, archive, version-history.
- ``/api/workflows*`` — every authenticated user; returns only
  workflows visible to the caller (state + team + new-tool-cascade).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import Executable, select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import (
    get_current_user,
    get_db,
    get_settings,
    get_workflow_graph_service,
    get_workflow_run_service,
    get_workflow_service,
)
from matofx.api.schemas import (
    AdminWorkflowMemberEntry,
    AdminWorkflowResponse,
    CustomWorkflowArchiveRequest,
    CustomWorkflowCreateRequest,
    CustomWorkflowMemberEntry,
    CustomWorkflowResponse,
    CustomWorkflowShareCandidate,
    CustomWorkflowShareEntry,
    CustomWorkflowSummary,
    CustomWorkflowUpdateRequest,
    WorkflowArchiveRequest,
    WorkflowChainEntry,
    WorkflowCreateRequest,
    WorkflowGraphEdgeResponse,
    WorkflowGraphNodeResponse,
    WorkflowGraphRequest,
    WorkflowGraphResponse,
    WorkflowPromoteRequest,
    WorkflowRunChildJob,
    WorkflowRunResponse,
    WorkflowRunStartRequest,
    WorkflowSummaryResponse,
    WorkflowUpdateRequest,
    WorkflowVersionEntry,
    WorkflowVersionList,
)
from matofx.config import Settings
from matofx.hierarchy.errors import AccessDeniedError, NotFoundError
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.tools.orm import Tool
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission
from matofx.workflows.errors import (
    ArchivedToolMemberError,
    DuplicateMemberError,
    DuplicateWorkflowError,
    EmptyWorkflowError,
    InvalidShareRecipientError,
    NotCustomWorkflowError,
    NotOwnerError,
    TooFewMembersError,
    ToolNotVisibleToRecipientError,
    TooManyMembersError,
    UnknownToolMemberError,
    UpstreamOrderingError,
    WorkflowArchivedError,
    WorkflowError,
    WorkflowNotFoundError,
    WorkflowStateError,
)
from matofx.workflows.graph_service import WorkflowGraphService
from matofx.workflows.graph_types import WorkflowGraph
from matofx.workflows.ordering import infer_edges_with_fields
from matofx.workflows.orm import Workflow, WorkflowKind, WorkflowRun, WorkflowShare
from matofx.workflows.run_service import WorkflowRunService, compute_state
from matofx.workflows.service import (
    CustomWorkflowDraft,
    WorkflowDraft,
    WorkflowService,
)

router = APIRouter()


# ---------- helpers ------------------------------------------------------


async def _require_manage(session: AsyncSession, actor_user_id: int) -> None:
    if not await user_has_permission(session, actor_user_id, Permission.WORKFLOWS_MANAGE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You don't have permission to manage workflows "
                f"({Permission.WORKFLOWS_MANAGE.value})."
            ),
        )


def _map_domain_error(exc: Exception) -> HTTPException:  # noqa: PLR0911 - a flat dispatch; splitting it would hide the mapping
    if isinstance(exc, WorkflowNotFoundError):
        return HTTPException(status_code=404, detail=exc.detail)
    if isinstance(exc, DuplicateWorkflowError):
        return HTTPException(status_code=409, detail=exc.detail)
    if isinstance(exc, NotOwnerError):
        # Not "you don't exist", but "you can't change this one".
        return HTTPException(status_code=403, detail=exc.detail)
    if isinstance(exc, NotCustomWorkflowError):
        # Wrong shape — caller hit the custom-workflow surface against
        # an admin row. Surface as 404 to avoid leaking the existence
        # of the admin row to a user who couldn't otherwise see it.
        return HTTPException(status_code=404, detail=exc.detail)
    if isinstance(
        exc,
        (
            EmptyWorkflowError,
            UnknownToolMemberError,
            ArchivedToolMemberError,
            DuplicateMemberError,
            UpstreamOrderingError,
            WorkflowStateError,
            WorkflowArchivedError,
            TooFewMembersError,
            TooManyMembersError,
            InvalidShareRecipientError,
            ToolNotVisibleToRecipientError,
        ),
    ):
        return HTTPException(status_code=400, detail=exc.detail)
    if isinstance(exc, WorkflowError):
        return HTTPException(status_code=400, detail=exc.detail)
    return HTTPException(status_code=500, detail=str(exc))


async def _resolve_member_entries(
    session: AsyncSession,
    workflow_service: WorkflowService,
    wf: Workflow,
) -> list[AdminWorkflowMemberEntry]:
    """Render each :class:`WorkflowMember` row as the admin wire shape.

    Looks up each member's tool row to surface name / current version
    / state. Tools that no longer exist (shouldn't happen — the FK is
    RESTRICT) appear with empty name/version so the admin still sees
    *something* rather than a 500.
    """
    members = await workflow_service.list_members(session, wf.id)
    aliases = [m.tool_alias for m in members]
    tools: dict[str, Tool] = {}
    if aliases:
        stmt = select(Tool).where(Tool.alias.in_(aliases))
        for tool in (await session.execute(stmt)).scalars():
            tools[tool.alias] = tool

    entries: list[AdminWorkflowMemberEntry] = []
    for m in members:
        member_tool = tools.get(m.tool_alias)
        if member_tool is None:
            entries.append(
                AdminWorkflowMemberEntry(
                    position=m.position,
                    tool_alias=m.tool_alias,
                    tool_name="",
                    tool_version="",
                    tool_state="missing",
                )
            )
            continue
        state = "archived" if member_tool.archived_at is not None else member_tool.state
        entries.append(
            AdminWorkflowMemberEntry(
                position=m.position,
                tool_alias=m.tool_alias,
                tool_name=member_tool.name,
                tool_version=member_tool.version,
                tool_state=state,
            )
        )
    return entries


async def _to_admin_response(
    session: AsyncSession,
    workflow_service: WorkflowService,
    wf: Workflow,
) -> AdminWorkflowResponse:
    """Render the full admin wire shape including member entries."""
    members = await _resolve_member_entries(session, workflow_service, wf)
    descriptor = dict(wf.descriptor) if isinstance(wf.descriptor, dict) else {}
    description = str(descriptor.get("description") or "")
    teams_raw = descriptor.get("teams") or []
    teams = [str(t) for t in teams_raw if isinstance(t, str)]
    return AdminWorkflowResponse(
        id=wf.id,
        alias=wf.alias,
        name=wf.name,
        version=wf.version,
        state=wf.state,
        kind=str(wf.kind or "admin"),
        description=description,
        teams=teams,
        members=members,
        archived_at=wf.archived_at,
        archive_reason=wf.archive_reason,
        created_at=wf.created_at,
        updated_at=wf.updated_at,
        icon_filename=(
            str(descriptor.get("icon_filename")) if descriptor.get("icon_filename") else None
        ),
    )


def _to_summary(
    wf: Workflow,
    member_aliases: list[str],
    member_chain: list[WorkflowChainEntry],
) -> WorkflowSummaryResponse:
    descriptor = dict(wf.descriptor) if isinstance(wf.descriptor, dict) else {}
    description = str(descriptor.get("description") or "")
    teams_raw = descriptor.get("teams") or []
    teams = [str(t) for t in teams_raw if isinstance(t, str)]
    return WorkflowSummaryResponse(
        alias=wf.alias,
        name=wf.name,
        version=wf.version,
        state=wf.state,
        description=description,
        teams=teams,
        tool_aliases=member_aliases,
        member_chain=member_chain,
        icon_filename=(
            str(descriptor.get("icon_filename")) if descriptor.get("icon_filename") else None
        ),
    )


async def _build_member_chain(
    db: AsyncSession, member_aliases: list[str]
) -> list[WorkflowChainEntry]:
    """Build the flow-ordered tool chain for a workflow summary card.

    ``member_aliases`` arrive in workflow ``position`` order — the same
    order the run page renders tabs in. Each returned entry carries the
    tool's **display name** (labels-not-names) and a ``flows_from_prev``
    flag: ``True`` when the previous member's output feeds this one (a
    descriptor-inferred edge exists between the adjacent pair), ``False``
    otherwise. The home-page card renders an arrow ``→`` when the flag
    is set and a neutral dot ``·`` when it isn't, so unchained /
    standalone tools read as separated rather than sequenced.

    Edges are inferred from the same descriptors the workflow-structure
    graph uses (:func:`infer_edges_with_fields`). A member whose tool
    row can't be resolved (rare — the membership FK is RESTRICT) falls
    back to its alias and is treated as unchained.
    """
    if not member_aliases:
        return []
    tools_by_alias = {
        tool.alias: tool
        for tool in (
            await db.execute(select(Tool).where(Tool.alias.in_(set(member_aliases))))
        ).scalars()
    }
    descriptors_by_alias = {
        alias: (dict(tool.descriptor) if isinstance(tool.descriptor, dict) else {})
        for alias, tool in tools_by_alias.items()
    }
    edges, _labels = infer_edges_with_fields(member_aliases, descriptors_by_alias)

    chain: list[WorkflowChainEntry] = []
    prev_alias: str | None = None
    for alias in member_aliases:
        tool = tools_by_alias.get(alias)
        chain.append(
            WorkflowChainEntry(
                label=tool.name if tool is not None else alias,
                flows_from_prev=prev_alias is not None and (prev_alias, alias) in edges,
                archived=tool is not None and tool.archived_at is not None,
            )
        )
        prev_alias = alias
    return chain


# ---------- admin endpoints ---------------------------------------------


@router.get(
    "/admin/workflows",
    response_model=list[AdminWorkflowResponse],
)
async def admin_list_workflows(
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[AdminWorkflowResponse]:
    """List every workflow (active + archived). Requires ``workflows.manage``."""
    await _require_manage(db, current_user.id)
    rows = await workflow_service.list_all(db, include_archived=True)
    return [await _to_admin_response(db, workflow_service, wf) for wf in rows]


@router.get(
    "/admin/workflows/{alias}",
    response_model=AdminWorkflowResponse,
)
async def admin_get_workflow(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminWorkflowResponse:
    """Fetch one workflow (including members) for the edit page."""
    await _require_manage(db, current_user.id)
    try:
        wf = await workflow_service.get_by_alias(db, alias)
    except WorkflowError as exc:
        raise _map_domain_error(exc) from exc
    return await _to_admin_response(db, workflow_service, wf)


@router.post(
    "/admin/workflows",
    response_model=AdminWorkflowResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_workflow(
    body: WorkflowCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminWorkflowResponse:
    """Install a new workflow. Requires ``workflows.manage``."""
    await _require_manage(db, current_user.id)
    draft = WorkflowDraft(
        alias=body.alias,
        name=body.name,
        description=body.description,
        teams=list(body.teams),
        tool_aliases=list(body.tool_aliases),
    )
    try:
        wf = await workflow_service.install(db, actor_user_id=current_user.id, draft=draft)
    except WorkflowError as exc:
        raise _map_domain_error(exc) from exc
    return await _to_admin_response(db, workflow_service, wf)


@router.post(
    "/admin/workflows/{alias}/update",
    response_model=AdminWorkflowResponse,
)
async def admin_update_workflow(
    alias: str,
    body: WorkflowUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminWorkflowResponse:
    """Edit name / description / teams / member ordering. Bumps version + snapshots."""
    await _require_manage(db, current_user.id)
    draft = WorkflowDraft(
        alias=alias,
        name=body.name,
        description=body.description,
        teams=list(body.teams),
        tool_aliases=list(body.tool_aliases),
    )
    try:
        wf = await workflow_service.update(
            db, actor_user_id=current_user.id, alias=alias, draft=draft
        )
    except WorkflowError as exc:
        raise _map_domain_error(exc) from exc
    return await _to_admin_response(db, workflow_service, wf)


@router.post(
    "/admin/workflows/{alias}/promote",
    response_model=AdminWorkflowResponse,
)
async def admin_promote_workflow(
    alias: str,
    body: WorkflowPromoteRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminWorkflowResponse:
    """Move the workflow through the state machine (new/test/released)."""
    await _require_manage(db, current_user.id)
    try:
        wf = await workflow_service.promote_state(
            db,
            actor_user_id=current_user.id,
            alias=alias,
            new_state=body.new_state,
        )
    except WorkflowError as exc:
        raise _map_domain_error(exc) from exc
    return await _to_admin_response(db, workflow_service, wf)


@router.post(
    "/admin/workflows/{alias}/archive",
    response_model=AdminWorkflowResponse,
)
async def admin_archive_workflow(
    alias: str,
    body: WorkflowArchiveRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminWorkflowResponse:
    """Archive the workflow (soft-shelve; never deleted)."""
    await _require_manage(db, current_user.id)
    try:
        wf = await workflow_service.archive(
            db,
            actor_user_id=current_user.id,
            alias=alias,
            reason=body.reason or None,
        )
    except WorkflowError as exc:
        raise _map_domain_error(exc) from exc
    return await _to_admin_response(db, workflow_service, wf)


@router.post(
    "/admin/workflows/{alias}/unarchive",
    response_model=AdminWorkflowResponse,
)
async def admin_unarchive_workflow(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminWorkflowResponse:
    """Bring an archived workflow back to active status."""
    await _require_manage(db, current_user.id)
    try:
        wf = await workflow_service.unarchive(db, actor_user_id=current_user.id, alias=alias)
    except WorkflowError as exc:
        raise _map_domain_error(exc) from exc
    return await _to_admin_response(db, workflow_service, wf)


@router.post(
    "/admin/workflows/{alias}/icon",
    response_model=AdminWorkflowResponse,
)
async def admin_update_workflow_icon(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    icon: Annotated[
        UploadFile,
        File(description="Replacement PNG icon (100x100 recommended)."),
    ],
) -> AdminWorkflowResponse:
    """Upload (or replace) the workflow's display icon.

    Workflows don't ship a bundle so the icon lives under the shared
    ``workflows.icons_dir`` configured in ``matofx.toml``. The PNG is
    written at ``<icons_dir>/<alias>.png`` and the workflow gets the
    usual patch-bump + change-log snapshot. Requires
    ``workflows.manage``.
    """
    await _require_manage(db, current_user.id)
    max_bytes = settings.workflows.max_upload_bytes
    icon_bytes = await icon.read()
    if not icon_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An icon file is required.",
        )
    if len(icon_bytes) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Icon exceeds the configured cap of {max_bytes} bytes.",
        )
    try:
        wf = await workflow_service.update_icon(
            db,
            actor_user_id=current_user.id,
            alias=alias,
            icons_dir=settings.workflows.icons_dir,
            icon_bytes=icon_bytes,
        )
    except WorkflowError as exc:
        raise _map_domain_error(exc) from exc
    return await _to_admin_response(db, workflow_service, wf)


@router.get(
    "/admin/workflows/{alias}/versions",
    response_model=WorkflowVersionList,
)
async def admin_list_workflow_versions(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> WorkflowVersionList:
    """Snapshot history for the workflow, newest first."""
    await _require_manage(db, current_user.id)
    rows = await workflow_service.list_versions(db, alias)
    user_ids = {row.created_by_user_id for row in rows}
    labels: dict[int, str] = {}
    if user_ids:
        stmt = select(User).where(User.id.in_(user_ids))
        for u in await session_exec(db, stmt):
            display = u.full_name or u.username
            labels[u.id] = f"{display} ({u.username})"

    return WorkflowVersionList(
        versions=[
            WorkflowVersionEntry(
                id=row.id,
                version=row.version,
                created_at=row.created_at,
                created_by_label=labels.get(row.created_by_user_id, "—"),
                change_summary=row.change_summary,
                changes=dict(row.changes) if isinstance(row.changes, dict) else {},
                members_snapshot=list(row.members_snapshot)
                if isinstance(row.members_snapshot, list)
                else [],
            )
            for row in rows
        ]
    )


async def session_exec(db: AsyncSession, stmt: Executable) -> list[Any]:
    """Execute ``stmt`` and return its scalars as a list.

    A shim: it keeps the version handler readable and gives tracing one
    place to hook in later.
    """
    return list((await db.execute(stmt)).scalars())


# ---------- workflow runs ------------------------------------------------


def _build_run_response(
    run: WorkflowRun,
    children: Sequence[Job],
    *,
    wf: Workflow | None,
    scenario: Scenario | None,
    project: Project | None,
    submitter: User | None,
    tool_names: dict[str, str],
) -> WorkflowRunResponse:
    """Assemble the wire shape from already-resolved metadata (no DB I/O).

    The single source of truth for the run response. Both the per-run
    resolver (:func:`_workflow_run_to_response`) and the batched list
    endpoint feed it pre-fetched objects, so the shape can't drift
    between them.
    """
    submitter_label = (
        f"{submitter.full_name or submitter.username} ({submitter.username})"
        if submitter is not None
        else "—"
    )
    return WorkflowRunResponse(
        id=run.id,
        workflow_id=run.workflow_id,
        workflow_alias=wf.alias if wf else "",
        workflow_name=wf.name if wf else "",
        workflow_version_snapshot=run.workflow_version_snapshot,
        scenario_id=run.scenario_id,
        scenario_name=scenario.name if scenario else "",
        project_id=scenario.project_id if scenario else 0,
        project_name=project.name if project else "",
        name=run.name,
        submitted_by_user_id=run.submitted_by_user_id,
        submitted_by_label=submitter_label,
        state=compute_state(run, list(children)),
        children=[
            WorkflowRunChildJob(
                id=c.id,
                workflow_position=int(c.workflow_position or 0),
                tool_alias=c.tool_alias,
                tool_name=tool_names.get(c.tool_alias, c.tool_alias),
                name=c.name,
                state=c.state,
                created_at=c.created_at,
                started_at=c.started_at,
                completed_at=c.completed_at,
            )
            for c in children
        ],
        cancel_requested_at=run.cancel_requested_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


async def _workflow_run_to_response(
    db: AsyncSession,
    run: WorkflowRun,
    children: Sequence[Job],
) -> WorkflowRunResponse:
    """Render one run + its children as the wire shape (per-run lookups).

    Resolves the workflow / scenario / project / submitter / tool
    metadata, then hands them to :func:`_build_run_response`. For
    list endpoints prefer batching the lookups and calling
    :func:`_build_run_response` directly — see
    :func:`list_my_visible_workflow_runs`.
    """
    wf = await db.get(Workflow, run.workflow_id)
    scenario = await db.get(Scenario, run.scenario_id)
    project_id = scenario.project_id if scenario else 0
    project = await db.get(Project, project_id) if project_id else None
    submitter = await db.get(User, run.submitted_by_user_id)

    tool_aliases = [c.tool_alias for c in children]
    tool_names: dict[str, str] = {}
    if tool_aliases:
        stmt = select(Tool.alias, Tool.name).where(Tool.alias.in_(tool_aliases))
        for alias, name in (await db.execute(stmt)).all():
            tool_names[str(alias)] = str(name)

    return _build_run_response(
        run,
        children,
        wf=wf,
        scenario=scenario,
        project=project,
        submitter=submitter,
        tool_names=tool_names,
    )


@router.post(
    "/scenarios/{scenario_id}/workflow-runs",
    response_model=WorkflowRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_workflow_run(
    scenario_id: int,
    body: WorkflowRunStartRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    run_service: Annotated[WorkflowRunService, Depends(get_workflow_run_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> WorkflowRunResponse:
    """Kick off a workflow run on ``scenario_id``.

    Creates the workflow_run row + N child jobs (one per workflow
    member) in a single transaction. Each child starts in ``created``
    state; the user submits them via the existing per-job queue
    endpoint, one tab at a time.
    """
    try:
        run = await run_service.start_run(
            db,
            actor_user_id=current_user.id,
            scenario_id=scenario_id,
            workflow_alias=body.workflow_alias,
            name=body.name,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WorkflowError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    children = await run_service.list_children(db, run_id=run.id, actor_user_id=current_user.id)
    return await _workflow_run_to_response(db, run, children)


@router.get(
    "/workflow-runs/{run_id}",
    response_model=WorkflowRunResponse,
)
async def get_workflow_run(
    run_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    run_service: Annotated[WorkflowRunService, Depends(get_workflow_run_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> WorkflowRunResponse:
    """Fetch one workflow run with its child jobs."""
    try:
        run = await run_service.get_run(db, run_id=run_id, actor_user_id=current_user.id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    children = await run_service.list_children(db, run_id=run_id, actor_user_id=current_user.id)
    return await _workflow_run_to_response(db, run, children)


@router.post(
    "/workflow-runs/{run_id}/cancel",
    response_model=WorkflowRunResponse,
)
async def cancel_workflow_run(
    run_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    run_service: Annotated[WorkflowRunService, Depends(get_workflow_run_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> WorkflowRunResponse:
    """Mark the workflow run cancelled + signal still-pending children."""
    try:
        run = await run_service.cancel_run(db, actor_user_id=current_user.id, run_id=run_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    children = await run_service.list_children(db, run_id=run_id, actor_user_id=current_user.id)
    return await _workflow_run_to_response(db, run, children)


@router.get(
    "/scenarios/{scenario_id}/workflow-runs",
    response_model=list[WorkflowRunResponse],
)
async def list_workflow_runs_for_scenario(
    scenario_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    run_service: Annotated[WorkflowRunService, Depends(get_workflow_run_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[WorkflowRunResponse]:
    """List every workflow run on a scenario, newest first."""
    try:
        runs = await run_service.list_for_scenario(
            db, scenario_id=scenario_id, actor_user_id=current_user.id
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    out: list[WorkflowRunResponse] = []
    for run in runs:
        children = await run_service.list_children(db, run_id=run.id, actor_user_id=current_user.id)
        out.append(await _workflow_run_to_response(db, run, children))
    return out


@router.get(
    "/users/me/visible-workflow-runs",
    response_model=list[WorkflowRunResponse],
)
async def list_my_visible_workflow_runs(
    db: Annotated[AsyncSession, Depends(get_db)],
    run_service: Annotated[WorkflowRunService, Depends(get_workflow_run_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    limit: int = 50,
) -> list[WorkflowRunResponse]:
    """Cross-project listing of workflow runs the caller can see.

    Powers a future My Jobs "Workflow Runs" panel. Already wired so
    the data path is in place for the polish pass; the UI side lands
    iteratively.
    """
    runs = await run_service.list_visible(db, actor_user_id=current_user.id, limit=limit)
    if not runs:
        return []

    # Batch every per-run lookup instead of N+1 round-trips: the runs are
    # already visibility-filtered by ``list_visible``, so their children +
    # metadata can be pulled in a handful of ``IN (...)`` queries and
    # assembled in memory. (The previous per-run ``list_children`` +
    # workflow/scenario/project/submitter/tool gets made this ~6 queries
    # *per run* — slow once a user has many runs.)
    run_ids = [r.id for r in runs]
    children_by_run: dict[int, list[Job]] = defaultdict(list)
    child_stmt = select(Job).where(Job.workflow_run_id.in_(run_ids)).order_by(Job.workflow_position)
    for job in (await db.execute(child_stmt)).scalars():
        if job.workflow_run_id is not None:
            children_by_run[int(job.workflow_run_id)].append(job)

    workflows_by_id = {
        w.id: w
        for w in (
            await db.execute(select(Workflow).where(Workflow.id.in_({r.workflow_id for r in runs})))
        ).scalars()
    }
    scenarios_by_id = {
        s.id: s
        for s in (
            await db.execute(select(Scenario).where(Scenario.id.in_({r.scenario_id for r in runs})))
        ).scalars()
    }
    project_ids = {s.project_id for s in scenarios_by_id.values()}
    projects_by_id = (
        {
            p.id: p
            for p in (
                await db.execute(select(Project).where(Project.id.in_(project_ids)))
            ).scalars()
        }
        if project_ids
        else {}
    )
    users_by_id = {
        u.id: u
        for u in (
            await db.execute(
                select(User).where(User.id.in_({r.submitted_by_user_id for r in runs}))
            )
        ).scalars()
    }
    tool_aliases = {c.tool_alias for kids in children_by_run.values() for c in kids}
    tool_names: dict[str, str] = {}
    if tool_aliases:
        for alias, name in (
            await db.execute(select(Tool.alias, Tool.name).where(Tool.alias.in_(tool_aliases)))
        ).all():
            tool_names[str(alias)] = str(name)

    out: list[WorkflowRunResponse] = []
    for run in runs:
        scenario = scenarios_by_id.get(run.scenario_id)
        project = projects_by_id.get(scenario.project_id) if scenario else None
        out.append(
            _build_run_response(
                run,
                children_by_run.get(run.id, []),
                wf=workflows_by_id.get(run.workflow_id),
                scenario=scenario,
                project=project,
                submitter=users_by_id.get(run.submitted_by_user_id),
                tool_names=tool_names,
            )
        )
    return out


# ---------- user-facing endpoints ---------------------------------------


@router.get(
    "/workflows",
    response_model=list[WorkflowSummaryResponse],
)
async def list_visible_workflows(
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[WorkflowSummaryResponse]:
    """Visible-to-the-caller workflows for the scenario page chooser."""
    rows = await workflow_service.list_visible(db, actor_user_id=current_user.id)
    summaries: list[WorkflowSummaryResponse] = []
    for wf in rows:
        members = await workflow_service.list_members(db, wf.id)
        member_aliases = [m.tool_alias for m in members]
        member_chain = await _build_member_chain(db, member_aliases)
        summaries.append(_to_summary(wf, member_aliases, member_chain))
    return summaries


@router.get("/workflows/{alias}/icon", response_class=FileResponse)
async def get_workflow_icon(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    """Serve a workflow's PNG icon from the configured icons directory.

    Returns 404 when the descriptor has no ``icon_filename`` set or
    when the file is missing from disk — the home page falls back to
    the default schema glyph on either signal. ACL piggy-backs on the
    visibility list, so a user who can't see the workflow can't see
    its icon either.
    """
    visible = await workflow_service.list_visible(db, actor_user_id=current_user.id)
    wf = next((w for w in visible if w.alias == alias), None)
    if wf is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow not visible to this user.",
        )
    descriptor = wf.descriptor if isinstance(wf.descriptor, dict) else {}
    icon_filename = descriptor.get("icon_filename")
    if not icon_filename:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This workflow has no icon uploaded.",
        )
    icons_root = Path(settings.workflows.icons_dir).resolve()
    icon_path = (icons_root / str(icon_filename)).resolve()
    try:
        icon_path.relative_to(icons_root)
    except ValueError:
        # A traversal value in icon_filename would escape the icons dir;
        # refuse with a flat 404 rather than serving an arbitrary file.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The icon file is missing from the workflow icons directory.",
        ) from None
    if not icon_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The icon file is missing from the workflow icons directory.",
        )
    return FileResponse(path=icon_path, media_type="image/png", filename=icon_path.name)


# ---------- workflow structure graph (ADR-0013) -------------------------


def _graph_to_response(graph: WorkflowGraph) -> WorkflowGraphResponse:
    """Map a :class:`WorkflowGraph` to its Pydantic response shape.

    Direct field-by-field mapping. The run-state enum collapses to its
    string value; ``None`` passes through. Keeps the type-checker happy
    without ``cast()`` noise. ``workflow_id=0`` from the preview path
    translates to ``None`` on the wire so the UI can distinguish a
    real saved workflow from a preview without sniffing other fields.
    """
    return WorkflowGraphResponse(
        workflow_id=graph.workflow_id if graph.workflow_id else None,
        workflow_alias=graph.workflow_alias,
        workflow_name=graph.workflow_name,
        workflow_version=graph.workflow_version,
        workflow_state=graph.workflow_state,
        nodes=[
            WorkflowGraphNodeResponse(
                tool_alias=n.tool_alias,
                label=n.label,
                position=n.position,
                tool_state=n.tool_state,
                archived=n.archived,
                deep_link=n.deep_link,
                run_state=n.run_state.value if n.run_state is not None else None,
                child_job_id=n.child_job_id,
                payload=n.payload,
            )
            for n in graph.nodes
        ],
        edges=[
            WorkflowGraphEdgeResponse(
                source=e.source,
                target=e.target,
                field_labels=list(e.field_labels),
            )
            for e in graph.edges
        ],
        run_id=graph.run_id,
        run_state=graph.run_state,
    )


@router.get(
    "/workflows/{workflow_id}/graph",
    response_model=WorkflowGraphResponse,
)
async def get_workflow_graph(
    workflow_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    graph_service: Annotated[WorkflowGraphService, Depends(get_workflow_graph_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    run_id: int | None = None,
) -> WorkflowGraphResponse:
    """Assemble the workflow-structure graph for ``workflow_id``.

    Pure structural projection by default: nodes are member tools in
    workflow-position order, edges are inferred from
    ``descriptor.downstream_tools`` ∪ each downstream input's
    ``upstream_tool``. Standalone members (no descriptor-declared
    upstream / downstream within the chosen set) render as floating
    nodes on the canvas.

    Optional ``?run_id=`` overlay tags each node with the matching
    child job's runtime state (``ready`` / ``waiting`` / ``queued`` /
    ``running`` / ``complete`` / ``failed``) and pushes the child
    Job's id onto the node so the UI can deep-link from a click.

    Errors:

    - **404** — workflow doesn't exist; or workflow exists but isn't
      visible to the caller (existence-hidden, matching the rest of
      the workflow surface).
    - **404** — ``run_id`` supplied but the run doesn't exist /
      belongs to a different workflow / sits on a project the caller
      can't VIEW.
    """
    try:
        graph = await graph_service.build(
            db,
            workflow_id=workflow_id,
            actor_user_id=current_user.id,
            run_id=run_id,
        )
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.detail) from exc
    except NotFoundError as exc:
        # Bubbles from a bad ``run_id`` — existence-hide as 404.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _graph_to_response(graph)


@router.post(
    "/workflows/graph/preview",
    response_model=WorkflowGraphResponse,
)
async def preview_workflow_graph(
    payload: WorkflowGraphRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    graph_service: Annotated[WorkflowGraphService, Depends(get_workflow_graph_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> WorkflowGraphResponse:
    """Project a structure graph for an *unsaved* tool selection.

    Used by the workflow editor's "Preview structure" button — the
    user is still picking tools and wants to see the descriptor-
    inferred wiring before committing. The endpoint is POST (the
    alias list belongs in a body, not a query string) and does not
    persist anything; it's a pure projection over the existing tool
    descriptors.

    The response shape matches the standard graph endpoint with
    ``workflow_id=None`` and placeholder header values — the UI
    suppresses the workflow-header block in preview mode.

    Errors:

    - **400** — unknown alias, hidden alias (existence-hidden as
      "unknown"), or duplicate alias in the list.
    """
    try:
        graph = await graph_service.build_preview(
            db,
            tool_aliases=list(payload.tool_aliases),
            actor_user_id=current_user.id,
        )
    except WorkflowError as exc:
        raise _map_domain_error(exc) from exc
    return _graph_to_response(graph)


# ---------- custom workflows (user-owned) -------------------------------


async def _users_by_id(session: AsyncSession, user_ids: set[int]) -> dict[int, User]:
    """Bulk-load :class:`User` rows for the supplied id set."""
    if not user_ids:
        return {}
    stmt = select(User).where(User.id.in_(user_ids))
    return {u.id: u for u in (await session.execute(stmt)).scalars()}


def _format_user_label(user: User | None) -> tuple[str, str]:
    """Return ``(username, full_name)`` for a user, with sane fallbacks."""
    if user is None:
        return ("", "")
    full = user.full_name.strip() if user.full_name else ""
    return (user.username, full or user.username)


async def _to_custom_summary(
    session: AsyncSession,
    workflow_service: WorkflowService,
    wf: Workflow,
    *,
    caller_user_id: int,
    users_cache: dict[int, User] | None = None,
) -> CustomWorkflowSummary:
    """Render the compact list-row shape for a custom workflow."""
    members = await workflow_service.list_members(session, wf.id)
    # Resolve member aliases → tool *names* (labels) in run order so the
    # list row can show the chain without the caller fetching the full
    # row. Fall back to the alias when the tool's been deleted outright.
    tool_names: dict[str, str] = {}
    if members:
        name_stmt = select(Tool.alias, Tool.name).where(
            Tool.alias.in_([m.tool_alias for m in members])
        )
        for alias, name in (await session.execute(name_stmt)).all():
            tool_names[alias] = name
    member_tools = [
        tool_names.get(m.tool_alias, m.tool_alias)
        for m in sorted(members, key=lambda m: m.position)
    ]
    share_count_stmt = select(WorkflowShare.user_id).where(WorkflowShare.workflow_id == wf.id)
    share_count = len((await session.execute(share_count_stmt)).scalars().all())
    owner = (users_cache or {}).get(wf.owner_id or 0) if wf.owner_id is not None else None
    if owner is None and wf.owner_id is not None:
        owner = await session.get(User, wf.owner_id)
    username, full_name = _format_user_label(owner)
    return CustomWorkflowSummary(
        id=wf.id,
        alias=wf.alias,
        name=wf.name,
        visibility=wf.visibility or "",
        owner_id=int(wf.owner_id or 0),
        owner_username=username,
        owner_full_name=full_name,
        is_owner=wf.owner_id == caller_user_id,
        member_count=len(members),
        member_tools=member_tools,
        share_count=share_count,
        archived_at=wf.archived_at,
        updated_at=wf.updated_at,
    )


async def _to_custom_response(
    session: AsyncSession,
    workflow_service: WorkflowService,
    wf: Workflow,
    *,
    caller_user_id: int,
) -> CustomWorkflowResponse:
    """Render the full custom-workflow shape (members + shares + lineage)."""
    members = await workflow_service.list_members(session, wf.id)
    tool_rows: dict[str, Tool] = {}
    if members:
        stmt = select(Tool).where(Tool.alias.in_([m.tool_alias for m in members]))
        for tool in (await session.execute(stmt)).scalars():
            tool_rows[tool.alias] = tool

    member_entries: list[CustomWorkflowMemberEntry] = []
    for m in sorted(members, key=lambda m: m.position):
        member_tool = tool_rows.get(m.tool_alias)
        if member_tool is None:
            member_entries.append(
                CustomWorkflowMemberEntry(
                    position=m.position,
                    tool_alias=m.tool_alias,
                    tool_name="",
                    tool_state="missing",
                )
            )
            continue
        state = "archived" if member_tool.archived_at is not None else member_tool.state
        member_entries.append(
            CustomWorkflowMemberEntry(
                position=m.position,
                tool_alias=m.tool_alias,
                tool_name=member_tool.name,
                tool_state=state,
            )
        )

    share_stmt = (
        select(WorkflowShare)
        .where(WorkflowShare.workflow_id == wf.id)
        .order_by(WorkflowShare.shared_at)
    )
    share_rows = list((await session.execute(share_stmt)).scalars())
    share_user_ids = {s.user_id for s in share_rows}
    if wf.owner_id is not None:
        share_user_ids.add(int(wf.owner_id))
    users = await _users_by_id(session, share_user_ids)
    share_entries: list[CustomWorkflowShareEntry] = []
    for s in share_rows:
        recipient = users.get(s.user_id)
        username, full_name = _format_user_label(recipient)
        share_entries.append(
            CustomWorkflowShareEntry(
                user_id=s.user_id,
                username=username,
                full_name=full_name,
                shared_at=s.shared_at,
            )
        )

    owner = users.get(int(wf.owner_id or 0))
    owner_username, owner_full_name = _format_user_label(owner)
    descriptor = dict(wf.descriptor) if isinstance(wf.descriptor, dict) else {}
    return CustomWorkflowResponse(
        id=wf.id,
        alias=wf.alias,
        name=wf.name,
        description=str(descriptor.get("description") or ""),
        version=wf.version,
        visibility=wf.visibility or "",
        owner_id=int(wf.owner_id or 0),
        owner_username=owner_username,
        owner_full_name=owner_full_name,
        is_owner=wf.owner_id == caller_user_id,
        members=member_entries,
        shares=share_entries,
        copied_from_workflow_id=(
            int(descriptor["copied_from_workflow_id"])
            if isinstance(descriptor.get("copied_from_workflow_id"), int)
            else None
        ),
        archived_at=wf.archived_at,
        archive_reason=wf.archive_reason,
        archived_by_user_id=wf.archived_by_user_id,
        created_at=wf.created_at,
        updated_at=wf.updated_at,
    )


async def _require_custom_visible(
    session: AsyncSession,
    *,
    caller_user_id: int,
    wf: Workflow,
) -> None:
    """Refuse a GET that the caller shouldn't be able to see.

    Owner sees their own; share recipients see what was shared with
    them; inspector-permission holders see anything. Everyone else
    gets 404 (we don't leak existence).
    """
    if wf.kind != WorkflowKind.CUSTOM.value:
        raise HTTPException(status_code=404, detail="Not found.")
    if wf.owner_id == caller_user_id:
        return
    share_stmt = select(WorkflowShare.user_id).where(
        WorkflowShare.workflow_id == wf.id,
        WorkflowShare.user_id == caller_user_id,
    )
    if (await session.execute(share_stmt)).scalar_one_or_none() is not None:
        return
    if await user_has_permission(session, caller_user_id, Permission.CUSTOM_WORKFLOWS_INSPECT):
        return
    raise HTTPException(status_code=404, detail="Not found.")


@router.get(
    "/workflows/custom/_meta/share-candidates",
    response_model=list[CustomWorkflowShareCandidate],
)
async def list_share_candidates(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[CustomWorkflowShareCandidate]:
    """Active users other than the caller, ordered by display name.

    Powers the share-picker dropdown on ``/my-workflows``. We don't
    gate by team membership here — that's the service layer's job at
    save time (a recipient who can't see a member tool gets a
    friendly :class:`ToolNotVisibleToRecipientError`). Hiding
    candidates at this layer would leave the owner unable to
    distinguish "this user doesn't exist" from "this user can't see
    one of your tools".
    """
    stmt = (
        select(User)
        .where(User.is_active.is_(True), User.id != current_user.id)
        .order_by(User.full_name, User.username)
    )
    rows = (await db.execute(stmt)).scalars()
    return [
        CustomWorkflowShareCandidate(
            user_id=u.id,
            username=u.username,
            full_name=(u.full_name or "").strip() or u.username,
        )
        for u in rows
    ]


@router.get(
    "/workflows/custom",
    response_model=list[CustomWorkflowSummary],
)
async def list_my_custom_workflows(
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    include_archived: bool = False,
) -> list[CustomWorkflowSummary]:
    """Owner-owned + shared-with-me custom workflows for ``/my-workflows``."""
    rows = await workflow_service.list_for_user(
        db,
        actor_user_id=current_user.id,
        include_archived=include_archived,
    )
    owner_ids = {wf.owner_id for wf in rows if wf.owner_id is not None}
    users = await _users_by_id(db, owner_ids)
    return [
        await _to_custom_summary(
            db,
            workflow_service,
            wf,
            caller_user_id=current_user.id,
            users_cache=users,
        )
        for wf in rows
    ]


@router.post(
    "/workflows/custom",
    response_model=CustomWorkflowResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_custom_workflow(
    body: CustomWorkflowCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CustomWorkflowResponse:
    """Create a new custom workflow owned by the caller."""
    draft = CustomWorkflowDraft(
        name=body.name,
        description=body.description,
        tool_aliases=list(body.tool_aliases),
        share_with_user_ids=list(body.share_with_user_ids),
    )
    try:
        wf = await workflow_service.create_custom(db, actor_user_id=current_user.id, draft=draft)
    except WorkflowError as exc:
        raise _map_domain_error(exc) from exc
    return await _to_custom_response(db, workflow_service, wf, caller_user_id=current_user.id)


@router.get(
    "/workflows/custom/{workflow_id}",
    response_model=CustomWorkflowResponse,
)
async def get_custom_workflow(
    workflow_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CustomWorkflowResponse:
    """Fetch a single custom workflow (owner / share recipient / inspector)."""
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Not found.")
    await _require_custom_visible(db, caller_user_id=current_user.id, wf=wf)
    return await _to_custom_response(db, workflow_service, wf, caller_user_id=current_user.id)


@router.post(
    "/workflows/custom/{workflow_id}/update",
    response_model=CustomWorkflowResponse,
)
async def update_custom_workflow(
    workflow_id: int,
    body: CustomWorkflowUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CustomWorkflowResponse:
    """Owner-only edit: name, description, member set, share list."""
    draft = CustomWorkflowDraft(
        name=body.name,
        description=body.description,
        tool_aliases=list(body.tool_aliases),
        share_with_user_ids=list(body.share_with_user_ids),
    )
    try:
        wf = await workflow_service.update_custom(
            db,
            actor_user_id=current_user.id,
            workflow_id=workflow_id,
            draft=draft,
        )
    except WorkflowError as exc:
        raise _map_domain_error(exc) from exc
    return await _to_custom_response(db, workflow_service, wf, caller_user_id=current_user.id)


@router.post(
    "/workflows/custom/{workflow_id}/archive",
    response_model=CustomWorkflowResponse,
)
async def archive_custom_workflow(
    workflow_id: int,
    body: CustomWorkflowArchiveRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CustomWorkflowResponse:
    """Archive a custom workflow (owner or inspector)."""
    try:
        wf = await workflow_service.archive_custom(
            db,
            actor_user_id=current_user.id,
            workflow_id=workflow_id,
            reason=body.reason or None,
        )
    except WorkflowError as exc:
        raise _map_domain_error(exc) from exc
    return await _to_custom_response(db, workflow_service, wf, caller_user_id=current_user.id)


@router.post(
    "/workflows/custom/{workflow_id}/unarchive",
    response_model=CustomWorkflowResponse,
)
async def unarchive_custom_workflow(
    workflow_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CustomWorkflowResponse:
    """Owner-only: bring an archived custom workflow back to active."""
    try:
        wf = await workflow_service.unarchive_custom(
            db, actor_user_id=current_user.id, workflow_id=workflow_id
        )
    except WorkflowError as exc:
        raise _map_domain_error(exc) from exc
    return await _to_custom_response(db, workflow_service, wf, caller_user_id=current_user.id)


@router.get(
    "/admin/custom-workflows",
    response_model=list[CustomWorkflowSummary],
)
async def admin_list_custom_workflows(
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    owner_id: int | None = None,
    contains_tool_alias: str | None = None,
    include_archived: bool = True,
    search: str | None = None,
) -> list[CustomWorkflowSummary]:
    """Inspector listing for ``/admin/custom-workflows``.

    Gated on :data:`Permission.CUSTOM_WORKFLOWS_INSPECT`. Returns
    every custom workflow in the system, optionally filtered by
    owner / contains-tool and a free-text ``search`` (name, owner, or
    tool) that is additive to the structured filters. Archived rows
    are included by default since the inspector's primary job is
    post-hoc troubleshooting.
    """
    try:
        rows = await workflow_service.list_for_admin(
            db,
            actor_user_id=current_user.id,
            include_archived=include_archived,
            owner_id=owner_id,
            contains_tool_alias=contains_tool_alias,
            search=search,
        )
    except WorkflowError as exc:
        raise _map_domain_error(exc) from exc
    owner_ids = {wf.owner_id for wf in rows if wf.owner_id is not None}
    users = await _users_by_id(db, owner_ids)
    return [
        await _to_custom_summary(
            db,
            workflow_service,
            wf,
            caller_user_id=current_user.id,
            users_cache=users,
        )
        for wf in rows
    ]


@router.post(
    "/workflows/custom/{workflow_id}/copy",
    response_model=CustomWorkflowResponse,
    status_code=status.HTTP_201_CREATED,
)
async def copy_custom_workflow(
    workflow_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CustomWorkflowResponse:
    """Fork any visible workflow into the caller's own list."""
    try:
        wf = await workflow_service.copy_to_owner(
            db,
            actor_user_id=current_user.id,
            source_workflow_id=workflow_id,
        )
    except WorkflowError as exc:
        raise _map_domain_error(exc) from exc
    return await _to_custom_response(db, workflow_service, wf, caller_user_id=current_user.id)
