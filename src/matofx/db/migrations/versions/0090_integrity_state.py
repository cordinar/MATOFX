"""Chain-integrity state for read-only mode (ADR-0019 slice 6).

Revision ID: 0090
Revises: 0089
Create Date: 2026-08-10

One singleton row recording the last audit-chain verification and, when
it failed, the Super User acknowledgement that lifts read-only mode.

Persisted rather than recomputed: read-only has to be decided on every
mutating request, and a full chain walk is far too expensive for the hot
path. Absent row = never checked = assumed good, so an install that
upgrades into this migration isn't read-only until something actually
fails.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0090"
down_revision: str | None = "0089"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create ``integrity_state``."""
    op.create_table(
        "integrity_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("chain_ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("records_checked", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", sa.String(1024), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(255), nullable=True),
        sa.Column("acknowledgement_note", sa.String(1024), nullable=True),
    )


def downgrade() -> None:
    """Drop ``integrity_state``."""
    op.drop_table("integrity_state")
