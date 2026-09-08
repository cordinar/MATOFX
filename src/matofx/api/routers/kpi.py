"""HTTP API for KPI extraction + per-tool results warehouse (ADR-0012 §7).

Four GET endpoints under ``/api/tools/{alias}/``:

- ``/kpis`` — list KPIs declared by the tool (one row per
  ``(name, label, unit)`` tuple, deduplicated across versions).
- ``/results`` — paginated warehouse query, one row per visible job,
  with pivoted KPI values + a ``hidden_by_acl`` rollup.
- ``/results/timeseries?kpi_name=...`` — trend chart data for one
  KPI in ascending ``extracted_at`` order.
- ``/results/export.csv`` — full filtered set as a streaming
  ``text/csv`` body; supports the unbounded-selection
  ``?job_ids=...`` allow-list from the warehouse page (design
  §8.1 export bar).

Gating (project-wide toggle rule, design §3.3):

- **Feature flag** — a router-wide dependency 404s every route when
  ``kpi_extraction`` is off; the surface is *absent* for a customer
  without the flag.
- **Permission** — composes the existing per-project ACL inside the
  service (``hierarchy.service._visible_project_ids`` equivalent
  reproduced in :mod:`matofx.kpi.service`). There is no new
  ``kpi.view`` permission; the descriptor is the source of truth for
  "what is a KPI" and the per-project ACL is the source of truth for
  "which jobs you can read". Adding a separate KPI permission would
  duplicate ``jobs.view``'s ladder without expressive gain (design
  §15).

The V&V status filter (``?vv_status=...``) is omitted from this
slice — it lands in slice 9 alongside the V&V join. The router
contract is forwards-compatible: slice 9 only adds a query parameter.
"""

from __future__ import annotations

import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import get_current_user, get_db, get_settings
from matofx.api.schemas import (
    KpiCatalogEntryResponse,
    TimeseriesPointResponse,
    TimeseriesResponse,
    ToolKpiCatalogResponse,
    WarehousePageResponse,
    WarehouseRowResponse,
)
from matofx.config import Settings
from matofx.features import Feature, feature_enabled
from matofx.kpi import (
    WarehouseFilters,
    iter_warehouse_csv,
    list_kpis_for_tool,
    list_warehouse_rows,
    timeseries_for_kpi,
)
from matofx.reports.warehouse_report import (
    WarehouseReportContext,
    build_warehouse_report,
)
from matofx.tools.orm import Tool
from matofx.users.orm import User


def _require_kpi_feature(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """404 every route when the KPI feature flag is off (routes *absent*)."""
    if not feature_enabled(settings.features, Feature.KPI_EXTRACTION):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")


router = APIRouter(dependencies=[Depends(_require_kpi_feature)])

_Db = Annotated[AsyncSession, Depends(get_db)]
_Me = Annotated[User, Depends(get_current_user)]


# ---- /kpis ------------------------------------------------------------


@router.get(
    "/tools/{tool_alias}/kpis",
    response_model=ToolKpiCatalogResponse,
)
async def get_tool_kpis(
    tool_alias: str,
    db: _Db,
    current_user: _Me,
    version_id: Annotated[int | None, Query(ge=1)] = None,
) -> ToolKpiCatalogResponse:
    """List the KPIs declared by the tool's released version(s)."""
    del current_user
    entries = await list_kpis_for_tool(db, tool_alias=tool_alias, tool_version_id=version_id)
    return ToolKpiCatalogResponse(
        tool_alias=tool_alias,
        tool_version_id=version_id,
        entries=[
            KpiCatalogEntryResponse(
                kpi_name=e.kpi_name,
                kpi_label=e.kpi_label,
                kpi_unit=e.kpi_unit,
                kpi_category=e.kpi_category,
            )
            for e in entries
        ],
    )


# ---- /results ---------------------------------------------------------


def _parse_job_ids(raw: str | None) -> tuple[int, ...] | None:
    """Parse a comma-separated job_ids list; return ``None`` when unset.

    Empty string is treated as an explicit empty allow-list (zero
    results); any non-integer entry → 400.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "":
        return ()
    try:
        return tuple(int(p) for p in raw.split(","))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="job_ids must be a comma-separated list of integers",
        ) from exc


def _parse_int_list(raw: list[int] | None) -> tuple[int, ...]:
    """Pass-through helper — FastAPI gives us a list when the param repeats."""
    return tuple(raw) if raw else ()


def _parse_kpi_ranges(
    raw: list[str] | None,
) -> tuple[tuple[str, float | None, float | None], ...]:
    """Parse repeated ``kpi_range=name:min:max`` params into value filters.

    ``min`` / ``max`` may be empty (open on that side). An entry with both
    sides empty is dropped (no constraint). Non-numeric bounds → 400.
    """
    if not raw:
        return ()

    def _num(text: str) -> float | None:
        text = text.strip()
        if text == "":
            return None
        try:
            return float(text)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"kpi_range bound {text!r} is not a number",
            ) from exc

    out: list[tuple[str, float | None, float | None]] = []
    for item in raw:
        parts = item.split(":")
        if len(parts) != 3:  # noqa: PLR2004 — name:min:max is a fixed 3-part shape
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="kpi_range must be 'name:min:max' (min/max may be empty)",
            )
        name = parts[0].strip()
        if not name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="kpi_range needs a KPI name",
            )
        vmin, vmax = _num(parts[1]), _num(parts[2])
        if vmin is None and vmax is None:
            continue
        out.append((name, vmin, vmax))
    return tuple(out)


@router.get(
    "/tools/{tool_alias}/results",
    response_model=WarehousePageResponse,
)
async def get_tool_results(
    tool_alias: str,
    db: _Db,
    current_user: _Me,
    version_id: Annotated[int | None, Query(ge=1)] = None,
    project_id: Annotated[list[int] | None, Query()] = None,
    scenario_id: Annotated[list[int] | None, Query()] = None,
    requestor_user_id: Annotated[list[int] | None, Query()] = None,
    completed_from: Annotated[str | None, Query()] = None,
    completed_to: Annotated[str | None, Query()] = None,
    job_ids: Annotated[str | None, Query()] = None,
    kpi_range: Annotated[list[str] | None, Query()] = None,
    vv_status: Annotated[str, Query()] = "any",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> WarehousePageResponse:
    """Paginated warehouse table — one row per visible completed job."""
    if vv_status not in {"any", "verified_only", "verified_validated"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="vv_status must be any / verified_only / verified_validated",
        )
    filters = WarehouseFilters(
        tool_version_id=version_id,
        project_ids=_parse_int_list(project_id),
        scenario_ids=_parse_int_list(scenario_id),
        requestor_user_ids=_parse_int_list(requestor_user_id),
        completed_from=_parse_iso_datetime(completed_from, "completed_from"),
        completed_to=_parse_iso_datetime(completed_to, "completed_to"),
        job_ids=_parse_job_ids(job_ids),
        kpi_ranges=_parse_kpi_ranges(kpi_range),
        vv_status=vv_status,
    )
    page_obj = await list_warehouse_rows(
        db,
        actor_user_id=current_user.id,
        tool_alias=tool_alias,
        filters=filters,
        page=page,
        page_size=page_size,
    )
    return WarehousePageResponse(
        rows=[
            WarehouseRowResponse(
                job_id=r.job_id,
                project_id=r.project_id,
                scenario_id=r.scenario_id,
                parametric_study_id=r.parametric_study_id,
                requestor_user_id=r.requestor_user_id,
                completed_at=r.completed_at,
                tool_version_id=r.tool_version_id,
                kpi_values=r.kpi_values,
                kpi_units=r.kpi_units,
                vv_status=r.vv_status,
                project_name=r.project_name,
                scenario_name=r.scenario_name,
                requestor_name=r.requestor_name,
                tool_version_label=r.tool_version_label,
            )
            for r in page_obj.rows
        ],
        page=page_obj.page,
        page_size=page_obj.page_size,
        total=page_obj.total,
        hidden_by_acl=page_obj.hidden_by_acl,
    )


# ---- /results/timeseries ---------------------------------------------


@router.get(
    "/tools/{tool_alias}/results/timeseries",
    response_model=TimeseriesResponse,
)
async def get_tool_results_timeseries(
    tool_alias: str,
    db: _Db,
    current_user: _Me,
    kpi_name: Annotated[str, Query(min_length=1, max_length=64)],
    version_id: Annotated[int | None, Query(ge=1)] = None,
    project_id: Annotated[list[int] | None, Query()] = None,
    scenario_id: Annotated[list[int] | None, Query()] = None,
    requestor_user_id: Annotated[list[int] | None, Query()] = None,
    completed_from: Annotated[str | None, Query()] = None,
    completed_to: Annotated[str | None, Query()] = None,
    job_ids: Annotated[str | None, Query()] = None,
) -> TimeseriesResponse:
    """Time-series for one KPI, ascending by ``extracted_at``."""
    filters = WarehouseFilters(
        tool_version_id=version_id,
        project_ids=_parse_int_list(project_id),
        scenario_ids=_parse_int_list(scenario_id),
        requestor_user_ids=_parse_int_list(requestor_user_id),
        completed_from=_parse_iso_datetime(completed_from, "completed_from"),
        completed_to=_parse_iso_datetime(completed_to, "completed_to"),
        job_ids=_parse_job_ids(job_ids),
    )
    points = await timeseries_for_kpi(
        db,
        actor_user_id=current_user.id,
        tool_alias=tool_alias,
        kpi_name=kpi_name,
        filters=filters,
    )
    return TimeseriesResponse(
        tool_alias=tool_alias,
        kpi_name=kpi_name,
        points=[
            TimeseriesPointResponse(
                extracted_at=p.extracted_at,
                value_numeric=p.value_numeric,
                job_id=p.job_id,
            )
            for p in points
        ],
    )


# ---- /results/export.csv ---------------------------------------------


@router.get("/tools/{tool_alias}/results/export.csv")
async def get_tool_results_csv(
    tool_alias: str,
    db: _Db,
    current_user: _Me,
    version_id: Annotated[int | None, Query(ge=1)] = None,
    project_id: Annotated[list[int] | None, Query()] = None,
    scenario_id: Annotated[list[int] | None, Query()] = None,
    requestor_user_id: Annotated[list[int] | None, Query()] = None,
    completed_from: Annotated[str | None, Query()] = None,
    completed_to: Annotated[str | None, Query()] = None,
    job_ids: Annotated[str | None, Query()] = None,
    kpi_range: Annotated[list[str] | None, Query()] = None,
    vv_status: Annotated[str, Query()] = "any",
) -> StreamingResponse:
    """Stream the filtered set as ``text/csv`` (capped at 100k rows).

    Honours the same filters as the table (so the export matches what you
    see). When ``job_ids`` is supplied, intersects with the ACL — the
    response silently drops IDs the requester can't see (no info leak
    about invisible rows; design §7).
    """
    if vv_status not in {"any", "verified_only", "verified_validated"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="vv_status must be any / verified_only / verified_validated",
        )
    filters = WarehouseFilters(
        tool_version_id=version_id,
        project_ids=_parse_int_list(project_id),
        scenario_ids=_parse_int_list(scenario_id),
        requestor_user_ids=_parse_int_list(requestor_user_id),
        completed_from=_parse_iso_datetime(completed_from, "completed_from"),
        completed_to=_parse_iso_datetime(completed_to, "completed_to"),
        job_ids=_parse_job_ids(job_ids),
        kpi_ranges=_parse_kpi_ranges(kpi_range),
        vv_status=vv_status,
    )
    catalog = await list_kpis_for_tool(db, tool_alias=tool_alias, tool_version_id=version_id)
    chunks = iter_warehouse_csv(
        db,
        actor_user_id=current_user.id,
        tool_alias=tool_alias,
        filters=filters,
        kpi_catalog=catalog,
    )
    return StreamingResponse(
        chunks,
        media_type="text/csv",
        headers={
            "Content-Disposition": (f'attachment; filename="{tool_alias}_results.csv"'),
        },
    )


# ---- /results/export.pdf (printable HTML for browser-print → PDF) ----


@router.get("/tools/{tool_alias}/results/export.pdf")
async def get_tool_results_pdf(
    tool_alias: str,
    db: _Db,
    current_user: _Me,
    version_id: Annotated[int | None, Query(ge=1)] = None,
    project_id: Annotated[list[int] | None, Query()] = None,
    scenario_id: Annotated[list[int] | None, Query()] = None,
    requestor_user_id: Annotated[list[int] | None, Query()] = None,
    completed_from: Annotated[str | None, Query()] = None,
    completed_to: Annotated[str | None, Query()] = None,
    job_ids: Annotated[str | None, Query()] = None,
    kpi_range: Annotated[list[str] | None, Query()] = None,
    vv_status: Annotated[str, Query()] = "any",
    page_size_cap: Annotated[int, Query(ge=1, le=10000)] = 5000,
) -> HTMLResponse:
    """Self-contained printable HTML — browser-prints to PDF on load.

    Same ADR-0004 pattern as the per-job report: serve an inlined HTML
    document with a print-on-load script, the operator picks "Save as
    PDF" in the print dialog. The endpoint name ``export.pdf`` matches
    the user-facing "Download PDF" action even though the content
    type is ``text/html`` — the browser does the actual PDF conversion.
    Honours the same filters as the table.

    ``page_size_cap`` caps how many rows the report walks (default
    5000 — a reasonable bound for a print-friendly table). Past that
    cap the footer notes the truncation.
    """
    import datetime as _dt  # noqa: PLC0415

    if vv_status not in {"any", "verified_only", "verified_validated"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="vv_status must be any / verified_only / verified_validated",
        )
    filters = WarehouseFilters(
        tool_version_id=version_id,
        project_ids=_parse_int_list(project_id),
        scenario_ids=_parse_int_list(scenario_id),
        requestor_user_ids=_parse_int_list(requestor_user_id),
        completed_from=_parse_iso_datetime(completed_from, "completed_from"),
        completed_to=_parse_iso_datetime(completed_to, "completed_to"),
        job_ids=_parse_job_ids(job_ids),
        kpi_ranges=_parse_kpi_ranges(kpi_range),
        vv_status=vv_status,
    )
    catalog = await list_kpis_for_tool(db, tool_alias=tool_alias, tool_version_id=version_id)
    page = await list_warehouse_rows(
        db,
        actor_user_id=current_user.id,
        tool_alias=tool_alias,
        filters=filters,
        page=1,
        page_size=min(page_size_cap, 200),
    )
    # The list_warehouse_rows page_size is capped at 200 by the
    # service; the PDF endpoint walks pages up to page_size_cap.
    all_rows = list(page.rows)
    fetched = len(all_rows)
    next_page = 2
    while fetched < page.total and fetched < page_size_cap:
        more = await list_warehouse_rows(
            db,
            actor_user_id=current_user.id,
            tool_alias=tool_alias,
            filters=filters,
            page=next_page,
            page_size=200,
        )
        if not more.rows:
            break
        all_rows.extend(more.rows)
        fetched = len(all_rows)
        next_page += 1
    truncated = page.total > page_size_cap

    catalog_dicts = [
        {
            "kpi_name": e.kpi_name,
            "kpi_label": e.kpi_label,
            "kpi_unit": e.kpi_unit,
            "kpi_category": e.kpi_category,
        }
        for e in catalog
    ]
    row_dicts = [
        {
            "job_id": r.job_id,
            "project_id": r.project_id,
            "project_name": r.project_name,
            "scenario_id": r.scenario_id,
            "scenario_name": r.scenario_name,
            "parametric_study_id": r.parametric_study_id,
            "requestor_user_id": r.requestor_user_id,
            "requestor_name": r.requestor_name,
            "completed_at": r.completed_at,
            "tool_version_id": r.tool_version_id,
            "tool_version_label": r.tool_version_label,
            "kpi_values": r.kpi_values,
            "kpi_units": r.kpi_units,
        }
        for r in all_rows
    ]

    filter_summary_parts: list[str] = []
    if version_id is not None:
        filter_summary_parts.append(f"version_id={version_id}")
    if completed_from is not None:
        filter_summary_parts.append(f"completed_from={completed_from}")
    if completed_to is not None:
        filter_summary_parts.append(f"completed_to={completed_to}")
    if job_ids is not None:
        filter_summary_parts.append("job_ids=(selection)")
    filter_summary = ", ".join(filter_summary_parts)

    # Show the tool's human display name in the report title, not its
    # internal alias ([[feedback_use_labels_not_names]]).
    tool_row = (await db.execute(select(Tool).where(Tool.alias == tool_alias))).scalar_one_or_none()
    tool_name = tool_row.name if tool_row is not None else tool_alias

    context = WarehouseReportContext(
        tool_alias=tool_alias,
        tool_name=tool_name,
        generated_at=_dt.datetime.now(tz=_dt.UTC),
        kpi_entries=catalog_dicts,
        rows=row_dicts,
        total=(page_size_cap if truncated else page.total),
        hidden_by_acl=page.hidden_by_acl,
        filter_summary=filter_summary,
        auto_print=True,
    )
    html_body = build_warehouse_report(context)
    return HTMLResponse(content=html_body)


# ---- helpers ----------------------------------------------------------


def _parse_iso_datetime(raw: str | None, field: str) -> datetime.datetime | None:
    """Lenient ISO-8601 parser; 400 on garbage."""
    if raw is None:
        return None
    import datetime as _dt  # noqa: PLC0415

    try:
        return _dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be an ISO-8601 datetime",
        ) from exc
