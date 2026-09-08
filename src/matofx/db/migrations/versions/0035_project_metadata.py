"""Project access model rework (ADR-0002), part 3: typed metadata.

Revision ID: 0035
Revises: 0034
Create Date: 2026-05-17

Creates the admin-configurable typed metadata taxonomy:

- ``project_metadata_field`` — field definitions (single/multi-select,
  date, number; required; display order; show-in-table; active).
- ``project_metadata_value`` — allowed values for select fields.
- ``project_metadata`` — per-project selections. Exactly one of
  ``value_id`` / ``value_num`` / ``value_date`` is set, matching the
  field's type (CHECK-enforced; the service validates the type match).

No-delete (§D20): retiring a field/value flips ``active`` — rows are
never hard-deleted, so existing project selections keep resolving.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | None = None
depends_on: str | None = None


def _bigint() -> sa.types.TypeEngine[Any]:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Create the metadata taxonomy tables."""
    op.create_table(
        "project_metadata_field",
        sa.Column("id", _bigint(), nullable=False, autoincrement=True),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("field_type", sa.String(length=16), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("show_in_table", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", _bigint(), nullable=False),
        sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state_changed_by_user_id", _bigint(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_metadata_field")),
        sa.UniqueConstraint("key", name=op.f("uq_project_metadata_field_key")),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_project_metadata_field_created_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["state_changed_by_user_id"],
            ["user.id"],
            name=op.f("fk_project_metadata_field_state_changed_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "field_type IN ('single_select','multi_select','date','number')",
            name=op.f("ck_project_metadata_field_type"),
        ),
    )

    op.create_table(
        "project_metadata_value",
        sa.Column("id", _bigint(), nullable=False, autoincrement=True),
        sa.Column("field_id", _bigint(), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_metadata_value")),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["project_metadata_field.id"],
            name=op.f("fk_project_metadata_value_field_id_project_metadata_field"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("field_id", "value", name="uq_project_metadata_value"),
    )
    op.create_index(
        op.f("ix_project_metadata_value_field_id"),
        "project_metadata_value",
        ["field_id"],
    )

    op.create_table(
        "project_metadata",
        sa.Column("id", _bigint(), nullable=False, autoincrement=True),
        sa.Column("project_id", _bigint(), nullable=False),
        sa.Column("field_id", _bigint(), nullable=False),
        sa.Column("value_id", _bigint(), nullable=True),
        sa.Column("value_num", sa.Numeric(), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_metadata")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_project_metadata_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["project_metadata_field.id"],
            name=op.f("fk_project_metadata_field_id_project_metadata_field"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["value_id"],
            ["project_metadata_value.id"],
            name=op.f("fk_project_metadata_value_id_project_metadata_value"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "( (CASE WHEN value_id IS NOT NULL THEN 1 ELSE 0 END) "
            "+ (CASE WHEN value_num IS NOT NULL THEN 1 ELSE 0 END) "
            "+ (CASE WHEN value_date IS NOT NULL THEN 1 ELSE 0 END) ) = 1",
            name="ck_project_metadata_exactly_one_value",
        ),
    )
    op.create_index(op.f("ix_project_metadata_project_id"), "project_metadata", ["project_id"])
    op.create_index("ix_project_metadata_field_id", "project_metadata", ["field_id"])


def downgrade() -> None:
    """Drop the metadata taxonomy tables."""
    op.drop_index("ix_project_metadata_field_id", table_name="project_metadata")
    op.drop_index(op.f("ix_project_metadata_project_id"), table_name="project_metadata")
    op.drop_table("project_metadata")
    op.drop_index(
        op.f("ix_project_metadata_value_field_id"),
        table_name="project_metadata_value",
    )
    op.drop_table("project_metadata_value")
    op.drop_table("project_metadata_field")
