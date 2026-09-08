"""Job Cohorts: drop the home ``project_id`` — a workspace-level object.

Revision ID: 0075
Revises: 0074
Create Date: 2026-06-11

Backs ``designs/job-cohorts.md`` D5 / §5. A cohort is no longer homed
under a project: governance moved to the ``cohorts.curate`` capability
(migration 0074), so the home ``project_id`` lost its only job (the ACL
source) and is removed here. ``project_ids`` survives as the optional
*positive* project filter (``NULL`` = search the whole workspace).

**Scope preservation.** A pre-existing cohort with ``project_ids IS
NULL`` was scoped to its home project (resolve defaulted to
``[project_id]``). Dropping ``project_id`` would silently widen those to
the whole workspace, so the upgrade first pins ``project_ids =
[project_id]`` for every such row. The backfill runs per-row through a
typed table construct so the JSON serialises correctly on both SQLite and
Postgres (no dialect-specific ``json_array``).

The column drop goes through ``batch_alter_table`` so SQLite rebuilds the
table without ``project_id`` and its ``fk_job_cohort_project_id_project``
FK; the ``ix_job_cohort_project_id_state`` listing index is replaced by a
plain ``ix_job_cohort_state`` (the workspace list filters on state only).

**Downgrade** is best-effort: it re-adds ``project_id`` (nullable),
backfills it from the first ``project_ids`` entry, and restores the FK +
index — but it cannot reconstruct a home for cohorts created
workspace-wide (``project_ids IS NULL``), so the column is left nullable
rather than forced back to NOT NULL. Lossy by nature (the original drop
discarded the home).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _cohort_table() -> sa.TableClause:
    return sa.table(
        "job_cohort",
        sa.column("id", _BIGINT),
        sa.column("project_id", _BIGINT),
        sa.column("project_ids", sa.JSON()),
    )


def upgrade() -> None:
    """Pin implicit scope, then drop the home ``project_id`` column."""
    bind = op.get_bind()
    jc = _cohort_table()

    # Preserve the implicit "home project only" scope before the column goes.
    rows = bind.execute(
        sa.select(jc.c.id, jc.c.project_id).where(jc.c.project_ids.is_(None))
    ).fetchall()
    for rid, pid in rows:
        bind.execute(sa.update(jc).where(jc.c.id == rid).values(project_ids=[pid]))

    op.drop_index("ix_job_cohort_project_id_state", table_name="job_cohort")
    with op.batch_alter_table("job_cohort") as batch:
        batch.drop_column("project_id")
    op.create_index("ix_job_cohort_state", "job_cohort", ["state"])


def downgrade() -> None:
    """Best-effort restore of the home ``project_id`` (left nullable)."""
    bind = op.get_bind()

    op.drop_index("ix_job_cohort_state", table_name="job_cohort")
    op.add_column("job_cohort", sa.Column("project_id", _BIGINT, nullable=True))

    jc = _cohort_table()
    rows = bind.execute(sa.select(jc.c.id, jc.c.project_ids)).fetchall()
    for rid, project_ids in rows:
        pid = project_ids[0] if project_ids else None
        bind.execute(sa.update(jc).where(jc.c.id == rid).values(project_id=pid))

    with op.batch_alter_table("job_cohort") as batch:
        batch.create_foreign_key(
            "fk_job_cohort_project_id_project",
            "project",
            ["project_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index("ix_job_cohort_project_id_state", "job_cohort", ["project_id", "state"])
