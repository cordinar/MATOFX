"""Tests for migration ``0052_lineage_permission``.

The migration seeds ``lineage.view`` onto the ``administrator`` role
(``super_user`` holds it implicitly via the catalog sentinel). Slice 3
contract from ``designs/data-lineage.md`` §11 calls for an explicit
seed test so a future migration drift doesn't silently strip the grant.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.users.permissions import Permission, user_has_permission


@pytest.mark.asyncio
async def test_administrator_role_has_lineage_view_after_migration(
    lineage_engine: AsyncEngine,
    user_service,
) -> None:
    """Migration ``0052`` lands ``lineage.view`` on every administrator.

    Build a fresh ``administrator`` after the conftest's migration chain
    has run to ``head`` and verify the user has the permission via the
    role grant (not just the catalog sentinel).
    """
    factory = make_session_factory(lineage_engine)
    async with session_scope(factory) as session:
        admin = await user_service.create_user(
            session,
            username="ada",
            email="ada@example.com",
            password="adapassword",
            full_name="Ada",
            role="administrator",
        )
        admin_id = admin.id
    async with session_scope(factory) as session:
        assert await user_has_permission(session, admin_id, Permission.LINEAGE_VIEW)


@pytest.mark.asyncio
async def test_super_user_holds_lineage_view_via_catalog_sentinel(
    lineage_engine: AsyncEngine,
    user_service,
) -> None:
    """``super_user`` holds every catalog permission (sentinel short-circuit).

    The migration doesn't seed a row for super-users — they hold every
    :class:`Permission` member implicitly. This test pins that
    invariant for lineage.view alongside the migration test so a drift
    in the sentinel surfaces as a clear failure here, not deep in an
    unrelated API test.
    """
    factory = make_session_factory(lineage_engine)
    async with session_scope(factory) as session:
        root = await user_service.create_user(
            session,
            username="root",
            email="root@example.com",
            password="rootpassword",
            full_name="Root",
            role="super_user",
        )
        root_id = root.id
    async with session_scope(factory) as session:
        assert await user_has_permission(session, root_id, Permission.LINEAGE_VIEW)


@pytest.mark.asyncio
async def test_regular_user_does_not_hold_lineage_view_by_default(
    lineage_engine: AsyncEngine,
    user_service,
) -> None:
    """``regular_user`` does **not** hold ``lineage.view`` out of the box.

    Operators grant it explicitly via the role editor when a custom
    role needs it. This test pins the default exclusion so a stray
    edit to :data:`REGULAR_USER_DEFAULT_PERMISSIONS` surfaces as a
    failure here.
    """
    factory = make_session_factory(lineage_engine)
    async with session_scope(factory) as session:
        regular = await user_service.create_user(
            session,
            username="rita",
            email="rita@example.com",
            password="ritapassword",
            full_name="Rita",
        )
        regular_id = regular.id
    async with session_scope(factory) as session:
        assert not await user_has_permission(session, regular_id, Permission.LINEAGE_VIEW)
