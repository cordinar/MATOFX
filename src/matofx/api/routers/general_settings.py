"""Admin → General Settings router.

Mirrors the shape of the runner ``/admin/runtime-settings`` routes
in :mod:`matofx.api.routers.admin` but parameterised for the
General Settings catalog (session policy, password rules,
notifications, housekeeping, branding). Per-row role-gating is
handled inside :class:`matofx.config.GeneralSettingsService`; this
router just plumbs the caller's resolved role through.

Routes:

* ``GET  /api/admin/general-settings`` — snapshot of catalog +
  values + active delegations.
* ``POST /api/admin/general-settings/{key}`` — set a value.
* ``POST /api/admin/general-settings/{key}/reset`` — restore the default.
* ``POST /api/admin/general-settings/{key}/delegate`` — super-user only;
  grant / revoke per-row delegation to a lower-tier role.
* ``PUT  /api/admin/general-settings/{key}/image`` — upload an image
  (header icon or favicon). Multipart form, ``file`` field.
* ``GET  /api/admin/inactive-users/preview?threshold_days=N`` —
  preview of users the inactivity sweep would auto-disable at the
  proposed threshold. Powers the save-flow confirm modal.

The image upload route is separate so the standard set route can
keep using JSON. Validation (mime type, max size) lives in the
service so both routes apply the same rules.
"""

from __future__ import annotations

import datetime as _dt
import mimetypes
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Path,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api._uploads import read_upload_capped
from matofx.api.deps import (
    get_current_user,
    get_current_user_role,
    get_db,
    get_general_settings_service,
    get_settings,
)
from matofx.api.schemas import (
    AdminAutoDisablePreviewResponse,
    AdminAutoDisablePreviewRow,
    AdminGeneralSettingCatalogEntry,
    AdminGeneralSettingDelegation,
    AdminGeneralSettingsResponse,
    AdminGeneralSettingValue,
)
from matofx.config import GENERAL_SETTINGS_CATALOG, GeneralSettingsService, Settings
from matofx.config.general_service import (
    load_general_setting_delegations,
    load_general_settings,
)
from matofx.users.inactive_sweeper import preview_inactive_users
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission
from matofx.users.roles import UserRole

router = APIRouter()


_BRANDING_IMAGE_KEYS: frozenset[str] = frozenset({"header_icon_path", "favicon_path"})


# ---------------------------------------------------------------------------
# Catalog snapshot helper
# ---------------------------------------------------------------------------


def _build_catalog() -> list[AdminGeneralSettingCatalogEntry]:
    """Serialise :data:`GENERAL_SETTINGS_CATALOG` into the wire shape.

    The catalog never changes at runtime within a single process so
    this could be cached at import time. We rebuild it per request
    for now because the cost is trivial (32 dataclass-to-pydantic
    conversions) and a future feature-flag-gated catalog row would
    surface cleanly without an invalidation step.
    """
    return [
        AdminGeneralSettingCatalogEntry(
            key=spec.key,
            kind=spec.kind,
            label=spec.label,
            description=spec.description,
            default=spec.default,
            requires_role=spec.requires_role,
            delegatable_to=spec.delegatable_to,
            unit=spec.unit,
            min_value=spec.min_value,
            max_value=spec.max_value,
            choices=list(spec.choices) if spec.choices is not None else None,
            accepted_mime_types=(
                list(spec.accepted_mime_types) if spec.accepted_mime_types is not None else None
            ),
            redacted=spec.redacted,
            pending_restart=spec.pending_restart,
            zero_means_never=spec.zero_means_never,
        )
        for spec in GENERAL_SETTINGS_CATALOG.values()
    ]


async def _build_response(
    db: AsyncSession,
    service: GeneralSettingsService,
    *,
    actor_user_id: int,
    actor_role: UserRole,
) -> AdminGeneralSettingsResponse:
    """Assemble the full GET response: catalog + values + delegations."""
    values = await service.get_all(db, actor_user_id=actor_user_id, actor_role=actor_role)
    delegations = await load_general_setting_delegations(db)
    return AdminGeneralSettingsResponse(
        catalog=_build_catalog(),
        values=values,
        delegations=delegations,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/admin/general-settings",
    response_model=AdminGeneralSettingsResponse,
)
async def get_general_settings(
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[GeneralSettingsService, Depends(get_general_settings_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    current_role: Annotated[str, Depends(get_current_user_role)],
) -> AdminGeneralSettingsResponse:
    """Snapshot every General Settings key + the catalog.

    Any authenticated user can read; the service redacts secret
    values and the UI handles "lock this row when caller can't
    edit" client-side using ``catalog[*].requires_role``.
    """
    return await _build_response(
        db, service, actor_user_id=current_user.id, actor_role=_as_role(current_role)
    )


@router.post(
    "/admin/general-settings/{key}",
    response_model=AdminGeneralSettingsResponse,
)
async def set_general_setting(
    body: AdminGeneralSettingValue,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[GeneralSettingsService, Depends(get_general_settings_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    current_role: Annotated[str, Depends(get_current_user_role)],
    settings: Annotated[Settings, Depends(get_settings)],
    key: Annotated[str, Path()],
) -> AdminGeneralSettingsResponse:
    """Set one key to ``body.value``. Returns the full updated snapshot."""
    await service.set(
        db,
        actor_user_id=current_user.id,
        actor_role=_as_role(current_role),
        key=key,
        value=body.value,
    )
    await _maybe_refresh_branding(db, key, settings)
    return await _build_response(
        db, service, actor_user_id=current_user.id, actor_role=_as_role(current_role)
    )


@router.post(
    "/admin/general-settings/{key}/reset",
    response_model=AdminGeneralSettingsResponse,
)
async def reset_general_setting(
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[GeneralSettingsService, Depends(get_general_settings_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    current_role: Annotated[str, Depends(get_current_user_role)],
    settings: Annotated[Settings, Depends(get_settings)],
    key: Annotated[str, Path()],
) -> AdminGeneralSettingsResponse:
    """Reset one key to its catalog default."""
    await service.reset(
        db,
        actor_user_id=current_user.id,
        actor_role=_as_role(current_role),
        key=key,
    )
    await _maybe_refresh_branding(db, key, settings)
    return await _build_response(
        db, service, actor_user_id=current_user.id, actor_role=_as_role(current_role)
    )


@router.post(
    "/admin/general-settings/{key}/delegate",
    response_model=AdminGeneralSettingsResponse,
)
async def delegate_general_setting(
    body: AdminGeneralSettingDelegation,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[GeneralSettingsService, Depends(get_general_settings_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    current_role: Annotated[str, Depends(get_current_user_role)],
    key: Annotated[str, Path()],
) -> AdminGeneralSettingsResponse:
    """Super-user only: grant or revoke per-row delegation.

    Passing ``delegated_to_role=null`` clears the grant. The role
    string (when non-null) must match the catalog spec's
    ``delegatable_to`` exactly — the service rejects mismatches
    with the standard access-denied error so the grant can't widen
    beyond what the catalog advertises.
    """
    await service.set_delegation(
        db,
        actor_user_id=current_user.id,
        actor_role=_as_role(current_role),
        key=key,
        delegated_to_role=body.delegated_to_role,
    )
    return await _build_response(
        db, service, actor_user_id=current_user.id, actor_role=_as_role(current_role)
    )


@router.put(
    "/admin/general-settings/{key}/image",
    response_model=AdminGeneralSettingsResponse,
)
async def upload_general_setting_image(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[GeneralSettingsService, Depends(get_general_settings_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    current_role: Annotated[str, Depends(get_current_user_role)],
    settings: Annotated[Settings, Depends(get_settings)],
    key: Annotated[str, Path()],
    file: Annotated[UploadFile, File()],
) -> AdminGeneralSettingsResponse:
    """Upload an image for an ``image``-kind setting (header icon or favicon).

    Validates mime type and size against the catalog spec + the per-
    image cap from :class:`matofx.config.BrandingSettings`. The
    stored basename ends up in the DB row; the bytes land under the
    configured ``branding.icons_dir`` directory.
    """
    # Resolve the cap *before* reading so the body is never fully buffered
    # for an oversized upload (memory-exhaustion guard); ``set_image`` still
    # re-checks the length as defence in depth.
    max_bytes = _image_upload_cap(key, settings)
    data = await read_upload_capped(
        file, max_bytes=max_bytes, request=request, what="image"
    )
    await service.set_image(
        db,
        actor_user_id=current_user.id,
        actor_role=_as_role(current_role),
        key=key,
        filename=file.filename or key,
        data=data,
        mime_type=file.content_type or "application/octet-stream",
        branding_dir=settings.branding.icons_dir,
        max_bytes=max_bytes,
    )
    await _maybe_refresh_branding(db, key, settings)
    return await _build_response(
        db, service, actor_user_id=current_user.id, actor_role=_as_role(current_role)
    )


@router.get(
    "/admin/inactive-users/preview",
    response_model=AdminAutoDisablePreviewResponse,
)
async def preview_inactive_user_sweep(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    threshold_days: Annotated[
        int, Query(ge=1, le=3650, description="Proposed threshold in days.")
    ],
    limit: Annotated[
        int, Query(ge=1, le=500, description="Max preview rows to return.")
    ] = 100,
) -> AdminAutoDisablePreviewResponse:
    """Preview the users the inactivity sweep would auto-disable at ``threshold_days``.

    Powers the General Settings save-flow confirm modal so the
    operator sees an exact count plus the most-inactive names
    before committing. Doesn't actually disable anyone — it's a
    pure read. Super-users are excluded from the count (mirroring
    the sweep's own exclusion).

    Permission: gated on ``users.manage`` — the preview surfaces
    inactive-account identity (usernames, real names, roles,
    last-login), so it must not be reachable by any authenticated
    caller. The General Settings *page* lock is not enough; this API
    route enforces the same right server-side.
    """
    if not await user_has_permission(db, current_user.id, Permission.USERS_MANAGE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You don't have permission to preview inactive users "
                "(users.manage). Ask an administrator if you need access."
            ),
        )
    now = _dt.datetime.now(tz=_dt.UTC)
    rows, total = await preview_inactive_users(
        db,
        threshold_days=threshold_days,
        limit=limit,
        as_of=now,
    )
    return AdminAutoDisablePreviewResponse(
        threshold_days=threshold_days,
        count=total,
        users=[
            AdminAutoDisablePreviewRow(
                user_id=row.user_id,
                username=row.username,
                full_name=row.full_name,
                last_login_at=row.last_login_at,
                created_at=row.created_at,
                days_inactive=row.days_inactive,
                role=row.role,
            )
            for row in rows
        ],
    )


# ---------------------------------------------------------------------------
# Public branding image route — serves the saved header icon / favicon to
# the browser so the chrome can ``<link rel="icon">`` / ``<img src="...">``
# them. No auth: every authenticated and anonymous request sees the same
# branding, just like a typical site favicon.
# ---------------------------------------------------------------------------


@router.get("/branding/{key}", include_in_schema=False)
async def get_branding_image(
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    key: Annotated[str, Path()],
) -> FileResponse:
    """Serve the uploaded image bytes for a branding key.

    Returns 404 when the key isn't a branding key, when no override
    has been saved, or when the file is missing from disk (e.g. the
    branding directory was wiped). The browser then falls back to
    its default tab icon / the chrome's no-icon path.
    """
    if key not in _BRANDING_IMAGE_KEYS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    snapshot = await load_general_settings(db)
    basename = snapshot.get(key)
    if not isinstance(basename, str) or not basename:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    path = settings.branding.icons_dir / basename
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    media_type, _ = mimetypes.guess_type(str(path))
    return FileResponse(path, media_type=media_type or "application/octet-stream")


# ---------------------------------------------------------------------------
# Branding cache wiring
# ---------------------------------------------------------------------------


_BRANDING_CACHE_KEYS: frozenset[str] = frozenset(
    {
        "app_display_title",
        "header_icon_path",
        "favicon_path",
        "maintenance_banner_text",
        "maintenance_window_iso",
        "support_email_footer",
        # Not branding per se, but the formatter reads it off the same
        # cached snapshot, so a save must refresh the cache too.
        "default_timezone",
    }
)


async def _maybe_refresh_branding(db: AsyncSession, key: str, settings: Settings) -> None:
    """Refresh the chrome's branding cache if this key is a branding row.

    Pure best-effort: a refresh failure (rare — same query the GET
    route just succeeded on) is swallowed so the user's save still
    succeeds. The cache stays on the previous value until the next
    request that triggers a refresh.
    """
    if key not in _BRANDING_CACHE_KEYS:
        return
    # Lazy import: matofx.ui isn't always installed (matofx-api can run
    # without the NiceGUI shell), so importing at module scope would
    # break headless deploys.
    try:
        from matofx.ui.branding_cache import refresh_branding_cache  # noqa: PLC0415
    except ImportError:
        return
    try:
        await refresh_branding_cache(db, icons_dir=settings.branding.icons_dir)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_role(name: str) -> UserRole:
    """Narrow the role string to the :data:`UserRole` Literal.

    The Literal alias is a typing hint; at runtime every string is
    fine. The service's ``has_role_at_least`` treats unknown names
    as below every threshold, so a custom role would simply be
    denied editing — which is the right behaviour given the catalog
    only references the four built-in roles.
    """
    return name  # type: ignore[return-value]


def _image_upload_cap(key: str, settings: Settings) -> int:
    """Per-key image upload cap.

    Favicons need a much tighter cap than header icons because real
    favicons are kilobytes; allowing 8 MiB lets a misuploaded photo
    fill the disk with no warning. Header icons get a more generous
    cap to accommodate high-DPI wordmarks.
    """
    if key == "favicon_path":
        return settings.branding.favicon_max_upload_bytes
    if key == "header_icon_path":
        return settings.branding.header_icon_max_upload_bytes
    # Fallback for any future image keys: use the more conservative
    # cap. The catalog's per-spec validation will reject unknown
    # image keys at the service layer before reaching this branch.
    return settings.branding.favicon_max_upload_bytes
