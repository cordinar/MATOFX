"""Studies engine: rename ``parametric_study_result`` → ``study_result``.

Revision ID: 0058
Revises: 0057
Create Date: 2026-06-02

Backs ``designs/studies-engine.md`` §6 + ``designs/surrogate-family.md``
§5.6 / §17 Phase 1 (ADR-0008) — the shared substrate the surrogate
family **and** the optimisation framework (ADR-0009) both consume. The
single per-child results store grows a discriminator so the three study
siblings (parametric / optimisation / cohort) can all feed one
``study_result`` table read by one Results explorer:

- the table is renamed ``parametric_study_result`` → ``study_result``;
- ``source_kind`` (TEXT, CHECK in ``parametric`` / ``optimisation`` /
  ``cohort``) + ``source_id`` (the logical FK target — the kind decides
  which sibling table it points at, an un-FK'd discriminator pair, the
  same pattern the approval-target columns use) are added;
- existing rows backfill ``source_kind='parametric'`` and
  ``source_id = parametric_study_id``.

This is **step one of the standard two-step rename** (studies-engine
§6): the old ``parametric_study_id`` column is *kept* here so the
current consumers (``record_study_result`` write hook, ``ResultsService``
/ ``SeriesService`` reads, all keyed on ``Job.parametric_study_id``)
keep working unchanged. A later migration drops ``parametric_study_id``
once every consumer reads via ``(source_kind, source_id)``.

SQLite can't add a NOT NULL column with no default to a populated table,
nor add a CHECK in place, so the NOT NULL + CHECK step goes through a
recreate-style ``batch_alter_table`` — the exact technique migrations
0038 / 0046 / 0049 / 0055 use. The backfill runs between adding the
nullable columns and tightening them, so the recreate's data copy never
sees a NULL in a NOT NULL column. CHECK body is a **frozen literal** (a
migration must not import the ORM). The pre-existing FK / unique /
index names are preserved through the rename + recreate (reflection
reproduces them), so ``uq_parametric_study_result_job_id`` and the
``parametric_study`` / ``job`` cascades survive intact.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# Frozen literal — do NOT import the ORM CHECK body (studies-engine §6).
_SOURCE_KIND_CHECK = "source_kind IN ('parametric', 'optimisation', 'cohort')"


def upgrade() -> None:
    """Rename the store, add + backfill the ``(source_kind, source_id)`` discriminator."""
    op.rename_table("parametric_study_result", "study_result")

    # Add nullable first so the populated table accepts the new columns;
    # tighten to NOT NULL + CHECK after the backfill.
    op.add_column("study_result", sa.Column("source_kind", sa.String(16), nullable=True))
    op.add_column("study_result", sa.Column("source_id", _BIGINT, nullable=True))

    op.execute(
        "UPDATE study_result SET source_kind = 'parametric', source_id = parametric_study_id"
    )

    with op.batch_alter_table("study_result", recreate=recreate_on_sqlite()) as batch:
        batch.alter_column("source_kind", existing_type=sa.String(16), nullable=False)
        batch.alter_column("source_id", existing_type=_BIGINT, nullable=False)
        batch.create_check_constraint("ck_study_result_source_kind", _SOURCE_KIND_CHECK)

    # Index the discriminator the unified Results explorer filters on.
    op.create_index(
        "ix_study_result_source_kind_source_id",
        "study_result",
        ["source_kind", "source_id"],
    )


def downgrade() -> None:
    """Reverse — drop the discriminator + index, rename back to the parametric store.

    Lossless: every row's ``source_kind`` is ``'parametric'`` at this
    revision (the only producer so far), so dropping the discriminator
    leaves the original ``parametric_study_result`` shape with no orphan.
    """
    op.drop_index("ix_study_result_source_kind_source_id", table_name="study_result")
    with op.batch_alter_table("study_result", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("ck_study_result_source_kind", type_="check")
        batch.drop_column("source_id")
        batch.drop_column("source_kind")
    op.rename_table("study_result", "parametric_study_result")
