"""Per-user dark/light theme preference.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-10

Adds:

- ``user.theme_preference`` — text, NOT NULL, server_default ``'dark'``,
  CHECK ``theme_preference IN ('light', 'dark')``. The Account page lets
  the user flip between the two; the chrome reads the stored value on
  every page render so a sign-in on a fresh machine carries the user's
  preference with them.

Existing rows backfill to ``'dark'`` so pilot users see no visual
change after the upgrade — the dashboard has been dark-only up to now.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add user.theme_preference (NOT NULL, default 'dark', CHECK ('light','dark'))."""
    with op.batch_alter_table("user") as batch:
        batch.add_column(
            sa.Column(
                "theme_preference",
                sa.String(length=8),
                nullable=False,
                server_default="dark",
            )
        )
        batch.create_check_constraint(
            "ck_user_theme_preference",
            "theme_preference IN ('light', 'dark')",
        )


def downgrade() -> None:
    """Drop the CHECK and the column."""
    with op.batch_alter_table("user") as batch:
        batch.drop_constraint("ck_user_theme_preference", type_="check")
        batch.drop_column("theme_preference")
