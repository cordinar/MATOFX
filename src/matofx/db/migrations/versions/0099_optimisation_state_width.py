"""Widen ``optimisation_study.state`` to fit its own vocabulary.

Revision ID: 0099
Revises: 0098

0066 sized the column ``VARCHAR(16)``, but ``pending_start_approval`` (22)
and ``pending_results_approval`` (24, added by 0079) are longer. SQLite
ignores the length, so it went unnoticed; Postgres refuses the write, and
no optimisation could be held for approval there.

Postgres only: SQLite has nothing to fix, and altering a column there
means rebuilding the table and its CHECK for no effect.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Widen the column to 32."""
    if op.get_bind().dialect.name == "sqlite":
        return
    op.alter_column(
        "optimisation_study",
        "state",
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Narrow it again (fails if a row sits in a pending-approval state)."""
    if op.get_bind().dialect.name == "sqlite":
        return
    op.alter_column(
        "optimisation_study",
        "state",
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=False,
    )
