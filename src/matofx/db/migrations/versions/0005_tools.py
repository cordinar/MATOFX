"""Tool registry table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-03

Creates the ``tool`` table with the New / Test / Released state
vocabulary enforced by a CHECK constraint.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the ``tool`` table."""
    op.create_table(
        "tool",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("alias", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("descriptor", sa.JSON(), nullable=False),
        sa.Column("bundle_path", sa.String(length=512), nullable=False),
        sa.Column(
            "installed_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool")),
        sa.UniqueConstraint("alias", name=op.f("uq_tool_alias")),
        sa.ForeignKeyConstraint(
            ["installed_by_user_id"],
            ["user.id"],
            name=op.f("fk_tool_installed_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('new','test','released')",
            name="ck_tool_state",
        ),
    )
    op.create_index("ix_tool_state", "tool", ["state"])


def downgrade() -> None:
    """Drop the ``tool`` table."""
    op.drop_index("ix_tool_state", table_name="tool")
    op.drop_table("tool")
