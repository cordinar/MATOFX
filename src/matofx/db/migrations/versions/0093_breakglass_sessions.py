"""Mark break-glass sessions so they can be bounded and shown (ADR-0019 §C13).

Revision ID: 0093
Revises: 0092
Create Date: 2026-08-12

A Super User is admitted over the licence cap so an administrator is
never locked out at exactly the moment the licence needs fixing. That
admission was recorded in the audit chain and counted in the compliance
report, but nothing on the *session* said so — which left two holes.

The session could sit open indefinitely, so a deliberate emergency
became a quiet permanent excess; and Admin → Active sessions, the page
whose whole job is getting back under the cap, could not show which
sessions were the over-cap ones.

Capping the *number* of break-glass admissions was considered and
rejected: two stale Super User sessions holding the cap would lock out
the administrator break-glass exists to admit. Bounding the *duration*
has no such failure mode — admission is never refused, it simply does
not last for ever.

Existing rows default to false. That is correct rather than merely
convenient: a session created before this column existed was admitted
under a cap check that had no break-glass concept to record.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0093"
down_revision: str | None = "0092"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``auth_session.breakglass``."""
    with op.batch_alter_table("auth_session") as batch:
        batch.add_column(
            sa.Column(
                "breakglass",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    """Drop the column. Live break-glass sessions simply stop being marked."""
    with op.batch_alter_table("auth_session") as batch:
        batch.drop_column("breakglass")
