"""Tool: add ``run_cap`` nullable column for the per-tool study cap.

Revision ID: 0040
Revises: 0039
Create Date: 2026-05-20

Backs the 2026-05-20 amendment to ADR-0001 /
``designs/parametric-studies.md`` §16: the install-wide default cap was
raised 200 -> 500, with a per-tool override editable in the Edit Tool
Metadata tab. ``tool.run_cap`` is a nullable integer; ``NULL`` means
"fall through to the install default". An additive nullable column has
no CHECK / nullability change, so a plain ``op.add_column`` /
``op.drop_column`` works on both SQLite and Postgres without the
recreate-style ``batch_alter_table`` (cf. migrations 0031 / 0032 / 0038
/ 0039 — those touch CHECKs and need the recreate).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``tool.run_cap`` (nullable Integer)."""
    op.add_column("tool", sa.Column("run_cap", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop ``tool.run_cap``."""
    op.drop_column("tool", "run_cap")
