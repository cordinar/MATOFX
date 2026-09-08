"""Cohort source filters: which kind of run, and which named studies.

The cohort builder gained three narrowing pickers — **Source** (standalone /
workflow / parametric study / optimisation) and, for the two study kinds, the
studies **by name**. They shipped as preview-only because there was nowhere to
keep them, which made them useless the moment a curator re-opened the cohort:
the selections were gone, and a live cohort could never be expressed as "the
runs of these three studies" at all.

Three nullable JSON columns, matching the existing filter columns beside them
(``scenario_ids``, ``owner_user_ids``, …). ``NULL`` means *no filter here*,
which is what every existing cohort wants and gets for free.

Revision ID: 0097
Revises: 0096
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the three source filters, all nullable (no backfill needed)."""
    op.add_column("job_cohort", sa.Column("source_kinds", sa.JSON(), nullable=True))
    op.add_column("job_cohort", sa.Column("parametric_study_ids", sa.JSON(), nullable=True))
    op.add_column("job_cohort", sa.Column("optimisation_study_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop them again; a cohort that used one simply stops filtering on it."""
    op.drop_column("job_cohort", "optimisation_study_ids")
    op.drop_column("job_cohort", "parametric_study_ids")
    op.drop_column("job_cohort", "source_kinds")
