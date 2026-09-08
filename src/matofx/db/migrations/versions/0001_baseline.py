"""Baseline (empty) — establishes the migration chain.

Revision ID: 0001
Revises:
Create Date: 2026-05-03

This is intentionally a no-op. Domain tables land in Phase 1 as
separate revisions on top of this baseline. Having an explicit
baseline rather than an empty ``versions/`` lets us validate the
Alembic pipe end-to-end (upgrade head, downgrade base) on both
SQLite and Postgres from day one.
"""

from __future__ import annotations

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """No-op baseline."""


def downgrade() -> None:
    """No-op baseline."""
