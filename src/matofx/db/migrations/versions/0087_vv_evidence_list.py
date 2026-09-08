"""Multiple evidence items per V&V record (ADR-0011 follow-up).

Revision ID: 0087
Revises: 0086
Create Date: 2026-07-22

Adds ``evidence_json`` to ``tool_vv_record`` and ``job_vv_record`` — a list of
evidence items ``[{"kind": "library"|"url", "file_version_id": int|null,
"url": str|null}, ...]`` so one record can cite several files and/or links of
mixed type. The legacy single ``evidence_file_version_id`` / ``evidence_url``
columns are kept and mirror the first library / url item for back-compat;
existing rows default to ``[]`` and are synthesised from those scalars on read.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0087"
down_revision: str | None = "0086"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES = ("tool_vv_record", "job_vv_record")


def upgrade() -> None:
    """Add the ``evidence_json`` list column to both V&V record tables."""
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "evidence_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        )


def downgrade() -> None:
    """Drop the ``evidence_json`` column from both V&V record tables."""
    for table in _TABLES:
        op.drop_column(table, "evidence_json")
