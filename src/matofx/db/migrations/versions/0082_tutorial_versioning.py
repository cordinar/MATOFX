"""Tutorial versioning: lineage grouping + archived (superseded) marker.

Revision ID: 0082
Revises: 0081
Create Date: 2026-06-23

Tutorials were already append-only in spirit (upload a new row, flip the
old inactive). This makes "update, keeping the old version" first-class
and removes hard deletion:

- ``tutorial.lineage_id`` — groups every version of the *same* tutorial.
  Backfilled to the row's own id, so each existing tutorial becomes the
  head of its own single-entry lineage. An "update" creates a new row
  carrying the source row's ``lineage_id``.
- ``tutorial.superseded_at`` — when set, this row is an **archived
  previous version**: replaced by a newer row in the same lineage,
  forced inactive, never surfaced to users, kept for history. ``NULL``
  ⇒ the current version of its lineage (still activatable / deactivatable
  as before).
- ``ix_tutorial_lineage`` — index for grouping a lineage's versions.

No data is deleted; deletion of tutorials is removed from the app, so
old versions accumulate as archived history.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add lineage_id + superseded_at, backfill lineage_id = id, index it."""
    with op.batch_alter_table("tutorial") as batch:
        batch.add_column(sa.Column("lineage_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
    # Each existing row heads its own lineage.
    op.execute("UPDATE tutorial SET lineage_id = id WHERE lineage_id IS NULL")
    op.create_index("ix_tutorial_lineage", "tutorial", ["lineage_id"])


def downgrade() -> None:
    """Reverse 0082 — drop the index and both columns."""
    op.drop_index("ix_tutorial_lineage", table_name="tutorial")
    with op.batch_alter_table("tutorial") as batch:
        batch.drop_column("superseded_at")
        batch.drop_column("lineage_id")
