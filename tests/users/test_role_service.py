"""Tests for the custom-role surface and the install-wide invariants."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.users import (
    CannotEditBuiltInRoleError,
    CannotEditSuperUserPermissionsError,
    DuplicateRoleError,
    LastSuperUserError,
    NotACapabilityRoleError,
    Permission,
    PermissionDeniedError,
    PermissionKindMismatchError,
    Role,
    RoleDeletionForbiddenError,
    RoleNotFoundError,
    RoleService,
    SuperUserRoleChangeRequiresSuperUserError,
    UnknownPermissionError,
    User,
    UserCapabilityRole,
    UserRole,
    UserService,
    make_role_service,
    user_has_permission,
)


async def _make_user(
    factory,
    user_service: UserService,
    *,
    username: str,
    role: UserRole,
) -> int:
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username=username,
            email=f"{username}@example.com",
            password="securepassword1",
            full_name=username.title(),
            role=role,
        )
        return user.id


@pytest.fixture
def role_service(audit_writer: AuditWriter) -> RoleService:
    return make_role_service(audit_writer)


@pytest.mark.asyncio
async def test_built_in_roles_are_seeded(users_engine: AsyncEngine) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        from sqlalchemy import select  # noqa: PLC0415

        kinds = {
            row[0]: row[1] for row in (await session.execute(select(Role.name, Role.kind))).all()
        }
    # Four base built-ins (ADR-0006-era) plus every seeded capability role:
    # library_curator / materials_curator (0044), vv_curator (0056),
    # model_curator (0062), optimisation_approver (0070), cohort_curator (0074).
    assert kinds == {
        "super_user": "base",
        "administrator": "base",
        "beta_tester": "base",
        "regular_user": "base",
        "library_curator": "capability",
        "materials_curator": "capability",
        "vv_curator": "capability",
        "model_curator": "capability",
        "optimisation_approver": "capability",
        "cohort_curator": "capability",
    }


@pytest.mark.asyncio
async def test_super_user_has_every_permission(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service, username="root", role="super_user")
    async with session_scope(factory) as session:
        for perm in Permission:
            assert await user_has_permission(session, user_id, perm)


@pytest.mark.asyncio
async def test_administrator_default_permissions(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        # Has install / promote / see-all
        assert await user_has_permission(session, user_id, Permission.PROJECTS_SEE_ALL)
        assert await user_has_permission(session, user_id, Permission.TOOLS_INSTALL)
        assert await user_has_permission(session, user_id, Permission.ROLES_MANAGE)
        # Doesn't have create-for-others (super-user only)
        assert not await user_has_permission(
            session, user_id, Permission.PROJECTS_CREATE_FOR_OTHERS
        )


@pytest.mark.asyncio
async def test_beta_tester_default_permissions(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service, username="beth", role="beta_tester")
    async with session_scope(factory) as session:
        assert await user_has_permission(session, user_id, Permission.TOOLS_SEE_TEST_STATE)
        assert not await user_has_permission(session, user_id, Permission.PROJECTS_SEE_ALL)
        assert not await user_has_permission(session, user_id, Permission.TOOLS_INSTALL)


@pytest.mark.asyncio
async def test_regular_user_default_permissions(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service, username="alice", role="regular_user")
    # Regular users get exactly the REGULAR_USER_DEFAULT_PERMISSIONS set
    # (custom-workflow + parametric-study creation, the latter added by
    # ADR-0001). Everything else stays restricted to roles that opt in.
    from matofx.users.permissions import (  # noqa: PLC0415
        REGULAR_USER_DEFAULT_PERMISSIONS,
    )

    async with session_scope(factory) as session:
        for perm in Permission:
            has = await user_has_permission(session, user_id, perm)
            if perm in REGULAR_USER_DEFAULT_PERMISSIONS:
                assert has, f"regular_user should hold {perm}"
            else:
                assert not has, f"regular_user should not hold {perm}"


@pytest.mark.asyncio
async def test_admin_can_create_custom_role(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        role = await role_service.create_role(
            session,
            actor_user_id=admin_id,
            name="qa-lead",
            description="QA validation lead",
            permissions=[Permission.TOOLS_SEE_TEST_STATE],
        )
    assert role.name == "qa-lead"
    assert role.is_built_in is False


@pytest.mark.asyncio
async def test_user_assigned_to_custom_role_gets_its_permissions(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    target_id = await _make_user(factory, user_service, username="alice", role="regular_user")
    async with session_scope(factory) as session:
        await role_service.create_role(
            session,
            actor_user_id=admin_id,
            name="qa-lead",
            description="QA",
            permissions=[Permission.TOOLS_SEE_TEST_STATE],
        )
        await role_service.assign_role_to_user(
            session,
            actor_user_id=admin_id,
            target_user_id=target_id,
            role_name="qa-lead",
        )
    async with session_scope(factory) as session:
        assert await user_has_permission(session, target_id, Permission.TOOLS_SEE_TEST_STATE)
        # Doesn't have other admin permissions.
        assert not await user_has_permission(session, target_id, Permission.TOOLS_INSTALL)


@pytest.mark.asyncio
async def test_regular_user_cannot_create_role(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service, username="alice", role="regular_user")
    async with session_scope(factory) as session:
        with pytest.raises(PermissionDeniedError):
            await role_service.create_role(
                session,
                actor_user_id=user_id,
                name="x",
                description="x",
                permissions=[],
            )


@pytest.mark.asyncio
async def test_cannot_create_role_with_unknown_permission(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        with pytest.raises(UnknownPermissionError):
            await role_service.create_role(
                session,
                actor_user_id=admin_id,
                name="bogus",
                description="x",
                permissions=["projects.invent_a_permission"],
            )


@pytest.mark.asyncio
async def test_create_capability_role_with_curate_permission(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    """A capability role carries curate permissions and is kind='capability'."""
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        role = await role_service.create_role(
            session,
            actor_user_id=admin_id,
            name="docs_curator",
            description="Curates library docs.",
            permissions=[Permission.LIBRARY_CURATE],
            kind="capability",
        )
        assert role.kind == "capability"


@pytest.mark.asyncio
async def test_capability_role_rejects_base_permission(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    """A capability may hold only capability-tier (*.curate) permissions."""
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        with pytest.raises(PermissionKindMismatchError):
            await role_service.create_role(
                session,
                actor_user_id=admin_id,
                name="bad_capability",
                description="",
                permissions=[Permission.TOOLS_INSTALL],
                kind="capability",
            )


@pytest.mark.asyncio
async def test_base_role_rejects_curate_permission(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    """A base role may not hold a curate permission (those belong to capabilities)."""
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        with pytest.raises(PermissionKindMismatchError):
            await role_service.create_role(
                session,
                actor_user_id=admin_id,
                name="over_eager_base",
                description="",
                permissions=[Permission.MATERIALS_CURATE],
                kind="base",
            )


@pytest.mark.asyncio
async def test_set_permissions_enforces_capability_kind(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    """Editing a capability's permissions still rejects base-tier slugs."""
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        cap = await role_service.create_role(
            session,
            actor_user_id=admin_id,
            name="lib_curator",
            description="",
            permissions=[Permission.LIBRARY_CURATE],
            kind="capability",
        )
        cap_id = cap.id
    async with session_scope(factory) as session:
        with pytest.raises(PermissionKindMismatchError):
            await role_service.set_permissions(
                session,
                actor_user_id=admin_id,
                role_id=cap_id,
                permissions=[Permission.TOOLS_INSTALL],
            )


@pytest.mark.asyncio
async def test_super_user_permissions_immutable(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        super_role = await role_service.get_by_name(session, "super_user")
        with pytest.raises(CannotEditSuperUserPermissionsError):
            await role_service.set_permissions(
                session,
                actor_user_id=admin_id,
                role_id=super_role.id,
                permissions=[Permission.TOOLS_INSTALL],
            )


@pytest.mark.asyncio
async def test_no_role_can_be_deleted(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    """Groups are permanent for audit — neither built-in nor custom roles delete."""
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        regular = await role_service.get_by_name(session, "regular_user")
        with pytest.raises(RoleDeletionForbiddenError):
            await role_service.delete_role(session, actor_user_id=admin_id, role_id=regular.id)
    # A custom group with no members is equally undeletable.
    async with session_scope(factory) as session:
        custom = await role_service.create_role(
            session,
            actor_user_id=admin_id,
            name="throwaway",
            description="",
            permissions=[],
            kind="base",
        )
        custom_id = custom.id
    async with session_scope(factory) as session:
        with pytest.raises(RoleDeletionForbiddenError):
            await role_service.delete_role(session, actor_user_id=admin_id, role_id=custom_id)


@pytest.mark.asyncio
async def test_admin_can_tune_built_in_non_super_role_permissions(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    """Customers can tune e.g. ``regular_user``'s default permission set."""
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        regular = await role_service.get_by_name(session, "regular_user")
        await role_service.set_permissions(
            session,
            actor_user_id=admin_id,
            role_id=regular.id,
            permissions=[Permission.TOOLS_SEE_TEST_STATE],
        )

    # Now any regular_user has the permission.
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")
    async with session_scope(factory) as session:
        assert await user_has_permission(session, alice_id, Permission.TOOLS_SEE_TEST_STATE)


@pytest.mark.asyncio
async def test_last_super_user_cannot_be_demoted(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    root_id = await _make_user(factory, user_service, username="root", role="super_user")
    async with session_scope(factory) as session:
        with pytest.raises(LastSuperUserError):
            await role_service.assign_role_to_user(
                session,
                actor_user_id=root_id,
                target_user_id=root_id,
                role_name="administrator",
            )


@pytest.mark.asyncio
async def test_super_user_can_be_demoted_when_another_exists(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    root_id = await _make_user(factory, user_service, username="root", role="super_user")
    backup_id = await _make_user(factory, user_service, username="backup", role="super_user")
    async with session_scope(factory) as session:
        await role_service.assign_role_to_user(
            session,
            actor_user_id=root_id,
            target_user_id=backup_id,
            role_name="administrator",
        )
    async with session_scope(factory) as session:
        from matofx.users.orm import User  # noqa: PLC0415

        backup = await session.get(User, backup_id)
        assert backup is not None
        target_role = await session.get(Role, backup.role_id)
        assert target_role is not None
        assert target_role.name == "administrator"


@pytest.mark.asyncio
async def test_administrator_cannot_demote_super_user(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    """Holding ``roles.manage`` is not enough — only a super-user may demote one.

    Two super-users exist so the last-super-user guard would *not* fire;
    the refusal must come purely from the actor-not-super-user check.
    """
    factory = make_session_factory(users_engine)
    await _make_user(factory, user_service, username="root", role="super_user")
    target_id = await _make_user(factory, user_service, username="backup", role="super_user")
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        with pytest.raises(SuperUserRoleChangeRequiresSuperUserError):
            await role_service.assign_role_to_user(
                session,
                actor_user_id=admin_id,
                target_user_id=target_id,
                role_name="administrator",
            )


@pytest.mark.asyncio
async def test_administrator_cannot_promote_to_super_user(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    """An administrator with ``roles.manage`` still can't grant super_user."""
    factory = make_session_factory(users_engine)
    await _make_user(factory, user_service, username="root", role="super_user")
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    target_id = await _make_user(factory, user_service, username="alice", role="regular_user")
    async with session_scope(factory) as session:
        with pytest.raises(SuperUserRoleChangeRequiresSuperUserError):
            await role_service.assign_role_to_user(
                session,
                actor_user_id=admin_id,
                target_user_id=target_id,
                role_name="super_user",
            )


@pytest.mark.asyncio
async def test_super_user_can_promote_to_super_user(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    root_id = await _make_user(factory, user_service, username="root", role="super_user")
    target_id = await _make_user(factory, user_service, username="alice", role="regular_user")
    async with session_scope(factory) as session:
        await role_service.assign_role_to_user(
            session,
            actor_user_id=root_id,
            target_user_id=target_id,
            role_name="super_user",
        )
    async with session_scope(factory) as session:
        from matofx.users.orm import User  # noqa: PLC0415

        target = await session.get(User, target_id)
        assert target is not None
        target_role = await session.get(Role, target.role_id)
        assert target_role is not None
        assert target_role.name == "super_user"


@pytest.mark.asyncio
async def test_administrator_cannot_create_super_user(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """Creating a super_user account is a grant — barred to non-super-users."""
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        with pytest.raises(SuperUserRoleChangeRequiresSuperUserError):
            await user_service.create_user(
                session,
                username="newroot",
                email="newroot@example.com",
                password="securepassword1",
                full_name="New Root",
                role="super_user",
                actor=f"user:{admin_id}",
                actor_user_id=admin_id,
            )


@pytest.mark.asyncio
async def test_super_user_can_create_super_user(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    root_id = await _make_user(factory, user_service, username="root", role="super_user")
    async with session_scope(factory) as session:
        created = await user_service.create_user(
            session,
            username="newroot",
            email="newroot@example.com",
            password="securepassword1",
            full_name="New Root",
            role="super_user",
            actor=f"user:{root_id}",
            actor_user_id=root_id,
        )
        new_id = created.id
    async with session_scope(factory) as session:
        from matofx.users.orm import User  # noqa: PLC0415

        new_user = await session.get(User, new_id)
        assert new_user is not None
        new_role = await session.get(Role, new_user.role_id)
        assert new_role is not None
        assert new_role.name == "super_user"


@pytest.mark.asyncio
async def test_last_super_user_cannot_be_deactivated(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    root_id = await _make_user(factory, user_service, username="root", role="super_user")
    async with session_scope(factory) as session:
        with pytest.raises(LastSuperUserError):
            await user_service.deactivate(
                session,
                user_id=root_id,
                actor=f"user:{root_id}",
                actor_user_id=root_id,
            )


@pytest.mark.asyncio
async def test_super_user_can_be_deactivated_when_another_exists(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    root_id = await _make_user(factory, user_service, username="root", role="super_user")
    backup_id = await _make_user(factory, user_service, username="backup", role="super_user")
    async with session_scope(factory) as session:
        await user_service.deactivate(
            session,
            user_id=backup_id,
            actor=f"user:{root_id}",
            actor_user_id=root_id,
        )
    async with session_scope(factory) as session:
        from matofx.users.orm import User  # noqa: PLC0415

        backup = await session.get(User, backup_id)
        assert backup is not None
        assert backup.is_active is False


@pytest.mark.asyncio
async def test_unknown_role_name_at_assign(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        with pytest.raises(RoleNotFoundError):
            await role_service.assign_role_to_user(
                session,
                actor_user_id=admin_id,
                target_user_id=admin_id,
                role_name="ghost-role",
            )


async def _denied_rows(factory, *, action: str) -> list[Any]:
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.audit.orm import AuditLog  # noqa: PLC0415

    async with session_scope(factory) as session:
        stmt = (
            select(AuditLog)
            .where(AuditLog.action == action, AuditLog.outcome == "denied")
            .order_by(AuditLog.id)
        )
        return list((await session.execute(stmt)).scalars())


@pytest.mark.asyncio
async def test_denied_demotion_is_audited(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    await _make_user(factory, user_service, username="root", role="super_user")
    target_id = await _make_user(factory, user_service, username="backup", role="super_user")
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        with pytest.raises(SuperUserRoleChangeRequiresSuperUserError):
            await role_service.assign_role_to_user(
                session,
                actor_user_id=admin_id,
                target_user_id=target_id,
                role_name="administrator",
            )

    rows = await _denied_rows(factory, action="users.role.assign")
    assert len(rows) == 1
    row = rows[0]
    assert row.actor == f"user:{admin_id}"
    assert row.resource_id == str(target_id)
    assert row.payload["attempted_role"] == "administrator"
    assert "super-user" in row.reason


@pytest.mark.asyncio
async def test_denied_last_super_user_demotion_is_audited(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    root_id = await _make_user(factory, user_service, username="root", role="super_user")
    async with session_scope(factory) as session:
        with pytest.raises(LastSuperUserError):
            await role_service.assign_role_to_user(
                session,
                actor_user_id=root_id,
                target_user_id=root_id,
                role_name="administrator",
            )

    rows = await _denied_rows(factory, action="users.role.assign")
    assert len(rows) == 1
    assert "active super-user" in rows[0].reason


@pytest.mark.asyncio
async def test_denied_super_user_creation_is_audited(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        with pytest.raises(SuperUserRoleChangeRequiresSuperUserError):
            await user_service.create_user(
                session,
                username="newroot",
                email="newroot@example.com",
                password="securepassword1",
                full_name="New Root",
                role="super_user",
                actor=f"user:{admin_id}",
                actor_user_id=admin_id,
            )

    rows = await _denied_rows(factory, action="auth.user.create")
    assert len(rows) == 1
    assert rows[0].resource_id == "newroot"
    assert rows[0].payload["attempted_role"] == "super_user"


@pytest.mark.asyncio
async def test_denied_last_super_user_deactivation_is_audited(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    root_id = await _make_user(factory, user_service, username="root", role="super_user")
    async with session_scope(factory) as session:
        with pytest.raises(LastSuperUserError):
            await user_service.deactivate(
                session,
                user_id=root_id,
                actor=f"user:{root_id}",
                actor_user_id=root_id,
            )

    rows = await _denied_rows(factory, action="auth.user.deactivate")
    assert len(rows) == 1
    assert rows[0].resource_id == str(root_id)
    assert "active super-user" in rows[0].reason


# --- capability roles (ADR-0005) ---


async def _capability_count(factory, *, user_id: int) -> int:
    from sqlalchemy import func, select  # noqa: PLC0415

    async with session_scope(factory) as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(UserCapabilityRole)
                .where(UserCapabilityRole.user_id == user_id)
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_grant_capability_role_adds_permission(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    user_id = await _make_user(factory, user_service, username="lib", role="regular_user")

    async with session_scope(factory) as session:
        assert not await user_has_permission(session, user_id, Permission.LIBRARY_CURATE)
        role = await role_service.grant_capability_role(
            session,
            actor_user_id=admin_id,
            target_user_id=user_id,
            role_name="library_curator",
        )
        assert role.name == "library_curator"

    async with session_scope(factory) as session:
        # The capability permission now resolves, base role unchanged.
        assert await user_has_permission(session, user_id, Permission.LIBRARY_CURATE)
        membership = await session.get(UserCapabilityRole, {"user_id": user_id, "role_id": role.id})
        assert membership is not None
        assert membership.granted_by_user_id == admin_id

    # Grant is audited.
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.audit.orm import AuditLog  # noqa: PLC0415

    async with session_scope(factory) as session:
        rows = list(
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "users.user.capability_role.grant")
                )
            ).scalars()
        )
    assert len(rows) == 1
    assert rows[0].resource_id == str(user_id)
    assert rows[0].payload["role"] == "library_curator"


@pytest.mark.asyncio
async def test_grant_capability_role_is_idempotent(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    user_id = await _make_user(factory, user_service, username="lib", role="regular_user")

    for _ in range(2):
        async with session_scope(factory) as session:
            await role_service.grant_capability_role(
                session,
                actor_user_id=admin_id,
                target_user_id=user_id,
                role_name="library_curator",
            )

    assert await _capability_count(factory, user_id=user_id) == 1


@pytest.mark.asyncio
async def test_revoke_capability_role_removes_permission(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    user_id = await _make_user(factory, user_service, username="mat", role="regular_user")

    async with session_scope(factory) as session:
        await role_service.grant_capability_role(
            session,
            actor_user_id=admin_id,
            target_user_id=user_id,
            role_name="materials_curator",
        )
    async with session_scope(factory) as session:
        await role_service.revoke_capability_role(
            session,
            actor_user_id=admin_id,
            target_user_id=user_id,
            role_name="materials_curator",
        )

    async with session_scope(factory) as session:
        assert not await user_has_permission(session, user_id, Permission.MATERIALS_CURATE)
    assert await _capability_count(factory, user_id=user_id) == 0

    # Revoking again is a no-op (idempotent), not an error.
    async with session_scope(factory) as session:
        await role_service.revoke_capability_role(
            session,
            actor_user_id=admin_id,
            target_user_id=user_id,
            role_name="materials_curator",
        )


@pytest.mark.asyncio
async def test_grant_base_role_as_capability_is_rejected(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    user_id = await _make_user(factory, user_service, username="reg", role="regular_user")

    async with session_scope(factory) as session:
        with pytest.raises(NotACapabilityRoleError):
            await role_service.grant_capability_role(
                session,
                actor_user_id=admin_id,
                target_user_id=user_id,
                role_name="administrator",
            )


@pytest.mark.asyncio
async def test_grant_capability_role_requires_roles_manage(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    actor_id = await _make_user(factory, user_service, username="reg", role="regular_user")
    target_id = await _make_user(factory, user_service, username="vic", role="regular_user")

    async with session_scope(factory) as session:
        with pytest.raises(PermissionDeniedError):
            await role_service.grant_capability_role(
                session,
                actor_user_id=actor_id,
                target_user_id=target_id,
                role_name="library_curator",
            )


@pytest.mark.asyncio
async def test_grant_unknown_capability_role_raises(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    user_id = await _make_user(factory, user_service, username="reg", role="regular_user")

    async with session_scope(factory) as session:
        with pytest.raises(RoleNotFoundError):
            await role_service.grant_capability_role(
                session,
                actor_user_id=admin_id,
                target_user_id=user_id,
                role_name="ghost_curator",
            )


@pytest.mark.asyncio
async def test_list_user_capability_roles(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    user_id = await _make_user(factory, user_service, username="both", role="regular_user")

    async with session_scope(factory) as session:
        for name in ("materials_curator", "library_curator"):
            await role_service.grant_capability_role(
                session,
                actor_user_id=admin_id,
                target_user_id=user_id,
                role_name=name,
            )

    async with session_scope(factory) as session:
        roles = await role_service.list_user_capability_roles(session, user_id)
    assert [r.name for r in roles] == ["library_curator", "materials_curator"]


@pytest.mark.asyncio
async def test_set_user_capability_roles_replaces(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    user_id = await _make_user(factory, user_service, username="set", role="regular_user")

    # Start with {library_curator}.
    async with session_scope(factory) as session:
        result = await role_service.set_user_capability_roles(
            session,
            actor_user_id=admin_id,
            target_user_id=user_id,
            role_names=["library_curator"],
        )
    assert [r.name for r in result] == ["library_curator"]

    # Replace with {materials_curator}: library revoked, materials granted.
    async with session_scope(factory) as session:
        result = await role_service.set_user_capability_roles(
            session,
            actor_user_id=admin_id,
            target_user_id=user_id,
            role_names=["materials_curator"],
        )
    assert [r.name for r in result] == ["materials_curator"]
    async with session_scope(factory) as session:
        assert not await user_has_permission(session, user_id, Permission.LIBRARY_CURATE)
        assert await user_has_permission(session, user_id, Permission.MATERIALS_CURATE)

    # Empty list clears membership.
    async with session_scope(factory) as session:
        result = await role_service.set_user_capability_roles(
            session,
            actor_user_id=admin_id,
            target_user_id=user_id,
            role_names=[],
        )
    assert result == []
    assert await _capability_count(factory, user_id=user_id) == 0


@pytest.mark.asyncio
async def test_set_user_capability_roles_rejects_base_role_atomically(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    """A base-role name aborts the whole set before any change lands."""
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    user_id = await _make_user(factory, user_service, username="set", role="regular_user")

    async with session_scope(factory) as session:
        await role_service.grant_capability_role(
            session,
            actor_user_id=admin_id,
            target_user_id=user_id,
            role_name="library_curator",
        )

    async with session_scope(factory) as session:
        with pytest.raises(NotACapabilityRoleError):
            await role_service.set_user_capability_roles(
                session,
                actor_user_id=admin_id,
                target_user_id=user_id,
                role_names=["materials_curator", "administrator"],
            )

    # The pre-existing membership is untouched (validation precedes mutation).
    async with session_scope(factory) as session:
        roles = await role_service.list_user_capability_roles(session, user_id)
    assert [r.name for r in roles] == ["library_curator"]


# --- Rename a custom role ----------------------------------------------


@pytest.mark.asyncio
async def test_rename_custom_role_keeps_members(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    target_id = await _make_user(factory, user_service, username="bob", role="regular_user")

    async with session_scope(factory) as session:
        role = await role_service.create_role(
            session,
            actor_user_id=admin_id,
            name="qa-lead",
            description="QA",
            permissions=[Permission.TOOLS_SEE_TEST_STATE],
        )
        role_id = role.id
        await role_service.assign_role_to_user(
            session, actor_user_id=admin_id, target_user_id=target_id, role_name="qa-lead"
        )

    async with session_scope(factory) as session:
        renamed = await role_service.rename_role(
            session, actor_user_id=admin_id, role_id=role_id, name="qa-manager"
        )
        assert renamed.name == "qa-manager"

    # The user's base role still resolves — membership binds by role id.
    async with session_scope(factory) as session:
        user = await session.get(User, target_id)
        assert user is not None
        role = await session.get(Role, user.role_id)
    assert role is not None
    assert role.name == "qa-manager"


@pytest.mark.asyncio
async def test_rename_built_in_role_rejected(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    async with session_scope(factory) as session:
        regular = await role_service.get_by_name(session, "regular_user")
        regular_id = regular.id
    async with session_scope(factory) as session:
        with pytest.raises(CannotEditBuiltInRoleError):
            await role_service.rename_role(
                session, actor_user_id=admin_id, role_id=regular_id, name="basic_user"
            )


@pytest.mark.asyncio
async def test_rename_custom_role_to_builtin_name_rejected(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    async with session_scope(factory) as session:
        role = await role_service.create_role(
            session, actor_user_id=admin_id, name="qa-lead", description="QA", permissions=[]
        )
        role_id = role.id
    async with session_scope(factory) as session:
        with pytest.raises(CannotEditBuiltInRoleError):
            await role_service.rename_role(
                session, actor_user_id=admin_id, role_id=role_id, name="administrator"
            )


@pytest.mark.asyncio
async def test_rename_custom_role_collision_rejected(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    async with session_scope(factory) as session:
        await role_service.create_role(
            session, actor_user_id=admin_id, name="qa-lead", description="QA", permissions=[]
        )
        other = await role_service.create_role(
            session, actor_user_id=admin_id, name="qa-second", description="QA2", permissions=[]
        )
        other_id = other.id
    async with session_scope(factory) as session:
        with pytest.raises(DuplicateRoleError):
            await role_service.rename_role(
                session, actor_user_id=admin_id, role_id=other_id, name="qa-lead"
            )


# --- Bulk add to a capability ------------------------------------------


@pytest.mark.asyncio
async def test_add_users_to_capability_bulk(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")
    bob_id = await _make_user(factory, user_service, username="bob", role="regular_user")

    async with session_scope(factory) as session:
        cap = await role_service.get_by_name(session, "library_curator")
        cap_id = cap.id
        # Alice already holds it — bulk add must be idempotent for her.
        await role_service.grant_capability_role(
            session, actor_user_id=admin_id, target_user_id=alice_id, role_name="library_curator"
        )

    async with session_scope(factory) as session:
        await role_service.add_users_to_capability(
            session, actor_user_id=admin_id, role_id=cap_id, user_ids=[alice_id, bob_id]
        )

    async with session_scope(factory) as session:
        alice_caps = await role_service.list_user_capability_roles(session, alice_id)
        bob_caps = await role_service.list_user_capability_roles(session, bob_id)
    assert [r.name for r in alice_caps] == ["library_curator"]
    assert [r.name for r in bob_caps] == ["library_curator"]


@pytest.mark.asyncio
async def test_add_users_to_capability_rejects_base_role(
    users_engine: AsyncEngine,
    user_service: UserService,
    role_service: RoleService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")
    async with session_scope(factory) as session:
        base = await role_service.get_by_name(session, "regular_user")
        base_id = base.id
    async with session_scope(factory) as session:
        with pytest.raises(NotACapabilityRoleError):
            await role_service.add_users_to_capability(
                session, actor_user_id=admin_id, role_id=base_id, user_ids=[alice_id]
            )
