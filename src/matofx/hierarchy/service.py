"""Project / Scenario / Job services.

Three small services with the same shape: each owns a single resource
type, every mutation records an audit event, and access is checked at
each call site against the supplied ``actor_user_id``.

Access model (ADR-0002)
========================

A user's effective :class:`ProjectAccessLevel` on a project is the
maximum of:

- ``OWNER`` if the user is the project's ``creator_user_id`` (the
  immutable Creator — provenance + permanent authority floor).
- ``OWNER`` if the user holds :attr:`Permission.PROJECTS_SEE_ALL`
  (default: super_user, administrator).
- The tier of the single ``project_acl`` row for ``(project, user)``
  (``viewer → VIEW``, ``member → EDIT``, ``owner → OWNER``).
- Otherwise ``NONE``.

Resolution is a single user-id lookup — teams are not ACL principals
(ADR-0002 supersedes the Phase 1 ``project_team`` model). Read paths
require ``VIEW`` or above; mutate paths (project/scenario/job content)
require ``EDIT`` or above; ACL management + archive require ``OWNER``.

Anyone whose effective level is ``NONE`` gets :class:`NotFoundError`
(deliberately ambiguous with "the resource really doesn't exist", to
avoid leaking existence). Authenticated callers attempting an action
above their level get :class:`AccessDeniedError`.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeGuard

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from matofx.audit.schema import RecordInput
from matofx.hierarchy.errors import (
    AccessDeniedError,
    ConcurrentModificationError,
    CreatorImmutableError,
    DuplicateNameError,
    DuplicateToolInScenarioError,
    InvalidJobStateError,
    InvalidRoleError,
    InvalidStateError,
    JobAlreadyTerminalError,
    JobNotQueueableError,
    MetadataError,
    NotFoundError,
    ResultsWithheldError,
    StorageLocationUnavailableError,
    ToolNotRunnableError,
    UserAlreadyGrantedError,
    UserNotGrantedError,
)
from matofx.hierarchy.metadata_format import mask_has_token, matches_mask
from matofx.hierarchy.orm import (
    ACL_ROLE_TO_LEVEL,
    DEFAULT_NUMBER_FORMAT,
    JOB_STATES,
    NUMBER_FORMATS,
    PROJECT_ACL_ROLES,
    PROJECT_METADATA_FIELD_TYPES,
    PROJECT_STATES,
    SELECT_FIELD_TYPES,
    Job,
    Project,
    ProjectAccessLevel,
    ProjectAcl,
    ProjectMetadata,
    ProjectMetadataField,
    ProjectMetadataValue,
    Scenario,
)
from matofx.users.permissions import Permission, user_has_permission

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.approvals import Decision
    from matofx.approvals.orm import ApprovalRequest
    from matofx.audit import AuditWriter
    from matofx.tools.descriptor import ToolDescriptor
    from matofx.tools.exec.contracts import OutputsDocument


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


# ---- access resolution -----------------------------------------------------


_LEVEL_RANK: dict[ProjectAccessLevel, int] = {
    ProjectAccessLevel.NONE: 0,
    ProjectAccessLevel.VIEW: 1,
    ProjectAccessLevel.EDIT: 2,
    ProjectAccessLevel.OWNER: 3,
}


async def _resolve_access(
    session: AsyncSession, project: Project, user_id: int
) -> ProjectAccessLevel:
    """Return the user's effective access level on ``project`` (ADR-0002).

    A single user-id lookup: Creator-implied OWNER, then
    ``projects.see_all``-implied OWNER, then the one ``project_acl``
    row for this user resolved through :data:`ACL_ROLE_TO_LEVEL`.
    Teams are never consulted (they are not ACL principals).
    """
    if project.creator_user_id == user_id:
        return ProjectAccessLevel.OWNER
    if await user_has_permission(session, user_id, Permission.PROJECTS_SEE_ALL):
        return ProjectAccessLevel.OWNER

    grant = await session.get(ProjectAcl, (project.id, user_id))
    if grant is None:
        return ProjectAccessLevel.NONE
    return ACL_ROLE_TO_LEVEL.get(grant.role, ProjectAccessLevel.NONE)


def _meets(level: ProjectAccessLevel, required: ProjectAccessLevel) -> bool:
    """``True`` if ``level`` is at least ``required`` in the access ladder."""
    return _LEVEL_RANK[level] >= _LEVEL_RANK[required]


async def _require_project_access(
    session: AsyncSession,
    *,
    project_id: int,
    actor_user_id: int,
    required: ProjectAccessLevel,
    resource_type: str = "project",
    resource_id: int | None = None,
) -> Project:
    """Fetch a project and assert the actor has at least ``required`` access.

    Returns the fetched project (callers usually need it). Raises:

    - :class:`NotFoundError` if the project doesn't exist OR the
      actor's effective access is ``NONE`` (existence-hiding).
    - :class:`AccessDeniedError` if the actor has some access but
      below ``required``.
    """
    rid = resource_id if resource_id is not None else project_id
    project = await session.get(Project, project_id)
    if project is None:
        raise NotFoundError(resource_type, rid)
    level = await _resolve_access(session, project, actor_user_id)
    if level == ProjectAccessLevel.NONE:
        raise NotFoundError(resource_type, rid)
    if not _meets(level, required):
        raise AccessDeniedError
    return project


async def _job_project_id(session: AsyncSession, job: Job) -> int:
    """Resolve a job's owning project — via its scenario *or* its study.

    Every job has exactly one home (the ``ck_job_exactly_one_home``
    1-of-3 CHECK): ad-hoc / workflow-run children carry ``scenario_id``;
    parametric-study children carry ``parametric_study_id``;
    optimisation-study children carry ``optimisation_study_id`` (the
    other two NULL in each case). All inherit their project's ACL
    wholesale, so every per-job access gate resolves the project
    through here. Raises :class:`NotFoundError` (existence-hiding) when
    no home resolves — e.g. the parent was archived out from under the
    row.
    """
    if job.scenario_id is not None:
        scenario = await session.get(Scenario, job.scenario_id)
        if scenario is not None:
            return scenario.project_id
    elif job.parametric_study_id is not None:
        from matofx.studies.orm import ParametricStudy  # noqa: PLC0415

        study = await session.get(ParametricStudy, job.parametric_study_id)
        if study is not None:
            return study.project_id
    elif job.optimisation_study_id is not None:
        from matofx.studies.optimisation.orm import OptimisationStudy  # noqa: PLC0415

        opt_study = await session.get(OptimisationStudy, job.optimisation_study_id)
        if opt_study is not None:
            return opt_study.project_id
    raise NotFoundError("job", job.id)


# ---- metadata validation (ADR-0002) ----------------------------------------


def _coerce_number(raw: Any, label: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise MetadataError(f"'{label}' must be a number.") from exc


def _coerce_integer(raw: Any, label: str) -> int:
    num = _coerce_number(raw, label)
    if num != int(num):
        raise MetadataError(f"'{label}' must be a whole number.")
    return int(num)


def _coerce_pattern(raw: Any, mask: str, label: str) -> str:
    if raw is None or str(raw).strip() == "":
        raise MetadataError(f"Enter a value for '{label}'.")
    text = str(raw).strip()
    if not matches_mask(mask, text):
        raise MetadataError(f"'{label}' must match the format {mask}.")
    return text


def _normalise_number_format(
    field_type: str,
    number_format: str | None,
    format_mask: str | None,
) -> tuple[str | None, str | None]:
    """Validate + normalise a ``(number_format, format_mask)`` pair.

    Returns the pair to persist. Non-number fields always store
    ``(None, None)``. A ``number`` field defaults to ``decimal`` and
    only keeps a mask for the ``pattern`` format; ``pattern`` requires a
    mask with at least one token. Raises :class:`MetadataError` on a bad
    format name or an unusable mask.
    """
    if field_type != "number":
        return None, None
    fmt = number_format or DEFAULT_NUMBER_FORMAT
    if fmt not in NUMBER_FORMATS:
        raise MetadataError(
            f"'{fmt}' isn't a valid number format. Expected one of: "
            + ", ".join(NUMBER_FORMATS)
            + "."
        )
    if fmt != "pattern":
        return fmt, None
    mask = (format_mask or "").strip()
    if not mask or not mask_has_token(mask):
        raise MetadataError(
            "A custom-format field needs a mask with at least one 0 (digit) "
            "or A (letter), e.g. AA-000-0000."
        )
    return fmt, mask


def _number_metadata_row(
    field: ProjectMetadataField, sel: dict[str, Any], project_id: int
) -> ProjectMetadata:
    """Build the :class:`ProjectMetadata` row for a ``number`` field.

    Reads + validates the right selection key for the field's
    ``number_format`` (``pattern`` → text, ``integer`` / ``decimal`` →
    numeric). Raises :class:`MetadataError` on a bad value.
    """
    fmt = field.number_format or DEFAULT_NUMBER_FORMAT
    if fmt == "pattern":
        return ProjectMetadata(
            project_id=project_id,
            field_id=field.id,
            value_text=_coerce_pattern(sel.get("value_text"), field.format_mask or "", field.label),
        )
    if fmt == "integer":
        return ProjectMetadata(
            project_id=project_id,
            field_id=field.id,
            value_num=_coerce_integer(sel.get("value_num"), field.label),
        )
    return ProjectMetadata(
        project_id=project_id,
        field_id=field.id,
        value_num=_coerce_number(sel.get("value_num"), field.label),
    )


def _coerce_date(raw: Any, label: str) -> _dt.date:
    if isinstance(raw, _dt.datetime):
        return raw.date()
    if isinstance(raw, _dt.date):
        return raw
    try:
        return _dt.date.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise MetadataError(f"'{label}' must be a date (YYYY-MM-DD).") from exc


async def _build_metadata_rows(
    session: AsyncSession,
    project_id: int,
    fields: dict[int, ProjectMetadataField],
    selections: list[dict[str, Any]],
) -> list[ProjectMetadata]:
    """Validate ``selections`` against the taxonomy → unsaved rows.

    Raises :class:`MetadataError` (plain-English, names the field) on
    the first problem; never writes. The caller persists the returned
    rows atomically after clearing the project's existing selections.
    """
    rows: list[ProjectMetadata] = []
    seen_single: set[int] = set()
    provided: set[int] = set()

    for sel in selections:
        field_id = sel.get("field_id")
        field = fields.get(field_id) if field_id is not None else None
        if field is None:
            raise MetadataError("That metadata field doesn't exist.")
        if not field.active:
            raise MetadataError(f"'{field.label}' is no longer an available metadata field.")
        provided.add(field.id)

        if field.field_type in SELECT_FIELD_TYPES:
            value_id = sel.get("value_id")
            if value_id is None:
                raise MetadataError(f"Pick a value for '{field.label}'.")
            value = await session.get(ProjectMetadataValue, value_id)
            if value is None or value.field_id != field.id:
                raise MetadataError(f"That isn't a valid option for '{field.label}'.")
            if not value.active:
                raise MetadataError(
                    f"'{value.value}' is no longer a selectable option for '{field.label}'."
                )
            if field.field_type == "single_select":
                if field.id in seen_single:
                    raise MetadataError(f"'{field.label}' only accepts one value.")
                seen_single.add(field.id)
            rows.append(
                ProjectMetadata(project_id=project_id, field_id=field.id, value_id=value_id)
            )
        elif field.field_type == "number":
            rows.append(_number_metadata_row(field, sel, project_id))
        else:  # date
            rows.append(
                ProjectMetadata(
                    project_id=project_id,
                    field_id=field.id,
                    value_date=_coerce_date(sel.get("value_date"), field.label),
                )
            )

    missing = [f.label for f in fields.values() if f.active and f.required and f.id not in provided]
    if missing:
        raise MetadataError(
            "These metadata fields are required: " + ", ".join(sorted(missing)) + "."
        )
    return rows


# ---- services --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectWithMeta:
    """A project plus the display fields the My Projects table needs.

    ``owner_user_id`` / ``owner_name`` resolve the project's *current*
    owner (the home-redesign §8.1 "Owner" column): the holder of an
    explicit ``owner`` ACL grant after a transfer, else the immutable
    Creator. ``my_role`` is the caller's resolved ACL role
    (``"owner"`` / ``"member"`` / ``"viewer"``) for the "My role"
    column — Creator and ``projects.see_all`` both resolve to
    ``"owner"``. Computed in a handful of batch queries by
    :meth:`ProjectService.list_for_user_with_meta` so the table never
    fans out a lookup per row.
    """

    project: Project
    owner_user_id: int
    owner_name: str
    my_role: str


@dataclass(frozen=True)
class ProjectService:
    """CRUD for projects + the per-user four-role ACL (ADR-0002).

    The Creator is the immutable ``project.creator_user_id`` (resolved
    to Owner-equivalent authority, never an ACL row). Owners/Members/
    Viewers are ``project_acl`` rows. Edit / ACL / archive / metadata
    require OWNER; create scenario / run job require EDIT (the Member
    tier); read requires VIEW.
    """

    audit: AuditWriter

    # ---- create / read / update --------------------------------------

    async def create(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        name: str,
        description: str = "",
        creator_user_id: int | None = None,
    ) -> Project:
        """Create a project. The Creator defaults to the actor.

        Creating a project *for another user* (a different
        ``creator_user_id``) requires
        :attr:`Permission.PROJECTS_CREATE_FOR_OTHERS`. The
        ``creator_user_id`` written here is frozen for the life of the
        project (provenance + permanent authority floor).
        """
        creator = creator_user_id if creator_user_id is not None else actor_user_id
        if creator != actor_user_id and not await user_has_permission(
            session, actor_user_id, Permission.PROJECTS_CREATE_FOR_OTHERS
        ):
            raise AccessDeniedError

        project = Project(
            name=name.strip(),
            description=description,
            creator_user_id=creator,
        )
        session.add(project)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateNameError("project") from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="hierarchy.project.create",
                resource_type="project",
                resource_id=str(project.id),
                outcome="success",
                payload={"name": project.name, "creator_user_id": creator},
            ),
        )
        return project

    async def get(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        actor_user_id: int,
    ) -> Project:
        """Fetch one project; requires VIEW access."""
        return await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.VIEW,
        )

    async def list_for_user(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        include_archived: bool = False,
    ) -> list[Project]:
        """List every project visible to the actor.

        Includes projects the user created, projects they hold an ACL
        grant on, and (with ``projects.see_all``) every project in the
        install. ``include_archived`` keeps archived/cancelled rows in
        the result; by default only ``active`` projects are returned
        (the home table hides archived behind a filter).
        """
        stmt = select(Project).order_by(Project.created_at.desc())
        if not include_archived:
            stmt = stmt.where(Project.state == "active")
        if not await user_has_permission(session, actor_user_id, Permission.PROJECTS_SEE_ALL):
            shared = select(ProjectAcl.project_id).where(ProjectAcl.user_id == actor_user_id)
            stmt = stmt.where((Project.creator_user_id == actor_user_id) | Project.id.in_(shared))
        return list((await session.execute(stmt)).scalars())

    async def list_for_user_with_meta(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        include_archived: bool = False,
    ) -> list[ProjectWithMeta]:
        """:meth:`list_for_user` enriched with owner + caller-role display fields.

        Resolves the My Projects table's "Owner" and "My role" columns
        (home-redesign §8.1) in batch — three small queries total,
        regardless of how many projects come back — so the table never
        does a per-row lookup:

        1. every explicit ``owner`` ACL grant across the visible
           projects (the post-transfer owner; absent → Creator owns);
        2. the caller's own ACL rows (for "My role");
        3. the display names for the resolved owner ids.

        Creator and ``projects.see_all`` both resolve the caller's role
        to ``"owner"``; otherwise it's the stored ACL role, defaulting
        to ``"viewer"`` (the floor for a project that's visible at all).
        """
        from matofx.users.orm import User  # noqa: PLC0415 — avoid a users↔hierarchy import cycle

        projects = await self.list_for_user(
            session, actor_user_id=actor_user_id, include_archived=include_archived
        )
        if not projects:
            return []
        project_ids = [p.id for p in projects]

        owner_grant_rows = (
            await session.execute(
                select(ProjectAcl.project_id, ProjectAcl.user_id).where(
                    ProjectAcl.project_id.in_(project_ids),
                    ProjectAcl.role == "owner",
                )
            )
        ).all()
        # Not ``dict(rows)``: Row is a tuple at runtime but mypy will not
        # take it as one, and the comprehension types.
        explicit_owner: dict[int, int] = {  # noqa: C416
            uid: pid for uid, pid in owner_grant_rows
        }

        caller_rows = (
            await session.execute(
                select(ProjectAcl.project_id, ProjectAcl.role).where(
                    ProjectAcl.project_id.in_(project_ids),
                    ProjectAcl.user_id == actor_user_id,
                )
            )
        ).all()
        caller_role: dict[int, str] = {pid: role for pid, role in caller_rows}  # noqa: C416
        see_all = await user_has_permission(session, actor_user_id, Permission.PROJECTS_SEE_ALL)

        owner_ids: dict[int, int] = {
            p.id: explicit_owner.get(p.id, p.creator_user_id) for p in projects
        }
        name_rows = (
            await session.execute(
                select(User.id, User.full_name, User.username).where(
                    User.id.in_(set(owner_ids.values()))
                )
            )
        ).all()
        names: dict[int, str] = {
            uid: (full_name or username) for uid, full_name, username in name_rows
        }

        out: list[ProjectWithMeta] = []
        for p in projects:
            oid = owner_ids[p.id]
            if p.creator_user_id == actor_user_id or see_all:
                my_role = "owner"
            else:
                my_role = caller_role.get(p.id, "viewer")
            out.append(
                ProjectWithMeta(
                    project=p,
                    owner_user_id=oid,
                    owner_name=names.get(oid, ""),
                    my_role=my_role,
                )
            )
        return out

    async def update(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        actor_user_id: int,
        name: str | None = None,
        description: str | None = None,
        expected_version: int | None = None,
    ) -> Project:
        """Update name and/or description; requires OWNER (Creator/Owner).

        Pass ``expected_version`` (the version the caller saw) to opt
        into optimistic locking — :class:`ConcurrentModificationError`
        is raised if another writer updated the project since.
        """
        project = await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.OWNER,
        )
        if expected_version is not None and project.version != expected_version:
            raise ConcurrentModificationError("project")
        if name is not None:
            project.name = name.strip()
        if description is not None:
            project.description = description
        try:
            await session.flush()
        except StaleDataError as exc:
            raise ConcurrentModificationError("project") from exc
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateNameError("project") from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="hierarchy.project.update",
                resource_type="project",
                resource_id=str(project.id),
                outcome="success",
                payload={"name": project.name},
            ),
        )
        return project

    # ---- lifecycle (no-delete / §D20) --------------------------------

    async def set_state(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        actor_user_id: int,
        state: str,
    ) -> Project:
        """Move a project to ``archived`` / ``cancelled``. OWNER required.

        Nothing is ever deleted (§D20) — this only flips the lifecycle
        ``state`` and stamps ``state_changed_at`` / ``_by``. ``active``
        is not a valid target here (un-archive is not a v1 flow); a
        no-op transition raises :class:`InvalidStateError`.
        """
        if state not in PROJECT_STATES or state == "active":
            raise InvalidStateError(
                "Projects can only be archived or cancelled, not deleted or reactivated."
            )
        project = await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.OWNER,
        )
        if project.state == state:
            raise InvalidStateError(f"This project is already {state}.")
        project.state = state
        project.state_changed_at = _utcnow()
        project.state_changed_by_user_id = actor_user_id
        try:
            await session.flush()
        except StaleDataError as exc:
            raise ConcurrentModificationError("project") from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="hierarchy.project.archive",
                resource_type="project",
                resource_id=str(project.id),
                outcome="success",
                payload={"state": state},
            ),
        )
        return project

    # ---- ownership transfer (admin / Owner) --------------------------

    async def transfer_ownership(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        actor_user_id: int,
        new_owner_user_id: int,
    ) -> Project:
        """Hand the project to a successor Owner (ADR-0002).

        ``creator_user_id`` is **never** touched. The successor is
        granted an ``owner`` ACL row; **every other non-Creator
        ``owner`` grant is revoked** (Members/Viewers untouched). If
        the successor is the Creator the ACL is left alone (the Creator
        is already the authority floor) but other Owners are still
        revoked.

        Allowed when the actor is the Creator, holds an explicit
        ``owner`` ACL grant, or holds
        :attr:`Permission.PROJECTS_TRANSFER_OWNERSHIP` (default:
        super_user only — ``projects.see_all`` alone is deliberately
        *not* enough, mirroring §D20's gating of the admin transfer
        path behind a dedicated permission). No access at all →
        existence-hidden.
        """
        project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("project", project_id)

        level = await _resolve_access(session, project, actor_user_id)
        is_creator = project.creator_user_id == actor_user_id
        own_grant = await session.get(ProjectAcl, (project_id, actor_user_id))
        is_explicit_owner = own_grant is not None and own_grant.role == "owner"
        has_perm = await user_has_permission(
            session, actor_user_id, Permission.PROJECTS_TRANSFER_OWNERSHIP
        )
        if not (is_creator or has_perm or is_explicit_owner):
            if level == ProjectAccessLevel.NONE:
                raise NotFoundError("project", project_id)
            raise AccessDeniedError

        # Revoke every other non-Creator owner grant.
        owner_rows = (
            await session.execute(
                select(ProjectAcl).where(
                    ProjectAcl.project_id == project_id,
                    ProjectAcl.role == "owner",
                )
            )
        ).scalars()
        revoked: list[int] = []
        for row in list(owner_rows):
            if row.user_id != new_owner_user_id:
                revoked.append(row.user_id)
                await session.delete(row)

        # Grant the successor Owner unless they are the Creator (the
        # Creator can't be an ACL row and already has full authority).
        if new_owner_user_id != project.creator_user_id:
            existing = await session.get(ProjectAcl, (project_id, new_owner_user_id))
            if existing is None:
                session.add(
                    ProjectAcl(
                        project_id=project_id,
                        user_id=new_owner_user_id,
                        role="owner",
                        granted_by_user_id=actor_user_id,
                    )
                )
            else:
                existing.role = "owner"
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="hierarchy.project.transfer_ownership",
                resource_type="project",
                resource_id=str(project.id),
                outcome="success",
                payload={
                    "new_owner_user_id": new_owner_user_id,
                    "revoked_owner_user_ids": revoked,
                    "creator_user_id": project.creator_user_id,
                },
            ),
        )
        return project

    # ---- access resolution (read-only helper) ------------------------

    async def access_for(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        actor_user_id: int,
    ) -> ProjectAccessLevel:
        """Return the actor's effective access level on the project.

        Returns :attr:`ProjectAccessLevel.NONE` rather than raising,
        so callers can branch UI on it.
        """
        project = await session.get(Project, project_id)
        if project is None:
            return ProjectAccessLevel.NONE
        return await _resolve_access(session, project, actor_user_id)

    # ---- per-user ACL management (OWNER) -----------------------------

    async def list_acl(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        actor_user_id: int,
    ) -> list[ProjectAcl]:
        """Return every per-user grant on the project. VIEW required.

        The Creator is *not* in this list (it is
        ``project.creator_user_id``); callers compose the two for
        display.
        """
        await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.VIEW,
        )
        stmt = (
            select(ProjectAcl)
            .where(ProjectAcl.project_id == project_id)
            .order_by(ProjectAcl.granted_at)
        )
        return list((await session.execute(stmt)).scalars())

    async def _guard_acl_target(self, project: Project, user_id: int) -> None:
        if user_id == project.creator_user_id:
            raise CreatorImmutableError

    async def add_acl_entry(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        user_id: int,
        role: str,
        actor_user_id: int,
    ) -> ProjectAcl:
        """Grant ``user_id`` a role on the project. OWNER required."""
        if role not in PROJECT_ACL_ROLES:
            raise InvalidRoleError(role)
        project = await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.OWNER,
        )
        await self._guard_acl_target(project, user_id)
        if await session.get(ProjectAcl, (project_id, user_id)) is not None:
            raise UserAlreadyGrantedError

        grant = ProjectAcl(
            project_id=project_id,
            user_id=user_id,
            role=role,
            granted_by_user_id=actor_user_id,
        )
        session.add(grant)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise NotFoundError("user", user_id) from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="hierarchy.project.acl.add",
                resource_type="project",
                resource_id=str(project_id),
                outcome="success",
                payload={"user_id": user_id, "role": role},
            ),
        )
        return grant

    async def update_acl_entry(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        user_id: int,
        role: str,
        actor_user_id: int,
    ) -> ProjectAcl:
        """Change ``user_id``'s role on the project. OWNER required."""
        if role not in PROJECT_ACL_ROLES:
            raise InvalidRoleError(role)
        project = await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.OWNER,
        )
        await self._guard_acl_target(project, user_id)
        grant = await session.get(ProjectAcl, (project_id, user_id))
        if grant is None:
            raise UserNotGrantedError
        if grant.role == role:
            return grant

        previous = grant.role
        grant.role = role
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="hierarchy.project.acl.update",
                resource_type="project",
                resource_id=str(project_id),
                outcome="success",
                payload={
                    "user_id": user_id,
                    "previous_role": previous,
                    "new_role": role,
                },
            ),
        )
        return grant

    async def remove_acl_entry(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        user_id: int,
        actor_user_id: int,
    ) -> None:
        """Revoke ``user_id``'s grant on the project. OWNER required."""
        project = await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.OWNER,
        )
        await self._guard_acl_target(project, user_id)
        grant = await session.get(ProjectAcl, (project_id, user_id))
        if grant is None:
            raise UserNotGrantedError

        await session.execute(
            delete(ProjectAcl).where(
                ProjectAcl.project_id == project_id,
                ProjectAcl.user_id == user_id,
            )
        )
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="hierarchy.project.acl.remove",
                resource_type="project",
                resource_id=str(project_id),
                outcome="success",
                payload={"user_id": user_id},
            ),
        )

    async def set_acl(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        grants: Iterable[tuple[int, str]],
        actor_user_id: int,
    ) -> None:
        """Replace the project's full per-user ACL with ``grants``.

        ``grants`` is an iterable of ``(user_id, role)`` pairs. Used by
        the share dialog's batch save (incl. the team bulk-pick, which
        the UI expands to individual user rows client-side). The
        Creator is never a target. OWNER required.
        """
        target = dict(grants)
        for role in target.values():
            if role not in PROJECT_ACL_ROLES:
                raise InvalidRoleError(role)
        project = await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.OWNER,
        )
        for user_id in target:
            await self._guard_acl_target(project, user_id)

        current_rows = (
            await session.execute(select(ProjectAcl).where(ProjectAcl.project_id == project_id))
        ).scalars()
        current = {row.user_id: row.role for row in current_rows}

        for user_id in set(current) - set(target):
            await self.remove_acl_entry(
                session,
                project_id=project_id,
                user_id=user_id,
                actor_user_id=actor_user_id,
            )
        for user_id, role in target.items():
            if user_id in current:
                if current[user_id] != role:
                    await self.update_acl_entry(
                        session,
                        project_id=project_id,
                        user_id=user_id,
                        role=role,
                        actor_user_id=actor_user_id,
                    )
            else:
                await self.add_acl_entry(
                    session,
                    project_id=project_id,
                    user_id=user_id,
                    role=role,
                    actor_user_id=actor_user_id,
                )

    # ---- typed metadata (OWNER to write, VIEW to read) ---------------

    async def get_metadata(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        actor_user_id: int,
    ) -> list[ProjectMetadata]:
        """Return the project's metadata selections. VIEW required."""
        await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.VIEW,
        )
        stmt = select(ProjectMetadata).where(ProjectMetadata.project_id == project_id)
        return list((await session.execute(stmt)).scalars())

    async def set_metadata(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        actor_user_id: int,
        selections: Iterable[dict[str, Any]],
    ) -> None:
        """Replace the project's metadata selections. OWNER required.

        ``selections`` is an iterable of dicts, each ``{"field_id": N,
        "value_id"|"value_num"|"value_date": ...}``. Validated against
        the live taxonomy: unknown/inactive field → error; value type
        must match the field type; a select value must be an active
        option of that field; every ``required`` active field must be
        present. All-or-nothing — a single bad entry rejects the whole
        save and nothing is written.
        """
        await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.OWNER,
        )

        fields = {f.id: f for f in (await session.execute(select(ProjectMetadataField))).scalars()}
        rows = await _build_metadata_rows(session, project_id, fields, list(selections))

        await session.execute(
            delete(ProjectMetadata).where(ProjectMetadata.project_id == project_id)
        )
        for row in rows:
            session.add(row)
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="hierarchy.project.metadata.update",
                resource_type="project",
                resource_id=str(project_id),
                outcome="success",
                payload={"selection_count": len(rows)},
            ),
        )

    # ---- metadata taxonomy admin (projects.manage_metadata) ----------

    async def _require_metadata_admin(self, session: AsyncSession, actor_user_id: int) -> None:
        if not await user_has_permission(
            session, actor_user_id, Permission.PROJECTS_MANAGE_METADATA
        ):
            raise AccessDeniedError

    async def list_active_metadata_fields(
        self, session: AsyncSession
    ) -> list[tuple[ProjectMetadataField, list[ProjectMetadataValue]]]:
        """Active fields + their active values, for the project form.

        No permission gate — any user who can create/edit a project
        needs the taxonomy to render the form. Inactive fields/values
        are excluded (they stay on projects already using them but are
        never offered for new selections).
        """
        fields = list(
            (
                await session.execute(
                    select(ProjectMetadataField)
                    .where(ProjectMetadataField.active.is_(True))
                    .order_by(ProjectMetadataField.display_order, ProjectMetadataField.id)
                )
            ).scalars()
        )
        out: list[tuple[ProjectMetadataField, list[ProjectMetadataValue]]] = []
        for f in fields:
            values: list[ProjectMetadataValue] = []
            if f.field_type in SELECT_FIELD_TYPES:
                values = list(
                    (
                        await session.execute(
                            select(ProjectMetadataValue)
                            .where(
                                ProjectMetadataValue.field_id == f.id,
                                ProjectMetadataValue.active.is_(True),
                            )
                            .order_by(
                                ProjectMetadataValue.display_order,
                                ProjectMetadataValue.id,
                            )
                        )
                    ).scalars()
                )
            out.append((f, values))
        return out

    async def admin_list_metadata_fields(
        self, session: AsyncSession, *, actor_user_id: int
    ) -> list[tuple[ProjectMetadataField, list[ProjectMetadataValue]]]:
        """Every field (incl. inactive) + every value. Admin-gated."""
        await self._require_metadata_admin(session, actor_user_id)
        fields = list(
            (
                await session.execute(
                    select(ProjectMetadataField).order_by(
                        ProjectMetadataField.display_order, ProjectMetadataField.id
                    )
                )
            ).scalars()
        )
        out: list[tuple[ProjectMetadataField, list[ProjectMetadataValue]]] = []
        for f in fields:
            values = list(
                (
                    await session.execute(
                        select(ProjectMetadataValue)
                        .where(ProjectMetadataValue.field_id == f.id)
                        .order_by(
                            ProjectMetadataValue.display_order,
                            ProjectMetadataValue.id,
                        )
                    )
                ).scalars()
            )
            out.append((f, values))
        return out

    async def admin_create_metadata_field(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        key: str,
        label: str,
        field_type: str,
        required: bool = False,
        display_order: int = 0,
        show_in_table: bool = False,
        number_format: str | None = None,
        format_mask: str | None = None,
    ) -> ProjectMetadataField:
        """Create a metadata field. Admin-gated.

        For ``field_type == "number"``, ``number_format`` selects
        ``integer`` / ``decimal`` / ``pattern`` (``None`` ⇒ ``decimal``);
        a ``pattern`` field requires ``format_mask`` (e.g. ``AA-000-0000``).
        Both are forced to ``None`` for non-number fields.
        """
        await self._require_metadata_admin(session, actor_user_id)
        if field_type not in PROJECT_METADATA_FIELD_TYPES:
            raise MetadataError(
                f"'{field_type}' isn't a valid field type. Expected one of: "
                + ", ".join(PROJECT_METADATA_FIELD_TYPES)
                + "."
            )
        norm_format, norm_mask = _normalise_number_format(field_type, number_format, format_mask)
        field = ProjectMetadataField(
            key=key.strip(),
            label=label.strip(),
            field_type=field_type,
            number_format=norm_format,
            format_mask=norm_mask,
            required=required,
            display_order=display_order,
            show_in_table=show_in_table,
            created_by_user_id=actor_user_id,
        )
        session.add(field)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateNameError("metadata field") from exc
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="admin.metadata.field.create",
                resource_type="project_metadata_field",
                resource_id=str(field.id),
                outcome="success",
                payload={"key": field.key, "field_type": field_type},
            ),
        )
        return field

    async def admin_update_metadata_field(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        field_id: int,
        label: str | None = None,
        required: bool | None = None,
        display_order: int | None = None,
        show_in_table: bool | None = None,
        number_format: str | None = None,
        format_mask: str | None = None,
        active: bool | None = None,
    ) -> ProjectMetadataField:
        """Edit or retire a metadata field. Admin-gated.

        ``key`` and ``field_type`` are immutable (existing project
        selections depend on them). ``number_format`` / ``format_mask``
        only apply to ``number`` fields and re-validate the same way as
        on create; changing them affects new selections only (already
        stored values are kept as-is, per the no-delete model).
        """
        await self._require_metadata_admin(session, actor_user_id)
        field = await session.get(ProjectMetadataField, field_id)
        if field is None:
            raise NotFoundError("metadata field", field_id)
        if label is not None:
            field.label = label.strip()
        if required is not None:
            field.required = required
        if display_order is not None:
            field.display_order = display_order
        if show_in_table is not None:
            field.show_in_table = show_in_table
        if number_format is not None or format_mask is not None:
            if field.field_type != "number":
                raise MetadataError(
                    f"'{field.label}' isn't a number field — it has no number format."
                )
            field.number_format, field.format_mask = _normalise_number_format(
                field.field_type,
                number_format if number_format is not None else field.number_format,
                format_mask if format_mask is not None else field.format_mask,
            )
        action = "admin.metadata.field.update"
        if active is not None and active != field.active:
            field.active = active
            field.state_changed_at = _utcnow()
            field.state_changed_by_user_id = actor_user_id
            action = "admin.metadata.field.deactivate" if not active else action
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=action,
                resource_type="project_metadata_field",
                resource_id=str(field.id),
                outcome="success",
                payload={"active": field.active},
            ),
        )
        return field

    async def admin_create_metadata_value(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        field_id: int,
        value: str,
        display_order: int = 0,
    ) -> ProjectMetadataValue:
        """Add an allowed value to a select field. Admin-gated."""
        await self._require_metadata_admin(session, actor_user_id)
        field = await session.get(ProjectMetadataField, field_id)
        if field is None:
            raise NotFoundError("metadata field", field_id)
        if field.field_type not in SELECT_FIELD_TYPES:
            raise MetadataError(
                f"'{field.label}' is a {field.field_type} field — it has no value list."
            )
        row = ProjectMetadataValue(
            field_id=field_id, value=value.strip(), display_order=display_order
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateNameError("metadata value") from exc
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="admin.metadata.value.create",
                resource_type="project_metadata_value",
                resource_id=str(row.id),
                outcome="success",
                payload={"field_id": field_id, "value": row.value},
            ),
        )
        return row

    async def admin_update_metadata_value(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        value_id: int,
        value: str | None = None,
        display_order: int | None = None,
        active: bool | None = None,
    ) -> ProjectMetadataValue:
        """Rename / reorder / retire a metadata value. Admin-gated."""
        await self._require_metadata_admin(session, actor_user_id)
        row = await session.get(ProjectMetadataValue, value_id)
        if row is None:
            raise NotFoundError("metadata value", value_id)
        if value is not None:
            row.value = value.strip()
        if display_order is not None:
            row.display_order = display_order
        if active is not None:
            row.active = active
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateNameError("metadata value") from exc
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="admin.metadata.value.update",
                resource_type="project_metadata_value",
                resource_id=str(row.id),
                outcome="success",
                payload={"active": row.active},
            ),
        )
        return row

    async def admin_reorder_metadata_values(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        field_id: int,
        ordered_value_ids: list[int],
    ) -> None:
        """Set each value's ``display_order`` to its index in the list.

        ``ordered_value_ids`` must be exactly the field's value ids (all
        of them, including retired ones), each once. Admin-gated.
        """
        await self._require_metadata_admin(session, actor_user_id)
        field = await session.get(ProjectMetadataField, field_id)
        if field is None:
            raise NotFoundError("metadata field", field_id)
        rows = list(
            (
                await session.execute(
                    select(ProjectMetadataValue).where(ProjectMetadataValue.field_id == field_id)
                )
            ).scalars()
        )
        by_id = {r.id: r for r in rows}
        if len(ordered_value_ids) != len(by_id) or set(ordered_value_ids) != set(by_id):
            raise MetadataError(
                "The reorder list must include every value of this field exactly once."
            )
        for index, value_id in enumerate(ordered_value_ids):
            by_id[value_id].display_order = index
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="admin.metadata.value.reorder",
                resource_type="project_metadata_field",
                resource_id=str(field_id),
                outcome="success",
                payload={"order": ordered_value_ids},
            ),
        )


@dataclass(frozen=True)
class ScenarioService:
    """CRUD for scenarios."""

    audit: AuditWriter

    async def create(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        project_id: int,
        name: str,
        description: str = "",
    ) -> Scenario:
        """Create a scenario inside ``project_id``. EDIT required on the project."""
        await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.EDIT,
        )

        scenario = Scenario(
            project_id=project_id,
            name=name.strip(),
            description=description,
        )
        session.add(scenario)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateNameError("scenario") from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="hierarchy.scenario.create",
                resource_type="scenario",
                resource_id=str(scenario.id),
                outcome="success",
                payload={"project_id": project_id, "name": scenario.name},
            ),
        )
        return scenario

    async def get(
        self,
        session: AsyncSession,
        *,
        scenario_id: int,
        actor_user_id: int,
    ) -> Scenario:
        """Fetch one scenario; VIEW required on the parent project."""
        scenario = await session.get(Scenario, scenario_id)
        if scenario is None:
            raise NotFoundError("scenario", scenario_id)
        await _require_project_access(
            session,
            project_id=scenario.project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.VIEW,
            resource_type="scenario",
            resource_id=scenario_id,
        )
        return scenario

    async def list_for_project(
        self,
        session: AsyncSession,
        *,
        project_id: int,
        actor_user_id: int,
    ) -> list[Scenario]:
        """List scenarios in ``project_id``; VIEW required on the project."""
        await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.VIEW,
        )
        stmt = (
            select(Scenario)
            .where(Scenario.project_id == project_id)
            .order_by(Scenario.created_at.desc())
        )
        return list((await session.execute(stmt)).scalars())


async def _read_output_document(
    *,
    mount_path: str,
    working_dir_relative: str,
    tool_alias: str,
) -> OutputsDocument:
    """Parse ``<wd>/outputs/outputs.json`` from disk into an :class:`OutputsDocument`.

    Used by :meth:`JobService.get_output_document`. Lives at module
    scope (rather than as a method) so it can be unit-tested without a
    full service+session fixture, and so the I/O happens off the event
    loop via :func:`asyncio.to_thread`. Missing-file and malformed-file
    both resolve to an empty document for the same reason: the Compare
    workspace prefers a graceful "no outputs available" cell to a 500.
    """
    import asyncio  # noqa: PLC0415 — narrow scope, no module-level dep
    import json  # noqa: PLC0415
    import logging  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from matofx.tools.exec.contracts import (  # noqa: PLC0415
        OUTPUTS_DOCUMENT_SCHEMA_VERSION,
        OutputsDocument,
    )

    logger = logging.getLogger(__name__)
    empty = OutputsDocument(
        schema_version=OUTPUTS_DOCUMENT_SCHEMA_VERSION,
        tool_alias=tool_alias,
        outputs=[],
    )

    target = Path(mount_path) / working_dir_relative / "outputs" / "outputs.json"

    def _read_blocking() -> bytes | None:
        try:
            return target.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning(
                "output_document.io_error",
                extra={"path": str(target), "error": str(exc)},
            )
            return None

    raw = await asyncio.to_thread(_read_blocking)
    if raw is None:
        return empty
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "output_document.malformed_json",
            extra={"path": str(target), "error": str(exc)},
        )
        return empty
    try:
        return OutputsDocument.model_validate(decoded)
    except Exception as exc:
        logger.warning(
            "output_document.schema_mismatch",
            extra={"path": str(target), "error": str(exc)},
        )
        return empty


def _output_leaf(filename: str) -> str:
    """Normalise a descriptor file-output ``filename`` to its outputs-relative leaf.

    A descriptor may declare the filename relative to the working dir *with*
    the ``outputs/`` prefix (e.g. ``"outputs/temperature_final.vtu"``) or as a
    bare leaf (``"result.csv"``). Both must resolve under ``<wd>/outputs/``, so
    strip a leading ``./`` / ``outputs/`` before joining — the same
    normalisation the jobs-output download route applies (``jobs.py``).
    """
    leaf = filename.replace("\\", "/").strip("/")
    while leaf.startswith("./"):
        leaf = leaf[2:]
    if leaf.startswith("outputs/"):
        leaf = leaf[len("outputs/") :]
    return leaf


def _job_file_output_specs(
    descriptor: Mapping[str, Any],
    accepted_exts: set[str],
    expected_field_array: str | None = None,
) -> list[dict[str, Any]]:
    """File-output specs (name / label / filename / file_type) for the picker.

    Walks the descriptor's ``outputs`` for ``type="file"`` entries that
    declare a ``filename``. When ``accepted_exts`` is non-empty (lowercase,
    no leading dot — the target input's allowed extensions) only outputs
    whose filename suffix matches survive, so the cross-tool picker never
    offers a file the consuming input can't accept.

    When ``expected_field_array`` is given (the consuming input's
    ``expected_field_array`` — the nodal array it needs, e.g. ``"temperature"``)
    only **field** outputs carrying that exact array survive, so a
    temperature-field input never lists a failure-index field even though both
    are ``.vtu``. Malformed entries skip.
    """
    want_field = (expected_field_array or "").strip().lower()
    specs: list[dict[str, Any]] = []
    for out in descriptor.get("outputs") or []:
        if not isinstance(out, Mapping) or out.get("type") != "file":
            continue
        name = out.get("name")
        filename = out.get("filename")
        if not (isinstance(name, str) and name and isinstance(filename, str) and filename):
            continue
        if accepted_exts:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext not in accepted_exts:
                continue
        if want_field and str(out.get("field_node_array_name") or "").strip().lower() != want_field:
            continue
        specs.append(
            {
                "name": name,
                "label": out.get("label") or name,
                "filename": filename,
                "file_type": out.get("file_type") or "",
                # The nodal array a field file carries (e.g. "temperature" vs
                # "failure_index") — lets the picker label distinguish two
                # otherwise look-alike ``.vtu`` files.
                "field_array": out.get("field_node_array_name") or "",
            }
        )
    return specs


def _collect_output_resolution_work(
    candidates: list[Job],
    descriptors_by_version: dict[int, Mapping[str, Any]],
    mounts_by_location: dict[int, str],
    accepted_exts: set[str],
    expected_field_array: str | None = None,
) -> tuple[list[tuple[int, str, list[dict[str, Any]]]], dict[int, list[dict[str, Any]]]]:
    """Build the ``(job_id, working_dir, specs)`` stat list for the picker.

    Skips jobs missing a pinned version / storage location / working dir, or
    whose descriptor declares no (matching) file outputs. Returns the work
    list plus a ``{job_id: specs}`` map reused when assembling result rows.
    """
    from pathlib import Path  # noqa: PLC0415

    specs_by_job: dict[int, list[dict[str, Any]]] = {}
    work: list[tuple[int, str, list[dict[str, Any]]]] = []
    for job in candidates:
        if (
            job.tool_version_id is None
            or job.storage_location_id is None
            or not job.working_dir_relative
        ):
            continue
        descriptor = descriptors_by_version.get(int(job.tool_version_id))
        mount = mounts_by_location.get(int(job.storage_location_id))
        if descriptor is None or mount is None:
            continue
        specs = _job_file_output_specs(descriptor, accepted_exts, expected_field_array)
        if not specs:
            continue
        specs_by_job[int(job.id)] = specs
        work.append((int(job.id), str(Path(mount) / job.working_dir_relative), specs))
    return work, specs_by_job


def _assemble_output_candidate_rows(
    candidates: list[Job],
    *,
    resolved: dict[int, dict[str, str]],
    descriptors_by_version: dict[int, Mapping[str, Any]],
    specs_by_job: dict[int, list[dict[str, Any]]],
    scenarios_by_id: dict[int, Scenario],
    projects_by_id: dict[int, Project],
) -> list[dict[str, Any]]:
    """Flatten resolved file outputs into picker rows (tool → job → output)."""
    from pathlib import Path  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    for job in candidates:
        jid = int(job.id)
        found = resolved.get(jid)
        if not found:
            continue
        descriptor = (
            descriptors_by_version.get(int(job.tool_version_id))
            if job.tool_version_id is not None
            else None
        )
        tool_name = str((descriptor or {}).get("name") or job.tool_alias)
        scenario = scenarios_by_id.get(job.scenario_id) if job.scenario_id else None
        project = projects_by_id.get(scenario.project_id) if scenario is not None else None
        specs_by_name = {s["name"]: s for s in specs_by_job.get(jid, [])}
        for output_name, abs_path in found.items():
            spec = specs_by_name.get(output_name, {})
            out.append(
                {
                    "value": abs_path,
                    "filename": Path(abs_path).name,
                    "file_type": spec.get("file_type") or "",
                    "field_array": spec.get("field_array") or "",
                    "output_name": output_name,
                    "output_label": spec.get("label") or output_name,
                    "tool_alias": job.tool_alias,
                    "tool_name": tool_name,
                    "job_id": jid,
                    "project_name": project.name if project is not None else "",
                    "scenario_name": scenario.name if scenario is not None else "",
                }
            )
    return out


def _resolve_existing_output_files(
    work: list[tuple[int, str, list[dict[str, Any]]]],
) -> dict[int, dict[str, str]]:
    """Stat each candidate file output; keep only those present on disk.

    Runs in a worker thread (mount stat I/O) via :func:`asyncio.to_thread`.
    ``work`` is ``(job_id, working_dir, specs)``; returns
    ``{job_id: {output_name: absolute_path}}`` for the artefacts that exist
    (a genuinely-absent file is silently dropped — the picker shows only
    real files).
    """
    from pathlib import Path  # noqa: PLC0415

    found: dict[int, dict[str, str]] = {}
    for job_id, working_dir, specs in work:
        base = Path(working_dir) / "outputs"
        for spec in specs:
            # A descriptor ``filename`` may be declared relative to the
            # working dir *with* the ``outputs/`` prefix (e.g. Thermal Soak's
            # "outputs/temperature_final.vtu") — strip it so we don't build a
            # doubled ``outputs/outputs/`` path. Mirrors the jobs-output route.
            primary = base / _output_leaf(spec["filename"])
            if primary.is_file():
                found.setdefault(job_id, {})[spec["name"]] = str(primary)
    return found


_PLACEHOLDER_JOB_NAMES: frozenset[str] = frozenset(
    {
        "",
        "Untitled run",
    }
)
"""Names treated as 'no real name was supplied' by :meth:`JobService.create`.

When the workspace opens a fresh draft it POSTs the API default
(``"Untitled run"`` — see :class:`matofx.api.schemas.JobDraftCreate`).
We treat any of these as a request to auto-generate the structured
``Proj X Scen Y Tool Z Job 0000123`` default."""


async def _auto_job_name(
    session: AsyncSession,
    *,
    scenario: Scenario,
    tool_alias: str,
    job_id: int,
) -> str | None:
    """Build the structured default job name ``Proj X Scen Y Z Job 0000123``.

    Resolves the project name from the scenario's foreign key and the
    tool's friendly name from the ``tool`` table. Returns ``None`` when
    either lookup fails — the caller keeps the placeholder rather than
    crashing the create.
    """
    from matofx.tools.orm import Tool  # noqa: PLC0415

    project = await session.get(Project, scenario.project_id)
    project_name = (project.name if project is not None else "").strip()
    scenario_name = (scenario.name or "").strip()
    tool_row = (
        await session.execute(select(Tool).where(Tool.alias == tool_alias))
    ).scalar_one_or_none()
    tool_label = ""
    if tool_row is not None:
        tool_label = (tool_row.name or "").strip() or tool_row.alias
    if not project_name and not scenario_name and not tool_label:
        return None
    # Spec'd as ``Proj <name> Scen <name> <Tool Name> <Job ID>``. Job
    # ids zero-pad to seven digits so the rendered name stays
    # consistent with the ``format_job_id`` helper used elsewhere.
    return (f"Proj {project_name} Scen {scenario_name} {tool_label} Job {job_id:07d}").strip()


def _is_number(value: object) -> TypeGuard[float]:
    """``True`` for a real numeric value — int / float but not bool.

    Booleans are an ``int`` subclass in Python; the similarity ranker
    treats them as categorical (exact-match gate), never as a numeric
    distance axis, so they must be screened out of every numeric path.
    Typed as a :class:`TypeGuard` so a guarded ``Job.inputs.get(field)``
    (``Any | None``) narrows to a floatable value for the type checker.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class SimilarityRanked:
    """One ranked candidate from :func:`rank_jobs_by_similarity`.

    Pairs the matched :class:`Job` with its weighted distance to the
    reference point and the derived ``closeness_pct`` the results table
    shows. Rows come back sorted closest-first.
    """

    job: Job
    distance: float
    closeness_pct: float


def rank_jobs_by_similarity(
    candidates: list[Job],
    *,
    reference_inputs: dict[str, Any],
    numeric_fields: set[str],
    weights: dict[str, float] | None = None,
    top_n: int = 25,
    exclude_job_id: int | None = None,
) -> list[SimilarityRanked]:
    """Rank ``candidates`` by closeness of their inputs to a reference point.

    The "find similar runs" core (roadmap, extends §16.5). Pure and
    DB-free so it unit-tests in isolation; :meth:`JobService.find_similar_by_inputs`
    supplies the ACL-correct candidate set.

    Distance model:

    - **Categorical gate** — every field in ``reference_inputs`` that is
      *not* a numeric field (``string`` / ``boolean`` / ``select`` /
      ``multiselect`` / ``file``) and carries a non-empty reference
      value must match exactly, or the candidate is dropped. This is the
      "categorical exact match" half of the spec.
    - **Numeric distance** — over the numeric reference fields that
      actually *vary* across the gated set (constant factors carry no
      information and are skipped), each factor is min-max normalised
      into ``[0, 1]`` (the reference value is folded into the min/max so
      it normalises consistently). Distance is the square root of the
      weighted sum of squared normalised differences. A candidate
      missing a numeric factor (or holding a non-numeric value there) is
      penalised the maximum normalised difference of ``1.0`` on that
      axis rather than silently scoring as a perfect match.

    Weighting:

    - ``weights`` ``None`` / empty → **uniform** (every active factor
      weight ``1.0``).
    - ``weights`` given (``{factor: |SRC|}`` from a study's standardised
      regression coefficients) → each active factor uses its weight;
      active factors the study didn't score fall back to the mean of the
      scored weights so a swept-but-unstudied input still counts.

    ``closeness_pct`` is ``100 * (1 - distance / sqrt(sum_of_weights))``
    clamped to ``[0, 100]`` — ``100`` when there is no numeric axis to
    discriminate on (e.g. only categorical gates), so a pure-categorical
    match reads as a full match. Ties break newest-first by job id.
    """
    pool = [j for j in candidates if exclude_job_id is None or int(j.id) != int(exclude_job_id)]

    # --- categorical gate -------------------------------------------------
    categorical_ref = {
        field: value
        for field, value in reference_inputs.items()
        if field not in numeric_fields and value not in (None, "", [])
    }
    gated = [
        job
        for job in pool
        if all((job.inputs or {}).get(field) == value for field, value in categorical_ref.items())
    ]

    # --- active numeric axes + their normalisation ranges -----------------
    numeric_ref = {
        field: float(value)
        for field, value in reference_inputs.items()
        if field in numeric_fields and _is_number(value)
    }
    ranges: dict[str, tuple[float, float]] = {}
    for field, ref_val in numeric_ref.items():
        lo = hi = ref_val
        for job in gated:
            got = (job.inputs or {}).get(field)
            if _is_number(got):
                got_f = float(got)
                lo = min(lo, got_f)
                hi = max(hi, got_f)
        if hi > lo:  # constant factors carry no information — skip
            ranges[field] = (lo, hi)
    active = list(ranges)

    # --- resolve per-axis weights ----------------------------------------
    if weights:
        scored = [w for w in (weights.get(f, 0.0) for f in active) if w and w > 0]
        default_w = (sum(scored) / len(scored)) if scored else 1.0
        weight_of = {
            f: (weights[f] if (f in weights and weights[f] and weights[f] > 0) else default_w)
            for f in active
        }
    else:
        weight_of = dict.fromkeys(active, 1.0)
    sum_w = sum(weight_of.values())
    max_distance = math.sqrt(sum_w) if sum_w > 0 else 0.0

    # --- score every gated candidate -------------------------------------
    ranked: list[SimilarityRanked] = []
    for job in gated:
        inputs = job.inputs or {}
        acc = 0.0
        for field in active:
            lo, hi = ranges[field]
            span = hi - lo
            ref_norm = (numeric_ref[field] - lo) / span
            got = inputs.get(field)
            diff = (float(got) - lo) / span - ref_norm if _is_number(got) else 1.0
            acc += weight_of[field] * diff * diff
        distance = math.sqrt(acc)
        if max_distance > 0:
            closeness = 100.0 * (1.0 - distance / max_distance)
            closeness = max(0.0, min(100.0, closeness))
        else:
            closeness = 100.0
        ranked.append(SimilarityRanked(job=job, distance=distance, closeness_pct=closeness))

    ranked.sort(key=lambda r: (r.distance, -int(r.job.id)))
    return ranked[: max(1, int(top_n))]


@dataclass(frozen=True)
class JobService:
    """CRUD for jobs.

    Phase 1 only handles the metadata side of jobs — creation,
    listing, and state transitions driven by an external runner.
    The actual execution backend lives in :mod:`matofx.tools` (next
    on the Phase 1 roadmap), which is the only sanctioned caller of
    :meth:`_transition_state`.
    """

    audit: AuditWriter
    environment: str | None = None
    """The deployment environment (``"dev"`` / ``"test"`` / ``"staging"``
    / ``"prod"``). Threaded into the per-job working directory path so
    runs from different environments living under the same shared mount
    don't collide and operators can tell at a glance which env produced
    a given on-disk job folder. ``None`` keeps the legacy
    ``jobs/job_<id>/`` layout for tests / callers that haven't been
    wired up yet."""

    async def create(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        scenario_id: int,
        name: str,
        tool_alias: str,
        inputs: dict[str, Any] | None = None,
    ) -> Job:
        """Create a job in ``scenario_id`` in state ``"created"``.

        EDIT on the parent project required. Raises
        :class:`NotFoundError` if ``tool_alias`` doesn't refer to an
        installed tool (the FK on ``tool.alias`` enforces this at the
        DB layer). Raises :class:`DuplicateToolInScenarioError` if the
        scenario already has a job for this tool — the workspace
        catches this and redirects to the existing job (per the
        "one tool per scenario" rule locked 2026-05-08).
        """
        scenario = await session.get(Scenario, scenario_id)
        if scenario is None:
            raise NotFoundError("scenario", scenario_id)
        await _require_project_access(
            session,
            project_id=scenario.project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.EDIT,
            resource_type="scenario",
            resource_id=scenario_id,
        )

        # Pin the tool's *current* version onto the new job so a later
        # metadata edit / bundle replace can't retroactively change
        # what this job runs against. The runner reads the captured
        # tool_version's bundle_path at execution time. Lazy-imported
        # to avoid a circular tools↔hierarchy dependency at module
        # load.
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        cleaned_alias = tool_alias.strip()
        version_stmt = (
            select(ToolVersion.id, ToolVersion.descriptor_snapshot)
            .where(ToolVersion.alias == cleaned_alias)
            .order_by(ToolVersion.created_at.desc())
            .limit(1)
        )
        version_row = (await session.execute(version_stmt)).first()
        if version_row is None:
            raise NotFoundError("tool", tool_alias)
        tool_version_id, descriptor_snapshot = version_row
        # A synthesised surrogate-tool pins the fitted model version in its
        # descriptor (ADR-0008 §7.4); carry that onto the job so lineage
        # can draw the surrogate edge. Hand-authored tools leave it unset.
        surrogate_version_id = None
        if isinstance(descriptor_snapshot, dict):
            surrogate_version_id = descriptor_snapshot.get("surrogate_model_version_id")

        # Pre-flight uniqueness check so the friendly error carries the
        # existing job's id; the workspace uses that id to redirect
        # the user to the existing run rather than dead-end on the
        # error. The DB-level unique constraint (added in 0017) is
        # still the authoritative gate — the IntegrityError catch
        # below covers the rare TOCTOU race where two concurrent
        # creates slip past this check.
        existing_stmt = select(Job.id).where(
            Job.scenario_id == scenario_id, Job.tool_alias == cleaned_alias
        )
        existing_id = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing_id is not None:
            raise DuplicateToolInScenarioError(cleaned_alias, existing_job_id=int(existing_id))

        job = Job(
            scenario_id=scenario_id,
            name=name.strip(),
            tool_alias=cleaned_alias,
            tool_version_id=int(tool_version_id),
            state="created",
            inputs=inputs or {},
            created_by_user_id=actor_user_id,
            surrogate_model_version_id=(
                int(surrogate_version_id) if surrogate_version_id is not None else None
            ),
        )
        session.add(job)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            # Two paths into the IntegrityError: the FK on tool_alias
            # (caller passed an unknown tool — translate to NotFound)
            # or the unique constraint on (scenario_id, tool_alias)
            # (concurrent submission won the race — translate to
            # DuplicateToolInScenarioError without the id, since we
            # rolled back the lookup).
            constraint_text = str(exc.orig).lower() if exc.orig is not None else str(exc).lower()
            if "uq_job_scenario_tool" in constraint_text:
                raise DuplicateToolInScenarioError(cleaned_alias) from exc
            raise NotFoundError("tool", tool_alias) from exc

        # Auto-populate a structured default name when the caller passed
        # the placeholder. The job-id isn't known until after the flush,
        # so we patch it here rather than at row-construction time. The
        # user can still rename the draft via the workspace before
        # submitting — this only seeds the field so they're not staring
        # at "Untitled run" by default.
        if job.name in _PLACEHOLDER_JOB_NAMES:
            patched = await _auto_job_name(
                session,
                scenario=scenario,
                tool_alias=cleaned_alias,
                job_id=job.id,
            )
            if patched is not None:
                job.name = patched
                # Re-flush so the friendly name is persisted before the
                # endpoint returns — the workspace renders the new draft
                # row immediately after this call and expects the name
                # field to be settled. Name uniqueness isn't enforced at
                # the DB level so there's no realistic IntegrityError path
                # here; if the flush blows up for another reason we keep
                # the placeholder rather than failing the whole create.
                with contextlib.suppress(IntegrityError):
                    await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="hierarchy.job.create",
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                payload={
                    "scenario_id": scenario_id,
                    "name": job.name,
                    "tool_alias": job.tool_alias,
                },
            ),
        )
        return job

    async def get(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        actor_user_id: int,
    ) -> Job:
        """Fetch one job; VIEW required on the project."""
        job = await session.get(Job, job_id)
        if job is None:
            raise NotFoundError("job", job_id)
        project_id = await _job_project_id(session, job)
        await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.VIEW,
            resource_type="job",
            resource_id=job_id,
        )
        return job

    async def ensure_outputs_accessible(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        actor_user_id: int,
        actor_username: str,
        actor_is_approver: bool,
        actor_is_admin: bool,
    ) -> Job:
        """VIEW-gate a job, then enforce the post-approval output lockdown.

        The single chokepoint every output surface routes through
        (§16.15).

        - **Normal state** → plain VIEW gate on the project (same as
          :meth:`get`); a user with no access gets
          :class:`NotFoundError` (existence-hidden).
        - **``pending_results`` / ``results_withheld``** → outputs are
          visible only to admins (``actor_is_admin``) and **eligible**
          approvers (a current ``actor_is_approver`` whose username is
          in the ``post`` request's snapshotted
          ``eligible_approvers``). Their authority overrides the
          project ACL — an approver must be able to review the
          outputs to decide release even without project membership
          (mirrors the pre-gate's project-EDIT bypass).
        - Anyone else who *would* normally see the job (project VIEW,
          including the original requestor) gets
          :class:`ResultsWithheldError`; anyone with no access at all
          still gets :class:`NotFoundError`, so the gate never leaks
          the existence of a job to someone who couldn't otherwise
          see it.
        """
        job = await session.get(Job, job_id)
        if job is None:
            raise NotFoundError("job", job_id)
        project_id = await _job_project_id(session, job)

        if job.state not in ("pending_results", "results_withheld"):
            await _require_project_access(
                session,
                project_id=project_id,
                actor_user_id=actor_user_id,
                required=ProjectAccessLevel.VIEW,
                resource_type="job",
                resource_id=job_id,
            )
            return job

        # Withheld / pending: approver-or-admin authority overrides the
        # project ACL.
        if actor_is_admin:
            return job
        if actor_is_approver:
            from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

            stmt = (
                select(ApprovalRequest)
                .where(ApprovalRequest.job_id == job.id)
                .where(ApprovalRequest.kind == "post")
                .order_by(ApprovalRequest.id.desc())
            )
            req = (await session.execute(stmt)).scalars().first()
            if req is not None and actor_username.strip().lower() in (req.eligible_approvers or []):
                return job

        # Not approver/admin. Existence-hide from anyone with no
        # access (→ NotFoundError); tell those who would normally see
        # the job that the results are gated (→ ResultsWithheldError).
        await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.VIEW,
            resource_type="job",
            resource_id=job_id,
        )
        raise ResultsWithheldError(job.state)

    async def get_output_document(
        self,
        session: AsyncSession,
        *,
        job_id: int,
    ) -> OutputsDocument:
        """Return the parsed ``outputs/outputs.json`` for a finished job.

        Authorisation is owned by the caller's
        :meth:`ensure_outputs_accessible` chokepoint — the only path
        that reaches this method (§16.15). It deliberately does **not**
        re-run the project-VIEW ACL: ``self.get()`` is a *stricter* gate
        unaware of the approver/admin lockdown override, so it 404'd an
        eligible non-member approver out of the document they must
        review to decide release. The sibling surfaces
        (``list_job_outputs``/``get_job_output``/zips) already trust the
        chokepoint and don't re-gate; this matches them.

        Serves the document for any state in which the run has produced
        outputs (``complete``/``failed``/``pending_results``/
        ``results_withheld`` — the last two only ever follow a
        successful run). :class:`NotFoundError` when the job is missing
        or hasn't produced outputs yet; a missing/unreadable document on
        an outputs-bearing job resolves to an empty
        :class:`OutputsDocument` so Compare can still render the row.
        """
        from matofx.tools.exec.contracts import (  # noqa: PLC0415 — avoid tools↔hierarchy import cycle
            OUTPUTS_DOCUMENT_SCHEMA_VERSION,
            OutputsDocument,
        )
        from matofx.tools.exec_orm import StorageLocation  # noqa: PLC0415

        # Authorisation already enforced by the caller's
        # ensure_outputs_accessible chokepoint — fetch the row directly
        # rather than via self.get() (which would re-VIEW-gate and
        # exclude the non-member approver the chokepoint just admitted).
        job = await session.get(Job, job_id)
        if job is None or job.state not in {
            "complete",
            "failed",
            "pending_results",
            "results_withheld",
        }:
            raise NotFoundError("output_document", job_id)

        empty = OutputsDocument(
            schema_version=OUTPUTS_DOCUMENT_SCHEMA_VERSION,
            tool_alias=job.tool_alias,
            outputs=[],
        )
        if job.storage_location_id is None or job.working_dir_relative is None:
            return empty
        location = await session.get(StorageLocation, job.storage_location_id)
        if location is None:
            return empty
        return await _read_output_document(
            mount_path=location.mount_path,
            working_dir_relative=job.working_dir_relative,
            tool_alias=job.tool_alias,
        )

    async def list_for_scenario(
        self,
        session: AsyncSession,
        *,
        scenario_id: int,
        actor_user_id: int,
    ) -> list[Job]:
        """List jobs in ``scenario_id``; VIEW required on the project."""
        scenario = await session.get(Scenario, scenario_id)
        if scenario is None:
            raise NotFoundError("scenario", scenario_id)
        await _require_project_access(
            session,
            project_id=scenario.project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.VIEW,
            resource_type="scenario",
            resource_id=scenario_id,
        )
        stmt = select(Job).where(Job.scenario_id == scenario_id).order_by(Job.created_at.desc())
        return list((await session.execute(stmt)).scalars())

    async def list_visible_to_user(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        query: str | None = None,
        tool_alias: str | None = None,
        state: str | None = None,
        mine: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Job]:
        """List every job the user can see across every accessible project.

        Single-query cross-project rollup used by the My Jobs
        workspace's global Job-ID search picker (search-as-you-type),
        the empty-state recent-activity panel, and the Compare Select
        tab's job picker.

        Access ladder mirrors :meth:`ProjectService.list_for_user`:
        owners + team-members see their projects; ``projects.see_all``
        permission opens the whole install.

        Optional filters:

        - ``query`` matches case-insensitively against ``Job.name``.
          Project / scenario / tool-name matches are out of scope here
          — the API layer joins those for the response shape but the
          DB filter stays narrow so the query plan stays efficient.
        - ``tool_alias`` exact-matches; lets the workspace's
          "previous-job" popover restrict to runs of the same tool.
        - ``state`` exact-matches against the closed-vocabulary
          ``Job.state`` ("created" / "queued" / "running" / "complete"
          / "failed" / "cancelled").
        - ``mine`` restricts to jobs the actor *submitted*. Layers on
          top of the visibility ladder above (so a user without
          ``projects.see_all`` who toggles ``mine`` sees the
          intersection: jobs they submitted in projects they can see).

        Newest first by ``created_at``. ``limit`` is hard-capped at
        500 to keep the picker payload reasonable; clients paginate
        with ``offset`` if more is genuinely needed.
        """
        capped_limit = max(1, min(int(limit), 500))
        capped_offset = max(0, int(offset))

        stmt = (
            select(Job)
            .join(Scenario, Scenario.id == Job.scenario_id)
            .join(Project, Project.id == Scenario.project_id)
            .order_by(Job.created_at.desc())
            .limit(capped_limit)
            .offset(capped_offset)
        )

        if not await user_has_permission(session, actor_user_id, Permission.PROJECTS_SEE_ALL):
            shared = select(ProjectAcl.project_id).where(ProjectAcl.user_id == actor_user_id)
            stmt = stmt.where((Project.creator_user_id == actor_user_id) | Project.id.in_(shared))

        if query:
            stmt = stmt.where(Job.name.ilike(f"%{query.strip()}%"))
        if tool_alias:
            stmt = stmt.where(Job.tool_alias == tool_alias)
        if state:
            stmt = stmt.where(Job.state == state)
        if mine:
            stmt = stmt.where(Job.created_by_user_id == actor_user_id)

        return list((await session.execute(stmt)).scalars())

    async def search_by_inputs(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        tool_alias: str,
        equality_filters: dict[str, Any] | None = None,
        numeric_filters: dict[str, float] | None = None,
        numeric_tolerance_pct: float = 25.0,
        limit: int = 500,
    ) -> list[Job]:
        """Find completed runs of ``tool_alias`` whose inputs match the filters.

        Powers the v2 ``/job-search`` page (port of legacy
        ``functions/job_search.py``). The ACL-correct visible-jobs
        rollup is reused from :meth:`list_visible_to_user` so this
        method stays small: pull the candidate set, then post-filter
        in Python on each ``Job.inputs`` dict.

        Filter semantics (mirrors legacy strict-equality with numeric
        tolerance):

        - ``equality_filters`` — for each ``(field, value)``, the job
          matches only if ``Job.inputs.get(field) == value``. Use this
          for list / select / multiselect / file / string fields. The
          API layer is the type-aware boundary that splits filters
          between this bucket and :param:`numeric_filters`.
        - ``numeric_filters`` — for each ``(field, value)``, the job
          matches only if ``low <= Job.inputs.get(field) <= high``
          where ``low = value * (1 - tol/100)`` and
          ``high = value * (1 + tol/100)``. Non-numeric job values are
          treated as no-match. ``value == 0`` collapses the band to
          exact zero — documented edge, matches legacy behaviour.
        - Empty / ``None`` / empty-list filter values are skipped at
          the API layer; this method assumes its filter dicts already
          carry only the constraints to apply.

        Always pins ``state == "complete"``: the page is for
        comparing finished runs, so partial / failed jobs are out of
        scope. ``limit`` caps the candidate set fetched from
        :meth:`list_visible_to_user`; the post-filter pass is O(n) so
        a higher ceiling here costs less than another network round
        trip.
        """
        candidates = await self.list_visible_to_user(
            session,
            actor_user_id=actor_user_id,
            tool_alias=tool_alias,
            state="complete",
            limit=limit,
        )
        if not (equality_filters or numeric_filters):
            return candidates

        eq = dict(equality_filters or {})
        nm = dict(numeric_filters or {})
        tol = float(numeric_tolerance_pct) / 100.0

        out: list[Job] = []
        for job in candidates:
            inputs = job.inputs or {}
            if not all(inputs.get(field) == value for field, value in eq.items()):
                continue
            ok = True
            for field, target in nm.items():
                got = inputs.get(field)
                if not isinstance(got, (int, float)) or isinstance(got, bool):
                    ok = False
                    break
                target_f = float(target)
                low = target_f * (1.0 - tol)
                high = target_f * (1.0 + tol)
                if low > high:
                    low, high = high, low
                if not (low <= float(got) <= high):
                    ok = False
                    break
            if ok:
                out.append(job)
        return out

    async def find_similar_by_inputs(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        tool_alias: str,
        reference_inputs: dict[str, Any],
        numeric_fields: set[str],
        weights: dict[str, float] | None = None,
        top_n: int = 25,
        exclude_job_id: int | None = None,
        limit: int = 500,
    ) -> list[SimilarityRanked]:
        """Rank completed runs of ``tool_alias`` by closeness to a reference.

        The "find similar runs" extension to Job Search (§16.5). Reuses
        the same ACL-correct, ``state == "complete"`` candidate set as
        :meth:`search_by_inputs`, then hands off to the pure
        :func:`rank_jobs_by_similarity` for the weighted-Euclidean
        ranking. The API layer is the type-aware boundary: it supplies
        ``numeric_fields`` (descriptor ``integer`` / ``float`` inputs)
        and resolves ``weights`` (a study's standardised regression
        coefficients, or ``None`` for uniform) before calling.
        """
        candidates = await self.list_visible_to_user(
            session,
            actor_user_id=actor_user_id,
            tool_alias=tool_alias,
            state="complete",
            limit=limit,
        )
        return rank_jobs_by_similarity(
            candidates,
            reference_inputs=reference_inputs,
            numeric_fields=numeric_fields,
            weights=weights,
            top_n=top_n,
            exclude_job_id=exclude_job_id,
        )

    async def list_file_input_history(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        tool_alias: str,
        field: str,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Enriched distinct-values rollup for a file input.

        Same ACL-filtered candidate set as
        :meth:`list_distinct_input_values`, but each surviving value
        carries the originating job's id + project name + scenario
        name so the workspace's file-input picker can label each
        choice with ``"<filename>  ·  Job 0001234 (Project, Scenario)"``
        rather than a bare path. Most-recent-first observation; the
        first job a value was seen on supplies the metadata.

        Returned dicts shape::

            {
                "value": "<the path string>",
                "job_id": <int>,
                "project_name": "<str>",
                "scenario_name": "<str>",
            }
        """
        candidates = await self.list_visible_to_user(
            session,
            actor_user_id=actor_user_id,
            tool_alias=tool_alias,
            limit=limit,
        )
        if not candidates:
            return []

        scenario_ids = {j.scenario_id for j in candidates}
        scenarios_by_id: dict[int, Scenario] = {
            s.id: s
            for s in (
                await session.execute(select(Scenario).where(Scenario.id.in_(scenario_ids)))
            ).scalars()
        }
        project_ids = {s.project_id for s in scenarios_by_id.values()}
        projects_by_id: dict[int, Project] = {
            p.id: p
            for p in (
                await session.execute(select(Project).where(Project.id.in_(project_ids)))
            ).scalars()
        }

        out: list[dict[str, Any]] = []
        seen_values: set[str] = set()
        for job in candidates:
            value = (job.inputs or {}).get(field)
            if not isinstance(value, str) or not value:
                continue
            if value in seen_values:
                continue
            seen_values.add(value)
            if job.scenario_id is None:
                # Parametric-study children (scenario_id NULL) aren't part
                # of this scenario-scoped input-history surface (design §9).
                continue
            scenario = scenarios_by_id.get(job.scenario_id)
            project = projects_by_id.get(scenario.project_id) if scenario is not None else None
            out.append(
                {
                    "value": value,
                    "job_id": int(job.id),
                    "project_name": project.name if project is not None else "",
                    "scenario_name": scenario.name if scenario is not None else "",
                }
            )
        return out

    async def _completed_visible_jobs_for_versions(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        version_ids: set[int],
        limit: int,
    ) -> list[Job]:
        """Completed jobs of the given tool versions the actor can see.

        The query-level filter behind :meth:`list_file_output_candidates`:
        restricts to ``Job.tool_version_id IN version_ids`` so only jobs that
        could carry a matching file output are fetched (no blanket recent-jobs
        scan). ACL ladder mirrors :meth:`list_visible_to_user` (owner /
        team-member / ``projects.see_all``); inner-joins Scenario so
        parametric-study children are excluded. Newest first.
        """
        if not version_ids:
            return []
        capped = max(1, min(int(limit), 1000))
        stmt = (
            select(Job)
            .join(Scenario, Scenario.id == Job.scenario_id)
            .join(Project, Project.id == Scenario.project_id)
            .where(Job.state == "complete")
            .where(Job.tool_version_id.in_(version_ids))
            .order_by(Job.created_at.desc())
            .limit(capped)
        )
        if not await user_has_permission(session, actor_user_id, Permission.PROJECTS_SEE_ALL):
            shared = select(ProjectAcl.project_id).where(ProjectAcl.user_id == actor_user_id)
            stmt = stmt.where((Project.creator_user_id == actor_user_id) | Project.id.in_(shared))
        return list((await session.execute(stmt)).scalars())

    async def list_file_output_candidates(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        accepted_file_types: list[str] | None = None,
        expected_field_array: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Visible completed jobs' **file outputs**, for the cross-tool picker.

        Powers the file-input widget's "use a file produced by another job"
        mode: a standalone run of one tool (e.g. Bracket Stress) can consume a
        *different* tool's result file (e.g. Thermal Soak's temperature field)
        without the user re-typing a mount path. Complements
        :meth:`list_file_input_history` (same-tool *input* reuse) — this one
        surfaces *output* artefacts across every tool the user can see.

        **Relevant jobs are selected by a query filter, not by post-scanning a
        slice of recent jobs.** We first walk the (small) set of tool-version
        descriptors to find the versions that even *declare* a matching file
        output (``accepted_file_types`` suffix + ``expected_field_array`` nodal
        array), then query only completed, visible jobs of those versions. So a
        relevant job is never missed because unrelated newer jobs crowd it out.
        Each surviving output is resolved to its on-disk path
        ``<mount>/<working_dir_relative>/outputs/<filename>`` and kept only if
        the file exists.

        Returned dicts (flat; the UI groups tool → job → output)::

            {
                "value": "<absolute path>", "filename": "<leaf>",
                "file_type": "<mesh|field|data|image|...>", "field_array": "...",
                "output_name": "...", "output_label": "...",
                "tool_alias": "...", "tool_name": "...",
                "job_id": <int>, "project_name": "...", "scenario_name": "...",
            }

        Newest-job-first; ``limit`` bounds the number of *matching* jobs. ACL
        mirrors :meth:`list_visible_to_user` (inner-joins Scenario, so
        parametric-study children are excluded).
        """
        from matofx.tools.exec_orm import StorageLocation  # noqa: PLC0415
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        accepted_exts = {
            ext.lower().lstrip(".")
            for ext in (accepted_file_types or [])
            if isinstance(ext, str) and ext.strip()
        }

        # 1. Which tool versions declare a file output matching this input?
        #    A cheap descriptor scan (few rows) — not a job scan.
        descriptors_by_version: dict[int, Mapping[str, Any]] = {}
        rows = await session.execute(select(ToolVersion.id, ToolVersion.descriptor_snapshot))
        for vid, snap in rows.all():
            if isinstance(snap, Mapping) and _job_file_output_specs(
                snap, accepted_exts, expected_field_array
            ):
                descriptors_by_version[int(vid)] = snap
        if not descriptors_by_version:
            return []

        # 2. Completed, visible jobs of exactly those versions (the filter).
        candidates = await self._completed_visible_jobs_for_versions(
            session,
            actor_user_id=actor_user_id,
            version_ids=set(descriptors_by_version),
            limit=limit,
        )
        if not candidates:
            return []

        location_ids = {
            j.storage_location_id for j in candidates if j.storage_location_id is not None
        }
        mounts_by_location: dict[int, str] = {}
        if location_ids:
            for loc in (
                await session.execute(
                    select(StorageLocation).where(StorageLocation.id.in_(location_ids))
                )
            ).scalars():
                if loc.mount_path:
                    mounts_by_location[int(loc.id)] = loc.mount_path

        scenario_ids = {j.scenario_id for j in candidates if j.scenario_id is not None}
        scenarios_by_id: dict[int, Scenario] = {
            s.id: s
            for s in (
                await session.execute(select(Scenario).where(Scenario.id.in_(scenario_ids)))
            ).scalars()
        }
        project_ids = {s.project_id for s in scenarios_by_id.values()}
        projects_by_id: dict[int, Project] = {
            p.id: p
            for p in (
                await session.execute(select(Project).where(Project.id.in_(project_ids)))
            ).scalars()
        }

        work, specs_by_job = _collect_output_resolution_work(
            candidates,
            descriptors_by_version,
            mounts_by_location,
            accepted_exts,
            expected_field_array,
        )
        if not work:
            return []

        import asyncio  # noqa: PLC0415

        resolved = await asyncio.to_thread(_resolve_existing_output_files, work)
        return _assemble_output_candidate_rows(
            candidates,
            resolved=resolved,
            descriptors_by_version=descriptors_by_version,
            specs_by_job=specs_by_job,
            scenarios_by_id=scenarios_by_id,
            projects_by_id=projects_by_id,
        )

    async def list_distinct_input_values(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        tool_alias: str,
        field: str,
        limit: int = 500,
    ) -> list[Any]:
        """Distinct values seen in ``Job.inputs[field]`` across visible runs.

        Built for the file-input dropdown on ``/job-search``: when a
        descriptor declares an input as ``file``, we surface every
        distinct value the user has previously submitted for that
        field (typically a path on the shared mount), so the user
        picks from history rather than re-typing the path. ACL is
        inherited from :meth:`list_visible_to_user`.

        Order is "most-recent-first observation"; duplicates collapse.
        Skips entries where ``Job.inputs[field]`` is ``None`` or
        missing — they're not useful as filter options.
        """
        candidates = await self.list_visible_to_user(
            session,
            actor_user_id=actor_user_id,
            tool_alias=tool_alias,
            limit=limit,
        )
        seen: list[Any] = []
        seen_set: set[Any] = set()
        for job in candidates:
            value = (job.inputs or {}).get(field)
            if value is None or value == "":
                continue
            try:
                key = value if isinstance(value, (str, int, float, bool)) else repr(value)
            except Exception:
                key = repr(value)
            if key in seen_set:
                continue
            seen_set.add(key)
            seen.append(value)
        return seen

    async def update_draft(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        actor_user_id: int,
        inputs: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> Job:
        """Save typed values on a draft job without queueing it for the runner.

        Powers the workspace's "Save Inputs" button — the user clicks
        it to persist the form to the DB so a later session can resume
        the draft with the same values. Idempotent: re-saving doesn't
        change the row's state. Refuses to touch a non-draft job
        (raises :class:`JobNotQueueableError` with the current state so
        the UI can surface "this job has already been submitted").

        Strictly DB-only — the working directory is NOT staged here.
        Staging happens at :meth:`queue_for_runner` time so the
        launcher / inputs.json / static files only land on disk when
        the user actually wants to run the tool. That keeps the shared
        mount free of half-completed scratch jobs and means a user can
        update a draft cheaply without ever touching the file system.
        """
        # 1. Job + access (same gate as queue_for_runner — saving the
        #    form is a privileged action because the inputs may contain
        #    file paths or other sensitive data).
        job = await self.get(session, job_id=job_id, actor_user_id=actor_user_id)
        project_id = await _job_project_id(session, job)
        await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.EDIT,
            resource_type="job",
            resource_id=job_id,
        )

        # 2. State — only drafts can be edited via this path.
        if job.state != "created":
            raise JobNotQueueableError(job.state)

        # 3. Apply the patch. ``inputs`` of ``None`` means "don't
        #    touch them"; an empty dict means "save an empty form"
        #    (a legitimate state right after the user clicks the
        #    Save Inputs button on an unfilled form). ``name``
        #    follows the same convention.
        if inputs is not None:
            job.inputs = dict(inputs)
        if name is not None:
            cleaned = name.strip()
            if cleaned:
                job.name = cleaned

        try:
            await session.flush()
        except StaleDataError as exc:
            raise ConcurrentModificationError("job") from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="hierarchy.job.draft_update",
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                payload={
                    "name": job.name,
                    "input_keys": sorted((inputs or {}).keys()),
                },
            ),
        )
        return job

    async def request_cancel(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        actor_user_id: int,
        bypass_access: bool = False,
    ) -> Job:
        """Mark a job for cancellation; the runner picks it up on its next tick.

        Access ladder:

        - The project's owner / a user with EDIT access via team grant
          / a user with :attr:`Permission.PROJECTS_SEE_ALL` can cancel
          any job in that project.
        - A user with :attr:`Permission.JOBS_CANCEL_ANY` (super-user
          sentinel only) can cancel any job in the install, even ones
          they otherwise couldn't see.
        - ``bypass_access=True`` skips the ladder entirely — used by the
          Live Jobs bulk-cancel surface, which has already gated on
          :attr:`Permission.LIVE_JOBS_CANCEL` before calling.

        Refusals:

        - :class:`NotFoundError` — job doesn't exist OR the actor's
          effective project access is ``NONE`` (existence-hidden) AND
          they don't hold ``jobs.cancel_any``.
        - :class:`AccessDeniedError` — actor has VIEW (or below) on
          the project but not EDIT or higher.
        - :class:`JobAlreadyTerminalError` — job is already
          ``complete`` or ``failed``; nothing to cancel.

        Idempotency: calling ``request_cancel`` on a job that already
        has ``cancel_requested_at`` set is a no-op — the existing
        timestamp / requester is preserved and the call returns the
        job unchanged. The runner's cancel loop is the source of
        truth for what happens next.
        """
        job = await session.get(Job, job_id)
        if job is None:
            raise NotFoundError("job", job_id)

        # Super-user override — bypasses the project-access ladder. The
        # Live Jobs surface passes ``bypass_access=True`` after gating on
        # ``live_jobs.cancel`` (the actor may hold no project ACL at all).
        has_cancel_any = await user_has_permission(
            session, actor_user_id, Permission.JOBS_CANCEL_ANY
        )

        if not (bypass_access or has_cancel_any):
            project_id = await _job_project_id(session, job)
            await _require_project_access(
                session,
                project_id=project_id,
                actor_user_id=actor_user_id,
                required=ProjectAccessLevel.EDIT,
                resource_type="job",
                resource_id=job_id,
            )

        if job.state in {"complete", "failed"}:
            raise JobAlreadyTerminalError(job.state)

        if job.cancel_requested_at is not None:
            # Already requested — keep the original timestamp /
            # requester so the audit trail stays accurate.
            return job

        now = _utcnow()
        job.cancel_requested_at = now
        job.cancel_requested_by_user_id = actor_user_id
        try:
            await session.flush()
        except StaleDataError as exc:
            raise ConcurrentModificationError("job") from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="hierarchy.job.cancel_requested",
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                payload={
                    "previous_state": job.state,
                    "via_cancel_any": has_cancel_any,
                },
            ),
        )
        return job

    async def queue_or_hold_for_approval(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        actor_user_id: int,
        inputs: dict[str, Any],
        priority: bool = False,
        justification: str = "",
    ) -> Job:
        """The approvals gate that sits above :meth:`queue_for_runner`.

        Reads ``approvals_policy`` from the job's *captured*
        ``tool_version`` snapshot (pinned at create time — a later
        policy edit can't change a job already in flight):

        - ``pre`` / ``both`` → the job is held: the form snapshot is
          persisted, the state moves to ``pending_approval``, and a
          ``pre`` :class:`~matofx.approvals.ApprovalRequest` is opened
          for the tool's listed approvers. **No working directory is
          staged** — the hold is DB-only; staging happens on approval
          when :meth:`queue_for_runner` runs.
        - ``none`` / ``post`` → delegates straight to
          :meth:`queue_for_runner` (the ``post`` gate triggers when
          the run finishes, not at submit).

        The submitter must hold EDIT on the project to submit at all;
        only the later approval-triggered queue skips that recheck.
        """
        from matofx.approvals import make_approval_service  # noqa: PLC0415
        from matofx.tools.descriptor import ToolDescriptor  # noqa: PLC0415
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        job = await self.get(session, job_id=job_id, actor_user_id=actor_user_id)
        project_id = await _job_project_id(session, job)
        await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.EDIT,
            resource_type="job",
            resource_id=job_id,
        )
        if job.state != "created":
            raise JobNotQueueableError(job.state)

        tool_version = await session.get(ToolVersion, job.tool_version_id)
        if tool_version is None:
            raise ToolNotRunnableError(
                job.tool_alias,
                "the captured tool_version snapshot is missing — investigate; "
                "snapshots are append-only and shouldn't be deleted",
            )
        descriptor = ToolDescriptor.model_validate(tool_version.descriptor_snapshot)

        if descriptor.approvals_policy not in ("pre", "both"):
            # none / post → normal submission. The post gate fires when
            # the run finishes (Phase 3), not at submit time.
            return await self.queue_for_runner(
                session,
                job_id=job_id,
                actor_user_id=actor_user_id,
                inputs=inputs,
                priority=priority,
            )

        # Pre-approval hold. DB-only: persist the exact form snapshot
        # the approver will review (and that queue_for_runner will run
        # verbatim on approval); do NOT stage a working directory.
        job.inputs = dict(inputs)
        if priority:
            job.priority = True
        previous = job.state
        job.state = "pending_approval"
        await session.flush()

        approval_service = make_approval_service(self.audit)
        request = await approval_service.create_request(
            session,
            job_id=job.id,
            kind="pre",
            requested_by_user_id=actor_user_id,
            eligible_approvers=list(descriptor.approver_usernames),
            tool_alias=job.tool_alias,
            tool_version_id=job.tool_version_id,
            justification=justification,
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="hierarchy.job.pending_approval",
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                payload={
                    "previous_state": previous,
                    "tool_alias": job.tool_alias,
                    "approvals_policy": descriptor.approvals_policy,
                    "approval_request_id": request.id,
                    "eligible_approvers": list(request.eligible_approvers),
                },
            ),
        )
        return job

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
    ) -> tuple[ApprovalRequest, Job]:
        """Apply an approver's decision and drive the job — pre **or** post.

        Records the decision first — delegating authorisation and the
        ``approver`` / ``self`` / ``admin_override`` classification to
        :meth:`ApprovalService.decide` — then applies the
        kind-appropriate side-effect:

        **pre** (gate before the run)

        - *approve* → the held job is queued via
          :meth:`queue_for_runner` with the project-EDIT recheck
          skipped (approver authority ≠ project access), audited
          ``via='approval'`` and attributed to the approver. The
          persisted input snapshot is exactly what runs.
        - *deny* → the job moves terminally to ``denied`` (it never ran).

        **post** (gate before results are released)

        - *approve* → the held outputs are released: the job moves from
          ``pending_results`` to ``complete`` (no re-run, no re-stage —
          the run already finished).
        - *deny* → the job moves terminally to ``results_withheld``;
          outputs stay on disk but are never released to the requestor.

        The mandatory deny reason (enforced by the approval service) is
        carried into the audit row in every deny path.
        """
        from matofx.approvals import make_approval_service  # noqa: PLC0415

        approval_service = make_approval_service(self.audit)
        request = await approval_service.decide(
            session,
            request_id=request_id,
            decider_user_id=decider_user_id,
            decider_username=decider_username,
            decision=decision,
            reason=reason,
            decider_is_approver=decider_is_approver,
            decider_is_admin=decider_is_admin,
        )

        job = await session.get(Job, request.job_id)
        if job is None:
            raise NotFoundError("job", request.job_id or 0)

        if request.kind == "pre":
            if request.status == "approved":
                # queue_for_runner's invariant is state == 'created'.
                # The hold left the job in 'pending_approval'; flip it
                # back so the unchanged stage+queue path runs. The
                # actor stays the original requestor (so the VIEW-gated
                # get() resolves); the EDIT recheck is skipped and the
                # audit credits the approver, tagged via='approval'.
                job.state = "created"
                await session.flush()
                queued = await self.queue_for_runner(
                    session,
                    job_id=job.id,
                    actor_user_id=request.requested_by_user_id,
                    inputs=dict(job.inputs or {}),
                    priority=bool(job.priority),
                    access_check=False,
                    audit_via="approval",
                    audit_actor_user_id=decider_user_id,
                )
                return request, queued

            previous = job.state
            job.state = "denied"
            await session.flush()
            await self.audit.record(
                session,
                RecordInput(
                    actor=f"user:{decider_user_id}",
                    actor_type="user",
                    action="hierarchy.job.denied",
                    resource_type="job",
                    resource_id=str(job.id),
                    outcome="success",
                    payload={
                        "previous_state": previous,
                        "tool_alias": job.tool_alias,
                        "approval_request_id": request.id,
                    },
                    reason=request.decision_reason,
                ),
            )
            return request, job

        # --- post: release or withhold the already-produced results ---
        previous = job.state
        if request.status == "approved":
            job.state = "complete"
            action = "hierarchy.job.results_released"
        else:
            job.state = "results_withheld"
            action = "hierarchy.job.results_withheld"
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{decider_user_id}",
                actor_type="user",
                action=action,
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                payload={
                    "previous_state": previous,
                    "tool_alias": job.tool_alias,
                    "approval_request_id": request.id,
                },
                reason=(request.decision_reason if request.status == "denied" else None),
            ),
        )
        return request, job

    async def queue_for_runner(  # noqa: PLR0912, PLR0915 — single tightly-coupled flow; splitting hurts readability
        self,
        session: AsyncSession,
        *,
        job_id: int,
        actor_user_id: int,
        inputs: dict[str, Any],
        priority: bool = False,
        access_check: bool = True,
        audit_via: str | None = None,
        audit_actor_user_id: int | None = None,
        wired_file_inputs: dict[str, list[str]] | None = None,
    ) -> Job:
        """Stage the working directory and move the job from ``created`` to ``queued``.

        Called from the API after :meth:`create`. Picks the active
        :class:`StorageLocation`, builds the v2 :class:`InputsDocument`
        from the supplied form values + the tool's descriptor, stages
        the bundle into ``<mount>/jobs/job_<id>/`` via
        :func:`stage_working_dir`, and persists the resulting
        ``storage_location_id`` / ``working_dir_relative`` / ``inputs``
        onto the job row. The runner's pickup loop takes it from there.

        ``access_check`` / ``audit_via`` / ``audit_actor_user_id`` exist
        for the approval path (§16.15): when an approver releases a
        held job, authority comes from being a listed approver, **not**
        from project access, so the EDIT recheck is skipped
        (``access_check=False``) while ``actor_user_id`` stays the
        original requestor (so the VIEW-gated :meth:`get` still
        resolves). The audit row is then attributed to the approver
        (``audit_actor_user_id``) and tagged ``via='approval'``.
        Defaults preserve the original behaviour for every existing
        caller.

        ``wired_file_inputs`` is the workflow file-wiring hand-off
        (ADR-0015): ``{field_name: [absolute upstream output paths]}`` for
        the file inputs the headless orchestrator
        (:meth:`WorkflowRunService.advance`) wired from a completed upstream
        member's file output. After the draft-upload promotion runs, those
        source files are copied into ``inputs/uploaded/<field>/`` and the
        field value is rewritten to the staged copy
        (:func:`stage_wired_file_inputs`). ``None`` for every non-workflow
        caller — the manual / standalone path is untouched.

        Refusals:

        - :class:`NotFoundError` — job missing or actor can't see it.
        - :class:`AccessDeniedError` — actor lacks EDIT on the project
          (only when ``access_check`` is true).
        - :class:`JobNotQueueableError` — job isn't in ``created`` state.
        - :class:`ToolNotRunnableError` — tool archived / wrong state /
          bundle missing on disk.
        - :class:`StorageLocationUnavailableError` — no active
          ``storage_location`` row to stage under.
        """
        # Lazy imports — keep module import-time graph small and avoid
        # the matofx.tools → matofx.hierarchy back-import.
        import asyncio  # noqa: PLC0415
        import sys  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        from matofx.runner.settings import (  # noqa: PLC0415
            DEFAULT_RUNTIME_SETTINGS,
            load_runtime_settings,
        )
        from matofx.tools.descriptor import ToolDescriptor  # noqa: PLC0415
        from matofx.tools.exec import (  # noqa: PLC0415
            InputsDocument,
            stage_working_dir,
        )
        from matofx.tools.exec_orm import PythonEnv, StorageLocation  # noqa: PLC0415
        from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415

        # 1. Job + access.
        job = await self.get(session, job_id=job_id, actor_user_id=actor_user_id)
        project_id = await _job_project_id(session, job)
        if access_check:
            await _require_project_access(
                session,
                project_id=project_id,
                actor_user_id=actor_user_id,
                required=ProjectAccessLevel.EDIT,
                resource_type="job",
                resource_id=job_id,
            )

        # 2. State.
        if job.state != "created":
            raise JobNotQueueableError(job.state)

        # 3. Tool — must exist, be released, not archived. The runtime
        # bundle path comes from the *captured* tool_version snapshot
        # (pinned at submit time) so a metadata edit / bundle replace
        # after submission can't change what this job runs against.
        tool = (
            await session.execute(select(Tool).where(Tool.alias == job.tool_alias))
        ).scalar_one_or_none()
        if tool is None:
            raise ToolNotRunnableError(job.tool_alias, "the tool is no longer installed")
        if tool.archived_at is not None:
            raise ToolNotRunnableError(job.tool_alias, "the tool has been archived")
        # A job may only be staged by someone who can *see* the tool —
        # the same canonical predicate the catalog and workflow surfaces
        # use, so "can't see it ⇒ can't run it" holds at the API layer,
        # not just in the UI. This covers both the state gate (pre-release
        # tools need the matching ``tools.see_*`` permission) and the team
        # gate (a team-restricted tool refuses non-members even when
        # ``released``). Archival is handled with its own message above,
        # so pass ``include_archived=True`` here to avoid masking it.
        from matofx.tools.service import can_user_see_tool  # noqa: PLC0415

        if not await can_user_see_tool(session, actor_user_id, tool, include_archived=True):
            raise ToolNotRunnableError(
                job.tool_alias,
                (
                    "you don't have access to this tool. It may be unreleased "
                    f"(state {tool.state!r}) or restricted to teams you're not a "
                    "member of. Ask an administrator to release it, add you to "
                    "the relevant team, or grant the appropriate tools.see_* "
                    "permission."
                ),
            )
        tool_version = await session.get(ToolVersion, job.tool_version_id)
        if tool_version is None:
            raise ToolNotRunnableError(
                job.tool_alias,
                "the captured tool_version snapshot is missing — investigate; "
                "snapshots are append-only and shouldn't be deleted",
            )
        if not Path(tool_version.bundle_path).is_dir():
            raise ToolNotRunnableError(
                job.tool_alias,
                "the captured snapshot's bundle directory is missing on disk",
            )

        # 3a. V&V policy gate (ADR-0011 §6.4). If the project's
        # ``vv_required_for_submit`` metadata is "yes", refuse to queue
        # a tool that isn't both verified AND validated (the badge
        # ``verified_validated``). Other selections — "no" / "not_set" /
        # absent — pass through silently. Lazy import keeps the
        # vv → hierarchy → vv loop one-way.
        from matofx.vv.policy import check_submit_policy  # noqa: PLC0415

        await check_submit_policy(
            session,
            project_id=project_id,
            tool_alias=job.tool_alias,
            tool_version=tool_version.version,
            tool_version_id=tool_version.id,
            audit=self.audit,
        )

        # 4. Storage location — must be exactly one active row in Phase 1.
        active_locations = list(
            (
                await session.execute(
                    select(StorageLocation)
                    .where(StorageLocation.is_active.is_(True))
                    .where(StorageLocation.retired_at.is_(None))
                    .order_by(StorageLocation.id.asc())
                )
            ).scalars()
        )
        if not active_locations:
            raise StorageLocationUnavailableError
        location = active_locations[0]

        # 5. Build the InputsDocument from the captured snapshot's
        # descriptor + supplied values. We deliberately read from the
        # *snapshot* not the live tool row — the snapshot is what was
        # pinned at submit time, so any subsequent edits to the tool
        # don't change what this job sees.
        descriptor = ToolDescriptor.model_validate(tool_version.descriptor_snapshot)

        # 5a. Resolve `material` inputs (ADR-0007 §3). Each material field's
        # selected material_version_id is frozen into a nested
        # {id, name, class, version, properties} block in inputs.json — the
        # tool reads frozen values, never the live catalog, so a re-run is
        # reproducible — and a job_material_use provenance row is written.
        # Mutating `inputs` here means the block flows through staging,
        # promotion, the on-disk rewrite, and the persisted Job.inputs.
        inputs = await self._resolve_material_inputs(session, job.id, descriptor, dict(inputs))

        # 5b. Resolve "Pick from library" file inputs (ADR-0006 §6). Each
        # file field whose draft value is a library reference has the
        # chosen version's bytes staged into the draft upload area so the
        # promotion step below copies them into
        # ``inputs/uploaded/<field>/<filename>`` like any other upload —
        # the runner contract is unchanged — and a job_library_file_use
        # provenance row is written.
        inputs = await self._resolve_library_inputs(
            session, job.id, actor_user_id, descriptor, inputs, mount_path=location.mount_path
        )

        # 5c. Workflow file-wiring (ADR-0015). When this is a workflow-member
        # job and the caller didn't pass an explicit hand-off (the manual
        # tab-by-tab run path — only the headless ``advance`` passes it), resolve
        # the member's file-wired inputs from its completed upstream siblings and
        # stage them. The per-tab UI seeds scalar wired values but can't seed a
        # file output (its ``value`` is ``None``), so without this the wired file
        # input arrives empty.
        if wired_file_inputs is None and job.workflow_run_id is not None:
            from matofx.workflows.run_service import (  # noqa: PLC0415 — avoid import cycle
                make_workflow_run_service,
            )

            wrs = make_workflow_run_service(self.audit, environment=self.environment)
            primaries, resolved_sources = await wrs.resolve_wired_files_for_member(session, job)
            for field_name, primary in primaries.items():
                if not inputs.get(field_name):  # never override a user-supplied value
                    inputs = {**inputs, field_name: primary}
            wired_file_inputs = resolved_sources or None

        inputs_payload = []
        for field in descriptor.inputs:
            value = inputs.get(field.name, field.default)
            inputs_payload.append(
                {
                    "name": field.name,
                    "label": field.label,
                    "type": field.type,
                    "unit": field.unit,
                    "value": value,
                }
            )
        inputs_document = InputsDocument(
            job_id=job.id,
            tool_alias=descriptor.alias,
            tool_version=descriptor.version,
            inputs=inputs_payload,  # type: ignore[arg-type]  # pydantic coerces dicts
        )

        # 6. Stage. Pick up cancel_grace_s from the live runtime settings
        # so the launcher's grace matches what the runner / admin expect.
        # Keep submit working if the runtime_setting table is briefly
        # unreachable; the catalog defaults are safe.
        try:
            runtime = await load_runtime_settings(session)
        except Exception:
            runtime = DEFAULT_RUNTIME_SETTINGS

        # Bake the environment into the path so a single shared mount
        # serving dev / staging / prod doesn't have job folders from
        # three deployments interleaved at the top level. Operators
        # eyeballing the FS can tell at a glance which env produced
        # which run. Sanitised to alphanumeric + underscore so a
        # malformed Settings.environment can't escape the namespace.
        env_segment = _safe_env_segment(self.environment)
        if env_segment:
            working_dir_relative = f"jobs/{env_segment}/job_{job.id:07d}"
        else:
            working_dir_relative = f"jobs/job_{job.id:07d}"

        # Resolve the interpreter the launcher will invoke. A tool that
        # pins a PythonEnv (``tool.python_env_id``) runs under it; a tool
        # that pins nothing falls back to the MATOFX server's own
        # interpreter — the "main MATOFX venv". A pinned env's
        # ``interpreter_path`` is used verbatim when absolute, or resolved
        # against its storage location's ``mount_path`` when relative (the
        # "install the venv once on the shared drive" case, so it needn't
        # be present on every AOS / HPC compute node). A deactivated env
        # still resolves: deactivation only stops *new* tools binding to
        # it, it doesn't strand jobs for tools already bound. The FK is
        # ``ondelete=RESTRICT`` so a referenced row can't vanish, but we
        # fall back rather than crash if the registry is somehow missing.
        interpreter_path = sys.executable
        if tool.python_env_id is not None:
            env = await session.get(PythonEnv, tool.python_env_id)
            if env is not None:
                if env.storage_location_id is None:
                    interpreter_path = env.interpreter_path
                else:
                    env_location = await session.get(StorageLocation, env.storage_location_id)
                    interpreter_path = (
                        str(Path(env_location.mount_path) / env.interpreter_path)
                        if env_location is not None
                        else env.interpreter_path
                    )

        # A job reaching staging for the first time has no committed
        # ``working_dir_relative`` yet (it's persisted only after this
        # succeeds, below). So if a folder already sits at the deterministic
        # job path, it can only be an orphan from a reused job id or a
        # database rollback that put the id counter behind the filesystem —
        # never a live job's directory. Let staging move it aside and carry
        # on rather than hard-failing the submit. A re-stage of an
        # already-staged job (``working_dir_relative`` set) keeps the strict
        # refuse-to-overwrite guard.
        reclaim_orphaned = job.working_dir_relative is None

        def _stage_blocking() -> None:
            stage_working_dir(
                mount_path=location.mount_path,
                working_dir_relative=working_dir_relative,
                bundle_path=tool_version.bundle_path,
                inputs_document=inputs_document,
                interpreter_path=interpreter_path,
                main_script_relative=descriptor.main_script,
                cancel_grace_seconds=runtime.cancel_grace_s,
                reclaim_orphaned=reclaim_orphaned,
            )

        await asyncio.to_thread(_stage_blocking)

        # Promote any drag-drop / browse-uploaded files from the draft
        # staging area into the freshly-staged working directory. The
        # helper copies each upload to
        # ``inputs/uploaded/<field_name>/<filename>``, rewrites the
        # matching ``inputs[<field_name>]`` value to the new path, and
        # deletes the draft staging tree so the on-disk state matches
        # "this job is no longer a draft". Inputs that don't point at
        # a draft path (typed paths, previous-job picks) pass through
        # unchanged.
        import json  # noqa: PLC0415

        from matofx.tools.exec.uploads import (  # noqa: PLC0415
            promote_draft_uploads_into_working_dir,
        )

        working_dir_absolute = Path(location.mount_path) / working_dir_relative

        def _promote_blocking() -> dict[str, Any]:
            return promote_draft_uploads_into_working_dir(
                mount_path=location.mount_path,
                environment=self.environment,
                job_id=job.id,
                working_dir_absolute=working_dir_absolute,
                inputs=dict(inputs),
            )

        promoted_inputs = await asyncio.to_thread(_promote_blocking)

        # Workflow file-wiring (ADR-0015). Copy each file-wired input's
        # resolved upstream output(s) into ``inputs/uploaded/<field>/`` and
        # rewrite the field value to the staged copy. Runs after the draft
        # promotion (those paths live under the draft area, these under
        # another job's retained working dir) so both rewrites compose into
        # the final input map written to inputs.json below. A no-op for every
        # standalone / manual submit (``wired_file_inputs`` is None).
        if wired_file_inputs:
            from matofx.tools.exec.uploads import (  # noqa: PLC0415
                stage_wired_file_inputs,
            )

            def _stage_wired_blocking() -> dict[str, Any]:
                return stage_wired_file_inputs(
                    working_dir_absolute=working_dir_absolute,
                    inputs=dict(promoted_inputs),
                    wired_file_sources=wired_file_inputs,
                )

            promoted_inputs = await asyncio.to_thread(_stage_wired_blocking)

        # Rewrite the inputs.json on disk so the launcher's input
        # parser sees the post-promotion paths. ``stage_working_dir``
        # already wrote the pre-promotion version; we overwrite here
        # rather than restructuring the staging API to keep the change
        # surface tight. Future cleanup: thread the promoted paths
        # through staging so it writes the right values first time.
        if promoted_inputs != dict(inputs):
            inputs = promoted_inputs
            inputs_payload = []
            for field in descriptor.inputs:
                value = inputs.get(field.name, field.default)
                inputs_payload.append(
                    {
                        "name": field.name,
                        "label": field.label,
                        "type": field.type,
                        "unit": field.unit,
                        "value": value,
                    }
                )
            updated_inputs_document = InputsDocument(
                job_id=job.id,
                tool_alias=descriptor.alias,
                tool_version=descriptor.version,
                inputs=inputs_payload,  # type: ignore[arg-type]
            )

            def _rewrite_blocking() -> None:
                inputs_json_path = working_dir_absolute / "inputs" / "inputs.json"
                inputs_json_path.write_text(
                    json.dumps(updated_inputs_document.model_dump(mode="json"), indent=2),
                    encoding="utf-8",
                )

            await asyncio.to_thread(_rewrite_blocking)

        # 7. Persist the queued state.
        previous = job.state
        job.state = "queued"
        job.storage_location_id = location.id
        job.working_dir_relative = working_dir_relative
        job.inputs = dict(inputs)  # snapshot of the form values for audit / re-render
        # Skip-queue flag from the API. Permission was checked by the
        # router; the service trusts that gate and just persists the
        # value. Default False means the row joins the back of the
        # pickup queue, which is the right behaviour for the regular
        # Submit button.
        if priority:
            job.priority = True
        try:
            await session.flush()
        except StaleDataError as exc:
            raise ConcurrentModificationError("job") from exc

        audit_payload: dict[str, Any] = {
            "previous_state": previous,
            "tool_alias": job.tool_alias,
            "storage_location_id": location.id,
            "working_dir_relative": working_dir_relative,
        }
        if audit_via is not None:
            audit_payload["via"] = audit_via
        audit_actor = audit_actor_user_id if audit_actor_user_id is not None else actor_user_id
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{audit_actor}",
                actor_type="user",
                action="hierarchy.job.queue",
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                payload=audit_payload,
            ),
        )
        return job

    async def _resolve_material_inputs(
        self,
        session: AsyncSession,
        job_id: int,
        descriptor: ToolDescriptor,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Freeze ``material`` inputs into the value map + write provenance (ADR-0007 §3).

        For each ``material`` descriptor field whose value is a selected
        ``material_version_id``, expand it into the frozen
        ``{material_id, name, class, version_*, properties}`` block and
        write a ``job_material_use`` row. Returns ``inputs`` with those
        fields' values replaced by their frozen blocks; non-material
        inputs pass through unchanged. A re-queue / retry sees an
        already-frozen block (``Job.inputs`` kept the frozen value) — it
        is left untouched (reproducible) and its provenance re-recorded.
        """
        material_fields = [f for f in descriptor.inputs if f.type == "material"]
        if not material_fields:
            return inputs
        from matofx.materials import make_materials_service  # noqa: PLC0415

        service = make_materials_service(self.audit)
        await service.clear_job_uses(session, job_id=job_id)
        resolved = dict(inputs)
        for field in material_fields:
            selected = resolved.get(field.name)
            if selected is None:
                continue
            if isinstance(selected, dict):
                # Already a frozen block (re-queue / retry): keep it as-is
                # so the re-run is reproducible; just re-record provenance.
                version_id = selected.get("version_id")
                if isinstance(version_id, int):
                    await service.record_job_use(
                        session,
                        job_id=job_id,
                        field_name=field.name,
                        material_version_id=version_id,
                    )
                continue
            try:
                version_id = int(selected)
            except (TypeError, ValueError):
                continue
            resolved[field.name] = await service.resolve_for_submit(
                session,
                material_version_id=version_id,
                required_properties=field.required_properties,
            )
            await service.record_job_use(
                session, job_id=job_id, field_name=field.name, material_version_id=version_id
            )
        return resolved

    async def _resolve_library_inputs(
        self,
        session: AsyncSession,
        job_id: int,
        actor_user_id: int,
        descriptor: ToolDescriptor,
        inputs: dict[str, Any],
        *,
        mount_path: str,
    ) -> dict[str, Any]:
        """Promote "Pick from library" file inputs into the upload path (ADR-0006 §6).

        For each ``file`` descriptor field whose draft value is a library
        reference, fetch the chosen version's bytes (access-checked),
        stage them into the draft upload area so the existing promotion
        path copies them into ``inputs/uploaded/<field>/<filename>``
        unchanged, rewrite the field value to that staged path, and write
        a ``job_library_file_use`` provenance row. Non-library file
        inputs (typed paths, drag-drop uploads, previous-job picks) pass
        through untouched. A re-queue / retry sees an already-promoted
        path (not a library reference) and so is a no-op.
        """
        file_fields = [f for f in descriptor.inputs if f.type == "file"]
        if not file_fields:
            return inputs
        from matofx.library import make_library_service  # noqa: PLC0415
        from matofx.library.refs import parse_library_ref  # noqa: PLC0415
        from matofx.tools.exec.uploads import stage_draft_upload  # noqa: PLC0415

        service = make_library_service(self.audit, environment=self.environment)
        resolved = dict(inputs)
        for field in file_fields:
            version_id = parse_library_ref(resolved.get(field.name))
            if version_id is None:
                continue
            version, content = await service.open_version_bytes(
                session, actor_user_id=actor_user_id, version_id=version_id
            )
            stored = stage_draft_upload(
                mount_path=mount_path,
                environment=self.environment,
                job_id=job_id,
                field_name=field.name,
                filename=version.original_filename,
                content=content,
            )
            resolved[field.name] = str(stored.absolute_path)
            await service.record_job_use(
                session,
                job_id=job_id,
                field_name=field.name,
                library_file_version_id=version_id,
            )
        return resolved

    async def _transition_state(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        actor_user_id: int,
        new_state: str,
    ) -> Job:
        """Move a job to a new state. **Internal** — runner-only.

        The transition graph (created → queued → running →
        complete | failed) lands with the execution backend in
        :mod:`matofx.tools`. This method enforces the closed
        vocabulary and the timestamp side-effects only; the runner is
        responsible for ordering.
        """
        if new_state not in JOB_STATES:
            raise InvalidJobStateError(new_state)

        job = await self.get(session, job_id=job_id, actor_user_id=actor_user_id)
        previous = job.state
        job.state = new_state
        now = _utcnow()
        if new_state == "running" and job.started_at is None:
            job.started_at = now
        if new_state in {"complete", "failed"}:
            job.completed_at = now
        try:
            await session.flush()
        except StaleDataError as exc:
            raise ConcurrentModificationError("job") from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=f"hierarchy.job.state.{new_state}",
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                payload={"previous": previous, "new": new_state},
            ),
        )
        return job


# ---- factories -------------------------------------------------------------


def make_project_service(audit: AuditWriter) -> ProjectService:
    """Factory: build a :class:`ProjectService`."""
    return ProjectService(audit=audit)


def make_scenario_service(audit: AuditWriter) -> ScenarioService:
    """Factory: build a :class:`ScenarioService`."""
    return ScenarioService(audit=audit)


def make_job_service(audit: AuditWriter, *, environment: str | None = None) -> JobService:
    """Factory: build a :class:`JobService`.

    ``environment`` is threaded from :class:`matofx.config.Settings`
    so the staged working-dir path can include it (see
    :attr:`JobService.environment`). ``None`` falls back to the legacy
    flat layout — kept for tests / callers that haven't been updated.
    """
    return JobService(audit=audit, environment=environment)


def _safe_env_segment(environment: str | None) -> str:
    """Sanitise ``Settings.environment`` for use as a path segment.

    Keeps only the lowercase ASCII letters, digits, ``-`` and ``_`` from
    the input — anything else is dropped so a typo or hostile config
    can't escape the ``jobs/`` namespace. Returns an empty string when
    ``environment`` is ``None`` or empty after filtering, which makes
    the caller fall back to the env-less path layout.
    """
    if not environment:
        return ""
    return "".join(c for c in environment.lower() if c.isalnum() or c in "-_")
