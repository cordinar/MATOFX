"""Seed the ``lineage.view`` permission onto administrator.

Revision ID: 0052
Revises: 0051
Create Date: 2026-05-26

The lineage / provenance graph surface (ADR-0010) is gated by a new
``lineage.view`` permission. Holders can centre lineage on any object
they can already see via the owning domain's ACL — the permission
opens the lineage view *channel*; per-node visibility intersects with
existing domain ACLs at resolution time (see
:meth:`matofx.lineage.LineageService.build_graph`).

This migration seeds it onto the seeded ``administrator`` role so
existing installs pick the surface up without manual fiddling;
``super_user`` holds it implicitly via the catalog sentinel. The slug
itself is defined in :mod:`matofx.users.permissions`.

The companion :class:`LineageService` runs without this seed — it
checks the permission at the gate point, returning
:class:`matofx.lineage.LineageAccessDeniedError` when missing — but
without seeding, an administrator on an upgraded install would have
to grant it manually before the **Admin → Lineage** standalone page
or any inline "Provenance" tab worked for them.
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | None = None
depends_on: str | None = None


# Frozen literals — a migration must never import the app's enums
# (the catalog can drift between heads; the migration is point-in-time).
_NEW_ADMIN_PERMISSIONS: tuple[str, ...] = ("lineage.view",)


def upgrade() -> None:
    """Add ``lineage.view`` to the administrator role."""
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
    """Remove the ``lineage.view`` row(s) seeded by upgrade."""
    bind = op.get_bind()
    for perm in _NEW_ADMIN_PERMISSIONS:
        bind.execute(
            sa.text("DELETE FROM role_permission WHERE permission = :p").bindparams(p=perm)
        )
