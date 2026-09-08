"""Admin → Send Notifications router (broadcast email + its template).

Gated end-to-end by :attr:`Permission.NOTIFICATIONS_SEND`. The
header/footer template is persisted through
:class:`~matofx.config.GeneralSettingsService` (the four
``broadcast_*`` catalog rows — deliberately absent from the General
Settings *page*, present in the catalog only to reuse its
validation / audit / image-storage machinery). The actual fan-out is
:class:`~matofx.notify.BroadcastService`.

Routes (all under ``/api``):

* ``GET    /admin/notifications/template`` — text + image-set flags.
* ``POST   /admin/notifications/template`` — save header/footer text.
* ``PUT    /admin/notifications/template/{slot}/image`` — upload image.
* ``DELETE /admin/notifications/template/{slot}/image`` — clear image.
* ``GET    /admin/notifications/template/{slot}/image`` — serve image
  (admin-only; for the compose page's live preview).
* ``POST   /admin/notifications/broadcast`` — send to the audience.
"""

from __future__ import annotations

import mimetypes
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api._uploads import read_upload_capped
from matofx.api.deps import (
    get_broadcast_service,
    get_current_user,
    get_current_user_role,
    get_db,
    get_general_settings_service,
    get_settings,
)
from matofx.api.schemas import (
    BroadcastAudienceOption,
    BroadcastAudienceOptions,
    BroadcastFailure,
    BroadcastResponse,
    NotificationTemplateResponse,
    NotificationTemplateUpdate,
)
from matofx.config import GeneralSettingsService, Settings
from matofx.config.general_service import load_general_settings
from matofx.notify import BroadcastAttachmentInput, BroadcastService
from matofx.notify.broadcast import PERMISSION_GROUP_LABELS, PERMISSION_GROUPS
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission
from matofx.users.roles import UserRole

_GROUP_AUDIENCES = ("teams", "roles", "curators")

router = APIRouter()

_Slot = Literal["header", "footer"]
_TEXT_KEY: dict[_Slot, str] = {
    "header": "broadcast_header_text",
    "footer": "broadcast_footer_text",
}
_IMAGE_KEY: dict[_Slot, str] = {
    "header": "broadcast_header_image",
    "footer": "broadcast_footer_image",
}


async def _require_send(session: AsyncSession, actor_user_id: int) -> None:
    """Raise 403 unless the actor holds ``notifications.send``."""
    if not await user_has_permission(session, actor_user_id, Permission.NOTIFICATIONS_SEND):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You don't have permission to send notifications "
                "(notifications.send). Ask an administrator if you need access."
            ),
        )


def _as_role(name: str) -> UserRole:
    """Narrow the role string to the :data:`UserRole` Literal (runtime no-op)."""
    return name  # type: ignore[return-value]


def _slot_or_404(slot: str) -> _Slot:
    if slot not in ("header", "footer"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown template slot; expected 'header' or 'footer'.",
        )
    return slot  # type: ignore[return-value]


async def _template_response(session: AsyncSession) -> NotificationTemplateResponse:
    """Build the template snapshot from the effective catalog values."""
    snap = await load_general_settings(session)
    return NotificationTemplateResponse(
        header_text=str(snap.get("broadcast_header_text") or ""),
        footer_text=str(snap.get("broadcast_footer_text") or ""),
        header_image=bool(snap.get("broadcast_header_image")),
        footer_image=bool(snap.get("broadcast_footer_image")),
    )


@router.get(
    "/admin/notifications/template",
    response_model=NotificationTemplateResponse,
)
async def get_notification_template(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> NotificationTemplateResponse:
    """Return the persisted header/footer template."""
    await _require_send(db, current_user.id)
    return await _template_response(db)


@router.post(
    "/admin/notifications/template",
    response_model=NotificationTemplateResponse,
)
async def set_notification_template(
    body: NotificationTemplateUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[GeneralSettingsService, Depends(get_general_settings_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    current_role: Annotated[str, Depends(get_current_user_role)],
) -> NotificationTemplateResponse:
    """Save the header + footer text blocks."""
    await _require_send(db, current_user.id)
    role = _as_role(current_role)
    await service.set(
        db,
        actor_user_id=current_user.id,
        actor_role=role,
        key="broadcast_header_text",
        value=body.header_text,
    )
    await service.set(
        db,
        actor_user_id=current_user.id,
        actor_role=role,
        key="broadcast_footer_text",
        value=body.footer_text,
    )
    return await _template_response(db)


@router.put(
    "/admin/notifications/template/{slot}/image",
    response_model=NotificationTemplateResponse,
)
async def upload_notification_template_image(
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[GeneralSettingsService, Depends(get_general_settings_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    current_role: Annotated[str, Depends(get_current_user_role)],
    settings: Annotated[Settings, Depends(get_settings)],
    slot: Annotated[str, Path()],
    file: Annotated[UploadFile, File()],
) -> NotificationTemplateResponse:
    """Upload the header or footer image. Mime/size validated in the service."""
    await _require_send(db, current_user.id)
    resolved = _slot_or_404(slot)
    data = await read_upload_capped(
        file,
        max_bytes=settings.branding.header_icon_max_upload_bytes,
        what="image",
    )
    await service.set_image(
        db,
        actor_user_id=current_user.id,
        actor_role=_as_role(current_role),
        key=_IMAGE_KEY[resolved],
        filename=file.filename or _IMAGE_KEY[resolved],
        data=data,
        mime_type=file.content_type or "application/octet-stream",
        branding_dir=settings.branding.icons_dir,
        max_bytes=settings.branding.header_icon_max_upload_bytes,
    )
    return await _template_response(db)


@router.delete(
    "/admin/notifications/template/{slot}/image",
    response_model=NotificationTemplateResponse,
)
async def clear_notification_template_image(
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[GeneralSettingsService, Depends(get_general_settings_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    current_role: Annotated[str, Depends(get_current_user_role)],
    slot: Annotated[str, Path()],
) -> NotificationTemplateResponse:
    """Clear the header or footer image (resets the row to no override)."""
    await _require_send(db, current_user.id)
    resolved = _slot_or_404(slot)
    await service.reset(
        db,
        actor_user_id=current_user.id,
        actor_role=_as_role(current_role),
        key=_IMAGE_KEY[resolved],
    )
    return await _template_response(db)


@router.get(
    "/admin/notifications/template/{slot}/image",
    include_in_schema=False,
)
async def get_notification_template_image(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    slot: Annotated[str, Path()],
) -> FileResponse:
    """Serve the saved image bytes for the compose page's preview.

    Admin-only (unlike the public branding route) — the broadcast
    template isn't public-facing. 404 when no image is saved or the
    file is missing from disk.
    """
    await _require_send(db, current_user.id)
    resolved = _slot_or_404(slot)
    snap = await load_general_settings(db)
    basename = snap.get(_IMAGE_KEY[resolved])
    if not isinstance(basename, str) or not basename:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    path = settings.branding.icons_dir / basename
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    media_type, _ = mimetypes.guess_type(str(path))
    return FileResponse(path, media_type=media_type or "application/octet-stream")


@router.get(
    "/admin/notifications/audience-options",
    response_model=BroadcastAudienceOptions,
)
async def get_broadcast_audience_options(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BroadcastAudienceOptions:
    """List the teams / roles / curator groups a broadcast can target.

    Gated by ``notifications.send`` so the compose page can fill its
    multiselects without the broader ``teams.manage`` / ``users.manage``
    permissions those entities' own admin endpoints require.
    """
    from sqlalchemy import select as _select  # noqa: PLC0415

    from matofx.users.role_orm import Role, RolePermission  # noqa: PLC0415
    from matofx.users.team_orm import Team  # noqa: PLC0415

    await _require_send(db, current_user.id)

    # "World" is the seeded universal-visibility marker (migration 0014),
    # not a real team — and equivalent to "Everyone" anyway. Hide it.
    team_rows = (
        await db.execute(
            _select(Team.id, Team.name).where(Team.name != "World").order_by(Team.name)
        )
    ).all()

    # Roles whose own picker is "Specific curators and approvers" — the
    # capability roles that grant one of the curate/approve permissions —
    # are excluded from the access-rights-groups list to avoid a confusing
    # duplicate (and the ugly snake-case rendering, e.g. "Vv Curator").
    group_slugs = set(PERMISSION_GROUPS.values())
    excluded_role_ids = {
        rid
        for (rid,) in (
            await db.execute(
                _select(RolePermission.role_id).where(
                    RolePermission.permission.in_(group_slugs)
                )
            )
        ).all()
    }
    role_rows = (await db.execute(_select(Role.id, Role.name, Role.kind))).all()
    roles = [
        (rid, name)
        for rid, name, kind in role_rows
        if not (kind == "capability" and rid in excluded_role_ids)
    ]
    roles.sort(key=lambda r: _role_sort_key(r[1]))

    return BroadcastAudienceOptions(
        teams=[BroadcastAudienceOption(id=str(tid), label=name) for tid, name in team_rows],
        roles=[
            BroadcastAudienceOption(id=str(rid), label=_humanise_role(name))
            for rid, name in roles
        ],
        curators=[
            BroadcastAudienceOption(id=key, label=label)
            for key, label in PERMISSION_GROUP_LABELS.items()
        ],
    )


#: Built-in base roles in descending-rights order; everything else sorts
#: after these, alphabetically by label.
_BASE_ROLE_ORDER: tuple[str, ...] = (
    "super_user",
    "administrator",
    "beta_tester",
    "regular_user",
)


def _role_sort_key(name: str) -> tuple[int, str]:
    """Sort the four built-in base roles by rank, then custom roles A→Z."""
    try:
        return (0, f"{_BASE_ROLE_ORDER.index(name):02d}")
    except ValueError:
        return (1, _humanise_role(name).lower())


def _humanise_role(name: str) -> str:
    """Render a stored snake-case role name in plain English (Title Case)."""
    return name.replace("_", " ").title() if name else name


@router.post(
    "/admin/notifications/broadcast",
    response_model=BroadcastResponse,
)
async def send_broadcast(
    db: Annotated[AsyncSession, Depends(get_db)],
    broadcast: Annotated[BroadcastService, Depends(get_broadcast_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    audience: Annotated[str, Form()],
    subject: Annotated[str, Form()],
    body: Annotated[str, Form()],
    group_kind: Annotated[str, Form()] = "",
    selection: Annotated[list[str] | None, Form()] = None,
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> BroadcastResponse:
    """Send the composed message, wrapped in the saved template.

    ``audience`` is the base scope (``all`` / ``active``). An optional
    ``group_kind`` (``teams`` / ``roles`` / ``curators``) narrows it to
    the entities in ``selection``; recipients are resolved server-side by
    :class:`BroadcastService`.
    """
    await _require_send(db, current_user.id)

    chosen = [s for s in (selection or []) if str(s).strip()]
    if audience not in ("all", "active"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Audience must be 'all' or 'active'.",
        )
    if group_kind and group_kind not in _GROUP_AUDIENCES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Group must be 'teams', 'roles', or 'curators'.",
        )
    if group_kind in _GROUP_AUDIENCES and not chosen:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Select at least one team, access-rights group, or "
                "curator group for this audience."
            ),
        )
    if not subject.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A subject is required.",
        )
    if not body.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A message body is required.",
        )

    attachments: list[BroadcastAttachmentInput] = []
    for upload in files or []:
        raw = await upload.read()
        if not raw:
            continue
        attachments.append(
            BroadcastAttachmentInput(
                filename=upload.filename or "attachment",
                data=raw,
                content_type=upload.content_type or "application/octet-stream",
            )
        )

    # ``audience`` is narrowed to the Audience literal by the validation above.
    result = await broadcast.send_broadcast(
        db,
        actor_user_id=current_user.id,
        audience=audience,  # type: ignore[arg-type]
        subject=subject,
        body=body,
        attachments=attachments,
        group_kind=group_kind,
        selection=chosen,
    )
    return BroadcastResponse(
        disabled=result.disabled,
        attempted=result.attempted,
        sent=result.sent,
        failed=[BroadcastFailure(email=f["email"], reason=f["reason"]) for f in result.failed],
    )
