"""Tests for the multi-role / capability-role foundation (ADR-0005).

Covers the slice-1 data model + resolver: the seeded ``library_curator``
/ ``materials_curator`` capability roles, the ``role.kind``
discriminator, and the base-plus-capability permission union in
:func:`user_has_permission`. The grant/revoke service layer is tested
separately once it lands.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.users import (
    Permission,
    Role,
    UserCapabilityRole,
    UserRole,
    UserService,
    effective_permissions,
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


async def _role_id(factory, name: str) -> int:
    async with session_scope(factory) as session:
        return (await session.execute(select(Role.id).where(Role.name == name))).scalar_one()


async def _grant_capability(factory, *, user_id: int, role_id: int) -> None:
    """Insert a capability membership directly (no service layer yet in slice 1)."""
    async with session_scope(factory) as session:
        session.add(UserCapabilityRole(user_id=user_id, role_id=role_id))


@pytest.mark.asyncio
async def test_capability_roles_are_seeded(users_engine: AsyncEngine) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        roles = {
            r.name: r
            for r in (
                await session.execute(
                    select(Role).where(Role.name.in_(["library_curator", "materials_curator"]))
                )
            )
            .scalars()
            .all()
        }
    assert set(roles) == {"library_curator", "materials_curator"}
    for role in roles.values():
        assert role.kind == "capability"
        assert role.is_built_in is True


@pytest.mark.asyncio
async def test_built_in_base_roles_have_kind_base(users_engine: AsyncEngine) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        kinds = {
            row[0]: row[1]
            for row in (
                await session.execute(
                    select(Role.name, Role.kind).where(
                        Role.name.in_(
                            ["super_user", "administrator", "beta_tester", "regular_user"]
                        )
                    )
                )
            ).all()
        }
    assert kinds == {
        "super_user": "base",
        "administrator": "base",
        "beta_tester": "base",
        "regular_user": "base",
    }


@pytest.mark.asyncio
async def test_capability_roles_carry_their_curate_permission(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """A user joined to a capability role gains exactly that curate slug."""
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service, username="mat", role="regular_user")

    async with session_scope(factory) as session:
        assert not await user_has_permission(session, user_id, Permission.MATERIALS_CURATE)
        assert not await user_has_permission(session, user_id, Permission.LIBRARY_CURATE)

    materials_id = await _role_id(factory, "materials_curator")
    await _grant_capability(factory, user_id=user_id, role_id=materials_id)

    async with session_scope(factory) as session:
        # Gains the granted slug ...
        assert await user_has_permission(session, user_id, Permission.MATERIALS_CURATE)
        # ... but not the other curator's slug.
        assert not await user_has_permission(session, user_id, Permission.LIBRARY_CURATE)


@pytest.mark.asyncio
async def test_effective_permissions_are_base_union_capability(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """Granting a capability role adds to, never replaces, the base set."""
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service, username="lib", role="regular_user")

    library_id = await _role_id(factory, "library_curator")
    await _grant_capability(factory, user_id=user_id, role_id=library_id)

    async with session_scope(factory) as session:
        # Capability permission resolves ...
        assert await user_has_permission(session, user_id, Permission.LIBRARY_CURATE)
        # ... and the base role's own defaults still resolve.
        assert await user_has_permission(session, user_id, Permission.PARAMETRIC_STUDIES_CREATE)
        # A permission held by neither stays denied.
        assert not await user_has_permission(session, user_id, Permission.ROLES_MANAGE)


@pytest.mark.asyncio
async def test_capability_membership_does_not_change_base_role(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """The base role on User.role_id is untouched by a capability grant."""
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service, username="ann", role="regular_user")
    regular_id = await _role_id(factory, "regular_user")
    library_id = await _role_id(factory, "library_curator")

    await _grant_capability(factory, user_id=user_id, role_id=library_id)

    async with session_scope(factory) as session:
        from matofx.users import User  # noqa: PLC0415

        user = await session.get(User, user_id)
        assert user is not None
        assert user.role_id == regular_id


@pytest.mark.asyncio
async def test_super_user_holds_curate_via_sentinel(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """super_user gets every catalog slug, including the new curate ones."""
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service, username="root", role="super_user")
    async with session_scope(factory) as session:
        assert await user_has_permission(session, user_id, Permission.LIBRARY_CURATE)
        assert await user_has_permission(session, user_id, Permission.MATERIALS_CURATE)


@pytest.mark.asyncio
async def test_effective_permissions_is_base_union_capability(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """The whole-set helper returns base defaults plus capability grants."""
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service, username="eff", role="regular_user")
    library_id = await _role_id(factory, "library_curator")
    await _grant_capability(factory, user_id=user_id, role_id=library_id)

    async with session_scope(factory) as session:
        perms = await effective_permissions(session, user_id)
    assert Permission.LIBRARY_CURATE.value in perms
    assert Permission.PARAMETRIC_STUDIES_CREATE.value in perms  # base-role default
    assert Permission.MATERIALS_CURATE.value not in perms
    assert Permission.ROLES_MANAGE.value not in perms


@pytest.mark.asyncio
async def test_effective_permissions_super_user_is_full_catalog(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service, username="root", role="super_user")
    async with session_scope(factory) as session:
        perms = await effective_permissions(session, user_id)
    assert perms == {p.value for p in Permission}
