"""Project access model rework (ADR-0002), part 2: project_acl.

Revision ID: 0034
Revises: 0033
Create Date: 2026-05-17

Creates ``project_acl`` — the per-user grant table that replaces the
dropped ``project_team`` (0033). One row per ``(project, user)``;
``role`` is CHECK-constrained to ``owner`` / ``member`` / ``viewer``.
The Creator is *not* stored here (it is ``project.creator_user_id``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | None = None
depends_on: str | None = None


def _bigint() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Create project_acl."""
    op.create_table(
        "project_acl",
        sa.Column("project_id", _bigint(), nullable=False),
        sa.Column("user_id", _bigint(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_by_user_id", _bigint(), nullable=True),
        sa.PrimaryKeyConstraint("project_id", "user_id", name=op.f("pk_project_acl")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_project_acl_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_project_acl_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"],
            ["user.id"],
            name=op.f("fk_project_acl_granted_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'member', 'viewer')",
            name=op.f("ck_project_acl_role"),
        ),
    )
    op.create_index("ix_project_acl_user_id", "project_acl", ["user_id"])


def downgrade() -> None:
    """Drop project_acl."""
    op.drop_index("ix_project_acl_user_id", table_name="project_acl")
    op.drop_table("project_acl")
