"""Tutorials table + tutorials.manage permission seed.

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-11

Adds the ``tutorial`` table for admin-curated PDF + video help
material surfaced on the home page, and seeds the new
``tutorials.manage`` permission onto the ``administrator`` role.

The ``super_user`` role gets the permission implicitly via the
catalog sentinel (:data:`matofx.users.permissions.SUPER_USER_PERMISSIONS`)
so no row is needed for it.
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | None = None
depends_on: str | None = None


_NEW_ADMIN_PERMISSIONS: tuple[str, ...] = ("tutorials.manage",)


def upgrade() -> None:
    """Create the ``tutorial`` table + seed ``tutorials.manage`` on administrator."""
    op.create_table(
        "tutorial",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("tool_alias", sa.String(length=64), nullable=True),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=127), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_path", sa.String(length=512), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "uploaded_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tutorial")),
        sa.ForeignKeyConstraint(
            ["tool_alias"],
            ["tool.alias"],
            name=op.f("fk_tutorial_tool_alias_tool"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"],
            ["user.id"],
            name=op.f("fk_tutorial_uploaded_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "media_type IN ('document', 'video')",
            name="ck_tutorial_media_type",
        ),
    )
    op.create_index(
        "ix_tutorial_tool_alias",
        "tutorial",
        ["tool_alias"],
    )
    op.create_index(
        "ix_tutorial_tool_active",
        "tutorial",
        ["tool_alias", "media_type", "is_active"],
    )

    # Seed the new permission onto the administrator role. ``super_user``
    # gets it for free via the catalog sentinel — no row needed.
    bind = op.get_bind()
    admin_id_row = bind.execute(
        sa.text("SELECT id FROM role WHERE name = :n").bindparams(n="administrator")
    ).fetchone()
    if admin_id_row is None:
        return  # Greenfield install — built-in roles not seeded yet.
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
    """Drop the table + remove the seeded permission rows."""
    bind = op.get_bind()
    for perm in _NEW_ADMIN_PERMISSIONS:
        bind.execute(
            sa.text("DELETE FROM role_permission WHERE permission = :p").bindparams(p=perm)
        )
    op.drop_index("ix_tutorial_tool_active", table_name="tutorial")
    op.drop_index("ix_tutorial_tool_alias", table_name="tutorial")
    op.drop_table("tutorial")
