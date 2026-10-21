"""Workflow-target optimisation: study scenario home + many runs per workflow.

Revision ID: 0068
Revises: 0067
Create Date: 2026-06-08

Phase 11 (ADR-0009 §3.4) lets an optimisation study optimise a whole
workflow chain. Each candidate is a :class:`WorkflowRun`, and a run (plus
its child jobs' 1-of-3 home CHECK) needs a **scenario**. A study is a
*project* child with no scenario, so a workflow-target study auto-creates
**one dedicated scenario** in its project at ``configure`` to host every
candidate run (the maintainer's chosen model). Two schema changes:

- ``optimisation_study.scenario_id`` (nullable FK RESTRICT) — the
  dedicated scenario for a ``workflow`` target; ``NULL`` for a ``tool``
  target (whose children are scenario-less, homed on the study itself).

- ``workflow_run``'s ``(scenario_id, workflow_id)`` uniqueness is
  **relaxed to a partial unique index** that applies only to manual runs
  (``WHERE optimisation_study_id IS NULL``). A manual scenario still
  hosts at most one run per workflow; an optimisation study runs the same
  workflow many times in its dedicated scenario (one run per candidate).

SQLite can't ADD an FK or swap a table constraint in place, so both go
through recreate-style ``batch_alter_table`` (migrations 0010 / 0031 /
0032 / 0066 / 0067). Greenfield-safe.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_MANUAL_RUN_PREDICATE = "optimisation_study_id IS NULL"


def upgrade() -> None:
    """Add the study scenario FK + relax workflow_run uniqueness to manual runs."""
    with op.batch_alter_table("optimisation_study", recreate=recreate_on_sqlite()) as batch:
        batch.add_column(sa.Column("scenario_id", _BIGINT, nullable=True))
        batch.create_foreign_key(
            op.f("fk_optimisation_study_scenario_id_scenario"),
            "scenario",
            ["scenario_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    # Swap the table-level unique constraint for a partial unique index so
    # only manual runs are one-per-workflow-per-scenario; optimisation
    # candidate runs share the study's dedicated scenario.
    with op.batch_alter_table("workflow_run", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("uq_workflow_run_scenario_workflow", type_="unique")
    op.create_index(
        "uq_workflow_run_scenario_workflow",
        "workflow_run",
        ["scenario_id", "workflow_id"],
        unique=True,
        sqlite_where=sa.text(_MANUAL_RUN_PREDICATE),
        postgresql_where=sa.text(_MANUAL_RUN_PREDICATE),
    )


def downgrade() -> None:
    """Restore the full unique constraint + drop the study scenario FK."""
    op.drop_index("uq_workflow_run_scenario_workflow", table_name="workflow_run")
    with op.batch_alter_table("workflow_run", recreate=recreate_on_sqlite()) as batch:
        batch.create_unique_constraint(
            "uq_workflow_run_scenario_workflow", ["scenario_id", "workflow_id"]
        )

    with op.batch_alter_table("optimisation_study", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint(
            op.f("fk_optimisation_study_scenario_id_scenario"), type_="foreignkey"
        )
        batch.drop_column("scenario_id")
