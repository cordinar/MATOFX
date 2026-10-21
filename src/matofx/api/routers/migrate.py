"""HTTP surface for cross-install Tools/Workflows migration.

Six endpoints, all under ``/api/admin/migrate``:

* ``GET  /api/admin/migrate/this-install-fingerprint`` — return this
  install's Ed25519 fingerprint so the operator can paste it into a
  peer install's trust list.
* ``GET  /api/admin/migrate/trusted-peers`` — list trust entries.
* ``POST /api/admin/migrate/trusted-peers`` — add a peer.
* ``DELETE /api/admin/migrate/trusted-peers/{fingerprint}`` — remove
  a peer (the ``reason`` rides in the JSON body, audit-logged).
* ``POST /api/admin/migrate/export`` — JSON body lists what to bundle;
  response is the gzipped tarball as an ``application/gzip`` download.
* ``POST /api/admin/migrate/import`` — multipart upload of the bundle
  file; optional ``dry_run=true`` query param; response is a JSON
  envelope carrying the :class:`ImportResult` and the live
  :class:`ProgressEvent` stream collected during the run.

All six are gated by :attr:`Permission.MIGRATE_TOOLS_WORKFLOWS`,
which super-users hold by default and administrators must be granted
explicitly (see PROJECT_BRIEF.md §Q5 amendment).

Domain errors fall through to the global handler in
:mod:`matofx.api.errors` for uniform HTTP shape. The router catches
the inline ``ValueError`` raised by the trust service on an empty
label and translates to ``400`` since it isn't a domain-error class.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api._uploads import read_upload_capped
from matofx.api.deps import (
    get_current_user,
    get_db,
    get_export_service,
    get_import_service,
    get_install_key_pair,
    get_settings,
    get_trusted_peer_service,
)
from matofx.config import Settings
from matofx.migrate.orm import MigrateTrustedPeer
from matofx.migrate.progress import ProgressEvent
from matofx.migrate.service import (
    ExportService,
    ImportItemOutcome,
    ImportResult,
    ImportService,
)
from matofx.migrate.signing import (
    KeyPair,
    compute_fingerprint,
    display_fingerprint,
)
from matofx.migrate.trust import TrustedPeerService
from matofx.users.errors import PermissionDeniedError
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission

router = APIRouter()


# ---------------------------------------------------------------------------
# Permission gate
# ---------------------------------------------------------------------------


async def _require_migrate(session: AsyncSession, user_id: int) -> None:
    """403-on-no for :attr:`Permission.MIGRATE_TOOLS_WORKFLOWS`.

    Mirrors the inline ``_require`` pattern used by every other admin
    router in the tree — the global error handler turns
    :class:`PermissionDeniedError` into a 403 with the slug name in
    the body.
    """
    if not await user_has_permission(session, user_id, Permission.MIGRATE_TOOLS_WORKFLOWS):
        raise PermissionDeniedError(Permission.MIGRATE_TOOLS_WORKFLOWS.value)


# ---------------------------------------------------------------------------
# Response / request models
# ---------------------------------------------------------------------------


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FingerprintResponse(_Frozen):
    """Returned by ``GET /api/admin/migrate/this-install-fingerprint``."""

    fingerprint_canonical: str
    """64-character lowercase hex. Used in API calls and matched against trust rows."""

    fingerprint_display: str
    """Colon-separated uppercase form (``9F:2E:...``). What the operator
    eyeballs / pastes into the destination's trust dialog."""


class TrustedPeerResponse(_Frozen):
    """One row in the trust list."""

    fingerprint: str
    fingerprint_display: str
    label: str
    added_by_user_id: int | None
    added_at: _dt.datetime
    last_used_at: _dt.datetime | None
    notes: str

    @classmethod
    def from_orm_row(cls, row: MigrateTrustedPeer) -> TrustedPeerResponse:
        """Build the response from a stored trusted-peer row."""
        return cls(
            fingerprint=row.fingerprint,
            fingerprint_display=display_fingerprint(row.fingerprint),
            label=row.label,
            added_by_user_id=row.added_by_user_id,
            added_at=row.added_at,
            last_used_at=row.last_used_at,
            notes=row.notes,
        )


class AddTrustedPeerRequest(_Frozen):
    """Body for ``POST /api/admin/migrate/trusted-peers``."""

    fingerprint: str = Field(min_length=1)
    """Anything :func:`canonicalise_fingerprint` accepts — colon-separated,
    bare hex, with or without ``SHA256:`` prefix."""

    label: str = Field(min_length=1, max_length=128)
    """Short human-readable name for the source install (e.g. ``"Staging"``)."""

    notes: str = Field(default="", max_length=4000)


class RemoveTrustedPeerRequest(_Frozen):
    """Body for ``DELETE /api/admin/migrate/trusted-peers/{fingerprint}``."""

    reason: str = Field(min_length=1, max_length=2000)
    """Plain-English why-text. Captured in the audit row."""


class ToolSpec(_Frozen):
    """One ``(alias, version)`` entry in the export request."""

    alias: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)


class ExportBundleRequest(_Frozen):
    """Body for ``POST /api/admin/migrate/export``."""

    tool_specs: list[ToolSpec] = Field(default_factory=list)
    workflow_aliases: list[str] = Field(default_factory=list)
    notes: str = Field(default="", max_length=4000)
    include_required: bool = False
    """Also bundle workflow members and the upstream tools the selected tools
    are wired from (ADR-0023). The additions come back in the
    ``X-Matofx-Included-Tools`` response header as ``alias:version,...``."""


class ProgressEventResponse(_Frozen):
    """JSON shape of one :class:`ProgressEvent`."""

    level: str
    message: str
    item_kind: str | None = None
    item_alias: str | None = None
    item_version: str | None = None
    reason: str | None = None
    extra: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_domain(cls, event: ProgressEvent) -> ProgressEventResponse:
        """Build the response from a domain progress event."""
        return cls(
            level=event.level,
            message=event.message,
            item_kind=event.item_kind,
            item_alias=event.item_alias,
            item_version=event.item_version,
            reason=event.reason,
            extra=dict(event.extra),
        )


class ImportItemOutcomeResponse(_Frozen):
    """JSON shape of one :class:`ImportItemOutcome`."""

    kind: str
    alias: str
    version: str | None
    outcome: str
    reason: str | None

    @classmethod
    def from_domain(cls, item: ImportItemOutcome) -> ImportItemOutcomeResponse:
        """Build the response from one item's import outcome."""
        return cls(
            kind=item.kind,
            alias=item.alias,
            version=item.version,
            outcome=item.outcome,
            reason=item.reason,
        )


class ImportResultResponse(_Frozen):
    """JSON shape of :class:`ImportResult`."""

    source_fingerprint: str
    source_fingerprint_display: str
    source_label: str
    bundle_hash: str
    notes: str
    created_at_utc: _dt.datetime
    created_by: str
    items: list[ImportItemOutcomeResponse]
    dry_run: bool
    imported_count: int
    skipped_count: int
    noop_count: int
    link_repairs: list[dict[str, str]] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, result: ImportResult) -> ImportResultResponse:
        """Build the response from a completed import result."""
        return cls(
            source_fingerprint=result.source_fingerprint,
            source_fingerprint_display=display_fingerprint(result.source_fingerprint),
            source_label=result.source_label,
            bundle_hash=result.bundle_hash,
            notes=result.notes,
            created_at_utc=result.created_at_utc,
            created_by=result.created_by,
            items=[ImportItemOutcomeResponse.from_domain(i) for i in result.items],
            dry_run=result.dry_run,
            imported_count=result.imported_count,
            skipped_count=result.skipped_count,
            noop_count=result.noop_count,
            link_repairs=list(result.link_repairs),
        )


class ImportEnvelope(_Frozen):
    """Response wrapper for ``POST /api/admin/migrate/import``.

    Carries both the structured :class:`ImportResultResponse` for the
    UI's summary card and the full ordered list of progress events for
    the live status log. Events are returned in emission order.
    """

    result: ImportResultResponse
    events: list[ProgressEventResponse]


# ---------------------------------------------------------------------------
# Trust list
# ---------------------------------------------------------------------------


@router.get(
    "/admin/migrate/this-install-fingerprint",
    response_model=FingerprintResponse,
    summary="Return this install's signing-key fingerprint",
)
async def get_this_install_fingerprint(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    key_pair: Annotated[KeyPair, Depends(get_install_key_pair)],
) -> FingerprintResponse:
    """Compute this install's Ed25519 fingerprint.

    The operator copies the display form into a peer install's trust
    dialog so the peer will accept bundles signed here.
    """
    await _require_migrate(db, current_user.id)
    fingerprint = compute_fingerprint(key_pair.public_key)
    return FingerprintResponse(
        fingerprint_canonical=fingerprint,
        fingerprint_display=display_fingerprint(fingerprint),
    )


@router.get(
    "/admin/migrate/trusted-peers",
    response_model=list[TrustedPeerResponse],
    summary="List trusted source installs",
)
async def list_trusted_peers(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    trust_service: Annotated[TrustedPeerService, Depends(get_trusted_peer_service)],
) -> list[TrustedPeerResponse]:
    """Return every trust entry, newest first."""
    await _require_migrate(db, current_user.id)
    rows = await trust_service.list_all(db)
    return [TrustedPeerResponse.from_orm_row(r) for r in rows]


@router.post(
    "/admin/migrate/trusted-peers",
    response_model=TrustedPeerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a trusted source install",
)
async def add_trusted_peer(
    body: AddTrustedPeerRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    trust_service: Annotated[TrustedPeerService, Depends(get_trusted_peer_service)],
) -> TrustedPeerResponse:
    """Add a fingerprint to the trust list.

    Empty labels are rejected as 400 (the trust service raises a plain
    :class:`ValueError` for this; it isn't a domain-error class so the
    global handler doesn't see it). Duplicate fingerprints surface as
    409 via the global handler.
    """
    await _require_migrate(db, current_user.id)
    try:
        row = await trust_service.add_peer(
            db,
            actor_user_id=current_user.id,
            fingerprint=body.fingerprint,
            label=body.label,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return TrustedPeerResponse.from_orm_row(row)


@router.delete(
    "/admin/migrate/trusted-peers/{fingerprint}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a trusted source install",
)
async def remove_trusted_peer(
    fingerprint: str,
    body: RemoveTrustedPeerRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    trust_service: Annotated[TrustedPeerService, Depends(get_trusted_peer_service)],
) -> Response:
    """Remove a peer by canonical or pasted-form fingerprint.

    Path parameter accepts any form :func:`canonicalise_fingerprint`
    handles; the body's mandatory ``reason`` lands in the audit row.
    """
    await _require_migrate(db, current_user.id)
    await trust_service.remove_peer(
        db,
        actor_user_id=current_user.id,
        fingerprint=fingerprint,
        reason=body.reason,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@router.post(
    "/admin/migrate/export",
    summary="Build and download a signed migration bundle",
    responses={
        200: {
            "content": {"application/gzip": {}},
            "description": "Signed gzipped tarball — pass to the destination "
            "install's import endpoint.",
        }
    },
)
async def export_bundle(
    body: ExportBundleRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    export_service: Annotated[ExportService, Depends(get_export_service)],
    key_pair: Annotated[KeyPair, Depends(get_install_key_pair)],
) -> Response:
    """Build the bundle and return it as an ``application/gzip`` download.

    The browser saves the bytes under the suggested filename
    (``matofx-migrate-<timestamp>-<short-fingerprint>.tar.gz``); a
    parallel ``migrate.bundle.exported`` audit event lands on this
    install's chain.
    """
    await _require_migrate(db, current_user.id)
    fingerprint = compute_fingerprint(key_pair.public_key)
    selection = await export_service.resolve_selection(
        db,
        tool_specs=[(t.alias, t.version) for t in body.tool_specs],
        workflow_aliases=list(body.workflow_aliases),
        include_required=body.include_required,
    )
    bundle_bytes = await export_service.build_bundle(
        db,
        actor_user_id=current_user.id,
        key_pair=key_pair,
        source_install_id=fingerprint,  # informational; verification rides on the FP
        tool_specs=selection.tool_specs,
        workflow_aliases=list(body.workflow_aliases),
        notes=body.notes,
        actor_label=current_user.email,
    )
    filename = _suggested_bundle_filename(fingerprint)
    return Response(
        content=bundle_bytes,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Bundle-Fingerprint": fingerprint,
            "X-Matofx-Included-Tools": ",".join(f"{a}:{v}" for a, v in selection.added),
        },
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@router.post(
    "/admin/migrate/import",
    response_model=ImportEnvelope,
    summary="Verify, dry-run, or commit a migration bundle",
)
async def import_bundle(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    import_service: Annotated[ImportService, Depends(get_import_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile, File(description="The .tar.gz bundle to import.")],
    dry_run: Annotated[
        bool,
        Query(
            description="When true, walks the import flow and reports outcomes "
            "without writing anything. The UI uses this to render the preview "
            "card before the operator clicks 'Confirm import'.",
        ),
    ] = False,
) -> ImportEnvelope:
    """Upload a bundle and import (or preview) it.

    The response carries both the structured :class:`ImportResult` for
    the summary card AND the full ordered :class:`ProgressEvent` list
    for the live status log. The UI typically calls this twice — once
    with ``dry_run=true`` to render the preview, then again with
    ``dry_run=false`` after the operator confirms.
    """
    await _require_migrate(db, current_user.id)
    # Bounded read against the configured per-bundle cap so a large upload
    # can't exhaust the API host's memory before import even starts.
    contents = await read_upload_capped(
        file,
        max_bytes=settings.tools.max_upload_bytes,
        request=request,
        what="bundle",
    )
    if not contents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file is empty. Choose a .tar.gz bundle "
            "file produced by another install's export.",
        )
    events: list[ProgressEvent] = []
    result = await import_service.import_bundle(
        db,
        actor_user_id=current_user.id,
        bundle_bytes=contents,
        on_event=events.append,
        dry_run=dry_run,
    )
    return ImportEnvelope(
        result=ImportResultResponse.from_domain(result),
        events=[ProgressEventResponse.from_domain(e) for e in events],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _suggested_bundle_filename(fingerprint: str) -> str:
    """Build the ``Content-Disposition`` filename for an exported bundle.

    Example: ``matofx-migrate-2026-05-15T14-22-08Z-9f2ea17b.tar.gz``.
    """
    timestamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    short_fp = fingerprint[:8]
    safe_stem = _FILENAME_SAFE_RE.sub("-", f"matofx-migrate-{timestamp}-{short_fp}")
    return f"{safe_stem}.tar.gz"
