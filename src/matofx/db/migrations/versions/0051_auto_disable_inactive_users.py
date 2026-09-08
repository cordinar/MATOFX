"""Auto-disable inactive users — column adds + last_login_at backfill.

Revision ID: 0051
Revises: 0050
Create Date: 2026-05-26

Adds the on-row state the inactivity-sweep feature reads and writes:

- ``user.last_grace_warning_at`` — when the grace-warning email was
  last sent for the current inactivity stretch. Cleared on the next
  successful login so a re-stale user gets a fresh warning later.
- ``runtime_setting.delegated_to_role`` — per-row delegation grant.
  When a super-user-only setting marks itself
  ``delegatable_to=<role>`` in the catalog, a super-user can store
  that role here at runtime to let callers of that role mutate the
  *value*. Only super-users may set / clear the delegation itself.

Also performs a one-shot **backfill of ``user.last_login_at``** for
existing accounts whose timestamp is NULL — pre-feature installs
have many such rows because the column existed but wasn't always
populated. The backfill reads the most recent ``granted`` event from
``user_logon_event`` per user; failing that it falls back to
``user.created_at``. Without this, the first sweep on a pre-existing
install would treat every NULL row as "infinitely old" and disable
the entire user base on the first run.

No data is seeded. The three new General Settings rows live in
:mod:`matofx.config.general_settings` (catalog-only — no DB row
needed until an admin saves an override) and default to 0
("feature off") so the migration alone never disables anyone.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the two new columns and backfill ``user.last_login_at``."""
    with op.batch_alter_table("user", recreate="never") as batch:
        batch.add_column(
            sa.Column(
                "last_grace_warning_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
    with op.batch_alter_table("runtime_setting", recreate="never") as batch:
        batch.add_column(
            sa.Column(
                "delegated_to_role",
                sa.String(length=32),
                nullable=True,
            )
        )

    # Backfill ``user.last_login_at`` for accounts that never had a
    # login (or were created before the column was populated). Picks
    # the most recent ``granted`` event from ``user_logon_event`` if
    # one exists, else falls back to ``user.created_at``. Both paths
    # use server-side SQL — no need to load Python ORM objects, which
    # keeps the migration usable from the migrate-bundle import flow
    # (the ORM-free constraint).
    #
    # We intentionally only touch rows where last_login_at IS NULL so
    # an install that already populated the column keeps its history.
    op.execute(
        sa.text(
            """
            UPDATE "user"
               SET last_login_at = COALESCE(
                       (SELECT MAX(ule.at)
                          FROM user_logon_event ule
                         WHERE ule.user_id = "user".id
                           AND ule.event = 'granted'),
                       "user".created_at
                   )
             WHERE last_login_at IS NULL
            """
        )
    )


def downgrade() -> None:
    """Drop the columns. The backfilled ``last_login_at`` values stay."""
    with op.batch_alter_table("runtime_setting", recreate="never") as batch:
        batch.drop_column("delegated_to_role")
    with op.batch_alter_table("user", recreate="never") as batch:
        batch.drop_column("last_grace_warning_at")
