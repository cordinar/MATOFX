"""Project access model rework (ADR-0002), part 1.

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-17

ADR-0002 supersedes the Phase 1 team-keyed project ACL with a per-user
four-role model and adds the §D20 no-delete ``state`` lifecycle. This
revision:

- **drops ``project_team``** — teams are no longer an ACL principal.
  The v2 install is pre-pilot so there is no production grant data to
  migrate; the cut is clean (design migration §2.5).
- **renames ``project.owner_user_id`` → ``creator_user_id``** (the
  immutable Creator) and moves the per-creator name uniqueness +
  index + FK onto the new column name.
- **adds the ``state`` / ``state_changed_at`` /
  ``state_changed_by_user_id`` trio** (CHECK-constrained) to
  ``project`` and ``scenario``; existing rows backfill to ``active``
  via the column server-default.

SQLite can't ALTER a CHECK / rename a column in a live table, so the
changes go through recreate-style ``batch_alter_table`` (as 0010 /
0031). The column rename and the constraints that reference the new
name are done in **separate batches** — Alembic resolves
constraints added in the same batch against the *pre-rename* column
names, which silently drops them (learned the hard way). Constraints
use literal names (as 0010 / 0031) — not the metadata naming
convention, which would double the ``ck_``/``uq_`` prefixes inside a
batch.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | None = None
depends_on: str | None = None

_PROJECT_STATE_CHECK = "state IN ('active','archived','cancelled')"


def _bigint() -> sa.types.TypeEngine[Any]:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _add_state_trio(table: str) -> None:
    with op.batch_alter_table(table, recreate="always") as batch:
        batch.add_column(
            sa.Column("state", sa.String(length=16), nullable=False, server_default="active")
        )
        batch.add_column(sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("state_changed_by_user_id", _bigint(), nullable=True))
        batch.create_foreign_key(
            f"fk_{table}_state_changed_by_user_id_user",
            "user",
            ["state_changed_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(f"ck_{table}_state", _PROJECT_STATE_CHECK)


def upgrade() -> None:
    """Drop project_team; rename creator; add the state trio."""
    # 1. The superseded team-keyed ACL. Nothing references project_team.
    op.drop_index("ix_project_team_team_id", table_name="project_team")
    op.drop_table("project_team")

    # 2a. project: rename owner_user_id → creator_user_id (rename-only
    #     batch — drop the old name-scoped unique + FK with it).
    op.drop_index(op.f("ix_project_owner_user_id"), table_name="project")
    with op.batch_alter_table("project", recreate="always") as batch:
        batch.drop_constraint("uq_project_owner_name", type_="unique")
        batch.drop_constraint("fk_project_owner_user_id_user", type_="foreignkey")
        batch.alter_column("owner_user_id", new_column_name="creator_user_id")

    # 2b. project: now that the column is creator_user_id, add the state
    #     trio + the creator FK/unique/index that reference it.
    with op.batch_alter_table("project", recreate="always") as batch:
        batch.add_column(
            sa.Column("state", sa.String(length=16), nullable=False, server_default="active")
        )
        batch.add_column(sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("state_changed_by_user_id", _bigint(), nullable=True))
        batch.create_foreign_key(
            "fk_project_creator_user_id_user",
            "user",
            ["creator_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_project_state_changed_by_user_id_user",
            "user",
            ["state_changed_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_unique_constraint("uq_project_creator_name", ["creator_user_id", "name"])
        batch.create_check_constraint("ck_project_state", _PROJECT_STATE_CHECK)
    op.create_index(op.f("ix_project_creator_user_id"), "project", ["creator_user_id"])

    # 3. scenario: state trio (CHECK needs the recreate path on SQLite).
    _add_state_trio("scenario")


def downgrade() -> None:
    """Reverse: drop the state trio, rename back, recreate project_team."""
    with op.batch_alter_table("scenario", recreate="always") as batch:
        batch.drop_constraint("ck_scenario_state", type_="check")
        batch.drop_constraint("fk_scenario_state_changed_by_user_id_user", type_="foreignkey")
        batch.drop_column("state_changed_by_user_id")
        batch.drop_column("state_changed_at")
        batch.drop_column("state")

    op.drop_index(op.f("ix_project_creator_user_id"), table_name="project")
    with op.batch_alter_table("project", recreate="always") as batch:
        batch.drop_constraint("ck_project_state", type_="check")
        batch.drop_constraint("uq_project_creator_name", type_="unique")
        batch.drop_constraint("fk_project_state_changed_by_user_id_user", type_="foreignkey")
        batch.drop_constraint("fk_project_creator_user_id_user", type_="foreignkey")
        batch.drop_column("state_changed_by_user_id")
        batch.drop_column("state_changed_at")
        batch.drop_column("state")
    with op.batch_alter_table("project", recreate="always") as batch:
        batch.alter_column("creator_user_id", new_column_name="owner_user_id")
        batch.create_foreign_key(
            "fk_project_owner_user_id_user",
            "user",
            ["owner_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint("uq_project_owner_name", ["owner_user_id", "name"])
    op.create_index(op.f("ix_project_owner_user_id"), "project", ["owner_user_id"])

    op.create_table(
        "project_team",
        sa.Column("project_id", _bigint(), nullable=False),
        sa.Column("team_id", _bigint(), nullable=False),
        sa.Column("access_level", sa.String(length=16), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_by_user_id", _bigint(), nullable=True),
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
