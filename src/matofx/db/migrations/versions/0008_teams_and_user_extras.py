"""Teams + user extras (first/last name, approver flag, logon-grant lifecycle).

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-03

Adds:

- ``team`` — flat catalog of teams (legacy parity, required for the
  per-user team membership shipped here and the per-tool ``categories``
  cross-validation).
- ``user_team`` — many-to-many join, composite PK ``(user_id, team_id)``.
- New columns on ``user``: ``first_name``, ``last_name``, ``is_approver``,
  ``logon_granted_at``, ``logon_revoked_at``.
- Two new permission slugs: ``users.manage`` and ``teams.manage``,
  granted to the seeded ``administrator`` role.

For existing rows in ``user``: backfills ``first_name``/``last_name``
by splitting ``full_name`` on the first space; ``logon_granted_at``
backfills to ``created_at``; ``logon_revoked_at`` stays NULL (current
``is_active`` users haven't been revoked).
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create teams + user_team, extend user, seed new permissions."""
    op.create_table(
        "team",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_team")),
        sa.UniqueConstraint("name", name=op.f("uq_team_name")),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_team_created_by_user_id_user"),
            ondelete="SET NULL",
        ),
    )

    op.create_table(
        "user_team",
        sa.Column(
            "user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column(
            "team_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "joined_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("user_id", "team_id", name=op.f("pk_user_team")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_user_team_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["team.id"],
            name=op.f("fk_user_team_team_id_team"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["joined_by_user_id"],
            ["user.id"],
            name=op.f("fk_user_team_joined_by_user_id_user"),
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_user_team_team_id", "user_team", ["team_id"])

    # User table extras. Use batch_alter_table so SQLite's table-rebuild
    # path picks up the new columns + their server-side defaults.
    with op.batch_alter_table("user") as batch:
        batch.add_column(
            sa.Column("first_name", sa.String(length=128), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("last_name", sa.String(length=128), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column(
                "is_approver",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("logon_granted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("logon_revoked_at", sa.DateTime(timezone=True), nullable=True))

    bind = op.get_bind()

    # Backfill first_name / last_name from full_name (split on first space).
    rows = bind.execute(sa.text('SELECT id, full_name FROM "user"')).fetchall()
    for row in rows:
        user_id = row[0]
        full = (row[1] or "").strip()
        if not full:
            first, last = "", ""
        elif " " in full:
            first, _, last = full.partition(" ")
        else:
            first, last = full, ""
        bind.execute(
            sa.text(
                'UPDATE "user" SET first_name = :first, last_name = :last WHERE id = :id'
            ).bindparams(first=first, last=last, id=user_id)
        )

    # Backfill logon_granted_at from created_at for existing rows.
    bind.execute(
        sa.text('UPDATE "user" SET logon_granted_at = created_at WHERE logon_granted_at IS NULL')
    )

    # logon_granted_at is now NOT NULL going forward.
    with op.batch_alter_table("user") as batch:
        batch.alter_column("logon_granted_at", nullable=False)

    # Seed the two new permissions onto the administrator role. The
    # super_user role's permissions are sentinel-managed in code (it
    # always has every Permission catalog entry), so we don't seed
    # super_user here.
    admin_id = bind.execute(
        sa.text("SELECT id FROM role WHERE name = :name").bindparams(name="administrator")
    ).scalar_one_or_none()
    if admin_id is not None:
        now = _dt.datetime.now(tz=_dt.UTC)
        op.bulk_insert(
            sa.table(
                "role_permission",
                sa.column("role_id", sa.Integer()),
                sa.column("permission", sa.String()),
                sa.column("granted_at", sa.DateTime(timezone=True)),
            ),
            [
                {"role_id": admin_id, "permission": "users.manage", "granted_at": now},
                {"role_id": admin_id, "permission": "teams.manage", "granted_at": now},
            ],
        )


def downgrade() -> None:
    """Revert the schema additions.

    No permission rows are removed — the closed catalog declaration in
    code is the authoritative reference.
    """
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM role_permission WHERE permission IN ('users.manage', 'teams.manage')")
    )

    with op.batch_alter_table("user") as batch:
        batch.drop_column("logon_revoked_at")
        batch.drop_column("logon_granted_at")
        batch.drop_column("is_approver")
        batch.drop_column("last_name")
        batch.drop_column("first_name")

    op.drop_index("ix_user_team_team_id", table_name="user_team")
    op.drop_table("user_team")
    op.drop_table("team")
