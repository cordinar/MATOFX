"""Per-user locale + timezone preference.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-11

Adds:

- ``user.locale_preference`` — text, NOT NULL, server_default ``'auto'``.
  BCP-47 tag (``'en-GB'``, ``'fr-FR'``, …) or the literal ``'auto'`` so
  the UI uses whatever the browser reports.
- ``user.timezone_preference`` — text, NOT NULL, server_default
  ``'auto'``. IANA tz id (``'Europe/London'``, ``'America/New_York'``, …)
  or the literal ``'auto'`` for browser-detected.

Both fields drive the top-banner live clock and every
``format_user_datetime`` call site. Existing rows backfill to
``'auto'`` so no visible behaviour change for in-place upgrades —
``'auto'`` lands at the same JS default the page was already using.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add user.locale_preference + user.timezone_preference."""
    with op.batch_alter_table("user") as batch:
        batch.add_column(
            sa.Column(
                "locale_preference",
                sa.String(length=35),
                nullable=False,
                server_default="auto",
            )
        )
        batch.add_column(
            sa.Column(
                "timezone_preference",
                sa.String(length=64),
                nullable=False,
                server_default="auto",
            )
        )


def downgrade() -> None:
    """Drop the two regional-preference columns."""
    with op.batch_alter_table("user") as batch:
        batch.drop_column("timezone_preference")
        batch.drop_column("locale_preference")
