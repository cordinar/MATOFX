"""Parametric studies: per-child results store.

Revision ID: 0041
Revises: 0040
Create Date: 2026-05-20

Backs ``designs/parametric-studies.md`` §10.0 (2026-05-19 amendment)
+ ADR-0001 revision + ``decisions/0003-job-io-document-retention.md``:
the durable per-child ``parametric_study_result`` store the runner
writes at ``_post_complete`` **before** ``clean_directory`` runs.
One row per child job (UNIQUE on ``job_id``); ``scalars`` holds
every numeric + string output from the child's ``OutputsDocument``
(read by 2-PS-b0's response picker, 2-PS-b's plots/sensitivity, and
2-PS-c's surrogate); ``retained_files`` references descriptor
``csv_plot`` + ``retain_files`` artefacts that survived the
ADR-0003 retention floor (drill-down + curve-overlay target these).
Cascades on study/job delete (drafts get reaped per D20).

Greenfield insert — no data backfill is needed: existing study
children pre-date the store and their ``parametric_study_result``
rows will only appear when a child finishes after this migration is
applied. No CHECK / nullability changes on existing tables, so a
plain ``op.create_table`` works on both SQLite and Postgres — no
recreate-style ``batch_alter_table`` (cf. the 0031 / 0039 template
that *modifies* an existing CHECK).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Create the ``parametric_study_result`` table + its index."""
    op.create_table(
        "parametric_study_result",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("parametric_study_id", _BIGINT, nullable=False),
        sa.Column("job_id", _BIGINT, nullable=False),
        sa.Column("scalars", sa.JSON(), nullable=False),
        sa.Column("retained_files", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parametric_study_result")),
        sa.ForeignKeyConstraint(
            ["parametric_study_id"],
            ["parametric_study.id"],
            name=op.f("fk_parametric_study_result_parametric_study_id_parametric_study"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_parametric_study_result_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("job_id", name=op.f("uq_parametric_study_result_job_id")),
    )
    op.create_index(
        "ix_parametric_study_result_parametric_study_id",
        "parametric_study_result",
        ["parametric_study_id"],
    )


def downgrade() -> None:
    """Drop the ``parametric_study_result`` table and its index."""
    op.drop_index(
        "ix_parametric_study_result_parametric_study_id",
        table_name="parametric_study_result",
    )
    op.drop_table("parametric_study_result")
