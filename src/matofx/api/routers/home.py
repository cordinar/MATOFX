"""Home dashboard router — the header greeting's live summary aggregate.

GET /api/home/summary → {active_jobs, running_optimisations,
running_parametric_studies, pending_approvals, can_approve} for the
authenticated caller.

A single cheap roll-up (home-redesign §8.6) so the Home page doesn't
walk every project → scenario → job at load just to render the one-line
summary under the greeting. Every count is filtered through the same
project-ACL visibility ladder the rest of the API enforces, so the
summary can never widen what the caller can see.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import (
    get_approval_service,
    get_current_user,
    get_db,
    get_job_service,
    get_optimisation_service,
    get_project_service,
    get_settings,
    get_study_service,
    get_tool_service,
    get_workflow_service,
)
from matofx.api.routers.jobs import hydrate_visible_job_responses
from matofx.api.schemas import HomeSummary, SearchHit
from matofx.approvals import ApprovalService
from matofx.config import Settings
from matofx.features import Feature, feature_enabled
from matofx.hierarchy.orm import Job, Project, ProjectAcl, Scenario
from matofx.hierarchy.service import JobService, ProjectService
from matofx.studies.optimisation.orm import OptimisationStudy
from matofx.studies.optimisation.service import OptimisationService
from matofx.studies.orm import ParametricStudy
from matofx.studies.service import StudyService
from matofx.tools.service import ToolService
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission
from matofx.workflows.service import WorkflowService

router = APIRouter()

_SEARCH_PER_TYPE_CAP = 6
"""Most hits returned per entity type, so the dropdown stays scannable."""

# Jobs that count as "active" for the summary headline: everything that
# isn't a terminal state. Mirrors the closed JOB_STATES vocabulary —
# terminal = complete / failed / denied / results_withheld.
_ACTIVE_JOB_STATES: frozenset[str] = frozenset(
    {"created", "queued", "running", "pending_approval", "pending_results"}
)


async def _restrict_to_visible_projects(
    session: AsyncSession,
    stmt: Select,  # type: ignore[type-arg]
    *,
    project_id_column: object,
    actor_user_id: int,
) -> Select:  # type: ignore[type-arg]
    """Add the project-ACL visibility filter to a count query.

    Mirrors :meth:`ProjectService.list_for_user`: a user with
    ``projects.see_all`` sees the whole install; everyone else sees only
    projects they created or hold an ACL grant on. ``project_id_column``
    is the column on the query's driving table that points at
    ``project.id`` (e.g. ``Scenario.project_id`` for jobs, or the
    study's own ``project_id``).
    """
    if await user_has_permission(session, actor_user_id, Permission.PROJECTS_SEE_ALL):
        return stmt
    shared = select(ProjectAcl.project_id).where(ProjectAcl.user_id == actor_user_id)
    return stmt.where(
        project_id_column.in_(  # type: ignore[attr-defined]
            select(Project.id).where(
                (Project.creator_user_id == actor_user_id) | Project.id.in_(shared)
            )
        )
    )


@router.get("/home/summary", response_model=HomeSummary)
async def home_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    approval_service: Annotated[ApprovalService, Depends(get_approval_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> HomeSummary:
    """Aggregate the header summary counts for the caller.

    Only scenario-homed jobs are counted as "active jobs": parametric-
    and optimisation-study child jobs are represented by the dedicated
    study counts, so counting them here would double up.
    """
    # Active jobs — scenario-homed, visible, non-terminal.
    jobs_stmt = (
        select(func.count())
        .select_from(Job)
        .join(Scenario, Scenario.id == Job.scenario_id)
        .where(Job.state.in_(_ACTIVE_JOB_STATES))
    )
    jobs_stmt = await _restrict_to_visible_projects(
        db, jobs_stmt, project_id_column=Scenario.project_id, actor_user_id=current_user.id
    )
    active_jobs = int((await db.execute(jobs_stmt)).scalar_one())

    # Running optimisation studies.
    opt_stmt = (
        select(func.count())
        .select_from(OptimisationStudy)
        .where(OptimisationStudy.state == "running")
    )
    opt_stmt = await _restrict_to_visible_projects(
        db,
        opt_stmt,
        project_id_column=OptimisationStudy.project_id,
        actor_user_id=current_user.id,
    )
    running_optimisations = int((await db.execute(opt_stmt)).scalar_one())

    # Running parametric studies.
    study_stmt = (
        select(func.count())
        .select_from(ParametricStudy)
        .where(ParametricStudy.state == "running")
    )
    study_stmt = await _restrict_to_visible_projects(
        db,
        study_stmt,
        project_id_column=ParametricStudy.project_id,
        actor_user_id=current_user.id,
    )
    running_parametric_studies = int((await db.execute(study_stmt)).scalar_one())

    # Pending approvals addressed to the caller (approvers only).
    can_approve = bool(current_user.is_approver)
    pending_approvals = 0
    if can_approve:
        pending_approvals = await approval_service.count_pending_for_user(
            db, username=current_user.username or "", include_all=False
        )

    return HomeSummary(
        active_jobs=active_jobs,
        running_optimisations=running_optimisations,
        running_parametric_studies=running_parametric_studies,
        pending_approvals=pending_approvals,
        can_approve=can_approve,
    )


def _matches(query: str, *fields: str | None) -> bool:
    """Case-insensitive substring match of ``query`` across ``fields``."""
    return any(query in (f or "").lower() for f in fields)


@router.get("/search", response_model=list[SearchHit])
async def search(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    study_service: Annotated[StudyService, Depends(get_study_service)],
    optimisation_service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    workflow_service: Annotated[WorkflowService, Depends(get_workflow_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    q: str = "",
    scope: str = "all",
) -> list[SearchHit]:
    """Global quick-find across the caller's visible entities (home §6).

    Searches projects, jobs, parametric studies, optimisations, tools and
    workflows — every candidate resolved through the same ACL / visibility
    ladder the rest of the API enforces, then substring-matched on its
    friendly label / context (never an alias). Studies and optimisations
    are additionally gated by their feature flag. Results are grouped by
    type and capped per type so the header dropdown stays scannable.

    Material / file scopes aren't covered yet; the header shows "no
    matches" for those scopes until they land.
    """
    text = q.strip().lower()
    if not text:
        return []

    def want(entity_type: str) -> bool:
        return scope in ("all", entity_type)

    hits: list[SearchHit] = []
    # Resolve the caller's visible projects once — jobs / studies /
    # optimisations all hang off them.
    projects = await project_service.list_for_user(db, actor_user_id=current_user.id)

    if want("project"):
        matched = [
            p for p in projects if _matches(text, p.name, p.description)
        ][:_SEARCH_PER_TYPE_CAP]
        hits.extend(
            SearchHit(
                type="project",
                type_label="Projects",
                label=p.name,
                sublabel=(p.description or "")[:80],
                route=f"/projects/{p.id}",
            )
            for p in matched
        )

    if want("job"):
        hits.extend(await _search_jobs(db, job_service, current_user, text))

    if want("study") and feature_enabled(settings.features, Feature.PARAMETRIC_STUDIES):
        hits.extend(
            await _search_studies(db, study_service, current_user, projects, text)
        )

    if want("optimisation") and feature_enabled(
        settings.features, Feature.OPTIMISATION_FRAMEWORK
    ):
        hits.extend(
            await _search_optimisations(db, optimisation_service, current_user, projects, text)
        )

    if want("tool"):
        tools = await tool_service.list_visible(db, actor_user_id=current_user.id)
        matched_tools = [t for t in tools if _matches(text, t.name, t.alias)][
            :_SEARCH_PER_TYPE_CAP
        ]
        # Where a tool hit points depends on what the user can do with it:
        # someone who can manage tools jumps to the New/edit Tool editor;
        # everyone else lands on the tool-filtered Job Search (tools have no
        # standalone end-user page). Deep-link with the alias either way so
        # the destination opens on this tool, not an empty picker.
        can_edit_tools = await user_has_permission(
            db, current_user.id, Permission.TOOLS_INSTALL
        )
        hits.extend(
            SearchHit(
                type="tool",
                type_label="Tools",
                label=t.name,
                sublabel=f"Tool · v{t.version}",
                route=(
                    f"/admin/tools/{t.alias}/edit"
                    if can_edit_tools
                    else f"/job-search?tool={t.alias}"
                ),
            )
            for t in matched_tools
        )

    if want("workflow"):
        workflows = await workflow_service.list_visible(db, actor_user_id=current_user.id)
        matched_wf = [w for w in workflows if _matches(text, w.name, w.alias)][
            :_SEARCH_PER_TYPE_CAP
        ]
        hits.extend(
            SearchHit(
                type="workflow",
                type_label="Workflows",
                label=w.name,
                sublabel=f"Workflow · v{w.version}",
                route="/my-workflows",
            )
            for w in matched_wf
        )

    return hits


async def _search_studies(
    db: AsyncSession,
    study_service: StudyService,
    current_user: User,
    projects: list[Project],
    text: str,
) -> list[SearchHit]:
    """Parametric-study hits across the caller's visible projects."""
    out: list[SearchHit] = []
    for p in projects:
        if len(out) >= _SEARCH_PER_TYPE_CAP:
            break
        studies = await study_service.list_for_project(
            db, project_id=p.id, actor_user_id=current_user.id
        )
        for s in studies:
            if len(out) >= _SEARCH_PER_TYPE_CAP:
                break
            if _matches(text, s.name):
                out.append(
                    SearchHit(
                        type="study",
                        type_label="Studies",
                        label=s.name,
                        sublabel=f"Parametric · {p.name}",
                        status=str(s.state or "").replace("_", " ").title(),
                        route=f"/studies/{s.id}",
                    )
                )
    return out


async def _search_optimisations(
    db: AsyncSession,
    optimisation_service: OptimisationService,
    current_user: User,
    projects: list[Project],
    text: str,
) -> list[SearchHit]:
    """Optimisation-study hits across the caller's visible projects."""
    out: list[SearchHit] = []
    for p in projects:
        if len(out) >= _SEARCH_PER_TYPE_CAP:
            break
        opts = await optimisation_service.list_for_project(
            db, project_id=p.id, actor_user_id=current_user.id
        )
        for s in opts:
            if len(out) >= _SEARCH_PER_TYPE_CAP:
                break
            if _matches(text, s.name):
                out.append(
                    SearchHit(
                        type="optimisation",
                        type_label="Optimisations",
                        label=s.name,
                        sublabel=f"Optimisation · {p.name}",
                        status=str(s.state or "").replace("_", " ").title(),
                        route=f"/optimisation/{s.id}",
                    )
                )
    return out


async def _search_jobs(
    db: AsyncSession, job_service: JobService, current_user: User, text: str
) -> list[SearchHit]:
    """ACL-filtered job hits with friendly project / scenario / tool labels.

    ``JobService.list_visible_to_user`` already substring-matches the
    query against ``Job.name`` and applies the project-ACL ladder;
    :func:`hydrate_visible_job_responses` then resolves the friendly
    project / scenario / tool labels in one pass. Workflow-child jobs
    deep-link into the My Jobs workspace; standalone jobs to the job
    page, mirroring the home table's row-click behaviour.
    """
    jobs = await job_service.list_visible_to_user(
        db, actor_user_id=current_user.id, query=text, limit=_SEARCH_PER_TYPE_CAP
    )
    responses = await hydrate_visible_job_responses(db, jobs)
    out: list[SearchHit] = []
    for r in responses:
        if r.workflow_run_id:
            route = f"/my-jobs?workflow_run={r.workflow_run_id}&job={r.id}"
        else:
            route = f"/jobs/{r.id}"
        out.append(
            SearchHit(
                type="job",
                type_label="Jobs",
                label=r.tool_name or r.name,
                sublabel=f"#{r.id:07d} · {r.project_name} / {r.scenario_name}",
                status=str(r.state or "").replace("_", " ").title(),
                route=route,
            )
        )
    return out
