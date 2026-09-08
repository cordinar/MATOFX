"""Headless workflow orchestration: auto-advance + open inputs + optimisation home.

Revision ID: 0067
Revises: 0066
Create Date: 2026-06-08

The workflow engine was UI-driven only: a run's child jobs sat in
``created`` until a human submitted each tab, and upstream→downstream
value transfer happened in the browser form, never reaching the DB. The
optimisation runtime (ADR-0009 §3.4, Phase 11) is a headless daemon, so a
``workflow`` target needs a run that drives itself to completion. This
revision adds the three columns that make a run headless-capable:

- ``workflow_run.auto_advance`` (bool, default false) — when set, the
  runner queues each child as soon as its upstream members complete
  (``WorkflowRunService.advance``), resolving wired inputs from upstream
  outputs server-side. Manual UI runs leave it false and are unchanged.
- ``workflow_run.open_inputs_json`` (JSON, nullable) — the open
  (non-wired) input values for an auto-advancing run, keyed
  ``{tool_alias: {input_name: value}}``. For an optimisation candidate
  this is the proposed factor vector.
- ``workflow_run.optimisation_study_id`` (nullable FK RESTRICT, indexed)
  — run-level membership for the ``workflow`` target (§3.4 / §12.2). The
  child ``Job`` rows keep their normal scenario home; only the run
  carries the study FK, so the job 1-of-3 home CHECK is untouched.

SQLite can't ADD an FK in place, so the change goes through a
recreate-style ``batch_alter_table`` (the technique migrations 0010 /
0031 / 0032 / 0066 used). Greenfield-safe: the v2 install has no
``workflow_run`` rows that conflict.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Add the three headless-orchestration columns to ``workflow_run``."""
    with op.batch_alter_table("workflow_run", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "auto_advance",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("open_inputs_json", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("optimisation_study_id", _BIGINT, nullable=True))
        batch.create_foreign_key(
            op.f("fk_workflow_run_optimisation_study_id_optimisation_study"),
            "optimisation_study",
            ["optimisation_study_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_workflow_run_optimisation_study_id", ["optimisation_study_id"]
        )


def downgrade() -> None:
    """Drop the headless-orchestration columns + their FK/index."""
    with op.batch_alter_table("workflow_run", recreate="always") as batch:
        batch.drop_index("ix_workflow_run_optimisation_study_id")
        batch.drop_constraint(
            op.f("fk_workflow_run_optimisation_study_id_optimisation_study"),
            type_="foreignkey",
        )
        batch.drop_column("optimisation_study_id")
        batch.drop_column("open_inputs_json")
        batch.drop_column("auto_advance")
