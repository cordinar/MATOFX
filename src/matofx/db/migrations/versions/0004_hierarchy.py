"""Project, scenario, job tables.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-03

Creates ``project``, ``scenario``, and ``job`` with cascading FKs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create hierarchy tables."""
    op.create_table(
        "project",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "owner_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project")),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            name=op.f("fk_project_owner_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("owner_user_id", "name", name="uq_project_owner_name"),
    )
    op.create_index(op.f("ix_project_owner_user_id"), "project", ["owner_user_id"])

    op.create_table(
        "scenario",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column(
            "project_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scenario")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_scenario_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("project_id", "name", name="uq_scenario_project_name"),
    )
    op.create_index(op.f("ix_scenario_project_id"), "scenario", ["project_id"])

    op.create_table(
        "job",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column(
            "scenario_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("tool_alias", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job")),
        sa.ForeignKeyConstraint(
            ["scenario_id"],
            ["scenario.id"],
            name=op.f("fk_job_scenario_id_scenario"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_job_created_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('created','queued','running','complete','failed')",
            name="ck_job_state",
        ),
    )
    op.create_index(op.f("ix_job_scenario_id"), "job", ["scenario_id"])
    op.create_index("ix_job_state", "job", ["state"])


def downgrade() -> None:
    """Drop hierarchy tables."""
    op.drop_index("ix_job_state", table_name="job")
    op.drop_index(op.f("ix_job_scenario_id"), table_name="job")
    op.drop_table("job")
    op.drop_index(op.f("ix_scenario_project_id"), table_name="scenario")
    op.drop_table("scenario")
    op.drop_index(op.f("ix_project_owner_user_id"), table_name="project")
    op.drop_table("project")
