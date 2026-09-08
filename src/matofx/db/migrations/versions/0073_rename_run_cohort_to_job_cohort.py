"""Job Cohorts: rename ``run_cohort`` → ``job_cohort`` (+ its indexes).

Revision ID: 0073
Revises: 0072
Create Date: 2026-06-11

Backs ``designs/job-cohorts.md`` D1 — the "Run Cohorts" → "Job Cohorts"
rename. The user-facing feature, the ORM class (``RunCohort`` →
``JobCohort``), and the table are renamed together so nothing in the
stack still says "run cohort".

Scope of this migration: the **table** and its two listing **indexes**.
The internal constraint names (``pk_run_cohort`` / the four
``fk_run_cohort_*`` / ``ck_run_cohort_state``) are deliberately left
unchanged — exactly as migration 0058 preserved ``study_result``'s
reflected constraint names through its table rename. Runtime queries key
on table + column names (which the ORM drives from ``__tablename__``),
never on constraint names; the only DB test is the
``alembic upgrade head`` / ``downgrade base`` roundtrip, which a plain
rename satisfies on both SQLite and Postgres. Relabelling the
constraints would force a fragile ``batch_alter_table`` recreate for no
functional gain — a trivial follow-up if full cosmetic consistency is
ever wanted.

No data moves: ``rename_table`` is metadata-only, and no other table
carries a hard FK *to* ``run_cohort`` (``study_result`` references a
cohort only through its un-FK'd ``(source_kind='cohort', source_id)``
discriminator pair, which is a value, not a constraint).
"""

from __future__ import annotations

from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Rename the table and re-create its indexes under the new name."""
    op.drop_index("ix_run_cohort_tool_alias", table_name="run_cohort")
    op.drop_index("ix_run_cohort_project_id_state", table_name="run_cohort")
    op.rename_table("run_cohort", "job_cohort")
    op.create_index("ix_job_cohort_project_id_state", "job_cohort", ["project_id", "state"])
    op.create_index("ix_job_cohort_tool_alias", "job_cohort", ["tool_alias"])


def downgrade() -> None:
    """Reverse — rename back to ``run_cohort`` with its original indexes."""
    op.drop_index("ix_job_cohort_tool_alias", table_name="job_cohort")
    op.drop_index("ix_job_cohort_project_id_state", table_name="job_cohort")
    op.rename_table("job_cohort", "run_cohort")
    op.create_index("ix_run_cohort_project_id_state", "run_cohort", ["project_id", "state"])
    op.create_index("ix_run_cohort_tool_alias", "run_cohort", ["tool_alias"])
