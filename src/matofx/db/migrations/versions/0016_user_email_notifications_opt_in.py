"""Per-user opt-in for job-state notification emails.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-08

Adds:

- ``user.email_notifications_enabled`` — boolean, NOT NULL, defaults
  to TRUE. Layered with the per-tool descriptor flag the
  :class:`~matofx.notify.email.EmailNotifier` already honours: an
  email is sent only when the tool is opted in *and* the recipient is
  opted in.

Existing rows backfill to TRUE so we don't silently mute users who
were already receiving job-state emails before this column existed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add user.email_notifications_enabled (NOT NULL, default TRUE)."""
    with op.batch_alter_table("user") as batch:
        batch.add_column(
            sa.Column(
                "email_notifications_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    """Drop user.email_notifications_enabled."""
    with op.batch_alter_table("user") as batch:
        batch.drop_column("email_notifications_enabled")
