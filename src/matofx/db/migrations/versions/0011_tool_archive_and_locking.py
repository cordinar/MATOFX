"""Tool soft-archive lifecycle + optimistic-lock row_version + descriptor field rename.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-03

Adds to ``tool``:

- ``archived_at`` (nullable timestamptz)
- ``archived_by_user_id`` (FK user, ON DELETE SET NULL)
- ``archive_reason`` (text, nullable)
- ``row_version`` (Integer, NOT NULL, default 1) — optimistic-lock counter

The ``descriptor`` JSON shape changed too: ``categories`` →
``teams``. For existing rows we rewrite the JSON in place
(equivalent rename — the field's meaning is unchanged, just the key).
This is a brand-new install in Phase 1 so most rows simply don't
have ``categories`` to rewrite, but the path is included for
completeness.

No new permission slugs — install + edit + archive all gate on the
existing ``Permission.TOOLS_INSTALL``; promotion still gates on
``Permission.TOOLS_PROMOTE_STATE``.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add archive columns + row_version; rewrite categories→teams in JSON."""
    with op.batch_alter_table("tool") as batch:
        batch.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column(
                "archived_by_user_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("archive_reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"))
        batch.create_foreign_key(
            op.f("fk_tool_archived_by_user_id_user"),
            "user",
            ["archived_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_tool_archived_at", ["archived_at"])

    # Rewrite descriptor JSON: rename "categories" → "teams" on every row.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, descriptor FROM tool")).fetchall()
    for row in rows:
        tool_id = row[0]
        descriptor = row[1]
        if isinstance(descriptor, str):
            descriptor = json.loads(descriptor)
        if not isinstance(descriptor, dict) or "categories" not in descriptor:
            continue
        descriptor["teams"] = descriptor.pop("categories")
        bind.execute(
            sa.text("UPDATE tool SET descriptor = :d WHERE id = :id").bindparams(
                # Typed as JSON: Postgres won't store a bare string in a json column.
                sa.bindparam("d", descriptor, type_=sa.JSON),
                id=tool_id,
            )
        )


def downgrade() -> None:
    """Drop archive columns + row_version; rewrite teams→categories in JSON."""
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, descriptor FROM tool")).fetchall()
    for row in rows:
        tool_id = row[0]
        descriptor = row[1]
        if isinstance(descriptor, str):
            descriptor = json.loads(descriptor)
        if not isinstance(descriptor, dict) or "teams" not in descriptor:
            continue
        descriptor["categories"] = descriptor.pop("teams")
        bind.execute(
            sa.text("UPDATE tool SET descriptor = :d WHERE id = :id").bindparams(
                # Typed as JSON: Postgres won't store a bare string in a json column.
                sa.bindparam("d", descriptor, type_=sa.JSON),
                id=tool_id,
            )
        )

    with op.batch_alter_table("tool") as batch:
        batch.drop_index("ix_tool_archived_at")
        batch.drop_constraint(op.f("fk_tool_archived_by_user_id_user"), type_="foreignkey")
        batch.drop_column("row_version")
        batch.drop_column("archive_reason")
        batch.drop_column("archived_by_user_id")
        batch.drop_column("archived_at")
