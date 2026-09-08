"""HTTP API for parametric studies (ADR-0001 / §G39a).

Mirrors the workflow-run router conventions: thin endpoints that
delegate to :class:`~matofx.studies.StudyService`, ``Annotated`` deps,
domain errors propagate to the central handlers in
:mod:`matofx.api.errors` (registered for the ``StudyGenerationError`` /
``StudyStateError`` families plus the hierarchy ``NotFoundError`` /
``AccessDeniedError``), so there is no per-endpoint try/except.

Gating (design §12):

- **Feature flag** — a router-wide dependency 404s every route when
  ``features.parametric_studies`` is off, so the surface is *absent*
  for a customer without the flag (not merely forbidden).
- **Permission** — every mutation requires ``parametric_studies.create``;
  read endpoints follow the project ACL the service already enforces.
- **Anti-escalation (§G35 Q2)** — creating / submitting a study
  requires the caller to already be authorised to run the underlying
  tool (the tool-state visibility ladder), re-checked at submit.
"""

from __future__ import annotations

import contextlib
import csv
import io
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api._approval_notify import notify_pending_approval
from matofx.api.deps import (
    get_current_user,
    get_db,
    get_notifier,
    get_results_service,
    get_series_service,
    get_settings,
    get_study_service,
)
from matofx.api.schemas import (
    AnovaResponse,
    AnovaTermEntry,
    AvailableModesResponse,
    CsvAggregateResponse,
    CsvAggregateValue,
    CsvChildSeries,
    CsvSeriesResponse,
    FactorSensitivityEntry,
    FactorSpecPreviewRequest,
    FactorSpecPreviewResponse,
    MainEffectPoint,
    ResponseSourceCsvPlot,
    ResponseSourceScalar,
    ResponseSourcesDescriptor,
    RsmResponse,
    SensitivityMatrixResponse,
    SensitivityMatrixRow,
    SensitivityResponse,
    StudyChildJob,
    StudyChildResultEntry,
    StudyCreateRequest,
    StudyDetailResponse,
    StudyResponse,
    StudyResultsResponse,
)
from matofx.approvals.orm import ApprovalRequest
from matofx.config import Settings
from matofx.features import Feature, feature_enabled
from matofx.hierarchy.errors import NotFoundError
from matofx.hierarchy.orm import Job, Project
from matofx.notify import Notifier
from matofx.studies import (
    FactorSpec,
    ResultsService,
    SamplingParams,
    SeriesService,
    StudyChildResult,
    StudyResultsView,
    StudyService,
    anova_contributions,
    available_modes,
    compute_sensitivity,
    fit_rsm,
    generate,
    main_effects,
    rsm_predict,
    varied_columns,
)
from matofx.studies.orm import ParametricStudy
from matofx.tools.descriptor import ToolDescriptor
from matofx.tools.orm import Tool, ToolVersion
from matofx.tools.states import user_can_see_state
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission

# Preview reports what a study *would* generate even when that exceeds
# the run cap — the cap is a submit-time refusal (design §4 step 4 vs
# §9 step 5). A ceiling this high never trips RunCapExceededError but
# still bounds a pathological factor spec.
_PREVIEW_CAP = 10_000_000
# Install-wide default cap when neither the request nor a per-tool
# override sets one (2026-05-20 amendment; per-tool override lands in
# pre-b0-β). Pilot-revisable — see designs/parametric-studies.md §16.
_DEFAULT_RUN_CAP = 500
# Above this many distinct levels a factor is continuous, not a categorical
# ANOVA / main-effects factor (keeps LHS sweeps out of the s-4 plots).
_ANOVA_MAX_LEVELS = 12
# Resolution of the RSM contour mesh per axis (s-5).
_CONTOUR_STEPS = 25
# A contour needs two axes — below this many varied factors there's nothing
# to plot (the fit + stationary point are still returned).
_MIN_CONTOUR_FACTORS = 2


def _require_studies_feature(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """404 every route when the feature flag is off (routes *absent*)."""
    if not feature_enabled(settings.features, Feature.PARAMETRIC_STUDIES):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")


router = APIRouter(dependencies=[Depends(_require_studies_feature)])


async def _require_create(session: AsyncSession, actor_user_id: int) -> None:
    if not await user_has_permission(session, actor_user_id, Permission.PARAMETRIC_STUDIES_CREATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You don't have permission to create or run parametric "
                f"studies ({Permission.PARAMETRIC_STUDIES_CREATE.value})."
            ),
        )


async def _require_tool_runnable(
    session: AsyncSession, actor_user_id: int, tool_alias: str
) -> None:
    """Anti-escalation (§G35 Q2) — caller must be authorised to run the tool.

    The same tool-state visibility ladder a direct job submission uses
    (released → any active user; test/new → the matching see-state
    permission). Re-checked at submit.
    """
    tool = (
        await session.execute(select(Tool).where(Tool.alias == tool_alias.strip()))
    ).scalar_one_or_none()
    if tool is None:
        raise NotFoundError("tool", tool_alias)
    if not await user_can_see_state(session, actor_user_id, tool.state):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"You aren't authorised to run {tool_alias!r}, so you can't "
                "build a parametric study around it. Ask an administrator "
                "for access to that tool."
            ),
        )


async def _resolve_cap(db: AsyncSession, tool_alias: str, body_cap: int | None) -> int:
    """Effective run cap: request override -> per-tool override -> install default.

    The per-tool override lives on ``Tool.run_cap`` and is editable by
    admin/SU on the Edit Tool Metadata tab (ADR-0001 2026-05-20
    revision). ``_require_tool_runnable`` has already validated the
    tool exists in every caller, so a ``None`` here means "no per-tool
    override set".
    """
    if body_cap is not None:
        return body_cap
    per_tool = (
        await db.execute(select(Tool.run_cap).where(Tool.alias == tool_alias.strip()))
    ).scalar_one_or_none()
    if per_tool is not None:
        return int(per_tool)
    return _DEFAULT_RUN_CAP


async def _latest_descriptor(db: AsyncSession, tool_alias: str) -> ToolDescriptor:
    """The tool's currently-deployed descriptor (newest version snapshot)."""
    tv = (
        await db.execute(
            select(ToolVersion)
            .where(ToolVersion.alias == tool_alias.strip())
            .order_by(ToolVersion.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if tv is None:
        raise NotFoundError("tool", tool_alias)
    return ToolDescriptor.model_validate(tv.descriptor_snapshot)


def _to_study(
    s: ParametricStudy,
    *,
    tool_name: str = "",
    project_name: str = "",
    created_by_name: str = "",
    tool_version: str = "",
) -> StudyResponse:
    return StudyResponse(
        id=s.id,
        project_id=s.project_id,
        project_name=project_name,
        name=s.name,
        description=s.description,
        tool_alias=s.tool_alias,
        tool_name=tool_name,
        tool_version_id=s.tool_version_id,
        tool_version=tool_version,
        created_by_name=created_by_name,
        mode=s.mode,
        state=s.state,
        factor_spec=s.factor_spec or {},
        sampling_params=s.sampling_params or {},
        run_cap=s.run_cap,
        generated_count=s.generated_count,
        skipped_invalid_count=s.skipped_invalid_count,
        seed=s.seed,
        created_by_user_id=s.created_by_user_id,
        created_at=s.created_at,
        updated_at=s.updated_at,
        state_changed_at=s.state_changed_at,
        cancel_requested_at=s.cancel_requested_at,
    )


async def _enrich_studies(
    db: AsyncSession, studies: Sequence[ParametricStudy]
) -> list[StudyResponse]:
    """Attach display names: tool label, project, creator, version string.

    One batched lookup each (over all the studies' tool aliases / project
    ids / creator ids / pinned version ids) so the catalogue rows can read
    by label rather than the raw alias / id
    ([[feedback_use_labels_not_names]]).
    """
    if not studies:
        return []
    aliases = {s.tool_alias for s in studies}
    project_ids = {s.project_id for s in studies}
    user_ids = {s.created_by_user_id for s in studies}
    version_ids = {s.tool_version_id for s in studies}
    tool_names = {
        str(a): str(n)
        for a, n in (
            await db.execute(select(Tool.alias, Tool.name).where(Tool.alias.in_(aliases)))
        ).all()
    }
    project_names = {
        int(i): str(n)
        for i, n in (
            await db.execute(select(Project.id, Project.name).where(Project.id.in_(project_ids)))
        ).all()
    }
    creator_names = {
        int(i): str(full or username)
        for i, full, username in (
            await db.execute(
                select(User.id, User.full_name, User.username).where(User.id.in_(user_ids))
            )
        ).all()
    }
    version_strings = {
        int(i): str(v)
        for i, v in (
            await db.execute(
                select(ToolVersion.id, ToolVersion.version).where(ToolVersion.id.in_(version_ids))
            )
        ).all()
    }
    return [
        _to_study(
            s,
            tool_name=tool_names.get(s.tool_alias, ""),
            project_name=project_names.get(s.project_id, ""),
            created_by_name=creator_names.get(s.created_by_user_id, ""),
            tool_version=version_strings.get(s.tool_version_id, ""),
        )
        for s in studies
    ]


async def _enrich_study(db: AsyncSession, study: ParametricStudy) -> StudyResponse:
    """Single-study convenience over :func:`_enrich_studies`."""
    return (await _enrich_studies(db, [study]))[0]


def _to_child(j: Job) -> StudyChildJob:
    return StudyChildJob(
        id=j.id,
        name=j.name,
        tool_alias=j.tool_alias,
        state=j.state,
        inputs=j.inputs or {},
        created_at=j.created_at,
        started_at=j.started_at,
        completed_at=j.completed_at,
        exit_code=j.exit_code,
        cancel_requested_at=j.cancel_requested_at,
    )


async def _detail(
    db: AsyncSession, study_service: StudyService, actor_user_id: int, study_id: int
) -> StudyDetailResponse:
    study = await study_service.get(db, actor_user_id=actor_user_id, study_id=study_id)
    children = await study_service.list_children(db, actor_user_id=actor_user_id, study_id=study_id)
    # Pinned-descriptor label/unit map so the Progress list shows labels,
    # not raw input names (the same source the Results tab uses).
    descriptor = await _pinned_descriptor_for(db, study.tool_version_id)
    return StudyDetailResponse(
        study=await _enrich_study(db, study),
        children=[_to_child(c) for c in children],
        # ``unit`` is nullable on the descriptor (and ``label`` defaults to
        # the name); coerce so the dict[str, str] schema is satisfied.
        input_labels={i.name: (i.label or i.name) for i in descriptor.inputs},
        input_units={i.name: (i.unit or "") for i in descriptor.inputs},
        input_groups=[
            {"name": g.name, "label": g.label, "column": g.column, "row": g.row}
            for g in (descriptor.input_groups or [])
        ],
        input_group_of={i.name: i.group for i in descriptor.inputs if i.group},
    )


@router.post(
    "/projects/{project_id}/studies",
    response_model=StudyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_study(
    project_id: int,
    body: StudyCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    study_service: Annotated[StudyService, Depends(get_study_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StudyResponse:
    """Create a ``draft`` parametric study in a project."""
    await _require_create(db, current_user.id)
    await _require_tool_runnable(db, current_user.id, body.tool_alias)
    study = await study_service.create(
        db,
        actor_user_id=current_user.id,
        project_id=project_id,
        name=body.name,
        tool_alias=body.tool_alias,
        mode=body.mode,
        factor_spec=body.factor_spec,
        sampling_params=body.sampling_params,
        description=body.description,
        seed=body.seed,
        run_cap=await _resolve_cap(db, body.tool_alias, body.run_cap),
    )
    return await _enrich_study(db, study)


@router.get("/projects/{project_id}/studies", response_model=list[StudyResponse])
async def list_studies(
    project_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    study_service: Annotated[StudyService, Depends(get_study_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[StudyResponse]:
    """Every study in a project, newest first (project VIEW required)."""
    studies = await study_service.list_for_project(
        db, actor_user_id=current_user.id, project_id=project_id
    )
    return await _enrich_studies(db, studies)


@router.get("/studies/{study_id}", response_model=StudyDetailResponse)
async def get_study(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    study_service: Annotated[StudyService, Depends(get_study_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StudyDetailResponse:
    """One study + its child jobs (project VIEW required)."""
    return await _detail(db, study_service, current_user.id, study_id)


@router.post("/studies/{study_id}/submit", response_model=StudyResponse)
async def submit_study(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    study_service: Annotated[StudyService, Depends(get_study_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    notifier: Annotated[Notifier, Depends(get_notifier)],
) -> StudyResponse:
    """Generate the matrix and fan it into queued child jobs."""
    await _require_create(db, current_user.id)
    study = await study_service.get(db, actor_user_id=current_user.id, study_id=study_id)
    # Anti-escalation re-check at submit (§G35 Q2): the actor must still
    # be authorised to run the pinned tool.
    await _require_tool_runnable(db, current_user.id, study.tool_alias)
    submitted = await study_service.submit(db, actor_user_id=current_user.id, study_id=study_id)
    # No-op unless the submit opened a pending study-level approval gate.
    await notify_pending_approval(
        db, notifier, target_column=ApprovalRequest.parametric_study_id, target_value=study_id
    )
    return await _enrich_study(db, submitted)


@router.post("/studies/{study_id}/cancel", response_model=StudyResponse)
async def cancel_study(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    study_service: Annotated[StudyService, Depends(get_study_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StudyResponse:
    """Cancel the study and signal still-pending children."""
    await _require_create(db, current_user.id)
    study = await study_service.cancel(db, actor_user_id=current_user.id, study_id=study_id)
    return await _enrich_study(db, study)


@router.post(
    "/studies/{study_id}/children/{job_id}/retry",
    response_model=StudyDetailResponse,
)
async def retry_study_child(
    study_id: int,
    job_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    study_service: Annotated[StudyService, Depends(get_study_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StudyDetailResponse:
    """Re-queue one failed child; returns the refreshed study + children."""
    await _require_create(db, current_user.id)
    await study_service.retry_child(
        db, actor_user_id=current_user.id, study_id=study_id, job_id=job_id
    )
    return await _detail(db, study_service, current_user.id, study_id)


@router.get(
    "/studies/available-modes/{tool_alias}",
    response_model=AvailableModesResponse,
)
async def study_available_modes(
    tool_alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AvailableModesResponse:
    """Modes the tool's input *types* can support (selector pre-filter).

    The two-layer rule (design §9 step 3): this greys impossible modes
    up front; ``FactorSpec.validate_for_mode`` still re-checks the
    actual Constant/Vary selection at preview and submit.
    """
    await _require_create(db, current_user.id)
    await _require_tool_runnable(db, current_user.id, tool_alias)
    descriptor = await _latest_descriptor(db, tool_alias)
    modes = {str(k): v for k, v in available_modes(descriptor).items()}
    return AvailableModesResponse(modes=modes)


@router.post("/studies/preview", response_model=FactorSpecPreviewResponse)
async def preview_factor_spec(
    body: FactorSpecPreviewRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FactorSpecPreviewResponse:
    """The live pre-submit count + run matrix (design §9 step 4/5).

    Pure: resolves the tool's currently-deployed descriptor and runs
    the generator with an effectively-unbounded cap so the form can
    show "would run N (M skipped)" before the study is even created.
    The full matrix is returned only when the design fits the form's
    cap; over-cap the modal shows the refusal + count instead (§9.5).
    The real run-cap refusal happens at submit.
    """
    await _require_create(db, current_user.id)
    await _require_tool_runnable(db, current_user.id, body.tool_alias)
    descriptor = await _latest_descriptor(db, body.tool_alias)
    spec = FactorSpec.from_obj(body.factor_spec)
    sampling = SamplingParams.model_validate(body.sampling_params or {})
    result = generate(
        descriptor,
        spec,
        body.mode,  # type: ignore[arg-type]
        sampling,
        run_cap=_PREVIEW_CAP,
        seed=body.seed,
    )
    cap = await _resolve_cap(db, body.tool_alias, body.run_cap)
    over_cap = result.generated_count > cap
    return FactorSpecPreviewResponse(
        generated_count=result.generated_count,
        skipped_invalid_count=result.skipped_invalid_count,
        raw_count=result.raw_count,
        skip_reasons=result.skip_reasons,
        level_warnings=list(result.level_warnings),
        run_cap=cap,
        over_cap=over_cap,
        points=[] if over_cap else result.points,
    )


# --- results (2-PS-b0 / ADR-0001 §10.0) ---------------------------------


async def _pinned_descriptor_for(db: AsyncSession, tool_version_id: int) -> ToolDescriptor:
    """Resolve the *pinned* descriptor for a study from its ``tool_version_id``.

    Unlike :func:`_latest_descriptor` (which serves preview /
    available-modes against the currently-deployed version), the
    results endpoint reads response sources from the snapshot the
    study was *created* against (§5 tool-version pinning).
    """
    tv = (
        await db.execute(select(ToolVersion).where(ToolVersion.id == tool_version_id))
    ).scalar_one_or_none()
    if tv is None:  # pragma: no cover — FK guarantees this row exists
        raise NotFoundError("tool_version", str(tool_version_id))
    return ToolDescriptor.model_validate(tv.descriptor_snapshot)


def _extract_response_sources(descriptor: ToolDescriptor) -> ResponseSourcesDescriptor:
    """Pick scalars + ``csv_plot`` files out of the descriptor's outputs."""
    scalars: list[ResponseSourceScalar] = []
    csv_plots: list[ResponseSourceCsvPlot] = []
    for out in descriptor.outputs:
        if out.type in ("number", "string"):
            scalars.append(
                ResponseSourceScalar(
                    name=out.name,
                    label=out.label,
                    unit=out.unit,
                    type=out.type,
                )
            )
        elif out.type == "file" and out.file_type == "csv_plot" and out.filename:
            csv_plots.append(
                ResponseSourceCsvPlot(
                    name=out.name,
                    label=out.label,
                    filename=out.filename,
                    xaxis_column=out.xaxis_column,
                    yaxis_column=out.yaxis_column,
                    plot_title=out.plot_title,
                    xaxis_title=out.xaxis_title,
                    yaxis_title=out.yaxis_title,
                )
            )
    return ResponseSourcesDescriptor(scalars=scalars, csv_plots=csv_plots)


def _results_csv(view: StudyResultsView) -> bytes:
    """Serialise the study results matrix to CSV bytes.

    Columns: ``job_id, state, <input names...>, <scalar names...>``.
    Input + scalar columns are taken from the union of keys across
    all children (sorted) so a still-running child with no scalars
    yet still produces a row with empty cells for the missing
    scalars (consistent with the matrix display).
    """
    input_cols = sorted({k for c in view.children for k in c.inputs})
    scalar_cols = sorted({k for c in view.children for k in c.scalars})
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["job_id", "state", *input_cols, *scalar_cols])
    for c in view.children:
        writer.writerow(
            [
                c.job_id,
                c.state,
                *("" if c.inputs.get(col) is None else c.inputs.get(col) for col in input_cols),
                *("" if c.scalars.get(col) is None else c.scalars.get(col) for col in scalar_cols),
            ]
        )
    return buf.getvalue().encode("utf-8")


@router.get("/studies/{study_id}/results", response_model=StudyResultsResponse)
async def get_study_results(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    results_service: Annotated[ResultsService, Depends(get_results_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StudyResultsResponse:
    """The study's results matrix + descriptor-derived response sources.

    One row per child (children that haven't completed yet surface
    with empty ``scalars`` so the UI can still render the row, per
    D3 / partial-data banner). ``response_sources`` is derived from
    the *pinned* tool descriptor (§5), so the picker stays stable
    across mid-study tool updates. ACL: project VIEW (enforced by
    :meth:`ResultsService.get_results_for_study` via
    :meth:`StudyService.get`).
    """
    view = await results_service.get_results_for_study(
        db, actor_user_id=current_user.id, study_id=study_id
    )
    descriptor = await _pinned_descriptor_for(db, view.tool_version_id)
    # Physical output bounds (ADR-0017) are version-independent facts (a failure
    # index is >= 0 whatever the tool version), so resolve them from the tool's
    # *current* descriptor — that way declaring a bound benefits existing studies
    # (the profiler clamp) without a re-run. Falls back to the pinned descriptor.
    bounds_descriptor = descriptor
    with contextlib.suppress(NotFoundError):
        bounds_descriptor = await _latest_descriptor(db, view.tool_alias)
    return StudyResultsResponse(
        study_id=view.study_id,
        tool_alias=view.tool_alias,
        tool_version_id=view.tool_version_id,
        response_sources=_extract_response_sources(descriptor),
        input_labels={i.name: i.label for i in descriptor.inputs},
        input_units={i.name: i.unit for i in descriptor.inputs},
        input_options={i.name: list(i.options) for i in descriptor.inputs if i.options},
        input_groups={i.name: (i.group or "") for i in descriptor.inputs},
        output_groups={o.name: (o.group or "") for o in descriptor.outputs},
        input_group_labels={g.name: g.label for g in descriptor.input_groups},
        output_group_labels={g.name: g.label for g in descriptor.output_groups},
        output_bounds={
            o.name: {"min": o.min, "max": o.max}
            for o in bounds_descriptor.outputs
            if o.type == "number" and (o.min is not None or o.max is not None)
        },
        children=[
            StudyChildResultEntry(
                job_id=c.job_id,
                state=c.state,
                inputs=c.inputs,
                scalars=c.scalars,
                retained_files=c.retained_files,
                validity=c.validity,
                validity_reasons=c.validity_reasons,
            )
            for c in view.children
        ],
    )


@router.get("/studies/{study_id}/results.csv")
async def export_study_results_csv(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    results_service: Annotated[ResultsService, Depends(get_results_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    """CSV download of the matrix (design §10 / 2-PS-b0).

    Filename ``study_{id:07d}_results.csv``; rows = children;
    columns = ``job_id, state, <inputs...>, <scalars...>``. ACL =
    same as the JSON endpoint (project VIEW).
    """
    view = await results_service.get_results_for_study(
        db, actor_user_id=current_user.id, study_id=study_id
    )
    body = _results_csv(view)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (f'attachment; filename="study_{study_id:07d}_results.csv"'),
        },
    )


@router.get("/studies/{study_id}/csv-series", response_model=CsvSeriesResponse)
async def get_study_csv_series(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    series_service: Annotated[SeriesService, Depends(get_series_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    output: Annotated[str, Query(min_length=1, max_length=64)],
    x_column: Annotated[int, Query(ge=0)] = 0,
    y_column: Annotated[int, Query(ge=0)] = 1,
    max_points: Annotated[int, Query(ge=2, le=10_000)] = 500,
) -> CsvSeriesResponse:
    """Per-child down-sampled ``(x, y)`` series for a ``csv_plot`` output (2-PS-b).

    Feeds the curve-overlay. Reads the retained files server-side and
    down-samples each series to ``max_points``. ACL = project VIEW.
    """
    view = await series_service.get_series(
        db,
        actor_user_id=current_user.id,
        study_id=study_id,
        output_name=output,
        x_column=x_column,
        y_column=y_column,
        max_points=max_points,
    )
    return CsvSeriesResponse(
        study_id=view.study_id,
        output_name=view.output_name,
        x_column=view.x_column,
        y_column=view.y_column,
        children=[CsvChildSeries(job_id=c.job_id, x=c.x, y=c.y) for c in view.children],
    )


@router.get("/studies/{study_id}/csv-aggregate", response_model=CsvAggregateResponse)
async def get_study_csv_aggregate(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    series_service: Annotated[SeriesService, Depends(get_series_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    output: Annotated[str, Query(min_length=1, max_length=64)],
    column: Annotated[int, Query(ge=0)],
    aggregate: Annotated[str, Query(pattern="^(last|max|min|mean)$")] = "last",
) -> CsvAggregateResponse:
    """Per-child reduction of a ``csv_plot`` column to a scalar response (2-PS-b).

    The computed half of the b0-5 response picker. ACL = project VIEW.
    """
    view = await series_service.get_aggregate(
        db,
        actor_user_id=current_user.id,
        study_id=study_id,
        output_name=output,
        column=column,
        aggregate=aggregate,
    )
    return CsvAggregateResponse(
        study_id=view.study_id,
        output_name=view.output_name,
        column=view.column,
        aggregate=view.aggregate,
        values=[CsvAggregateValue(job_id=v.job_id, value=v.value) for v in view.values],
    )


# --- sensitivity (2-PS-b5) ----------------------------------------------


def _numeric_factor_columns(
    children: Sequence[StudyChildResult],
) -> tuple[list[str], dict[str, list[float | None]]]:
    """Numeric inputs across children → (sorted names, per-child columns).

    A key is a factor when at least one child has a numeric (non-bool)
    value for it; each column is aligned to ``children`` order with
    ``None`` where a child's value is missing or non-numeric. Mirrors the
    Explore tab's ``_numeric_inputs`` so the sensitivity axes match the
    factors the user can already plot.
    """
    names = sorted(
        {
            k
            for c in children
            for k, v in c.inputs.items()
            if isinstance(v, int | float) and not isinstance(v, bool)
        }
    )
    columns: dict[str, list[float | None]] = {n: [] for n in names}
    for c in children:
        for n in names:
            v = c.inputs.get(n)
            columns[n].append(
                float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None
            )
    return names, columns


def _varied_factor_columns(
    children: Sequence[StudyChildResult],
) -> tuple[list[str], dict[str, list[float | None]]]:
    """Like :func:`_numeric_factor_columns` but only the **varied** inputs.

    Sensitivity ranks the inputs the study actually swept (≥ 2 distinct
    values across children) — a constant input is not a sensitivity axis
    (2-PS-b'). Filtering is delegated to :func:`varied_columns`.
    """
    _, columns = _numeric_factor_columns(children)
    names = varied_columns(columns)
    return names, {n: columns[n] for n in names}


def _scalar_vector(children: Sequence[StudyChildResult], name: str) -> list[float | None]:
    """Per-child numeric value of one scalar output, aligned to ``children``."""
    out: list[float | None] = []
    for c in children:
        v = c.scalars.get(name)
        out.append(float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None)
    return out


async def _response_vector(
    db: AsyncSession,
    series_service: SeriesService,
    *,
    actor_user_id: int,
    study_id: int,
    children: Sequence[StudyChildResult],
    source: str,
    response: str,
    column: int,
    aggregate: str,
) -> tuple[str, list[float | None]]:
    """Per-child response aligned to ``children`` order, plus a display label.

    ``source="scalar"`` reads the named scalar output off each child;
    ``source="csv_plot"`` reduces a column of the named ``csv_plot``
    output server-side (the same path the response picker uses).
    """
    if source == "csv_plot":
        agg = await series_service.get_aggregate(
            db,
            actor_user_id=actor_user_id,
            study_id=study_id,
            output_name=response,
            column=column,
            aggregate=aggregate,
        )
        by_job = {v.job_id: v.value for v in agg.values}
        label = f"{response} - col{column} - {aggregate}"
        return label, [by_job.get(c.job_id) for c in children]
    return response, _scalar_vector(children, response)


@router.get("/studies/{study_id}/sensitivity", response_model=SensitivityResponse)
async def get_study_sensitivity(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    results_service: Annotated[ResultsService, Depends(get_results_service)],
    series_service: Annotated[SeriesService, Depends(get_series_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Annotated[str, Query(min_length=1, max_length=64)],
    source: Annotated[str, Query(pattern="^(scalar|csv_plot)$")] = "scalar",
    column: Annotated[int, Query(ge=0)] = 0,
    aggregate: Annotated[str, Query(pattern="^(last|max|min|mean)$")] = "last",
) -> SensitivityResponse:
    """Rank the study's varied factors by their influence on one response (2-PS-b5).

    Only the inputs the study actually swept are ranked (2-PS-b').
    Pearson + Spearman correlation per factor, plus standardized
    regression coefficients (the tornado magnitudes) from an OLS fit on
    z-scored inputs and response. The response axis is a scalar output
    (``source=scalar``) or a ``csv_plot`` column reduced by an aggregate
    (``source=csv_plot`` + ``column`` / ``aggregate``), matching the
    Results-tab response picker. Computed only over children that have a
    response value; below three the result is flagged ``sufficient=False``
    so the UI greys the panel. ACL = project VIEW (enforced by the
    results service via :meth:`StudyService.get`).
    """
    view = await results_service.get_results_for_study(
        db, actor_user_id=current_user.id, study_id=study_id
    )
    names, columns = _varied_factor_columns(view.children)
    label, values = await _response_vector(
        db,
        series_service,
        actor_user_id=current_user.id,
        study_id=study_id,
        children=view.children,
        source=source,
        response=response,
        column=column,
        aggregate=aggregate,
    )
    result = compute_sensitivity(label, names, columns, values)
    return SensitivityResponse(
        study_id=view.study_id,
        response=result.response,
        factors=[
            FactorSensitivityEntry(
                factor=f.factor,
                pearson=f.pearson,
                spearman=f.spearman,
                std_coef=f.std_coef,
                n=f.n,
                pearson_p=f.pearson_p,
                spearman_p=f.spearman_p,
                std_coef_se=f.std_coef_se,
                std_coef_p=f.std_coef_p,
                std_coef_ci_low=f.std_coef_ci_low,
                std_coef_ci_high=f.std_coef_ci_high,
            )
            for f in result.factors
        ],
        r_squared=result.r_squared,
        adj_r_squared=result.adj_r_squared,
        rmse=result.rmse,
        n_samples=result.n_samples,
        sufficient=result.sufficient,
        message=result.message,
    )


@router.get("/studies/{study_id}/sensitivity-matrix", response_model=SensitivityMatrixResponse)
async def get_study_sensitivity_matrix(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    results_service: Annotated[ResultsService, Depends(get_results_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    responses: Annotated[list[str], Query(min_length=1, max_length=50)],
) -> SensitivityMatrixResponse:
    """Standardized-regression coefficients for varied inputs x several outputs (2-PS-b').

    The grid behind the sensitivity **table**: rows are the study's varied
    inputs, columns are the caller's chosen scalar outputs, each cell the
    standardized regression coefficient (``None`` where the fit could not
    run for that output). Scalars only — repeat ``responses=`` per output.
    ACL = project VIEW (via :meth:`StudyService.get`).
    """
    view = await results_service.get_results_for_study(
        db, actor_user_id=current_user.id, study_id=study_id
    )
    names, columns = _varied_factor_columns(view.children)
    coef_by_factor: dict[str, dict[str, float | None]] = {n: {} for n in names}
    pval_by_factor: dict[str, dict[str, float | None]] = {n: {} for n in names}
    r_squared: dict[str, float | None] = {}
    seen: list[str] = []
    for response in responses:
        if response in seen:
            continue
        seen.append(response)
        result = compute_sensitivity(
            response, names, columns, _scalar_vector(view.children, response)
        )
        coef = {f.factor: f.std_coef for f in result.factors}
        pval = {f.factor: f.std_coef_p for f in result.factors}
        for n in names:
            coef_by_factor[n][response] = coef.get(n)
            pval_by_factor[n][response] = pval.get(n)
        r_squared[response] = result.r_squared
    return SensitivityMatrixResponse(
        study_id=view.study_id,
        factors=names,
        responses=seen,
        rows=[
            SensitivityMatrixRow(
                factor=n, coefficients=coef_by_factor[n], pvalues=pval_by_factor[n]
            )
            for n in names
        ],
        r_squared=r_squared,
        message=None,
    )


@router.get("/studies/{study_id}/anova", response_model=AnovaResponse)
async def get_study_anova(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    results_service: Annotated[ResultsService, Depends(get_results_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Annotated[str, Query(min_length=1, max_length=64)],
) -> AnovaResponse:
    """ANOVA % contribution + main effects for one scalar response (2-PS-c-stats #5).

    Suits designed (grid / factorial) studies: each varied input with a
    small number of levels is treated as a categorical factor, and the
    total variance is decomposed by main effect + two-factor interaction
    (sequential / Type I). ``main_effects`` carries the mean response per
    level for the categorical factors. ACL = project VIEW.
    """
    view = await results_service.get_results_for_study(
        db, actor_user_id=current_user.id, study_id=study_id
    )
    _, columns = _varied_factor_columns(view.children)
    resp = _scalar_vector(view.children, response)
    res = anova_contributions(columns, resp)
    effects = {
        name: pts
        for name, pts in main_effects(columns, resp).items()
        if len(pts) <= _ANOVA_MAX_LEVELS
    }
    return AnovaResponse(
        study_id=view.study_id,
        response=response,
        terms=[AnovaTermEntry(term=t.term, contribution=t.contribution) for t in res.terms],
        residual=res.residual,
        n=res.n,
        message=res.message,
        main_effects={
            name: [MainEffectPoint(level=lvl, mean=mean) for lvl, mean in pts]
            for name, pts in effects.items()
        },
    )


def _linspace(lo: float, hi: float, steps: int) -> list[float]:
    """``steps`` evenly-spaced points from ``lo`` to ``hi`` inclusive."""
    if steps <= 1 or hi <= lo:
        return [lo]
    span = (hi - lo) / (steps - 1)
    return [lo + span * i for i in range(steps)]


@router.get("/studies/{study_id}/rsm", response_model=RsmResponse)
async def get_study_rsm(
    study_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    results_service: Annotated[ResultsService, Depends(get_results_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Annotated[str, Query(min_length=1, max_length=64)],
    x: Annotated[str, Query(max_length=64)] = "",
    y: Annotated[str, Query(max_length=64)] = "",
) -> RsmResponse:
    """Quadratic response surface for one scalar output (2-PS-c-stats #6).

    Fits ``y ~ 1 + linear + squares + 2-way interactions`` over the varied
    inputs, then returns the fit quality, the surface's stationary point,
    and a contour mesh over the chosen ``x`` / ``y`` factors (others held
    at their mean). Needs more complete runs than quadratic terms; ``x`` /
    ``y`` default to the first two varied factors. ACL = project VIEW.
    """
    view = await results_service.get_results_for_study(
        db, actor_user_id=current_user.id, study_id=study_id
    )
    _, columns = _varied_factor_columns(view.children)
    fit = fit_rsm(columns, _scalar_vector(view.children, response))

    x_name = y_name = ""
    x_grid: list[float] = []
    y_grid: list[float] = []
    z: list[list[float]] = []
    message = fit.message
    if fit.coef and len(fit.factors) >= _MIN_CONTOUR_FACTORS:
        x_name = x if x in fit.factors else fit.factors[0]
        y_name = y if y in fit.factors else next(f for f in fit.factors if f != x_name)
        x_lo, x_hi = fit.ranges[x_name]
        y_lo, y_hi = fit.ranges[y_name]
        x_grid = _linspace(x_lo, x_hi, _CONTOUR_STEPS)
        y_grid = _linspace(y_lo, y_hi, _CONTOUR_STEPS)
        z = [[rsm_predict(fit, {x_name: xv, y_name: yv}) for xv in x_grid] for yv in y_grid]
    elif fit.coef:
        message = "A response-surface contour needs at least two varied factors."

    return RsmResponse(
        study_id=view.study_id,
        response=response,
        factors=fit.factors,
        x=x_name,
        y=y_name,
        x_grid=x_grid,
        y_grid=y_grid,
        z=z,
        r_squared=fit.r_squared,
        adj_r_squared=fit.adj_r_squared,
        rmse=fit.rmse,
        n=fit.n,
        stationary=fit.stationary,
        stationary_kind=fit.stationary_kind,
        stationary_value=fit.stationary_value,
        message=message,
    )
