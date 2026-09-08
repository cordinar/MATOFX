"""Surrogate family: held-constant training inputs on a model version (ADR-0008 §7.6).

Revision ID: 0064
Revises: 0063
Create Date: 2026-06-04

Adds ``surrogate_model_version.input_constants`` — a ``{name: value}`` JSON
map of the inputs that were held identical across every training run (the
context the surrogate was fitted in, as opposed to the varied ``factors``).
The exporter surfaces it as the "held constant during training" half of a
synthesised surrogate-tool's input form, so a user sees both what they can
vary and what the surrogate assumes fixed.

Additive, nullable column (rows fitted before this revision read as
``NULL`` → ``{}`` at the ORM); a plain ``add_column`` works on both SQLite
and Postgres with no table recreate.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the nullable ``input_constants`` JSON column."""
    op.add_column(
        "surrogate_model_version",
        sa.Column("input_constants", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("surrogate_model_version", "input_constants")
