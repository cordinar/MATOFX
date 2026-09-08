"""HTTP API for the governed file library (ADR-0006 §7).

Thin endpoints that delegate to :class:`~matofx.library.LibraryService`;
domain errors propagate to the central handlers in
:mod:`matofx.api.errors` (the ``LibraryError`` family + the
``ApprovalError`` family raised by the directed-approver gate), so there
is no per-endpoint try/except.

Gating (design §7 / project-wide toggle rule):

- **Feature flag** — a router-wide dependency 404s every route when
  ``file_library`` is off, so the surface is *absent* for a customer
  without the flag.
- **Permission** — visibility + mutate rights are resolved inside the
  service from live scope/ACL/capability state (personal = owner,
  project = ADR-0002 Member+, global publish = ``LIBRARY_CURATE``).
- **Approval** — create / add-version / scope-change resolve the
  ``library_global_publish_approval`` toggle here and pass it to the
  service with the author's nominated approver; with the gate on,
  landing a file in **global** stays ``pending`` until a *second*
  curator approves it.

Approve / reject reuse the approvals surface (the directed-approver
extension) — they are not library-router endpoints.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api._approval_notify import notify_pending_approval
from matofx.api._uploads import read_upload_capped
from matofx.api.deps import (
    get_current_user,
    get_db,
    get_library_service,
    get_notifier,
    get_settings,
)
from matofx.api.schemas import (
    LibraryApproverOption,
    LibraryFileCreatePayload,
    LibraryFileDetailResponse,
    LibraryFileResponse,
    LibraryMetadataPatch,
    LibraryPickerCandidate,
    LibraryScopeChangeRequest,
    LibraryToolAssignmentResponse,
    LibraryToolAssignRequest,
    LibraryVersionCreatePayload,
    LibraryVersionResponse,
)
from matofx.approvals.orm import ApprovalRequest
from matofx.config import Settings
from matofx.config.general_service import load_general_settings
from matofx.features import Feature, feature_enabled
from matofx.library import LibraryService, PickerCandidate, ToolAssignmentSpec
from matofx.library.orm import LibraryFile, LibraryFileTool, LibraryFileVersion
from matofx.notify import Notifier
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission


def _require_library_feature(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    """404 every route when the file-library feature flag is off (routes *absent*)."""
    if not feature_enabled(settings.features, Feature.FILE_LIBRARY):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")


router = APIRouter(dependencies=[Depends(_require_library_feature)])

_Db = Annotated[AsyncSession, Depends(get_db)]
_Me = Annotated[User, Depends(get_current_user)]
_Svc = Annotated[LibraryService, Depends(get_library_service)]
_Cfg = Annotated[Settings, Depends(get_settings)]
_Notify = Annotated[Notifier, Depends(get_notifier)]


async def _notify_library_pending(db: AsyncSession, notifier: Notifier, file_id: int) -> None:
    """Email the chosen curator if the file's newest version is gated.

    No-op when nothing is pending (e.g. the file landed in a personal /
    project scope that isn't gated).
    """
    version_id = (
        (
            await db.execute(
                select(LibraryFileVersion.id)
                .where(LibraryFileVersion.library_file_id == file_id)
                .order_by(LibraryFileVersion.ordinal.desc())
            )
        )
        .scalars()
        .first()
    )
    if version_id is not None:
        await notify_pending_approval(
            db,
            notifier,
            target_column=ApprovalRequest.library_file_version_id,
            target_value=version_id,
        )


# ---- helpers -------------------------------------------------------------


async def _approval_required(session: AsyncSession) -> bool:
    """Resolve the ``library_global_publish_approval`` toggle (default ON)."""
    values = await load_general_settings(session)
    return bool(values.get("library_global_publish_approval", True))


async def _resolve_approver(
    session: AsyncSession, approver_user_id: int | None
) -> tuple[int | None, str | None]:
    """Validate the nominated approver is an active curator; return (id, username)."""
    if approver_user_id is None:
        return None, None
    user = await session.get(User, approver_user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The chosen reviewer isn't an active user.",
        )
    if not await user_has_permission(session, user.id, Permission.LIBRARY_CURATE):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The chosen reviewer isn't a library curator.",
        )
    return user.id, user.username


def _parse_metadata[PayloadT: BaseModel](raw: str, model: type[PayloadT]) -> PayloadT:
    """Parse the multipart ``metadata`` JSON field into its payload model."""
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"The metadata field isn't valid: {exc.errors()[0]['msg']}.",
        ) from exc


async def _read_upload(upload: UploadFile, settings: Settings) -> tuple[str, bytes]:
    """Read an upload fully, enforcing the app-wide file-size ceiling.

    Uses the chunked, capped reader so an oversized body is refused
    without first buffering the whole thing into memory (a cheap DoS on
    the shared UI/API process).
    """
    content = await read_upload_capped(
        upload,
        max_bytes=settings.uploads.default_max_file_bytes,
        what="file",
    )
    return upload.filename or "upload", content


def _version_response(v: LibraryFileVersion) -> LibraryVersionResponse:
    return LibraryVersionResponse(
        id=v.id,
        ordinal=v.ordinal,
        original_filename=v.original_filename,
        content_hash=v.content_hash,
        size_bytes=v.size_bytes,
        extension=v.extension,
        provenance=v.provenance,
        reference=v.reference,
        version_notes=v.version_notes,
        publish_state=v.publish_state,
        created_at=v.created_at,
    )


def _tool_response(t: LibraryFileTool) -> LibraryToolAssignmentResponse:
    return LibraryToolAssignmentResponse(id=t.id, tool_alias=t.tool_alias, field_name=t.field_name)


def _file_response(
    lib: LibraryFile,
    *,
    version_count: int = 0,
    tools: list[LibraryFileTool] | None = None,
) -> LibraryFileResponse:
    return LibraryFileResponse(
        id=lib.id,
        name=lib.name,
        description=lib.description,
        file_kind=lib.file_kind,
        tags=list(lib.tags or []),
        scope=lib.scope,
        project_id=lib.project_id,
        owner_user_id=lib.owner_user_id,
        handling_label=lib.handling_label,
        state=lib.state,
        version=lib.version,
        created_at=lib.created_at,
        updated_at=lib.updated_at,
        version_count=version_count,
        tools=[_tool_response(t) for t in (tools or [])],
    )


async def _fetch_library_version_counts(
    session: AsyncSession, file_ids: list[int]
) -> dict[int, int]:
    """One aggregate query → ``{library_file_id: count}`` (avoids N+1 on list)."""
    if not file_ids:
        return {}
    stmt = (
        select(LibraryFileVersion.library_file_id, func.count())
        .where(LibraryFileVersion.library_file_id.in_(file_ids))
        .group_by(LibraryFileVersion.library_file_id)
    )
    return {int(fid): int(n) for fid, n in (await session.execute(stmt)).all()}


async def _fetch_library_tool_assignments(
    session: AsyncSession, file_ids: list[int]
) -> dict[int, list[LibraryFileTool]]:
    """One query → ``{library_file_id: [assignment, …]}`` (avoids N+1 on list)."""
    if not file_ids:
        return {}
    stmt = (
        select(LibraryFileTool)
        .where(LibraryFileTool.library_file_id.in_(file_ids))
        .order_by(LibraryFileTool.id.asc())
    )
    out: dict[int, list[LibraryFileTool]] = {}
    for row in (await session.execute(stmt)).scalars():
        out.setdefault(int(row.library_file_id), []).append(row)
    return out


async def _detail_response(
    svc: LibraryService, session: AsyncSession, *, actor_user_id: int, file_id: int
) -> LibraryFileDetailResponse:
    lib = await svc.get_file(session, actor_user_id=actor_user_id, file_id=file_id)
    versions = await svc.list_versions(session, actor_user_id=actor_user_id, file_id=file_id)
    tools = await svc.list_tool_assignments(session, actor_user_id=actor_user_id, file_id=file_id)
    return LibraryFileDetailResponse(
        file=_file_response(lib, version_count=len(versions)),
        versions=[_version_response(v) for v in versions],
        tools=[_tool_response(t) for t in tools],
    )


def _picker_candidate(c: PickerCandidate) -> LibraryPickerCandidate:
    return LibraryPickerCandidate(
        file_id=c.file_id,
        name=c.name,
        description=c.description,
        scope=c.scope,
        file_kind=c.file_kind,
        tags=list(c.tags),
        version_id=c.version_id,
        ordinal=c.ordinal,
        original_filename=c.original_filename,
        extension=c.extension,
        size_bytes=c.size_bytes,
        content_hash=c.content_hash,
    )


async def _field_accepted_extensions(
    session: AsyncSession, *, tool_alias: str, field_name: str, version: str | None
) -> set[str] | None:
    """Resolve a tool version's ``accepted_file_types`` for one file field.

    The picker filters NULL-field library assignments by extension (§3),
    using the constraints of the **selected** tool version. When the
    version is omitted we use the newest installed snapshot. Returns
    ``None`` (no filter) when the tool / field / constraint is absent.
    """
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    stmt = select(ToolVersion).where(ToolVersion.alias == tool_alias)
    if version is not None:
        stmt = stmt.where(ToolVersion.version == version)
    stmt = stmt.order_by(ToolVersion.created_at.desc()).limit(1)
    tool_version = (await session.execute(stmt)).scalar_one_or_none()
    if tool_version is None:
        return None
    descriptor = dict(tool_version.descriptor_snapshot or {})
    for entry in descriptor.get("inputs") or []:
        if entry.get("name") == field_name and entry.get("type") == "file":
            accepted = entry.get("accepted_file_types") or []
            exts = {str(t).strip().lstrip(".").lower() for t in accepted}
            exts.discard("")
            return exts or None
    return None


# ---- picker + approvers (distinct top segments — no ordering hazard) ------


@router.get("/library/picker", response_model=list[LibraryPickerCandidate])
async def picker(
    db: _Db,
    current_user: _Me,
    svc: _Svc,
    tool: Annotated[str, Query(min_length=1)],
    field: Annotated[str, Query(min_length=1)],
    version: str | None = Query(default=None),
) -> list[LibraryPickerCandidate]:
    """Library files selectable for tool ``tool``, file field ``field`` (§3).

    Returns the latest *published* version of every ``active`` file the
    caller can see that is assigned to the tool — field-pinned, or via a
    NULL-field assignment whose extension the selected tool version's
    ``accepted_file_types`` permits.
    """
    exts = await _field_accepted_extensions(db, tool_alias=tool, field_name=field, version=version)
    candidates = await svc.list_for_picker(
        db,
        actor_user_id=current_user.id,
        tool_alias=tool,
        field_name=field,
        accepted_extensions=exts,
    )
    return [_picker_candidate(c) for c in candidates]


@router.get("/library/approvers", response_model=list[LibraryApproverOption])
async def approvers(
    db: _Db,
    current_user: _Me,
    svc: _Svc,
    scope: str = Query(default="global"),
) -> list[LibraryApproverOption]:
    """Active library curators the caller can nominate to review a publish.

    Excludes the caller (no self-approval). ``scope`` is accepted for
    forward-compatibility; only ``global`` additions need an approver.
    """
    del svc, scope
    rows = (
        (await db.execute(select(User).where(User.is_active.is_(True)).order_by(User.username)))
        .scalars()
        .all()
    )
    out: list[LibraryApproverOption] = []
    for u in rows:
        if u.id == current_user.id:
            continue
        if await user_has_permission(db, u.id, Permission.LIBRARY_CURATE):
            out.append(
                LibraryApproverOption(user_id=u.id, username=u.username, full_name=u.full_name)
            )
    return out


# ---- files CRUD ----------------------------------------------------------


@router.get("/library/files", response_model=list[LibraryFileResponse])
async def list_files(
    db: _Db,
    current_user: _Me,
    svc: _Svc,
    scope: str | None = Query(default=None),
    tool: str | None = Query(default=None),
    include_retired: bool = Query(default=False),
) -> list[LibraryFileResponse]:
    """Browse library files the caller can see (scope + tool filters)."""
    files = await svc.list_files(
        db,
        actor_user_id=current_user.id,
        scope=scope,
        tool_alias=tool,
        include_retired=include_retired,
    )
    file_ids = [int(f.id) for f in files]
    counts = await _fetch_library_version_counts(db, file_ids)
    assignments = await _fetch_library_tool_assignments(db, file_ids)
    return [
        _file_response(
            f,
            version_count=counts.get(int(f.id), 0),
            tools=assignments.get(int(f.id), []),
        )
        for f in files
    ]


@router.post(
    "/library/files",
    response_model=LibraryFileDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_file(
    db: _Db,
    current_user: _Me,
    svc: _Svc,
    settings: _Cfg,
    notifier: _Notify,
    metadata: Annotated[str, Form(description="JSON-serialised LibraryFileCreatePayload.")],
    file: Annotated[UploadFile, File(description="The file's first version bytes.")],
) -> LibraryFileDetailResponse:
    """Create a library file + its first immutable version (multipart).

    ``metadata`` is a JSON ``LibraryFileCreatePayload`` carried in the
    multipart form alongside the ``file`` upload (mirrors tool install).
    Landing in **global** with the publish toggle ON requires an
    ``approver_user_id``.
    """
    payload = _parse_metadata(metadata, LibraryFileCreatePayload)
    filename, content = await _read_upload(file, settings)
    approval_required = await _approval_required(db)
    approver_id, approver_name = await _resolve_approver(db, payload.approver_user_id)
    lib = await svc.create_file(
        db,
        actor_user_id=current_user.id,
        name=payload.name,
        description=payload.description,
        original_filename=filename,
        content=content,
        provenance=payload.provenance,
        tool_assignments=[
            ToolAssignmentSpec(tool_alias=a.tool_alias, field_name=a.field_name)
            for a in payload.tool_assignments
        ],
        scope=payload.scope,
        project_id=payload.project_id,
        file_kind=payload.file_kind,
        tags=payload.tags,
        handling_label=payload.handling_label,
        reference=payload.reference,
        version_notes=payload.version_notes,
        approval_required=approval_required,
        designated_approver_user_id=approver_id,
        designated_approver_username=approver_name,
    )
    await _notify_library_pending(db, notifier, lib.id)
    return await _detail_response(svc, db, actor_user_id=current_user.id, file_id=lib.id)


@router.get("/library/files/{file_id}", response_model=LibraryFileDetailResponse)
async def get_file(
    file_id: int, db: _Db, current_user: _Me, svc: _Svc
) -> LibraryFileDetailResponse:
    """One library file + its full version history + tool assignments."""
    return await _detail_response(svc, db, actor_user_id=current_user.id, file_id=file_id)


@router.patch("/library/files/{file_id}", response_model=LibraryFileDetailResponse)
async def patch_file(
    file_id: int, body: LibraryMetadataPatch, db: _Db, current_user: _Me, svc: _Svc
) -> LibraryFileDetailResponse:
    """Edit entry-level metadata (optimistic-locked)."""
    await svc.update_metadata(
        db,
        actor_user_id=current_user.id,
        file_id=file_id,
        expected_version=body.expected_version,
        name=body.name,
        description=body.description,
        file_kind=body.file_kind,
        tags=body.tags,
        handling_label=body.handling_label,
    )
    return await _detail_response(svc, db, actor_user_id=current_user.id, file_id=file_id)


@router.post(
    "/library/files/{file_id}/versions",
    response_model=LibraryFileDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_version(
    file_id: int,
    db: _Db,
    current_user: _Me,
    svc: _Svc,
    settings: _Cfg,
    notifier: _Notify,
    metadata: Annotated[str, Form(description="JSON-serialised LibraryVersionCreatePayload.")],
    file: Annotated[UploadFile, File(description="The new version's bytes.")],
) -> LibraryFileDetailResponse:
    """Append a new immutable version (multipart). Re-gates if global."""
    payload = _parse_metadata(metadata, LibraryVersionCreatePayload)
    filename, content = await _read_upload(file, settings)
    approval_required = await _approval_required(db)
    approver_id, approver_name = await _resolve_approver(db, payload.approver_user_id)
    await svc.add_version(
        db,
        actor_user_id=current_user.id,
        file_id=file_id,
        original_filename=filename,
        content=content,
        provenance=payload.provenance,
        reference=payload.reference,
        version_notes=payload.version_notes,
        approval_required=approval_required,
        designated_approver_user_id=approver_id,
        designated_approver_username=approver_name,
    )
    await _notify_library_pending(db, notifier, file_id)
    return await _detail_response(svc, db, actor_user_id=current_user.id, file_id=file_id)


@router.post("/library/files/{file_id}/scope", response_model=LibraryFileDetailResponse)
async def change_scope(
    file_id: int,
    body: LibraryScopeChangeRequest,
    db: _Db,
    current_user: _Me,
    svc: _Svc,
    notifier: _Notify,
) -> LibraryFileDetailResponse:
    """Change a file's scope; landing in global enters the publish gate (§5)."""
    approval_required = await _approval_required(db)
    approver_id, approver_name = await _resolve_approver(db, body.approver_user_id)
    await svc.set_scope(
        db,
        actor_user_id=current_user.id,
        file_id=file_id,
        new_scope=body.new_scope,
        project_id=body.project_id,
        expected_version=body.expected_version,
        approval_required=approval_required,
        designated_approver_user_id=approver_id,
        designated_approver_username=approver_name,
    )
    await _notify_library_pending(db, notifier, file_id)
    return await _detail_response(svc, db, actor_user_id=current_user.id, file_id=file_id)


@router.post(
    "/library/files/{file_id}/tools",
    response_model=LibraryFileDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def assign_tool(
    file_id: int, body: LibraryToolAssignRequest, db: _Db, current_user: _Me, svc: _Svc
) -> LibraryFileDetailResponse:
    """Assign the file to a ``(tool_alias[, field_name])`` pair."""
    await svc.assign_tool(
        db,
        actor_user_id=current_user.id,
        file_id=file_id,
        tool_alias=body.tool_alias,
        field_name=body.field_name,
    )
    return await _detail_response(svc, db, actor_user_id=current_user.id, file_id=file_id)


@router.delete("/library/files/{file_id}/tools/{map_id}", response_model=LibraryFileDetailResponse)
async def unassign_tool(
    file_id: int, map_id: int, db: _Db, current_user: _Me, svc: _Svc
) -> LibraryFileDetailResponse:
    """Remove a tool assignment by its row id; the last one can't be removed."""
    row = await db.get(LibraryFileTool, map_id)
    if row is None or row.library_file_id != file_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That tool assignment doesn't exist on this library file.",
        )
    await svc.unassign_tool(
        db,
        actor_user_id=current_user.id,
        file_id=file_id,
        tool_alias=row.tool_alias,
        field_name=row.field_name,
    )
    return await _detail_response(svc, db, actor_user_id=current_user.id, file_id=file_id)


@router.post("/library/files/{file_id}/retire", response_model=LibraryFileResponse)
async def retire_file(file_id: int, db: _Db, current_user: _Me, svc: _Svc) -> LibraryFileResponse:
    """Retire a file (soft delete; never a hard delete)."""
    lib = await svc.retire(db, actor_user_id=current_user.id, file_id=file_id)
    counts = await _fetch_library_version_counts(db, [int(lib.id)])
    return _file_response(lib, version_count=counts.get(int(lib.id), 0))


@router.post("/library/files/{file_id}/restore", response_model=LibraryFileResponse)
async def restore_file(file_id: int, db: _Db, current_user: _Me, svc: _Svc) -> LibraryFileResponse:
    """Restore a retired file (reverse of retire; back to ``active``)."""
    lib = await svc.restore(db, actor_user_id=current_user.id, file_id=file_id)
    counts = await _fetch_library_version_counts(db, [int(lib.id)])
    return _file_response(lib, version_count=counts.get(int(lib.id), 0))


@router.get("/library/files/{file_id}/versions/{version_id}/download")
async def download_version(
    file_id: int, version_id: int, db: _Db, current_user: _Me, svc: _Svc
) -> Response:
    """Stream a version's bytes (access-checked + audited as a download)."""
    version, content = await svc.open_version_bytes(
        db, actor_user_id=current_user.id, version_id=version_id
    )
    if version.library_file_id != file_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="We couldn't find that library file version.",
        )
    filename = version.original_filename.replace('"', "")
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
