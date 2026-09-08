"""Workflow-scoped tutorials.

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-14

Extends the ``tutorial`` table with a third scope: per-workflow.

Adds:

- ``tutorial.workflow_alias`` — nullable FK to ``workflow.alias``
  (``ondelete=RESTRICT``). When set, the tutorial surfaces on the
  Home page's Workflow Quick Access card for that workflow.
- ``ix_tutorial_workflow_alias`` — single-column index.
- ``ix_tutorial_workflow_active`` — composite index mirroring the
  per-tool one (``workflow_alias, media_type, is_active``) used by
  the activation-cascade query.
- ``ck_tutorial_scope_exclusive`` — at most one of ``tool_alias`` /
  ``workflow_alias`` may be set. Both NULL is still valid (general
  tutorial).

Activation rule for workflow tutorials mirrors tool tutorials:
service-layer cascade pins at most one active document + one active
video per workflow. General-scope behaviour is unchanged.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add workflow_alias + scope-exclusivity CHECK + indexes."""
    with op.batch_alter_table("tutorial") as batch:
        batch.add_column(sa.Column("workflow_alias", sa.String(length=64), nullable=True))
        batch.create_foreign_key(
            "fk_tutorial_workflow_alias_workflow",
            "workflow",
            ["workflow_alias"],
            ["alias"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_tutorial_scope_exclusive",
            "NOT (tool_alias IS NOT NULL AND workflow_alias IS NOT NULL)",
        )

    op.create_index(
        "ix_tutorial_workflow_alias",
        "tutorial",
        ["workflow_alias"],
    )
    op.create_index(
        "ix_tutorial_workflow_active",
        "tutorial",
        ["workflow_alias", "media_type", "is_active"],
    )


def downgrade() -> None:
    """Reverse 0025 — drop indexes, CHECK, FK, column."""
    op.drop_index("ix_tutorial_workflow_active", table_name="tutorial")
    op.drop_index("ix_tutorial_workflow_alias", table_name="tutorial")
    with op.batch_alter_table("tutorial") as batch:
        batch.drop_constraint("ck_tutorial_scope_exclusive", type_="check")
        batch.drop_constraint("fk_tutorial_workflow_alias_workflow", type_="foreignkey")
        batch.drop_column("workflow_alias")
