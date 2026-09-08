"""MOP/CoP: persist the latest metamodel-of-optimal-prognosis analysis on a study.

Revision ID: 0069
Revises: 0068
Create Date: 2026-06-08

Phase 12 (ADR-0009 §5.7). A ``bo`` study can run MOP/CoP: fit the
best-Q² metamodel over its runs, attribute a Coefficient of Prognosis to
each factor, and freeze the low-CoP factors out of the search space. The
analysis result + the (user-overridable) frozen-factor decision are
recorded on the study so the UI can show the CoP bar chart and the loop
can read the active search space.

One additive, nullable column — a plain ADD COLUMN on both engines:

- ``optimisation_study.mop_state_json`` — ``{best_kind, q2, cop: [...],
  kind_q2, cutoff, frozen_factors: [...], updated_at}``; ``NULL`` until
  the first MOP analysis runs (and for every non-MOP study).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the nullable ``mop_state_json`` column."""
    op.add_column("optimisation_study", sa.Column("mop_state_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop ``mop_state_json``."""
    op.drop_column("optimisation_study", "mop_state_json")
