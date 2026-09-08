"""Job Cohorts: store tolerated ``major.minor`` version bands.

Revision ID: 0076
Revises: 0075
Create Date: 2026-06-11

Backs ``designs/job-cohorts.md`` §3.2 (future-patch tolerance). A cohort
keyed on ``major.minor`` bands should keep matching *future* patch
releases within those bands, not just the patches that existed when it
was built. To do that the cohort stores the chosen bands
(``tolerated_version_bands`` — e.g. ``["1.2", "2.0"]``) and the live
membership query expands them to the *current* matching
``tool_version.id``s on every read.

``tolerated_tool_version_ids`` is kept as a last-known expansion snapshot
(display + fixed-cohort member validation + the legacy id path when a
cohort has no bands). Adding ``tolerated_version_bands`` nullable is the
whole change — no backfill: existing cohorts simply stay on the id path
until they're next edited through the builder (which writes bands), so
nothing changes membership unexpectedly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0076"
down_revision: str | None = "0075"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the nullable ``tolerated_version_bands`` JSON column."""
    op.add_column("job_cohort", sa.Column("tolerated_version_bands", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop the ``tolerated_version_bands`` column."""
    op.drop_column("job_cohort", "tolerated_version_bands")
