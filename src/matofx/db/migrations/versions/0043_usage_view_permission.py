"""Seed the ``usage.view`` permission onto administrator.

Revision ID: 0043
Revises: 0042
Create Date: 2026-05-22

The Usage Statistics surface (``Admin → Usage Statistics``) is gated by
the new ``usage.view`` slug. This migration seeds it onto the seeded
``administrator`` role so existing installs pick the surface up without
manual fiddling; ``super_user`` holds it implicitly via the catalog
sentinel. The slug itself is defined in :mod:`matofx.users.permissions`.
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | None = None
depends_on: str | None = None


_NEW_ADMIN_PERMISSIONS: tuple[str, ...] = ("usage.view",)


def upgrade() -> None:
    """Add ``usage.view`` to the administrator role."""
    bind = op.get_bind()
    admin_id_row = bind.execute(
        sa.text("SELECT id FROM role WHERE name = :n").bindparams(n="administrator")
    ).fetchone()
    if admin_id_row is None:
        return  # Greenfield install: nothing seeded yet.
    admin_id: int = admin_id_row[0]

    existing = {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT permission FROM role_permission "
                "WHERE role_id = :rid AND permission IN :perms"
            ).bindparams(
                sa.bindparam("rid", admin_id),
                sa.bindparam("perms", list(_NEW_ADMIN_PERMISSIONS), expanding=True),
            )
        ).fetchall()
    }

    now = _dt.datetime.now(tz=_dt.UTC)
    rows = [
        {"role_id": admin_id, "permission": perm, "granted_at": now}
        for perm in _NEW_ADMIN_PERMISSIONS
        if perm not in existing
    ]
    if rows:
        op.bulk_insert(
            sa.table(
                "role_permission",
                sa.column("role_id", sa.Integer()),
                sa.column("permission", sa.String()),
                sa.column("granted_at", sa.DateTime(timezone=True)),
            ),
            rows,
        )


def downgrade() -> None:
    """Remove the ``usage.view`` row(s) seeded by upgrade."""
    bind = op.get_bind()
    for perm in _NEW_ADMIN_PERMISSIONS:
        bind.execute(
            sa.text("DELETE FROM role_permission WHERE permission = :p").bindparams(p=perm)
        )
