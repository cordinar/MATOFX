"""Custom roles + permission catalog.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-03

Replaces the previous string ``user.role`` column with a foreign key
into a new ``role`` table. Also creates the ``role_permission`` join
table holding the closed permission catalog assignments.

Seeds the four built-in roles (``super_user``, ``administrator``,
``beta_tester``, ``regular_user``) with their default permission sets
and backfills any existing users from their old ``role`` string.
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


_BUILT_IN_ROLES = (
    {
        "name": "super_user",
        "description": (
            "Full control. Permissions are sentinel-managed in code; this "
            "role always holds every permission in the catalog and cannot "
            "be edited at runtime."
        ),
    },
    {
        "name": "administrator",
        "description": "Operational control: manage users, install / promote tools, manage roles.",
    },
    {
        "name": "beta_tester",
        "description": (
            "Standard user with one extra capability: visibility of tools in the Test state."
        ),
    },
    {
        "name": "regular_user",
        "description": (
            "Default role for new accounts. Sees only their own projects and Released tools."
        ),
    },
)


_DEFAULT_PERMISSIONS = {
    "administrator": (
        "projects.see_all",
        "tools.install",
        "tools.promote_state",
        "tools.see_new_state",
        "tools.see_test_state",
        "users.create_with_role",
        "roles.manage",
    ),
    "beta_tester": ("tools.see_test_state",),
    "regular_user": (),
    # super_user gets nothing seeded — sentinel-managed in code.
}


def upgrade() -> None:
    """Create role + role_permission, seed built-ins, migrate user.role."""
    op.create_table(
        "role",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_built_in", sa.Boolean(), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role")),
        sa.UniqueConstraint("name", name=op.f("uq_role_name")),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_role_created_by_user_id_user"),
            ondelete="SET NULL",
        ),
    )

    op.create_table(
        "role_permission",
        sa.Column(
            "role_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("permission", sa.String(length=64), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("role_id", "permission", name=op.f("pk_role_permission")),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["role.id"],
            name=op.f("fk_role_permission_role_id_role"),
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_role_permission_role_id", "role_permission", ["role_id"])

    # Seed built-in roles.
    role_table = sa.table(
        "role",
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String()),
        sa.column("description", sa.String()),
        sa.column("is_built_in", sa.Boolean()),
        sa.column("created_by_user_id", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = _dt.datetime.now(tz=_dt.UTC)
    op.bulk_insert(
        role_table,
        [
            {
                "name": role["name"],
                "description": role["description"],
                "is_built_in": True,
                "created_by_user_id": None,
                "created_at": now,
                "updated_at": now,
            }
            for role in _BUILT_IN_ROLES
        ],
    )

    # Look up the seeded ids and seed permissions.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT name, id FROM role")).fetchall()
    name_to_id: dict[str, int] = {row[0]: row[1] for row in rows}

    perm_rows: list[dict[str, object]] = []
    for role_name, perms in _DEFAULT_PERMISSIONS.items():
        rid = name_to_id[role_name]
        perm_rows.extend({"role_id": rid, "permission": p, "granted_at": now} for p in perms)
    if perm_rows:
        op.bulk_insert(
            sa.table(
                "role_permission",
                sa.column("role_id", sa.Integer()),
                sa.column("permission", sa.String()),
                sa.column("granted_at", sa.DateTime(timezone=True)),
            ),
            perm_rows,
        )

    # Migrate user.role string → user.role_id FK.
    with op.batch_alter_table("user") as batch:
        batch.add_column(
            sa.Column(
                "role_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                nullable=True,
            )
        )

    # Backfill role_id from the existing role string.
    for name, rid in name_to_id.items():
        bind.execute(
            sa.text('UPDATE "user" SET role_id = :rid WHERE role = :name').bindparams(
                rid=rid, name=name
            )
        )

    # Drop the old role string column + its CHECK constraint, set role_id NOT NULL,
    # add the FK. SQLite needs batch_alter_table for column drop + constraint changes.
    with op.batch_alter_table("user") as batch:
        batch.drop_constraint("ck_user_role", type_="check")
        batch.drop_column("role")
        batch.alter_column("role_id", nullable=False)
        batch.create_foreign_key(
            "fk_user_role_id_role",
            "role",
            ["role_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    """Reverse — recreate user.role string, drop role tables.

    Users assigned to custom roles are coerced to ``regular_user``
    because the pre-0006 schema only knows the four built-in role
    names. This is data loss; downgrades are an emergency tool, not
    routine, and customers should never run this on production.
    """
    bind = op.get_bind()
    with op.batch_alter_table("user") as batch:
        batch.add_column(sa.Column("role", sa.String(length=32), nullable=True))

    built_in = bind.execute(
        sa.text("SELECT id, name FROM role WHERE is_built_in = :flag").bindparams(flag=True)
    ).fetchall()
    for rid, name in built_in:
        bind.execute(
            sa.text('UPDATE "user" SET role = :name WHERE role_id = :rid').bindparams(
                rid=rid, name=name
            )
        )
    # Coerce any users still on a custom role to regular_user.
    bind.execute(sa.text("UPDATE \"user\" SET role = 'regular_user' WHERE role IS NULL"))

    with op.batch_alter_table("user") as batch:
        batch.drop_constraint("fk_user_role_id_role", type_="foreignkey")
        batch.drop_column("role_id")
        batch.alter_column("role", nullable=False)
        batch.create_check_constraint(
            "ck_user_role",
            "role IN ('super_user','administrator','beta_tester','regular_user')",
        )

    op.drop_index("ix_role_permission_role_id", table_name="role_permission")
    op.drop_table("role_permission")
    op.drop_table("role")
