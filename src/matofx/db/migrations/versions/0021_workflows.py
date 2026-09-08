"""Workflow / workflow_member / workflow_version tables + workflows.manage perm.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-11

Admin-released workflows: a workflow groups N tools into an ordered
sequence that surfaces on a scenario as a tabbed run. See
``project_workflows_design.md`` (memory) for the locked decisions.
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | None = None
depends_on: str | None = None


_NEW_ADMIN_PERMISSIONS: tuple[str, ...] = ("workflows.manage",)


def upgrade() -> None:
    """Create the three workflow tables + seed the new permission."""
    op.create_table(
        "workflow",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("alias", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("descriptor", sa.JSON(), nullable=False),
        sa.Column(
            "installed_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "archived_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column("archive_reason", sa.Text(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow")),
        sa.UniqueConstraint("alias", name=op.f("uq_workflow_alias")),
        sa.ForeignKeyConstraint(
            ["installed_by_user_id"],
            ["user.id"],
            name=op.f("fk_workflow_installed_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["archived_by_user_id"],
            ["user.id"],
            name=op.f("fk_workflow_archived_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "state IN ('new','test','released')",
            name="ck_workflow_state",
        ),
    )
    op.create_index("ix_workflow_state", "workflow", ["state"])
    op.create_index("ix_workflow_archived_at", "workflow", ["archived_at"])

    op.create_table(
        "workflow_member",
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
        sa.Column("tool_alias", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_member")),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflow.id"],
            name=op.f("fk_workflow_member_workflow_id_workflow"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tool_alias"],
            ["tool.alias"],
            name=op.f("fk_workflow_member_tool_alias_tool"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workflow_id",
            "position",
            name="uq_workflow_member_workflow_id_position",
        ),
        sa.UniqueConstraint(
            "workflow_id",
            "tool_alias",
            name="uq_workflow_member_workflow_id_tool_alias",
        ),
        sa.CheckConstraint("position >= 1", name="ck_workflow_member_position_positive"),
    )
    op.create_index("ix_workflow_member_workflow_id", "workflow_member", ["workflow_id"])
    op.create_index("ix_workflow_member_tool_alias", "workflow_member", ["tool_alias"])

    op.create_table(
        "workflow_version",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("alias", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("descriptor_snapshot", sa.JSON(), nullable=False),
        sa.Column("members_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("change_summary", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_version")),
        sa.ForeignKeyConstraint(
            ["alias"],
            ["workflow.alias"],
            name=op.f("fk_workflow_version_alias_workflow"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_workflow_version_created_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("alias", "version", name="uq_workflow_version_alias_version"),
    )
    op.create_index(
        "ix_workflow_version_alias_created",
        "workflow_version",
        ["alias", "created_at"],
    )

    # Seed the new permission onto the administrator role. super_user
    # gets it implicitly via the catalog sentinel.
    bind = op.get_bind()
    admin_id_row = bind.execute(
        sa.text("SELECT id FROM role WHERE name = :n").bindparams(n="administrator")
    ).fetchone()
    if admin_id_row is None:
        return  # Greenfield install — built-in roles not seeded yet.
    admin_id: int = admin_id_row[0]

    existing = {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT permission FROM role_permission "
                "WHERE role_id = :rid AND permission IN :perms"
            ).bindparams(
                sa.bindparam("rid", admin_id),
                sa.bindparam(
                    "perms",
                    list(_NEW_ADMIN_PERMISSIONS),
                    expanding=True,
                ),
            )
        ).fetchall()
    }

    now = _dt.datetime.now(tz=_dt.UTC)
    rows = [
        {"role_id": admin_id, "permission": perm, "granted_at": now}
        for perm in _NEW_ADMIN_PERMISSIONS
        if perm not in existing
    ]
    if rows:
        op.bulk_insert(
            sa.table(
                "role_permission",
                sa.column("role_id", sa.Integer()),
                sa.column("permission", sa.String()),
                sa.column("granted_at", sa.DateTime(timezone=True)),
            ),
            rows,
        )


def downgrade() -> None:
    """Drop the three workflow tables + the seeded permission rows."""
    bind = op.get_bind()
    for perm in _NEW_ADMIN_PERMISSIONS:
        bind.execute(
            sa.text("DELETE FROM role_permission WHERE permission = :p").bindparams(p=perm)
        )
    op.drop_index("ix_workflow_version_alias_created", table_name="workflow_version")
    op.drop_table("workflow_version")
    op.drop_index("ix_workflow_member_tool_alias", table_name="workflow_member")
    op.drop_index("ix_workflow_member_workflow_id", table_name="workflow_member")
    op.drop_table("workflow_member")
    op.drop_index("ix_workflow_archived_at", table_name="workflow")
    op.drop_index("ix_workflow_state", table_name="workflow")
    op.drop_table("workflow")
