"""TutorialService — CRUD + activation cascade for uploaded tutorials.

Construct one per process; the dataclass is cheap and stateless
beyond the audit-writer reference. All state-changing methods record
an audit row through the supplied :class:`AuditWriter`.

Activation rule:

- For tool tutorials (``tool_alias`` non-NULL), activating one row
  cascades the *active siblings of the same media_type* on the same
  tool to inactive — so a tool ends up with at most one active
  document and one active video.
- For workflow tutorials (``workflow_alias`` non-NULL), the same
  cascade applies, scoped to (``workflow_alias``, ``media_type``).
- For general tutorials (both alias columns NULL), activation is
  independent — multiple general docs / videos can all be active.

Tutorials are never deleted: :meth:`update_version` publishes a new
version and **archives** the old one (``superseded_at`` set, kept on
disk for history), and inactive current versions keep their file so
re-activation is instant.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from matofx.audit.schema import RecordInput
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
from matofx.tutorials.orm import MEDIA_TYPES, Tutorial
from matofx.tutorials.storage import (
    is_allowed_extension,
    storage_path_for,
    tutorials_dir,
)
from matofx.workflows.orm import Workflow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


@dataclass(frozen=True)
class TutorialService:
    """CRUD + activation for the ``tutorial`` table."""

    audit: AuditWriter
    tutorials_root: Path
    """Filesystem root for staged tutorial files (resolved from
    :attr:`matofx.config.TutorialsSettings.tutorials_dir`)."""

    max_upload_bytes: int
    """Per-file size cap; matches the API's multipart guardrail."""

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_all(self, session: AsyncSession) -> list[Tutorial]:
        """Return every tutorial row, newest first (admin view)."""
        stmt = select(Tutorial).order_by(Tutorial.created_at.desc(), Tutorial.id.desc())
        return list((await session.execute(stmt)).scalars())

    async def list_active(
        self,
        session: AsyncSession,
        *,
        tool_alias: str | None = None,
        workflow_alias: str | None = None,
    ) -> list[Tutorial]:
        """Return *active* tutorials, optionally scoped to a tool or workflow.

        Both alias args default to ``None`` ⇒ return the active
        **general** tutorials (both scope columns NULL). Supply at most
        one of ``tool_alias`` or ``workflow_alias`` to fetch tutorials
        for that specific scope; supplying both raises
        :class:`ScopeConflictError`.
        """
        if tool_alias is not None and workflow_alias is not None:
            raise ScopeConflictError()
        stmt = select(Tutorial).where(Tutorial.is_active.is_(True))
        if tool_alias is not None:
            stmt = stmt.where(Tutorial.tool_alias == tool_alias)
        elif workflow_alias is not None:
            stmt = stmt.where(Tutorial.workflow_alias == workflow_alias)
        else:
            stmt = stmt.where(
                Tutorial.tool_alias.is_(None),
                Tutorial.workflow_alias.is_(None),
            )
        stmt = stmt.order_by(Tutorial.media_type, Tutorial.created_at.desc())
        return list((await session.execute(stmt)).scalars())

    async def list_active_for_user(self, session: AsyncSession) -> list[Tutorial]:
        """Return every active tutorial across every scope (home-page view).

        Ordering: general first (both scope cols NULL), then tools
        grouped by alias, then workflows grouped by alias. The home
        page re-buckets the result into general / per-tool / per-workflow
        in a single pass, so a stable sort matters more than the exact
        ordering between tool and workflow groups.
        """
        stmt = (
            select(Tutorial)
            .where(Tutorial.is_active.is_(True))
            .order_by(
                # General first (both NULL), then tool-scoped, then workflow-scoped.
                (Tutorial.tool_alias.is_(None) & Tutorial.workflow_alias.is_(None)).desc(),
                Tutorial.tool_alias.is_(None).desc(),
                Tutorial.tool_alias,
                Tutorial.workflow_alias,
                Tutorial.media_type,
                Tutorial.created_at.desc(),
            )
        )
        return list((await session.execute(stmt)).scalars())

    async def get(self, session: AsyncSession, tutorial_id: int) -> Tutorial:
        """Return one row by id; raises :class:`TutorialNotFoundError` when missing."""
        row = await session.get(Tutorial, tutorial_id)
        if row is None:
            raise TutorialNotFoundError(tutorial_id)
        return row

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def create_upload(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        tool_alias: str | None,
        workflow_alias: str | None,
        media_type: str,
        version: str,
        title: str,
        description: str,
        original_filename: str,
        mime_type: str,
        contents: bytes,
        activate: bool,
    ) -> Tutorial:
        """Stage an uploaded file + create the row.

        Validates the chosen media type and file extension, writes
        the file under :attr:`tutorials_root`, and inserts the DB
        row. When ``activate=True`` the activation cascade runs
        immediately (for tool / workflow tutorials — general tutorials
        are independent).

        At most one of ``tool_alias`` / ``workflow_alias`` may be
        non-NULL; both NULL ⇒ a general tutorial.
        """
        if tool_alias is not None and workflow_alias is not None:
            raise ScopeConflictError()
        if media_type not in MEDIA_TYPES:
            raise InvalidMediaTypeError(
                f"{media_type!r} isn't a valid tutorial media type; "
                f"expected one of {sorted(MEDIA_TYPES)}."
            )
        if not is_allowed_extension(original_filename, media_type):
            raise InvalidExtensionError(
                f"The file {original_filename!r} isn't a recognised {media_type} format."
            )
        if len(contents) > self.max_upload_bytes:
            raise UploadTooLargeError(
                f"That file is larger than the per-tutorial cap "
                f"({self.max_upload_bytes // (1024 * 1024)} MiB)."
            )
        if tool_alias is not None:
            tool = (
                await session.execute(select(Tool).where(Tool.alias == tool_alias))
            ).scalar_one_or_none()
            if tool is None:
                raise UnknownToolError(tool_alias)
        if workflow_alias is not None:
            workflow = (
                await session.execute(select(Workflow).where(Workflow.alias == workflow_alias))
            ).scalar_one_or_none()
            if workflow is None:
                raise UnknownWorkflowError(workflow_alias)

        now = _utcnow()
        row = Tutorial(
            tool_alias=tool_alias,
            workflow_alias=workflow_alias,
            media_type=media_type,
            version=version.strip() or "1",
            title=title.strip(),
            description=description.strip(),
            original_filename=original_filename,
            mime_type=mime_type,
            size_bytes=len(contents),
            storage_path="",  # set below once id is known
            is_active=False,  # flipped via activate() below for clean cascade
            uploaded_by_user_id=actor_user_id,
            uploaded_at=now,
        )
        session.add(row)
        await session.flush()

        # A brand-new upload heads its own lineage (later "update" calls
        # create new rows that inherit this id).
        row.lineage_id = row.id

        root = tutorials_dir(self.tutorials_root)
        target = storage_path_for(root, row.id, original_filename)
        target.write_bytes(contents)
        row.storage_path = str(target)
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tutorials.tutorial.upload",
                resource_type="tutorial",
                resource_id=str(row.id),
                outcome="success",
                payload={
                    "tool_alias": tool_alias,
                    "workflow_alias": workflow_alias,
                    "media_type": media_type,
                    "version": row.version,
                    "title": row.title,
                    "size_bytes": row.size_bytes,
                },
            ),
        )

        if activate:
            await self.activate(session, tutorial_id=row.id, actor_user_id=actor_user_id)
        return row

    async def activate(
        self,
        session: AsyncSession,
        *,
        tutorial_id: int,
        actor_user_id: int,
    ) -> Tutorial:
        """Flip ``is_active=True`` on ``tutorial_id``, cascading siblings inactive.

        Cascade applies to **tool** tutorials (``tool_alias`` set) and
        **workflow** tutorials (``workflow_alias`` set), scoped to the
        matching column + ``media_type``. General tutorials activate
        independently (no cap).
        """
        row = await self.get(session, tutorial_id)
        if row.superseded_at is not None:
            raise ArchivedTutorialError(row.id)
        if row.is_active:
            return row

        if row.tool_alias is not None:
            # Cascade: any other active row with the same (tool_alias, media_type)
            # flips to inactive so the home page renders a single active
            # document + a single active video per tool.
            await session.execute(
                update(Tutorial)
                .where(
                    Tutorial.tool_alias == row.tool_alias,
                    Tutorial.media_type == row.media_type,
                    Tutorial.id != row.id,
                    Tutorial.is_active.is_(True),
                )
                .values(is_active=False)
            )
        elif row.workflow_alias is not None:
            # Same cascade, scoped to (workflow_alias, media_type).
            await session.execute(
                update(Tutorial)
                .where(
                    Tutorial.workflow_alias == row.workflow_alias,
                    Tutorial.media_type == row.media_type,
                    Tutorial.id != row.id,
                    Tutorial.is_active.is_(True),
                )
                .values(is_active=False)
            )

        row.is_active = True
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tutorials.tutorial.activate",
                resource_type="tutorial",
                resource_id=str(row.id),
                outcome="success",
                payload={
                    "tool_alias": row.tool_alias,
                    "workflow_alias": row.workflow_alias,
                    "media_type": row.media_type,
                },
            ),
        )
        return row

    async def deactivate(
        self,
        session: AsyncSession,
        *,
        tutorial_id: int,
        actor_user_id: int,
    ) -> Tutorial:
        """Flip ``is_active=False`` on ``tutorial_id``."""
        row = await self.get(session, tutorial_id)
        if not row.is_active:
            return row
        row.is_active = False
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tutorials.tutorial.deactivate",
                resource_type="tutorial",
                resource_id=str(row.id),
                outcome="success",
            ),
        )
        return row

    async def update_version(
        self,
        session: AsyncSession,
        *,
        tutorial_id: int,
        actor_user_id: int,
        version: str,
        title: str,
        description: str,
        original_filename: str,
        mime_type: str,
        contents: bytes,
    ) -> Tutorial:
        """Publish a new version of a tutorial, archiving the old one.

        Creates a new row in the **same scope and media type** as the
        source, inheriting its ``lineage_id``, and stages the new file.
        The source row is then marked archived (``superseded_at`` set,
        forced inactive) — its file is **kept** on disk for history. If
        the source was the active (shown) version, the new version takes
        over as active (running the usual sibling cascade for
        tool/workflow scope); if the source was hidden, the new version
        is created hidden too, leaving the admin to activate it.

        Scope and media type are fixed by the source — an update replaces
        like with like. The title/description may change; the version
        label defaults to the source's when left blank.

        Raises :class:`ArchivedTutorialError` if the target is itself an
        archived previous version (update the current one instead).
        """
        source = await self.get(session, tutorial_id)
        if source.superseded_at is not None:
            raise ArchivedTutorialError(source.id)
        if not is_allowed_extension(original_filename, source.media_type):
            raise InvalidExtensionError(
                f"The file {original_filename!r} isn't a recognised {source.media_type} format."
            )
        if len(contents) > self.max_upload_bytes:
            raise UploadTooLargeError(
                f"That file is larger than the per-tutorial cap "
                f"({self.max_upload_bytes // (1024 * 1024)} MiB)."
            )

        now = _utcnow()
        was_active = source.is_active
        new_row = Tutorial(
            tool_alias=source.tool_alias,
            workflow_alias=source.workflow_alias,
            media_type=source.media_type,
            version=version.strip() or source.version,
            title=title.strip() or source.title,
            description=description.strip(),
            original_filename=original_filename,
            mime_type=mime_type,
            size_bytes=len(contents),
            storage_path="",  # set below once id is known
            is_active=False,  # flipped via activate() below when inheriting visibility
            uploaded_by_user_id=actor_user_id,
            uploaded_at=now,
            lineage_id=source.lineage_id or source.id,
        )
        session.add(new_row)
        await session.flush()

        root = tutorials_dir(self.tutorials_root)
        target = storage_path_for(root, new_row.id, original_filename)
        target.write_bytes(contents)
        new_row.storage_path = str(target)

        # Archive the source: keep the row + file, but mark it a previous
        # version and ensure it's no longer surfaced to users.
        source.superseded_at = now
        source.is_active = False
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="tutorials.tutorial.update",
                resource_type="tutorial",
                resource_id=str(new_row.id),
                outcome="success",
                payload={
                    "supersedes_id": source.id,
                    "lineage_id": new_row.lineage_id,
                    "tool_alias": new_row.tool_alias,
                    "workflow_alias": new_row.workflow_alias,
                    "media_type": new_row.media_type,
                    "version": new_row.version,
                    "title": new_row.title,
                    "size_bytes": new_row.size_bytes,
                },
            ),
        )

        if was_active:
            await self.activate(session, tutorial_id=new_row.id, actor_user_id=actor_user_id)
        return new_row


def make_tutorial_service(
    audit: AuditWriter,
    *,
    tutorials_root: Path,
    max_upload_bytes: int,
) -> TutorialService:
    """Wire up a :class:`TutorialService` from validated config + audit writer."""
    return TutorialService(
        audit=audit,
        tutorials_root=tutorials_root,
        max_upload_bytes=max_upload_bytes,
    )
