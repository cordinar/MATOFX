"""Tool registry service.

CRUD for installed tools, with permission-based visibility filtering
on reads, privileged-tier-only state transitions and edits on writes,
and approver / team cross-validation against the install's user and
team catalogs.

Phase 1 covers the catalog half — install, update_descriptor,
update_bundle, archive, unarchive, list_visible, get, promote_state.
The execution backend that actually *runs* a tool against a job lands
in a follow-up step (subprocess-based for MVP demo; the synthesised-
image containerised execution per §F29 lands in Phase 2).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from matofx.audit.schema import RecordInput
from matofx.tools.descriptor import (
    GUIDE_CAPTION_MAX,
    MAX_GUIDE_IMAGES,
    ToolDescriptor,
)
from matofx.tools.errors import (
    AliasMismatchError,
    ApproverNotEnabledError,
    DuplicateToolError,
    InvalidBundlePathError,
    InvalidFileEditError,
    InvalidPromotionError,
    InvalidStateTransitionError,
    SyntaxValidationError,
    ToolArchivedError,
    ToolNotFoundError,
    ToolPermissionError,
    UnknownApproverError,
    UnknownMaterialClassError,
    UnknownMaterialPropertyError,
    UnknownTeamError,
)
from matofx.tools.forms.seeders import Preset, load_presets
from matofx.tools.orm import Tool, ToolVersion
from matofx.tools.staging import BundleStagingError, copy_bundle_for_minor_bump
from matofx.tools.states import TOOL_STATES, ToolState, user_can_see_state
from matofx.tools.syntax_check import check_syntax
from matofx.tools.versioning import (
    INITIAL_VERSION,
    bump_major,
    bump_minor,
    bump_patch,
    compute_bundle_hash,
    diff_descriptor,
    first_release_snap,
    headline_for_bundle_replace,
    headline_for_file_edit,
    headline_for_first_release_snap,
    headline_for_initial_install,
    headline_for_metadata_edit,
)
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission
from matofx.users.team_orm import Team

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


_BUNDLE_PATH_MAX_LENGTH = 512
"""Match the ``bundle_path`` column width in ``tool.orm``."""

# Image formats accepted for tool guide images: extension -> canonical form.
_GUIDE_IMAGE_EXTENSIONS: dict[str, str] = {
    "png": "png",
    "jpg": "jpg",
    "jpeg": "jpg",
    "webp": "webp",
}


@dataclass(frozen=True, slots=True)
class GuideImageEdit:
    """One entry in a desired guide-image set (see :meth:`ToolService.update_input_guides`).

    Exactly one of ``existing_filename`` (keep a guide already in the
    bundle) or ``new_bytes`` (write a freshly-uploaded image) is set. The
    order of the edits list is the display order.
    """

    caption: str = ""
    existing_filename: str | None = None
    new_bytes: bytes | None = None
    new_ext: str = "png"


def _guide_filename_for(bundle: Path, edit: GuideImageEdit, current_files: set[str]) -> str:
    """Resolve one edit to a bundle-relative filename, writing new bytes.

    A ``existing_filename`` must already be a guide of this tool (guards
    against referencing an arbitrary bundle file). New bytes are written
    under a content-addressed name so re-uploading the same image is a
    no-op. Raises :class:`ValueError` for a bad request and
    :class:`InvalidFileEditError` if the write fails.
    """
    if edit.existing_filename is not None:
        if edit.existing_filename not in current_files:
            raise ValueError("A guide image referenced an image that isn't part of this tool.")
        return edit.existing_filename
    if edit.new_bytes is None:
        raise ValueError("A new guide image is missing its image data.")
    ext = _GUIDE_IMAGE_EXTENSIONS.get(edit.new_ext.lower())
    if ext is None:
        raise ValueError("Guide images must be PNG, JPG or WebP.")
    digest = hashlib.sha1(edit.new_bytes).hexdigest()[:16]  # noqa: S324 - CAS name, not security
    filename = f"guide-{digest}.{ext}"
    try:
        (bundle / filename).write_bytes(edit.new_bytes)
    except OSError as exc:
        raise InvalidFileEditError(
            "(bundle)",
            f"writing a guide image to the bundle directory failed: {exc}",
        ) from exc
    return filename


def _resolve_guide_edits(
    bundle: Path, edits: list[GuideImageEdit], current_files: set[str]
) -> tuple[list[dict[str, str]], set[str]]:
    """Turn the desired edits into ``input_guides`` entries + the kept filenames."""
    new_guides: list[dict[str, str]] = []
    kept: set[str] = set()
    for edit in edits:
        caption = (edit.caption or "").strip()[:GUIDE_CAPTION_MAX]
        filename = _guide_filename_for(bundle, edit, current_files)
        new_guides.append({"filename": filename, "caption": caption})
        kept.add(filename)
    return new_guides, kept

# Tool state graph: forward promotions must follow this rank order.
# Rollbacks (downgrades) are always allowed.
_STATE_RANK: dict[str, int] = {"new": 0, "test": 1, "released": 2}


def _validate_bundle_path(path: str) -> str:
    """Reject empty / path-traversing / wildly malformed bundle paths.

    Returns the cleaned path. Defence-in-depth: tools install is a
    privileged action, but if the install API is ever surfaced to the
    web layer a rogue path with ``..`` segments could escape the
    intended bundle root.
    """
    cleaned = path.strip()
    if not cleaned:
        raise InvalidBundlePathError(path, "the path can't be empty")
    # Reject NUL bytes outright (some OSes silently truncate).
    if "\x00" in cleaned:
        raise InvalidBundlePathError(cleaned, "embedded NUL bytes aren't allowed")
    # Reject explicit parent-directory traversal in any platform's path syntax.
    parts: tuple[str, ...] = PurePosixPath(cleaned).parts + PureWindowsPath(cleaned).parts
    if any(part == ".." for part in parts):
        raise InvalidBundlePathError(cleaned, "the path contains '..' segments")
    if len(cleaned) > _BUNDLE_PATH_MAX_LENGTH:
        raise InvalidBundlePathError(
            cleaned, f"the path is longer than {_BUNDLE_PATH_MAX_LENGTH} characters"
        )
    return cleaned


_DESCRIPTOR_JSON_FILENAME = "descriptor.json"
"""Excluded from :meth:`ToolService.apply_file_edit` — descriptor changes go through
:meth:`ToolService.update_descriptor` (patch bump) so the validation pipeline runs."""

_FILE_EDIT_SNAPSHOT_TRUNCATE = 50_000
"""How many characters of each file's old/new content the snapshot keeps.

Sized so a typical descriptor / Python tool script (a few thousand lines)
round-trips fully into the change-log modal. The cap remains as a sanity
bound — generated artefacts (logs, big JSON dumps) checked into a bundle
shouldn't be able to bloat the ``tool_version`` row indefinitely — but
hand-written source files render in full."""


def _truncate_for_snapshot(text: str) -> str:
    """Trim a file's contents to a preview suitable for the change-log modal."""
    if len(text) <= _FILE_EDIT_SNAPSHOT_TRUNCATE:
        return text
    return text[: _FILE_EDIT_SNAPSHOT_TRUNCATE - 1].rstrip() + "…"


def _safe_compute_bundle_hash(bundle_path: str) -> str:
    """Hash the bundle dir for the V&V cohort key, returning ``""`` on failure.

    Called from :meth:`ToolService._record_snapshot`. The strict
    :func:`compute_bundle_hash` raises if the bundle dir doesn't exist
    or isn't a directory; for the snapshot path we want to write the
    row either way (a missing bundle is a deployment problem, not a
    blocker for recording history). The empty-string sentinel means
    "unknown" — the V&V badge query treats it as a singleton cohort
    so the row participates in no inheritance until the bundle is
    fixed and a fresh snapshot computes a real hash.
    """
    try:
        return compute_bundle_hash(Path(str(bundle_path)))
    except (FileNotFoundError, NotADirectoryError, OSError, PermissionError):
        return ""


def _read_bundle_file_for_edit(bundle_root: Path, rel_path: str) -> str:
    """Read ``rel_path`` from the bundle as text, with all the safety checks.

    Used by :meth:`ToolService.apply_file_edit` to load the *current*
    contents of every file the operator wants to change so the snapshot
    can record an old→new diff. Reused by the API's read-file endpoint.

    Raises :class:`InvalidFileEditError` for: empty / NUL-bearing /
    absolute-rooted paths, paths that resolve outside the bundle,
    paths that don't exist or aren't regular files, the
    ``descriptor.json`` carve-out, and files that aren't valid UTF-8.
    """
    cleaned = rel_path.strip()
    if not cleaned:
        raise InvalidFileEditError(rel_path, "the file path can't be empty")
    if "\x00" in cleaned:
        raise InvalidFileEditError(rel_path, "embedded NUL bytes aren't allowed")
    posix_parts = PurePosixPath(cleaned).parts
    win_parts = PureWindowsPath(cleaned).parts
    if any(p == ".." for p in posix_parts + win_parts):
        raise InvalidFileEditError(rel_path, "the path contains '..' segments")
    if PurePosixPath(cleaned).is_absolute() or PureWindowsPath(cleaned).is_absolute():
        raise InvalidFileEditError(rel_path, "absolute paths aren't allowed")
    if cleaned == _DESCRIPTOR_JSON_FILENAME or cleaned.endswith("/" + _DESCRIPTOR_JSON_FILENAME):
        raise InvalidFileEditError(
            rel_path,
            "the descriptor file can't be edited as text — use the metadata "
            "form (patch bump) or the descriptor wizard instead",
        )

    bundle_resolved = bundle_root.resolve()
    target = (bundle_root / cleaned).resolve()
    try:
        target.relative_to(bundle_resolved)
    except ValueError as exc:
        raise InvalidFileEditError(
            rel_path, "the path resolves outside the tool's bundle directory"
        ) from exc
    if not target.exists():
        raise InvalidFileEditError(
            rel_path,
            "the file doesn't exist in the current bundle (creating new files "
            "would be a major change — use Replace bundle instead)",
        )
    if not target.is_file():
        raise InvalidFileEditError(rel_path, "the path doesn't point at a regular file")
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidFileEditError(
            rel_path, "the file isn't valid UTF-8 text — only text files are editable"
        ) from exc
    except OSError as exc:  # pragma: no cover — race between exists() and read_text()
        raise InvalidFileEditError(rel_path, f"the file couldn't be read: {exc}") from exc


@dataclass(frozen=True)
class ToolService:
    """Manages the installed-tool registry."""

    audit: AuditWriter

    # ---- install / update / archive ---------------------------------

    async def install(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        descriptor: ToolDescriptor,
        bundle_path: str,
    ) -> Tool:
        """Register a new tool. Privileged-tier action.

        The tool starts in the ``new`` state regardless of what the
        caller asks for; promotion happens via :meth:`promote_state`
        once the tool author and reviewer agree it's ready.

        The version is forced to ``"0.1.0"`` regardless of what the
        operator's descriptor says — server controls the lifecycle from
        Session B on. An initial :class:`ToolVersion` snapshot row is
        written so the change-log modal has somewhere to anchor.

        Validates the descriptor's referenced teams + approvers
        against the install (raising :class:`UnknownTeamError`,
        :class:`UnknownApproverError`, or :class:`ApproverNotEnabledError`
        when references don't resolve) and the bundle path against
        the path-traversal guard.
        """
        await self._require_install_permission(session, actor_user_id)
        cleaned_bundle = _validate_bundle_path(bundle_path)
        # Force the descriptor's version to 0.1.0 — the operator's
        # initial choice is ignored so the auto-bump rules can't
        # silently disagree with what's on disk later.
        descriptor = descriptor.model_copy(update={"version": INITIAL_VERSION})
        await self._validate_descriptor_references(session, descriptor)

        tool = Tool(
            alias=descriptor.alias,
            name=descriptor.name,
            version=descriptor.version,
            state="new",
            descriptor=descriptor.model_dump(mode="json"),
            bundle_path=cleaned_bundle,
            installed_by_user_id=actor_user_id,
        )
        session.add(tool)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateToolError(descriptor.alias) from exc

        await self._record_snapshot(
            session,
            tool=tool,
            actor_user_id=actor_user_id,
            change_summary=headline_for_initial_install(),
            changes={},
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tools.tool.install",
                resource_type="tool",
                resource_id=str(tool.id),
                outcome="success",
                payload={
                    "alias": tool.alias,
                    "version": tool.version,
                    "state": tool.state,
                },
            ),
        )
        return tool

    async def update_descriptor(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        descriptor: ToolDescriptor,
        expected_row_version: int | None = None,
    ) -> Tool:
        """Patch-bump a tool's descriptor metadata. Privileged-tier action.

        The supplied descriptor's :attr:`alias` must match the row's
        existing alias (aliases identify the tool; renaming would
        orphan jobs). Refuses if the tool is archived. Validates the
        new descriptor's teams and approvers the same way
        :meth:`install` does.

        Auto-bumps the patch number (X.Y.Z → X.Y.Z+1). The supplied
        descriptor's ``version`` is ignored; the server is the
        authoritative source of bumps from Session B on. A
        :class:`ToolVersion` snapshot is written for the change-log
        modal. **No-op edits skip the snapshot** — if every metadata
        field is unchanged, the call returns the existing row without
        bumping.
        """
        await self._require_install_permission(session, actor_user_id)
        tool = await self._fetch_by_alias(session, descriptor.alias)
        if tool is None:
            raise ToolNotFoundError(descriptor.alias)
        if tool.alias != descriptor.alias:
            raise AliasMismatchError(tool.alias, descriptor.alias)
        if tool.archived_at is not None:
            raise ToolArchivedError(tool.alias)
        if expected_row_version is not None and tool.row_version != expected_row_version:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool")

        # Diff first so we can short-circuit no-op edits.
        old_descriptor = dict(tool.descriptor) if isinstance(tool.descriptor, dict) else {}
        new_descriptor_dict = descriptor.model_dump(mode="json")
        changes = diff_descriptor(old_descriptor, new_descriptor_dict)
        if not changes:
            return tool  # no metadata field actually changed

        await self._validate_descriptor_references(session, descriptor)
        old_version = str(tool.version)
        new_version = bump_patch(old_version)
        descriptor = descriptor.model_copy(update={"version": new_version})

        previous_descriptor = tool.descriptor
        tool.name = descriptor.name
        tool.version = new_version
        tool.descriptor = descriptor.model_dump(mode="json")
        try:
            await session.flush()
        except StaleDataError as exc:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool") from exc

        await self._record_snapshot(
            session,
            tool=tool,
            actor_user_id=actor_user_id,
            change_summary=headline_for_metadata_edit(old_version, new_version, changes),
            changes=changes,
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tools.tool.update_descriptor",
                resource_type="tool",
                resource_id=str(tool.id),
                outcome="success",
                payload={
                    "alias": tool.alias,
                    "previous_version": previous_descriptor.get("version"),
                    "new_version": new_version,
                    "changes": changes,
                },
            ),
        )
        return tool

    async def set_run_cap(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
        run_cap: int | None,
        expected_row_version: int | None = None,
    ) -> Tool:
        """Set or clear the per-tool parametric-study run cap.

        ``run_cap = None`` clears the override (study creation falls
        back to the install-wide default - currently 500). Otherwise
        the integer is the per-tool cap (``ge=1`` enforced at the API
        schema). Row-level operational setting - *not* a descriptor
        field - so no version bump and no ``ToolVersion`` snapshot.
        Audited. ADR-0001 2026-05-20 revision /
        ``designs/parametric-studies.md`` §16.
        """
        await self._require_install_permission(session, actor_user_id)
        tool = await self._fetch_by_alias(session, alias)
        if tool is None:
            raise ToolNotFoundError(alias)
        if tool.archived_at is not None:
            raise ToolArchivedError(tool.alias)
        if expected_row_version is not None and tool.row_version != expected_row_version:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool")
        if tool.run_cap == run_cap:
            return tool  # no-op: skip flush + audit
        previous = tool.run_cap
        tool.run_cap = run_cap
        try:
            await session.flush()
        except StaleDataError as exc:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool") from exc
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tools.tool.set_run_cap",
                resource_type="tool",
                resource_id=str(tool.id),
                outcome="success",
                payload={"alias": tool.alias, "previous": previous, "new": run_cap},
            ),
        )
        return tool

    async def set_python_env(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
        python_env_id: int | None,
        expected_row_version: int | None = None,
    ) -> Tool:
        """Pin (or clear) the Python interpreter a tool's jobs run under.

        ``python_env_id = None`` clears the pin — the launcher falls
        back to the MATOFX server's own interpreter (the "main MATOFX
        environment") at submit time. Otherwise the id must reference
        an *active* ``python_env`` row; an inactive or missing env
        raises :class:`PythonEnvNotUsableError`. Row-level operational
        setting (like :meth:`set_run_cap`) — *not* a descriptor field —
        so no version bump and no ``ToolVersion`` snapshot. Audited.
        """
        from matofx.tools.errors import PythonEnvNotUsableError  # noqa: PLC0415
        from matofx.tools.exec_orm import PythonEnv  # noqa: PLC0415

        await self._require_install_permission(session, actor_user_id)
        tool = await self._fetch_by_alias(session, alias)
        if tool is None:
            raise ToolNotFoundError(alias)
        if tool.archived_at is not None:
            raise ToolArchivedError(tool.alias)
        if expected_row_version is not None and tool.row_version != expected_row_version:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool")
        if python_env_id is not None:
            env = await session.get(PythonEnv, python_env_id)
            if env is None or not env.is_active:
                raise PythonEnvNotUsableError(python_env_id)
        if tool.python_env_id == python_env_id:
            return tool  # no-op: skip flush + audit
        previous = tool.python_env_id
        tool.python_env_id = python_env_id
        try:
            await session.flush()
        except StaleDataError as exc:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool") from exc
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tools.tool.set_python_env",
                resource_type="tool",
                resource_id=str(tool.id),
                outcome="success",
                payload={"alias": tool.alias, "previous": previous, "new": python_env_id},
            ),
        )
        return tool

    async def update_icon(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
        icon_bytes: bytes,
        expected_row_version: int | None = None,
    ) -> Tool:
        """Replace the tool's icon PNG without re-uploading the bundle.

        The icon is a *display* asset, not a runtime input — jobs don't
        execute against it, so we deliberately don't clone the bundle
        the way :meth:`apply_file_edit` does. The PNG is written
        in-place at ``<bundle_path>/<alias>.png``, the descriptor's
        ``icon_filename`` is set to ``"<alias>.png"`` if it wasn't
        already, and the tool is patch-bumped + snapshotted so the
        change-log records who did it and when. The bundle directory
        must already exist on disk; if it doesn't (corrupt install,
        manual delete) an :class:`InvalidFileEditError` is raised so
        the operator gets the same friendly message they'd see for a
        broken file edit.

        Refuses on archived tools, same as every other mutating call.

        Args:
            session: Active SQLAlchemy session.
            actor_user_id: The operator triggering the change.
            alias: Tool to update.
            icon_bytes: Raw PNG bytes (already validated as PNG by the
                caller — the API layer enforces the content-type).
            expected_row_version: Optional optimistic-lock guard.
        """
        await self._require_install_permission(session, actor_user_id)
        tool = await self._fetch_by_alias(session, alias)
        if tool is None:
            raise ToolNotFoundError(alias)
        if tool.archived_at is not None:
            raise ToolArchivedError(tool.alias)
        if expected_row_version is not None and tool.row_version != expected_row_version:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool")

        bundle = Path(str(tool.bundle_path)).resolve()
        if not bundle.is_dir():
            raise InvalidFileEditError(
                "(bundle)",
                "the tool's bundle directory doesn't exist on disk; recover "
                "the bundle from a backup or replace it before updating the icon",
            )

        icon_filename = f"{tool.alias}.png"
        try:
            (bundle / icon_filename).write_bytes(icon_bytes)
        except OSError as exc:
            raise InvalidFileEditError(
                "(bundle)",
                f"writing the new icon to the bundle directory failed: {exc}",
            ) from exc

        old_descriptor = dict(tool.descriptor) if isinstance(tool.descriptor, dict) else {}
        new_descriptor = dict(old_descriptor)
        new_descriptor["icon_filename"] = icon_filename
        old_version = str(tool.version)
        new_version = bump_patch(old_version)
        new_descriptor["version"] = new_version
        changes = diff_descriptor(old_descriptor, new_descriptor)
        # `changes` includes the version bump itself plus any
        # icon_filename → icon_filename transition (which is empty
        # when icon_filename was already set); the size of the diff
        # doesn't affect whether we bump — every icon swap is a
        # patch bump, just like every metadata edit. The on-disk PNG
        # changed, that's the headline.

        tool.descriptor = new_descriptor
        tool.version = new_version
        try:
            await session.flush()
        except StaleDataError as exc:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool") from exc

        await self._record_snapshot(
            session,
            tool=tool,
            actor_user_id=actor_user_id,
            change_summary=f"Icon update — patch bump {old_version} → {new_version}",
            changes=changes,
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tools.tool.update_icon",
                resource_type="tool",
                resource_id=str(tool.id),
                outcome="success",
                payload={
                    "alias": tool.alias,
                    "previous_version": old_version,
                    "new_version": new_version,
                    "icon_filename": icon_filename,
                    "icon_bytes": len(icon_bytes),
                },
            ),
        )
        return tool

    async def update_input_guides(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
        edits: list[GuideImageEdit],
        expected_row_version: int | None = None,
    ) -> Tool:
        """Replace the tool's guide-image set (add / keep / reorder / delete).

        Mirrors :meth:`update_icon`. Image bytes are written into the
        bundle directory in-place; only ``{filename, caption}`` entries
        are recorded on the descriptor's ``input_guides``, in the order
        given (the display order). New images are content-addressed
        (``guide-<hash>.<ext>``) so re-uploading the same picture is
        idempotent. Bundle files that were guides but are no longer
        referenced are deleted. One patch bump + change-log snapshot,
        same as an icon swap.

        Raises :class:`ValueError` for a bad request (too many images,
        an unsupported format, or an ``existing_filename`` that isn't a
        current guide of this tool — defence against referencing an
        arbitrary bundle file).
        """
        await self._require_install_permission(session, actor_user_id)
        tool = await self._fetch_by_alias(session, alias)
        if tool is None:
            raise ToolNotFoundError(alias)
        if tool.archived_at is not None:
            raise ToolArchivedError(tool.alias)
        if expected_row_version is not None and tool.row_version != expected_row_version:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool")
        if len(edits) > MAX_GUIDE_IMAGES:
            raise ValueError(
                f"A tool can have at most {MAX_GUIDE_IMAGES} guide images; "
                f"{len(edits)} were supplied."
            )

        bundle = Path(str(tool.bundle_path)).resolve()
        if not bundle.is_dir():
            raise InvalidFileEditError(
                "(bundle)",
                "the tool's bundle directory doesn't exist on disk; recover "
                "the bundle from a backup or replace it before updating guides",
            )

        old_descriptor = dict(tool.descriptor) if isinstance(tool.descriptor, dict) else {}
        current = old_descriptor.get("input_guides") or []
        current_files = {
            str(g.get("filename"))
            for g in current
            if isinstance(g, dict) and g.get("filename")
        }

        new_guides, kept = _resolve_guide_edits(bundle, edits, current_files)

        # Remove bundle files that were guides but are no longer referenced.
        # Only ever touches files the descriptor previously listed as guides.
        for stale in current_files - kept:
            with contextlib.suppress(OSError):
                (bundle / stale).unlink(missing_ok=True)

        new_descriptor = dict(old_descriptor)
        new_descriptor["input_guides"] = new_guides
        old_version = str(tool.version)
        new_version = bump_patch(old_version)
        new_descriptor["version"] = new_version
        changes = diff_descriptor(old_descriptor, new_descriptor)

        tool.descriptor = new_descriptor
        tool.version = new_version
        try:
            await session.flush()
        except StaleDataError as exc:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool") from exc

        await self._record_snapshot(
            session,
            tool=tool,
            actor_user_id=actor_user_id,
            change_summary=f"Guide images update — patch bump {old_version} → {new_version}",
            changes=changes,
        )
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tools.tool.update_guides",
                resource_type="tool",
                resource_id=str(tool.id),
                outcome="success",
                payload={
                    "alias": tool.alias,
                    "previous_version": old_version,
                    "new_version": new_version,
                    "guide_count": len(new_guides),
                },
            ),
        )
        return tool

    async def update_bundle(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
        bundle_path: str,
        expected_row_version: int | None = None,
    ) -> Tool:
        """Replace the on-disk bundle path for an installed tool.

        Used when a privileged-tier user has staged a new bundle
        directory (with updated static files, icon, scripts) and
        wants to point the tool at it. Refuses on archived tools.
        """
        await self._require_install_permission(session, actor_user_id)
        tool = await self._fetch_by_alias(session, alias)
        if tool is None:
            raise ToolNotFoundError(alias)
        if tool.archived_at is not None:
            raise ToolArchivedError(tool.alias)
        if expected_row_version is not None and tool.row_version != expected_row_version:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool")

        cleaned = _validate_bundle_path(bundle_path)
        previous = tool.bundle_path
        tool.bundle_path = cleaned
        try:
            await session.flush()
        except StaleDataError as exc:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool") from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tools.tool.update_bundle",
                resource_type="tool",
                resource_id=str(tool.id),
                outcome="success",
                payload={
                    "alias": tool.alias,
                    "previous_bundle_path": previous,
                    "new_bundle_path": cleaned,
                },
            ),
        )
        return tool

    async def archive(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
        reason: str | None = None,
    ) -> Tool:
        """Archive a tool (soft-shelve; never delete). Privileged-tier action.

        Archived tools stay in the DB so existing jobs' ``tool_alias``
        FK keeps resolving, but they don't appear in
        :meth:`list_visible` and :meth:`promote_state` /
        :meth:`update_descriptor` refuse on them.
        """
        await self._require_install_permission(session, actor_user_id)
        tool = await self._fetch_by_alias(session, alias)
        if tool is None:
            raise ToolNotFoundError(alias)
        if tool.archived_at is not None:
            return tool

        tool.archived_at = _utcnow()
        tool.archived_by_user_id = actor_user_id
        tool.archive_reason = reason
        await session.flush()

        # Cascade: archiving a tool auto-archives every non-archived
        # workflow that includes it. Lazy import keeps tools/workflows
        # one-directional at module load.
        from matofx.workflows.service import cascade_archive_for_tool  # noqa: PLC0415

        cascaded = await cascade_archive_for_tool(
            session,
            tool_alias=tool.alias,
            actor_user_id=actor_user_id,
            audit=self.audit,
        )

        # No tool_version snapshot here — the change log is keyed on
        # (alias, version) and archive doesn't change the version, so a
        # snapshot would collide. Archive/unarchive surface in the change
        # log from the audit trail instead (see the change-log endpoint),
        # which is why ``version`` rides along in the payload.
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tools.tool.archive",
                resource_type="tool",
                resource_id=str(tool.id),
                outcome="success",
                payload={
                    "alias": tool.alias,
                    "version": tool.version,
                    "reason": reason,
                    "workflows_archived": cascaded,
                },
            ),
        )
        return tool

    async def unarchive(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
    ) -> Tool:
        """Bring an archived tool back to active status. Privileged-tier action."""
        await self._require_install_permission(session, actor_user_id)
        tool = await self._fetch_by_alias(session, alias)
        if tool is None:
            raise ToolNotFoundError(alias)
        if tool.archived_at is None:
            return tool

        tool.archived_at = None
        tool.archived_by_user_id = None
        tool.archive_reason = None
        await session.flush()

        # See archive(): no snapshot (version unchanged). The change log
        # picks this up from the audit trail; ``version`` rides along.
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tools.tool.unarchive",
                resource_type="tool",
                resource_id=str(tool.id),
                outcome="success",
                payload={"alias": tool.alias, "version": tool.version},
            ),
        )
        return tool

    # ---- read ------------------------------------------------------

    async def get(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
        include_archived: bool = False,
    ) -> Tool:
        """Look up a tool by alias, enforcing state + team visibility.

        Archived tools are excluded by default; pass
        ``include_archived=True`` (admin admin-history pane only) to
        see them. The team filter — applied to non-privileged users
        only — gates access on the tool's ``descriptor.teams`` list
        (see :meth:`list_visible` for the rules).
        """
        tool = await self._fetch_by_alias(session, alias)
        if tool is None or not await can_user_see_tool(
            session, actor_user_id, tool, include_archived=include_archived
        ):
            # Existence-hide: a tool the caller can't see 404s as missing.
            raise ToolNotFoundError(alias)
        return tool

    async def list_presets(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
    ) -> list[Preset]:
        """Read the tool bundle's ``presets.toml`` (or legacy JSON).

        ACL via :meth:`get` — a user who can't see the tool can't see
        its presets. Returns an empty list when the bundle has no
        preset file or the file is malformed (the workspace surfaces
        "no presets available" rather than crashing).
        """
        tool = await self.get(session, actor_user_id=actor_user_id, alias=alias)
        descriptor = ToolDescriptor.model_validate(tool.descriptor)
        return load_presets(tool.bundle_path, descriptor)

    async def list_visible(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        include_archived: bool = False,
    ) -> list[Tool]:
        """List every tool the actor can see, ordered by name.

        Two filters apply, in conjunction:

        - **State** — :func:`user_can_see_state` gates by lifecycle
          stage (``new`` → ``tools.see_new_state``, ``test`` →
          ``tools.see_test_state``, ``released`` → all authenticated).
        - **Teams** — privileged-tier users (anyone holding
          ``tools.see_new_state``, i.e. the seeded admin / super-user)
          see every tool. Everyone else sees a tool only when:

          * the tool's team list contains ``World`` (the universal
            marker — see migration 0014), OR
          * the tool's team list is empty (treated as "ungrouped",
            same as ``World`` for backwards-compat with hand-edited
            descriptors), OR
          * the actor belongs to at least one team in the tool's
            team list.

        Archived tools are excluded unless ``include_archived=True``.
        """
        visible_states, actor_team_names, privileged = await _visibility_context(
            session, actor_user_id
        )
        stmt = select(Tool).order_by(Tool.name)
        if not include_archived:
            stmt = stmt.where(Tool.archived_at.is_(None))
        rows = list((await session.execute(stmt)).scalars())
        return [
            t
            for t in rows
            if _tool_visible_given(
                t,
                visible_states=visible_states,
                actor_team_names=actor_team_names,
                privileged=privileged,
                include_archived=include_archived,
            )
        ]

    # ---- promotion -------------------------------------------------

    async def promote_state(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
        new_state: str,
    ) -> Tool:
        """Move a tool to a new state. Privileged-tier action.

        Forward promotions must follow ``new → test → released`` —
        skipping straight from ``new`` to ``released`` is rejected
        (every tool goes through ``test`` so beta testers actually
        validate it). Rollback (any earlier state) is always allowed
        for broken-release recovery. Refuses on archived tools.
        """
        if new_state not in TOOL_STATES:
            raise InvalidStateTransitionError(new_state)
        await self._require_promote_permission(session, actor_user_id)

        tool = await self._fetch_by_alias(session, alias)
        if tool is None:
            raise ToolNotFoundError(alias)
        if tool.archived_at is not None:
            raise ToolArchivedError(tool.alias)

        previous = tool.state
        if _is_forward_skip(previous, new_state):
            raise InvalidPromotionError(previous, new_state)

        # Promotions to ``released`` bump the version so each release is
        # distinguishable from the previous one:
        #
        #   - First promotion from a ``0.x.y`` row snaps to ``1.0.0``.
        #   - Subsequent promotions to released (after a rollback) major-
        #     bump (``X.Y.Z → X+1.0.0``) — a re-release is a new edition
        #     of the released artifact, on par semantically with a bundle
        #     replacement.
        #   - Rollbacks (released → test, test → new) and no-op
        #     transitions never touch the version.
        old_version = str(tool.version)
        new_version: str | None = None
        snap_summary: str | None = None
        if new_state == "released" and previous != "released":
            first_snap = first_release_snap(old_version)
            if first_snap is not None:
                new_version = first_snap
                snap_summary = headline_for_first_release_snap(old_version)
            else:
                new_version = bump_major(old_version)
                snap_summary = (
                    f"Re-promoted to Released — major-bumped from {old_version} to {new_version}"
                )

        tool.state = new_state
        if new_version is not None:
            tool.version = new_version
            descriptor_dict = dict(tool.descriptor) if isinstance(tool.descriptor, dict) else {}
            descriptor_dict["version"] = new_version
            tool.descriptor = descriptor_dict
        try:
            await session.flush()
        except StaleDataError as exc:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool") from exc

        if new_version is not None:
            await self._record_snapshot(
                session,
                tool=tool,
                actor_user_id=actor_user_id,
                change_summary=snap_summary or "",
                changes={"version": {"old": old_version, "new": new_version}},
            )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=f"tools.tool.state.{new_state}",
                resource_type="tool",
                resource_id=str(tool.id),
                outcome="success",
                payload={
                    "alias": tool.alias,
                    "previous": previous,
                    "new": new_state,
                    "version_snap": new_version,
                },
            ),
        )
        return tool

    # ---- bundle replacement (major bump) ---------------------------

    async def replace_bundle_and_descriptor(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
        descriptor: ToolDescriptor,
        bundle_path: str,
        expected_row_version: int | None = None,
    ) -> Tool:
        """Combined major-bump operation used by the replace-bundle endpoint.

        Updates ``Tool.bundle_path`` AND ``Tool.descriptor`` together,
        bumps the major version (``X.Y.Z → X+1.0.0``), and writes a
        single :class:`ToolVersion` snapshot capturing both the new
        bundle path and the descriptor diff. The supplied descriptor's
        ``version`` is overridden — server is the authority.

        Refuses on archived tools. Validates the descriptor's team +
        approver references the same way :meth:`install` does.
        """
        await self._require_install_permission(session, actor_user_id)
        tool = await self._fetch_by_alias(session, alias)
        if tool is None:
            raise ToolNotFoundError(alias)
        if tool.alias != descriptor.alias:
            raise AliasMismatchError(tool.alias, descriptor.alias)
        if tool.archived_at is not None:
            raise ToolArchivedError(tool.alias)
        if expected_row_version is not None and tool.row_version != expected_row_version:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool")

        cleaned_bundle = _validate_bundle_path(bundle_path)
        old_version = str(tool.version)
        new_version = bump_major(old_version)
        descriptor = descriptor.model_copy(update={"version": new_version})
        await self._validate_descriptor_references(session, descriptor)

        old_descriptor = dict(tool.descriptor) if isinstance(tool.descriptor, dict) else {}
        new_descriptor_dict = descriptor.model_dump(mode="json")
        # Diff captures any descriptor field changes that came in
        # alongside the bundle replacement; the headline summary is
        # bundle-replace-flavoured but the bullet list still includes
        # the field-level old/new pairs.
        changes = diff_descriptor(old_descriptor, new_descriptor_dict)

        tool.name = descriptor.name
        tool.version = new_version
        tool.descriptor = new_descriptor_dict
        tool.bundle_path = cleaned_bundle
        try:
            await session.flush()
        except StaleDataError as exc:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool") from exc

        await self._record_snapshot(
            session,
            tool=tool,
            actor_user_id=actor_user_id,
            change_summary=headline_for_bundle_replace(old_version, new_version),
            changes=changes,
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tools.tool.replace_bundle",
                resource_type="tool",
                resource_id=str(tool.id),
                outcome="success",
                payload={
                    "alias": tool.alias,
                    "previous_version": old_version,
                    "new_version": new_version,
                    "bundle_path": cleaned_bundle,
                    "changes": changes,
                },
            ),
        )
        return tool

    # ---- in-UI file edit (minor bump — Session C wiring) -----------

    async def apply_file_edit(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
        bundles_dir: Path,
        file_edits: dict[str, str],
        expected_row_version: int | None = None,
    ) -> Tool:
        """Minor-bump: in-UI text-file edit.

        ``file_edits`` is ``{relative_path: <new contents>}``. The
        service owns the disk write end-to-end — it clones the current
        bundle to a fresh ``<bundles_dir>/<alias>/<new_version>/``
        directory and applies the edits there, leaving the previous
        version's directory intact so jobs that captured the previous
        ``tool_version_id`` keep resolving against their original files.

        Bumps minor (X.Y.Z → X.Y+1.0) and records a snapshot whose
        ``changes`` carries ``{path: {"old": <text>, "new": <text>}}``
        truncated to keep the row compact.

        Refuses:

        - empty ``file_edits`` → returns the existing row (no-op).
        - ``descriptor.json`` in the edit set → use
          :meth:`update_descriptor` (patch bump) for metadata changes.
        - paths that escape the bundle directory.
        - paths that don't currently exist in the bundle (creating new
          files isn't a minor change — that's a bundle replacement).

        Args:
            session: Active SQLAlchemy session.
            actor_user_id: The operator triggering the edit.
            alias: Tool to edit.
            bundles_dir: The configured ``ToolsSettings.bundles_dir``;
                used to compute the new per-version directory.
            file_edits: ``{bundle-relative path: new contents}``.
            expected_row_version: Optional optimistic-lock guard.
        """
        await self._require_install_permission(session, actor_user_id)
        tool = await self._fetch_by_alias(session, alias)
        if tool is None:
            raise ToolNotFoundError(alias)
        if tool.archived_at is not None:
            raise ToolArchivedError(tool.alias)
        if expected_row_version is not None and tool.row_version != expected_row_version:
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool")
        if not file_edits:
            return tool

        # Validate every requested path before touching disk: traversal,
        # the descriptor-json carve-out, and existence checks all run
        # against the *current* bundle directory.
        old_bundle = Path(str(tool.bundle_path)).resolve()
        if not old_bundle.is_dir():
            raise InvalidFileEditError(
                "(bundle)",
                "the tool's bundle directory doesn't exist on disk; recover "
                "the bundle from a backup or replace it before editing files",
            )
        old_contents: dict[str, str] = {}
        for rel_path in file_edits:
            old_contents[rel_path] = _read_bundle_file_for_edit(old_bundle, rel_path)

        # Compute the diff and short-circuit if nothing actually changed.
        file_diffs: dict[str, dict[str, str]] = {}
        for rel_path, new_text in file_edits.items():
            if old_contents[rel_path] != new_text:
                file_diffs[rel_path] = {
                    "old": _truncate_for_snapshot(old_contents[rel_path]),
                    "new": _truncate_for_snapshot(new_text),
                }
        if not file_diffs:
            return tool

        # Syntax-validate every changed file before any disk write or
        # version bump. A broken edit (Python, JSON, YAML, TOML)
        # mustn't land on disk or bump the tool — the operator gets a
        # 422 with structured per-line errors rendered inline by the
        # editor. Files with no parser (Markdown, plain text) pass
        # through unchecked.
        for rel_path, new_text in file_edits.items():
            if rel_path not in file_diffs:
                continue
            issues = check_syntax(rel_path, new_text)
            if issues:
                raise SyntaxValidationError(rel_path, [issue.to_dict() for issue in issues])

        old_version = str(tool.version)
        new_version = bump_minor(old_version)
        await self._apply_file_edit_disk_and_db(
            session,
            tool=tool,
            bundles_dir=bundles_dir,
            old_bundle=old_bundle,
            new_version=new_version,
            file_edits=file_edits,
        )

        files = tuple(sorted(file_diffs))
        await self._record_snapshot(
            session,
            tool=tool,
            actor_user_id=actor_user_id,
            change_summary=headline_for_file_edit(old_version, new_version, files),
            changes=dict(file_diffs),
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tools.tool.apply_file_edit",
                resource_type="tool",
                resource_id=str(tool.id),
                outcome="success",
                payload={
                    "alias": tool.alias,
                    "previous_version": old_version,
                    "new_version": new_version,
                    "files": list(files),
                },
            ),
        )
        return tool

    async def _apply_file_edit_disk_and_db(
        self,
        session: AsyncSession,
        *,
        tool: Tool,
        bundles_dir: Path,
        old_bundle: Path,
        new_version: str,
        file_edits: dict[str, str],
    ) -> None:
        """Clone the bundle, write the edits, update the row.

        Extracted from :meth:`apply_file_edit` so the public method
        stays under the branch-count threshold. Owns the on-disk
        cleanup if the DB flush or any disk write fails: the new
        per-version directory is removed before the exception
        propagates so a half-edited bundle never sticks around.
        """
        try:
            new_bundle = copy_bundle_for_minor_bump(
                bundles_dir=bundles_dir,
                alias=tool.alias,
                source_bundle=old_bundle,
                new_version=new_version,
            )
        except BundleStagingError as exc:
            raise InvalidFileEditError("(bundle)", exc.detail) from exc

        try:
            for rel_path, new_text in file_edits.items():
                (new_bundle / rel_path).write_text(new_text, encoding="utf-8")

            descriptor_dict = dict(tool.descriptor) if isinstance(tool.descriptor, dict) else {}
            descriptor_dict["version"] = new_version
            tool.descriptor = descriptor_dict
            tool.version = new_version
            tool.bundle_path = str(new_bundle)
            await session.flush()
        except StaleDataError as exc:
            shutil.rmtree(new_bundle, ignore_errors=True)
            from matofx.hierarchy.errors import ConcurrentModificationError  # noqa: PLC0415

            raise ConcurrentModificationError("tool") from exc
        except OSError as exc:
            shutil.rmtree(new_bundle, ignore_errors=True)
            raise InvalidFileEditError(
                "(bundle)",
                f"writing the edits to the new bundle directory failed: {exc}",
            ) from exc

    # ---- snapshot writer -------------------------------------------

    async def _record_snapshot(
        self,
        session: AsyncSession,
        *,
        tool: Tool,
        actor_user_id: int,
        change_summary: str,
        changes: dict[str, Any],
    ) -> ToolVersion:
        """Insert a :class:`ToolVersion` row capturing the tool's current state.

        Always called *after* the ``Tool`` row's mutations have been
        flushed so ``tool.descriptor`` / ``tool.version`` /
        ``tool.bundle_path`` reflect the new state.

        Computes ``bundle_hash`` (the V&V cohort key per ADR-0011 /
        ``designs/vv-tracking.md`` §6.5) by hashing the bundle directory
        contents at ``tool.bundle_path``. If the bundle path doesn't
        resolve to a directory on disk — common in tests, possible on
        a botched install — the snapshot lands with ``bundle_hash=""``
        (the "unknown" sentinel). An empty hash is treated as a
        singleton cohort by the V&V badge query, so it simply doesn't
        participate in inheritance; the snapshot itself still writes
        cleanly and the change-log narrative keeps working.
        """
        snapshot = ToolVersion(
            alias=tool.alias,
            version=tool.version,
            descriptor_snapshot=(
                dict(tool.descriptor) if isinstance(tool.descriptor, dict) else {}
            ),
            bundle_path=tool.bundle_path,
            bundle_hash=_safe_compute_bundle_hash(tool.bundle_path),
            created_by_user_id=actor_user_id,
            change_summary=change_summary,
            changes=changes,
        )
        session.add(snapshot)
        await session.flush()
        return snapshot

    # ---- internals -------------------------------------------------

    async def _require_install_permission(self, session: AsyncSession, user_id: int) -> None:
        if not await user_has_permission(session, user_id, Permission.TOOLS_INSTALL):
            raise ToolPermissionError

    async def _require_promote_permission(self, session: AsyncSession, user_id: int) -> None:
        if not await user_has_permission(session, user_id, Permission.TOOLS_PROMOTE_STATE):
            raise ToolPermissionError

    async def _fetch_by_alias(self, session: AsyncSession, alias: str) -> Tool | None:
        stmt = select(Tool).where(Tool.alias == alias.strip().lower())
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _validate_descriptor_references(
        self, session: AsyncSession, descriptor: ToolDescriptor
    ) -> None:
        """Resolve teams + approvers; raise the typed error on any miss.

        ``"World"`` (the universal-visibility marker) is always
        considered valid even if the migration hasn't seeded its
        team row — see :data:`WORLD_TEAM_NAME`.
        """
        if descriptor.teams:
            # Filter out the always-valid virtual team before looking
            # up the rest in the DB. That way long-running installs
            # that haven't yet picked up migration 0014 still accept
            # "World" in tool descriptors.
            real_teams = [t for t in descriptor.teams if t != WORLD_TEAM_NAME]
            if real_teams:
                team_stmt = select(Team.name).where(Team.name.in_(real_teams))
                found = set((await session.execute(team_stmt)).scalars())
                unknown = [t for t in real_teams if t not in found]
                if unknown:
                    raise UnknownTeamError(unknown)

        if descriptor.approver_usernames:
            user_stmt = select(User).where(User.username.in_(descriptor.approver_usernames))
            users = list((await session.execute(user_stmt)).scalars())
            by_username = {u.username: u for u in users}
            unknown_users = [u for u in descriptor.approver_usernames if u not in by_username]
            if unknown_users:
                raise UnknownApproverError(unknown_users)
            not_active_or_not_approver = [
                u
                for u in descriptor.approver_usernames
                if not by_username[u].is_active or not by_username[u].is_approver
            ]
            if not_active_or_not_approver:
                raise ApproverNotEnabledError(not_active_or_not_approver)

        await self._validate_material_fields(session, descriptor)

    async def _validate_material_fields(
        self, session: AsyncSession, descriptor: ToolDescriptor
    ) -> None:
        """Resolve every ``material`` input against the live class schema (ADR-0007 §3).

        Each ``material`` field's ``material_class`` must name an
        ``active`` material class, and any explicit ``required_properties``
        must be properties that class declares. The descriptor's own
        validators already guarantee a ``material`` field has a
        ``material_class``, so this is purely the DB-coupled half.
        """
        material_fields = [f for f in descriptor.inputs if f.type == "material"]
        if not material_fields:
            return
        # Lazy import: keeps the tools package from importing the
        # materials ORM at module load (it's only needed here).
        from matofx.materials.orm import MaterialClass, MaterialClassProperty  # noqa: PLC0415

        wanted = {f.material_class for f in material_fields if f.material_class}
        rows = (
            (await session.execute(select(MaterialClass).where(MaterialClass.name.in_(wanted))))
            .scalars()
            .all()
        )
        active_by_name = {c.name: c for c in rows if c.state == "active"}
        for field in material_fields:
            cls = active_by_name.get(field.material_class or "")
            if cls is None:
                raise UnknownMaterialClassError(field.material_class or "", field.name)
            if field.required_properties:
                # Descriptors reference properties by **slug** (the
                # immutable snake_case identifier, ADR-0007 + migration
                # 0050). A class's display ``prop_name`` is editable; the
                # slug stays put, so install-time validation matches by
                # slug too. (For ergonomic backward compatibility on a
                # one-off hand-edited descriptor, we also accept the
                # display label.)
                declared_slugs: set[str] = set()
                declared_names: set[str] = set()
                for row in (
                    await session.execute(
                        select(MaterialClassProperty.slug, MaterialClassProperty.prop_name).where(
                            MaterialClassProperty.class_id == cls.id
                        )
                    )
                ).all():
                    declared_slugs.add(str(row[0]))
                    declared_names.add(str(row[1]))
                missing = [
                    p
                    for p in field.required_properties
                    if p not in declared_slugs and p not in declared_names
                ]
                if missing:
                    raise UnknownMaterialPropertyError(cls.name, missing, field.name)


def _is_forward_skip(current: str, target: str) -> bool:
    """``True`` when ``current → target`` is a forward jump that skips ``test``."""
    cur_rank = _STATE_RANK.get(current)
    tgt_rank = _STATE_RANK.get(target)
    if cur_rank is None or tgt_rank is None:
        return False
    return tgt_rank > cur_rank + 1


def make_tool_service(audit: AuditWriter) -> ToolService:
    """Factory: build a :class:`ToolService` from the audit writer."""
    return ToolService(audit=audit)


# ---- visibility helpers -------------------------------------------

WORLD_TEAM_NAME = "World"
"""Universal-visibility marker. A tool whose ``descriptor.teams`` list
contains this string is visible to every authenticated user (subject
to state-based gates). Seeded by migration 0014; see also
:meth:`ToolService.list_visible` for the rules."""


def _tool_team_names(tool: Tool) -> list[str]:
    """Pull the descriptor's ``teams`` list off the tool row, defensively.

    Older rows or hand-rolled descriptors might be missing the field
    or have a non-list value; treat anything unparseable as empty.
    """
    raw = tool.descriptor if isinstance(tool.descriptor, dict) else {}
    teams = raw.get("teams") if isinstance(raw, dict) else None
    if not isinstance(teams, list):
        return []
    return [str(t) for t in teams if isinstance(t, str)]


def _tool_visible_to_member(tool: Tool, actor_team_names: set[str]) -> bool:
    """Apply the team-membership rule for a non-privileged user.

    See :meth:`ToolService.list_visible` for the spec. Empty team list
    is treated the same as ``["World"]`` (universally visible) for
    backwards-compat with hand-edited descriptors that pre-date the
    World convention.
    """
    tool_teams = _tool_team_names(tool)
    if not tool_teams or WORLD_TEAM_NAME in tool_teams:
        return True
    return any(t in actor_team_names for t in tool_teams)


async def _user_team_names(session: AsyncSession, user_id: int) -> set[str]:
    """Set of team names the user belongs to. Empty when the user has no teams."""
    # Lazy import to keep the team→user dependency one-way at module level.
    from matofx.users.team_orm import UserTeam  # noqa: PLC0415

    stmt = (
        select(Team.name)
        .join(UserTeam, UserTeam.team_id == Team.id)
        .where(UserTeam.user_id == user_id)
    )
    return set((await session.execute(stmt)).scalars())


async def _user_can_see_tool_teams(session: AsyncSession, user_id: int, tool: Tool) -> bool:
    """Single-tool variant of :func:`_tool_visible_to_member`.

    Used by :meth:`ToolService.get` so a direct-by-alias lookup
    matches the listing's filter — same answer either way.
    """
    if await user_has_permission(session, user_id, Permission.TOOLS_SEE_NEW_STATE):
        return True
    actor_teams = await _user_team_names(session, user_id)
    return _tool_visible_to_member(tool, actor_teams)


async def _visibility_context(
    session: AsyncSession, user_id: int
) -> tuple[set[str], set[str], bool]:
    """Pre-compute, once, the three inputs the tool-visibility rule needs.

    Returns ``(visible_states, actor_team_names, privileged)``:

    - ``visible_states`` — the lifecycle states this user may see.
    - ``actor_team_names`` — the user's team names (empty when
      ``privileged``; the rule short-circuits before consulting it).
    - ``privileged`` — holds ``tools.see_new_state`` (admin / super_user),
      which bypasses the team gate entirely.

    Computing this once lets callers that check many tools (listings,
    the workflow member-cascade) keep the per-tool decision pure and
    in-memory via :func:`_tool_visible_given`.
    """
    visible_states = {
        state for state in TOOL_STATES if await user_can_see_state(session, user_id, state)
    }
    privileged = await user_has_permission(session, user_id, Permission.TOOLS_SEE_NEW_STATE)
    actor_team_names: set[str] = set() if privileged else await _user_team_names(session, user_id)
    return visible_states, actor_team_names, privileged


def _tool_visible_given(
    tool: Tool,
    *,
    visible_states: set[str],
    actor_team_names: set[str],
    privileged: bool,
    include_archived: bool = False,
) -> bool:
    """The single, canonical tool-visibility rule (pure / in-memory).

    A tool is visible iff all three gates pass, in conjunction:

    1. **Archived** — archived rows are hidden unless ``include_archived``.
    2. **State** — the tool's lifecycle state is one the user may see.
    3. **Team** — privileged users bypass; everyone else needs the tool
       to be ``World`` / un-grouped, or to share at least one team with
       the user.

    Both the async single-item predicate (:func:`can_user_see_tool`) and
    the batch listing paths feed pre-computed context into this one
    function, so the rule is expressed in exactly one place.
    """
    if tool.archived_at is not None and not include_archived:
        return False
    if tool.state not in visible_states:
        return False
    if privileged:
        return True
    return _tool_visible_to_member(tool, actor_team_names)


async def can_user_see_tool(
    session: AsyncSession,
    user_id: int,
    tool: Tool,
    *,
    include_archived: bool = False,
) -> bool:
    """Canonical single-tool visibility check: archived + state + team.

    This is *the* "can this user see this tool?" predicate. Single-tool
    fetch (:meth:`ToolService.get`), the workflow member-cascade
    (:func:`matofx.workflows.service.can_user_see_workflow`), and the
    job submit/stage gate all resolve to the same answer through it (and
    through the pure :func:`_tool_visible_given` it delegates to). Use
    this — never a partial re-implementation — anywhere a tool is shown,
    fetched, or acted on.
    """
    visible_states, actor_team_names, privileged = await _visibility_context(session, user_id)
    return _tool_visible_given(
        tool,
        visible_states=visible_states,
        actor_team_names=actor_team_names,
        privileged=privileged,
        include_archived=include_archived,
    )


__all__ = ["WORLD_TEAM_NAME", "ToolService", "ToolState", "make_tool_service"]
