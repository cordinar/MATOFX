"""Seed tests for ``vv_curator`` capability role + admin grant (slice 3 / migration 0056).

After 0056, the install must carry:

- A built-in capability role named ``vv_curator`` (``kind='capability'``,
  ``is_built_in=True``).
- A ``role_permission`` row granting ``vv.curate`` to that role.
- A ``role_permission`` row granting ``vv.curate`` to the seeded
  ``administrator`` role (so existing admin installs pick up the V&V
  surface without manual fiddling).
- ``super_user`` holds ``vv.curate`` implicitly via the catalog sentinel
  in :func:`matofx.users.permissions.user_has_permission` — no DB row.

These tests exercise the raw schema (the seed wrote SQL via ``op``, not
the ORM), so they read from ``role`` and ``role_permission`` directly.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.users.permissions import Permission


@pytest.mark.asyncio
async def test_vv_curator_role_exists(vv_engine: AsyncEngine) -> None:
    """``vv_curator`` exists with the expected built-in + capability flags."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        row = (
            await session.execute(
                sa.text("SELECT name, is_built_in, kind FROM role WHERE name = 'vv_curator'")
            )
        ).fetchone()
    assert row is not None, "vv_curator role was not seeded"
    name, is_built_in, kind = row
    assert name == "vv_curator"
    assert bool(is_built_in) is True
    assert kind == "capability"


@pytest.mark.asyncio
async def test_vv_curator_holds_vv_curate(vv_engine: AsyncEngine) -> None:
    """The ``vv_curator`` capability carries exactly ``vv.curate``."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rows = await session.execute(
            sa.text(
                "SELECT rp.permission "
                "FROM role_permission rp "
                "JOIN role r ON r.id = rp.role_id "
                "WHERE r.name = 'vv_curator'"
            )
        )
        perms = {r[0] for r in rows.fetchall()}
    assert perms == {"vv.curate"}, (
        f"Expected vv_curator to carry exactly ['vv.curate'], got {sorted(perms)}"
    )


@pytest.mark.asyncio
async def test_administrator_holds_vv_curate(vv_engine: AsyncEngine) -> None:
    """``administrator`` gains ``vv.curate`` (so the surface is visible on upgrade)."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        rows = await session.execute(
            sa.text(
                "SELECT 1 "
                "FROM role_permission rp "
                "JOIN role r ON r.id = rp.role_id "
                "WHERE r.name = 'administrator' AND rp.permission = 'vv.curate'"
            )
        )
        present = rows.first() is not None
    assert present, "vv.curate was not granted to administrator"


def test_super_user_implicit_grant_via_catalog_sentinel() -> None:
    """``super_user`` gets ``vv.curate`` via the Permission-enum sentinel.

    No row in ``role_permission`` for super_user — the runtime check
    (:func:`user_has_permission`) short-circuits to True for super_user
    on any permission listed in the :class:`Permission` enum. This test
    asserts the enum membership; the runtime path itself is covered by
    the users-suite tests.

    The slug must be in the Permission catalog. If a refactor removes
    it, the slice-4 service's gate check stops working silently — the
    catalog membership is the source of truth, so we assert it here
    rather than only by docstring.
    """
    assert Permission.VV_CURATE.value == "vv.curate"
    assert "vv.curate" in {p.value for p in Permission}


@pytest.mark.asyncio
async def test_seed_produces_exactly_one_row_per_grant(vv_engine: AsyncEngine) -> None:
    """The seed wrote exactly one row per role + per grant — no duplicates.

    The migration uses ``WHERE NOT EXISTS``-style guards on every insert,
    so if migration 0056 were ever extended in place and re-run by a
    deployer the row counts must stay at 1. The upgrade-downgrade-
    upgrade cycle baked into ``tests/db/test_alembic.py`` exercises the
    full chain re-application; this test pins the per-row invariant on
    its own so a regression in 0056's guards shows up locally too.
    """
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        role_count = (
            await session.execute(sa.text("SELECT COUNT(*) FROM role WHERE name = 'vv_curator'"))
        ).scalar_one()
        grant_count_curator = (
            await session.execute(
                sa.text(
                    "SELECT COUNT(*) FROM role_permission rp "
                    "JOIN role r ON r.id = rp.role_id "
                    "WHERE r.name = 'vv_curator' AND rp.permission = 'vv.curate'"
                )
            )
        ).scalar_one()
        grant_count_admin = (
            await session.execute(
                sa.text(
                    "SELECT COUNT(*) FROM role_permission rp "
                    "JOIN role r ON r.id = rp.role_id "
                    "WHERE r.name = 'administrator' AND rp.permission = 'vv.curate'"
                )
            )
        ).scalar_one()

    assert role_count == 1, f"Expected 1 vv_curator row, got {role_count}"
    assert grant_count_curator == 1, f"Expected 1 vv_curator grant, got {grant_count_curator}"
    assert grant_count_admin == 1, f"Expected 1 administrator grant, got {grant_count_admin}"
