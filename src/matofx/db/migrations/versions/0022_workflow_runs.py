"""Workflow runs: workflow_run table + job.workflow_run_id wiring.

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-11

The workflow_run table tracks one execution of a workflow inside a
scenario. Each run owns N child jobs (one per workflow member). The
existing ``job.uq_job_scenario_tool`` constraint relaxes into a pair
of *partial* unique indexes so workflow children + standalone jobs
can coexist:

- ``uq_job_scenario_tool_standalone`` — one job per (scenario, tool)
  when ``workflow_run_id IS NULL`` (i.e. a direct tool submission).
- ``uq_job_workflow_run_tool`` — one job per (workflow_run, tool)
  when ``workflow_run_id IS NOT NULL``. Workflow definitions already
  refuse duplicate members; this mirrors it at the DB layer.

State for a workflow_run isn't stored — it's derived from the child
jobs at read time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create workflow_run + add job wiring + swap unique constraint."""
    op.create_table(
        "workflow_run",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column(
            "workflow_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("workflow_version_snapshot", sa.String(length=32), nullable=False),
        sa.Column(
            "scenario_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "submitted_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_requested_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_run")),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflow.id"],
            name=op.f("fk_workflow_run_workflow_id_workflow"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scenario_id"],
            ["scenario.id"],
            name=op.f("fk_workflow_run_scenario_id_scenario"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_user_id"],
            ["user.id"],
            name=op.f("fk_workflow_run_submitted_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cancel_requested_by_user_id"],
            ["user.id"],
            name=op.f("fk_workflow_run_cancel_requested_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "scenario_id",
            "workflow_id",
            name="uq_workflow_run_scenario_workflow",
        ),
    )
    op.create_index("ix_workflow_run_scenario_id", "workflow_run", ["scenario_id"])
    op.create_index("ix_workflow_run_workflow_id", "workflow_run", ["workflow_id"])

    # Mutate the job table: add the new columns, swap the unique
    # constraint for two partial unique indexes. ``batch_alter_table``
    # handles SQLite's "must recreate the table to drop a constraint"
    # quirk transparently and is a no-op on Postgres.
    with op.batch_alter_table("job") as batch:
        batch.add_column(
            sa.Column(
                "workflow_run_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                sa.ForeignKey(
                    "workflow_run.id",
                    name=op.f("fk_job_workflow_run_id_workflow_run"),
                    ondelete="RESTRICT",
                ),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("workflow_position", sa.Integer(), nullable=True))
        # The previous unconditional unique constraint has to go so
        # workflow-child jobs can share a tool across two runs in the
        # same scenario.
        batch.drop_constraint("uq_job_scenario_tool", type_="unique")

    op.create_index(
        "ix_job_workflow_run_id",
        "job",
        ["workflow_run_id"],
    )
    op.create_index(
        "uq_job_scenario_tool_standalone",
        "job",
        ["scenario_id", "tool_alias"],
        unique=True,
        sqlite_where=sa.text("workflow_run_id IS NULL"),
        postgresql_where=sa.text("workflow_run_id IS NULL"),
    )
    op.create_index(
        "uq_job_workflow_run_tool",
        "job",
        ["workflow_run_id", "tool_alias"],
        unique=True,
        sqlite_where=sa.text("workflow_run_id IS NOT NULL"),
        postgresql_where=sa.text("workflow_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Reverse: drop the new tables / columns / indexes, restore the old constraint."""
    op.drop_index("uq_job_workflow_run_tool", table_name="job")
    op.drop_index("uq_job_scenario_tool_standalone", table_name="job")
    op.drop_index("ix_job_workflow_run_id", table_name="job")
    with op.batch_alter_table("job") as batch:
        batch.create_unique_constraint("uq_job_scenario_tool", ["scenario_id", "tool_alias"])
        batch.drop_column("workflow_position")
        batch.drop_column("workflow_run_id")
    op.drop_index("ix_workflow_run_workflow_id", table_name="workflow_run")
    op.drop_index("ix_workflow_run_scenario_id", table_name="workflow_run")
    op.drop_table("workflow_run")
