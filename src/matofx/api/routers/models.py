"""HTTP API for the surrogate Model registry (ADR-0008 §5 / §11.2).

Thin endpoints over :class:`~matofx.models.ModelService`; domain errors
(`ModelError` family + the hierarchy ``NotFoundError`` / ``AccessDeniedError``)
propagate to the central handlers in :mod:`matofx.api.errors`.

Gating (surrogate §14):

- **Feature flag** — a router-wide dependency 404s every route when
  ``features.surrogate_family`` is off.
- **Permission** — read is scope-resolved by the service (personal =
  owner, project = Viewer+, global = everyone); ``global`` create needs
  the Model Curator capability (``models.curate``), resolved here and
  passed to the service. Export-as-tool inherits the tool install /
  promote permission ladder.

Covers the full lifecycle: the ``/models`` catalogue + per-model detail,
create / refit / metadata / scope / retire, the two-gate global *release*
flow (submit-review → directed check via the approvals router → su/admin
release, governed by ``models_global_publish_approval``), and
export-as-tool.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api._approval_notify import notify_pending_approval
from matofx.api.deps import (
    get_cohort_service,
    get_current_user,
    get_db,
    get_factory,
    get_model_service,
    get_notifier,
    get_settings,
    get_tool_service,
)
from matofx.api.schemas import (
    FieldGeometryResponse,
    FieldPredictRequest,
    FieldPredictResponse,
    ModelApproverOption,
    ModelCompareRequest,
    ModelCompareResponse,
    ModelCoverageFactor,
    ModelCoverageResponse,
    ModelCreateRequest,
    ModelDetailResponse,
    ModelExportRequest,
    ModelExportResponse,
    ModelFieldMethodFit,
    ModelFitJobResponse,
    ModelMethodFit,
    ModelRefitRequest,
    ModelResponse,
    ModelScopeRequest,
    ModelSubmitReviewRequest,
    ModelUpdateRequest,
    ModelVersionCoverageResponse,
    ModelVersionResponse,
    PredictSupportResponse,
    SurrogateExclusionSummary,
)
from matofx.approvals.orm import ApprovalRequest
from matofx.audit import ensure_chain_head
from matofx.config import Settings
from matofx.config.general_service import load_general_settings
from matofx.db import SessionFactory, session_scope
from matofx.features import Feature, feature_enabled
from matofx.hierarchy.orm import Project
from matofx.models import ModelService, register_as_tool
from matofx.models.fit_jobs import FIT_JOBS, FitJob
from matofx.models.orm import SurrogateModel, SurrogateModelVersion
from matofx.notify import Notifier
from matofx.studies.cohort.orm import JobCohort
from matofx.studies.cohort.service import CohortService
from matofx.studies.optimisation.orm import OptimisationStudy
from matofx.studies.orm import ParametricStudy
from matofx.studies.surrogate.ingest import FactorFilter
from matofx.tools.orm import Tool
from matofx.tools.service import ToolService
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission


def _require_surrogate_feature(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """404 every route when the surrogate-family flag is off (routes *absent*)."""
    if not feature_enabled(settings.features, Feature.SURROGATE_FAMILY):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")


router = APIRouter(dependencies=[Depends(_require_surrogate_feature)])


def _to_model(
    m: SurrogateModel,
    *,
    tool_name: str = "",
    project_name: str = "",
    created_by_name: str = "",
) -> ModelResponse:
    return ModelResponse(
        id=m.id,
        name=m.name,
        description=m.description,
        tags=list(m.tags or []),
        tool_alias=m.tool_alias,
        tool_name=tool_name,
        scope=m.scope,
        project_id=m.project_id,
        project_name=project_name,
        owner_user_id=m.owner_user_id,
        state=m.state,
        published_version_id=m.published_version_id,
        created_by_user_id=m.created_by_user_id,
        created_by_name=created_by_name,
        created_at=m.created_at,
        version=m.version,
    )


async def _enrich_models(db: AsyncSession, models: list[SurrogateModel]) -> list[ModelResponse]:
    """Attach display names: tool label, project, creator — for the catalogue.

    One batched lookup each so rows read by label, not the raw alias / id
    ([[feedback_use_labels_not_names]]).
    """
    if not models:
        return []
    tool_names = {
        str(a): str(n)
        for a, n in (
            await db.execute(
                select(Tool.alias, Tool.name).where(Tool.alias.in_({m.tool_alias for m in models}))
            )
        ).all()
    }
    project_ids = {m.project_id for m in models if m.project_id is not None}
    project_names = (
        {
            int(i): str(n)
            for i, n in (
                await db.execute(
                    select(Project.id, Project.name).where(Project.id.in_(project_ids))
                )
            ).all()
        }
        if project_ids
        else {}
    )
    creator_names = {
        int(uid): str(full or username)
        for uid, full, username in (
            await db.execute(
                select(User.id, User.full_name, User.username).where(
                    User.id.in_({m.created_by_user_id for m in models})
                )
            )
        ).all()
    }
    return [
        _to_model(
            m,
            tool_name=tool_names.get(m.tool_alias, ""),
            project_name=project_names.get(m.project_id, "") if m.project_id is not None else "",
            created_by_name=creator_names.get(m.created_by_user_id, ""),
        )
        for m in models
    ]


async def _enrich_model(db: AsyncSession, model: SurrogateModel) -> ModelResponse:
    """Single-model convenience over :func:`_enrich_models`."""
    return (await _enrich_models(db, [model]))[0]


def _to_version(v: SurrogateModelVersion) -> ModelVersionResponse:
    return ModelVersionResponse(
        id=v.id,
        ordinal=v.ordinal,
        kind=v.kind,
        factors=list(v.factors or []),
        responses=list(v.responses or []),
        source_kind=v.source_kind,
        source_id=v.source_id,
        n_training_points=v.n_training_points,
        quality=dict(v.quality or {}),
        publish_state=v.publish_state,
        surrogate_method=v.surrogate_method,
        fitted_at=v.fitted_at,
        fitted_by_user_id=v.fitted_by_user_id,
    )


async def _approval_required(db: AsyncSession) -> bool:
    """Resolve the ``models_global_publish_approval`` toggle (default ON, §6.3)."""
    values = await load_general_settings(db)
    return bool(values.get("models_global_publish_approval", True))


async def _resolve_checker(
    db: AsyncSession, checker_user_id: int | None
) -> tuple[int | None, str | None]:
    """Validate the nominated checker is an active Model Curator; return (id, username)."""
    if checker_user_id is None:
        return None, None
    user = await db.get(User, checker_user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The chosen checker isn't an active user.",
        )
    if not await user_has_permission(db, user.id, Permission.MODELS_CURATE):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The chosen checker isn't a Model Curator.",
        )
    return user.id, user.username


async def _version_in_model(
    db: AsyncSession, model_id: int, version_id: int
) -> SurrogateModelVersion:
    """Fetch a version, asserting it belongs to ``model_id`` (the URL pair)."""
    version = await db.get(SurrogateModelVersion, version_id)
    if version is None or version.surrogate_model_id != model_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="That model version doesn't exist."
        )
    return version


async def _versions_for(db: AsyncSession, model_id: int) -> list[SurrogateModelVersion]:
    return list(
        (
            await db.execute(
                select(SurrogateModelVersion)
                .where(SurrogateModelVersion.surrogate_model_id == model_id)
                .order_by(SurrogateModelVersion.ordinal.desc())
            )
        )
        .scalars()
        .all()
    )


async def _ensure_cohort_results(
    db: AsyncSession,
    cohort_service: CohortService,
    actor_user_id: int,
    source_kind: str,
    source_id: int,
) -> None:
    """Materialise a cohort's ``study_result`` rows before a fit reads them.

    Cohort results are populated lazily — only a cohort *results* read writes
    them (ADR-0008 §3.3). The surrogate ingestion reads those rows directly,
    so building / refitting from a cohort whose results page was never opened
    would silently train on nothing. Populate here (idempotent, ACL-respecting
    via ``resolve_members``) so the fit always sees the live membership. A
    no-op for non-cohort sources.
    """
    if source_kind != "cohort":
        return
    await cohort_service.populate_study_results(
        db, actor_user_id=actor_user_id, cohort_id=source_id
    )
    # Commit before fitting. Populating always runs a DELETE, which on SQLite
    # takes the database's write lock; left open, that lock was held for the
    # whole fit (minutes with tuning), so every other write in the app — even
    # the session-activity touch on the page's own progress poll — waited and
    # then failed with "database is locked". The page reported the build as
    # failed while it went on to save the model, and a second click made a
    # duplicate. The rows are an idempotent projection, safe to commit alone.
    await db.commit()


@router.get("/models/picker", response_model=list[ModelResponse])
async def picker(
    tool: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[ModelResponse]:
    """Models for ``tool`` the caller may pick (§6.1); retired/un-published hidden."""
    models = await service.list_for_picker(db, actor_user_id=current_user.id, tool_alias=tool)
    return [_to_model(m) for m in models]


@router.get("/models/approvers", response_model=list[ModelApproverOption])
async def model_approvers(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[ModelApproverOption]:
    """Active Model Curators the caller can nominate as checker (excludes self)."""
    rows = (
        (await db.execute(select(User).where(User.is_active.is_(True)).order_by(User.username)))
        .scalars()
        .all()
    )
    out: list[ModelApproverOption] = []
    for u in rows:
        if u.id == current_user.id:
            continue
        if await user_has_permission(db, u.id, Permission.MODELS_CURATE):
            out.append(
                ModelApproverOption(user_id=u.id, username=u.username, full_name=u.full_name)
            )
    return out


@router.get("/models/versions/{version_id}/coverage", response_model=ModelCoverageResponse)
async def version_coverage(
    version_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelCoverageResponse:
    """The frozen training-range factors of a version (the §7.3 coverage gauge).

    Drives the surrogate-tool submit form's advisory gauge. Scope-read
    access-checked — a caller who can run the exported tool but can't read
    the underlying model simply gets a 404 and the form omits the gauge.
    """
    factors = await service.coverage_for_version(
        db, actor_user_id=current_user.id, version_id=version_id
    )
    return ModelCoverageResponse(
        version_id=version_id,
        factors=[
            ModelCoverageFactor(
                name=str(f["name"]), min=f.get("min"), max=f.get("max"), unit=f.get("unit")
            )
            for f in factors
        ],
    )


@router.post(
    "/models/{model_id}/versions/{version_id}/field-predict",
    response_model=FieldPredictResponse,
)
async def field_predict(
    model_id: int,  # noqa: ARG001 - path context; access keyed on version
    version_id: int,
    body: FieldPredictRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FieldPredictResponse:
    """Reconstruct a field surrogate's nodal field at one point (§4.5 viewer).

    Drives the predicted-field viewer on the model detail page — design
    factors (plus ``time`` for a time-resolved surrogate) in, a nodal field +
    per-node 1-sigma out. Scope-read access-checked; the sha-verified bundle
    is loaded off the event loop. 404s for a non-field version via the
    service's plain-English refusal mapped by the central error handlers.
    """
    result = await service.field_predict(
        db,
        actor_user_id=current_user.id,
        version_id=version_id,
        point=dict(body.point),
        response_name=body.response_name,
    )
    return FieldPredictResponse(
        version_id=result.version_id,
        n_nodes=result.n_nodes,
        field=result.field,
        field_std=result.field_std,
        mode_coefficients=result.mode_coefficients,
        has_geometry=result.has_geometry,
        support=(PredictSupportResponse(**result.support) if result.support is not None else None),
    )


@router.get(
    "/models/{model_id}/versions/{version_id}/field-geometry",
    response_model=FieldGeometryResponse,
)
async def field_geometry(
    model_id: int,  # noqa: ARG001 - path context; access keyed on version
    version_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    response_name: str | None = None,
) -> FieldGeometryResponse:
    """The reference-mesh surface for a field version's 3-D viewer (§4.5).

    Fetched once when the viewer opens; ``has_geometry=False`` means the
    field has no mesh (``.npy``) and the viewer draws the value-vs-node line.
    ``response_name`` picks which field of a ``combined`` model (ADR-0018).
    Scope-read access-checked; the surface extraction runs off the event loop.
    """
    result = await service.field_geometry(
        db, actor_user_id=current_user.id, version_id=version_id, response_name=response_name
    )
    return FieldGeometryResponse(
        version_id=result.version_id,
        has_geometry=result.has_geometry,
        points=result.points,
        i=result.i,
        j=result.j,
        k=result.k,
    )


@router.post("/models/compare-fits", response_model=ModelCompareResponse)
async def compare_fits(
    body: ModelCompareRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    cohort_service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelCompareResponse:
    """Fit several methods over the same data, persist none (§4 bake-off).

    Returns each method's cross-validated quality + held-out pairs (for
    overlaid parity plots), the best picks and the ADR-0022 per-result tuning,
    so the user compares before committing. They then ``POST /api/models`` with
    the chosen method. Large comparisons belong on the background variant,
    ``POST /api/models/compare-fits/jobs``.
    """
    return await _run_compare(db, service, cohort_service, current_user.id, body)


@router.post("/models/compare-fits/jobs", response_model=ModelFitJobResponse)
async def start_compare_fits_job(
    body: ModelCompareRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    factory: Annotated[SessionFactory, Depends(get_factory)],
    service: Annotated[ModelService, Depends(get_model_service)],
    cohort_service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelFitJobResponse:
    """Start :func:`compare_fits` in the background (ADR-0022 §7)."""
    user_id = current_user.id

    async def _work(job: FitJob) -> dict[str, Any]:
        async with session_scope(factory) as session:
            await ensure_chain_head(session)
            result = await _run_compare(session, service, cohort_service, user_id, body, job.report)
        return result.model_dump(mode="json")

    title = await _source_title(db, body.source_kind, body.source_id)
    return _job_response(
        FIT_JOBS.start("compare", user_id, _work, title=title, context=body.model_dump(mode="json"))
    )


@router.get("/models/fit-jobs", response_model=list[ModelFitJobResponse])
async def list_fit_jobs(
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[ModelFitJobResponse]:
    """The caller's background fits still held (running, or finished in the last hour).

    Results are left out, as a comparison can be large; fetch one job for its result.
    """
    return [
        _job_response(job).model_copy(update={"result": None})
        for job in FIT_JOBS.for_user(current_user.id)
    ]


@router.get("/models/fit-jobs/{job_id}", response_model=ModelFitJobResponse)
async def get_fit_job(
    job_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelFitJobResponse:
    """A background fit's progress, and its result once finished (own jobs only)."""
    job = FIT_JOBS.get(job_id, current_user.id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That fit isn't running any more — it may have been lost in a restart.",
        )
    return _job_response(job)


def _job_response(job: FitJob) -> ModelFitJobResponse:
    model_id = job.context.get("model_id")
    if job.kind == "create" and job.status == "done" and isinstance(job.result, dict):
        model_id = job.result.get("id")
    return ModelFitJobResponse(
        id=job.id,
        kind=job.kind,
        status=job.status,
        done=job.done,
        total=job.total,
        message=job.message,
        error=job.error,
        result=job.result if job.status == "done" else None,
        title=job.title,
        context=job.context,
        model_id=int(model_id) if model_id is not None else None,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


async def _source_title(db: AsyncSession, source_kind: str, source_id: int) -> str:
    """The source's name for the fits list; a plain fallback if it can't be read."""
    orm: Any = {
        "cohort": JobCohort,
        "parametric": ParametricStudy,
        "optimisation": OptimisationStudy,
    }.get(source_kind)
    row = await db.get(orm, source_id) if orm is not None else None
    name = getattr(row, "name", None)
    return str(name) if name else f"Source #{source_id}"


async def _run_compare(
    db: AsyncSession,
    service: ModelService,
    cohort_service: CohortService,
    user_id: int,
    body: ModelCompareRequest,
    progress: Any = None,
) -> ModelCompareResponse:
    await _ensure_cohort_results(db, cohort_service, user_id, body.source_kind, body.source_id)
    factor_filter = FactorFilter(output_ranges=body.output_filter) if body.output_filter else None
    per_method: list[dict[str, Any]] = []
    best_overall: str | None = None
    best_per_response: dict[str, str] = {}
    excluded: dict[str, Any] = {"n_excluded": 0, "reason_counts": {}}
    tuning: dict[str, Any] = {}
    if body.response_names and body.methods:
        per_method, best_overall, best_per_response, excluded, tuning = await service.compare_fits(
            db,
            actor_user_id=user_id,
            source_kind=body.source_kind,
            source_id=body.source_id,
            response_names=body.response_names,
            methods=body.methods,
            factor_filter=factor_filter,
            tool_alias=body.tool_alias,
            tuning_mode=body.tuning_mode,
            progress=progress,
        )
    # Field bake-off (ADR-0018): resolve the field specs from the descriptor and
    # rank the chosen field methods (incl 'auto') on Field Q².
    field_per_method: list[dict[str, Any]] = []
    best_field_overall: str | None = None
    if body.field_response_names and body.field_methods:
        if progress is not None:
            progress(0, 0, "Fitting the 3-D field outputs")
        field_specs = []
        for name in body.field_response_names:
            fs = await service.resolve_field_spec(
                db, tool_alias=body.tool_alias, response_name=name
            )
            if fs is not None:
                field_specs.append(fs)
        if field_specs:
            field_per_method, best_field_overall, field_excluded = await service.compare_field_fits(
                db,
                source_kind=body.source_kind,
                source_id=body.source_id,
                field_specs=field_specs,
                methods=body.field_methods,
                factor_filter=factor_filter,
            )
            if int(field_excluded["n_excluded"]) > int(excluded["n_excluded"]):
                excluded = field_excluded
    return ModelCompareResponse(
        responses=list(body.response_names),
        methods=[ModelMethodFit(**m) for m in per_method],
        best_overall=best_overall,
        best_per_response=best_per_response,
        field_responses=list(body.field_response_names),
        field_methods=[ModelFieldMethodFit(**m) for m in field_per_method],
        best_field_overall=best_field_overall,
        excluded=SurrogateExclusionSummary(**excluded),
        tuning=tuning,
    )


@router.get("/models", response_model=list[ModelResponse])
async def list_models(
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    tool: str | None = None,
    scope: str | None = None,
    include_retired: bool = False,
) -> list[ModelResponse]:
    """The catalogue: every model the caller may read (in-progress included).

    Unlike ``/models/picker`` this surfaces the caller's own drafts /
    pending entries so the workspace can badge their status. Optional
    ``tool`` / ``scope`` filters; retired hidden unless ``include_retired``.
    """
    models = await service.list_models(
        db,
        actor_user_id=current_user.id,
        tool_alias=tool,
        scope=scope,
        include_retired=include_retired,
    )
    return await _enrich_models(db, list(models))


@router.post("/models", response_model=ModelResponse, status_code=status.HTTP_201_CREATED)
async def create_model(
    body: ModelCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    cohort_service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelResponse:
    """Fit + persist a new model (personal/project publish directly; global gated).

    A single response naming a tool's ``field`` output is routed to the POD
    field path automatically (the descriptor is authoritative, so the client
    needs no extra flag). Large builds belong on the background variant,
    ``POST /api/models/jobs``.
    """
    return await _run_create(db, service, cohort_service, current_user.id, body)


@router.post("/models/jobs", response_model=ModelFitJobResponse)
async def start_create_model_job(
    body: ModelCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    factory: Annotated[SessionFactory, Depends(get_factory)],
    service: Annotated[ModelService, Depends(get_model_service)],
    cohort_service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelFitJobResponse:
    """Start :func:`create_model` in the background (ADR-0022 §7).

    Permission is checked before the job starts, so a user without it gets the
    403 at once instead of from a job minutes later.
    """
    await _require_create_permission(db, current_user.id)
    user_id = current_user.id

    async def _work(job: FitJob) -> dict[str, Any]:
        async with session_scope(factory) as session:
            await ensure_chain_head(session)
            result = await _run_create(session, service, cohort_service, user_id, body, job.report)
        return result.model_dump(mode="json")

    return _job_response(
        FIT_JOBS.start(
            "create", user_id, _work, title=body.name, context=body.model_dump(mode="json")
        )
    )


async def _require_create_permission(db: AsyncSession, user_id: int) -> bool:
    """Raise 403 unless the user may create models; returns whether they curate."""
    is_curator = await user_has_permission(db, user_id, Permission.MODELS_CURATE)
    # Create is allowed to base ``models.create`` holders or to curators
    # (who can always create). Curate stays the gate for global publishes.
    if not is_curator and not await user_has_permission(db, user_id, Permission.MODELS_CREATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You don't have permission to create surrogate models "
                f"({Permission.MODELS_CREATE.value})."
            ),
        )
    return is_curator


async def _run_create(
    db: AsyncSession,
    service: ModelService,
    cohort_service: CohortService,
    user_id: int,
    body: ModelCreateRequest,
    progress: Any = None,
) -> ModelResponse:
    await _ensure_cohort_results(db, cohort_service, user_id, body.source_kind, body.source_id)
    is_curator = await _require_create_permission(db, user_id)
    # Split the requested responses into scalar names + field specs — the
    # descriptor is authoritative (ADR-0018), so a request mixing numbers and
    # field outputs creates one combined model.
    scalar_responses: list[str] = []
    field_specs = []
    for name in body.response_names:
        fs = await service.resolve_field_spec(db, tool_alias=body.tool_alias, response_name=name)
        if fs is not None:
            field_specs.append(fs)
        else:
            scalar_responses.append(name)
    factor_filter = FactorFilter(output_ranges=body.output_filter) if body.output_filter else None
    model, _version, excluded = await service.create_model(
        db,
        actor_user_id=user_id,
        name=body.name,
        tool_alias=body.tool_alias,
        scope=body.scope,
        source_kind=body.source_kind,
        source_id=body.source_id,
        response_names=scalar_responses,
        project_id=body.project_id,
        surrogate_kind=body.surrogate_kind,
        description=body.description,
        tags=body.tags,
        keep_training_data=body.keep_training_data,
        is_curator=is_curator,
        field_specs=field_specs,
        field_auto_select=body.field_auto_select,
        field_surrogate_kind=body.field_surrogate_kind,
        factor_filter=factor_filter,
        tuning_mode=body.tuning_mode,
        candidate_methods=body.candidate_methods,
        tuning_overrides={k: v.model_dump() for k, v in body.tuning_overrides.items()},
        progress=progress,
    )
    return _to_model(model).model_copy(update={"excluded": SurrogateExclusionSummary(**excluded)})


@router.get("/models/{model_id}", response_model=ModelDetailResponse)
async def get_model(
    model_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelDetailResponse:
    """A model + its version history (scope-resolved read)."""
    model = await service.get(db, actor_user_id=current_user.id, model_id=model_id)
    versions = await _versions_for(db, model_id)
    return ModelDetailResponse(
        model=await _enrich_model(db, model), versions=[_to_version(v) for v in versions]
    )


@router.patch("/models/{model_id}", response_model=ModelResponse)
async def update_model(
    model_id: int,
    body: ModelUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelResponse:
    """Edit a model's mutable entry metadata."""
    model = await service.update_metadata(
        db,
        actor_user_id=current_user.id,
        model_id=model_id,
        name=body.name,
        description=body.description,
        tags=body.tags,
    )
    return _to_model(model)


@router.post("/models/{model_id}/refit", response_model=ModelVersionResponse)
async def refit_model(
    model_id: int,
    body: ModelRefitRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    cohort_service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelVersionResponse:
    """Re-fit on the source's current data → a new version (§8)."""
    return await _run_refit(db, service, cohort_service, current_user.id, model_id, body)


@router.post("/models/{model_id}/refit/jobs", response_model=ModelFitJobResponse)
async def start_refit_model_job(
    model_id: int,
    body: ModelRefitRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    factory: Annotated[SessionFactory, Depends(get_factory)],
    service: Annotated[ModelService, Depends(get_model_service)],
    cohort_service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelFitJobResponse:
    """Start :func:`refit_model` in the background (ADR-0022 §7).

    Write access is checked before the job starts, so a refusal is immediate.
    """
    model = await service.get(db, actor_user_id=current_user.id, model_id=model_id)
    user_id = current_user.id

    async def _work(job: FitJob) -> dict[str, Any]:
        async with session_scope(factory) as session:
            await ensure_chain_head(session)
            result = await _run_refit(
                session, service, cohort_service, user_id, model_id, body, job.report
            )
        return result.model_dump(mode="json")

    return _job_response(
        FIT_JOBS.start(
            "refit",
            user_id,
            _work,
            title=str(model.name),
            context={**body.model_dump(mode="json"), "model_id": model_id},
        )
    )


async def _run_refit(
    db: AsyncSession,
    service: ModelService,
    cohort_service: CohortService,
    user_id: int,
    model_id: int,
    body: ModelRefitRequest,
    progress: Any = None,
) -> ModelVersionResponse:
    # A refit reads its source from the latest version; if that source is a
    # cohort, refresh its lazily-materialised results first (see compare/create).
    latest = (
        (
            await db.execute(
                select(SurrogateModelVersion)
                .where(SurrogateModelVersion.surrogate_model_id == model_id)
                .order_by(SurrogateModelVersion.ordinal.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if latest is not None:
        await _ensure_cohort_results(
            db, cohort_service, user_id, latest.source_kind, latest.source_id
        )
    version = await service.refit(
        db,
        actor_user_id=user_id,
        model_id=model_id,
        surrogate_kind=body.surrogate_kind,
        tuning_mode=body.tuning_mode,
        progress=progress,
    )
    return _to_version(version)


@router.post("/models/{model_id}/scope", response_model=ModelResponse)
async def set_model_scope(
    model_id: int,
    body: ModelScopeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelResponse:
    """Move a model between scope tiers (§6.4 — personal↔project)."""
    model = await service.set_scope(
        db,
        actor_user_id=current_user.id,
        model_id=model_id,
        new_scope=body.new_scope,
        project_id=body.project_id,
    )
    return _to_model(model)


@router.post("/models/{model_id}/retire", response_model=ModelResponse)
async def retire_model(
    model_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelResponse:
    """Retire a model — withdrawn from pickers, never hard-deleted (D20)."""
    model = await service.retire(db, actor_user_id=current_user.id, model_id=model_id)
    return _to_model(model)


@router.post(
    "/models/{model_id}/versions/{version_id}/submit-review",
    response_model=ModelVersionResponse,
)
async def submit_review(
    model_id: int,
    version_id: int,
    body: ModelSubmitReviewRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    notifier: Annotated[Notifier, Depends(get_notifier)],
) -> ModelVersionResponse:
    """Submit a draft global version into the publish gate (gate 1, §6.3).

    Reads the ``models_global_publish_approval`` toggle: ON → a directed
    checker request (the body's ``checker_user_id``, a Model Curator ≠
    builder); OFF → the builder (who must be a Model Curator) checks
    directly.
    """
    await _version_in_model(db, model_id, version_id)
    approval_required = await _approval_required(db)
    checker_id, checker_name = (
        await _resolve_checker(db, body.checker_user_id) if approval_required else (None, None)
    )
    builder_is_curator = await user_has_permission(db, current_user.id, Permission.MODELS_CURATE)
    version = await service.submit_for_review(
        db,
        actor_user_id=current_user.id,
        version_id=version_id,
        approval_required=approval_required,
        checker_user_id=checker_id,
        checker_username=checker_name,
        builder_is_curator=builder_is_curator,
    )
    if approval_required and checker_id is not None:
        await notify_pending_approval(
            db,
            notifier,
            target_column=ApprovalRequest.surrogate_model_version_id,
            target_value=version_id,
        )
    return _to_version(version)


@router.post(
    "/models/{model_id}/versions/{version_id}/release",
    response_model=ModelVersionResponse,
)
async def release_version(
    model_id: int,
    version_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelVersionResponse:
    """Su/admin release of a checked version to general use (gate 2, §6.3)."""
    await _version_in_model(db, model_id, version_id)
    is_admin = await user_has_permission(db, current_user.id, Permission.PROJECTS_SEE_ALL)
    version = await service.release_version(
        db, actor_user_id=current_user.id, version_id=version_id, is_admin=is_admin
    )
    return _to_version(version)


@router.post("/models/{model_id}/export-as-tool", response_model=ModelExportResponse)
async def export_as_tool(
    model_id: int,
    body: ModelExportRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelExportResponse:
    """Synthesise + install the model's published version as a tool (§7).

    Lands as a ``test`` ``ToolVersion`` for the su/admin to edit + promote
    to ``released``. Inherits the tool install / promote permission ladder.
    """
    model = await service.get(db, actor_user_id=current_user.id, model_id=model_id)
    version_id = model.published_version_id
    if version_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This model has no published version to export.",
        )
    version = await db.get(SurrogateModelVersion, version_id)
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="The published version is missing."
        )
    tool = await register_as_tool(
        db,
        tool_service=tool_service,
        audit=service.audit,
        model=model,
        version=version,
        actor_user_id=current_user.id,
        models_root=service.models_root,
        environment=service.environment,
        block_extrapolation=body.block_extrapolation,
        with_std=body.with_std,
    )
    return ModelExportResponse(tool_alias=tool.alias, tool_state=tool.state, version_id=version.id)


def _require_coverage_feature(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """404 this route when ``design_space_coverage`` is off (ADR-0020 §16).

    The models surface itself is gated on ``surrogate_family``; an install can
    have surrogates without having bought coverage, so this route carries its
    own gate and is *absent* rather than empty when the flag is off.
    """
    if not feature_enabled(settings.features, Feature.DESIGN_SPACE_COVERAGE):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")


@router.get(
    "/models/versions/{version_id}/design-coverage",
    response_model=ModelVersionCoverageResponse,
    dependencies=[Depends(_require_coverage_feature)],
)
async def version_design_coverage(
    version_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ModelService, Depends(get_model_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ModelVersionCoverageResponse:
    """The stored design-space coverage report for a version (ADR-0020 §9).

    Evidence, not recomputation: the verdict was reached at fit and travels
    with the version, so this reads what was decided rather than re-deciding
    it against today's data or today's thresholds.

    Deliberately a separate path from ``/coverage``, which is the §7.3
    training-range gauge. ``assessed=false`` means the version predates the
    feature — the caller renders "not assessed", never "good".
    """
    report = await service.coverage_report_for_version(
        db, actor_user_id=current_user.id, version_id=version_id
    )
    if report is None:
        return ModelVersionCoverageResponse(version_id=version_id, assessed=False)
    from matofx.api.routers.cohorts import _coverage_response  # noqa: PLC0415

    return ModelVersionCoverageResponse(
        version_id=version_id, assessed=True, report=_coverage_response(report)
    )
