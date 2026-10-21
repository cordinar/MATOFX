"""WorkflowService — CRUD + state machine + archive cascade.

Construct one per process (cheap dataclass, stateless beyond the
audit-writer reference). State-changing methods record audit rows
through the supplied :class:`AuditWriter`.

Locked decisions (see ``project_workflows_design.md`` memory):

- Tool versions float — a workflow stores ``tool_alias`` only.
- States mirror tools: ``new`` / ``test`` / ``released``.
- Archive cascade: when a tool is archived, every workflow it's a
  member of also archives. The cascade hook lives here as
  :func:`cascade_archive_for_tool`; ``ToolService.archive`` calls
  into it via lazy import to avoid a module-level cycle.
- Versioning: every install / edit / archive snapshots into
  ``workflow_version``.
- A workflow needs ≥1 member tool; member tools may not appear twice.
- Tools added to a workflow must not be archived at save time.
- ``new``-state member tools are allowed — the visibility cascade
  (workflow with a ``new`` member is admin/su only) is enforced at
  list time, not at save time.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from matofx.audit.schema import RecordInput
from matofx.tools.orm import Tool
from matofx.tools.states import DEFAULT_TOOL_STATE, TOOL_STATES, user_can_see_state
from matofx.tools.versioning import (
    FIRST_RELEASE_VERSION,
    INITIAL_VERSION,
    bump_major,
    bump_minor,
    bump_patch,
    first_release_snap,
)
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission
from matofx.workflows._custom_helpers import (
    load_tool_descriptors,
    synthesise_custom_alias,
    user_can_see_tool,
)
from matofx.workflows.errors import (
    ArchivedToolMemberError,
    DuplicateMemberError,
    DuplicateWorkflowError,
    EmptyWorkflowError,
    InvalidShareRecipientError,
    NotCustomWorkflowError,
    NotOwnerError,
    TooFewMembersError,
    ToolNotVisibleToRecipientError,
    TooManyMembersError,
    UnknownToolMemberError,
    UpstreamOrderingError,
    WorkflowArchivedError,
    WorkflowError,
    WorkflowNotFoundError,
    WorkflowStateError,
)
from matofx.workflows.ordering import compute_order
from matofx.workflows.orm import (
    CUSTOM_WORKFLOW_STATE,
    Workflow,
    WorkflowKind,
    WorkflowMember,
    WorkflowShare,
    WorkflowVersion,
    WorkflowVisibility,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


@dataclass(frozen=True)
class WorkflowDraft:
    """Caller-supplied workflow blueprint.

    Passed to :meth:`WorkflowService.install` and :meth:`update`. The
    ``descriptor`` JSON carries ``description`` + ``teams`` (and any
    future workflow-level metadata). The ordered member list ends up
    in dedicated rows on save.
    """

    alias: str
    name: str
    description: str
    teams: list[str]
    tool_aliases: list[str]


@dataclass(frozen=True)
class CustomWorkflowDraft:
    """Caller-supplied blueprint for a user-owned custom workflow.

    Three differences from :class:`WorkflowDraft`:

    - No ``alias`` field — the service synthesises a globally-unique
      one from the owner id and name (``custom_<owner_id>_<slug>``).
    - No ``teams`` field — custom workflows use the
      ``visibility`` + share-list model instead of team gating.
    - ``share_with_user_ids`` carries the share recipients. An empty
      list means private; non-empty means shared. The service
      validates that every recipient can see every member tool.

    The ``tool_aliases`` list is the user's *selection*, in any
    order. The service runs the ordering algorithm at save time to
    produce the actual run order.
    """

    name: str
    description: str
    tool_aliases: list[str]
    share_with_user_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class WorkflowService:
    """Workflow CRUD + state-machine."""

    audit: AuditWriter

    # ------------------------------------------------------------------
    # Privilege gates
    # ------------------------------------------------------------------

    async def _require_manage(self, session: AsyncSession, actor_user_id: int) -> None:
        if not await user_has_permission(session, actor_user_id, Permission.WORKFLOWS_MANAGE):
            raise WorkflowStateError(
                "You don't have permission to manage workflows "
                f"({Permission.WORKFLOWS_MANAGE.value})."
            )

    async def _require_custom_create(self, session: AsyncSession, actor_user_id: int) -> None:
        """Gate the custom-workflow creation path.

        Separate from :meth:`_require_manage` because the two
        permissions are independently grantable per role-design
        decisions — an admin without ``custom_workflows.create``
        wouldn't be able to create one of their own (rare but
        possible), and a regular user without ``workflows.manage``
        absolutely can create custom workflows.
        """
        if not await user_has_permission(
            session, actor_user_id, Permission.CUSTOM_WORKFLOWS_CREATE
        ):
            raise WorkflowStateError(
                "You don't have permission to create custom workflows "
                f"({Permission.CUSTOM_WORKFLOWS_CREATE.value})."
            )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_all(
        self,
        session: AsyncSession,
        *,
        include_archived: bool = True,
    ) -> list[Workflow]:
        """Admin view: every workflow (archived included by default)."""
        stmt = select(Workflow).order_by(Workflow.alias)
        if not include_archived:
            stmt = stmt.where(Workflow.archived_at.is_(None))
        return list((await session.execute(stmt)).scalars())

    async def list_visible(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
    ) -> list[Workflow]:
        """User view: workflows the caller may launch from the home surfaces.

        Backs the Workflow Quick Access card and the Add-Job(s) chooser.
        Two groups are merged:

        * **Admin** workflows gated by :func:`can_user_see_workflow`:
          archived rows are hidden; the row passes the same state + team
          gates as a tool **plus the member cascade** — every member tool
          must itself be visible to the caller, so a workflow can never
          expose a ``new``/``test`` or team-restricted tool the caller
          couldn't see directly.
        * The caller's **own** custom workflows — those they own or that
          are shared with them, exactly the set on ``/my-workflows``
          (:meth:`list_for_user`). Custom rows are ``released`` +
          un-teamed, so they must never fall through the admin state/team
          gate — doing so historically leaked every custom workflow to
          every user. Scoping them by ownership keeps each caller to their
          own. The broader ``custom_workflows.inspect`` grant is
          deliberately *not* applied here: an inspector's Quick Access
          shouldn't fill with every user's custom workflow — that's what
          the admin inspector page is for.
        """
        visible: list[Workflow] = []
        for wf in await self.list_all(session, include_archived=False):
            if wf.kind != WorkflowKind.ADMIN.value:
                continue  # custom rows handled by the ownership-scoped merge below
            if await can_user_see_workflow(session, actor_user_id=actor_user_id, wf=wf):
                visible.append(wf)
        # Merge in the caller's own custom workflows (owned + shared),
        # the same scope as the /my-workflows page — never the inspect-all set.
        visible.extend(await self.list_for_user(session, actor_user_id=actor_user_id))
        return visible

    async def get_by_alias(
        self,
        session: AsyncSession,
        alias: str,
    ) -> Workflow:
        """Direct fetch; raises :class:`WorkflowNotFoundError` when missing."""
        wf = await _fetch_by_alias(session, alias)
        if wf is None:
            raise WorkflowNotFoundError(f"No workflow called {alias!r}.")
        return wf

    async def list_members(
        self,
        session: AsyncSession,
        workflow_id: int,
    ) -> list[WorkflowMember]:
        """Return the workflow's members in display order (position ASC)."""
        stmt = (
            select(WorkflowMember)
            .where(WorkflowMember.workflow_id == workflow_id)
            .order_by(WorkflowMember.position)
        )
        return list((await session.execute(stmt)).scalars())

    async def list_versions(
        self,
        session: AsyncSession,
        alias: str,
    ) -> list[WorkflowVersion]:
        """Snapshot history for the workflow, newest first."""
        stmt = (
            select(WorkflowVersion)
            .where(WorkflowVersion.alias == alias)
            .order_by(WorkflowVersion.created_at.desc())
        )
        return list((await session.execute(stmt)).scalars())

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def install(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        draft: WorkflowDraft,
    ) -> Workflow:
        """Install a fresh workflow at version ``0.1.0`` in the ``new`` state."""
        await self._require_manage(session, actor_user_id)
        await _validate_members(session, draft.tool_aliases)
        _ensure_unique_members(draft.tool_aliases)
        _ensure_non_empty(draft.tool_aliases)

        descriptor = _build_descriptor(draft)
        now = _utcnow()
        wf = Workflow(
            alias=draft.alias.strip(),
            name=draft.name.strip(),
            version=INITIAL_VERSION,
            state=DEFAULT_TOOL_STATE,
            descriptor=descriptor,
            installed_by_user_id=actor_user_id,
            created_at=now,
            updated_at=now,
        )
        session.add(wf)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateWorkflowError(draft.alias) from exc

        _populate_members(session, wf.id, draft.tool_aliases)
        await session.flush()

        await _snapshot(
            session,
            wf=wf,
            members=draft.tool_aliases,
            actor_user_id=actor_user_id,
            change_summary=f"Initial install — {len(draft.tool_aliases)} tool(s)",
            changes={},
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.workflow.install",
                resource_type="workflow",
                resource_id=str(wf.id),
                outcome="success",
                payload={
                    "alias": wf.alias,
                    "version": wf.version,
                    "tool_aliases": list(draft.tool_aliases),
                },
            ),
        )
        return wf

    async def update(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
        draft: WorkflowDraft,
    ) -> Workflow:
        """Apply an edit; bumps the patch version and snapshots history.

        Membership changes (added / removed / reordered tools) bump
        the minor version instead. Mirrors tool patch-vs-minor:

        - Patch: metadata-only (name, description, teams).
        - Minor: member list changed or reordered.

        Major bumps are not used by the admin form — they fire when
        the runtime engine introduces a breaking schema, which is a
        manual intervention (Phase 2).
        """
        await self._require_manage(session, actor_user_id)
        wf = await self.get_by_alias(session, alias)
        if wf.archived_at is not None:
            raise WorkflowArchivedError(alias)

        await _validate_members(session, draft.tool_aliases)
        _ensure_unique_members(draft.tool_aliases)
        _ensure_non_empty(draft.tool_aliases)

        before_members = [m.tool_alias for m in await self.list_members(session, wf.id)]
        before_descriptor = dict(wf.descriptor) if isinstance(wf.descriptor, dict) else {}

        membership_changed = before_members != list(draft.tool_aliases)
        descriptor = _build_descriptor(draft)
        # Preserve display assets (icon) that aren't part of the edit form
        # so a metadata save doesn't wipe an uploaded icon.
        if before_descriptor.get("icon_filename"):
            descriptor["icon_filename"] = before_descriptor["icon_filename"]
        wf.name = draft.name.strip()
        wf.descriptor = descriptor
        if wf.teams_pending and descriptor.get("teams"):
            wf.teams_pending = False  # an admin has now chosen its teams
        wf.version = bump_minor(wf.version) if membership_changed else bump_patch(wf.version)
        wf.updated_at = _utcnow()

        if membership_changed:
            await session.execute(
                sa_delete(WorkflowMember).where(WorkflowMember.workflow_id == wf.id)
            )
            await session.flush()
            _populate_members(session, wf.id, draft.tool_aliases)

        await session.flush()

        changes = _diff_workflow(before_descriptor, descriptor, before_members, draft.tool_aliases)
        await _snapshot(
            session,
            wf=wf,
            members=draft.tool_aliases,
            actor_user_id=actor_user_id,
            change_summary=_summarise_changes(changes, membership_changed),
            changes=changes,
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.workflow.update",
                resource_type="workflow",
                resource_id=str(wf.id),
                outcome="success",
                payload={
                    "alias": wf.alias,
                    "new_version": wf.version,
                    "membership_changed": membership_changed,
                },
            ),
        )
        return wf

    async def promote_state(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
        new_state: str,
    ) -> Workflow:
        """Move the workflow into a new lifecycle state.

        Same vocabulary as tools (``new``/``test``/``released``).
        Forward and backward moves are both allowed for anyone holding
        ``workflows.manage`` — they own the publication ramp.
        """
        await self._require_manage(session, actor_user_id)
        if new_state not in TOOL_STATES:
            raise WorkflowStateError(
                f"{new_state!r} isn't a valid workflow state; expected one of {list(TOOL_STATES)}."
            )
        wf = await self.get_by_alias(session, alias)
        if wf.archived_at is not None:
            raise WorkflowArchivedError(alias)
        if wf.state == new_state:
            return wf
        if wf.teams_pending and wf.state == "new":
            # Imported with no resolvable teams (ADR-0023) — see Tool.teams_pending.
            raise WorkflowStateError(
                f"The workflow {alias!r} was imported without teams (none of its teams "
                "exist on this install). Set at least one team on its edit page first — "
                "choose World to make it available to everyone."
            )

        previous = wf.state
        # Promotions to ``released`` bump the version so each release is
        # distinguishable from the previous one:
        #
        #   - First promotion from a ``0.x.y`` row snaps to ``1.0.0``.
        #   - Subsequent promotions to released (after a rollback) major-
        #     bump (``X.Y.Z → X+1.0.0``) — a re-release is a new edition
        #     of the released artifact.
        #   - Rollbacks (released → test, test → new) and no-op transitions
        #     don't touch the version.
        #
        # Mirrors :meth:`matofx.tools.service.ToolService.promote_state` so
        # the two stay symmetric for the operator.
        old_version = str(wf.version)
        new_version: str | None = None
        snap_summary: str | None = None
        if new_state == "released" and previous != "released":
            first_snap = first_release_snap(old_version)
            if first_snap is not None:
                new_version = first_snap
                snap_summary = (
                    f"Promoted to Released — version snapped from "
                    f"{old_version} to {FIRST_RELEASE_VERSION}"
                )
            else:
                new_version = bump_major(old_version)
                snap_summary = (
                    f"Re-promoted to Released — major-bumped from {old_version} to {new_version}"
                )

        wf.state = new_state
        if new_version is not None:
            wf.version = new_version
        wf.updated_at = _utcnow()
        await session.flush()

        if new_version is not None:
            # Snapshot the bump so the change-log shows it.
            members = await self.list_members(session, wf.id)
            await _snapshot(
                session,
                wf=wf,
                members=[m.tool_alias for m in members],
                actor_user_id=actor_user_id,
                change_summary=snap_summary or "",
                changes={"version": {"old": old_version, "new": new_version}},
            )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.workflow.promote_state",
                resource_type="workflow",
                resource_id=str(wf.id),
                outcome="success",
                payload={
                    "alias": wf.alias,
                    "from": previous,
                    "to": new_state,
                    "version_snap": new_version,
                },
            ),
        )
        return wf

    async def update_icon(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
        icons_dir: Path,
        icon_bytes: bytes,
    ) -> Workflow:
        """Replace the workflow's icon PNG.

        Workflows don't ship a per-workflow bundle directory like tools
        do, so the icon lives under the shared ``workflows.icons_dir``
        configured in ``matofx.toml``. The PNG is written in-place at
        ``<icons_dir>/<alias>.png``, the descriptor's ``icon_filename``
        is set to ``"<alias>.png"`` if it wasn't already, and the
        workflow is patch-bumped + snapshotted so the change-log
        records who did it and when.

        Refuses on archived workflows, same as every other mutating
        call.
        """
        await self._require_manage(session, actor_user_id)
        wf = await self.get_by_alias(session, alias)
        if wf.archived_at is not None:
            raise WorkflowArchivedError(alias)

        target_dir = Path(icons_dir).resolve()
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkflowError(
                f"Couldn't create workflow icons directory {target_dir}: {exc}"
            ) from exc

        icon_filename = f"{wf.alias}.png"
        try:
            (target_dir / icon_filename).write_bytes(icon_bytes)
        except OSError as exc:
            raise WorkflowError(f"Writing the workflow icon to {target_dir} failed: {exc}") from exc

        before_descriptor = dict(wf.descriptor) if isinstance(wf.descriptor, dict) else {}
        new_descriptor = dict(before_descriptor)
        new_descriptor["icon_filename"] = icon_filename
        old_version = str(wf.version)
        new_version = bump_patch(old_version)
        wf.descriptor = new_descriptor
        wf.version = new_version
        wf.updated_at = _utcnow()
        await session.flush()

        members = [m.tool_alias for m in await self.list_members(session, wf.id)]
        await _snapshot(
            session,
            wf=wf,
            members=members,
            actor_user_id=actor_user_id,
            change_summary=f"Icon update — patch bump {old_version} → {new_version}",
            changes={
                "icon_filename": {
                    "old": before_descriptor.get("icon_filename"),
                    "new": icon_filename,
                }
            },
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.workflow.update_icon",
                resource_type="workflow",
                resource_id=str(wf.id),
                outcome="success",
                payload={
                    "alias": wf.alias,
                    "previous_version": old_version,
                    "new_version": new_version,
                    "icon_filename": icon_filename,
                    "icon_bytes": len(icon_bytes),
                },
            ),
        )
        return wf

    async def archive(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
        reason: str | None = None,
    ) -> Workflow:
        """Archive a workflow (soft-shelve; never deleted).

        Existing scenario runs that reference this workflow stay
        resolvable — only the listings hide it.
        """
        await self._require_manage(session, actor_user_id)
        wf = await self.get_by_alias(session, alias)
        if wf.archived_at is not None:
            return wf
        wf.archived_at = _utcnow()
        wf.archived_by_user_id = actor_user_id
        wf.archive_reason = reason
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.workflow.archive",
                resource_type="workflow",
                resource_id=str(wf.id),
                outcome="success",
                payload={"alias": wf.alias, "reason": reason},
            ),
        )
        return wf

    async def unarchive(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        alias: str,
    ) -> Workflow:
        """Bring an archived workflow back to active status."""
        await self._require_manage(session, actor_user_id)
        wf = await self.get_by_alias(session, alias)
        if wf.archived_at is None:
            return wf
        wf.archived_at = None
        wf.archived_by_user_id = None
        wf.archive_reason = None
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.workflow.unarchive",
                resource_type="workflow",
                resource_id=str(wf.id),
                outcome="success",
                payload={"alias": wf.alias},
            ),
        )
        return wf

    # ------------------------------------------------------------------
    # Custom workflows (user-owned)
    # ------------------------------------------------------------------

    async def create_custom(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        draft: CustomWorkflowDraft,
    ) -> Workflow:
        """Create a new user-owned custom workflow.

        Validates that the caller has ``custom_workflows.create``,
        that the selection holds 2–10 unique tools, that the caller
        can see each one (state + team gate), and that every share
        recipient can see every member tool. Computes the run order
        from descriptors via :func:`compute_order`, synthesises a
        globally-unique alias, and writes:

        1. The :class:`Workflow` row (``kind='custom'``, ``state='released'``,
           ``visibility='private'`` when no recipients,
           ``'shared'`` otherwise, ``owner_id=actor_user_id``).
        2. One :class:`WorkflowMember` row per tool in computed order.
        3. One :class:`WorkflowShare` row per recipient.
        4. A :class:`WorkflowVersion` snapshot.
        5. An audit event.

        The workflow's :attr:`installed_by_user_id` is the caller (the
        original creator); :attr:`owner_id` tracks the *current* owner
        which today is always equal to the creator but may diverge in
        a future ownership-transfer feature.
        """
        await self._require_custom_create(session, actor_user_id)

        # 1. Member-count + dedup.
        _ensure_custom_member_count(draft.tool_aliases)
        _ensure_unique_members(draft.tool_aliases)

        # 2. Tools exist + not archived (no order check — we compute it).
        tool_rows = await _load_member_tools(session, draft.tool_aliases)
        _assert_members_exist_and_not_archived(draft.tool_aliases, tool_rows)

        # 3. Owner can see each member tool.
        await _assert_member_tools_visible(session, user_id=actor_user_id, tool_rows=tool_rows)

        # 4. Compute the run order from descriptors.
        descriptors = await load_tool_descriptors(session, draft.tool_aliases)
        ordered_aliases = compute_order(draft.tool_aliases, descriptors)

        # 5. Validate share recipients (if any) against each member tool.
        recipients_by_id = await _validate_share_recipients(
            session,
            owner_id=actor_user_id,
            recipient_ids=draft.share_with_user_ids,
        )
        for recipient_id, recipient in recipients_by_id.items():
            await _assert_recipient_sees_member_tools(
                session,
                recipient_id=recipient_id,
                recipient_username=recipient.username,
                tool_rows=tool_rows,
            )

        # 6. Synthesise alias + insert row.
        alias = await synthesise_custom_alias(session, name=draft.name, owner_id=actor_user_id)
        visibility = WorkflowVisibility.SHARED if recipients_by_id else WorkflowVisibility.PRIVATE
        now = _utcnow()
        # Custom workflows live in 'released' from day one — they don't
        # ramp through new/test the way admin workflows do, so the 0.x.y
        # "pre-release" version prefix would be meaningless. Start at
        # 1.0.0 (FIRST_RELEASE_VERSION); subsequent edits bump patch
        # (metadata) or minor (members) from there.
        wf = Workflow(
            alias=alias,
            name=draft.name.strip(),
            version=FIRST_RELEASE_VERSION,
            kind=WorkflowKind.CUSTOM.value,
            state=CUSTOM_WORKFLOW_STATE,
            descriptor=_build_custom_descriptor(draft),
            installed_by_user_id=actor_user_id,
            owner_id=actor_user_id,
            visibility=visibility.value,
            created_at=now,
            updated_at=now,
        )
        session.add(wf)
        try:
            await session.flush()
        except IntegrityError as exc:
            # The synthesised alias is collision-checked but a parallel
            # creator could in theory race us. Surface as a duplicate
            # so the UI can retry rather than a generic DB error.
            await session.rollback()
            raise DuplicateWorkflowError(alias) from exc

        _populate_members(session, wf.id, ordered_aliases)
        _populate_shares(
            session,
            workflow_id=wf.id,
            owner_id=actor_user_id,
            recipient_ids=list(recipients_by_id),
        )
        await session.flush()

        await _snapshot(
            session,
            wf=wf,
            members=ordered_aliases,
            actor_user_id=actor_user_id,
            change_summary=(
                f"Custom workflow created — {len(ordered_aliases)} tool(s)"
                + (f", shared with {len(recipients_by_id)} user(s)" if recipients_by_id else "")
            ),
            changes={},
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.custom.create",
                resource_type="workflow",
                resource_id=str(wf.id),
                outcome="success",
                payload={
                    "alias": wf.alias,
                    "name": wf.name,
                    "visibility": wf.visibility,
                    "tool_aliases": list(ordered_aliases),
                    "share_with_user_ids": sorted(recipients_by_id),
                },
            ),
        )
        return wf

    async def update_custom(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        workflow_id: int,
        draft: CustomWorkflowDraft,
    ) -> Workflow:
        """Edit a user-owned custom workflow.

        Owner-only. The caller hands over a fresh
        :class:`CustomWorkflowDraft` carrying the desired final state:
        name, description, tool list (re-ordered automatically), and
        share recipient set. The service computes a diff against the
        current row + members + share list, applies the changes,
        snapshots the result, and audits.

        Bumps the **minor** version when membership changes (mirrors
        admin workflows), **patch** otherwise. Refuses on archived
        rows; an owner who wants to revive an archived workflow has
        to unarchive it explicitly first (Phase β).
        """
        wf = await _fetch_by_id(session, workflow_id)
        if wf is None:
            raise WorkflowNotFoundError(f"No workflow with id={workflow_id}.")
        _require_custom_kind(wf)
        if wf.archived_at is not None:
            raise WorkflowArchivedError(wf.alias)
        _require_owner(wf, actor_user_id)

        # Re-validate the proposed member list end-to-end.
        _ensure_custom_member_count(draft.tool_aliases)
        _ensure_unique_members(draft.tool_aliases)
        tool_rows = await _load_member_tools(session, draft.tool_aliases)
        _assert_members_exist_and_not_archived(draft.tool_aliases, tool_rows)
        await _assert_member_tools_visible(session, user_id=actor_user_id, tool_rows=tool_rows)

        descriptors = await load_tool_descriptors(session, draft.tool_aliases)
        ordered_aliases = compute_order(draft.tool_aliases, descriptors)

        # Resolve recipients + their tool visibility against the new
        # member set. (A tool the workflow previously didn't include
        # might now be off-limits to a current recipient.)
        recipients_by_id = await _validate_share_recipients(
            session,
            owner_id=actor_user_id,
            recipient_ids=draft.share_with_user_ids,
        )
        for recipient_id, recipient in recipients_by_id.items():
            await _assert_recipient_sees_member_tools(
                session,
                recipient_id=recipient_id,
                recipient_username=recipient.username,
                tool_rows=tool_rows,
            )

        # Snapshot the "before" state so we can write a diff.
        before_members = [m.tool_alias for m in await self.list_members(session, wf.id)]
        before_share_ids = await _current_share_ids(session, wf.id)
        before_descriptor = dict(wf.descriptor) if isinstance(wf.descriptor, dict) else {}
        before_name = wf.name
        before_visibility = wf.visibility

        membership_changed = before_members != list(ordered_aliases)
        new_descriptor = _build_custom_descriptor(draft)
        new_visibility = (
            WorkflowVisibility.SHARED if recipients_by_id else WorkflowVisibility.PRIVATE
        ).value
        target_share_ids = set(recipients_by_id)

        wf.name = draft.name.strip()
        wf.descriptor = new_descriptor
        wf.visibility = new_visibility
        wf.version = bump_minor(wf.version) if membership_changed else bump_patch(wf.version)
        wf.updated_at = _utcnow()

        if membership_changed:
            await session.execute(
                sa_delete(WorkflowMember).where(WorkflowMember.workflow_id == wf.id)
            )
            await session.flush()
            _populate_members(session, wf.id, ordered_aliases)

        # Reconcile share list against target set.
        to_remove = before_share_ids - target_share_ids
        to_add = target_share_ids - before_share_ids
        if to_remove:
            await session.execute(
                sa_delete(WorkflowShare).where(
                    WorkflowShare.workflow_id == wf.id,
                    WorkflowShare.user_id.in_(list(to_remove)),
                )
            )
        _populate_shares(
            session,
            workflow_id=wf.id,
            owner_id=actor_user_id,
            recipient_ids=sorted(to_add),
        )

        await session.flush()

        # Diff for the snapshot. Includes share-list changes since
        # they are part of the workflow's user-visible state.
        changes = _diff_custom_workflow(
            before_name=before_name,
            after_name=wf.name,
            before_desc=before_descriptor,
            after_desc=new_descriptor,
            before_members=before_members,
            after_members=ordered_aliases,
            before_visibility=before_visibility or "",
            after_visibility=new_visibility,
            before_share_ids=before_share_ids,
            after_share_ids=target_share_ids,
        )
        await _snapshot(
            session,
            wf=wf,
            members=ordered_aliases,
            actor_user_id=actor_user_id,
            change_summary=_summarise_custom_changes(changes, membership_changed),
            changes=changes,
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.custom.update",
                resource_type="workflow",
                resource_id=str(wf.id),
                outcome="success",
                payload={
                    "alias": wf.alias,
                    "new_version": wf.version,
                    "membership_changed": membership_changed,
                    "visibility": new_visibility,
                    "share_added": sorted(to_add),
                    "share_removed": sorted(to_remove),
                },
            ),
        )
        return wf

    async def archive_custom(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        workflow_id: int,
        reason: str | None = None,
    ) -> Workflow:
        """Archive a custom workflow.

        Two callers, two rules:

        - **Owner** archives their own workflow — ``reason`` optional;
          if omitted we record ``"Archived by owner"``.
        - **Admin** holding ``custom_workflows.inspect`` archives
          someone else's workflow — ``reason`` is **required**. The
          inspector page enforces this UI-side; we re-check here so
          the API can't bypass it.

        Idempotent: archiving an already-archived row is a no-op.
        """
        wf = await _fetch_by_id(session, workflow_id)
        if wf is None:
            raise WorkflowNotFoundError(f"No workflow with id={workflow_id}.")
        _require_custom_kind(wf)

        is_owner = wf.owner_id == actor_user_id
        is_inspector = await user_has_permission(
            session, actor_user_id, Permission.CUSTOM_WORKFLOWS_INSPECT
        )
        if not is_owner and not is_inspector:
            raise NotOwnerError(wf.alias)
        if not is_owner and not (reason and reason.strip()):
            raise WorkflowError(
                "Admin-driven archive of a custom workflow needs a "
                "reason. Type a short note explaining why so the owner "
                "can find it in their archived list."
            )

        if wf.archived_at is not None:
            return wf

        wf.archived_at = _utcnow()
        wf.archived_by_user_id = actor_user_id
        effective_reason = reason.strip() if (reason and reason.strip()) else "Archived by owner"
        wf.archive_reason = effective_reason
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=(
                    "workflows.custom.archive"
                    if is_owner
                    else "workflows.custom.archive.by_inspector"
                ),
                resource_type="workflow",
                resource_id=str(wf.id),
                outcome="success",
                payload={
                    "alias": wf.alias,
                    "owner_id": wf.owner_id,
                    "reason": effective_reason,
                    "by_inspector": not is_owner,
                },
            ),
        )
        return wf

    async def copy_to_owner(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        source_workflow_id: int,
    ) -> Workflow:
        """Fork a workflow into a new caller-owned custom workflow.

        Visible-source rule: the caller must (a) be able to see an
        admin workflow under the regular state+team gate, OR (b) own
        the source custom workflow, OR (c) appear in its share list,
        OR (d) hold ``custom_workflows.inspect``. Anything else is
        :class:`WorkflowNotFoundError` so we don't leak the existence
        of restricted rows.

        The new row:

        - ``kind='custom'``, ``state='released'``, ``visibility='private'``.
        - ``owner_id = caller``. ``installed_by_user_id = caller`` (the
          fork's "creator").
        - Member rows copied with positions preserved.
        - Share list **not** copied — the new owner starts private.
        - ``descriptor.copied_from_workflow_id`` carries the source row
          id for traceability; copy is otherwise independent of source.

        Caller must hold :data:`Permission.CUSTOM_WORKFLOWS_CREATE`
        (same gate as fresh creation — a fork is just a pre-filled
        create).
        """
        await self._require_custom_create(session, actor_user_id)
        source = await _fetch_by_id(session, source_workflow_id)
        if source is None or not await _can_user_see_workflow_for_copy(
            session, user_id=actor_user_id, wf=source
        ):
            raise WorkflowNotFoundError(
                f"No workflow with id={source_workflow_id} that you can copy."
            )

        before_members = [m.tool_alias for m in await self.list_members(session, source.id)]
        # We don't re-validate the member tool list against the new
        # owner's visibility — the source must already have been
        # legal for somebody, and the new owner is *making a copy*
        # to edit. If a member tool is invisible to them they'll
        # see the error on their first update_custom edit. This is
        # intentional: an inspector can copy a broken workflow into
        # their own list to reproduce a bug without it bouncing.
        descriptors = await load_tool_descriptors(session, before_members)
        ordered_aliases = compute_order(before_members, descriptors)

        # New name: "<original> (copy)" with " (copy 2)" bump on collision.
        new_name = await _pick_copy_name(session, source_name=source.name, owner_id=actor_user_id)
        alias = await synthesise_custom_alias(session, name=new_name, owner_id=actor_user_id)
        descriptor = {
            "description": (
                source.descriptor.get("description") if isinstance(source.descriptor, dict) else ""
            )
            or "",
            "copied_from_workflow_id": source.id,
        }
        now = _utcnow()
        # Forks start at 1.0.0 too — they're a fresh independent
        # workflow, not a continuation of the source's history.
        copy = Workflow(
            alias=alias,
            name=new_name,
            version=FIRST_RELEASE_VERSION,
            kind=WorkflowKind.CUSTOM.value,
            state=CUSTOM_WORKFLOW_STATE,
            descriptor=descriptor,
            installed_by_user_id=actor_user_id,
            owner_id=actor_user_id,
            visibility=WorkflowVisibility.PRIVATE.value,
            created_at=now,
            updated_at=now,
        )
        session.add(copy)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateWorkflowError(alias) from exc

        _populate_members(session, copy.id, ordered_aliases)
        await session.flush()

        await _snapshot(
            session,
            wf=copy,
            members=ordered_aliases,
            actor_user_id=actor_user_id,
            change_summary=f"Forked from workflow id={source.id} ({source.alias!r})",
            changes={},
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.custom.copy",
                resource_type="workflow",
                resource_id=str(copy.id),
                outcome="success",
                payload={
                    "alias": copy.alias,
                    "source_workflow_id": source.id,
                    "source_alias": source.alias,
                    "source_kind": source.kind,
                },
            ),
        )
        return copy

    async def list_for_user(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        include_archived: bool = False,
    ) -> list[Workflow]:
        """Custom workflows visible to the user (owned + shared-with).

        Returned rows are union of:

        - ``kind='custom'`` workflows where ``owner_id = actor_user_id``.
        - ``kind='custom'`` workflows where there's a
          :class:`WorkflowShare` row for the caller.

        Excludes admin workflows entirely — call :meth:`list_visible`
        for those. Excludes archived rows by default; set
        ``include_archived=True`` to include them (the
        ``/my-workflows`` page uses this for the archived tab).
        """
        owned_stmt = select(Workflow).where(
            Workflow.kind == WorkflowKind.CUSTOM.value,
            Workflow.owner_id == actor_user_id,
        )
        shared_stmt = (
            select(Workflow)
            .join(WorkflowShare, WorkflowShare.workflow_id == Workflow.id)
            .where(
                Workflow.kind == WorkflowKind.CUSTOM.value,
                WorkflowShare.user_id == actor_user_id,
            )
        )
        if not include_archived:
            owned_stmt = owned_stmt.where(Workflow.archived_at.is_(None))
            shared_stmt = shared_stmt.where(Workflow.archived_at.is_(None))

        owned = {wf.id: wf for wf in (await session.execute(owned_stmt)).scalars()}
        for wf in (await session.execute(shared_stmt)).scalars():
            owned.setdefault(wf.id, wf)
        return sorted(owned.values(), key=lambda w: (w.name.lower(), w.id))

    async def unarchive_custom(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        workflow_id: int,
    ) -> Workflow:
        """Bring an owner-archived custom workflow back to active.

        Owner-only. Admins driving an archive-with-reason via the
        inspector don't get an unarchive shortcut on this surface —
        they should ask the owner. Idempotent: unarchiving an
        already-active row is a no-op.
        """
        wf = await _fetch_by_id(session, workflow_id)
        if wf is None:
            raise WorkflowNotFoundError(f"No workflow with id={workflow_id}.")
        _require_custom_kind(wf)
        _require_owner(wf, actor_user_id)

        if wf.archived_at is None:
            return wf

        wf.archived_at = None
        wf.archived_by_user_id = None
        wf.archive_reason = None
        wf.updated_at = _utcnow()
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.custom.unarchive",
                resource_type="workflow",
                resource_id=str(wf.id),
                outcome="success",
                payload={"alias": wf.alias},
            ),
        )
        return wf

    async def list_for_admin(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        include_archived: bool = True,
        owner_id: int | None = None,
        contains_tool_alias: str | None = None,
        search: str | None = None,
    ) -> list[Workflow]:
        """Inspector listing for the ``/admin/custom-workflows`` page.

        Returns every ``kind='custom'`` workflow in the system,
        regardless of owner or share list, gated on
        :data:`Permission.CUSTOM_WORKFLOWS_INSPECT`. Default scope is
        "active + archived" because the inspector's primary job is
        post-hoc troubleshooting, which often involves an archived row.

        Optional filters (all additive — combined with ``AND``):

        - ``owner_id`` — restrict to one owner's workflows.
        - ``contains_tool_alias`` — restrict to workflows whose member
          list contains the supplied tool alias (joins against
          ``workflow_member``).
        - ``search`` — free-text, case-insensitive substring matched
          against the workflow name, the owner (username / full name),
          and member tools (alias / name). A row matches if *any* of
          those contain the term, so the operator can type a person's
          name or a tool name without picking the right dropdown first.
        """
        if not await user_has_permission(
            session, actor_user_id, Permission.CUSTOM_WORKFLOWS_INSPECT
        ):
            raise WorkflowStateError(
                "You don't have permission to inspect custom workflows "
                f"({Permission.CUSTOM_WORKFLOWS_INSPECT.value})."
            )

        stmt = select(Workflow).where(Workflow.kind == WorkflowKind.CUSTOM.value)
        if owner_id is not None:
            stmt = stmt.where(Workflow.owner_id == owner_id)
        if contains_tool_alias is not None:
            # IN, not JOIN + DISTINCT: Postgres cannot DISTINCT over the
            # ``json`` descriptor column.
            stmt = stmt.where(
                Workflow.id.in_(
                    select(WorkflowMember.workflow_id).where(
                        WorkflowMember.tool_alias == contains_tool_alias
                    )
                )
            )
        if search and (term := search.strip()):
            pattern = f"%{term}%"
            # EXISTS/IN sub-selects (not joins) so matching against the
            # owner or a member tool never multiplies the workflow rows.
            owner_match = Workflow.owner_id.in_(
                select(User.id).where(
                    or_(User.username.ilike(pattern), User.full_name.ilike(pattern))
                )
            )
            tool_match = Workflow.id.in_(
                select(WorkflowMember.workflow_id)
                .join(Tool, Tool.alias == WorkflowMember.tool_alias)
                .where(
                    or_(
                        WorkflowMember.tool_alias.ilike(pattern),
                        Tool.name.ilike(pattern),
                    )
                )
            )
            stmt = stmt.where(or_(Workflow.name.ilike(pattern), owner_match, tool_match))
        if not include_archived:
            stmt = stmt.where(Workflow.archived_at.is_(None))
        stmt = stmt.order_by(Workflow.updated_at.desc(), Workflow.id.desc())
        return list((await session.execute(stmt)).scalars())


# ----- archive cascade ----------------------------------------------------


async def cascade_archive_for_tool(
    session: AsyncSession,
    *,
    tool_alias: str,
    actor_user_id: int,
    audit: AuditWriter,
) -> list[str]:
    """Auto-archive every non-archived workflow containing ``tool_alias``.

    Called from :meth:`matofx.tools.service.ToolService.archive` via
    lazy import. Returns the aliases of workflows that were archived
    by this cascade so the caller can include the count in its
    response / log line.
    """
    # IN, not JOIN + DISTINCT: Postgres cannot DISTINCT over the ``json``
    # descriptor column.
    stmt = select(Workflow).where(
        Workflow.id.in_(
            select(WorkflowMember.workflow_id).where(WorkflowMember.tool_alias == tool_alias)
        ),
        Workflow.archived_at.is_(None),
    )
    affected = list((await session.execute(stmt)).scalars())
    archived_aliases: list[str] = []
    now = _utcnow()
    reason = f"Auto-archived: member tool {tool_alias!r} was archived"

    for wf in affected:
        wf.archived_at = now
        wf.archived_by_user_id = actor_user_id
        wf.archive_reason = reason
        archived_aliases.append(wf.alias)
        await audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.workflow.archive.cascade_from_tool_archive",
                resource_type="workflow",
                resource_id=str(wf.id),
                outcome="success",
                payload={
                    "alias": wf.alias,
                    "tool_alias": tool_alias,
                    "reason": reason,
                },
            ),
        )

    if archived_aliases:
        await session.flush()
    return archived_aliases


# ----- internals ----------------------------------------------------------


async def _fetch_by_alias(session: AsyncSession, alias: str) -> Workflow | None:
    stmt = select(Workflow).where(Workflow.alias == alias)
    return (await session.execute(stmt)).scalar_one_or_none()


def _build_descriptor(draft: WorkflowDraft) -> dict[str, Any]:
    """Assemble the JSON descriptor stored on the workflow row."""
    return {
        "description": draft.description.strip(),
        "teams": [t.strip() for t in draft.teams if t and t.strip()],
    }


def _populate_members(
    session: AsyncSession,
    workflow_id: int,
    tool_aliases: list[str],
) -> None:
    """Insert one :class:`WorkflowMember` row per tool, position 1..N."""
    now = _utcnow()
    for index, alias in enumerate(tool_aliases, start=1):
        session.add(
            WorkflowMember(
                workflow_id=workflow_id,
                tool_alias=alias,
                position=index,
                created_at=now,
            )
        )


async def _validate_members(
    session: AsyncSession,
    tool_aliases: list[str],
) -> None:
    """Validate the ordered member list.

    Runs three checks against the loaded :class:`Tool` rows:

    1. **Existence** — every alias resolves to an installed tool.
    2. **Not archived** — archived tools can't be added to a workflow.
    3. **Upstream ordering** — when a member tool's descriptor declares
       an input as ``downstream_input=True, upstream_tool=X`` *and* X
       is also in this workflow, X must appear *earlier* in the order.
       If X isn't in the workflow, the field falls back to free entry
       at run time (per the locked design decision) and ordering is
       irrelevant.
    """
    if not tool_aliases:
        return
    stmt = select(Tool).where(Tool.alias.in_(list(set(tool_aliases))))
    rows = {t.alias: t for t in (await session.execute(stmt)).scalars()}
    for alias in tool_aliases:
        tool = rows.get(alias)
        if tool is None:
            raise UnknownToolMemberError(alias)
        if tool.archived_at is not None:
            raise ArchivedToolMemberError(alias)

    check_upstream_ordering(
        tool_aliases,
        descriptors={alias: rows[alias].descriptor for alias in tool_aliases},
        names={alias: rows[alias].name for alias in tool_aliases},
    )


def check_upstream_ordering(
    tool_aliases: list[str],
    *,
    descriptors: Mapping[str, Any],
    names: Mapping[str, str],
) -> None:
    """Raise :class:`UpstreamOrderingError` if a member precedes its upstream.

    Pure: works from descriptors rather than DB rows, so a cross-install
    import can validate a workflow against tools that are still only in the
    bundle (ADR-0023). When a member's input is ``downstream_input=True,
    upstream_tool=X`` and X is also in ``tool_aliases``, X must come earlier.
    An upstream outside the workflow is fine — the field falls back to entry.
    """
    position_by_alias = {alias: idx for idx, alias in enumerate(tool_aliases)}
    for downstream_alias in tool_aliases:
        downstream_pos = position_by_alias[downstream_alias]
        for input_field in _iter_descriptor_inputs(descriptors.get(downstream_alias)):
            if not bool(input_field.get("downstream_input")):
                continue
            upstream_alias = input_field.get("upstream_tool")
            if not isinstance(upstream_alias, str) or not upstream_alias:
                continue
            if upstream_alias not in position_by_alias:
                continue
            if position_by_alias[upstream_alias] >= downstream_pos:
                raise UpstreamOrderingError(
                    downstream_tool_alias=downstream_alias,
                    downstream_tool_name=names.get(downstream_alias, downstream_alias),
                    upstream_tool_alias=upstream_alias,
                    upstream_tool_name=names.get(upstream_alias, upstream_alias),
                    input_field_name=str(input_field.get("name") or "?"),
                )


def _iter_descriptor_inputs(descriptor: Any) -> list[dict[str, Any]]:
    """Pull the input-field dict list off a tool's descriptor JSON, defensively.

    Tool descriptors are a JSON column; pre-validated by the descriptor
    Pydantic models at install time, but reading them here we still
    treat anything unexpected as "no inputs" rather than blowing up
    the workflow save with a ``KeyError``.
    """
    if not isinstance(descriptor, dict):
        return []
    inputs = descriptor.get("inputs")
    if not isinstance(inputs, list):
        return []
    return [item for item in inputs if isinstance(item, dict)]


def _ensure_unique_members(tool_aliases: list[str]) -> None:
    seen: set[str] = set()
    for alias in tool_aliases:
        if alias in seen:
            raise DuplicateMemberError(alias)
        seen.add(alias)


def _ensure_non_empty(tool_aliases: list[str]) -> None:
    if not tool_aliases:
        raise EmptyWorkflowError


def _workflow_team_names(wf: Workflow) -> list[str]:
    """Pull the descriptor's ``teams`` list off the workflow row defensively."""
    raw = wf.descriptor if isinstance(wf.descriptor, dict) else {}
    teams = raw.get("teams") if isinstance(raw, dict) else None
    if not isinstance(teams, list):
        return []
    return [str(t) for t in teams if isinstance(t, str)]


async def can_user_see_workflow(
    session: AsyncSession,
    *,
    actor_user_id: int,
    wf: Workflow,
) -> bool:
    """Canonical, kind-aware workflow-visibility predicate.

    The single source of truth for "can this user see this workflow?",
    shared by :meth:`WorkflowService.list_visible`, the graph service
    and the run launcher.

    **Admin** workflows must pass *all* of:

    - not archived;
    - the workflow's own state gate (``released`` for everyone, ``test``
      / ``new`` gated by the matching ``tools.see_*`` permission);
    - the workflow's own team gate (``World`` / un-grouped, or a shared
      team);
    - **the member cascade** — every member tool must itself be visible
      to the caller via :func:`can_user_see_tool`. This is what stops a
      workflow exposing a ``new``/``test`` or team-restricted tool the
      caller couldn't see directly.

    **Custom** workflows are scoped by ownership: visible to the owner,
    to explicit share recipients, or to an admin holding
    ``custom_workflows.inspect``. The member cascade deliberately does
    *not* hide a custom workflow from its owner — if the owner loses
    access to a member tool the workflow stays visible but read-only,
    and the per-job submit gate refuses to run the affected tool.
    """
    if wf.archived_at is not None:
        return False
    if wf.kind == WorkflowKind.CUSTOM.value:
        return await _can_see_custom_workflow(session, actor_user_id, wf)
    return await _can_see_admin_workflow(session, actor_user_id, wf)


async def _can_see_custom_workflow(session: AsyncSession, actor_user_id: int, wf: Workflow) -> bool:
    """Custom-workflow scope: owner, share recipient, or admin inspector."""
    if wf.owner_id == actor_user_id:
        return True
    share = (
        await session.execute(
            select(WorkflowShare.user_id).where(
                WorkflowShare.workflow_id == wf.id,
                WorkflowShare.user_id == actor_user_id,
            )
        )
    ).scalar_one_or_none()
    if share is not None:
        return True
    return await user_has_permission(session, actor_user_id, Permission.CUSTOM_WORKFLOWS_INSPECT)


async def _can_see_admin_workflow(session: AsyncSession, actor_user_id: int, wf: Workflow) -> bool:
    """Admin-workflow gates: own state + own team + the member cascade."""
    if not await user_can_see_state(session, actor_user_id, wf.state):
        return False

    # Lazy import keeps the tools.service ↔ workflows.service edge one-way.
    from matofx.tools.service import WORLD_TEAM_NAME, _user_team_names  # noqa: PLC0415

    # Privileged users (admin / super_user) see every tool, so neither the
    # own-team gate nor the member cascade can reject them — short-circuit.
    if await user_has_permission(session, actor_user_id, Permission.TOOLS_SEE_NEW_STATE):
        return True

    # Own team gate.
    teams = _workflow_team_names(wf)
    if teams and WORLD_TEAM_NAME not in teams:
        actor_teams = await _user_team_names(session, actor_user_id)
        if not (set(teams) & actor_teams):
            return False

    # Member cascade: every member tool must be visible to the caller.
    member_tools = (
        await session.execute(
            select(Tool)
            .join(WorkflowMember, WorkflowMember.tool_alias == Tool.alias)
            .where(WorkflowMember.workflow_id == wf.id)
        )
    ).scalars()
    for tool in member_tools:
        if not await user_can_see_tool(session, user_id=actor_user_id, tool=tool):
            return False
    return True


async def _snapshot(
    session: AsyncSession,
    *,
    wf: Workflow,
    members: list[str],
    actor_user_id: int,
    change_summary: str,
    changes: dict[str, Any],
) -> None:
    """Append a row to ``workflow_version``."""
    session.add(
        WorkflowVersion(
            alias=wf.alias,
            version=wf.version,
            descriptor_snapshot=dict(wf.descriptor) if isinstance(wf.descriptor, dict) else {},
            members_snapshot=[{"tool_alias": m, "position": i + 1} for i, m in enumerate(members)],
            created_by_user_id=actor_user_id,
            change_summary=change_summary,
            changes=changes,
        )
    )
    await session.flush()


def _diff_workflow(
    before_desc: dict[str, Any],
    after_desc: dict[str, Any],
    before_members: list[str],
    after_members: list[str],
) -> dict[str, Any]:
    """Per-field old/new diff for the change-log modal."""
    changes: dict[str, Any] = {}
    for key in ("description", "teams"):
        if before_desc.get(key) != after_desc.get(key):
            changes[key] = {"old": before_desc.get(key), "new": after_desc.get(key)}
    if before_members != after_members:
        changes["members"] = {"old": before_members, "new": after_members}
    return changes


def _summarise_changes(changes: dict[str, Any], membership_changed: bool) -> str:
    """One-line headline describing the edit, for the version log."""
    if not changes:
        return "No-op edit"
    parts: list[str] = []
    if membership_changed:
        parts.append("member list changed")
    metadata_keys = sorted(k for k in changes if k != "members")
    if metadata_keys:
        parts.append(f"metadata edit — {', '.join(metadata_keys)}")
    return " + ".join(parts) or "Edit"


# ----- custom-workflow internals ----------------------------------------

_CUSTOM_MIN_MEMBERS = 2
_CUSTOM_MAX_MEMBERS = 10


def _ensure_custom_member_count(tool_aliases: list[str]) -> None:
    """Custom workflows accept 2–10 tools per the locked design."""
    if len(tool_aliases) < _CUSTOM_MIN_MEMBERS:
        raise TooFewMembersError
    if len(tool_aliases) > _CUSTOM_MAX_MEMBERS:
        raise TooManyMembersError(len(tool_aliases))


async def _load_member_tools(
    session: AsyncSession,
    tool_aliases: list[str],
) -> dict[str, Tool]:
    """Fetch every member :class:`Tool` row keyed by alias."""
    if not tool_aliases:
        return {}
    stmt = select(Tool).where(Tool.alias.in_(list(set(tool_aliases))))
    return {t.alias: t for t in (await session.execute(stmt)).scalars()}


def _assert_members_exist_and_not_archived(
    tool_aliases: list[str],
    tool_rows: dict[str, Tool],
) -> None:
    """Raise on the first member alias that's missing or archived."""
    for alias in tool_aliases:
        tool = tool_rows.get(alias)
        if tool is None:
            raise UnknownToolMemberError(alias)
        if tool.archived_at is not None:
            raise ArchivedToolMemberError(alias)


async def _assert_member_tools_visible(
    session: AsyncSession,
    *,
    user_id: int,
    tool_rows: dict[str, Tool],
) -> None:
    """Every member tool must be visible to ``user_id``.

    Catches the "I'm trying to add a tool I'm not allowed to see"
    case at the service boundary. The UI usually filters to visible
    tools anyway, but this guards against direct-API hits.
    """
    for tool in tool_rows.values():
        if not await user_can_see_tool(session, user_id=user_id, tool=tool):
            raise UnknownToolMemberError(tool.alias)


async def _validate_share_recipients(
    session: AsyncSession,
    *,
    owner_id: int,
    recipient_ids: list[int],
) -> dict[int, User]:
    """Resolve recipient ids to User rows, dedup-aware.

    Refuses: sharing with yourself, missing users, deactivated users.
    Returns ``{user_id: User}`` so callers can pass the username into
    error messages without re-fetching.
    """
    if not recipient_ids:
        return {}
    unique_ids = sorted(set(recipient_ids))
    if owner_id in unique_ids:
        raise InvalidShareRecipientError(
            "You can't share a workflow with yourself — you already own it."
        )
    stmt = select(User).where(User.id.in_(unique_ids))
    found = {u.id: u for u in (await session.execute(stmt)).scalars()}
    missing = [rid for rid in unique_ids if rid not in found]
    if missing:
        raise InvalidShareRecipientError(
            f"Couldn't find user(s) with id(s) {missing}. Pick from the existing users list."
        )
    inactive = [u for u in found.values() if not u.is_active]
    if inactive:
        names = ", ".join(sorted(u.username for u in inactive))
        raise InvalidShareRecipientError(
            f"These users are deactivated and can't receive shared workflows: {names}."
        )
    return found


async def _assert_recipient_sees_member_tools(
    session: AsyncSession,
    *,
    recipient_id: int,
    recipient_username: str,
    tool_rows: dict[str, Tool],
) -> None:
    """Refuse the share when the recipient can't see a member tool.

    Per locked design: prompt the owner (raise) rather than silently
    hide. The :class:`ToolNotVisibleToRecipientError` message names
    both the recipient and the offending tool so the owner can
    decide whether to drop the recipient, drop the tool, or chase an
    admin grant.
    """
    for tool in tool_rows.values():
        if not await user_can_see_tool(session, user_id=recipient_id, tool=tool):
            raise ToolNotVisibleToRecipientError(
                recipient_username=recipient_username,
                tool_alias=tool.alias,
                tool_name=tool.name,
            )


def _build_custom_descriptor(draft: CustomWorkflowDraft) -> dict[str, Any]:
    """Assemble the JSON descriptor stored on a custom workflow row.

    Custom workflows don't use the ``teams`` field — visibility goes
    through the share-list mechanism instead. ``copied_from_workflow_id``
    is set by :meth:`copy_to_owner` and omitted here.
    """
    return {
        "description": draft.description.strip(),
    }


def _populate_shares(
    session: AsyncSession,
    *,
    workflow_id: int,
    owner_id: int,
    recipient_ids: list[int],
) -> None:
    """Insert one :class:`WorkflowShare` per recipient."""
    if not recipient_ids:
        return
    now = _utcnow()
    for recipient_id in recipient_ids:
        session.add(
            WorkflowShare(
                workflow_id=workflow_id,
                user_id=recipient_id,
                shared_at=now,
                shared_by_user_id=owner_id,
            )
        )


async def _fetch_by_id(session: AsyncSession, workflow_id: int) -> Workflow | None:
    """Direct fetch by primary key, returns ``None`` when missing."""
    return await session.get(Workflow, workflow_id)


def _require_custom_kind(wf: Workflow) -> None:
    """Raise :class:`NotCustomWorkflowError` when ``wf`` isn't custom."""
    if wf.kind != WorkflowKind.CUSTOM.value:
        raise NotCustomWorkflowError(wf.alias)


def _require_owner(wf: Workflow, actor_user_id: int) -> None:
    """Owner-only gate for custom-workflow mutations."""
    if wf.owner_id != actor_user_id:
        raise NotOwnerError(wf.alias)


async def _current_share_ids(session: AsyncSession, workflow_id: int) -> set[int]:
    """Return the set of user-ids currently in this workflow's share list."""
    stmt = select(WorkflowShare.user_id).where(WorkflowShare.workflow_id == workflow_id)
    return set((await session.execute(stmt)).scalars())


def _diff_custom_workflow(
    *,
    before_name: str,
    after_name: str,
    before_desc: dict[str, Any],
    after_desc: dict[str, Any],
    before_members: list[str],
    after_members: list[str],
    before_visibility: str,
    after_visibility: str,
    before_share_ids: set[int],
    after_share_ids: set[int],
) -> dict[str, Any]:
    """Per-field old/new diff for a custom-workflow snapshot."""
    changes: dict[str, Any] = {}
    if before_name != after_name:
        changes["name"] = {"old": before_name, "new": after_name}
    for key in ("description",):
        if before_desc.get(key) != after_desc.get(key):
            changes[key] = {"old": before_desc.get(key), "new": after_desc.get(key)}
    if before_members != after_members:
        changes["members"] = {"old": before_members, "new": after_members}
    if before_visibility != after_visibility:
        changes["visibility"] = {
            "old": before_visibility,
            "new": after_visibility,
        }
    if before_share_ids != after_share_ids:
        changes["shares"] = {
            "added": sorted(after_share_ids - before_share_ids),
            "removed": sorted(before_share_ids - after_share_ids),
        }
    return changes


async def _can_user_see_workflow_for_copy(
    session: AsyncSession,
    *,
    user_id: int,
    wf: Workflow,
) -> bool:
    """Visibility gate for :meth:`WorkflowService.copy_to_owner`.

    Returns ``True`` when the caller is allowed to fork ``wf``:

    - Admin workflows: state + team gate (same as
      :meth:`WorkflowService.list_visible`).
    - Custom workflows owned by the caller: always.
    - Custom workflows shared with the caller: always.
    - Any custom workflow when the caller holds
      ``custom_workflows.inspect``.
    """
    if wf.kind == WorkflowKind.ADMIN.value:
        if not await user_can_see_state(session, user_id, wf.state):
            return False
        teams = _workflow_team_names(wf)
        # Lazy import to avoid the tools.service ↔ workflows.service cycle.
        from matofx.tools.service import WORLD_TEAM_NAME, _user_team_names  # noqa: PLC0415

        if not teams or WORLD_TEAM_NAME in teams:
            return True
        actor_teams = await _user_team_names(session, user_id)
        return bool(set(teams) & actor_teams)

    # Custom workflow.
    if wf.owner_id == user_id:
        return True
    share = await session.execute(
        select(WorkflowShare.user_id).where(
            WorkflowShare.workflow_id == wf.id,
            WorkflowShare.user_id == user_id,
        )
    )
    if share.scalar_one_or_none() is not None:
        return True
    return await user_has_permission(session, user_id, Permission.CUSTOM_WORKFLOWS_INSPECT)


async def _pick_copy_name(
    session: AsyncSession,
    *,
    source_name: str,
    owner_id: int,
) -> str:
    """Pick the new copy's display name, bumping on owner-collision.

    The format is ``"<source_name> (copy)"`` for the first fork; if the
    owner already has a workflow with that name, ``" (copy 2)"``,
    ``" (copy 3)"``, etc. until a free slot is found. Limited to a few
    dozen attempts; in practice the loop almost always exits on the
    first iteration.
    """
    base = f"{source_name} (copy)"
    if not await _owner_has_workflow_named(session, owner_id=owner_id, name=base):
        return base
    for suffix in range(2, 100):
        candidate = f"{source_name} (copy {suffix})"
        if not await _owner_has_workflow_named(session, owner_id=owner_id, name=candidate):
            return candidate
    # Fall back to a timestamped name so we never hard-error here.
    return f"{source_name} (copy {_utcnow().isoformat(timespec='seconds')})"


async def _owner_has_workflow_named(
    session: AsyncSession,
    *,
    owner_id: int,
    name: str,
) -> bool:
    """``True`` when ``owner_id`` already has a (non-archived) workflow with this name."""
    stmt = (
        select(Workflow.id)
        .where(
            Workflow.kind == WorkflowKind.CUSTOM.value,
            Workflow.owner_id == owner_id,
            Workflow.name == name,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


def _summarise_custom_changes(changes: dict[str, Any], membership_changed: bool) -> str:
    """One-line headline describing a custom-workflow edit."""
    if not changes:
        return "No-op edit"
    parts: list[str] = []
    if membership_changed:
        parts.append("member list changed")
    if "shares" in changes:
        added = len(changes["shares"]["added"])
        removed = len(changes["shares"]["removed"])
        sentences: list[str] = []
        if added:
            sentences.append(f"shared with {added} more user(s)")
        if removed:
            sentences.append(f"unshared from {removed} user(s)")
        if sentences:
            parts.append(", ".join(sentences))
    metadata_keys = sorted(k for k in changes if k not in ("members", "shares"))
    if metadata_keys:
        parts.append(f"metadata edit — {', '.join(metadata_keys)}")
    return " + ".join(parts) or "Edit"


def make_workflow_service(audit: AuditWriter) -> WorkflowService:
    """Wire up a :class:`WorkflowService` from the ambient audit writer."""
    return WorkflowService(audit=audit)
