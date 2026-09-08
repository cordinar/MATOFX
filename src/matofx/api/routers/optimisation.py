"""HTTP API for optimisation studies (ADR-0009 §15).

Thin endpoints delegating to :class:`~matofx.studies.optimisation.OptimisationService`;
domain errors (`OptimisationError` family + the hierarchy ``NotFoundError`` /
``AccessDeniedError``) propagate to the central handlers in
:mod:`matofx.api.errors`, so there is no per-endpoint try/except.

Gating:

- **Feature flag** — a router-wide dependency 404s every route when
  ``features.optimisation_framework`` is off, so the surface is *absent*
  for a customer without the flag (not merely forbidden).
- **Permission** — no new top-level verb; configure / edit / halt /
  publish need Member+ on the study's project, read needs Viewer+, all
  enforced by the service.

The runtime (the outer loop) is hosted in the runner daemon, not here —
these endpoints only configure studies and read their live state; the
loop drives them once they reach ``running``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api._approval_notify import notify_pending_approval
from matofx.api.deps import (
    get_current_user,
    get_db,
    get_model_service,
    get_notifier,
    get_optimisation_service,
    get_settings,
)
from matofx.api.schemas import (
    OptimisationApproverOption,
    OptimisationConfigPatchRequest,
    OptimisationConfigureRequest,
    OptimisationContinueRequest,
    OptimisationConvergenceResponse,
    OptimisationCostEstimateResponse,
    OptimisationMopComputeRequest,
    OptimisationMopFrozenFactorsRequest,
    OptimisationMopResponse,
    OptimisationPublishSurrogateRequest,
    OptimisationPublishSurrogateResponse,
    OptimisationRecommendationResponse,
    OptimisationRunResultRow,
    OptimisationRunResultsResponse,
    OptimisationStartDecisionRequest,
    OptimisationStudyCreateRequest,
    OptimisationStudyResponse,
    OptimisationWorkflowStudyCreateRequest,
)
from matofx.approvals.orm import ApprovalRequest
from matofx.config import Settings
from matofx.features import Feature, feature_enabled
from matofx.hierarchy.orm import Project
from matofx.models.service import ModelService
from matofx.notify import Notifier
from matofx.studies.optimisation import OptimisationService
from matofx.studies.optimisation.orm import OptimisationStudy
from matofx.tools.orm import Tool, ToolVersion
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission
from matofx.workflows.orm import Workflow, WorkflowVersion


def _require_optimisation_feature(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """404 every route when the optimisation-framework flag is off."""
    if not feature_enabled(settings.features, Feature.OPTIMISATION_FRAMEWORK):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")


router = APIRouter(dependencies=[Depends(_require_optimisation_feature)])


def _to_study(
    s: OptimisationStudy,
    *,
    tool_name: str = "",
    workflow_name: str = "",
    project_name: str = "",
    created_by_name: str = "",
    tool_version: str = "",
    workflow_version: str = "",
) -> OptimisationStudyResponse:
    return OptimisationStudyResponse(
        id=s.id,
        project_id=s.project_id,
        project_name=project_name,
        name=s.name,
        description=s.description,
        target_kind=s.target_kind,
        tool_alias=s.tool_alias,
        tool_name=tool_name,
        tool_version_id=s.tool_version_id,
        tool_version=tool_version,
        workflow_alias=s.workflow_alias,
        workflow_name=workflow_name,
        workflow_version_id=s.workflow_version_id,
        workflow_version=workflow_version,
        scenario_id=s.scenario_id,
        config_json=dict(s.config_json or {}),
        state=s.state,
        stop_reason=s.stop_reason,
        last_batch_number=s.last_batch_number,
        current_best_json=s.current_best_json,
        mop_state=s.mop_state_json,
        halt_requested=s.halt_requested,
        created_by_user_id=s.created_by_user_id,
        created_by_name=created_by_name,
        created_at=s.created_at,
        archived_at=s.archived_at,
        version=s.version,
    )


async def _enrich_studies(
    db: AsyncSession, studies: list[OptimisationStudy]
) -> list[OptimisationStudyResponse]:
    """Attach display names: target label (tool/workflow), project, creator, version.

    One batched lookup each so the catalogue rows + detail caption read by
    label, never the raw alias / version id ([[feedback_use_labels_not_names]]).
    """
    if not studies:
        return []

    async def _map(col_key: Any, col_val: Any, ids: set[Any]) -> dict[Any, str]:
        if not ids:
            return {}
        rows = (await db.execute(select(col_key, col_val).where(col_key.in_(ids)))).all()
        return {k: str(v) for k, v in rows}

    tool_names = await _map(Tool.alias, Tool.name, {s.tool_alias for s in studies if s.tool_alias})
    wf_names = await _map(
        Workflow.alias, Workflow.name, {s.workflow_alias for s in studies if s.workflow_alias}
    )
    project_names = await _map(Project.id, Project.name, {s.project_id for s in studies})
    tool_versions = await _map(
        ToolVersion.id,
        ToolVersion.version,
        {s.tool_version_id for s in studies if s.tool_version_id},
    )
    wf_versions = await _map(
        WorkflowVersion.id,
        WorkflowVersion.version,
        {s.workflow_version_id for s in studies if s.workflow_version_id},
    )
    user_rows = (
        await db.execute(
            select(User.id, User.full_name, User.username).where(
                User.id.in_({s.created_by_user_id for s in studies})
            )
        )
    ).all()
    creator_names = {int(uid): str(full or username) for uid, full, username in user_rows}
    return [
        _to_study(
            s,
            tool_name=tool_names.get(s.tool_alias, "") if s.tool_alias else "",
            workflow_name=wf_names.get(s.workflow_alias, "") if s.workflow_alias else "",
            project_name=project_names.get(s.project_id, ""),
            created_by_name=creator_names.get(s.created_by_user_id, ""),
            tool_version=tool_versions.get(s.tool_version_id, "") if s.tool_version_id else "",
            workflow_version=(
                wf_versions.get(s.workflow_version_id, "") if s.workflow_version_id else ""
            ),
        )
        for s in studies
    ]


async def _enrich_study(db: AsyncSession, study: OptimisationStudy) -> OptimisationStudyResponse:
    """Single-study convenience over :func:`_enrich_studies`."""
    return (await _enrich_studies(db, [study]))[0]


@router.get(
    "/projects/{project_id}/optimisation-studies",
    response_model=list[OptimisationStudyResponse],
)
async def list_studies(
    project_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    include_archived: bool = False,
) -> list[OptimisationStudyResponse]:
    """List a project's optimisation studies (Viewer+); archived hidden by default."""
    studies = await service.list_for_project(
        db,
        actor_user_id=current_user.id,
        project_id=project_id,
        include_archived=include_archived,
    )
    return await _enrich_studies(db, list(studies))


async def _require_create(db: AsyncSession, actor_user_id: int) -> None:
    """403 unless the actor holds ``optimisation.create``."""
    if not await user_has_permission(db, actor_user_id, Permission.OPTIMISATION_CREATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You don't have permission to create optimisation studies "
                f"({Permission.OPTIMISATION_CREATE.value})."
            ),
        )


@router.post(
    "/projects/{project_id}/optimisation-studies",
    response_model=OptimisationStudyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_study(
    project_id: int,
    body: OptimisationStudyCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationStudyResponse:
    """Mint a ``draft`` optimisation study over one pinned tool version (Member+)."""
    await _require_create(db, current_user.id)
    study = await service.create_draft(
        db,
        actor_user_id=current_user.id,
        project_id=project_id,
        name=body.name,
        tool_alias=body.tool_alias,
        tool_version_id=body.tool_version_id,
        description=body.description,
    )
    return _to_study(study)


@router.post(
    "/projects/{project_id}/optimisation-studies/workflow",
    response_model=OptimisationStudyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workflow_study(
    project_id: int,
    body: OptimisationWorkflowStudyCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationStudyResponse:
    """Mint a ``draft`` study over one workflow chain (Member+, §3.4)."""
    await _require_create(db, current_user.id)
    study = await service.create_workflow_draft(
        db,
        actor_user_id=current_user.id,
        project_id=project_id,
        name=body.name,
        workflow_alias=body.workflow_alias,
        workflow_version_id=body.workflow_version_id,
        description=body.description,
    )
    return _to_study(study)


@router.get("/optimisation-approvers", response_model=list[OptimisationApproverOption])
async def list_optimisation_approvers(
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[OptimisationApproverOption]:
    """Eligible start-gate approvers for the create wizard's picker (§14.1)."""
    pairs = await service.list_eligible_approvers(db, actor_user_id=current_user.id)
    return [OptimisationApproverOption(user_id=uid, username=name) for uid, name in pairs]


@router.get("/optimisation-studies/{study_id}", response_model=OptimisationStudyResponse)
async def get_study(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationStudyResponse:
    """Fetch a study (Viewer+ on its home project)."""
    study = await service.get(db, actor_user_id=current_user.id, study_id=study_id)
    return await _enrich_study(db, study)


@router.patch("/optimisation-studies/{study_id}", response_model=OptimisationStudyResponse)
async def update_study(
    study_id: int,
    body: OptimisationConfigPatchRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationStudyResponse:
    """Merge a config step into a draft study (Member+, draft-only)."""
    study = await service.update_draft(
        db, actor_user_id=current_user.id, study_id=study_id, config_patch=body.config_patch
    )
    return _to_study(study)


@router.post("/optimisation-studies/{study_id}/configure", response_model=OptimisationStudyResponse)
async def configure_study(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    notifier: Annotated[Notifier, Depends(get_notifier)],
    body: OptimisationConfigureRequest | None = None,
) -> OptimisationStudyResponse:
    """Validate + freeze the config and launch the study (Member+).

    If the target tool requires start approval (§14.1), pass
    ``start_approver_user_id`` — the study parks in ``pending_start_approval``
    pending that approver's sign-off instead of launching.
    """
    study = await service.configure(
        db,
        actor_user_id=current_user.id,
        study_id=study_id,
        start_approver_user_id=body.start_approver_user_id if body else None,
    )
    # No-op unless the study parked at a pending start-approval gate.
    await notify_pending_approval(
        db, notifier, target_column=ApprovalRequest.optimisation_study_id, target_value=study_id
    )
    return _to_study(study)


@router.get(
    "/optimisation-studies/{study_id}/cost-estimate",
    response_model=OptimisationCostEstimateResponse,
)
async def study_cost_estimate(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationCostEstimateResponse:
    """Campaign footprint for the start gate + create wizard (Viewer+, §14.1)."""
    est = await service.estimate_cost(db, actor_user_id=current_user.id, study_id=study_id)
    return OptimisationCostEstimateResponse(**est)


@router.post(
    "/optimisation-studies/{study_id}/start-approve", response_model=OptimisationStudyResponse
)
async def start_approve(
    study_id: int,
    body: OptimisationStartDecisionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationStudyResponse:
    """The nominated approver grants the start gate → ``running`` (§14.1)."""
    is_admin = await user_has_permission(db, current_user.id, Permission.PROJECTS_SEE_ALL)
    study = await service.approve_start(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        study_id=study_id,
        decider_is_admin=is_admin,
        reason=body.reason,
    )
    return _to_study(study)


@router.post(
    "/optimisation-studies/{study_id}/start-reject", response_model=OptimisationStudyResponse
)
async def start_reject(
    study_id: int,
    body: OptimisationStartDecisionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationStudyResponse:
    """The nominated approver declines the start gate → ``cancelled`` (§14.1)."""
    is_admin = await user_has_permission(db, current_user.id, Permission.PROJECTS_SEE_ALL)
    study = await service.reject_start(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        study_id=study_id,
        decider_is_admin=is_admin,
        reason=body.reason,
    )
    return _to_study(study)


@router.post("/optimisation-studies/{study_id}/halt", response_model=OptimisationStudyResponse)
async def halt_study(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationStudyResponse:
    """Request a graceful halt (Member+); the loop stops at the next batch."""
    study = await service.request_halt(db, actor_user_id=current_user.id, study_id=study_id)
    return _to_study(study)


@router.post("/optimisation-studies/{study_id}/continue", response_model=OptimisationStudyResponse)
async def continue_study(
    study_id: int,
    body: OptimisationContinueRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationStudyResponse:
    """Continue a completed study by raising its run budget (Member+; §14)."""
    study = await service.extend_budget(
        db,
        actor_user_id=current_user.id,
        study_id=study_id,
        additional_runs=body.additional_runs,
    )
    return _to_study(study)


@router.post("/optimisation-studies/{study_id}/archive", response_model=OptimisationStudyResponse)
async def archive_study(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationStudyResponse:
    """Archive a study — hide from default lists, never delete (D20)."""
    study = await service.archive(db, actor_user_id=current_user.id, study_id=study_id)
    return _to_study(study)


@router.get(
    "/optimisation-studies/{study_id}/convergence",
    response_model=OptimisationConvergenceResponse,
)
async def study_convergence(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationConvergenceResponse:
    """The study's live convergence trace + best-so-far (Viewer+, Convergence tab)."""
    study = await service.get(db, actor_user_id=current_user.id, study_id=study_id)
    return OptimisationConvergenceResponse(
        study_id=study.id,
        state=study.state,
        stop_reason=study.stop_reason,
        last_batch_number=study.last_batch_number,
        current_best_json=study.current_best_json,
        trace=list(study.convergence_trace or []),
    )


@router.get(
    "/optimisation-studies/{study_id}/run-results",
    response_model=OptimisationRunResultsResponse,
)
async def study_run_results(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationRunResultsResponse:
    """Per-run results table for the study (Viewer+, Results tab)."""
    rows = await service.run_results(db, actor_user_id=current_user.id, study_id=study_id)
    return OptimisationRunResultsResponse(
        study_id=study_id,
        runs=[OptimisationRunResultRow(**r) for r in rows],
    )


@router.get(
    "/optimisation-studies/{study_id}/recommendation",
    response_model=OptimisationRecommendationResponse,
)
async def study_recommendation(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationRecommendationResponse:
    """The Navigator's suggested strategy for the draft config (Viewer+, §5.6)."""
    rec = await service.recommend(db, actor_user_id=current_user.id, study_id=study_id)
    return OptimisationRecommendationResponse(
        strategy=rec.strategy,
        run_mode=rec.run_mode,
        batch_size=rec.batch_size,
        rationale=rec.rationale,
        acquisition=rec.acquisition,
    )


@router.post("/optimisation-studies/{study_id}/mop", response_model=OptimisationMopResponse)
async def compute_mop(
    study_id: int,
    body: OptimisationMopComputeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationMopResponse:
    """Run a MOP/CoP analysis over the study's runs (Member+, bo + tool target; §5.7)."""
    state = await service.compute_mop(
        db, actor_user_id=current_user.id, study_id=study_id, cutoff=body.cutoff
    )
    return OptimisationMopResponse(**state)


@router.post(
    "/optimisation-studies/{study_id}/mop/frozen-factors",
    response_model=OptimisationMopResponse,
)
async def set_mop_frozen_factors(
    study_id: int,
    body: OptimisationMopFrozenFactorsRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationMopResponse:
    """Override which low-CoP factors are frozen out of the search (Member+; §5.7)."""
    state = await service.set_mop_frozen_factors(
        db,
        actor_user_id=current_user.id,
        study_id=study_id,
        frozen_factors=body.frozen_factors,
    )
    return OptimisationMopResponse(**state)


@router.post(
    "/optimisation-studies/{study_id}/publish-surrogate",
    response_model=OptimisationPublishSurrogateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def publish_surrogate(
    study_id: int,
    body: OptimisationPublishSurrogateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[OptimisationService, Depends(get_optimisation_service)],
    model_service: Annotated[ModelService, Depends(get_model_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OptimisationPublishSurrogateResponse:
    """Publish the study's working surrogate into the Model registry (Member+)."""
    version = await service.publish_current_surrogate(
        db,
        actor_user_id=current_user.id,
        study_id=study_id,
        model_service=model_service,
        name=body.name,
        scope=body.scope,
        surrogate_kind=body.surrogate_kind,
    )
    return OptimisationPublishSurrogateResponse(
        model_id=version.surrogate_model_id,
        version_id=version.id,
        ordinal=version.ordinal,
        scope=body.scope,
    )
