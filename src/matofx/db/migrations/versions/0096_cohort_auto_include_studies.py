"""Let a cohort adopt the runs of studies created to fill its gaps (ADR-0020 §11.5).

Revision ID: 0096
Revises: 0095
Create Date: 2026-08-27

"Suggest runs to fill the gaps" produces a parametric study, and the whole
point of it is that those runs come back and improve *this* cohort's coverage.
Until now nothing carried them back:

- an **auto-grow** cohort re-resolves its query on every read, so the new runs
  would usually join by themselves — but only if they also satisfy every other
  filter. A cohort narrowed by ``input_ranges``, by ``project_ids``, or by a
  completed-on window can perfectly well reject the very runs it asked for,
  and the user would see a study finish and the verdict not move;
- a **fixed** cohort is frozen to ``member_job_ids`` and would never take them
  at all.

So the link is recorded explicitly. ``auto_include_study_ids`` is the list of
studies this cohort commissioned; resolution unions their completed jobs into
the membership.

**Resolution-time, not event-time.** No completion hook, no queue, no race:
the runs appear in the cohort exactly when they are complete enough to be
resolved, which is the same rule ``auto_grow`` already works by. A hook would
have needed to fire reliably from the runner, survive a crash mid-batch, and
be idempotent; a union at read time is none of those things and cannot drift.

``excluded_job_ids`` still wins. A curator who threw a run out meant it, and a
study link must not quietly put it back.

Nullable, defaulting to NULL rather than ``[]`` — every existing cohort
commissioned nothing, and an empty list would be a claim where NULL is an
absence.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0096"
down_revision: str | None = "0095"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the study-link column."""
    op.add_column(
        "job_cohort",
        sa.Column("auto_include_study_ids", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """Drop it. The links are lost; the studies and their jobs are not."""
    op.drop_column("job_cohort", "auto_include_study_ids")
