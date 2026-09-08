"""Output validity on study_result (ADR-0017).

Revision ID: 0085
Revises: 0084
Create Date: 2026-06-30

Adds the per-result output-validity verdict to ``study_result`` — the unified
results store read by the study matrix, the scalar + field surrogates, and
optimisation. ``validity`` is ``valid`` | ``invalid`` and ``validity_reasons``
holds ``[{output, code, detail}, ...]`` (codes ``non_finite`` / ``below_min`` /
``above_max``). The runner stamps these at record time from each child's scalars
vs the descriptor's finiteness floor + ``min``/``max`` bounds; consumers exclude
invalid rows. Existing rows default to ``valid`` until the backfill (ADR-0017
slice 5) re-evaluates them. See ``designs/output-validity.md``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0085"
down_revision: str | None = "0084"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add validity + validity_reasons to study_result (existing rows -> valid)."""
    op.add_column(
        "study_result",
        sa.Column("validity", sa.String(16), nullable=False, server_default="valid"),
    )
    op.add_column(
        "study_result",
        sa.Column(
            "validity_reasons",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    """Drop the validity columns."""
    op.drop_column("study_result", "validity_reasons")
    op.drop_column("study_result", "validity")
