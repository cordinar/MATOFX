"""HTTP API for V&V (Verification & Validation) tracking (ADR-0011 §7).

Thin endpoints that delegate to :class:`~matofx.vv.VVService`; domain
errors propagate to the central handlers in :mod:`matofx.api.errors`
(the :class:`VVError` umbrella + the :class:`ApprovalError` family
raised by the directed-approver gate), so there is no per-endpoint
try/except.

Gating (project-wide toggle rule, design §7):

- **Feature flag** — a router-wide dependency 404s every route when
  ``vv_tracking`` is off, so the surface is *absent* for a customer
  without the flag.
- **Permission** — reads are open to any authenticated user; create /
  edit / submit / decide / retire require :attr:`~matofx.users.permissions.Permission.VV_CURATE`,
  enforced by the service (``PermissionDeniedError`` → 403). The
  curator inbox + the eligible-curator picker themselves require
  ``VV_CURATE`` (they're privileged views).
- **Approval toggle** — submit / decide resolve the
  ``vv_record_approval`` General Setting here and pass the resolved
  boolean to the service. The runtime read of the setting lands in
  slice 7 — for now we default ON, matching the design's secure
  default. This keeps the surface in place; flipping the toggle off
  requires the slice-7 settings catalog entry which the router will
  consult once it lands.
"""

from __future__ import annotations

import datetime as _dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api._approval_notify import notify_pending_approval
from matofx.api.deps import (
    get_current_user,
    get_db,
    get_notifier,
    get_settings,
    get_vv_service,
)
from matofx.api.schemas import (
    JobVVRecordCreateRequest,
    JobVVRecordPatchRequest,
    JobVVRecordResponse,
    ToolVVRecordCreateRequest,
    ToolVVRecordPatchRequest,
    ToolVVRecordResponse,
    VVCuratorOption,
    VVDecideRequest,
    VVEnvelopeCheckRequest,
    VVEnvelopeWarningResponse,
    VVEvidenceItem,
    VVEvidenceItemInput,
    VVMigrateSkip,
    VVMigrateToolRecordsRequest,
    VVMigrateToolRecordsResponse,
    VVNumericFactorResponse,
    VVPendingRecordResponse,
    VVRecordListPage,
    VVRecordSummaryEntry,
    VVStatusBadgeResponse,
    VVSubmitRequest,
)
from matofx.approvals.orm import ApprovalRequest
from matofx.config import Settings
from matofx.features import Feature, feature_enabled
from matofx.hierarchy.orm import Job
from matofx.library.orm import LibraryFile, LibraryFileVersion
from matofx.notify import Notifier
from matofx.tools.orm import Tool, ToolVersion
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission
from matofx.vv import (
    EnvelopeWarning,
    JobVVRecord,
    PendingVVRecord,
    ToolVVRecord,
    VVRecordRow,
    VVService,
    VVStatusBadge,
)


def _require_vv_feature(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """404 every route when the V&V feature flag is off (routes *absent*)."""
    if not feature_enabled(settings.features, Feature.VV_TRACKING):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")


router = APIRouter(dependencies=[Depends(_require_vv_feature)])

_Db = Annotated[AsyncSession, Depends(get_db)]
_Me = Annotated[User, Depends(get_current_user)]
_Svc = Annotated[VVService, Depends(get_vv_service)]
_Notify = Annotated[Notifier, Depends(get_notifier)]


# ---- helpers --------------------------------------------------------


def _tool_response(row: ToolVVRecord) -> ToolVVRecordResponse:
    return ToolVVRecordResponse.model_validate(
        {
            "id": row.id,
            "tool_version_id": row.tool_version_id,
            "kind": row.kind,
            "state": row.state,
            "evidence_file_version_id": row.evidence_file_version_id,
            "evidence_url": row.evidence_url,
            "applicable_range": dict(row.applicable_range_json or {}),
            "confidence_band": row.confidence_band,
            "standards_cited": list(row.standards_cited_json or []),
            "notes": row.notes,
            "recorded_by_user_id": row.recorded_by_user_id,
            "recorded_at": row.recorded_at,
            "decided_by_user_id": row.decided_by_user_id,
            "decided_at": row.decided_at,
            "decision_reason": row.decision_reason,
            "version": row.version,
        }
    )


def _job_response(row: JobVVRecord) -> JobVVRecordResponse:
    return JobVVRecordResponse.model_validate(
        {
            "id": row.id,
            "job_id": row.job_id,
            "kind": row.kind,
            "state": row.state,
            "evidence_file_version_id": row.evidence_file_version_id,
            "evidence_url": row.evidence_url,
            "notes": row.notes,
            "recorded_by_user_id": row.recorded_by_user_id,
            "recorded_at": row.recorded_at,
            "decided_by_user_id": row.decided_by_user_id,
            "decided_at": row.decided_at,
            "decision_reason": row.decision_reason,
            "version": row.version,
        }
    )


def _badge_response(badge: VVStatusBadge) -> VVStatusBadgeResponse:
    return VVStatusBadgeResponse(
        tool_version_id=badge.tool_version_id,
        kind=badge.kind,
        has_approved_verification=badge.has_approved_verification,
        has_approved_validation=badge.has_approved_validation,
        has_pending=badge.has_pending,
        latest_envelope=badge.latest_envelope,
        cohort_bundle_hash=badge.cohort_bundle_hash,
        contributing_version_ids=list(badge.contributing_version_ids),
        inherited=badge.inherited,
    )


def _warning_response(warning: EnvelopeWarning) -> VVEnvelopeWarningResponse:
    return VVEnvelopeWarningResponse(
        tool_version_id=warning.tool_version_id,
        out_of_range=[
            {
                "factor": factor,
                "value": value,
                "envelope_min": lo,
                "envelope_max": hi,
            }
            for factor, value, lo, hi in warning.out_of_range
        ],
    )


def _pending_response(p: PendingVVRecord) -> VVPendingRecordResponse:
    return VVPendingRecordResponse(
        record_id=p.record_id,
        record_kind=p.record_kind,
        vv_kind=p.vv_kind,
        recorded_by_user_id=p.recorded_by_user_id,
        recorded_at=p.recorded_at,
        target_id=p.target_id,
    )


_TOOL_VV_KIND_LABEL: dict[str, str] = {
    "verification": "Verification",
    "validation": "Validation",
}
_JOB_VV_KIND_LABEL: dict[str, str] = {
    "validated_against": "Validated against",
    "calibrated_from": "Calibrated from",
    "sanity_checked": "Sanity checked",
}


def _vv_kind_label(record_kind: str, vv_kind: str) -> str:
    table = _TOOL_VV_KIND_LABEL if record_kind == "tool" else _JOB_VV_KIND_LABEL
    return table.get(vv_kind, vv_kind.replace("_", " ").capitalize())


def _user_label(full_name: str | None, username: str | None) -> str:
    full = (full_name or "").strip()
    uname = str(username or "")
    return f"{full} ({uname})" if full else uname


async def _tool_version_labels(session: AsyncSession, tv_ids: set[int]) -> dict[int, str]:
    """``tool_version_id`` → ``"Tool Name vX"`` (name via alias, version off the row)."""
    if not tv_ids:
        return {}
    tv_rows = (
        await session.execute(
            select(ToolVersion.id, ToolVersion.alias, ToolVersion.version).where(
                ToolVersion.id.in_(tv_ids)
            )
        )
    ).all()
    aliases = {alias for _id, alias, _v in tv_rows}
    name_by_alias: dict[str, str] = {}
    if aliases:
        for alias, name in (
            await session.execute(select(Tool.alias, Tool.name).where(Tool.alias.in_(aliases)))
        ).all():
            name_by_alias[str(alias)] = str(name or alias)
    return {
        int(tv_id): f"{name_by_alias.get(str(alias), str(alias))} v{version}"
        for tv_id, alias, version in tv_rows
    }


async def _job_labels(session: AsyncSession, job_ids: set[int]) -> dict[int, str]:
    """``job_id`` → the job's display name."""
    if not job_ids:
        return {}
    return {
        int(jid): str(name or "")
        for jid, name in (
            await session.execute(select(Job.id, Job.name).where(Job.id.in_(job_ids)))
        ).all()
    }


async def _user_labels(session: AsyncSession, user_ids: set[int]) -> dict[int, str]:
    """``user_id`` → ``"Full Name (username)"``."""
    if not user_ids:
        return {}
    return {
        int(uid): _user_label(full_name, username)
        for uid, full_name, username in (
            await session.execute(
                select(User.id, User.full_name, User.username).where(User.id.in_(user_ids))
            )
        ).all()
    }


async def _evidence_labels(session: AsyncSession, ev_ids: set[int]) -> dict[int, str]:
    """``evidence_file_version_id`` → the parent ``LibraryFile.name``."""
    if not ev_ids:
        return {}
    ev_rows = (
        await session.execute(
            select(LibraryFileVersion.id, LibraryFileVersion.library_file_id).where(
                LibraryFileVersion.id.in_(ev_ids)
            )
        )
    ).all()
    parent_ids = {int(pid) for _id, pid in ev_rows}
    name_by_file: dict[int, str] = {}
    if parent_ids:
        for fid, name in (
            await session.execute(
                select(LibraryFile.id, LibraryFile.name).where(LibraryFile.id.in_(parent_ids))
            )
        ).all():
            name_by_file[int(fid)] = str(name or "")
    return {int(ev_id): name_by_file.get(int(pid), "") for ev_id, pid in ev_rows}


def _raw_evidence(row: ToolVVRecord | JobVVRecord) -> list[dict[str, Any]]:
    """One record's evidence list — the stored list, or synthesised from scalars."""
    items = list(row.evidence_json or [])
    if items:
        return items
    synth: list[dict[str, Any]] = []
    if row.evidence_file_version_id is not None:
        synth.append(
            {"kind": "library", "file_version_id": int(row.evidence_file_version_id), "url": None}
        )
    if row.evidence_url:
        synth.append({"kind": "url", "file_version_id": None, "url": str(row.evidence_url)})
    return synth


def _evidence_views(items: list[dict[str, Any]], ev_label: dict[int, str]) -> list[VVEvidenceItem]:
    """Turn raw evidence entries into resolved views (library name / the URL)."""
    views: list[VVEvidenceItem] = []
    for it in items:
        if it.get("kind") == "library" and it.get("file_version_id") is not None:
            fid = int(it["file_version_id"])
            views.append(
                VVEvidenceItem(
                    kind="library",
                    file_version_id=fid,
                    url=None,
                    label=ev_label.get(fid) or f"file version #{fid}",
                )
            )
        elif it.get("kind") == "url" and it.get("url"):
            url = str(it["url"])
            views.append(VVEvidenceItem(kind="url", file_version_id=None, url=url, label=url))
    return views


def _evidence_summary(views: list[VVEvidenceItem]) -> str | None:
    """Short one-liner for the register row's attach-icon tooltip."""
    if not views:
        return None
    if len(views) == 1:
        return views[0].label
    return f"{len(views)} evidence items"


def _evidence_ids(items: list[dict[str, Any]]) -> set[int]:
    return {
        int(it["file_version_id"])
        for it in items
        if it.get("kind") == "library" and it.get("file_version_id") is not None
    }


def _evidence_payload(evidence: list[VVEvidenceItemInput] | None) -> list[dict[str, Any]] | None:
    """Serialise a create/patch ``evidence`` list for the service (or None)."""
    if evidence is None:
        return None
    return [e.model_dump() for e in evidence]


def _summary_entry(
    r: VVRecordRow,
    *,
    tv_label: dict[int, str],
    job_label: dict[int, str],
    user_label: dict[int, str],
    ev_label: dict[int, str],
) -> VVRecordSummaryEntry:
    """Assemble one register entry from a raw row + the resolved label maps."""
    if r.record_kind == "tool":
        target_label = tv_label.get(r.target_id, f"tool version #{r.target_id}")
    else:
        target_label = job_label.get(r.target_id) or f"job #{r.target_id}"
    views = _evidence_views(r.evidence_items, ev_label)
    return VVRecordSummaryEntry(
        record_id=r.record_id,
        record_kind=r.record_kind,
        vv_kind=r.vv_kind,
        vv_kind_label=_vv_kind_label(r.record_kind, r.vv_kind),
        state=r.state,
        target_id=r.target_id,
        target_label=target_label,
        recorded_by_user_id=r.recorded_by_user_id,
        recorded_by_label=user_label.get(r.recorded_by_user_id, f"user #{r.recorded_by_user_id}"),
        recorded_at=r.recorded_at,
        decided_by_label=(
            user_label.get(r.decided_by_user_id) if r.decided_by_user_id is not None else None
        ),
        decided_at=r.decided_at,
        evidence_label=_evidence_summary(views),
        evidence_items=views,
        standards_cited=r.standards_cited,
        notes=r.notes,
    )


async def _enrich_vv_rows(
    session: AsyncSession, rows: list[VVRecordRow]
) -> list[VVRecordSummaryEntry]:
    """Resolve every id on the register rows to a human label in batched reads.

    Owns the cross-package joins the V&V service deliberately avoids:
    tool version → ``Tool.name`` + version, job → ``Job.name``, user →
    ``Full Name (username)``, evidence → ``LibraryFile.name`` (per item,
    falling back to the URL). Each label kind is resolved in one batched
    query, so a page of N rows costs a fixed handful of queries, never N+1.
    """
    tv_ids = {r.target_id for r in rows if r.record_kind == "tool"}
    job_ids = {r.target_id for r in rows if r.record_kind == "job"}
    user_ids = {r.recorded_by_user_id for r in rows} | {
        r.decided_by_user_id for r in rows if r.decided_by_user_id is not None
    }
    ev_ids: set[int] = set()
    for r in rows:
        ev_ids |= _evidence_ids(r.evidence_items)

    tv_label = await _tool_version_labels(session, tv_ids)
    job_label = await _job_labels(session, job_ids)
    user_label = await _user_labels(session, user_ids)
    ev_label = await _evidence_labels(session, ev_ids)

    return [
        _summary_entry(
            r, tv_label=tv_label, job_label=job_label, user_label=user_label, ev_label=ev_label
        )
        for r in rows
    ]


async def _resolve_approval_toggle(session: AsyncSession) -> bool:  # noqa: ARG001
    """Resolve the ``vv_record_approval`` toggle.

    Slice-5 placeholder: returns the secure default (``True``). The
    real read against ``general_setting`` lands in slice 7 alongside
    the catalog row that defines it. Keeps the router surface stable
    so slice 7's wiring is a one-line change.
    """
    return True


async def _require_curate(session: AsyncSession, user: User) -> None:
    """403 unless the caller holds ``vv.curate``.

    Service-side methods enforce this too via
    :meth:`VVService._require_curate`; the router-side pre-check exists
    so privileged-view endpoints (curator inbox, eligible-curator
    picker) refuse cleanly without the service having to first read
    rows it shouldn't surface.
    """
    if not await user_has_permission(session, user.id, Permission.VV_CURATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Curating V&V records requires the V&V curator capability role.",
        )


# ---- reads ----------------------------------------------------------


@router.get("/vv/status/tool/{tool_version_id}", response_model=VVStatusBadgeResponse)
async def status_for_tool_version(
    tool_version_id: int, db: _Db, current_user: _Me, svc: _Svc
) -> VVStatusBadgeResponse:
    """Inheritance-aware V&V badge for one tool version (design §5.1 / §6.5)."""
    del current_user
    badge = await svc.summarise_tool_status(db, tool_version_id=tool_version_id)
    return _badge_response(badge)


@router.post("/vv/envelope-check", response_model=VVEnvelopeWarningResponse | None)
async def envelope_check(
    body: VVEnvelopeCheckRequest, db: _Db, current_user: _Me, svc: _Svc
) -> VVEnvelopeWarningResponse | None:
    """Soft-warn check: do the submitted values fall outside the approved envelope?

    Returns ``None`` when no approved validation exists, when no
    envelope is declared on those validations, or when every supplied
    value falls inside the cohort's union envelope. The submit-form UI
    calls this on each value change to render a banner (design §6.5);
    the check is advisory — never blocks. The per-project
    ``vv_required_for_submit`` policy is the blocking gate (enforced
    server-side in :meth:`JobService.queue_for_runner`).
    """
    del current_user
    warning = await svc.resolve_envelope_warning(
        db, tool_version_id=body.tool_version_id, form_values=body.form_values
    )
    if warning is None:
        return None
    return _warning_response(warning)


@router.get("/vv/tool-records", response_model=list[ToolVVRecordResponse])
async def list_tool_records(
    db: _Db,
    current_user: _Me,
    svc: _Svc,
    tool_version_id: int = Query(...),
    include_retired: bool = Query(default=False),
) -> list[ToolVVRecordResponse]:
    """Every V&V record for one tool version (no inheritance expansion).

    Useful for the per-tool admin tab; the inheritance-expanded badge
    is served by :meth:`status_for_tool_version`.
    """
    del current_user
    records = await svc.list_for_tool_version(
        db, tool_version_id=tool_version_id, include_retired=include_retired
    )
    return [_tool_response(r) for r in records]


async def _record_evidence_views(
    session: AsyncSession, row: ToolVVRecord | JobVVRecord
) -> list[VVEvidenceItem]:
    """Resolve one record's full evidence list to labelled views."""
    raw = _raw_evidence(row)
    ev_label = await _evidence_labels(session, _evidence_ids(raw))
    return _evidence_views(raw, ev_label)


@router.get("/vv/tool-records/{record_id}", response_model=ToolVVRecordResponse)
async def get_tool_record(
    record_id: int, db: _Db, current_user: _Me, svc: _Svc
) -> ToolVVRecordResponse:
    """One tool V&V record, with resolved target / actor / evidence labels."""
    del current_user
    row = await svc.get_tool_record(db, record_id=record_id)
    user_ids = {row.recorded_by_user_id} | (
        {row.decided_by_user_id} if row.decided_by_user_id is not None else set()
    )
    users = await _user_labels(db, user_ids)
    targets = await _tool_version_labels(db, {row.tool_version_id})
    views = await _record_evidence_views(db, row)
    return _tool_response(row).model_copy(
        update={
            "target_label": targets.get(row.tool_version_id),
            "recorded_by_label": users.get(row.recorded_by_user_id),
            "decided_by_label": (
                users.get(row.decided_by_user_id) if row.decided_by_user_id is not None else None
            ),
            "evidence_label": _evidence_summary(views),
            "evidence_items": views,
        }
    )


_NUMERIC_INPUT_TYPES: frozenset[str] = frozenset({"integer", "float"})


@router.get(
    "/vv/numeric-factors/{tool_version_id}",
    response_model=list[VVNumericFactorResponse],
)
async def numeric_factors(
    tool_version_id: int, db: _Db, current_user: _Me
) -> list[VVNumericFactorResponse]:
    """Numeric inputs of a tool version — the factors the range editor offers.

    Read straight from the version's descriptor snapshot so the applicable-
    range editor lists the exact factors (by label) that the submit-time
    envelope check compares against (by ``name``). Empty list for a version
    with no numeric inputs, or one that doesn't exist (the caller just shows
    "no numeric inputs" rather than erroring).
    """
    del current_user
    snapshot = (
        await db.execute(
            select(ToolVersion.descriptor_snapshot).where(ToolVersion.id == tool_version_id)
        )
    ).scalar_one_or_none()
    if not snapshot:
        return []
    out: list[VVNumericFactorResponse] = []
    for inp in snapshot.get("inputs") or []:
        if inp.get("type") not in _NUMERIC_INPUT_TYPES:
            continue
        out.append(
            VVNumericFactorResponse(
                name=str(inp.get("name") or ""),
                label=str(inp.get("label") or inp.get("name") or ""),
                unit=inp.get("unit"),
                minimum=inp.get("minimum"),
                maximum=inp.get("maximum"),
            )
        )
    return out


@router.get("/vv/job-records", response_model=list[JobVVRecordResponse])
async def list_job_records(
    db: _Db,
    current_user: _Me,
    svc: _Svc,
    job_id: int = Query(...),
    include_retired: bool = Query(default=False),
) -> list[JobVVRecordResponse]:
    """Every V&V record for one job (no inheritance — job claims are per-run)."""
    del current_user
    records = await svc.list_for_job(db, job_id=job_id, include_retired=include_retired)
    return [_job_response(r) for r in records]


@router.get("/vv/job-records/{record_id}", response_model=JobVVRecordResponse)
async def get_job_record(
    record_id: int, db: _Db, current_user: _Me, svc: _Svc
) -> JobVVRecordResponse:
    """One job V&V record, with resolved target / actor / evidence labels."""
    del current_user
    row = await svc.get_job_record(db, record_id=record_id)
    user_ids = {row.recorded_by_user_id} | (
        {row.decided_by_user_id} if row.decided_by_user_id is not None else set()
    )
    users = await _user_labels(db, user_ids)
    targets = await _job_labels(db, {row.job_id})
    views = await _record_evidence_views(db, row)
    return _job_response(row).model_copy(
        update={
            "target_label": targets.get(row.job_id),
            "recorded_by_label": users.get(row.recorded_by_user_id),
            "decided_by_label": (
                users.get(row.decided_by_user_id) if row.decided_by_user_id is not None else None
            ),
            "evidence_label": _evidence_summary(views),
            "evidence_items": views,
        }
    )


_VV_RECORD_STATES: frozenset[str] = frozenset(
    {"draft", "pending", "approved", "rejected", "retired"}
)


@router.get("/vv/records", response_model=VVRecordListPage)
async def list_records(
    db: _Db,
    current_user: _Me,
    svc: _Svc,
    record_kind: Annotated[str, Query()] = "all",
    state: Annotated[list[str] | None, Query()] = None,
    kind: Annotated[list[str] | None, Query()] = None,
    author_user_id: Annotated[int | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    since: Annotated[_dt.datetime | None, Query()] = None,
    until: Annotated[_dt.datetime | None, Query()] = None,
    include_retired: Annotated[bool, Query()] = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> VVRecordListPage:
    """The cross-install V&V register — every record, both tables, filtered.

    The browsable backbone the id-keyed ``/vv/tool-records`` and
    ``/vv/job-records`` never offered. Reads are open to any authenticated
    user (V&V records are global). Filters AND-combine: ``record_kind`` in
    ``tool``/``job``/``all``; ``state`` and ``kind`` are repeatable; ``search``
    is a case-insensitive match over the resolved target / author / kind
    labels and notes; ``since`` / ``until`` bound ``recorded_at``.
    """
    del current_user
    if record_kind not in ("tool", "job", "all"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'record_kind' must be one of 'tool', 'job', 'all'.",
        )
    if state:
        bad = [s for s in state if s not in _VV_RECORD_STATES]
        if bad:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown state(s): {', '.join(bad)}.",
            )

    rows = await svc.list_all_records(
        db,
        record_kind=record_kind,  # type: ignore[arg-type]
        states=state,
        kinds=kind,
        author_user_id=author_user_id,
        since=since,
        until=until,
        include_retired=include_retired,
    )
    entries = await _enrich_vv_rows(db, rows)

    term = (search or "").strip().lower()
    if term:
        entries = [
            e
            for e in entries
            if term in e.target_label.lower()
            or term in e.recorded_by_label.lower()
            or term in e.vv_kind_label.lower()
            or term in (e.notes or "").lower()
        ]

    total = len(entries)
    start = (page - 1) * page_size
    return VVRecordListPage(
        rows=entries[start : start + page_size],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/vv/pending", response_model=list[VVPendingRecordResponse])
async def list_pending(db: _Db, current_user: _Me, svc: _Svc) -> list[VVPendingRecordResponse]:
    """Pending V&V records across both tables, oldest first. Curator-only."""
    await _require_curate(db, current_user)
    rows = await svc.list_pending_for_curator(db, actor_user_id=current_user.id)
    return [_pending_response(p) for p in rows]


@router.get("/vv/curators", response_model=list[VVCuratorOption])
async def list_curators(db: _Db, current_user: _Me, svc: _Svc) -> list[VVCuratorOption]:
    """Eligible-curator picker — every other curator the author can submit to.

    Curator-only (the picker is part of the author's submit flow, so
    the author must themselves be a curator under our authoring
    policy).
    """
    await _require_curate(db, current_user)
    pairs = await svc.list_eligible_curators(db, actor_user_id=current_user.id)
    return [VVCuratorOption(user_id=uid, username=uname) for uid, uname in pairs]


# ---- authoring (tool records) --------------------------------------


@router.post(
    "/vv/tool-records",
    response_model=ToolVVRecordResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_tool_record(
    body: ToolVVRecordCreateRequest, db: _Db, current_user: _Me, svc: _Svc
) -> ToolVVRecordResponse:
    """Author a draft V&V claim about a tool version. Requires curate."""
    row = await svc.record_tool_vv(
        db,
        actor_user_id=current_user.id,
        tool_version_id=body.tool_version_id,
        kind=body.kind,
        applicable_range=body.applicable_range,
        confidence_band=body.confidence_band,
        standards_cited=body.standards_cited,
        evidence_file_version_id=body.evidence_file_version_id,
        evidence_url=body.evidence_url,
        evidence_items=_evidence_payload(body.evidence),
        notes=body.notes,
    )
    return _tool_response(row)


@router.patch("/vv/tool-records/{record_id}", response_model=ToolVVRecordResponse)
async def patch_tool_record(
    record_id: int,
    body: ToolVVRecordPatchRequest,
    db: _Db,
    current_user: _Me,
    svc: _Svc,
) -> ToolVVRecordResponse:
    """Edit a draft tool V&V record. Author + curate + draft-only."""
    row = await svc.update_tool_record(
        db,
        actor_user_id=current_user.id,
        record_id=record_id,
        notes=body.notes,
        applicable_range=body.applicable_range,
        confidence_band=body.confidence_band,
        standards_cited=body.standards_cited,
        evidence_file_version_id=body.evidence_file_version_id,
        evidence_url=body.evidence_url,
        evidence_items=_evidence_payload(body.evidence),
    )
    return _tool_response(row)


@router.post("/vv/tool-records/{record_id}/submit", response_model=ToolVVRecordResponse)
async def submit_tool_record(
    record_id: int,
    body: VVSubmitRequest,
    db: _Db,
    current_user: _Me,
    svc: _Svc,
    notifier: _Notify,
) -> ToolVVRecordResponse:
    """Flip a tool V&V record from ``draft`` to ``pending`` + create the gate."""
    approval_required = await _resolve_approval_toggle(db)
    row = await svc.submit_tool_record(
        db,
        actor_user_id=current_user.id,
        record_id=record_id,
        approval_required=approval_required,
        designated_approver_user_id=body.designated_approver_user_id,
        designated_approver_username=body.designated_approver_username,
    )
    await notify_pending_approval(
        db, notifier, target_column=ApprovalRequest.tool_vv_record_id, target_value=record_id
    )
    return _tool_response(row)


@router.post("/vv/tool-records/{record_id}/approve", response_model=ToolVVRecordResponse)
async def approve_tool_record(
    record_id: int,
    body: VVDecideRequest,
    db: _Db,
    current_user: _Me,
    svc: _Svc,
) -> ToolVVRecordResponse:
    """Approve a pending tool V&V record. Requires curate."""
    approval_required = await _resolve_approval_toggle(db)
    row = await svc.approve_tool_record(
        db,
        decider_user_id=current_user.id,
        decider_username=current_user.username,
        record_id=record_id,
        reason=body.reason,
        approval_required=approval_required,
    )
    return _tool_response(row)


@router.post("/vv/tool-records/{record_id}/reject", response_model=ToolVVRecordResponse)
async def reject_tool_record(
    record_id: int,
    body: VVDecideRequest,
    db: _Db,
    current_user: _Me,
    svc: _Svc,
) -> ToolVVRecordResponse:
    """Reject a pending tool V&V record. Requires curate + reason."""
    approval_required = await _resolve_approval_toggle(db)
    row = await svc.reject_tool_record(
        db,
        decider_user_id=current_user.id,
        decider_username=current_user.username,
        record_id=record_id,
        reason=body.reason or "",
        approval_required=approval_required,
    )
    return _tool_response(row)


@router.post("/vv/tool-records/{record_id}/retire", response_model=ToolVVRecordResponse)
async def retire_tool_record(
    record_id: int, db: _Db, current_user: _Me, svc: _Svc
) -> ToolVVRecordResponse:
    """Retire a tool V&V record from any live state. Curate-gated."""
    row = await svc.retire_tool_record(db, actor_user_id=current_user.id, record_id=record_id)
    return _tool_response(row)


@router.post("/vv/tool-records/migrate", response_model=VVMigrateToolRecordsResponse)
async def migrate_tool_records(
    body: VVMigrateToolRecordsRequest, db: _Db, current_user: _Me, svc: _Svc
) -> VVMigrateToolRecordsResponse:
    """Carry approved V&V records forward from one tool version to another.

    Copies land as fresh drafts on the target version (re-review required);
    per-record problems come back as ``skipped`` rather than failing the
    whole call. Requires ``vv.curate`` (enforced by the service).
    """
    migrated, skipped = await svc.migrate_tool_records(
        db,
        actor_user_id=current_user.id,
        source_tool_version_id=body.source_tool_version_id,
        target_tool_version_id=body.target_tool_version_id,
        record_ids=body.record_ids,
    )
    return VVMigrateToolRecordsResponse(
        migrated=[_tool_response(r) for r in migrated],
        skipped=[VVMigrateSkip(source_record_id=rid, reason=reason) for rid, reason in skipped],
    )


# ---- authoring (job records) ---------------------------------------


@router.post(
    "/vv/job-records",
    response_model=JobVVRecordResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_job_record(
    body: JobVVRecordCreateRequest, db: _Db, current_user: _Me, svc: _Svc
) -> JobVVRecordResponse:
    """Author a draft V&V claim about a specific job run. Requires curate."""
    row = await svc.record_job_vv(
        db,
        actor_user_id=current_user.id,
        job_id=body.job_id,
        kind=body.kind,
        evidence_file_version_id=body.evidence_file_version_id,
        evidence_url=body.evidence_url,
        evidence_items=_evidence_payload(body.evidence),
        notes=body.notes,
    )
    return _job_response(row)


@router.patch("/vv/job-records/{record_id}", response_model=JobVVRecordResponse)
async def patch_job_record(
    record_id: int,
    body: JobVVRecordPatchRequest,
    db: _Db,
    current_user: _Me,
    svc: _Svc,
) -> JobVVRecordResponse:
    """Edit a draft job V&V record. Author + curate + draft-only."""
    row = await svc.update_job_record(
        db,
        actor_user_id=current_user.id,
        record_id=record_id,
        notes=body.notes,
        evidence_file_version_id=body.evidence_file_version_id,
        evidence_url=body.evidence_url,
        evidence_items=_evidence_payload(body.evidence),
    )
    return _job_response(row)


@router.post("/vv/job-records/{record_id}/submit", response_model=JobVVRecordResponse)
async def submit_job_record(
    record_id: int,
    body: VVSubmitRequest,
    db: _Db,
    current_user: _Me,
    svc: _Svc,
    notifier: _Notify,
) -> JobVVRecordResponse:
    """Flip a job V&V record from ``draft`` to ``pending`` + create the gate."""
    approval_required = await _resolve_approval_toggle(db)
    row = await svc.submit_job_record(
        db,
        actor_user_id=current_user.id,
        record_id=record_id,
        approval_required=approval_required,
        designated_approver_user_id=body.designated_approver_user_id,
        designated_approver_username=body.designated_approver_username,
    )
    await notify_pending_approval(
        db, notifier, target_column=ApprovalRequest.job_vv_record_id, target_value=record_id
    )
    return _job_response(row)


@router.post("/vv/job-records/{record_id}/approve", response_model=JobVVRecordResponse)
async def approve_job_record(
    record_id: int,
    body: VVDecideRequest,
    db: _Db,
    current_user: _Me,
    svc: _Svc,
) -> JobVVRecordResponse:
    """Approve a pending job V&V record. Requires curate."""
    approval_required = await _resolve_approval_toggle(db)
    row = await svc.approve_job_record(
        db,
        decider_user_id=current_user.id,
        decider_username=current_user.username,
        record_id=record_id,
        reason=body.reason,
        approval_required=approval_required,
    )
    return _job_response(row)


@router.post("/vv/job-records/{record_id}/reject", response_model=JobVVRecordResponse)
async def reject_job_record(
    record_id: int,
    body: VVDecideRequest,
    db: _Db,
    current_user: _Me,
    svc: _Svc,
) -> JobVVRecordResponse:
    """Reject a pending job V&V record. Requires curate + reason."""
    approval_required = await _resolve_approval_toggle(db)
    row = await svc.reject_job_record(
        db,
        decider_user_id=current_user.id,
        decider_username=current_user.username,
        record_id=record_id,
        reason=body.reason or "",
        approval_required=approval_required,
    )
    return _job_response(row)


@router.post("/vv/job-records/{record_id}/retire", response_model=JobVVRecordResponse)
async def retire_job_record(
    record_id: int, db: _Db, current_user: _Me, svc: _Svc
) -> JobVVRecordResponse:
    """Retire a job V&V record. Curate-gated."""
    row = await svc.retire_job_record(db, actor_user_id=current_user.id, record_id=record_id)
    return _job_response(row)
