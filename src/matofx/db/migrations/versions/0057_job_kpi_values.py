"""KPI extraction: the ``job_kpi_value`` table (ADR-0012).

Revision ID: 0057
Revises: 0056
Create Date: 2026-05-29

Backs ``designs/kpi-extraction.md`` §2.1: a write-once, indexed,
per-(job, KPI) store of headline numeric results. One row per declared
KPI per completed job; populated by the runner's post-completion hook
(slice 3) after ``outputs.json`` validation and before the cleanup
sweep (ADR-0003) — so even when the bulk outputs document ages out,
the headline scalars persist forever.

Single new table; no CHECK constraints (the table has no closed
vocabularies — ``kpi_name`` is validated against the live tool
descriptor at write time, not at schema time). FKs:

- ``job_id`` → ``job.id`` ``ON DELETE CASCADE`` — a deleted job leaves
  no semantic claim about its KPIs.
- ``tool_version_id`` → ``tool_version.id`` ``ON DELETE RESTRICT`` —
  retiring a tool version that still owns KPI rows should fail loudly
  rather than silently orphan them.

Composite UNIQUE on ``(job_id, kpi_name)`` — re-extraction (slice 4)
UPSERTs against this key; a job never has two rows for the same KPI.

Two performance indexes:

- ``ix_job_kpi_value_alias_kpi`` on ``(tool_alias, kpi_name)`` — the
  warehouse query path ("all values of KPI X for tool Y") hits this
  directly as an indexed range scan.
- ``ix_job_kpi_value_extracted_at`` — drives the time-series chart's
  date-window range scan.

``tool_alias`` and ``tool_version_id`` are denormalised from
``job.tool_alias`` / ``job.tool_version_id`` so the warehouse query
avoids the join. Cost: ~16 bytes per row of duplication; benefit:
single-index range scans replace 2-table joins on the hottest path.

Additive table creation only — plain ``op.create_table``, no
recreate-style ``batch_alter_table`` (no schema rewrite of existing
tables). One-way-lossy downgrade ``DROP TABLE`` — slice 4's
re-extraction can rebuild the rows from each job's stored
``outputs.json`` per ADR-0003.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Create the ``job_kpi_value`` table + its UNIQUE + two indexes."""
    op.create_table(
        "job_kpi_value",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("job_id", _BIGINT, nullable=False),
        sa.Column("tool_alias", sa.String(length=64), nullable=False),
        sa.Column("tool_version_id", _BIGINT, nullable=False),
        sa.Column("kpi_name", sa.String(length=64), nullable=False),
        sa.Column("value_numeric", sa.Numeric(precision=38, scale=12), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_kpi_value")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_job_kpi_value_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tool_version_id"],
            ["tool_version.id"],
            name=op.f("fk_job_kpi_value_tool_version_id_tool_version"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "job_id",
            "kpi_name",
            name=op.f("uq_job_kpi_value_job_kpi"),
        ),
    )
    op.create_index(
        "ix_job_kpi_value_alias_kpi",
        "job_kpi_value",
        ["tool_alias", "kpi_name"],
    )
    op.create_index(
        "ix_job_kpi_value_extracted_at",
        "job_kpi_value",
        ["extracted_at"],
    )


def downgrade() -> None:
    """Drop the ``job_kpi_value`` table + its indexes."""
    op.drop_index("ix_job_kpi_value_extracted_at", table_name="job_kpi_value")
    op.drop_index("ix_job_kpi_value_alias_kpi", table_name="job_kpi_value")
    op.drop_table("job_kpi_value")
