"""Per-user dashboard-shell preferences blob.

Revision ID: 0081
Revises: 0080
Create Date: 2026-06-22

Adds:

- ``user.ui_preferences`` — JSON object, NOT NULL, server_default
  ``'{}'``. Holds the home-redesign shell preferences (sidebar
  pinned / width / per-group collapse, Quick Access kind filter) that
  the design (``designs/home-redesign.md`` §9) requires to be
  stored server-side per user so they survive logout and follow the
  user across devices — the same durability guarantee
  ``theme_preference`` already has.

Existing rows backfill to an empty object; the chrome treats missing
keys as "use the default" (pinned, default width, all groups expanded,
Quick Access filter = ``All``), so the upgrade is visually neutral.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add user.ui_preferences (JSON, NOT NULL, default '{}')."""
    with op.batch_alter_table("user") as batch:
        batch.add_column(
            sa.Column(
                "ui_preferences",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )


def downgrade() -> None:
    """Drop the ui_preferences column."""
    with op.batch_alter_table("user") as batch:
        batch.drop_column("ui_preferences")
