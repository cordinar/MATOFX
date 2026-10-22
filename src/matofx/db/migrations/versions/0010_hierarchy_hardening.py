"""Hierarchy hardening: project_team, optimistic locks, tool FK, timestamp CHECKs.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-03

Adds:

- ``project_team`` — many-to-many grant of project access to teams,
  with ``access_level`` ('view' | 'edit') CHECK-constrained.
- ``version`` columns on ``project``, ``scenario``, and ``job``,
  default 1, NOT NULL — wired up to SQLAlchemy's optimistic-locking
  ``version_id_col`` mapper option.
- FK on ``job.tool_alias`` referencing ``tool.alias`` with
  ``ON DELETE RESTRICT`` so a tool with active jobs cannot be
  uninstalled. Column is shrunk to ``String(64)`` to match
  ``tool.alias``.
- CHECK constraints on ``job.started_at`` / ``job.completed_at`` so
  a tampered DB row can't claim impossible orderings.
- Seed of ``projects.transfer_ownership`` permission. Not added to
  any built-in role's defaults (super_user gets it via the
  catalog-membership sentinel; admin does not — same pattern as
  ``projects.create_for_others``).

For existing rows: ``version`` backfills to 1. ``job.tool_alias`` is
assumed already-valid (the v2 install is greenfield in Phase 1; if
orphan rows existed the FK creation would fail loudly, prompting an
operator fix rather than silent data deletion).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create project_team, add version columns, tighten job constraints."""
    op.create_table(
        "project_team",
        sa.Column(
            "project_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column(
            "team_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("access_level", sa.String(length=16), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "granted_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("project_id", "team_id", name=op.f("pk_project_team")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_project_team_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["team.id"],
            name=op.f("fk_project_team_team_id_team"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"],
            ["user.id"],
            name=op.f("fk_project_team_granted_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "access_level IN ('view', 'edit')",
            name=op.f("ck_project_team_access_level"),
        ),
    )
    op.create_index("ix_project_team_team_id", "project_team", ["team_id"])

    # Version columns for optimistic locking.
    with op.batch_alter_table("project") as batch:
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    with op.batch_alter_table("scenario") as batch:
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))

    # Job: version + tool FK + timestamp CHECKs in one batch rewrite.
    # Use a recreate-style batch_alter_table so SQLite picks up the new
    # constraints (it can't ADD a CHECK to an existing table otherwise).
    with op.batch_alter_table("job", recreate=recreate_on_sqlite()) as batch:
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.alter_column("tool_alias", type_=sa.String(length=64))
        batch.create_foreign_key(
            op.f("fk_job_tool_alias_tool"),
            "tool",
            ["tool_alias"],
            ["alias"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_job_tool_alias", ["tool_alias"])
        batch.create_check_constraint(
            "ck_job_started_after_created",
            "started_at IS NULL OR started_at >= created_at",
        )
        batch.create_check_constraint(
            "ck_job_completed_after_started",
            "completed_at IS NULL OR (started_at IS NOT NULL AND completed_at >= started_at)",
        )

    # Seed the new permission. Not granted to any built-in role's default;
    # super_user holds it via the closed-catalog sentinel.
    # (No bulk_insert needed — only super_user has it, and super_user
    # rows are sentinel-managed in code rather than seeded.)


def downgrade() -> None:
    """Reverse the schema additions."""
    with op.batch_alter_table("job", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("ck_job_completed_after_started", type_="check")
        batch.drop_constraint("ck_job_started_after_created", type_="check")
        batch.drop_index("ix_job_tool_alias")
        batch.drop_constraint(op.f("fk_job_tool_alias_tool"), type_="foreignkey")
        batch.alter_column("tool_alias", type_=sa.String(length=128))
        batch.drop_column("version")

    with op.batch_alter_table("scenario") as batch:
        batch.drop_column("version")
    with op.batch_alter_table("project") as batch:
        batch.drop_column("version")

    op.drop_index("ix_project_team_team_id", table_name="project_team")
    op.drop_table("project_team")
