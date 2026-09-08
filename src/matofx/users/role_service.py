"""Role management — custom roles + assignment.

Built-in roles are seeded by the migration and cannot be deleted or
renamed. ``super_user`` permissions are sentinel-managed and cannot be
edited at runtime. The other built-in roles (``administrator``,
``beta_tester``, ``regular_user``) and any custom roles can have their
permission sets adjusted by anyone holding ``roles.manage``.

Two install-wide invariants are enforced here:

1. The ``super_user`` role's permission set is immutable.
2. The install must always have at least one active user holding the
   ``super_user`` role. The last such user can't be deactivated and
   their role can't be downgraded.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from matofx.audit.schema import RecordInput
from matofx.users.errors import (
    CannotEditBuiltInRoleError,
    CannotEditSuperUserPermissionsError,
    DuplicateRoleError,
    LastSuperUserError,
    NotACapabilityRoleError,
    PermissionDeniedError,
    PermissionKindMismatchError,
    RoleDeletionForbiddenError,
    RoleNotFoundError,
    SuperUserRoleChangeRequiresSuperUserError,
    UnknownPermissionError,
)
from matofx.users.orm import User
from matofx.users.permissions import Permission, is_capability_permission, user_has_permission
from matofx.users.role_orm import Role, RolePermission, UserCapabilityRole

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


SUPER_USER_NAME = "super_user"
_BUILT_IN_NAMES = frozenset({SUPER_USER_NAME, "administrator", "beta_tester", "regular_user"})
ROLE_KINDS = ("base", "capability")


@dataclass(frozen=True)
class RoleService:
    """CRUD for roles and user→role assignment.

    Build once at startup (``make_role_service(audit)``); pass into
    the admin surface that lets a super-user / administrator manage
    the install's roles.
    """

    audit: AuditWriter

    # ---- read ----

    async def get_by_name(self, session: AsyncSession, name: str) -> Role:
        """Look up a role by name; raises if it doesn't exist."""
        role = (await session.execute(select(Role).where(Role.name == name))).scalar_one_or_none()
        if role is None:
            raise RoleNotFoundError(name)
        return role

    async def list_roles(self, session: AsyncSession) -> list[Role]:
        """Return every role in the install, ordered by name."""
        return list((await session.execute(select(Role).order_by(Role.name))).scalars())

    async def get_permissions(self, session: AsyncSession, role_id: int) -> set[str]:
        """Return the permission slugs granted by ``role_id``.

        For the super-user role, returns the full catalog (sentinel-managed).
        """
        role = await session.get(Role, role_id)
        if role is None:
            raise RoleNotFoundError(str(role_id))
        if role.name == SUPER_USER_NAME:
            return {p.value for p in Permission}
        stmt = select(RolePermission.permission).where(RolePermission.role_id == role_id)
        return set((await session.execute(stmt)).scalars().all())

    # ---- create ----

    async def create_role(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        name: str,
        description: str,
        permissions: Iterable[str],
        kind: str = "base",
    ) -> Role:
        """Create a new custom role of ``kind`` with the supplied permissions.

        ``kind`` is ``'base'`` (the default — a one-per-user role that
        carries privilege rank + hierarchy/ACL semantics) or
        ``'capability'`` (an additive, permission-only grant stacked on
        top of the base role, ADR-0005). The permission set must match
        the kind: a capability holds only capability-tier (``*.curate``)
        permissions, a base role only the rest
        (:exc:`PermissionKindMismatchError`).
        """
        await self._require_manage(session, actor_user_id)
        name = name.strip()
        if name in _BUILT_IN_NAMES:
            raise CannotEditBuiltInRoleError(name)
        if kind not in ROLE_KINDS:  # defensive — the API schema constrains this
            msg = f"kind must be one of {ROLE_KINDS}, got {kind!r}"
            raise ValueError(msg)

        slugs = self._validate_permissions(permissions, kind=kind)

        role = Role(
            name=name,
            description=description,
            is_built_in=False,
            kind=kind,
            created_by_user_id=actor_user_id,
        )
        session.add(role)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise CannotEditBuiltInRoleError(name) from exc

        for slug in slugs:
            session.add(RolePermission(role_id=role.id, permission=slug))
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="users.role.create",
                resource_type="role",
                resource_id=str(role.id),
                outcome="success",
                payload={"name": role.name, "kind": role.kind, "permissions": sorted(slugs)},
            ),
        )
        return role

    # ---- update ----

    async def set_permissions(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        role_id: int,
        permissions: Iterable[str],
    ) -> Role:
        """Replace the role's permission set with ``permissions``."""
        await self._require_manage(session, actor_user_id)

        role = await session.get(Role, role_id)
        if role is None:
            raise RoleNotFoundError(str(role_id))
        if role.name == SUPER_USER_NAME:
            raise CannotEditSuperUserPermissionsError

        slugs = self._validate_permissions(permissions, kind=role.kind)

        await session.execute(delete(RolePermission).where(RolePermission.role_id == role_id))
        for slug in slugs:
            session.add(RolePermission(role_id=role_id, permission=slug))
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="users.role.set_permissions",
                resource_type="role",
                resource_id=str(role_id),
                outcome="success",
                payload={"name": role.name, "permissions": sorted(slugs)},
            ),
        )
        return role

    async def update_description(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        role_id: int,
        description: str,
    ) -> Role:
        """Rewrite a role's description (built-in roles allowed; only the *name* is locked)."""
        await self._require_manage(session, actor_user_id)
        role = await session.get(Role, role_id)
        if role is None:
            raise RoleNotFoundError(str(role_id))
        role.description = description
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="users.role.update_description",
                resource_type="role",
                resource_id=str(role_id),
                outcome="success",
                payload={"name": role.name},
            ),
        )
        return role

    async def rename_role(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        role_id: int,
        name: str,
    ) -> Role:
        """Rename a custom role. Built-in role names are locked.

        Only the *name* changes — kind, permissions and memberships are
        untouched (everything binds by ``role.id``, so renames carry no
        risk of orphaning a reference). The new name can't collide with a
        built-in name or any existing role (:exc:`DuplicateRoleError`).
        """
        await self._require_manage(session, actor_user_id)
        role = await session.get(Role, role_id)
        if role is None:
            raise RoleNotFoundError(str(role_id))
        if role.is_built_in:
            raise CannotEditBuiltInRoleError(role.name)

        name = name.strip()
        if name in _BUILT_IN_NAMES:
            raise CannotEditBuiltInRoleError(name)
        if name == role.name:
            return role  # No-op rename.

        old_name = role.name
        role.name = name
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateRoleError(name) from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="users.role.rename",
                resource_type="role",
                resource_id=str(role_id),
                outcome="success",
                payload={"old_name": old_name, "new_name": name},
            ),
        )
        return role

    # ---- delete ----

    async def delete_role(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        role_id: int,
    ) -> None:
        """Refuse to delete any access-rights group — groups are permanent.

        Teams and access-rights groups are never deleted, even when they
        hold no members, so the audit trail can always resolve a
        historical membership reference back to a named group. The attempt
        is audited as ``denied`` and :class:`RoleDeletionForbiddenError` is
        raised. (The role is removed from circulation by emptying its
        membership, not by deletion.)
        """
        await self._require_manage(session, actor_user_id)
        role = await session.get(Role, role_id)
        if role is None:
            raise RoleNotFoundError(str(role_id))
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="users.role.delete",
                resource_type="role",
                resource_id=str(role_id),
                outcome="denied",
                reason="Access-rights groups are retained permanently for audit.",
                payload={"name": role.name},
            ),
        )
        raise RoleDeletionForbiddenError(role.name)

    # ---- assign ----

    async def assign_role_to_user(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        role_name: str,
    ) -> User:
        """Move ``target_user_id`` to ``role_name``. Enforces the last-super-user invariant."""
        await self._require_manage(session, actor_user_id)
        target = await session.get(User, target_user_id)
        if target is None:
            raise RoleNotFoundError(str(target_user_id))

        new_role = (
            await session.execute(select(Role).where(Role.name == role_name))
        ).scalar_one_or_none()
        if new_role is None:
            raise RoleNotFoundError(role_name)

        # Adding or removing the super_user role is reserved to super-users
        # themselves (holding roles.manage is not enough) — prevents an
        # administrator escalating themselves or anyone else to super_user,
        # or stripping a super-user. Additionally, a removal must never empty
        # the active super-user set.
        current_role = await session.get(Role, target.role_id)
        current_is_super = current_role is not None and current_role.name == SUPER_USER_NAME
        new_is_super = new_role.name == SUPER_USER_NAME
        if current_is_super != new_is_super:
            if not await user_is_super_user(session, actor_user_id):
                await self._audit_super_user_change_denied(
                    session,
                    actor_user_id=actor_user_id,
                    target_user_id=target_user_id,
                    attempted_role=new_role.name,
                    reason="Only a super-user can grant or remove the super_user role.",
                )
                raise SuperUserRoleChangeRequiresSuperUserError
            if current_is_super and not new_is_super:
                try:
                    await assert_not_last_super_user(session, target_user_id)
                except LastSuperUserError:
                    await self._audit_super_user_change_denied(
                        session,
                        actor_user_id=actor_user_id,
                        target_user_id=target_user_id,
                        attempted_role=new_role.name,
                        reason="Would leave the install without an active super-user.",
                    )
                    raise

        target.role_id = new_role.id
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="users.role.assign",
                resource_type="user",
                resource_id=str(target_user_id),
                outcome="success",
                payload={"new_role": new_role.name},
            ),
        )
        return target

    # ---- capability roles (ADR-0005) ----

    async def grant_capability_role(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        role_name: str,
    ) -> Role:
        """Stack a capability role on ``target_user_id`` — additive, idempotent.

        The user's single base role on ``User.role_id`` is untouched.
        Granting a role the user already holds is a no-op. Only
        ``capability``-kind roles can be granted this way; a base role
        raises :class:`NotACapabilityRoleError` (base roles are set via
        :meth:`assign_role_to_user`). The super-user invariants don't
        apply — capability roles carry no rank and never confer
        super-user status.
        """
        await self._require_manage(session, actor_user_id)
        target = await session.get(User, target_user_id)
        if target is None:
            raise RoleNotFoundError(str(target_user_id))

        role = await self._require_capability_role(session, role_name)

        existing = await session.get(
            UserCapabilityRole, {"user_id": target_user_id, "role_id": role.id}
        )
        if existing is not None:
            return role  # Idempotent: already granted.

        session.add(
            UserCapabilityRole(
                user_id=target_user_id,
                role_id=role.id,
                granted_by_user_id=actor_user_id,
            )
        )
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="users.user.capability_role.grant",
                resource_type="user",
                resource_id=str(target_user_id),
                outcome="success",
                payload={"role": role.name},
            ),
        )
        return role

    async def revoke_capability_role(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        role_name: str,
    ) -> Role:
        """Remove a capability role from ``target_user_id`` — idempotent.

        Revoking a role the user doesn't hold is a no-op. The base role
        is never affected. A base role raises
        :class:`NotACapabilityRoleError`.
        """
        await self._require_manage(session, actor_user_id)
        target = await session.get(User, target_user_id)
        if target is None:
            raise RoleNotFoundError(str(target_user_id))

        role = await self._require_capability_role(session, role_name)

        existing = await session.get(
            UserCapabilityRole, {"user_id": target_user_id, "role_id": role.id}
        )
        if existing is None:
            return role  # Idempotent: not held.

        await session.delete(existing)
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="users.user.capability_role.revoke",
                resource_type="user",
                resource_id=str(target_user_id),
                outcome="success",
                payload={"role": role.name},
            ),
        )
        return role

    async def set_user_capability_roles(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        role_names: Iterable[str],
    ) -> list[Role]:
        """Make the user's capability set exactly ``role_names`` (bulk replace).

        Computes the diff against the current memberships and grants the
        missing ones / revokes the extra ones, so each change is audited
        individually (mirrors ``TeamService.set_user_teams``). Validates
        every requested name is a ``capability``-kind role up front, so a
        bad name rejects the whole call before any change lands. Returns
        the resulting capability roles, ordered by name.
        """
        await self._require_manage(session, actor_user_id)
        target = await session.get(User, target_user_id)
        if target is None:
            raise RoleNotFoundError(str(target_user_id))

        desired = {name.strip() for name in role_names}
        # Validate the whole desired set before mutating anything.
        for name in desired:
            await self._require_capability_role(session, name)

        current = {
            role.name for role in await self.list_user_capability_roles(session, target_user_id)
        }
        for name in sorted(desired - current):
            await self.grant_capability_role(
                session,
                actor_user_id=actor_user_id,
                target_user_id=target_user_id,
                role_name=name,
            )
        for name in sorted(current - desired):
            await self.revoke_capability_role(
                session,
                actor_user_id=actor_user_id,
                target_user_id=target_user_id,
                role_name=name,
            )
        return await self.list_user_capability_roles(session, target_user_id)

    async def add_users_to_capability(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        role_id: int,
        user_ids: Iterable[int],
    ) -> Role:
        """Grant one capability role to many users in a single call.

        The mirror of the team-side bulk add: the operator picks several
        users on the capability's card and presses *Add* once. Each grant
        is idempotent (users already holding it are skipped) and audited
        individually via :meth:`grant_capability_role`. ``role_id`` must
        name a ``capability``-kind role (:exc:`NotACapabilityRoleError`);
        base roles are set one-per-user via :meth:`assign_role_to_user`.
        """
        await self._require_manage(session, actor_user_id)
        role = await session.get(Role, role_id)
        if role is None:
            raise RoleNotFoundError(str(role_id))
        if role.kind != "capability":
            raise NotACapabilityRoleError(role.name)
        for user_id in user_ids:
            await self.grant_capability_role(
                session,
                actor_user_id=actor_user_id,
                target_user_id=user_id,
                role_name=role.name,
            )
        return role

    async def list_user_capability_roles(
        self,
        session: AsyncSession,
        user_id: int,
    ) -> list[Role]:
        """Return the capability roles stacked on ``user_id``, ordered by name.

        Read-only and ungated at the service layer (mirrors
        :meth:`list_roles`); the calling surface gates visibility.
        """
        stmt = (
            select(Role)
            .join(UserCapabilityRole, UserCapabilityRole.role_id == Role.id)
            .where(UserCapabilityRole.user_id == user_id)
            .order_by(Role.name)
        )
        return list((await session.execute(stmt)).scalars())

    # ---- internals ----

    async def _require_capability_role(self, session: AsyncSession, role_name: str) -> Role:
        """Resolve ``role_name`` and assert it is a ``capability``-kind role."""
        role = (
            await session.execute(select(Role).where(Role.name == role_name))
        ).scalar_one_or_none()
        if role is None:
            raise RoleNotFoundError(role_name)
        if role.kind != "capability":
            raise NotACapabilityRoleError(role_name)
        return role

    async def _require_manage(self, session: AsyncSession, actor_user_id: int) -> None:
        if not await user_has_permission(session, actor_user_id, Permission.ROLES_MANAGE):
            raise PermissionDeniedError(Permission.ROLES_MANAGE.value)

    async def _audit_super_user_change_denied(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        target_user_id: int,
        attempted_role: str,
        reason: str,
    ) -> None:
        """Record a refused attempt to add/remove the super_user role.

        Same ``action`` as the success path so an auditor filtering on
        ``users.role.assign`` sees granted and denied attempts side by side.
        """
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="users.role.assign",
                resource_type="user",
                resource_id=str(target_user_id),
                outcome="denied",
                reason=reason,
                payload={"attempted_role": attempted_role},
            ),
        )

    @staticmethod
    def _validate_permissions(permissions: Iterable[str], *, kind: str) -> set[str]:
        """Validate a permission set against the catalog **and** the role kind.

        Every slug must be in the closed catalog
        (:exc:`UnknownPermissionError`). The set must also match ``kind``:
        a ``capability`` role may hold only capability-tier (``*.curate``)
        permissions and a ``base`` role only the rest, so the two tiers
        grant strictly non-overlapping sets (:exc:`PermissionKindMismatchError`).
        """
        catalog = {p.value for p in Permission}
        slugs: set[str] = set()
        for p in permissions:
            slug = p.value if isinstance(p, Permission) else p
            if slug not in catalog:
                raise UnknownPermissionError(slug)
            if is_capability_permission(slug) != (kind == "capability"):
                raise PermissionKindMismatchError(slug, kind)
            slugs.add(slug)
        return slugs


def make_role_service(audit: AuditWriter) -> RoleService:
    """Factory: build a :class:`RoleService` from the audit writer."""
    return RoleService(audit=audit)


async def assert_not_last_super_user(session: AsyncSession, leaving_user_id: int) -> None:
    """Refuse to leave the install with no active super-user.

    Raises :class:`LastSuperUserError` when dropping ``leaving_user_id``
    from the active super-user set would empty it. "Dropping" covers both
    demotion (role change off ``super_user``) and deactivation — each
    removes the user from the pool, so both paths must run this guard to
    preserve the install-wide at-least-one-super-user invariant.
    """
    super_role = (
        await session.execute(select(Role).where(Role.name == SUPER_USER_NAME))
    ).scalar_one()
    remaining = (
        (
            await session.execute(
                select(User).where(
                    User.role_id == super_role.id,
                    User.is_active.is_(True),
                    User.id != leaving_user_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if len(remaining) == 0:
        raise LastSuperUserError


async def user_is_super_user(session: AsyncSession, user_id: int) -> bool:
    """``True`` when ``user_id`` currently holds the ``super_user`` role."""
    user = await session.get(User, user_id)
    if user is None or user.role_id is None:
        return False
    role = await session.get(Role, user.role_id)
    return role is not None and role.name == SUPER_USER_NAME
