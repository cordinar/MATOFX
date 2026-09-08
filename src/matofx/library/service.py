"""LibraryService — the governed file-library lifecycle (ADR-0006 §4).

A library file is a reusable, governed tool-input file with immutable,
content-hashed versions, organised on two independent axes — tiered
**scope** (personal → project → global) and **tool assignment**
(tool → field → file, multi-tool). This service owns the lifecycle and
its audit trail:

- ``create_file`` / ``add_version`` — append-only immutable versioning
  (each version's bytes land on the active :class:`StorageLocation`
  mount via :mod:`matofx.library.storage`, hashed for integrity).
- ``update_metadata`` — entry-level fields, optimistic-locked.
- ``assign_tool`` / ``unassign_tool`` — the tool-assignment layer.
- ``set_scope`` — the governed scope transition; landing in **global**
  enters the directed-approver gate (ADR-0006 §5) when the install
  toggle is on, or publishes directly when a curator does it with the
  toggle off.
- ``decide_approval`` — apply a curator's approve/reject to a pending
  global publish (mirrors :meth:`StudyService.decide_approval`).
- ``retire`` / ``restore`` — soft delete, reversible by state (never a
  hard delete).
- ``list_for_picker`` — the §3 visible-files query for tool forms.
- ``open_version_bytes`` — access-checked read for download + promotion.

**Authorisation split** (mirrors the jobs/studies pattern): the service
resolves project ACLs and the ``LIBRARY_CURATE`` capability itself from
live state, but the install-level *toggle*
(``library_global_publish_approval``) and the *chosen approver* are
resolved by the caller (the API layer, reading General Settings) and
passed in — keeping the General-Settings dependency out of this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from matofx.audit.schema import RecordInput
from matofx.hierarchy.errors import (
    AccessDeniedError,
    NotFoundError,
    StorageLocationUnavailableError,
)
from matofx.hierarchy.orm import Project, ProjectAccessLevel
from matofx.hierarchy.service import _require_project_access, _resolve_access
from matofx.library import storage
from matofx.library.errors import (
    ApproverRequiredError,
    DuplicateToolAssignmentError,
    GlobalPublishNotPermittedError,
    InvalidScopeError,
    LibraryAccessDeniedError,
    LibraryConcurrentModificationError,
    LibraryFileNotFoundError,
    LibraryFileRetiredError,
    LibraryValidationError,
    LibraryVersionNotFoundError,
    ProjectScopeError,
    ToolAssignmentNotFoundError,
    ToolAssignmentRequiredError,
)
from matofx.library.orm import (
    LIBRARY_SCOPES,
    JobLibraryFileUse,
    LibraryFile,
    LibraryFileTool,
    LibraryFileVersion,
)
from matofx.users.permissions import (
    Permission,
    user_has_any_permission,
    user_has_permission,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.approvals import ApprovalRequest, Decision
    from matofx.audit import AuditWriter
    from matofx.tools.exec_orm import StorageLocation

# The approval kind used for a global-publish review. The approvals model
# is shared with jobs/studies (``pre`` = review-before, ``post`` =
# review-after); a library publish is reviewed *before* it becomes
# pickable, so it is a ``pre`` gate.
_PUBLISH_KIND = "pre"


@dataclass(frozen=True)
class ToolAssignmentSpec:
    """One ``(tool_alias[, field_name])`` mapping for :meth:`create_file`."""

    tool_alias: str
    field_name: str | None = None


@dataclass(frozen=True)
class PickerCandidate:
    """A file + the resolved version a tool form should offer (§3).

    Returned by :meth:`LibraryService.list_for_picker`; carries just
    enough for the form widget to render a choice and, on selection,
    record the ``version_id`` against the draft field.
    """

    file_id: int
    name: str
    description: str
    scope: str
    file_kind: str | None
    tags: list[str]
    version_id: int
    ordinal: int
    original_filename: str
    extension: str
    size_bytes: int
    content_hash: str


@dataclass(frozen=True)
class LibraryService:
    """Lifecycle + audit for governed library files (ADR-0006)."""

    audit: AuditWriter
    environment: str | None = None
    """Threaded into the on-disk path segment for stored bytes
    (``library/<env>/...``), mirroring how :class:`JobService` /
    :class:`StudyService` segment the shared mount per deployment."""

    # ----- create / version --------------------------------------------

    async def create_file(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        name: str,
        description: str,
        original_filename: str,
        content: bytes,
        provenance: str,
        tool_assignments: list[ToolAssignmentSpec],
        scope: str = "personal",
        project_id: int | None = None,
        file_kind: str | None = None,
        tags: list[str] | None = None,
        handling_label: str | None = None,
        reference: str | None = None,
        version_notes: str | None = None,
        approval_required: bool = True,
        designated_approver_user_id: int | None = None,
        designated_approver_username: str | None = None,
    ) -> LibraryFile:
        """Create a library file with its first immutable version.

        ``scope`` defaults to ``personal``. A ``project`` scope requires
        EDIT (Member+) on ``project_id``; a ``global`` scope enters the
        directed-approver gate (§5) when ``approval_required`` is on, or
        publishes directly when a curator does it with the toggle off.
        At least one tool assignment is required so the file is pickable
        somewhere.
        """
        await self._require_create(session, actor_user_id)
        self._check_scope(scope, project_id)
        clean_name = name.strip()
        if not clean_name:
            raise LibraryValidationError("A library file needs a name.")
        clean_provenance = provenance.strip()
        if not clean_provenance:
            raise LibraryValidationError(
                "A library file version needs a provenance note (its source)."
            )
        specs = self._dedupe_assignments(tool_assignments)

        if scope == "project":
            assert project_id is not None  # noqa: S101 — guaranteed by _check_scope
            await self._require_project_edit(session, project_id, actor_user_id)

        # Validate the global-landing preconditions *before* writing the
        # write-once bytes — see _require_landing_permitted.
        await self._require_landing_permitted(
            session,
            scope=scope,
            actor_user_id=actor_user_id,
            approval_required=approval_required,
            designated_approver_user_id=designated_approver_user_id,
            designated_approver_username=designated_approver_username,
        )

        location = await self._active_storage(session)

        lib = LibraryFile(
            name=clean_name,
            description=description,
            file_kind=file_kind,
            tags=list(tags or []),
            scope=scope,
            project_id=project_id,
            owner_user_id=actor_user_id,
            handling_label=handling_label,
            state="active",
            created_by_user_id=actor_user_id,
        )
        session.add(lib)
        await session.flush()

        stored = storage.store_version_bytes(
            mount_path=location.mount_path,
            environment=self.environment,
            file_id=lib.id,
            ordinal=1,
            filename=original_filename,
            content=content,
        )
        version = LibraryFileVersion(
            library_file_id=lib.id,
            ordinal=1,
            stored_path=stored.stored_path,
            original_filename=stored.original_filename,
            content_hash=stored.content_hash,
            size_bytes=stored.size_bytes,
            extension=stored.extension,
            provenance=clean_provenance,
            reference=reference,
            version_notes=version_notes,
            publish_state="draft",
            created_by_user_id=actor_user_id,
        )
        session.add(version)
        for spec in specs:
            session.add(
                LibraryFileTool(
                    library_file_id=lib.id,
                    tool_alias=spec.tool_alias,
                    field_name=spec.field_name,
                )
            )
        await session.flush()

        await self._apply_landing_gate(
            session,
            file=lib,
            version=version,
            actor_user_id=actor_user_id,
            approval_required=approval_required,
            designated_approver_user_id=designated_approver_user_id,
            designated_approver_username=designated_approver_username,
            is_new_file=True,
        )
        await session.flush()

        await self._audit(
            session,
            actor_user_id,
            "library.file.create",
            lib,
            payload={
                "name": lib.name,
                "scope": lib.scope,
                "project_id": lib.project_id,
                "tools": [s.tool_alias for s in specs],
                "version_id": version.id,
                "publish_state": version.publish_state,
                "content_hash": version.content_hash,
            },
        )
        return lib

    async def add_version(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        file_id: int,
        original_filename: str,
        content: bytes,
        provenance: str,
        reference: str | None = None,
        version_notes: str | None = None,
        approval_required: bool = True,
        designated_approver_user_id: int | None = None,
        designated_approver_username: str | None = None,
    ) -> LibraryFileVersion:
        """Append a new immutable version (``ordinal + 1``).

        Requires mutate access on the file's current scope. A new
        version of an already-**global** file "lands in global" and so
        re-enters the publish gate (§5); for personal / project files it
        is published immediately.
        """
        lib = await self._get_visible_file(session, file_id, actor_user_id)
        if lib.state == "retired":
            raise LibraryFileRetiredError(file_id)
        await self._require_mutate_access(session, lib, actor_user_id)

        clean_provenance = provenance.strip()
        if not clean_provenance:
            raise LibraryValidationError(
                "A library file version needs a provenance note (its source)."
            )

        # A new version of an already-global file re-enters the publish
        # gate; validate its preconditions before the write-once bytes
        # land (see _require_landing_permitted).
        await self._require_landing_permitted(
            session,
            scope=lib.scope,
            actor_user_id=actor_user_id,
            approval_required=approval_required,
            designated_approver_user_id=designated_approver_user_id,
            designated_approver_username=designated_approver_username,
        )

        location = await self._active_storage(session)
        ordinal = await self._next_ordinal(session, file_id)
        stored = storage.store_version_bytes(
            mount_path=location.mount_path,
            environment=self.environment,
            file_id=file_id,
            ordinal=ordinal,
            filename=original_filename,
            content=content,
        )
        version = LibraryFileVersion(
            library_file_id=file_id,
            ordinal=ordinal,
            stored_path=stored.stored_path,
            original_filename=stored.original_filename,
            content_hash=stored.content_hash,
            size_bytes=stored.size_bytes,
            extension=stored.extension,
            provenance=clean_provenance,
            reference=reference,
            version_notes=version_notes,
            publish_state="draft",
            created_by_user_id=actor_user_id,
        )
        session.add(version)
        await session.flush()

        await self._apply_landing_gate(
            session,
            file=lib,
            version=version,
            actor_user_id=actor_user_id,
            approval_required=approval_required,
            designated_approver_user_id=designated_approver_user_id,
            designated_approver_username=designated_approver_username,
            is_new_file=False,
        )
        await session.flush()

        await self._audit(
            session,
            actor_user_id,
            "library.file.version.add",
            lib,
            payload={
                "version_id": version.id,
                "ordinal": ordinal,
                "publish_state": version.publish_state,
                "content_hash": version.content_hash,
            },
        )
        return version

    # ----- metadata / tools / scope ------------------------------------

    async def update_metadata(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        file_id: int,
        expected_version: int | None = None,
        name: str | None = None,
        description: str | None = None,
        file_kind: str | None = None,
        tags: list[str] | None = None,
        handling_label: str | None = None,
    ) -> LibraryFile:
        """Edit entry-level metadata; mutate access + optimistic lock.

        Pass ``expected_version`` (the version the caller saw) to opt
        into optimistic locking. ``None`` arguments are left unchanged;
        this slice does not support clearing a nullable field back to
        NULL (pass an explicit value instead).
        """
        lib = await self._get_visible_file(session, file_id, actor_user_id)
        if lib.state == "retired":
            raise LibraryFileRetiredError(file_id)
        await self._require_mutate_access(session, lib, actor_user_id)
        self._check_optimistic_lock(lib, expected_version)

        if name is not None:
            clean = name.strip()
            if not clean:
                raise LibraryValidationError("A library file needs a name.")
            lib.name = clean
        if description is not None:
            lib.description = description
        if file_kind is not None:
            lib.file_kind = file_kind
        if tags is not None:
            lib.tags = list(tags)
        if handling_label is not None:
            lib.handling_label = handling_label
        await self._flush_optimistic(session)

        await self._audit(
            session,
            actor_user_id,
            "library.file.update",
            lib,
            payload={"name": lib.name},
        )
        return lib

    async def assign_tool(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        file_id: int,
        tool_alias: str,
        field_name: str | None = None,
    ) -> LibraryFileTool:
        """Assign the file to a ``(tool_alias[, field_name])`` pair."""
        lib = await self._get_visible_file(session, file_id, actor_user_id)
        if lib.state == "retired":
            raise LibraryFileRetiredError(file_id)
        await self._require_mutate_access(session, lib, actor_user_id)

        alias = tool_alias.strip()
        field = field_name.strip() if field_name else None
        row = LibraryFileTool(library_file_id=file_id, tool_alias=alias, field_name=field)
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateToolAssignmentError(alias, field) from exc

        await self._audit(
            session,
            actor_user_id,
            "library.file.tool.assign",
            lib,
            payload={"tool_alias": alias, "field_name": field},
        )
        return row

    async def unassign_tool(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        file_id: int,
        tool_alias: str,
        field_name: str | None = None,
    ) -> None:
        """Remove a tool assignment; the last one can't be removed.

        Every file must stay pickable by at least one tool, so removing
        the final assignment raises :class:`ToolAssignmentRequiredError`.
        """
        lib = await self._get_visible_file(session, file_id, actor_user_id)
        if lib.state == "retired":
            raise LibraryFileRetiredError(file_id)
        await self._require_mutate_access(session, lib, actor_user_id)

        alias = tool_alias.strip()
        field = field_name.strip() if field_name else None
        rows = list(
            (
                await session.execute(
                    select(LibraryFileTool).where(LibraryFileTool.library_file_id == file_id)
                )
            ).scalars()
        )
        match = next(
            (r for r in rows if r.tool_alias == alias and r.field_name == field),
            None,
        )
        if match is None:
            raise ToolAssignmentNotFoundError
        if len(rows) <= 1:
            raise ToolAssignmentRequiredError
        await session.delete(match)
        await session.flush()

        await self._audit(
            session,
            actor_user_id,
            "library.file.tool.unassign",
            lib,
            payload={"tool_alias": alias, "field_name": field},
        )

    async def set_scope(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        file_id: int,
        new_scope: str,
        project_id: int | None = None,
        expected_version: int | None = None,
        approval_required: bool = True,
        designated_approver_user_id: int | None = None,
        designated_approver_username: str | None = None,
    ) -> LibraryFile:
        """Change a file's scope; landing in global enters the gate (§5).

        Mutate access is checked against the file's **current** scope
        (you must be able to manage it as it is now). Moving to a
        ``project`` scope requires a ``project_id``; moving to ``global``
        re-enters the publish gate for the file's latest version.
        """
        self._check_scope(new_scope, project_id)
        lib = await self._get_visible_file(session, file_id, actor_user_id)
        if lib.state == "retired":
            raise LibraryFileRetiredError(file_id)
        await self._require_mutate_access(session, lib, actor_user_id)
        self._check_optimistic_lock(lib, expected_version)

        if new_scope == lib.scope and project_id == lib.project_id:
            return lib  # idempotent no-op

        # Moving *into* a project requires EDIT on the destination project.
        if new_scope == "project":
            assert project_id is not None  # noqa: S101 — guaranteed by _check_scope
            await self._require_project_edit(session, project_id, actor_user_id)

        previous = lib.scope
        lib.scope = new_scope
        lib.project_id = project_id

        if new_scope == "global":
            version = await self._latest_version(session, file_id)
            if version is None:
                raise LibraryVersionNotFoundError(0)
            await self._apply_landing_gate(
                session,
                file=lib,
                version=version,
                actor_user_id=actor_user_id,
                approval_required=approval_required,
                designated_approver_user_id=designated_approver_user_id,
                designated_approver_username=designated_approver_username,
                is_new_file=False,
            )
        await self._flush_optimistic(session)

        await self._audit(
            session,
            actor_user_id,
            "library.file.scope.change",
            lib,
            payload={"from": previous, "to": new_scope, "project_id": project_id},
        )
        return lib

    # ----- approval decision (ADR-0006 §5.4) ---------------------------

    async def decide_approval(
        self,
        session: AsyncSession,
        *,
        request_id: int,
        decider_user_id: int,
        decider_username: str,
        decision: Decision,
        reason: str | None = None,
        decider_is_approver: bool,
        decider_is_admin: bool,
    ) -> tuple[ApprovalRequest, LibraryFileVersion]:
        """Apply a curator's approve/reject to a pending global publish.

        Delegates the decision + the self-approval / eligibility defence
        to :class:`ApprovalService`, then applies the library-side
        effect:

        - **approved** → version ``publish_state='published'`` (pickable);
          a brand-new pending file becomes ``active``.
        - **denied** → version ``publish_state='rejected'``; the file
          keeps its prior published version + scope (its ``state`` is
          left as-is so the submitter can revise and re-propose).

        ``decider_is_approver`` is the caller-resolved "is this user
        still an eligible curator" fact (for a directed library gate the
        API resolves it from :attr:`Permission.LIBRARY_CURATE`).
        """
        from matofx.approvals import make_approval_service  # noqa: PLC0415

        request = await make_approval_service(self.audit).decide(
            session,
            request_id=request_id,
            decider_user_id=decider_user_id,
            decider_username=decider_username,
            decision=decision,
            reason=reason,
            decider_is_approver=decider_is_approver,
            decider_is_admin=decider_is_admin,
        )
        if request.library_file_version_id is None:
            raise LibraryVersionNotFoundError(0)
        version = await session.get(LibraryFileVersion, request.library_file_version_id)
        if version is None:
            raise LibraryVersionNotFoundError(request.library_file_version_id)
        lib = await session.get(LibraryFile, version.library_file_id)
        if lib is None:
            raise LibraryFileNotFoundError(version.library_file_id)

        if request.status == "approved":
            version.publish_state = "published"
            if lib.state == "pending":
                lib.state = "active"
            action, audit_reason = "library.file.approve", None
        else:
            version.publish_state = "rejected"
            action, audit_reason = "library.file.reject", request.decision_reason
        await session.flush()

        await self._audit(
            session,
            decider_user_id,
            action,
            lib,
            payload={
                "version_id": version.id,
                "decided_via": request.decided_via,
            },
            reason=audit_reason,
        )
        return request, version

    # ----- retire / restore --------------------------------------------

    async def retire(
        self, session: AsyncSession, *, actor_user_id: int, file_id: int
    ) -> LibraryFile:
        """Soft-delete: ``state='retired'`` (never a hard delete). Idempotent."""
        lib = await self._get_visible_file(session, file_id, actor_user_id)
        await self._require_mutate_access(session, lib, actor_user_id)
        if lib.state == "retired":
            return lib
        lib.state = "retired"
        await session.flush()
        await self._audit(session, actor_user_id, "library.file.retire", lib, payload={})
        return lib

    async def restore(
        self, session: AsyncSession, *, actor_user_id: int, file_id: int
    ) -> LibraryFile:
        """Reverse a retire: ``state='active'``. Idempotent."""
        lib = await self._get_visible_file(session, file_id, actor_user_id)
        await self._require_mutate_access(session, lib, actor_user_id)
        if lib.state != "retired":
            return lib
        lib.state = "active"
        await session.flush()
        await self._audit(session, actor_user_id, "library.file.restore", lib, payload={})
        return lib

    # ----- reads --------------------------------------------------------

    async def get_file(
        self, session: AsyncSession, *, actor_user_id: int, file_id: int
    ) -> LibraryFile:
        """Fetch one file the caller can see, else :class:`LibraryFileNotFoundError`."""
        return await self._get_visible_file(session, file_id, actor_user_id)

    async def list_versions(
        self, session: AsyncSession, *, actor_user_id: int, file_id: int
    ) -> list[LibraryFileVersion]:
        """Every version of a visible file, newest first."""
        await self._get_visible_file(session, file_id, actor_user_id)
        stmt = (
            select(LibraryFileVersion)
            .where(LibraryFileVersion.library_file_id == file_id)
            .order_by(LibraryFileVersion.ordinal.desc())
        )
        return list((await session.execute(stmt)).scalars())

    async def get_version(
        self, session: AsyncSession, *, actor_user_id: int, version_id: int
    ) -> LibraryFileVersion:
        """Fetch one version the caller can see (file-scope-gated).

        Raises :class:`LibraryVersionNotFoundError` for an unknown id;
        :class:`LibraryFileNotFoundError` if the owning file is hidden
        to the actor by the scope rules (ADR-0006 §3). The lineage
        layer (ADR-0010) reaches for this as the
        :attr:`matofx.lineage.types.NodeKind.LIBRARY_FILE_VERSION`
        resolver — version-scoped visibility is the file's visibility.
        """
        version = await session.get(LibraryFileVersion, version_id)
        if version is None:
            raise LibraryVersionNotFoundError(version_id)
        await self._get_visible_file(session, version.library_file_id, actor_user_id)
        return version

    async def list_tool_assignments(
        self, session: AsyncSession, *, actor_user_id: int, file_id: int
    ) -> list[LibraryFileTool]:
        """Every tool assignment of a visible file."""
        await self._get_visible_file(session, file_id, actor_user_id)
        stmt = (
            select(LibraryFileTool)
            .where(LibraryFileTool.library_file_id == file_id)
            .order_by(LibraryFileTool.id.asc())
        )
        return list((await session.execute(stmt)).scalars())

    async def list_files(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        scope: str | None = None,
        tool_alias: str | None = None,
        include_retired: bool = False,
    ) -> list[LibraryFile]:
        """Browse files the caller can see (scope-filtered), newest first.

        Optional ``scope`` / ``tool_alias`` filters narrow the set;
        retired files are excluded unless ``include_retired``. Pending
        global files surface only to their owner or a curator.
        """
        stmt = select(LibraryFile)
        if scope is not None:
            stmt = stmt.where(LibraryFile.scope == scope)
        if not include_retired:
            stmt = stmt.where(LibraryFile.state != "retired")
        if tool_alias is not None:
            assigned = select(LibraryFileTool.library_file_id).where(
                LibraryFileTool.tool_alias == tool_alias.strip()
            )
            stmt = stmt.where(LibraryFile.id.in_(assigned))
        stmt = stmt.order_by(LibraryFile.updated_at.desc(), LibraryFile.id.desc())
        rows = list((await session.execute(stmt)).scalars())

        is_curator = await user_has_permission(session, actor_user_id, Permission.LIBRARY_CURATE)
        project_access: dict[int, bool] = {}
        out: list[LibraryFile] = []
        for lib in rows:
            if await self._visible(session, lib, actor_user_id, is_curator, project_access):
                out.append(lib)
        return out

    async def list_for_picker(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        tool_alias: str,
        field_name: str,
        accepted_extensions: set[str] | None = None,
    ) -> list[PickerCandidate]:
        """The §3 visible-files query for a tool form's file field.

        Returns the latest **published** version of every ``active``
        file the caller can see that is assigned to ``tool_alias`` either

        - pinned to ``field_name``, or
        - via a NULL-field ("any compatible field") assignment whose
          version extension is in ``accepted_extensions``.

        ``accepted_extensions`` is the field's ``accepted_file_types``,
        resolved by the caller against the **selected tool version**
        (the descriptor lives outside this service). ``None`` means "no
        extension filter" — NULL-field assignments match any extension.
        A pending (un-approved) global file is never returned here; it
        surfaces only in the curator approval inbox until approved.
        """
        alias = tool_alias.strip()
        field = field_name.strip()
        exts = {e.lower().lstrip(".") for e in accepted_extensions} if accepted_extensions else None

        assignments = list(
            (
                await session.execute(
                    select(LibraryFileTool).where(LibraryFileTool.tool_alias == alias)
                )
            ).scalars()
        )
        # file_id -> (field_pinned, null_assigned)
        flags: dict[int, tuple[bool, bool]] = {}
        for a in assignments:
            pinned, null_a = flags.get(a.library_file_id, (False, False))
            if a.field_name == field:
                pinned = True
            elif a.field_name is None:
                null_a = True
            flags[a.library_file_id] = (pinned, null_a)

        is_curator = await user_has_permission(session, actor_user_id, Permission.LIBRARY_CURATE)
        project_access: dict[int, bool] = {}
        out: list[PickerCandidate] = []
        for file_id, (pinned, null_a) in flags.items():
            if not (pinned or null_a):
                continue
            lib = await session.get(LibraryFile, file_id)
            if lib is None or lib.state != "active":
                continue
            if not await self._visible(session, lib, actor_user_id, is_curator, project_access):
                continue
            version = await self._latest_version(session, file_id, published_only=True)
            if version is None:
                continue
            # Field-pinned assignment always qualifies; a NULL-field-only
            # match must satisfy the field's accepted extensions.
            if not pinned and exts is not None and version.extension not in exts:
                continue
            out.append(
                PickerCandidate(
                    file_id=lib.id,
                    name=lib.name,
                    description=lib.description,
                    scope=lib.scope,
                    file_kind=lib.file_kind,
                    tags=list(lib.tags or []),
                    version_id=version.id,
                    ordinal=version.ordinal,
                    original_filename=version.original_filename,
                    extension=version.extension,
                    size_bytes=version.size_bytes,
                    content_hash=version.content_hash,
                )
            )
        out.sort(key=lambda c: (c.scope, c.name.lower(), c.file_id))
        return out

    async def open_version_bytes(
        self, session: AsyncSession, *, actor_user_id: int, version_id: int
    ) -> tuple[LibraryFileVersion, bytes]:
        """Read a version's bytes (access-checked + audited).

        Visibility follows the file: an ``active`` file by scope, a
        pending / retired file only for its owner or a curator. The read
        is audited at the same level as a job-output download (§9).
        """
        version = await session.get(LibraryFileVersion, version_id)
        if version is None:
            raise LibraryVersionNotFoundError(version_id)
        lib = await self._get_visible_file(session, version.library_file_id, actor_user_id)
        content = storage.read_version_bytes(version.stored_path)
        await self._audit(
            session,
            actor_user_id,
            "library.file.download",
            lib,
            payload={"version_id": version.id, "ordinal": version.ordinal},
        )
        return version, content

    # ----- provenance (form integration seam, §2.4) --------------------

    async def record_job_use(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        field_name: str,
        library_file_version_id: int,
    ) -> JobLibraryFileUse:
        """Record that a job field was fed by a library version (§2.4).

        Written at submit/promotion time by the form-integration path
        (slice 6). Composite PK ``(job_id, field_name)`` — at most one
        library version per field per job.
        """
        row = JobLibraryFileUse(
            job_id=job_id,
            field_name=field_name,
            library_file_version_id=library_file_version_id,
        )
        session.add(row)
        await session.flush()
        return row

    # ----- internals ----------------------------------------------------

    @staticmethod
    def _check_scope(scope: str, project_id: int | None) -> None:
        if scope not in LIBRARY_SCOPES:
            raise InvalidScopeError(scope)
        if scope == "project" and project_id is None:
            raise ProjectScopeError("A project-scoped library file needs a project.")
        if scope != "project" and project_id is not None:
            raise ProjectScopeError("Only a project-scoped library file can belong to a project.")

    @staticmethod
    def _dedupe_assignments(
        specs: list[ToolAssignmentSpec],
    ) -> list[ToolAssignmentSpec]:
        if not specs:
            raise ToolAssignmentRequiredError
        seen: set[tuple[str, str | None]] = set()
        out: list[ToolAssignmentSpec] = []
        for s in specs:
            alias = s.tool_alias.strip()
            field = s.field_name.strip() if s.field_name else None
            if not alias:
                continue
            key = (alias, field)
            if key in seen:
                continue
            seen.add(key)
            out.append(ToolAssignmentSpec(tool_alias=alias, field_name=field))
        if not out:
            raise ToolAssignmentRequiredError
        return out

    async def _active_storage(self, session: AsyncSession) -> StorageLocation:
        from matofx.tools.exec_orm import StorageLocation  # noqa: PLC0415

        location = (
            await session.execute(
                select(StorageLocation)
                .where(StorageLocation.is_active.is_(True))
                .where(StorageLocation.retired_at.is_(None))
                .order_by(StorageLocation.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if location is None:
            raise StorageLocationUnavailableError
        return location

    async def _next_ordinal(self, session: AsyncSession, file_id: int) -> int:
        current = (
            await session.execute(
                select(func.max(LibraryFileVersion.ordinal)).where(
                    LibraryFileVersion.library_file_id == file_id
                )
            )
        ).scalar()
        return int(current or 0) + 1

    async def _latest_version(
        self, session: AsyncSession, file_id: int, *, published_only: bool = False
    ) -> LibraryFileVersion | None:
        stmt = select(LibraryFileVersion).where(LibraryFileVersion.library_file_id == file_id)
        if published_only:
            stmt = stmt.where(LibraryFileVersion.publish_state == "published")
        stmt = stmt.order_by(LibraryFileVersion.ordinal.desc()).limit(1)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _require_landing_permitted(
        self,
        session: AsyncSession,
        *,
        scope: str,
        actor_user_id: int,
        approval_required: bool,
        designated_approver_user_id: int | None,
        designated_approver_username: str | None,
    ) -> None:
        """Validate a global-landing op *before* its write-once bytes land.

        :meth:`matofx.library.storage.store_version_bytes` is write-once
        and non-transactional, so any failure raised *after* it would
        leave orphaned bytes on disk (and, on SQLite, a later file
        reusing the rolled-back surrogate id collides with them). This
        pre-flight raises the same errors :meth:`_apply_landing_gate`
        would — :class:`ApproverRequiredError` (gate on, no approver) and
        :class:`GlobalPublishNotPermittedError` (gate off, non-curator) —
        so the bytes are never written when the op can't proceed.
        Non-global scopes always publish immediately and never fail here.
        """
        if scope != "global":
            return
        if approval_required:
            username = (designated_approver_username or "").strip()
            if designated_approver_user_id is None or not username:
                raise ApproverRequiredError
            return
        if not await user_has_permission(session, actor_user_id, Permission.LIBRARY_CURATE):
            raise GlobalPublishNotPermittedError

    async def _apply_landing_gate(
        self,
        session: AsyncSession,
        *,
        file: LibraryFile,
        version: LibraryFileVersion,
        actor_user_id: int,
        approval_required: bool,
        designated_approver_user_id: int | None,
        designated_approver_username: str | None,
        is_new_file: bool,
    ) -> None:
        """Publish or gate a version per its file's scope (ADR-0006 §5)."""
        from matofx.approvals import make_approval_service  # noqa: PLC0415

        if file.scope != "global":
            version.publish_state = "published"
            return
        if approval_required:
            username = (designated_approver_username or "").strip()
            if designated_approver_user_id is None or not username:
                raise ApproverRequiredError
            version.publish_state = "pending"
            if is_new_file:
                file.state = "pending"
            await session.flush()
            await make_approval_service(self.audit).create_request(
                session,
                library_file_version_id=version.id,
                kind=_PUBLISH_KIND,
                requested_by_user_id=actor_user_id,
                eligible_approvers=[username],
                designated_approver_user_id=designated_approver_user_id,
            )
            return
        # Toggle off → only a curator may publish straight to global.
        if not await user_has_permission(session, actor_user_id, Permission.LIBRARY_CURATE):
            raise GlobalPublishNotPermittedError
        version.publish_state = "published"

    async def _get_visible_file(
        self, session: AsyncSession, file_id: int, user_id: int
    ) -> LibraryFile:
        lib = await session.get(LibraryFile, file_id)
        if lib is None:
            raise LibraryFileNotFoundError(file_id)
        is_curator = await user_has_permission(session, user_id, Permission.LIBRARY_CURATE)
        if not await self._visible(session, lib, user_id, is_curator, {}):
            raise LibraryFileNotFoundError(file_id)
        return lib

    async def _visible(
        self,
        session: AsyncSession,
        lib: LibraryFile,
        user_id: int,
        is_curator: bool,
        project_access: dict[int, bool],
    ) -> bool:
        """Can ``user_id`` see ``lib``? (scope + state, ADR-0006 §3).

        - The **owner** always sees their own file (any scope/state).
        - A **curator** sees any *non-personal* file — including a
          pending global file awaiting review and a retired
          project/global file — but never another user's personal file.
        - Everyone else sees only ``active`` files: ``global`` to all,
          ``project`` to ADR-0002 Viewer+, ``personal`` to no-one.
        """
        if lib.owner_user_id == user_id:
            return True
        if lib.scope != "personal" and is_curator:
            return True
        if lib.state != "active":
            return False
        if lib.scope == "personal":
            return False
        if lib.scope == "global":
            return True
        # project scope, active: ADR-0002 Viewer+ on the project.
        return await self._project_visible(session, lib.project_id, user_id, project_access)

    async def _project_visible(
        self,
        session: AsyncSession,
        project_id: int | None,
        user_id: int,
        cache: dict[int, bool],
    ) -> bool:
        """Viewer+ access on ``project_id`` (cached per call), or ``False``."""
        if project_id is None:
            return False
        if project_id not in cache:
            project = await session.get(Project, project_id)
            if project is None:
                cache[project_id] = False
            else:
                level = await _resolve_access(session, project, user_id)
                cache[project_id] = level != ProjectAccessLevel.NONE
        return cache[project_id]

    async def _require_mutate_access(
        self, session: AsyncSession, lib: LibraryFile, user_id: int
    ) -> None:
        """Assert the caller may mutate ``lib`` given its current scope.

        ``personal`` → the owner only; ``project`` → EDIT (Member+) on
        the project; ``global`` → the steward (owner) or any curator
        (the *publish* itself still goes through the §5 gate).
        """
        if lib.scope == "personal":
            if lib.owner_user_id != user_id:
                raise LibraryAccessDeniedError
            return
        if lib.scope == "project":
            assert lib.project_id is not None  # noqa: S101 — project scope ⇒ project_id
            await self._require_project_edit(session, lib.project_id, user_id)
            return
        # global: the steward (owner) or any curator may manage it.
        if lib.owner_user_id == user_id:
            return
        if not await user_has_permission(session, user_id, Permission.LIBRARY_CURATE):
            raise LibraryAccessDeniedError

    async def _require_create(self, session: AsyncSession, actor_user_id: int) -> None:
        """Gate adding/versioning files on ``library.create`` (or the curate capability).

        Contributing files is a base-tier action: a ``library.create``
        holder may add and version files, as may a ``library.curate``
        curator (who can always contribute). Assigning to tools, retiring,
        and global-publish approval stay curate-only.
        """
        if not await user_has_any_permission(
            session, actor_user_id, Permission.LIBRARY_CREATE, Permission.LIBRARY_CURATE
        ):
            raise LibraryAccessDeniedError

    async def _require_project_edit(
        self, session: AsyncSession, project_id: int, user_id: int
    ) -> None:
        try:
            await _require_project_access(
                session,
                project_id=project_id,
                actor_user_id=user_id,
                required=ProjectAccessLevel.EDIT,
            )
        except (NotFoundError, AccessDeniedError) as exc:
            raise LibraryAccessDeniedError from exc

    @staticmethod
    def _check_optimistic_lock(lib: LibraryFile, expected_version: int | None) -> None:
        if expected_version is not None and lib.version != expected_version:
            raise LibraryConcurrentModificationError

    @staticmethod
    async def _flush_optimistic(session: AsyncSession) -> None:
        try:
            await session.flush()
        except StaleDataError as exc:
            raise LibraryConcurrentModificationError from exc

    async def _audit(
        self,
        session: AsyncSession,
        actor_user_id: int,
        action: str,
        lib: LibraryFile,
        *,
        payload: dict[str, object],
        reason: str | None = None,
    ) -> None:
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=action,
                resource_type="library_file",
                resource_id=str(lib.id),
                outcome="success",
                payload=payload,
                reason=reason,
            ),
        )


def make_library_service(audit: AuditWriter, *, environment: str | None = None) -> LibraryService:
    """Wire up a :class:`LibraryService` from the ambient audit writer.

    ``environment`` is threaded into the on-disk path segment for stored
    bytes (API callers pass ``settings.environment``).
    """
    return LibraryService(audit=audit, environment=environment)
