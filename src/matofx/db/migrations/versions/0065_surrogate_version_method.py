"""Surrogate family: record the fitting strategy on a model version (ADR-0008 §4).

Revision ID: 0065
Revises: 0064
Create Date: 2026-06-04

Adds ``surrogate_model_version.surrogate_method`` — ``gp`` / ``gp_matern`` /
``poly`` / ``gbm`` — so the registry knows which strategy fitted each
version. It drives two things: the exporter only emits ``<response>_std``
outputs for methods that actually yield uncertainty (the GP kinds), and a
refit inherits the original method instead of silently defaulting to GP.

Additive, nullable column (rows fitted before this revision read as
``NULL`` → treated as ``gp`` by the ORM default); a plain ``add_column``
works on both SQLite and Postgres with no table recreate.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the nullable ``surrogate_method`` column."""
    op.add_column(
        "surrogate_model_version",
        sa.Column("surrogate_method", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("surrogate_model_version", "surrogate_method")
