"""Tools router — list installed tools + fetch full descriptor for form rendering.

GET  /api/tools                                  → list visible tools (filters by state-visibility).
GET  /api/tools/{alias}                          → full descriptor payload the UI form renderer
                                                   consumes to draw the input fields.
GET  /api/tools/{alias}/presets                  → preset bundle for the workspace's
                                                   "Use a preset" popover.
POST /api/tools/{alias}/job-search               → find completed jobs of the tool whose
                                                   inputs match the supplied filters
                                                   (powers the /job-search page).
GET  /api/tools/{alias}/distinct-input-values    → distinct values previously used in
                                                   ``Job.inputs[<field>]`` across visible
                                                   runs (file-input dropdown).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import (
    get_current_user,
    get_db,
    get_job_service,
    get_results_service,
    get_tool_service,
)
from matofx.api.routers.jobs import hydrate_visible_job_responses
from matofx.api.schemas import (
    DistinctInputValuesResponse,
    FileInputHistoryEntry,
    FileInputHistoryResponse,
    FileOutputCandidate,
    FileOutputCandidatesResponse,
    JobSearchRequest,
    JobSearchSimilarRequest,
    JobSearchSimilarResponse,
    PresetResponse,
    SimilarJobResponse,
    ToolDetailResponse,
    ToolSummaryResponse,
    ToolVersionOption,
    VisibleJobResponse,
)
from matofx.hierarchy.service import JobService
from matofx.studies.results_service import ResultsService
from matofx.tools.descriptor import tool_descriptions
from matofx.tools.orm import ToolVersion
from matofx.tools.service import ToolService
from matofx.users.orm import User

router = APIRouter()


_NUMERIC_INPUT_TYPES: frozenset[str] = frozenset({"integer", "float"})
"""Descriptor input ``type`` values that get the ± tolerance treatment.

Mirrored against ``InputFieldType`` in :mod:`matofx.tools.descriptor`
(``"integer"`` and ``"float"``). Any other type falls into the
exact-equality bucket on :meth:`JobService.search_by_inputs`.
"""


def _split_filters_by_descriptor(
    descriptor: dict[str, Any], filters: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, float]]:
    """Bucket the user's filter dict into equality vs numeric-range filters.

    Reads each input's declared ``type`` from the tool descriptor and
    routes numeric (``integer`` / ``float``) filters into the
    range-matching bucket; everything else (``select`` / ``multiselect``
    / ``string`` / ``boolean`` / ``file``) lands in the equality bucket.

    Empty / ``None`` / empty-list / empty-string filter values are
    skipped — they mean "no constraint on this field" per the legacy
    Job Search shape. Filters referencing a field absent from the
    descriptor are also dropped, defensively (a stale UI form against
    a re-installed tool shouldn't 400 the whole request).
    """
    inputs_meta: dict[str, dict[str, Any]] = {
        str(inp.get("name") or ""): inp
        for inp in (descriptor.get("inputs") or [])
        if inp.get("name")
    }

    equality: dict[str, Any] = {}
    numeric: dict[str, float] = {}
    for field, value in filters.items():
        meta = inputs_meta.get(field)
        if meta is None:
            continue
        if value in (None, "", []):
            continue
        if str(meta.get("type") or "") in _NUMERIC_INPUT_TYPES:
            try:
                numeric[field] = float(value)
            except (TypeError, ValueError):
                # Numeric field with a non-numeric filter value — drop
                # rather than 400; the UI shouldn't ever send this but
                # we'd rather degrade gracefully than block the search.
                continue
        else:
            equality[field] = value
    return equality, numeric


@router.get("", response_model=list[ToolSummaryResponse])
async def list_tools(
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[ToolSummaryResponse]:
    """List every tool the actor can see (filters by state-visibility)."""
    tools = await tool_service.list_visible(db, actor_user_id=current_user.id)
    summaries: list[ToolSummaryResponse] = []
    for t in tools:
        short, long = tool_descriptions(t.descriptor if isinstance(t.descriptor, dict) else {})
        summaries.append(
            ToolSummaryResponse(
                alias=t.alias,
                name=t.name,
                version=t.version,
                state=t.state,
                short_description=short,
                long_description=long,
                teams=list(t.descriptor.get("teams", []) or []),
                icon_filename=(
                    str(t.descriptor.get("icon_filename"))
                    if t.descriptor.get("icon_filename")
                    else None
                ),
            )
        )
    return summaries


@router.get("/file-output-candidates", response_model=FileOutputCandidatesResponse)
async def list_file_output_candidates(
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    accepted_file_types: Annotated[
        list[str] | None,
        Query(
            description=(
                "Allowed file extensions (e.g. '.vtu') of the consuming input. "
                "Repeat the param per extension; omit to accept any file output."
            ),
        ),
    ] = None,
    expected_field_array: Annotated[
        str | None,
        Query(
            description=(
                "The nodal field array the consuming input needs (e.g. "
                "'temperature'). When set, only field outputs carrying that "
                "exact array are returned — so a temperature-field input never "
                "lists a failure-index field. Omit for no field-level filter."
            ),
        ),
    ] = None,
) -> FileOutputCandidatesResponse:
    """File *outputs* of visible completed jobs, for the cross-tool picker.

    Registered before ``/{alias}`` so the literal path wins. Lets a
    standalone run reuse a *different* tool's result file as a file input
    (e.g. Bracket Stress consuming Thermal Soak's temperature field). The
    ``accepted_file_types`` filter is the consuming input's allowed
    extensions — a convenience filter, not a security boundary (ACL is the
    visible-jobs gate). Complements ``/{alias}/file-input-history`` (same-tool
    *input* reuse).
    """
    entries = await job_service.list_file_output_candidates(
        db,
        actor_user_id=current_user.id,
        accepted_file_types=accepted_file_types,
        expected_field_array=expected_field_array,
    )
    return FileOutputCandidatesResponse(
        entries=[FileOutputCandidate(**e) for e in entries],
    )


@router.get("/{alias}", response_model=ToolDetailResponse)
async def get_tool(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ToolDetailResponse:
    """Return the full descriptor payload for the named tool."""
    tool = await tool_service.get(db, actor_user_id=current_user.id, alias=alias)
    # Resolve the snapshot row matching the tool's released version
    # string so the UI can call ADR-0011 §7 V&V endpoints (badge,
    # envelope-check) without an extra alias→id round trip.
    tool_version_id = (
        await db.execute(
            select(ToolVersion.id)
            .where(ToolVersion.alias == tool.alias, ToolVersion.version == tool.version)
            .limit(1)
        )
    ).scalar_one_or_none()
    return ToolDetailResponse(
        alias=tool.alias,
        name=tool.name,
        version=tool.version,
        state=tool.state,
        descriptor=tool.descriptor,
        tool_version_id=tool_version_id,
    )


@router.get("/{alias}/versions", response_model=list[ToolVersionOption])
async def list_tool_version_options(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[ToolVersionOption]:
    """Every version of a visible tool, newest first, for a version picker.

    A non-admin counterpart to the admin change-log — enough to let a
    caller (e.g. the V&V authoring flow) record against a *specific*
    version rather than only the current release. ACL is the tool's own:
    ``tool_service.get`` raises if the caller can't see the tool.
    """
    tool = await tool_service.get(db, actor_user_id=current_user.id, alias=alias)
    rows = (
        await db.execute(
            select(ToolVersion.id, ToolVersion.version)
            .where(ToolVersion.alias == alias)
            .order_by(ToolVersion.created_at.desc())
        )
    ).all()
    return [
        ToolVersionOption(
            tool_version_id=int(vid),
            version=str(ver),
            is_current=str(ver) == tool.version,
        )
        for vid, ver in rows
    ]


def _confined_bundle_file(bundle_path: str, filename: str) -> Path | None:
    """Resolve ``filename`` under ``bundle_path`` and confine it to the bundle.

    The descriptor now rejects traversal in filename fields at validation
    time, but this is the serving-side backstop: resolve the join and
    return the path only if it stays inside the (resolved) bundle root
    and is a regular file. Returns ``None`` on any escape / miss so the
    caller raises a flat 404 without disclosing why.
    """
    bundle_root = Path(bundle_path).resolve()
    candidate = (bundle_root / filename).resolve()
    try:
        candidate.relative_to(bundle_root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


@router.get("/{alias}/icon", response_class=FileResponse)
async def get_tool_icon(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FileResponse:
    """Serve the tool's PNG icon from its bundle directory.

    Returns 404 when the descriptor has no ``icon_filename`` set or
    when the file is missing from disk — the home page falls back to
    the default wrench glyph on either signal. ACL piggy-backs on
    :meth:`ToolService.get`, so a user who can't see the tool can't
    see its icon.
    """
    tool = await tool_service.get(db, actor_user_id=current_user.id, alias=alias)
    descriptor = tool.descriptor if isinstance(tool.descriptor, dict) else {}
    icon_filename = descriptor.get("icon_filename")
    if not icon_filename:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This tool has no icon uploaded.",
        )
    icon_path = _confined_bundle_file(str(tool.bundle_path), str(icon_filename))
    if icon_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The icon file is missing from the tool bundle.",
        )
    return FileResponse(path=icon_path, media_type="image/png", filename=icon_path.name)


_GUIDE_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


@router.get("/{alias}/guide-images/{index}", response_class=FileResponse)
async def get_tool_guide_image(
    alias: str,
    index: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FileResponse:
    """Serve one of the tool's static guide images from its bundle directory.

    Guides are addressed by their position in the descriptor's
    ``input_guides`` list (0-based). Returns 404 when the index is out of
    range or the file is missing from disk. ACL piggy-backs on
    :meth:`ToolService.get`, like the icon endpoint.
    """
    tool = await tool_service.get(db, actor_user_id=current_user.id, alias=alias)
    descriptor = tool.descriptor if isinstance(tool.descriptor, dict) else {}
    guides = descriptor.get("input_guides") or []
    if not isinstance(guides, list) or index < 0 or index >= len(guides):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This tool has no guide image at that position.",
        )
    entry = guides[index]
    filename = str(entry.get("filename") or "") if isinstance(entry, dict) else ""
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That guide image has no file.",
        )
    path = _confined_bundle_file(str(tool.bundle_path), filename)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The guide image file is missing from the tool bundle.",
        )
    media_type = _GUIDE_MEDIA_TYPES.get(path.suffix.lstrip(".").lower(), "application/octet-stream")
    return FileResponse(path=path, media_type=media_type, filename=path.name)


@router.get("/{alias}/presets", response_model=list[PresetResponse])
async def list_tool_presets(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[PresetResponse]:
    """List presets bundled with the tool (workspace's "Use a preset" popover).

    Reads ``presets.toml`` (v2 shape) or legacy
    ``<alias>_presets.json`` from the tool's bundle. ACL piggy-backs
    on :meth:`ToolService.list_presets` — a user who can't see the
    tool can't see its presets. Returns ``[]`` for tools with no
    preset file (the workspace surfaces "no presets available" rather
    than crashing).
    """
    presets = await tool_service.list_presets(db, actor_user_id=current_user.id, alias=alias)
    return [
        PresetResponse(name=p.name, description=p.description, values=p.values) for p in presets
    ]


@router.post("/{alias}/job-search", response_model=list[VisibleJobResponse])
async def search_jobs_by_inputs(
    alias: str,
    body: JobSearchRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[VisibleJobResponse]:
    """Find completed runs of ``alias`` whose inputs match the supplied filters.

    Powers the ``/job-search`` page (port of legacy
    ``functions/job_search.py``). The API layer is the type-aware
    boundary: it loads the tool's descriptor, splits the flat
    ``filters`` dict into equality vs numeric-range buckets based on
    each field's declared type, and forwards both to
    :meth:`JobService.search_by_inputs`.

    The hydrated response carries friendly project / scenario / tool
    names so the page renders rows directly without secondary lookups
    — same shape as :func:`list_my_visible_jobs` so the page can
    re-use the existing render path.
    """
    tool = await tool_service.get(db, actor_user_id=current_user.id, alias=alias)
    equality_filters, numeric_filters = _split_filters_by_descriptor(
        tool.descriptor or {}, body.filters
    )
    jobs = await job_service.search_by_inputs(
        db,
        actor_user_id=current_user.id,
        tool_alias=alias,
        equality_filters=equality_filters,
        numeric_filters=numeric_filters,
        numeric_tolerance_pct=body.numeric_tolerance_pct,
    )
    return await hydrate_visible_job_responses(db, jobs)


def _numeric_descriptor_fields(descriptor: dict[str, Any]) -> set[str]:
    """Names of the descriptor inputs typed ``integer`` / ``float``.

    The find-similar ranker treats these as distance axes and every
    other input type as a categorical exact-match gate — same split the
    boolean Job Search uses via :func:`_split_filters_by_descriptor`.
    """
    return {
        str(inp.get("name") or "")
        for inp in (descriptor.get("inputs") or [])
        if inp.get("name") and str(inp.get("type") or "") in _NUMERIC_INPUT_TYPES
    }


@router.post("/{alias}/job-search/similar", response_model=JobSearchSimilarResponse)
async def find_similar_jobs(
    alias: str,
    body: JobSearchSimilarRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    results_service: Annotated[ResultsService, Depends(get_results_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> JobSearchSimilarResponse:
    """Rank completed runs of ``alias`` by closeness to a reference point.

    The "find similar runs" extension to Job Search (§16.5; powers the
    page's *Similar* mode). The reference point is resolved here, at the
    type-aware boundary:

    1. ``reference_job_id`` (optional) loads that run's inputs as the
       base — ACL-checked via :meth:`JobService.get`, and rejected with
       400 if it belongs to a different tool. That job is excluded from
       its own results unless ``exclude_job_id`` overrides.
    2. ``reference_inputs`` is overlaid on top, so a typed value wins
       over the seed run's value for the same field.

    Weighting is sensitivity-derived when
    :meth:`ResultsService.latest_sensitivity_weights` finds a usable
    study of the tool (``weighting="sensitivity"``), uniform otherwise
    (``weighting="uniform"``) — the response reports which, the weights
    applied, and a short source label for the UI note. Results re-use
    the shared :func:`hydrate_visible_job_responses` shape so the page
    renders them through the same row path as the boolean search.
    """
    tool = await tool_service.get(db, actor_user_id=current_user.id, alias=alias)
    numeric_fields = _numeric_descriptor_fields(tool.descriptor or {})

    reference: dict[str, Any] = {}
    if body.reference_job_id is not None:
        ref_job = await job_service.get(
            db, job_id=body.reference_job_id, actor_user_id=current_user.id
        )
        if ref_job.tool_alias != alias:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The reference job belongs to a different tool.",
            )
        reference.update(ref_job.inputs or {})
    reference.update(body.reference_inputs or {})

    exclude_job_id = (
        body.exclude_job_id if body.exclude_job_id is not None else body.reference_job_id
    )

    weights_result = await results_service.latest_sensitivity_weights(
        db, actor_user_id=current_user.id, tool_alias=alias
    )
    if weights_result is not None:
        weights, weighting_source = weights_result
        weighting = "sensitivity"
    else:
        weights, weighting_source, weighting = None, None, "uniform"

    ranked = await job_service.find_similar_by_inputs(
        db,
        actor_user_id=current_user.id,
        tool_alias=alias,
        reference_inputs=reference,
        numeric_fields=numeric_fields,
        weights=weights,
        top_n=body.top_n,
        exclude_job_id=exclude_job_id,
    )
    hydrated = await hydrate_visible_job_responses(db, [r.job for r in ranked])
    results = [
        SimilarJobResponse(
            **vjr.model_dump(),
            distance=r.distance,
            closeness_pct=r.closeness_pct,
        )
        for vjr, r in zip(hydrated, ranked, strict=True)
    ]
    return JobSearchSimilarResponse(
        weighting=weighting,
        weights=weights or {},
        weighting_source=weighting_source,
        results=results,
    )


@router.get("/{alias}/distinct-input-values", response_model=DistinctInputValuesResponse)
async def list_distinct_input_values(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    field: Annotated[
        str,
        Query(description="Descriptor input name to enumerate distinct values for."),
    ],
) -> DistinctInputValuesResponse:
    """Distinct values previously submitted for ``Job.inputs[<field>]``.

    Backs the file-input dropdown on the Job Search page: when the
    descriptor declares an input as ``type = "file"``, the page asks
    the server for every distinct path the user has used so they can
    pick from history rather than re-typing.

    The lookup runs through :meth:`ToolService.get` first to enforce
    ACL on the tool itself; the values themselves come from the
    ACL-filtered visible-jobs rollup.
    """
    await tool_service.get(db, actor_user_id=current_user.id, alias=alias)
    values = await job_service.list_distinct_input_values(
        db, actor_user_id=current_user.id, tool_alias=alias, field=field
    )
    return DistinctInputValuesResponse(field=field, values=values)


@router.get("/{alias}/file-input-history", response_model=FileInputHistoryResponse)
async def list_file_input_history(
    alias: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    tool_service: Annotated[ToolService, Depends(get_tool_service)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    field: Annotated[
        str,
        Query(description="Descriptor input name to enumerate file history for."),
    ],
) -> FileInputHistoryResponse:
    """Enriched file-input history — values + source-job metadata.

    Sibling of :func:`list_distinct_input_values`. The Job Search page
    only needs the raw value list (it surfaces them in a dropdown by
    the path itself); the workspace's rich file-input picker on
    ``/my-jobs`` needs the originating job + project + scenario name
    so it can label each option as
    ``"<filename>  ·  Job 0001234 (Project, Scenario)"``. Re-using the
    distinct-values endpoint for that would force every Job Search
    caller through an N+1 lookup it doesn't need, so we have two
    endpoints sharing the same ACL gate.
    """
    await tool_service.get(db, actor_user_id=current_user.id, alias=alias)
    entries = await job_service.list_file_input_history(
        db, actor_user_id=current_user.id, tool_alias=alias, field=field
    )
    return FileInputHistoryResponse(
        field=field,
        entries=[FileInputHistoryEntry(**e) for e in entries],
    )
