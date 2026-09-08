"""Job skip-queue priority flag.

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-13

Adds:

- ``job.priority`` — boolean, NOT NULL, server_default ``FALSE``.
  When ``TRUE`` the runner's pickup loop drains this job ahead of
  ``priority=FALSE`` jobs (ordered by ``priority DESC, created_at
  ASC``). Set by admins / super-users via the "Skip Queue" button on
  the workspace submit form when an urgent run needs to push past
  whatever else is queued.

Resource / licence gating is unchanged — priority only affects the
pickup *order*, not the *eligibility* check.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add job.priority (NOT NULL, default FALSE) and an index for the pickup order."""
    with op.batch_alter_table("job") as batch:
        batch.add_column(
            sa.Column(
                "priority",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    # The pickup loop scans every ``state='queued'`` job and sorts by
    # priority DESC, created_at ASC. A composite index on
    # (state, priority, created_at) keeps the scan cheap even under a
    # backlog; the existing ``ix_job_state`` covers the state filter
    # alone but doesn't help with the ordering.
    op.create_index(
        "ix_job_pickup_order",
        "job",
        ["state", "priority", "created_at"],
    )


def downgrade() -> None:
    """Drop the index and the column."""
    op.drop_index("ix_job_pickup_order", table_name="job")
    with op.batch_alter_table("job") as batch:
        batch.drop_column("priority")
