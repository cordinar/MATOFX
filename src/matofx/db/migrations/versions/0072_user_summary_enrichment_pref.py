"""Auto-summaries: per-user ``summary_enrichment_enabled`` opt-in.

Revision ID: 0072
Revises: 0071
Create Date: 2026-06-11

Backs ``designs/auto-summaries.md`` §6: a per-user opt-out from
AI-enriched result summaries. ``NOT NULL`` with ``server_default = TRUE`` so
every existing user defaults to opted-in (the install-wide
``llm_summaries_enabled`` setting is what actually gates enrichment; this only
selects which cached text a user sees).

Additive single-column add via ``batch_alter_table`` so SQLite recreates the
table cleanly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``user.summary_enrichment_enabled`` (NOT NULL, default TRUE)."""
    with op.batch_alter_table("user") as batch:
        batch.add_column(
            sa.Column(
                "summary_enrichment_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    """Drop ``user.summary_enrichment_enabled``."""
    with op.batch_alter_table("user") as batch:
        batch.drop_column("summary_enrichment_enabled")
