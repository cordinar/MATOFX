"""New-tool drafts: save partial New Tool setups for later resume.

Revision ID: 0084
Revises: 0083
Create Date: 2026-06-29

Adds ``tool_draft`` — a private, per-owner store of in-progress *new
tool* setups. A draft holds the typed metadata plus the inputs/outputs
wizard state (a descriptor-shaped JSON blob); it never holds the
uploaded bundle files (those are re-attached at the final install, per
the save-to-DB-only convention). Rows are deleted on successful install
or by the owner. The ``alias`` here is a remembered form value, not the
unique tool identifier — so it carries no uniqueness/FK constraint.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0084"
down_revision: str | None = "0083"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the tool_draft table + the owner/updated_at lookup index."""
    op.create_table(
        "tool_draft",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "owner_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(128), nullable=False, server_default=""),
        sa.Column("alias", sa.String(64), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_tool_draft_owner_updated", "tool_draft", ["owner_user_id", "updated_at"]
    )


def downgrade() -> None:
    """Drop the tool_draft table + its index."""
    op.drop_index("ix_tool_draft_owner_updated", table_name="tool_draft")
    op.drop_table("tool_draft")
