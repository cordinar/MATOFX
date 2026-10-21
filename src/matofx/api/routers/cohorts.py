"""HTTP API for job cohorts (ADR-0008 §3 / §11.1).

Thin endpoints that delegate to :class:`~matofx.studies.cohort.CohortService`;
domain errors (`CohortError` family + the hierarchy ``NotFoundError`` /
``AccessDeniedError``) propagate to the central handlers in
:mod:`matofx.api.errors`, so there is no per-endpoint try/except.

Gating (surrogate §14):

- **Feature flag** — a router-wide dependency 404s every route when
  ``features.surrogate_family`` is off, so the surface is *absent* for a
  customer without the flag (not merely forbidden).
- **Permission** — a cohort is a workspace-level object: create / edit /
  archive require the ``cohorts.curate`` capability (the ``cohort_curator``
  role; su/admin hold it) and reads are open to any authenticated caller,
  all enforced by the service. A cohort's member results stay intersected
  with the caller's per-project ACL, so an open read never exposes runs
  from projects the caller can't see (D7).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import (
    get_cohort_service,
    get_current_user,
    get_db,
    get_settings,
)
from matofx.api.schemas import (
    CohortBuildMetaResponse,
    CohortCandidate,
    CohortCoveragePlanRequest,
    CohortCoverageRequest,
    CohortCoverageStudyRequest,
    CohortCoverageSuggestRequest,
    CohortCreateRequest,
    CohortFacet,
    CohortMembersResponse,
    CohortNumericInput,
    CohortPreviewRequest,
    CohortPreviewResponse,
    CohortResponse,
    CohortResultColumn,
    CohortResultRow,
    CohortResultsResponse,
    CohortUpdateRequest,
    CohortVersionBand,
    CoverageDiagnosticsResponse,
    CoverageEnvelopeFactor,
    CoverageFactorRow,
    CoveragePairRow,
    CoveragePlanOption,
    CoveragePlanResponse,
    CoverageReportResponse,
    CoverageResponseRow,
    CoverageSuggestionResponse,
    CoverageWarningResponse,
    StudyResponse,
)
from matofx.config import Settings
from matofx.features import Feature, feature_enabled
from matofx.hierarchy.orm import Job
from matofx.studies.cohort import CohortService, OutputColumn
from matofx.studies.cohort.orm import JobCohort
from matofx.studies.cohort.service import _normalise_cohort_scalars, _version_sort_key
from matofx.studies.orm import StudyResult
from matofx.tools.orm import Tool, ToolVersion
from matofx.users.orm import User

# Filter fields shared by create + update — kept here so the two request
# schemas marshal into service kwargs identically.
_FILTER_FIELDS: tuple[str, ...] = (
    "project_ids",
    "scenario_ids",
    "owner_user_ids",
    "completed_after",
    "completed_before",
    "library_file_version_ids",
    "material_version_ids",
    "input_ranges",
    "cross_field_predicate",
    "source_kinds",
    "parametric_study_ids",
    "optimisation_study_ids",
    # Must match ``cohort.service._FILTER_FIELDS``. Two copies of one list is
    # how ``auto_include_study_ids`` came to be accepted by the schema and
    # discarded by both layers; a test now pins them equal.
    "auto_include_study_ids",
)


def _require_cohort_feature(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """404 every route when the surrogate-family flag is off (routes *absent*)."""
    if not feature_enabled(settings.features, Feature.SURROGATE_FAMILY):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")


def _require_coverage_feature(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """404 the coverage route when ``design_space_coverage`` is off (ADR-0020 §16).

    A second, narrower gate than the router-wide one: an install can have
    cohorts and surrogates without having bought coverage. Absent rather than
    forbidden, per the project-wide toggle rule.
    """
    if not feature_enabled(settings.features, Feature.DESIGN_SPACE_COVERAGE):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")


router = APIRouter(dependencies=[Depends(_require_cohort_feature)])


def _to_cohort(
    c: JobCohort,
    *,
    tool_name: str = "",
    created_by_name: str = "",
    member_count: int | None = None,
    tolerated_versions: list[str] | None = None,
) -> CohortResponse:
    return CohortResponse(
        id=c.id,
        name=c.name,
        description=c.description,
        tool_alias=c.tool_alias,
        tool_name=tool_name,
        tolerated_tool_version_ids=list(c.tolerated_tool_version_ids or []),
        tolerated_versions=tolerated_versions or [],
        tolerated_version_bands=c.tolerated_version_bands,
        member_job_ids=c.member_job_ids,
        excluded_job_ids=c.excluded_job_ids,
        project_ids=c.project_ids,
        scenario_ids=c.scenario_ids,
        owner_user_ids=c.owner_user_ids,
        source_kinds=c.source_kinds,
        parametric_study_ids=c.parametric_study_ids,
        optimisation_study_ids=c.optimisation_study_ids,
        completed_after=c.completed_after,
        completed_before=c.completed_before,
        library_file_version_ids=c.library_file_version_ids,
        material_version_ids=c.material_version_ids,
        input_ranges=c.input_ranges,
        auto_include_study_ids=c.auto_include_study_ids,
        cross_field_predicate=c.cross_field_predicate,
        auto_grow=c.auto_grow,
        state=c.state,
        last_resolved_at=c.last_resolved_at,
        created_by_user_id=c.created_by_user_id,
        created_by_name=created_by_name,
        member_count=member_count,
        created_at=c.created_at,
        version=c.version,
    )


async def _tool_names(db: AsyncSession, aliases: set[str]) -> dict[str, str]:
    """Map ``tool_alias → Tool.name`` (the display label) for the given aliases."""
    if not aliases:
        return {}
    rows = (await db.execute(select(Tool.alias, Tool.name).where(Tool.alias.in_(aliases)))).all()
    return {str(alias): str(name) for alias, name in rows}


async def _creator_names(db: AsyncSession, user_ids: set[int]) -> dict[int, str]:
    """Map ``user_id → display name`` (``full_name`` else ``username``)."""
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(User.id, User.full_name, User.username).where(User.id.in_(user_ids))
        )
    ).all()
    return {int(uid): str(full or username) for uid, full, username in rows}


async def _version_strings(db: AsyncSession, version_ids: set[int]) -> dict[int, str]:
    """Map ``tool_version.id → version string`` for the given ids."""
    if not version_ids:
        return {}
    rows = (
        await db.execute(
            select(ToolVersion.id, ToolVersion.version).where(ToolVersion.id.in_(version_ids))
        )
    ).all()
    return {int(vid): str(version) for vid, version in rows}


async def _enrich_cohorts(
    db: AsyncSession,
    service: CohortService,
    cohorts: list[JobCohort],
    *,
    actor_user_id: int,
) -> list[CohortResponse]:
    """Attach the catalogue display fields: tool label, creator, run count.

    ``member_count`` is the cohort's live membership *for this caller*
    (ACL-intersected, D7) — resolved per cohort, the same query the detail
    page's "N member run(s)" line uses, so the two never disagree.
    """
    tool_names = await _tool_names(db, {c.tool_alias for c in cohorts})
    creator_names = await _creator_names(db, {c.created_by_user_id for c in cohorts})
    version_strings = await _version_strings(
        db, {vid for c in cohorts for vid in (c.tolerated_tool_version_ids or [])}
    )
    out: list[CohortResponse] = []
    for c in cohorts:
        resolved = await service.resolve_members(
            db, actor_user_id=actor_user_id, cohort_id=c.id, stamp=False
        )
        # Resolve the tolerated version *ids* to their version strings,
        # newest first, for display (the raw ids are meaningless to a user).
        versions = [
            version_strings[vid]
            for vid in (c.tolerated_tool_version_ids or [])
            if vid in version_strings
        ]
        versions.sort(key=_version_sort_key, reverse=True)
        out.append(
            _to_cohort(
                c,
                tool_name=tool_names.get(c.tool_alias, ""),
                created_by_name=creator_names.get(c.created_by_user_id, ""),
                member_count=len(resolved.job_ids),
                tolerated_versions=versions,
            )
        )
    return out


@router.get("/cohorts", response_model=list[CohortResponse])
async def list_cohorts(
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    include_archived: bool = False,
) -> list[CohortResponse]:
    """List every cohort workspace-wide (open read); archived hidden unless asked."""
    cohorts = await service.list_all(
        db, actor_user_id=current_user.id, include_archived=include_archived
    )
    return await _enrich_cohorts(db, service, cohorts, actor_user_id=current_user.id)


@router.post("/cohorts", response_model=CohortResponse, status_code=status.HTTP_201_CREATED)
async def create_cohort(
    body: CohortCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CohortResponse:
    """Create an ``active`` workspace-level cohort (requires ``cohorts.curate``)."""
    # ``hasattr`` because not every filter exists on every request body:
    # a cohort cannot commission a study before it exists, so
    # ``auto_include_study_ids`` is on the update schema only.
    filters = {
        f: getattr(body, f)
        for f in _FILTER_FIELDS
        if hasattr(body, f) and getattr(body, f) is not None
    }
    cohort = await service.create_cohort(
        db,
        actor_user_id=current_user.id,
        name=body.name,
        tool_alias=body.tool_alias,
        tolerated_tool_version_ids=body.tolerated_tool_version_ids,
        tolerated_version_bands=body.tolerated_version_bands,
        description=body.description,
        auto_grow=body.auto_grow,
        member_job_ids=body.member_job_ids,
        excluded_job_ids=body.excluded_job_ids,
        **filters,
    )
    return _to_cohort(cohort)


@router.get("/cohorts/tools/{tool_alias}/build-meta", response_model=CohortBuildMetaResponse)
async def cohort_build_meta(
    tool_alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CohortBuildMetaResponse:
    """Version bands (major.minor) + numeric inputs for the builder (``cohorts.curate``)."""
    meta = await service.tool_build_meta(db, actor_user_id=current_user.id, tool_alias=tool_alias)
    return CohortBuildMetaResponse(
        version_bands=[
            CohortVersionBand(
                major_minor=b.major_minor,
                latest_version=b.latest_version,
                version_ids=b.version_ids,
            )
            for b in meta.version_bands
        ],
        launchers=[CohortFacet(id=f.id, name=f.name) for f in meta.launchers],
        parametric_studies=[CohortFacet(id=f.id, name=f.name) for f in meta.parametric_studies],
        optimisation_studies=[CohortFacet(id=f.id, name=f.name) for f in meta.optimisation_studies],
        numeric_inputs=[
            CohortNumericInput(name=n.name, label=n.label) for n in meta.numeric_inputs
        ],
    )


@router.post("/cohorts/preview", response_model=CohortPreviewResponse)
async def preview_cohort(
    body: CohortPreviewRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CohortPreviewResponse:
    """Preview the runs a query would gather, without saving (``cohorts.curate``)."""
    # ``hasattr`` because not every filter exists on every request body:
    # a cohort cannot commission a study before it exists, so
    # ``auto_include_study_ids`` is on the update schema only.
    filters = {
        f: getattr(body, f)
        for f in _FILTER_FIELDS
        if hasattr(body, f) and getattr(body, f) is not None
    }
    preview = await service.preview_candidates(
        db,
        actor_user_id=current_user.id,
        tool_alias=body.tool_alias,
        tolerated_tool_version_ids=body.tolerated_tool_version_ids,
        tolerated_version_bands=body.tolerated_version_bands,
        **filters,
    )
    return CohortPreviewResponse(
        rows=[
            CohortCandidate(
                job_id=r.job_id,
                project_id=r.project_id,
                source=r.source,
                source_name=r.source_name,
                created_by_user_id=r.created_by_user_id,
                tool_version_id=r.tool_version_id,
                completed_at=r.completed_at,
                inputs=r.inputs,
            )
            for r in preview.rows
        ],
        acl_hidden=preview.acl_hidden,
    )


@router.get("/cohorts/{cohort_id}", response_model=CohortResponse)
async def get_cohort(
    cohort_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CohortResponse:
    """Fetch a cohort (open read — any authenticated caller)."""
    cohort = await service.get(db, actor_user_id=current_user.id, cohort_id=cohort_id)
    enriched = await _enrich_cohorts(db, service, [cohort], actor_user_id=current_user.id)
    return enriched[0]


@router.patch("/cohorts/{cohort_id}", response_model=CohortResponse)
async def update_cohort(
    cohort_id: int,
    body: CohortUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CohortResponse:
    """Edit an ``active`` cohort's filters / metadata (requires ``cohorts.curate``).

    Filters use proper PATCH semantics: a key **absent** from the body is left
    alone, a key present and ``null`` is **cleared**. Selecting on
    ``is not None`` conflated the two and made a filter unremovable once set.
    """
    filters = {f: getattr(body, f) for f in _FILTER_FIELDS if f in body.model_fields_set}
    cohort = await service.update_cohort(
        db,
        actor_user_id=current_user.id,
        cohort_id=cohort_id,
        name=body.name,
        description=body.description,
        tolerated_tool_version_ids=body.tolerated_tool_version_ids,
        tolerated_version_bands=body.tolerated_version_bands,
        auto_grow=body.auto_grow,
        member_job_ids=body.member_job_ids,
        excluded_job_ids=body.excluded_job_ids,
        **filters,
    )
    return _to_cohort(cohort)


@router.post("/cohorts/{cohort_id}/archive", response_model=CohortResponse)
async def archive_cohort(
    cohort_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CohortResponse:
    """Archive a cohort — hide from default lists, never delete (D20); ``cohorts.curate``."""
    cohort = await service.archive_cohort(db, actor_user_id=current_user.id, cohort_id=cohort_id)
    return _to_cohort(cohort)


def _columns_for(declared: list[OutputColumn], present: set[str]) -> list[CohortResultColumn]:
    """Descriptor columns present in the rows, then undeclared extras (bare).

    Keeps the descriptor order for declared columns the data actually uses,
    then appends any key present in the rows but absent from the descriptor
    (an undeclared scalar / input) as a bare label with no unit.
    """
    cols = [
        CohortResultColumn(key=c.name, label=c.label, unit=c.unit)
        for c in declared
        if c.name in present
    ]
    declared_names = {c.name for c in declared}
    cols += [
        CohortResultColumn(key=key, label=key, unit="") for key in sorted(present - declared_names)
    ]
    return cols


async def _members(
    db: AsyncSession, service: CohortService, actor_user_id: int, cohort_id: int
) -> CohortMembersResponse:
    resolved = await service.resolve_members(db, actor_user_id=actor_user_id, cohort_id=cohort_id)
    return CohortMembersResponse(
        cohort_id=cohort_id,
        job_ids=resolved.job_ids,
        acl_hidden=resolved.acl_hidden,
        last_resolved_at=resolved.last_resolved_at,
    )


@router.get("/cohorts/{cohort_id}/members", response_model=CohortMembersResponse)
async def list_members(
    cohort_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CohortMembersResponse:
    """The cohort's live membership for the caller (open read)."""
    return await _members(db, service, current_user.id, cohort_id)


@router.post("/cohorts/{cohort_id}/resolve", response_model=CohortMembersResponse)
async def resolve_cohort(
    cohort_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CohortMembersResponse:
    """Force a membership re-resolve now (refresh the freshness stamp)."""
    return await _members(db, service, current_user.id, cohort_id)


@router.get("/cohorts/{cohort_id}/results", response_model=CohortResultsResponse)
async def cohort_results(
    cohort_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CohortResultsResponse:
    """Lazy-populate then return the cohort's visible member results (§3.3)."""
    written = await service.populate_study_results(
        db, actor_user_id=current_user.id, cohort_id=cohort_id
    )
    cohort = await service.get(db, actor_user_id=current_user.id, cohort_id=cohort_id)
    resolved = await service.resolve_members(
        db, actor_user_id=current_user.id, cohort_id=cohort_id, stamp=False
    )
    rows: list[CohortResultRow] = []
    present_keys: set[str] = set()
    present_input_keys: set[str] = set()
    if resolved.job_ids:
        # Each member's input values, so the matrix can show the inputs
        # alongside the response scalars.
        inputs_by_job = {
            int(jid): dict(inputs or {})
            for jid, inputs in (
                await db.execute(select(Job.id, Job.inputs).where(Job.id.in_(resolved.job_ids)))
            ).all()
        }
        result = await db.execute(
            select(StudyResult.job_id, StudyResult.scalars).where(
                StudyResult.source_kind == "cohort",
                StudyResult.source_id == cohort_id,
                StudyResult.job_id.in_(resolved.job_ids),
            )
        )
        for job_id, scalars in result.all():
            # ``populate`` already re-keys stored rows to bare names; this
            # second pass is belt-and-braces so the matrix is clean even
            # for a row that slipped through (e.g. a concurrent write).
            normalised = _normalise_cohort_scalars(dict(scalars or {}), cohort.tool_alias)
            present_keys.update(normalised)
            job_inputs = inputs_by_job.get(int(job_id), {})
            present_input_keys.update(job_inputs)
            rows.append(CohortResultRow(job_id=int(job_id), inputs=job_inputs, scalars=normalised))
    # Input + response columns in descriptor order (label + unit), each
    # restricted to the keys the rows actually carry, then any extra
    # (undeclared) keys appended bare.
    input_columns = _columns_for(
        await service.tool_input_columns(db, tool_alias=cohort.tool_alias), present_input_keys
    )
    columns = _columns_for(
        await service.tool_output_columns(db, tool_alias=cohort.tool_alias), present_keys
    )
    return CohortResultsResponse(
        cohort_id=cohort_id,
        written=written,
        acl_hidden=resolved.acl_hidden,
        rows=rows,
        input_columns=input_columns,
        columns=columns,
    )


@router.post(
    "/cohorts/{cohort_id}/coverage",
    response_model=CoverageReportResponse,
    dependencies=[Depends(_require_coverage_feature)],
)
async def cohort_coverage(
    cohort_id: int,
    payload: CohortCoverageRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CoverageReportResponse:
    """Answer "is this cohort fit to learn from?" (ADR-0020 §12 — read-only).

    The response-agnostic design pass, so it answers *before* anything is
    committed: no response chosen, no GP fitted. The declared envelope in
    ``payload`` overrides the observed spread the engine starts from —
    narrowing it is the expected edit.

    Open to anyone who can see the cohort. Spending compute on the runs it
    suggests is a separate right, gated where that happens (§11.5), not here.
    """
    overrides = _coverage_overrides(payload.envelope)
    report = await service.coverage_preview(
        db,
        actor_user_id=current_user.id,
        cohort_id=cohort_id,
        overrides=overrides or None,
    )
    return _coverage_response(report)


def _coverage_response(report: Any) -> CoverageReportResponse:
    """Marshal a :class:`CoverageReport` for the wire.

    Flattens the tuple-keyed correlation matrix to ``"a|b"`` — JSON cannot
    express a tuple key, and distorting the in-memory type for the sake of
    transport would be the wrong trade.
    """
    design = report.design
    diagnostics = design.diagnostics
    correlations = (
        None
        if diagnostics.factor_correlations is None
        else {f"{a}|{b}": v for (a, b), v in sorted(diagnostics.factor_correlations.items())}
    )
    return CoverageReportResponse(
        verdict=report.verdict,
        message=design.message,
        criterion=design.criterion,
        coverage_score=design.coverage_score,
        efficiency_score=design.efficiency_score,
        discriminating=design.discriminating,
        covering_radius=design.covering_radius,
        reference_radius=design.reference_radius,
        n_candidates=design.n_candidates,
        source_kind=report.source_kind,
        computed_at=report.computed_at,
        envelope=[
            CoverageEnvelopeFactor(
                name=f.name,
                label=f.display_name,
                kind=f.kind,
                scale=f.scale,
                lo=f.lo,
                hi=f.hi,
                levels=list(f.levels) if f.levels is not None else None,
                pinned_value=f.pinned_value,
                declared_levels=list(f.declared_levels),
                declared_lo=f.declared_lo,
                declared_hi=f.declared_hi,
                precision=f.precision,
                varying_kind=f.varying_kind,
                actionable=f.actionable,
            )
            for f in report.envelope.factors
        ],
        per_factor=[
            CoverageFactorRow(
                name=f.name,
                label=f.display_name,
                kind=f.kind,
                measure=f.measure,
                scale=f.scale,
                value=f.value,
                verdict=f.verdict,
                missing_values=list(f.missing_values),
                actionable=f.actionable,
                simulated_lo=f.simulated_lo,
                simulated_hi=f.simulated_hi,
                declared_lo=f.declared_lo,
                declared_hi=f.declared_hi,
                declared_value=f.declared_value,
                declared_verdict=f.declared_verdict,
                precision=report.envelope.by_name(f.name).precision,
            )
            for f in design.per_factor
        ],
        per_pair=[
            CoveragePairRow(
                a=p.a,
                b=p.b,
                a_label=p.a_label,
                b_label=p.b_label,
                n_points=p.n_points,
                coverage_score=p.coverage_score,
                covering_radius=p.covering_radius,
                reference_radius=p.reference_radius,
                verdict=p.verdict,
                spread_score=p.spread_score,
                spread_verdict=p.spread_verdict,
                discriminating=p.discriminating,
                detail=p.detail,
            )
            for p in design.per_pair
        ],
        n_pairs_examined=design.n_pairs_examined,
        min_points_per_pair=design.min_points_per_pair,
        min_discriminating_pair_runs=design.min_discriminating_pair_runs,
        density_discriminating=design.density_discriminating,
        min_discriminating_runs_per_input=design.min_discriminating_runs_per_input,
        sampling_modes=list(design.sampling_modes),
        n_pairs_total=design.n_pairs_total,
        per_response=[
            CoverageResponseRow(
                response_name=r.response_name,
                response_kind=r.response_kind,
                surrogate_method=r.surrogate_method,
                criterion=r.criterion,
                verdict=r.verdict,
                message=r.message,
                coverage_score=r.coverage_score,
                suggested_points=[dict(p) for p in r.suggested_points],
                edge_sigma_threshold=r.edge_sigma_threshold,
                available_statuses=list(r.available_statuses),
                ard_relevance=r.ard_relevance,
            )
            for r in report.per_response
        ],
        # `report.warnings` already *contains* `design.warnings` — `build_report`
        # copies them and appends the source framing. Concatenating both lists
        # rendered every warning twice, which a two-factor fixture with one
        # warning never showed.
        warnings=[
            CoverageWarningResponse(
                code=w.code,
                message=w.message,
                factor=w.factor,
                factor_label=w.factor_label,
            )
            for w in report.warnings
        ],
        diagnostics=CoverageDiagnosticsResponse(
            n_points=diagnostics.n_points,
            n_excluded_invalid=diagnostics.n_excluded_invalid,
            n_excluded_filtered=diagnostics.n_excluded_filtered,
            n_outside_envelope=diagnostics.n_outside_envelope,
            n_unmeasurable=diagnostics.n_unmeasurable,
            n_near_duplicates=diagnostics.n_near_duplicates,
            max_abs_correlation=diagnostics.max_abs_correlation,
            confounded=diagnostics.confounded,
            condition_number=diagnostics.condition_number,
            level_counts=diagnostics.level_counts,
            underpopulated_levels=[list(t) for t in diagnostics.underpopulated_levels],
            grouping_mode=diagnostics.grouping_mode,
            factor_correlations=correlations,
        ),
        points=[dict(row) for row in report.design_points],
    )


@router.post(
    "/cohorts/{cohort_id}/coverage/plan",
    response_model=CoveragePlanResponse,
    dependencies=[Depends(_require_coverage_feature)],
)
async def cohort_coverage_plan(
    cohort_id: int,
    payload: CohortCoveragePlanRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CoveragePlanResponse:
    """What each batch size would buy this cohort's coverage (§11.5).

    Read-only and open, like the report itself: pricing a batch spends
    nothing. Committing one is :func:`cohort_coverage_create_study`.
    """
    overrides = _coverage_overrides(payload.envelope)
    measure = (
        _coverage_overrides(payload.measure_envelope)
        if payload.measure_envelope is not None
        else None
    )
    plan = await service.coverage_suggestion_plan(
        db,
        actor_user_id=current_user.id,
        cohort_id=cohort_id,
        sizes=payload.sizes,
        overrides=overrides or None,
        measure_overrides=measure,
    )
    report = await service.coverage_preview(
        db, actor_user_id=current_user.id, cohort_id=cohort_id, overrides=overrides or None
    )
    return CoveragePlanResponse(
        options=[
            CoveragePlanOption(
                n=o.n,
                verdict=o.verdict,
                coverage_score=o.coverage_score,
                covering_radius=o.covering_radius,
                gap_after=o.gap_after,
                reaches_good=o.reaches_good,
                levels_short=o.levels_short,
            )
            for o in plan.options
        ],
        points=[dict(p) for p in plan.points],
        # Resolved here, from the envelope the engine already labelled, so the
        # table can show labels without the page re-deriving them.
        factor_labels={f.name: f.display_name for f in report.envelope.factors},
        current_verdict=plan.current_verdict,
        current_score=plan.current_score,
        current_gap=plan.current_gap,
        current_levels_short=plan.current_levels_short,
        already_good=plan.already_good,
        measured_axes=plan.measured_axes,
        warnings=[
            CoverageWarningResponse(
                code=w.code, message=w.message, factor=w.factor, factor_label=w.factor_label
            )
            for w in plan.warnings
        ],
    )


@router.post(
    "/cohorts/{cohort_id}/coverage/suggest",
    response_model=CoverageSuggestionResponse,
    dependencies=[Depends(_require_coverage_feature)],
)
async def cohort_coverage_suggest(
    cohort_id: int,
    payload: CohortCoverageSuggestRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CoverageSuggestionResponse:
    """Runs that would most improve this cohort's coverage (ADR-0020 §11.5).

    **Read-only and open** to anyone who can see the cohort: planning a batch
    spends nothing. Committing it is a different right — see
    :func:`cohort_coverage_create_study`.
    """
    batch = await service.coverage_suggest(
        db,
        actor_user_id=current_user.id,
        cohort_id=cohort_id,
        n=payload.n,
        overrides=_coverage_overrides(payload.envelope) or None,
    )
    return CoverageSuggestionResponse(
        response_name=batch.response_name,
        points=[dict(p) for p in batch.points],
        warnings=[
            CoverageWarningResponse(code=w.code, message=w.message, factor=w.factor)
            for w in batch.warnings
        ],
    )


@router.post(
    "/cohorts/{cohort_id}/coverage/study",
    response_model=StudyResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_coverage_feature)],
)
async def cohort_coverage_create_study(
    cohort_id: int,
    payload: CohortCoverageStudyRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CohortService, Depends(get_cohort_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> StudyResponse:
    """Commit the suggested runs as a governed parametric study (§11.5).

    The batch lands as an auditable **draft** inside the platform — not a CSV
    emailed around — so somebody reviews it before any solver time is spent.
    Gated on the study-create permission, unlike the analysis that produced it.
    """
    study = await service.create_study_from_coverage(
        db,
        actor_user_id=current_user.id,
        cohort_id=cohort_id,
        project_id=payload.project_id,
        name=payload.name,
        n=payload.n,
        overrides=_coverage_overrides(payload.envelope) or None,
    )
    return StudyResponse.model_validate(study, from_attributes=True)


def _coverage_overrides(rows: Any) -> dict[str, Any]:
    """Envelope-form edits as the engine's ``FactorOverride`` mapping."""
    from matofx.studies.coverage import FactorOverride  # noqa: PLC0415

    return {
        row.name: FactorOverride(
            lo=row.lo,
            hi=row.hi,
            scale=row.scale,
            levels=tuple(row.levels) if row.levels is not None else None,
            hold=row.hold,
        )
        for row in rows
    }
