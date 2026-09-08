"""Jobs router — submit / status / logs / outputs / cancel.

POST /api/scenarios/{sid}/jobs            → create + queue a job in one call.
GET  /api/scenarios/{sid}/jobs            → list jobs in a scenario.
GET  /api/users/me/visible-jobs           → cross-project ACL-filtered job list
                                            (powers the My Jobs workspace's global
                                            Job-ID picker + recent-activity panel).
GET  /api/jobs/{id}                       → status (state, heartbeat, timestamps, exit_code).
GET  /api/jobs/{id}/logs                  → captured stdout + stderr.
GET  /api/jobs/{id}/outputs               → list of output filenames.
GET  /api/jobs/{id}/output-document       → parsed outputs.json (variables + files).
GET  /api/jobs/{id}/outputs/{filename}    → stream one output file.
GET  /api/jobs/{id}/outputs.zip           → ZIP every file under outputs/.
GET  /api/jobs/{id}/all-files.zip         → ZIP the whole working directory.
POST /api/jobs/{id}/cancel                → request cancellation; runner picks it up.

The submit endpoint creates the job (state=created) and immediately
queues it (state=queued + working_dir staged). The runner daemon's
pickup loop drives it forward from there.
"""

from __future__ import annotations

import asyncio
import functools
import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api._uploads import read_upload_capped
from matofx.api.deps import (
    get_current_user,
    get_db,
    get_job_service,
    get_notifier,
    get_settings,
)
from matofx.api.schemas import (
    JobCreate,
    JobDraftCreate,
    JobDraftUpdateRequest,
    JobFieldFramesResponse,
    JobLogsResponse,
    JobOutputsListResponse,
    JobQueueRequest,
    JobResponse,
    VisibleJobResponse,
)
from matofx.config import Settings
from matofx.hierarchy.errors import DuplicateToolInScenarioError
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.hierarchy.service import JobService
from matofx.notify import Notifier
from matofx.tools.exec.contracts import OutputsDocument
from matofx.tools.orm import Tool, ToolVersion
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission

_MIN_DOWNSAMPLE_POINTS = 2
"""Fewer than two points cannot be down-sampled."""

_log = logging.getLogger(__name__)

router = APIRouter()


async def _accessible_outputs_job(
    db: AsyncSession,
    job_service: JobService,
    *,
    job_id: int,
    current_user: User,
) -> Job:
    """Resolve a job for an *output* surface, enforcing the §16.15 lockdown.

    The six output endpoints (output-document / outputs list / one
    output file / outputs.zip / all-files.zip / output cad-preview)
    all funnel through here so the post-approval rule lives in exactly
    one place. The caller's admin (see-all) capability is resolved
    once and handed to ``JobService.ensure_outputs_accessible``:
    normal jobs need only VIEW; jobs whose results are
    ``pending_results`` / ``results_withheld`` need an eligible
    approver or an admin (the requestor included is refused with
    :class:`ResultsWithheldError`). Run logs and the job status
    endpoint are deliberately *not* gated — only the results are.
    """
    is_admin = await user_has_permission(db, current_user.id, Permission.PROJECTS_SEE_ALL)
    return await job_service.ensure_outputs_accessible(
        db,
        job_id=job_id,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        actor_is_approver=current_user.is_approver,
        actor_is_admin=is_admin,
    )


async def _notify_pre_approval_opened(db: AsyncSession, notifier: Notifier, job: Job) -> None:
    """Fire-and-forget: tell eligible approvers a pre request opened (§16.15).

    No-ops unless the just-submitted job is actually held
    (``state == 'pending_approval'``). Never raises into the submit
    request — a relay hiccup must not fail the submission.
    """
    if job.state != "pending_approval":
        return
    from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

    try:
        req = (
            (
                await db.execute(
                    select(ApprovalRequest)
                    .where(ApprovalRequest.job_id == job.id)
                    .where(ApprovalRequest.kind == "pre")
                    .where(ApprovalRequest.status == "pending")
                    .order_by(ApprovalRequest.id.desc())
                )
            )
            .scalars()
            .first()
        )
        if req is not None:
            await notifier.notify_approval_request_opened(db, request=req)
    except Exception:
        _log.exception("pre-approval notification failed; swallowed")


def _to_response(job: Job, *, tool_version: str | None = None) -> JobResponse:
    return JobResponse(
        id=job.id,
        scenario_id=job.scenario_id,
        name=job.name,
        tool_alias=job.tool_alias,
        tool_version=tool_version,
        tool_version_id=job.tool_version_id,
        state=job.state,
        heartbeat_state=job.heartbeat_state,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        exit_code=job.exit_code,
        cancel_requested_at=job.cancel_requested_at,
        workflow_run_id=job.workflow_run_id,
        workflow_position=job.workflow_position,
        parametric_study_id=job.parametric_study_id,
        optimisation_study_id=job.optimisation_study_id,
        storage_location_id=job.storage_location_id,
        working_dir_relative=job.working_dir_relative,
        inputs=dict(job.inputs or {}),
        priority=bool(getattr(job, "priority", False)),
    )


async def _resolve_job_paths(db: AsyncSession, job: Job) -> tuple[Path, Path, Path]:
    """Return (working_dir, logs_dir, outputs_dir) for an in-flight or finished job.

    Raises 409 when the job has been queued but the working dir
    metadata is missing (shouldn't happen post-queue, but the explicit
    error is friendlier than a 500).
    """
    from matofx.tools.exec_orm import StorageLocation  # noqa: PLC0415

    if job.storage_location_id is None or job.working_dir_relative is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This job hasn't been staged yet — try again once it leaves the queued state.",
        )
    location = await db.get(StorageLocation, job.storage_location_id)
    if location is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The storage location for this job is no longer registered.",
        )
    wd = Path(location.mount_path) / job.working_dir_relative
    return wd, wd / "logs", wd / "outputs"


# --- draft file uploads ----------------------------------------------


async def _resolve_active_storage_location(db: AsyncSession) -> tuple[str, int]:
    """Pick the single active StorageLocation; return (mount_path, id)."""
    from matofx.hierarchy.errors import StorageLocationUnavailableError  # noqa: PLC0415
    from matofx.tools.exec_orm import StorageLocation  # noqa: PLC0415

    locations = list(
        (
            await db.execute(
                select(StorageLocation)
                .where(StorageLocation.is_active.is_(True))
                .where(StorageLocation.retired_at.is_(None))
                .order_by(StorageLocation.id.asc())
            )
        ).scalars()
    )
    if not locations:
        raise StorageLocationUnavailableError
    return locations[0].mount_path, locations[0].id


def _descriptor_file_field(descriptor: dict[str, Any], field_name: str) -> dict[str, Any] | None:
    """Find one declared ``file`` input by name; return ``None`` when missing."""
    for entry in descriptor.get("inputs") or []:
        if entry.get("name") == field_name and entry.get("type") == "file":
            return cast("dict[str, Any]", entry)
    return None


_JOB_DIR_RE = re.compile(r"^job_(\d+)$")


def _resolve_preview_path(path: str, mount_root: Path) -> tuple[Path, int]:
    """Resolve a preview ``path`` under the mount; return (target, owning_job_id).

    The preview surfaces (draft file preview + CAD preview) serve files
    out of the shared mount's ``drafts/`` and ``jobs/`` namespaces. A
    previewed file belongs *either* to the caller's own draft *or* to a
    previous job they picked a file from, so the owning job id is parsed
    from the path's ``job_<id>`` segment here and the caller must then
    authorise *that* job. Trusting the URL's ``job_id`` instead would let
    any user read another user's job/draft files just by being able to
    edit one draft of their own (horizontal access).

    Raises ``400`` when the path can't be resolved, escapes the mount,
    isn't under ``drafts/``/``jobs/``, or carries no ``job_<id>`` segment.
    """
    try:
        target = Path(path).resolve(strict=False)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That preview path can't be resolved on this server.",
        ) from exc
    try:
        relative = target.relative_to(mount_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Preview path must be under the active storage location.",
        ) from exc
    head = relative.parts[0] if relative.parts else ""
    if head not in {"drafts", "jobs"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Preview path must live under the drafts/ or jobs/ namespace on the shared mount."
            ),
        )
    # The owning job's directory sits at parts[1] (``<head>/job_<id>/…``)
    # or parts[2] (``<head>/<env>/job_<id>/…``). ``..`` was already
    # collapsed by ``resolve()`` above, so the first match is authoritative.
    owning_job_id: int | None = None
    for part in relative.parts[1:3]:
        match = _JOB_DIR_RE.match(part)
        if match:
            owning_job_id = int(match.group(1))
            break
    if owning_job_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Preview path doesn't identify a job directory on the shared mount.",
        )
    return target, owning_job_id


@router.post("/jobs/{job_id}/uploads/{field_name}", response_model=JobResponse)
async def upload_draft_file_input(
    job_id: int,
    field_name: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    current_user: Annotated[User, Depends(get_current_user)],
    file: Annotated[UploadFile, File(description="The file the user dropped in / browsed to.")],
) -> JobResponse:
    """Stage a user-uploaded file as a draft job's ``<field_name>`` input.

    Writes the bytes to ``<mount>/drafts/<env>/job_<id>/<field_name>/<filename>``
    and rewrites the draft's ``inputs[<field_name>]`` to the absolute
    path so the form value is persisted alongside the file. The job
    stays in ``created`` state — this endpoint is the upload primitive
    that backs the workspace's drag-drop / browse-button mode for
    file inputs; the user still has to click Submit to queue the run.

    Refusals:

    - ``404`` — job missing, or the descriptor doesn't declare a
      ``file`` input by that name.
    - ``403`` — caller lacks EDIT on the project.
    - ``409`` — job isn't in ``created`` state (already submitted).
    - ``413`` — bytes exceed the descriptor's
      ``max_file_size_mb`` cap (or the app-wide default).
    """
    from matofx.tools.exec.uploads import UploadError, stage_draft_upload  # noqa: PLC0415

    # 1. Authorise + state-gate via the service layer (same checks as
    #    update_draft so the upload endpoint can't be used to side-load
    #    files onto a queued / running job).
    job = await job_service.get(db, job_id=job_id, actor_user_id=current_user.id)
    if job.state != "created":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This job is in state {job.state!r}; you can only attach files while it's a draft."
            ),
        )

    # 2. Pull the captured-snapshot descriptor so the file_type / size
    #    cap come from what the user is actually submitting against
    #    (and not from a later metadata edit on the live tool row).
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    tool_version = await db.get(ToolVersion, job.tool_version_id)
    if tool_version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The tool snapshot for this draft is missing — try re-creating the draft.",
        )
    descriptor = dict(tool_version.descriptor_snapshot or {})
    field_spec = _descriptor_file_field(descriptor, field_name)
    if field_spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"This tool doesn't declare a file input named {field_name!r} — "
                "check the descriptor."
            ),
        )

    # 3. Enforce the descriptor's ``accepted_file_types`` whitelist.
    #    The client-side widget already filters via the HTML ``accept``
    #    attribute AND a per-upload extension check, but drag-drop
    #    bypasses the browser filter and a forged client could POST
    #    anything — the server stays the authoritative gate.
    accepted_types = list(field_spec.get("accepted_file_types") or [])
    if accepted_types:
        normalised_allowed = {str(t).strip().lstrip(".").lower() for t in accepted_types}
        normalised_allowed.discard("")
        client_filename = file.filename or ""
        ext = client_filename.rsplit(".", 1)[-1].lower() if "." in client_filename else ""
        if ext not in normalised_allowed:
            pretty_ext = f".{ext}" if ext else "(no extension)"
            pretty_allowed = ", ".join(f".{t}" for t in sorted(normalised_allowed))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"That {pretty_ext} file isn't accepted for this input. "
                    f"Accepted file types: {pretty_allowed}."
                ),
            )

    # 4. Enforce the per-field size cap declared in the descriptor.
    #    ``max_file_size_mb=None`` means the tool author didn't pin a
    #    limit; we still enforce an app-wide ceiling so a stray upload
    #    can't fill the shared mount.
    declared_mb = field_spec.get("max_file_size_mb")
    if isinstance(declared_mb, (int, float)) and declared_mb > 0:
        max_bytes = int(declared_mb * 1024 * 1024)
    else:
        max_bytes = settings.uploads.default_max_file_bytes

    # Read in bounded chunks, aborting at the cap, so a large upload can't
    # pin the API host's memory before the size check runs (the body is
    # never fully buffered for a rejected upload).
    content = await read_upload_capped(file, max_bytes=max_bytes, request=request, what="file")

    # 5. Stage the bytes onto the shared mount under the draft area.
    mount_path, location_id = await _resolve_active_storage_location(db)
    try:
        stored = stage_draft_upload(
            mount_path=mount_path,
            environment=settings.environment,
            job_id=job_id,
            field_name=field_name,
            filename=file.filename or "upload",
            content=content,
            max_bytes=max_bytes,
        )
    except UploadError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # 6. Persist the absolute path onto the draft so the form re-render
    #    sees the file (and ``queue_for_runner`` can promote it).
    new_inputs = dict(job.inputs or {})
    new_inputs[field_name] = str(stored.absolute_path)
    job.inputs = new_inputs
    job.storage_location_id = location_id
    await db.flush()
    return _to_response(job)


@router.get("/jobs/{job_id}/file-preview")
async def get_draft_file_preview(
    job_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    path: Annotated[
        str,
        Query(
            description=(
                "Absolute path of the file to preview. Must resolve under the "
                "active storage mount; rejected otherwise."
            ),
            min_length=1,
        ),
    ],
) -> FileResponse:
    """Stream a file under the storage mount for inline preview.

    Used by the draft-form's file-input widget to show a preview pane
    next to the upload field — both for files uploaded via the rich
    widget (path lives under ``<mount>/drafts/...``) and for files
    picked from a previous job (path lives under
    ``<mount>/jobs/.../inputs/uploaded/...``).

    Access ladder: the path is resolved, confined to the active
    :class:`StorageLocation`'s mount under a ``drafts/`` or ``jobs/``
    namespace (configs / audit dumps / etc. are refused), and the
    caller must hold VIEW on **both** the URL's draft job and the job
    that *owns* the file (parsed from the path's ``job_<id>`` segment).
    The owning-job check is what stops one user reading another's
    job/draft files via a forged ``path`` — a previous-job file pick
    legitimately points at a different job the caller can also view.
    """
    mount_path, _location_id = await _resolve_active_storage_location(db)
    mount_root = Path(mount_path).resolve(strict=False)
    target, owning_job_id = _resolve_preview_path(path, mount_root)
    # Gate on BOTH the draft context (URL job_id) and the job that OWNS
    # the file (parsed from the path). The latter is the fix for
    # horizontal access — a previous-job pick legitimately points at
    # another job, which the caller must independently hold VIEW on.
    # Same gate as ``GET /api/jobs/{id}``; existence-hidden on no access.
    await job_service.get(db, job_id=job_id, actor_user_id=current_user.id)
    if owning_job_id != job_id:
        await job_service.get(db, job_id=owning_job_id, actor_user_id=current_user.id)
    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No file at that path — it may have been cleaned up.",
        )
    return FileResponse(target, filename=target.name)


@router.get("/jobs/{job_id}/cad-preview")
async def get_draft_cad_preview(
    job_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    path: Annotated[
        str,
        Query(
            description=(
                "Absolute path of the CAD file to preview. Must resolve "
                "under the active storage mount, below a drafts/ or jobs/ "
                "segment, and have a supported CAD extension "
                "(.stl/.stp/.step/.iges/.igs/.obj)."
            ),
            min_length=1,
        ),
    ],
) -> Response:
    """Server-side CAD → STL conversion for inline preview.

    Reads the source file from disk and returns binary STL bytes that
    the browser-side ``ui.scene.stl`` viewer consumes directly. STL
    inputs pass through unchanged; STP / STEP / IGES go through
    cadquery + OpenCascade; OBJ goes through trimesh. The result is
    cached server-side on ``(sha256(source_bytes), extension)`` so a
    file rendered in My Jobs and then re-rendered in Compare only
    pays the tessellation cost once.

    Refusals:

    - ``400`` — path can't be resolved, lives outside the mount,
      isn't under ``drafts/`` or ``jobs/``, or carries an
      unsupported extension. Also returned when the source is
      larger than :data:`matofx.tools.cad_convert.MAX_SOURCE_BYTES`.
    - ``404`` — file doesn't exist (e.g. cleaned up post-run).
    - ``503`` — the optional cadquery / trimesh dependencies aren't
      installed on this MATOFX server (the rest of the app keeps
      working; only CAD previews are degraded).
    """
    import asyncio as _asyncio  # noqa: PLC0415

    from matofx.tools.cad_convert import (  # noqa: PLC0415
        SUPPORTED_CAD_EXTENSIONS,
        CadConversionError,
        convert_to_stl,
    )

    mount_path, _location_id = await _resolve_active_storage_location(db)
    mount_root = Path(mount_path).resolve(strict=False)
    target, owning_job_id = _resolve_preview_path(path, mount_root)
    # Gate on BOTH the draft context (URL job_id) and the job that OWNS
    # the file — the latter fixes horizontal access for previous-job picks.
    await job_service.get(db, job_id=job_id, actor_user_id=current_user.id)
    if owning_job_id != job_id:
        await job_service.get(db, job_id=owning_job_id, actor_user_id=current_user.id)
    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No file at that path — it may have been cleaned up.",
        )

    source_ext = target.suffix.lower().lstrip(".")
    if source_ext not in SUPPORTED_CAD_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Inline 3-D preview isn't supported for .{source_ext} files. "
                f"Supported formats: {', '.join(sorted(SUPPORTED_CAD_EXTENSIONS))}."
            ),
        )

    content = await _asyncio.to_thread(target.read_bytes)
    try:
        stl_bytes = await _asyncio.to_thread(convert_to_stl, content, source_ext)
    except CadConversionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "CAD preview requires the optional 'cadquery' and 'trimesh' "
                "packages, which aren't installed on this server. Re-install "
                "MATOFX with those dependencies to enable inline previews."
            ),
        ) from exc

    return Response(
        content=stl_bytes,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{target.stem}.stl"',
        },
    )


@router.get("/jobs/{job_id}/outputs/{filename:path}/cad-preview")
async def get_output_cad_preview(
    job_id: int,
    filename: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    """Sibling of :func:`get_draft_cad_preview` for outputs.

    Clients viewing a job's outputs don't have the storage mount on
    hand (we don't leak filesystem info to the UI), so they can't
    build the absolute path the draft cad-preview endpoint wants.
    This route accepts the output filename relative to the working
    directory's ``outputs/`` (same shape the ``/outputs/{filename}``
    download endpoint uses) and resolves to disk server-side.
    Returns STL bytes regardless of source format, same as the
    draft endpoint.
    """
    import asyncio as _asyncio  # noqa: PLC0415

    from matofx.tools.cad_convert import (  # noqa: PLC0415
        SUPPORTED_CAD_EXTENSIONS,
        CadConversionError,
        convert_to_stl,
    )

    cleaned = filename.replace("\\", "/").strip("/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned.startswith("outputs/"):
        cleaned = cleaned[len("outputs/") :]
    if not cleaned or cleaned in (".", "..") or cleaned.startswith("../"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Output filename can't be empty or escape the outputs directory.",
        )
    job = await _accessible_outputs_job(db, job_service, job_id=job_id, current_user=current_user)
    _wd, _logs, outputs_dir = await _resolve_job_paths(db, job)
    target = (outputs_dir / cleaned).resolve()
    try:
        target.relative_to(outputs_dir.resolve())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Requested path is not inside the job's outputs directory.",
        ) from exc
    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No output file named {filename!r} for this job.",
        )

    source_ext = target.suffix.lower().lstrip(".")
    if source_ext not in SUPPORTED_CAD_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Inline 3-D preview isn't supported for .{source_ext} files. "
                f"Supported formats: {', '.join(sorted(SUPPORTED_CAD_EXTENSIONS))}."
            ),
        )

    content = await _asyncio.to_thread(target.read_bytes)
    try:
        stl_bytes = await _asyncio.to_thread(convert_to_stl, content, source_ext)
    except CadConversionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "CAD preview requires the optional 'cadquery' and 'trimesh' "
                "packages, which aren't installed on this server."
            ),
        ) from exc

    return Response(
        content=stl_bytes,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{target.stem}.stl"',
        },
    )


@router.delete("/jobs/{job_id}/uploads/{field_name}", response_model=JobResponse)
async def clear_draft_file_input(
    job_id: int,
    field_name: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> JobResponse:
    """Remove a draft upload for one field; clear the draft's ``inputs[field]``.

    Backs the workspace form's "Clear" / "X" button on file inputs.
    Removes the bytes from the draft staging area and wipes the
    matching ``Job.inputs`` value so the form re-renders in its
    no-file state and the user can pick a different mode (re-upload
    or pick from a previous job).
    """
    from matofx.tools.exec.uploads import remove_draft_upload  # noqa: PLC0415

    job = await job_service.get(db, job_id=job_id, actor_user_id=current_user.id)
    if job.state != "created":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This job is in state {job.state!r}; you can only edit file "
                "inputs while it's a draft."
            ),
        )
    mount_path, _location_id = await _resolve_active_storage_location(db)
    remove_draft_upload(
        mount_path=mount_path,
        environment=settings.environment,
        job_id=job_id,
        field_name=field_name,
    )
    new_inputs = dict(job.inputs or {})
    new_inputs.pop(field_name, None)
    job.inputs = new_inputs
    await db.flush()
    return _to_response(job)


# --- draft create / queue ---------------------------------------------


@router.post(
    "/scenarios/{scenario_id}/draft-jobs",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_or_get_draft_job(
    scenario_id: int,
    body: JobDraftCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> JobResponse:
    """Create a draft job for ``(scenario_id, tool_alias)`` — idempotent.

    Locked 2026-05-08: each scenario can hold at most one job per
    tool. This endpoint is the safe entry point for the workspace's
    "open the form for a new run" flow:

    - First call → creates a ``created``-state row and returns it.
    - Subsequent calls (same scenario + tool) → return the existing
      row, regardless of its current state.

    Idempotency means the workspace can call this every time the
    user lands on the form branch without producing ghost drafts.
    The unique constraint at the DB level (``uq_job_scenario_tool``)
    is the authoritative gate — this endpoint catches its
    :class:`DuplicateToolInScenarioError` translation and returns
    the existing job instead of erroring.

    When ``body.inputs`` is non-empty the new draft is seeded with
    those values (powers ``/job-search``'s "Use this run as a
    starting point" action). Idempotency is preserved: if a draft
    already exists for the pair the seed inputs are silently dropped
    and the existing draft is returned unchanged.
    """
    try:
        job = await job_service.create(
            db,
            actor_user_id=current_user.id,
            scenario_id=scenario_id,
            name=body.name,
            tool_alias=body.tool_alias,
            inputs=body.inputs or None,
        )
    except DuplicateToolInScenarioError as exc:
        # Workspace's expected "draft already exists" path — return
        # the existing job instead of letting the 409 reach the UI.
        if exc.existing_job_id is None:
            raise
        existing = await job_service.get(
            db, job_id=int(exc.existing_job_id), actor_user_id=current_user.id
        )
        return _to_response(existing)
    return _to_response(job)


@router.post("/jobs/{job_id}/draft-update", response_model=JobResponse)
async def update_draft_job(
    job_id: int,
    body: JobDraftUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> JobResponse:
    """Save form values on a draft job without queueing it for the runner.

    Backs the workspace's "Save Inputs" button — the user keeps their
    work-in-progress without committing to a run. Strictly DB-only:
    no working directory is staged, no launcher rendered, no inputs.json
    written. Submitting later via :func:`queue_draft_job` stages
    everything in one shot and writes the latest values to disk; this
    endpoint just persists the WIP form to the row.

    Refuses to touch a non-draft job (raises 4xx with the current state
    so the UI can explain "this job has already been submitted").
    """
    job = await job_service.update_draft(
        db,
        job_id=job_id,
        actor_user_id=current_user.id,
        inputs=body.inputs,
        name=body.name,
    )
    return _to_response(job)


@router.post("/jobs/{job_id}/queue", response_model=JobResponse)
async def queue_draft_job(
    job_id: int,
    body: JobQueueRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    notifier: Annotated[Notifier, Depends(get_notifier)],
) -> JobResponse:
    """Promote a ``created``-state draft to ``queued`` with the supplied inputs.

    Companion to :func:`create_or_get_draft_job` — the workspace
    creates a draft on form open, lets the user fill it in, and POSTs
    here to actually run it. The runner's pickup loop drives it
    forward from there.

    Goes through the approvals gate (§16.15): if the tool's captured
    descriptor has a ``pre``/``both`` policy the job is held in
    ``pending_approval`` (DB-only, not staged) and a response with
    ``state="pending_approval"`` is returned instead of ``queued``.

    Refusals: :class:`JobNotQueueableError` (job isn't in ``created``
    state — happens if the user submitted twice rapidly),
    :class:`AccessDeniedError` (no EDIT on the project),
    :class:`ToolNotRunnableError` / :class:`StorageLocationUnavailableError`
    (install state issues that the user-friendly error surface
    explains).
    """
    if body.name is not None:
        # Update the draft's name first so the queued job carries the
        # user's chosen name. Goes through ``get`` so the same access
        # checks fire as ``queue_for_runner``.
        draft = await job_service.get(db, job_id=job_id, actor_user_id=current_user.id)
        draft.name = body.name.strip()
    # Skip-queue: gated on a dedicated permission so an ordinary user
    # can't sneak past the FIFO queue by forging the flag. We don't
    # want to silently strip the flag from an unprivileged caller —
    # surfacing the 403 makes the failure mode obvious in the UI.
    if body.priority:
        from matofx.users.permissions import (  # noqa: PLC0415
            Permission,
            user_has_permission,
        )

        if not await user_has_permission(db, current_user.id, Permission.JOBS_SKIP_QUEUE):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "You don't have permission to skip the queue. "
                    "Ask an administrator to grant you 'jobs.skip_queue', "
                    "or submit without the priority flag to join the "
                    "regular queue."
                ),
            )
    job = await job_service.queue_or_hold_for_approval(
        db,
        job_id=job_id,
        actor_user_id=current_user.id,
        inputs=body.inputs,
        priority=body.priority,
        justification=body.justification or "",
    )
    await _notify_pre_approval_opened(db, notifier, job)
    return _to_response(job)


# --- create / list / get -----------------------------------------------


@router.post(
    "/scenarios/{scenario_id}/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_job(
    scenario_id: int,
    body: JobCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    notifier: Annotated[Notifier, Depends(get_notifier)],
) -> JobResponse:
    """Create a job in ``created`` state, then immediately queue it for the runner.

    The two-step (create + queue) is one HTTP call; if staging fails
    the create is rolled back atomically with the rest of the request.
    Honours the approvals gate (§16.15): a ``pre``/``both``-policy
    tool yields ``state="pending_approval"`` (held, not staged) rather
    than ``queued`` — this path is not an approval bypass.
    """
    job = await job_service.create(
        db,
        actor_user_id=current_user.id,
        scenario_id=scenario_id,
        name=body.name,
        tool_alias=body.tool_alias,
        inputs=body.inputs,
    )
    job = await job_service.queue_or_hold_for_approval(
        db,
        job_id=job.id,
        actor_user_id=current_user.id,
        inputs=body.inputs,
        justification=body.justification or "",
    )
    await _notify_pre_approval_opened(db, notifier, job)
    return _to_response(job)


@router.get(
    "/scenarios/{scenario_id}/jobs",
    response_model=list[JobResponse],
)
async def list_jobs(
    scenario_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[JobResponse]:
    """List jobs in a scenario, newest first."""
    jobs = await job_service.list_for_scenario(
        db, scenario_id=scenario_id, actor_user_id=current_user.id
    )
    return [_to_response(j) for j in jobs]


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> JobResponse:
    """Fetch one job's current status (incl. the pinned tool version)."""
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    job = await job_service.get(db, job_id=job_id, actor_user_id=current_user.id)
    tool_version: str | None = None
    if job.tool_version_id is not None:
        version_row = await db.get(ToolVersion, job.tool_version_id)
        if version_row is not None:
            tool_version = version_row.version
    return _to_response(job, tool_version=tool_version)


# --- logs / outputs / cancel -------------------------------------------


#: A chunk with no newline in it is normally a line still being written,
#: so it is held back until the newline arrives. Past this size that
#: reasoning stops applying — a progress bar redrawing with carriage
#: returns never writes one — and the chunk is delivered as-is rather
#: than growing for
#: ever behind a newline that is not coming.
_MAX_UNTERMINATED_BYTES = 64 * 1024


def _read_log_blocking(path: Path, since: int, *, tail: bool) -> tuple[str, int, bool]:
    """Bytes after ``since``. Returns ``(text, next_offset, restarted)``.

    Two things make this more than a ``seek`` + ``read``:

    - **Notice a shrink.** A file smaller than the offset asked for was
      rotated or rewritten, so resuming mid-file would splice two
      different runs together. Start again from zero and say so.
    - **In ``tail`` mode, cut on a newline.** UTF-8 is multi-byte, and a
      solver writing degree signs will eventually have one straddle the
      boundary between two polls. Decoding each chunk independently
      would put a replacement character on both sides of the split.
      Ending every chunk at the last newline means no character is ever
      divided, and no log line is ever delivered in two halves. What
      trails the final newline stays put and arrives next tick.

    ``tail`` is off by default because the other caller — a finished
    job's page — must show *every* byte. A crash message written without
    a trailing newline is exactly the line that matters, and holding it
    back to wait for a newline that will never come would lose it.
    """
    if not path.is_file():
        return "", since, False
    size = path.stat().st_size
    restarted = size < since
    start = 0 if restarted else since
    if size <= start:
        return "", start, restarted
    with path.open("rb") as handle:
        handle.seek(start)
        chunk = handle.read()
    if tail:
        cut = chunk.rfind(b"\n")
        if cut == -1:
            if len(chunk) < _MAX_UNTERMINATED_BYTES:
                return "", start, restarted
        else:
            chunk = chunk[: cut + 1]
    return chunk.decode("utf-8", errors="replace"), start + len(chunk), restarted


@router.get("/jobs/{job_id}/logs", response_model=JobLogsResponse)
async def get_job_logs(
    job_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    since_stdout: Annotated[int, Query(ge=0)] = 0,
    since_stderr: Annotated[int, Query(ge=0)] = 0,
    since_launcher: Annotated[int, Query(ge=0)] = 0,
    tail: Annotated[bool, Query()] = False,
) -> JobLogsResponse:
    """Return the launcher-captured stdout / stderr text for the job.

    **Whole file, or just what is new.** With no ``since_*`` this reads
    each log from the start — what a finished job's page wants, and what
    every caller got before tail mode existed. With ``since_*`` set to
    the offsets from the previous response, only the bytes written since
    come back, which is what a live job's page polls for every couple of
    seconds. Re-sending a growing CFD log on every tick is the difference
    between a few hundred bytes and a few hundred megabytes.

    ``tail=true`` additionally ends each chunk on a line boundary, so a
    live view never has to render half a line or half a character; the
    remainder arrives on the next poll. Leave it off for a finished job,
    where a final line written without a trailing newline still counts.

    ``truncated`` says a log shrank below the offset asked for, so the
    caller must clear its view instead of appending.
    """
    job = await job_service.get(db, job_id=job_id, actor_user_id=current_user.id)
    _wd, logs_dir, _outputs = await _resolve_job_paths(db, job)
    # ``launcher.log`` captures the auto-generated launcher's own
    # output (anything it prints / raises before — or instead of —
    # spawning the tool). For most successful jobs this is empty; when
    # it isn't, it's the only place a launcher startup failure shows
    # up. Surfaced alongside stdout/stderr so the user has a single
    # logs view rather than digging through the working dir on disk.
    read = functools.partial(_read_log_blocking, tail=tail)
    out, err, launch = await asyncio.gather(
        asyncio.to_thread(read, logs_dir / "stdout.log", since_stdout),
        asyncio.to_thread(read, logs_dir / "stderr.log", since_stderr),
        asyncio.to_thread(read, logs_dir / "launcher.log", since_launcher),
    )
    return JobLogsResponse(
        stdout=out[0],
        stderr=err[0],
        launcher=launch[0],
        stdout_offset=out[1],
        stderr_offset=err[1],
        launcher_offset=launch[1],
        truncated=out[2] or err[2] or launch[2],
    )


@router.get("/jobs/{job_id}/output-document", response_model=OutputsDocument)
async def get_job_output_document(
    job_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutputsDocument:
    """Return the parsed ``outputs.json`` for a finished job.

    Mirrors the way :class:`JobResponse` exposes ``inputs`` — clients
    don't have to know the on-disk filename or do their own parsing.
    Powers the Compare workspace, where every selected job's outputs
    feed the diff computation and the per-job cell renderer.

    - Job not in a terminal state (``complete`` / ``failed``) → 404.
      Callers should poll the status endpoint and only request the
      document once the job has settled.
    - Job is terminal but the document is missing or malformed → 200
      with an empty document. The Compare grid renders a "no outputs
      available" cell rather than aborting the whole comparison.
    - Permission: VIEW on the parent project (or 404). Additionally,
      while the job's results are withheld/pending a post-approval
      (``pending_results`` / ``results_withheld``) only eligible
      approvers and admins get the document — everyone else gets
      :class:`ResultsWithheldError` (403, carrying ``results_state``
      so Compare degrades to a "results withheld" cell, §16.15).
    """
    await _accessible_outputs_job(db, job_service, job_id=job_id, current_user=current_user)
    return await job_service.get_output_document(db, job_id=job_id)


@router.get("/jobs/{job_id}/outputs", response_model=JobOutputsListResponse)
async def list_job_outputs(
    job_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> JobOutputsListResponse:
    """List the filenames in the job's ``outputs/`` directory."""
    job = await _accessible_outputs_job(db, job_service, job_id=job_id, current_user=current_user)
    _wd, _logs, outputs_dir = await _resolve_job_paths(db, job)

    def _list_blocking() -> list[str]:
        if not outputs_dir.is_dir():
            return []
        return sorted(p.name for p in outputs_dir.iterdir() if p.is_file())

    names = await asyncio.to_thread(_list_blocking)
    return JobOutputsListResponse(filenames=names)


_MAX_FIELD_FRAME_VALUES = 8_000_000
"""Upper bound on ``n_nodes x n_frames`` the inline field render will serialise.

A few-thousand-node demo mesh over a dozen frames is tens of KB; multi-million-
node CFD meshes (or very long transients) are out of scope for the live browser
view, so the endpoint refuses them rather than pushing hundreds of MB."""

_MAX_FIELD_FRAMES = 30
"""Cap on timesteps the inline viewer loads. A long transient (e.g. a 61-step
mixing run) is otherwise ~45 MB of JSON + a multi-minute parse; 30 evenly-spaced
frames render smoothly and keep the full time range. Applied as an even-stride
down-sample (endpoints kept), not a head-slice, so the end of the series shows."""


def _even_stride(items: list[Any], max_n: int) -> list[Any]:
    """Down-sample a sequence to ``max_n`` evenly-spaced items, keeping both ends."""
    n = len(items)
    if n <= max_n or max_n < _MIN_DOWNSAMPLE_POINTS:
        return items
    step = (n - 1) / (max_n - 1)
    idx = sorted({round(i * step) for i in range(max_n)})
    return [items[i] for i in idx]


def _find_field_output(descriptor: Any, output_name: str) -> dict[str, Any] | None:
    """The descriptor's ``file_type == "field"`` output matching ``output_name``."""
    outputs = (descriptor or {}).get("outputs") if isinstance(descriptor, dict) else None
    for out in outputs or []:
        if (
            isinstance(out, dict)
            and out.get("name") == output_name
            and out.get("file_type") == "field"
        ):
            return out
    return None


def _resolve_field_frame_paths(
    working_dir: Path, outputs_dir: Path, spec: dict[str, Any]
) -> list[Path]:
    """On-disk snapshot path(s) for a field output (sorted; empty when none).

    A transient field expands its ``field_timesteps`` glob against the working
    dir (sorted so time order is stable); a steady field resolves its single
    ``filename`` under ``outputs/``. Both guard against escaping the job dir.
    """
    timesteps = spec.get("field_timesteps")
    if isinstance(timesteps, str) and timesteps:
        matches: list[Path] = []
        for match in sorted(working_dir.glob(timesteps)):
            if not match.is_file():
                continue
            try:
                match.resolve().relative_to(working_dir.resolve())
            except (ValueError, OSError):
                continue
            matches.append(match)
        return matches
    filename = spec.get("filename")
    if not isinstance(filename, str) or not filename:
        return []
    cleaned = filename.replace("\\", "/").strip("/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned.startswith("outputs/"):
        cleaned = cleaned[len("outputs/") :]
    if not cleaned or cleaned in (".", "..") or cleaned.startswith("../"):
        return []
    target = (outputs_dir / cleaned).resolve()
    try:
        target.relative_to(outputs_dir.resolve())
    except ValueError:
        return []
    return [target] if target.is_file() else []


def _load_field_frames(
    frame_paths: list[Path], file_format: str | None, array_name: str | None
) -> tuple[
    list[list[float]] | None, tuple[list[int], list[int], list[int]] | None, list[list[float]], int
]:
    """``(points, faces, frames, n_nodes)`` for a field output (off-thread).

    Each frame is the per-node field of one snapshot; frames off the common
    mesh (a different node count) are dropped. ``points``/``faces`` come from
    the first geometry-bearing snapshot, ``None`` for ``.npy``.
    """
    import numpy as np  # noqa: PLC0415

    from matofx.models.bundle import extract_field_geometry  # noqa: PLC0415
    from matofx.studies.surrogate.field import load_field_snapshot  # noqa: PLC0415

    fmt = (file_format or "").lower().lstrip(".")
    frames: list[list[float]] = []
    n_nodes: int | None = None
    for path in frame_paths:
        arr = np.asarray(
            load_field_snapshot(path, file_format=fmt, field_node_array_name=array_name or None),
            dtype=float,
        ).ravel()
        if n_nodes is None:
            n_nodes = int(arr.shape[0])
        if arr.shape[0] != n_nodes:
            continue
        frames.append([float(v) for v in arr.tolist()])
    if not frames or n_nodes is None:
        return None, None, [], 0
    points: list[list[float]] | None = None
    faces: tuple[list[int], list[int], list[int]] | None = None
    if fmt != "npy":
        geometry = extract_field_geometry(frame_paths[0])
        if geometry is not None:
            pts, i, j, k = geometry
            points, faces = pts, (i, j, k)
    return points, faces, frames, n_nodes


@router.get("/jobs/{job_id}/field-frames", response_model=JobFieldFramesResponse)
async def get_job_field_frames(
    job_id: int,
    output: Annotated[str, Query(min_length=1, max_length=64)],
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> JobFieldFramesResponse:
    """One run's field output parsed for inline render (ADR-0008 §4.4).

    Resolves the named ``field`` output from the job's captured descriptor,
    finds its retained snapshot(s) on disk (a steady file or a transient
    ``field_timesteps`` series), and returns the shared mesh + per-frame node
    values the Job page draws as a coloured ``Mesh3d`` (with a time slider when
    there's more than one frame). Same output-lockdown gate as the file
    download; the parse runs off the event loop.
    """
    job = await _accessible_outputs_job(db, job_service, job_id=job_id, current_user=current_user)
    tool_version = (
        await db.get(ToolVersion, job.tool_version_id) if job.tool_version_id is not None else None
    )
    descriptor = tool_version.descriptor_snapshot if tool_version is not None else None
    spec = _find_field_output(descriptor, output)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"This job has no field output named {output!r}.",
        )
    working_dir, _logs, outputs_dir = await _resolve_job_paths(db, job)
    frame_paths = await asyncio.to_thread(
        _resolve_field_frame_paths, working_dir, outputs_dir, spec
    )
    if not frame_paths:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No retained field snapshot files were found for this output.",
        )
    frame_paths = _even_stride(frame_paths, _MAX_FIELD_FRAMES)
    points, faces, frames, n_nodes = await asyncio.to_thread(
        _load_field_frames, frame_paths, spec.get("field_format"), spec.get("field_node_array_name")
    )
    if not frames:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The field snapshot file(s) couldn't be read.",
        )
    if n_nodes * len(frames) > _MAX_FIELD_FRAME_VALUES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"This field is too large for the inline viewer "
                f"({n_nodes:,} nodes x {len(frames)} frames). Download it instead."
            ),
        )
    has_geometry = points is not None and faces is not None
    return JobFieldFramesResponse(
        output=output,
        has_geometry=has_geometry,
        n_frames=len(frames),
        n_nodes=n_nodes,
        points=points or [],
        i=list(faces[0]) if faces else [],
        j=list(faces[1]) if faces else [],
        k=list(faces[2]) if faces else [],
        frames=frames,
    )


@router.get("/jobs/{job_id}/outputs/{filename:path}")
async def get_job_output(
    job_id: int,
    filename: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FileResponse:
    """Stream one file out of the job's ``outputs/`` directory.

    The path is interpreted permissively to match what tool authors
    actually write in the descriptor's ``filename`` field (documented
    as *"relative path to the file in the job's working dir"*):

    * ``"file.csv"`` — bare leaf under ``outputs/``.
    * ``"outputs/file.csv"`` — working-dir-relative, with the explicit
      ``outputs/`` prefix that the descriptor convention permits.
    * ``"outputs/sub/dir/file.csv"`` — nested under ``outputs/``.

    Backslashes are normalised so Windows-authored descriptors keep
    working over the HTTP API. The path-traversal guard at the
    bottom catches anything that resolves outside the job's
    ``outputs/`` directory regardless of how the caller spelled it.
    """
    cleaned = filename.replace("\\", "/").strip("/")
    # Strip a leading "outputs/" or "./outputs/" — the descriptor's
    # ``filename`` is documented as working-dir-relative, so authors
    # routinely write the redundant prefix. Strip in a loop so
    # "./outputs/" also drops cleanly.
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned.startswith("outputs/"):
        cleaned = cleaned[len("outputs/") :]
    if not cleaned or cleaned in (".", "..") or cleaned.startswith("../"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Output filename can't be empty or escape the outputs directory.",
        )
    job = await _accessible_outputs_job(db, job_service, job_id=job_id, current_user=current_user)
    _wd, _logs, outputs_dir = await _resolve_job_paths(db, job)
    target = (outputs_dir / cleaned).resolve()
    try:
        target.relative_to(outputs_dir.resolve())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Requested path is not inside the job's outputs directory.",
        ) from exc
    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No output file named {filename!r} for this job.",
        )
    # The HTTP filename hint to the browser uses only the leaf so the
    # downloaded file is named "file.csv", not "outputs%2Ffile.csv".
    return FileResponse(target, filename=Path(cleaned).name)


@router.get("/jobs/{job_id}/outputs.zip")
async def get_job_outputs_zip(
    job_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    """Return every file under ``outputs/`` packaged as a single ZIP.

    Mirrors the legacy "Download Output Files" button on the job page —
    flat archive (no directory prefix), one entry per file directly
    under ``outputs/``. Subdirectories under ``outputs/`` are walked
    recursively with their relative path preserved.
    """
    job = await _accessible_outputs_job(db, job_service, job_id=job_id, current_user=current_user)
    _wd, _logs, outputs_dir = await _resolve_job_paths(db, job)
    data = await asyncio.to_thread(_build_zip_bytes, outputs_dir)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="job_{job_id:07d}_outputs.zip"',
        },
    )


@router.get("/jobs/{job_id}/all-files.zip")
async def get_job_all_files_zip(
    job_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    """Return the whole working directory as a single ZIP.

    Mirrors the legacy "Download All Tool Files" button — preserves
    the inputs/outputs/logs sub-directory structure inside the archive
    so the user gets a faithful snapshot of what ran.
    """
    job = await _accessible_outputs_job(db, job_service, job_id=job_id, current_user=current_user)
    working_dir, _logs, _outputs = await _resolve_job_paths(db, job)
    data = await asyncio.to_thread(_build_zip_bytes, working_dir)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="job_{job_id:07d}_all_files.zip"',
        },
    )


def _build_zip_bytes(root: Path) -> bytes:
    """Build a ZIP of every file under ``root``, preserving relative paths."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    arcname = str(path.relative_to(root)).replace("\\", "/")
                    zf.write(path, arcname=arcname)
    return buf.getvalue()


@router.post("/jobs/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(
    job_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> JobResponse:
    """Mark the job for cancellation; the runner picks it up on its next tick."""
    job = await job_service.request_cancel(db, job_id=job_id, actor_user_id=current_user.id)
    return _to_response(job)


# --- cross-project visible-jobs (My Jobs workspace) -------------------


@router.get("/users/me/visible-jobs", response_model=list[VisibleJobResponse])
async def list_my_visible_jobs(
    db: Annotated[AsyncSession, Depends(get_db)],
    job_service: Annotated[JobService, Depends(get_job_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    q: Annotated[
        str | None,
        Query(description="Case-insensitive substring match against job name."),
    ] = None,
    tool_alias: Annotated[
        str | None,
        Query(description="Restrict to runs of this tool (workspace 'previous-job' popover)."),
    ] = None,
    state: Annotated[
        str | None,
        Query(description="Restrict to one job state (e.g. 'complete')."),
    ] = None,
    mine: Annotated[
        bool,
        Query(description="Restrict to jobs the caller submitted."),
    ] = False,
    limit: Annotated[
        int,
        Query(ge=1, le=500, description="Max jobs returned (default 50, hard cap 500)."),
    ] = 50,
    offset: Annotated[int, Query(ge=0, description="Pagination offset.")] = 0,
) -> list[VisibleJobResponse]:
    """Cross-project list of jobs the current user can see.

    Powers two surfaces in the My Jobs workspace:

    - **Global Job-ID picker** (search-as-you-type) — pass ``q`` for
      live filtering; the picker queries on every keystroke.
    - **Recent activity panel** (empty-state) — hit it without filters
      to get the user's newest accessible jobs across every project.

    Each entry carries the friendly **tool name** (resolved from the
    ``tool`` table — not the alias) plus full project + scenario
    names so the workspace can render rows like:

    ::

        0001234 · Heat transfer run · Glass packaging / Variant A · Complete

    in a single round-trip with no follow-up name lookups.
    """
    jobs = await job_service.list_visible_to_user(
        db,
        actor_user_id=current_user.id,
        query=q,
        tool_alias=tool_alias,
        state=state,
        mine=mine,
        limit=limit,
        offset=offset,
    )
    return await hydrate_visible_job_responses(db, jobs)


async def hydrate_visible_job_responses(
    db: AsyncSession, jobs: list[Job]
) -> list[VisibleJobResponse]:
    """Batch-resolve project / scenario / tool labels onto a list of jobs.

    Shared between :func:`list_my_visible_jobs` and the per-tool
    job-search endpoint so both surfaces hand the UI the same response
    shape (friendly project + scenario + tool *names*) without each
    paying for N+1 SELECTs. The caller is responsible for ACL-filtering
    the input job list — this helper only does label lookups and
    assumes every parent is fetchable.
    """
    if not jobs:
        return []

    scenario_ids = {j.scenario_id for j in jobs}
    scenarios_by_id: dict[int, Scenario] = {
        s.id: s
        for s in (await db.execute(select(Scenario).where(Scenario.id.in_(scenario_ids)))).scalars()
    }
    project_ids = {s.project_id for s in scenarios_by_id.values()}
    projects_by_id: dict[int, Project] = {
        p.id: p
        for p in (await db.execute(select(Project).where(Project.id.in_(project_ids)))).scalars()
    }
    tool_aliases = {j.tool_alias for j in jobs}
    tools_by_alias: dict[str, Tool] = {
        t.alias: t
        for t in (await db.execute(select(Tool).where(Tool.alias.in_(tool_aliases)))).scalars()
    }
    submitter_ids = {j.created_by_user_id for j in jobs}
    submitters_by_id: dict[int, User] = {
        u.id: u
        for u in (await db.execute(select(User).where(User.id.in_(submitter_ids)))).scalars()
    }

    out: list[VisibleJobResponse] = []
    for job in jobs:
        scenario = scenarios_by_id.get(job.scenario_id) if job.scenario_id is not None else None
        project = projects_by_id.get(scenario.project_id) if scenario is not None else None
        tool = tools_by_alias.get(job.tool_alias)
        submitter = submitters_by_id.get(job.created_by_user_id)
        # Defensive defaults: a missing parent is unlikely (the JOIN in
        # the service-layer query enforced existence) but the fallback
        # keeps the response shape stable rather than 500-ing. The
        # submitter row is the only one that can plausibly be missing
        # in normal use (account deletion is rare but allowed) — we
        # fall back to an empty display name so the column reads as
        # "(unknown)" rather than blowing up.
        out.append(
            VisibleJobResponse(
                id=job.id,
                name=job.name,
                state=job.state,
                created_at=job.created_at,
                project_id=project.id if project is not None else 0,
                project_name=project.name if project is not None else "",
                scenario_id=scenario.id if scenario is not None else 0,
                scenario_name=scenario.name if scenario is not None else "",
                tool_alias=job.tool_alias,
                tool_name=tool.name if tool is not None else job.tool_alias,
                submitter_user_id=job.created_by_user_id,
                submitter_name=_submitter_display_name(submitter),
                workflow_run_id=job.workflow_run_id,
            )
        )
    return out


def _submitter_display_name(user: User | None) -> str:
    """Friendly name for the Submitter column.

    Prefer ``full_name`` (the legacy split-and-rejoin display label
    populated when the user was created via the admin UI). Fall back
    to ``username`` so admins watching the column never see a blank
    cell in practice. Empty string when the user row has been deleted
    out from under us.
    """
    if user is None:
        return ""
    full = (user.full_name or "").strip()
    if full:
        return full
    return user.username or ""
