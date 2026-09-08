"""Per-job HTML report endpoint.

``GET /api/jobs/{job_id}/report`` — render the job's standalone HTML
report (see :mod:`matofx.reports`) and stream it as an attachment.

Auth + project-VIEW + post-approval gating funnel through the same
:meth:`matofx.hierarchy.service.JobService.ensure_outputs_accessible`
chokepoint the rest of the job-outputs surface uses, so a report is
visible to exactly the users who can already see the job's outputs:
the requestor, project team members, super-users, and (for held
jobs) eligible approvers. Per ADR-0004 the endpoint returns 404 for
any state other than ``complete`` — partial reports for in-flight or
failed runs are out of scope for the first ship.

Every successful generation writes one audit row with action
``job.report.generate``.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import (
    get_audit,
    get_current_user,
    get_db,
    get_job_service,
    get_lineage_service,
    get_settings,
    get_summary_service,
)
from matofx.audit import AuditWriter
from matofx.audit.schema import RecordInput
from matofx.config import Settings
from matofx.features import Feature, feature_enabled
from matofx.hierarchy.errors import NotFoundError
from matofx.hierarchy.orm import Job
from matofx.hierarchy.service import JobService
from matofx.kpi import JobKpiValue, list_kpis_for_tool
from matofx.lineage import (
    LineageAccessDeniedError,
    LineageNotFoundError,
    LineageService,
    NodeKind,
    graph_to_svg,
)
from matofx.reports import ReportContext, build_report
from matofx.reports.assets import inline_input_image, inline_logo, is_previewable_image
from matofx.reports.builder import SiblingJob, WorkflowContext
from matofx.reports.outputs import OutputPreviewBundle, collect_output_previews
from matofx.summaries import (
    SummaryService,
    SummaryText,
    collect_job_facts,
    enrichment_context_if_enabled,
)
from matofx.tools.exec_orm import StorageLocation
from matofx.tools.orm import Tool, ToolVersion
from matofx.ui.branding_cache import get_branding
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission
from matofx.workflows.orm import Workflow, WorkflowRun

router = APIRouter()

_AUDIT_ACTION = "job.report.generate"


@router.get("/jobs/{job_id}/report")
async def get_job_report(
    job_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    lineage_service: Annotated[LineageService, Depends(get_lineage_service)],
    audit: Annotated[AuditWriter, Depends(get_audit)],
    summary_service: Annotated[SummaryService, Depends(get_summary_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    include_provenance: Annotated[bool, Query()] = False,
) -> Response:
    """Render the per-job HTML report and stream it as a download.

    Same auth / VIEW gate as the outputs surface. Returns 404 when
    the job exists but is not in the ``complete`` state, deliberately
    collapsing "not finished" and "not allowed" into a single
    indistinguishable response so the endpoint never leaks the
    pipeline state of a job a caller couldn't otherwise see.
    """
    is_admin = await user_has_permission(db, current_user.id, Permission.PROJECTS_SEE_ALL)
    job = await job_service.ensure_outputs_accessible(
        db,
        job_id=job_id,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        actor_is_approver=current_user.is_approver,
        actor_is_admin=is_admin,
    )
    if job.state != "complete":
        # See module docstring — non-``complete`` collapses to 404 so
        # the response shape doesn't disclose progress to a caller
        # who can't (yet) read the outputs.
        raise NotFoundError("job report", job_id)

    # ``Tool.alias`` is a uniquely-indexed column, not the PK — go
    # through ``select`` rather than ``db.get``.
    tool_row = (
        await db.execute(select(Tool).where(Tool.alias == job.tool_alias))
    ).scalar_one_or_none()
    tool_version_row = await db.get(ToolVersion, job.tool_version_id)
    tool_name = tool_row.name if tool_row is not None else job.tool_alias
    tool_version = tool_version_row.version if tool_version_row is not None else "unknown"
    descriptor = dict(tool_version_row.descriptor_snapshot) if tool_version_row is not None else {}
    job_inputs = dict(job.inputs or {})
    input_image_previews = await _collect_input_image_previews(db, job, descriptor)
    output_bundle = await _collect_outputs(db, job, descriptor)
    workflow_context = await _collect_workflow_context(db, job)

    branding = get_branding()
    logo_data_uri = inline_logo(
        icons_dir=settings.branding.icons_dir,
        basename=branding.header_icon_basename,
    )

    # Optional Provenance section (ADR-0010 slice 6). The toggle is
    # off by default to keep report file sizes predictable; when on,
    # the lineage service assembles the graph with the caller's own
    # ACL and the SVG renderer inlines it into the report. A missing
    # ``lineage.view`` permission falls through silently (the report
    # generates without the section); a soft-capped graph also
    # silently drops the SVG (``graph_to_svg`` returns ``None``).
    # Both make the report best-effort rather than an all-or-nothing
    # failure mode.
    provenance_svg: str | None = None
    if include_provenance:
        try:
            graph = await lineage_service.build_graph(
                db,
                kind=NodeKind.JOB,
                obj_id=job.id,
                actor_user_id=current_user.id,
                surface="report",
            )
            provenance_svg = graph_to_svg(graph)
        except (LineageAccessDeniedError, LineageNotFoundError):
            provenance_svg = None

    kpi_cards = await _load_kpi_cards(
        db,
        job_id=job.id,
        tool_alias=job.tool_alias,
        tool_version_id=job.tool_version_id,
        settings=settings,
    )

    # Auto-generated results summary (designs/auto-summaries.md).
    # Best-effort: a collection failure must never sink the report, so it
    # degrades to no Summary card exactly like the other optional sections.
    # Enrichment runs through the cached SummaryService when the install has
    # it on; the viewer's opt-out selects the templated baseline instead.
    summary: SummaryText | None = None
    try:
        facts = await collect_job_facts(
            db,
            job=job,
            settings=settings,
            tool_name=tool_name,
            outputs_doc=output_bundle.outputs_doc,
        )
        enrichment = await enrichment_context_if_enabled(
            db, settings=settings, audit=audit, actor=f"user:{current_user.id}"
        )
        cached = await summary_service.build(db, facts, enrichment=enrichment)
        summary = cached.render_for(
            prefer_enriched=bool(current_user.summary_enrichment_enabled)
        )
    except Exception:  # summary is decorative; never fail the report.
        summary = None

    context = ReportContext(
        job_id=job.id,
        tool_alias=job.tool_alias,
        tool_name=tool_name,
        tool_version=tool_version,
        submitted_by_username=current_user.username,
        submitted_at=job.created_at,
        completed_at=job.completed_at,
        report_generated_at=_dt.datetime.now(tz=_dt.UTC),
        app_display_title=branding.app_display_title,
        logo_data_uri=logo_data_uri,
        timezone=current_user.timezone_preference or "auto",
        locale=current_user.locale_preference or "auto",
        descriptor=descriptor,
        job_inputs=job_inputs,
        input_image_previews=input_image_previews,
        outputs_doc=output_bundle.outputs_doc,
        output_image_previews=output_bundle.image_previews,
        output_csv_tables=output_bundle.csv_tables,
        output_csv_plots=output_bundle.csv_plots,
        output_videos=output_bundle.videos,
        output_pdfs=output_bundle.pdfs,
        output_html_embeds=output_bundle.html_embeds,
        output_file_notes=output_bundle.file_notes,
        workflow_context=workflow_context,
        provenance_svg=provenance_svg,
        kpi_cards=kpi_cards,
        summary=summary,
    )
    html_text = build_report(context)

    await audit.record(
        db,
        RecordInput(
            actor=f"user:{current_user.id}",
            actor_type="user",
            action=_AUDIT_ACTION,
            resource_type="job",
            resource_id=str(job.id),
            outcome="success",
            payload={"tool_alias": job.tool_alias},
        ),
    )
    if kpi_cards:
        await audit.record(
            db,
            RecordInput(
                actor=f"user:{current_user.id}",
                actor_type="user",
                action="kpi.viewed",
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                payload={
                    "tool_alias": job.tool_alias,
                    "surface": "report",
                    "kpi_count": len(kpi_cards),
                },
            ),
        )

    filename = _attachment_filename(
        job_id=job.id,
        tool_alias=job.tool_alias,
        generated_at=context.report_generated_at,
    )
    return Response(
        content=html_text,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _collect_input_image_previews(
    db: AsyncSession,
    job: Job,
    descriptor: dict[str, Any],
) -> dict[str, str]:
    """Build ``{path: data_uri}`` for every previewable image input.

    Walks the descriptor's file-type fields, resolves their submitted
    paths against the job's :class:`StorageLocation` mount root, and
    asks :func:`inline_input_image` to embed each one. Anything that
    can't be resolved or isn't a recognised image extension is
    silently dropped — the report falls back to filename-only.
    """
    file_fields = [
        f
        for f in (descriptor.get("inputs") or [])
        if isinstance(f, dict) and f.get("type") == "file" and f.get("name")
    ]
    if not file_fields:
        return {}
    if job.storage_location_id is None:
        return {}
    location = await db.get(StorageLocation, job.storage_location_id)
    if location is None or not location.mount_path:
        return {}
    mount_root = Path(location.mount_path)
    previews: dict[str, str] = {}
    inputs = job.inputs or {}
    for spec in file_fields:
        value = inputs.get(str(spec["name"]))
        if not isinstance(value, str) or not value:
            continue
        if not is_previewable_image(value):
            continue
        data_uri = inline_input_image(value=value, mount_root=mount_root)
        if data_uri is not None:
            previews[value] = data_uri
    return previews


async def _collect_outputs(
    db: AsyncSession,
    job: Job,
    descriptor: dict[str, Any],
) -> OutputPreviewBundle:
    """Resolve the job's working dir and build the outputs context block.

    Returns an :class:`OutputPreviewBundle`. An empty bundle is
    produced when the job has no resolvable working directory (e.g.
    the storage location row went missing), so the report still
    renders the descriptor groups with filename-only entries.
    """
    if job.storage_location_id is None or not job.working_dir_relative:
        return OutputPreviewBundle()
    location = await db.get(StorageLocation, job.storage_location_id)
    if location is None or not location.mount_path:
        return OutputPreviewBundle()
    mount_root = Path(location.mount_path)
    working_dir = mount_root / job.working_dir_relative
    return collect_output_previews(
        descriptor=descriptor,
        mount_root=mount_root,
        working_dir=working_dir,
    )


async def _collect_workflow_context(
    db: AsyncSession,
    job: Job,
) -> WorkflowContext | None:
    """Build the workflow-card payload, or ``None`` for standalone jobs.

    Per ADR-0004 §5: when ``job.workflow_run_id`` is set, surface the
    parent workflow's name + every sibling job's tool + inputs in a
    collapsible block. Sibling outputs are deliberately not fetched —
    keeps the report's per-job focus while still surfacing the
    upstream-input trail.
    """
    if job.workflow_run_id is None:
        return None
    run = await db.get(WorkflowRun, job.workflow_run_id)
    if run is None:
        return None
    workflow_row = await db.get(Workflow, run.workflow_id)
    workflow_name = workflow_row.name if workflow_row is not None else f"Workflow {run.workflow_id}"
    run_name = run.name or workflow_name

    sibling_rows = (
        (await db.execute(select(Job).where(Job.workflow_run_id == run.id).order_by(Job.id.asc())))
        .scalars()
        .all()
    )
    siblings: list[SiblingJob] = []
    for row in sibling_rows:
        sibling_tool = (
            await db.execute(select(Tool).where(Tool.alias == row.tool_alias))
        ).scalar_one_or_none()
        sibling_version = await db.get(ToolVersion, row.tool_version_id)
        sibling_descriptor = (
            dict(sibling_version.descriptor_snapshot) if sibling_version is not None else {}
        )
        siblings.append(
            SiblingJob(
                job_id=int(row.id),
                tool_name=sibling_tool.name if sibling_tool is not None else row.tool_alias,
                tool_alias=row.tool_alias,
                is_self=(row.id == job.id),
                descriptor=sibling_descriptor,
                job_inputs=dict(row.inputs or {}),
            )
        )
    return WorkflowContext(
        workflow_name=workflow_name,
        run_name=run_name,
        siblings=siblings,
    )


def _attachment_filename(*, job_id: int, tool_alias: str, generated_at: _dt.datetime) -> str:
    """Compose the report's download filename.

    Padded ``job_id`` so directory listings sort numerically, plus
    the tool alias and a YYYYMMDD stamp so a user keeping several
    reports in one folder can tell them apart at a glance.
    """
    timestamp = generated_at.strftime("%Y%m%d")
    return f"report_job{job_id:07d}_{tool_alias}_{timestamp}.html"


async def _load_kpi_cards(
    session: AsyncSession,
    *,
    job_id: int,
    tool_alias: str,
    tool_version_id: int | None,
    settings: Settings,
) -> list[dict[str, Any]]:
    """Build the report's headline-KPI card list (ADR-0012 §8.4).

    Returns ``[]`` when the feature flag is off, when no KPIs are
    declared for the job's tool_version, or when no rows have been
    extracted yet. Cards are ordered by descriptor declaration order
    (the catalog list's natural order) so the report always shows
    KPIs in the same sequence the tool author intended.
    """
    if not feature_enabled(settings.features, Feature.KPI_EXTRACTION):
        return []
    if tool_version_id is None:
        return []
    catalog = await list_kpis_for_tool(
        session, tool_alias=tool_alias, tool_version_id=tool_version_id
    )
    if not catalog:
        return []
    rows = list(
        (
            await session.execute(
                select(JobKpiValue).where(JobKpiValue.job_id == job_id)
            )
        ).scalars()
    )
    values: dict[str, Any] = {}
    for r in rows:
        values[r.kpi_name] = (
            float(r.value_numeric) if r.value_numeric is not None else None
        )
    cards: list[dict[str, Any]] = []
    for entry in catalog:
        cards.append(
            {
                "kpi_name": entry.kpi_name,
                "kpi_label": entry.kpi_label,
                "kpi_unit": entry.kpi_unit,
                "value": values.get(entry.kpi_name),
            }
        )
    return cards
