"""Auto-generated results-summary endpoint.

``GET /api/summaries/{kind}/{id}`` returns the templated narrative (and, once
enrichment lands, the guard-passed LLM prose) for one terminal result. Access
is gated by the *underlying* result's own visibility check, so a summary is
never more visible than the thing it describes.

Slice 1 wires the ``job`` surface, computing the summary on demand. Slice 5
generalises the dispatch to all four surfaces and routes through
``SummaryService`` + the ``result_summary`` cache; the response shape here is
the stable contract the UI depends on.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import (
    get_audit,
    get_current_user,
    get_db,
    get_job_service,
    get_optimisation_service,
    get_results_service,
    get_settings,
    get_study_service,
    get_summary_service,
    get_workflow_run_service,
)
from matofx.audit import AuditWriter
from matofx.config import Settings
from matofx.hierarchy.errors import NotFoundError
from matofx.hierarchy.service import JobService
from matofx.studies import ResultsService, StudyService
from matofx.studies.optimisation import OptimisationService
from matofx.summaries import (
    NonTerminalResultError,
    SummaryFacts,
    SummaryService,
    collect_job_facts,
    collect_optimisation_facts,
    collect_parametric_facts,
    collect_workflow_facts,
    enrichment_context_if_enabled,
    is_terminal,
)
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission
from matofx.workflows.run_service import WorkflowRunService

router = APIRouter()


@router.get("/summaries/{kind}/{surface_id}")
async def get_summary(
    kind: str,
    surface_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    run_service: Annotated[WorkflowRunService, Depends(get_workflow_run_service)],
    study_service: Annotated[StudyService, Depends(get_study_service)],
    results_service: Annotated[ResultsService, Depends(get_results_service)],
    optimisation_service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    summary_service: Annotated[SummaryService, Depends(get_summary_service)],
    audit: Annotated[AuditWriter, Depends(get_audit)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Return the summary for one terminal result.

    Per-surface helpers collect the facts behind that surface's own VIEW gate;
    :class:`SummaryService` renders + caches them. 404 collapses "no such
    result", "not visible to you", and "not in a terminal state" into one
    indistinguishable response, like the report endpoint, so the surface never
    leaks a result's existence or progress to a caller who couldn't see it.
    """
    if kind == "job":
        facts = await _job_facts(
            db,
            job_service=job_service,
            current_user=current_user,
            settings=settings,
            job_id=surface_id,
        )
    elif kind == "workflow_run":
        facts = await _workflow_facts(
            db,
            run_service=run_service,
            current_user=current_user,
            settings=settings,
            run_id=surface_id,
        )
    elif kind == "parametric_study":
        facts = await _parametric_facts(
            db,
            study_service=study_service,
            results_service=results_service,
            current_user=current_user,
            study_id=surface_id,
        )
    elif kind == "optimisation_study":
        facts = await _optimisation_facts(
            db,
            optimisation_service=optimisation_service,
            current_user=current_user,
            study_id=surface_id,
        )
    else:
        # An unknown kind 404s like any unknown resource, keeping the
        # not-found contract.
        raise NotFoundError("summary", surface_id)

    enrichment = await enrichment_context_if_enabled(
        db, settings=settings, audit=audit, actor=f"user:{current_user.id}"
    )
    cached = await summary_service.build(db, facts, enrichment=enrichment)
    # Per-user opt-out: only surface the enriched prose to a viewer who hasn't
    # opted out. Enrichment still ran + cached for everyone else.
    prefer = bool(current_user.summary_enrichment_enabled)
    enriched = cached.enriched.to_dict() if (prefer and cached.enriched is not None) else None
    return {
        "surface_kind": kind,
        "surface_id": surface_id,
        "status": cached.status,
        "templated": cached.templated.to_dict(),
        "enriched": enriched,
        "generated_at": cached.generated_at.isoformat(),
    }


async def _job_facts(
    db: AsyncSession,
    *,
    job_service: JobService,
    current_user: User,
    settings: Settings,
    job_id: int,
) -> SummaryFacts:
    is_admin = await user_has_permission(db, current_user.id, Permission.PROJECTS_SEE_ALL)
    job = await job_service.ensure_outputs_accessible(
        db,
        job_id=job_id,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        actor_is_approver=current_user.is_approver,
        actor_is_admin=is_admin,
    )
    if not is_terminal(job.state):
        raise NotFoundError("summary", job_id)
    return await collect_job_facts(db, job=job, settings=settings)


async def _workflow_facts(
    db: AsyncSession,
    *,
    run_service: WorkflowRunService,
    current_user: User,
    settings: Settings,
    run_id: int,
) -> SummaryFacts:
    run = await run_service.get_run(db, run_id=run_id, actor_user_id=current_user.id)
    children = await run_service.list_children(db, run_id=run_id, actor_user_id=current_user.id)
    try:
        return await collect_workflow_facts(db, run=run, children=children, settings=settings)
    except NonTerminalResultError as exc:
        # A still-running / draft run has no summary yet — collapse to 404
        # so the surface matches the report endpoint's not-terminal stance.
        raise NotFoundError("summary", run_id) from exc


async def _parametric_facts(
    db: AsyncSession,
    *,
    study_service: StudyService,
    results_service: ResultsService,
    current_user: User,
    study_id: int,
) -> SummaryFacts:
    study = await study_service.get(db, actor_user_id=current_user.id, study_id=study_id)
    if not is_terminal(study.state):
        raise NotFoundError("summary", study_id)
    results = await results_service.get_results_for_study(
        db, actor_user_id=current_user.id, study_id=study_id
    )
    return await collect_parametric_facts(db, study=study, results=results)


async def _optimisation_facts(
    db: AsyncSession,
    *,
    optimisation_service: OptimisationService,
    current_user: User,
    study_id: int,
) -> SummaryFacts:
    study = await optimisation_service.get(db, actor_user_id=current_user.id, study_id=study_id)
    if not is_terminal(study.state):
        raise NotFoundError("summary", study_id)
    run_results = await optimisation_service.run_results(
        db, actor_user_id=current_user.id, study_id=study_id
    )
    return await collect_optimisation_facts(db, study=study, run_results=run_results)
