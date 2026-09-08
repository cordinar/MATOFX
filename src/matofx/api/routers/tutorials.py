"""Tutorials router — admin upload/manage + user-facing fetch.

Two surfaces ride this router under different prefixes:

- ``/api/admin/tutorials*`` — gated on
  :attr:`Permission.TUTORIALS_MANAGE`. Upload, activate / deactivate,
  delete.
- ``/api/tutorials*`` — every authenticated user can list active
  tutorials and download the file bytes. The home page reads from
  here; the admin page reads from the admin-prefixed surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api._uploads import read_upload_capped
from matofx.api.deps import (
    get_current_user,
    get_db,
    get_tutorial_service,
)
from matofx.api.schemas import TutorialResponse
from matofx.tools.orm import Tool
from matofx.tutorials.errors import (
    ArchivedTutorialError,
    InvalidExtensionError,
    InvalidMediaTypeError,
    ScopeConflictError,
    TutorialNotFoundError,
    UnknownToolError,
    UnknownWorkflowError,
    UploadTooLargeError,
)
from matofx.tutorials.orm import Tutorial
from matofx.tutorials.service import TutorialService
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission
from matofx.workflows.orm import Workflow

router = APIRouter()


# ---------- helpers ------------------------------------------------------


async def _require_manage(session: AsyncSession, actor_user_id: int) -> None:
    """Raise 403 if the caller can't manage tutorials."""
    if not await user_has_permission(session, actor_user_id, Permission.TUTORIALS_MANAGE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You don't have permission to manage tutorials "
                f"({Permission.TUTORIALS_MANAGE.value})."
            ),
        )


async def _row_to_response(
    session: AsyncSession,
    row: Tutorial,
) -> TutorialResponse:
    """Render a :class:`Tutorial` row as the wire shape.

    Looks up the linked tool's / workflow's pretty name + the
    uploader's display label inline so the UI doesn't fan out to
    /tools, /workflows, or /users.
    """
    tool_name: str | None = None
    if row.tool_alias is not None:
        tool = (
            await session.execute(select(Tool).where(Tool.alias == row.tool_alias))
        ).scalar_one_or_none()
        if tool is not None:
            tool_name = tool.name

    workflow_name: str | None = None
    if row.workflow_alias is not None:
        workflow = (
            await session.execute(select(Workflow).where(Workflow.alias == row.workflow_alias))
        ).scalar_one_or_none()
        if workflow is not None:
            workflow_name = workflow.name

    uploader_label: str | None = None
    if row.uploaded_by_user_id is not None:
        user = await session.get(User, row.uploaded_by_user_id)
        if user is not None:
            display = user.full_name or user.username
            uploader_label = f"{display} ({user.username})"

    return TutorialResponse(
        id=row.id,
        tool_alias=row.tool_alias,
        tool_name=tool_name,
        workflow_alias=row.workflow_alias,
        workflow_name=workflow_name,
        media_type=row.media_type,  # type: ignore[arg-type]
        version=row.version,
        title=row.title,
        description=row.description,
        original_filename=row.original_filename,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        is_active=row.is_active,
        lineage_id=row.lineage_id,
        is_archived=row.superseded_at is not None,
        uploaded_by_user_id=row.uploaded_by_user_id,
        uploaded_by_label=uploader_label,
        uploaded_at=row.uploaded_at,
        created_at=row.created_at,
        download_url=f"/api/tutorials/{row.id}/file",
    )


# Domain error → HTTP status. First matching type wins; anything not
# listed funnels to a 500 with the str() of the exception.
_ERROR_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (TutorialNotFoundError, 404),
    (ArchivedTutorialError, 409),
    (UnknownToolError, 400),
    (UnknownWorkflowError, 400),
    (ScopeConflictError, 400),
    (InvalidMediaTypeError, 400),
    (InvalidExtensionError, 400),
    (UploadTooLargeError, 413),
)


def _map_domain_error(exc: Exception) -> HTTPException:
    """Translate :mod:`matofx.tutorials.errors` to HTTPException."""
    detail = getattr(exc, "detail", str(exc))
    for err_type, code in _ERROR_STATUS:
        if isinstance(exc, err_type):
            return HTTPException(status_code=code, detail=detail)
    return HTTPException(status_code=500, detail=str(exc))


# ---------- admin endpoints ---------------------------------------------


@router.get(
    "/admin/tutorials",
    response_model=list[TutorialResponse],
)
async def admin_list_tutorials(
    db: Annotated[AsyncSession, Depends(get_db)],
    tutorial_service: Annotated[TutorialService, Depends(get_tutorial_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[TutorialResponse]:
    """Return every tutorial — active + inactive — newest first."""
    await _require_manage(db, current_user.id)
    rows = await tutorial_service.list_all(db)
    return [await _row_to_response(db, r) for r in rows]


@router.post(
    "/admin/tutorials",
    response_model=TutorialResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_upload_tutorial(
    db: Annotated[AsyncSession, Depends(get_db)],
    tutorial_service: Annotated[TutorialService, Depends(get_tutorial_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    media_type: Annotated[
        str,
        Form(description="'document' or 'video'."),
    ],
    title: Annotated[str, Form(description="User-facing tutorial title.")],
    file: Annotated[UploadFile, File(description="The PDF or video file.")],
    tool_alias: Annotated[
        str | None,
        Form(description="Tool the tutorial belongs to. Blank for general or workflow-scoped."),
    ] = None,
    workflow_alias: Annotated[
        str | None,
        Form(description="Workflow the tutorial belongs to. Blank for general or tool-scoped."),
    ] = None,
    version: Annotated[str, Form(description="Free-form version label.")] = "1",
    description: Annotated[
        str,
        Form(description="Optional plain-English description for the admin list."),
    ] = "",
    activate: Annotated[
        bool,
        Form(description="When true, immediately mark active (cascades inactive on siblings)."),
    ] = True,
) -> TutorialResponse:
    """Upload a new tutorial. Requires ``tutorials.manage``."""
    await _require_manage(db, current_user.id)

    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="Please attach a file to upload.",
        )

    contents = await read_upload_capped(
        file,
        max_bytes=tutorial_service.max_upload_bytes,
        what="tutorial",
    )
    if not contents:
        raise HTTPException(
            status_code=400,
            detail="The uploaded file is empty.",
        )

    cleaned_tool_alias = (tool_alias or "").strip() or None
    cleaned_workflow_alias = (workflow_alias or "").strip() or None
    try:
        row = await tutorial_service.create_upload(
            db,
            actor_user_id=current_user.id,
            tool_alias=cleaned_tool_alias,
            workflow_alias=cleaned_workflow_alias,
            media_type=media_type.strip(),
            version=version,
            title=title,
            description=description,
            original_filename=file.filename,
            mime_type=file.content_type or "application/octet-stream",
            contents=contents,
            activate=activate,
        )
    except Exception as exc:
        raise _map_domain_error(exc) from exc
    return await _row_to_response(db, row)


@router.post(
    "/admin/tutorials/{tutorial_id}/activate",
    response_model=TutorialResponse,
)
async def admin_activate_tutorial(
    tutorial_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    tutorial_service: Annotated[TutorialService, Depends(get_tutorial_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> TutorialResponse:
    """Activate ``tutorial_id`` (cascades inactive on tool-scoped siblings)."""
    await _require_manage(db, current_user.id)
    try:
        row = await tutorial_service.activate(
            db, tutorial_id=tutorial_id, actor_user_id=current_user.id
        )
    except Exception as exc:
        raise _map_domain_error(exc) from exc
    return await _row_to_response(db, row)


@router.post(
    "/admin/tutorials/{tutorial_id}/deactivate",
    response_model=TutorialResponse,
)
async def admin_deactivate_tutorial(
    tutorial_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    tutorial_service: Annotated[TutorialService, Depends(get_tutorial_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> TutorialResponse:
    """Flip ``is_active=False`` on a tutorial."""
    await _require_manage(db, current_user.id)
    try:
        row = await tutorial_service.deactivate(
            db, tutorial_id=tutorial_id, actor_user_id=current_user.id
        )
    except Exception as exc:
        raise _map_domain_error(exc) from exc
    return await _row_to_response(db, row)


@router.post(
    "/admin/tutorials/{tutorial_id}/update",
    response_model=TutorialResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_update_tutorial(
    tutorial_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    tutorial_service: Annotated[TutorialService, Depends(get_tutorial_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    file: Annotated[UploadFile, File(description="The replacement PDF or video file.")],
    title: Annotated[str, Form(description="Title for the new version.")] = "",
    version: Annotated[str, Form(description="Free-form version label for the new version.")] = "",
    description: Annotated[
        str,
        Form(description="Optional plain-English description for the new version."),
    ] = "",
) -> TutorialResponse:
    """Publish a new version of a tutorial, archiving the old one.

    Scope and media type are inherited from the source tutorial — an
    update replaces like with like. The previous version is kept as
    archived history (never deleted). Requires ``tutorials.manage``.
    """
    await _require_manage(db, current_user.id)

    if not file.filename:
        raise HTTPException(status_code=400, detail="Please attach a file to upload.")
    contents = await read_upload_capped(
        file,
        max_bytes=tutorial_service.max_upload_bytes,
        what="tutorial",
    )
    if not contents:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    try:
        row = await tutorial_service.update_version(
            db,
            tutorial_id=tutorial_id,
            actor_user_id=current_user.id,
            version=version,
            title=title,
            description=description,
            original_filename=file.filename,
            mime_type=file.content_type or "application/octet-stream",
            contents=contents,
        )
    except Exception as exc:
        raise _map_domain_error(exc) from exc
    return await _row_to_response(db, row)


# ---------- user-facing endpoints ---------------------------------------


@router.get(
    "/tutorials",
    response_model=list[TutorialResponse],
)
async def list_active_tutorials(
    db: Annotated[AsyncSession, Depends(get_db)],
    tutorial_service: Annotated[TutorialService, Depends(get_tutorial_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[TutorialResponse]:
    """Return every *active* tutorial — general + per-tool — for the home page.

    Returned in a stable order: general tutorials first, then per-tool
    grouped alphabetically, with the document for each tool listed
    before its video.
    """
    del current_user  # auth-gated only; no per-user scoping.
    rows = await tutorial_service.list_active_for_user(db)
    return [await _row_to_response(db, r) for r in rows]


@router.get("/tutorials/{tutorial_id}/file")
async def stream_tutorial_file(
    tutorial_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    tutorial_service: Annotated[TutorialService, Depends(get_tutorial_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FileResponse:
    """Stream the bytes of one tutorial.

    Inactive tutorials are still fetchable by an admin who knows the
    id (handy for previewing before activation) — the user-facing
    list endpoint hides them, which is what surfaces on the home
    page. Browsers receive ``Content-Disposition: inline`` so PDFs +
    common video formats render in the new tab the home-page link
    opens; the browser's "download" affordance still works from
    there if the user prefers.
    """
    del current_user  # auth-gated; the row id is the access key.
    try:
        row = await tutorial_service.get(db, tutorial_id)
    except TutorialNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc

    path = Path(row.storage_path)
    if not path.is_file():
        raise HTTPException(
            status_code=410,
            detail=(
                "The tutorial file is no longer on disk. Ask an administrator to re-upload it."
            ),
        )
    return FileResponse(
        path=path,
        media_type=row.mime_type,
        filename=row.original_filename,
        content_disposition_type="inline",
    )
