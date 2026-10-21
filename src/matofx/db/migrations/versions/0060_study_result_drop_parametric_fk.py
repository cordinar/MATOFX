"""Studies engine: finish the ``study_result`` rename (drop the old FK).

Revision ID: 0060
Revises: 0059
Create Date: 2026-06-02

Step **two** of the studies-engine §6 two-step rename (step one was
0058). Now that every consumer reads the unified store through the
``(source_kind, source_id)`` discriminator (the ``record_study_result``
write hook + ``ResultsService`` / ``SeriesService`` reads, all keyed on
``Job.parametric_study_id`` — the *job* column, untouched), the legacy
``study_result.parametric_study_id`` column is dropped:

- it was ``NOT NULL`` + FK to ``parametric_study``, so a ``cohort`` /
  ``optimisation`` row (no parametric study) literally could not be
  written while it existed — this unblocks the cohort lazy populate
  (surrogate §3.3) and the optimisation children (ADR-0009);
- the ``UNIQUE(job_id)`` constraint widens to
  ``UNIQUE(source_kind, source_id, job_id)`` so **one job may appear
  under several sources** (a completed job that is both a parametric
  child and a cohort member gets one row per source, §3.3).

The draft-reaping cascade is preserved: ``study_result.job_id`` still
carries its ``ON DELETE CASCADE`` to ``job`` (0041), so reaping a draft
job still removes its results row — the dropped ``parametric_study_id``
cascade was redundant with it.

SQLite can't drop a column / swap a constraint in place, so this goes
through a recreate-style ``batch_alter_table`` (the 0055 technique). The
single-column index + FK on ``parametric_study_id`` are dropped
explicitly before the column so the recreate never tries to reproduce
them against a missing column. No CHECK changes; no data backfill (the
discriminator already carries every row's source from 0058).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

_FK_NAME = "fk_parametric_study_result_parametric_study_id_parametric_study"
_IX_NAME = "ix_parametric_study_result_parametric_study_id"
_UQ_JOB = "uq_parametric_study_result_job_id"
_UQ_SOURCE = "uq_study_result_source_job"


def upgrade() -> None:
    """Drop ``parametric_study_id``; widen the per-job UNIQUE to per-source."""
    with op.batch_alter_table("study_result", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint(_UQ_JOB, type_="unique")
        batch.drop_index(_IX_NAME)
        batch.drop_constraint(_FK_NAME, type_="foreignkey")
        batch.drop_column("parametric_study_id")
        batch.create_unique_constraint(_UQ_SOURCE, ["source_kind", "source_id", "job_id"])


def downgrade() -> None:
    """Re-add ``parametric_study_id`` (backfilled from parametric rows).

    Re-imposing the original ``NOT NULL`` FK requires a value for every
    row. Only ``source_kind='parametric'`` rows exist before any cohort /
    optimisation producer ships, so backfilling ``= source_id`` restores
    the exact pre-0060 shape. Any non-parametric rows (none at this
    revision) are deleted first, mirroring the one-way downgrades 0055 /
    0049 use for added-target rows.
    """
    op.execute("DELETE FROM study_result WHERE source_kind <> 'parametric'")
    with op.batch_alter_table("study_result", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint(_UQ_SOURCE, type_="unique")
        batch.add_column(sa.Column("parametric_study_id", _BIGINT, nullable=True))
    op.execute("UPDATE study_result SET parametric_study_id = source_id")
    with op.batch_alter_table("study_result", recreate=recreate_on_sqlite()) as batch:
        batch.alter_column("parametric_study_id", existing_type=_BIGINT, nullable=False)
        batch.create_foreign_key(
            _FK_NAME,
            "parametric_study",
            ["parametric_study_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint(_UQ_JOB, ["job_id"])
    op.create_index(_IX_NAME, "study_result", ["parametric_study_id"])
