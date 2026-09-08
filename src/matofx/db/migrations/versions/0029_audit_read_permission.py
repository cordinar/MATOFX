"""Seed the ``audit.read`` permission onto the administrator role.

Revision ID: 0029
Revises: 0028
Create Date: 2026-05-15

Adds the new ``audit.read`` permission slug to the seeded
``administrator`` role so existing installs pick it up without manual
fiddling. ``super_user`` gets it implicitly via the catalog sentinel —
no row needed in ``role_permission`` for that role.

Held separately from ``database.explore`` so a compliance-focused
role can be granted audit-log access without unlocking the wider
database-browser surface (and vice-versa). The slug itself is defined
in :mod:`matofx.users.permissions`; this migration only seeds the row(s).
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | None = None
depends_on: str | None = None


_NEW_ADMIN_PERMISSIONS: tuple[str, ...] = ("audit.read",)


def upgrade() -> None:
    """Add ``audit.read`` to the administrator role's permission set."""
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
                sa.bindparam(
                    "perms",
                    list(_NEW_ADMIN_PERMISSIONS),
                    expanding=True,
                ),
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
    """Remove the ``audit.read`` row(s) seeded by :func:`upgrade`."""
    bind = op.get_bind()
    for perm in _NEW_ADMIN_PERMISSIONS:
        bind.execute(
            sa.text("DELETE FROM role_permission WHERE permission = :p").bindparams(p=perm)
        )
