"""Approvals: material-version approval target (ADR-0007 §5).

Revision ID: 0049
Revises: 0048
Create Date: 2026-05-25

Backs ADR-0007 §5: the materials database reuses ``matofx.approvals``
for the toggleable, directed-approver gate on material changes. Adds a
**fourth** approval target to ``approval_request``:

- ``material_version_id`` (nullable FK → ``material_version``, CASCADE)
  — a material-change gate, alongside ``job_id`` /
  ``parametric_study_id`` / ``library_file_version_id``. The
  exactly-one-target CHECK widens from "one of three" (migration 0046)
  to "one of four".

``designated_approver_user_id`` and the nullable ``tool_*`` columns
already exist (migration 0046, shared with ADR-0006), so this migration
only adds the new target column + widens the CHECK + adds a study/library
-style partial unique index keeping at most one pending
``(material_version, kind)`` gate.

SQLite can't drop-and-add a CHECK in place, so the change goes through a
recreate-style ``batch_alter_table`` — the exact technique migrations
0038 / 0046 used. CHECK bodies are **frozen literals** (a migration must
not import the ORM). No data backfill: every existing row is a job /
study / library gate and stays valid under the widened CHECK and the
nullable column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# Frozen literals — do NOT import the ORM CHECK bodies.
_TARGET_ONE_OF_FOUR = (
    "(CASE WHEN job_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN parametric_study_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN library_file_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN material_version_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)
_TARGET_ONE_OF_THREE = (
    "(CASE WHEN job_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN parametric_study_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN library_file_version_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)


def upgrade() -> None:
    """Add the material approval target; widen the exactly-one-target CHECK."""
    with op.batch_alter_table("approval_request", recreate=recreate_on_sqlite()) as batch:
        batch.add_column(sa.Column("material_version_id", _BIGINT, nullable=True))
        batch.create_foreign_key(
            op.f("fk_approval_request_material_version_id_material_version"),
            "material_version",
            ["material_version_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index("ix_approval_request_material_version_id", ["material_version_id"])
        batch.drop_constraint("ck_approval_request_exactly_one_target", type_="check")
        batch.create_check_constraint("ck_approval_request_exactly_one_target", _TARGET_ONE_OF_FOUR)

    op.create_index(
        "uq_approval_request_pending_material_kind",
        "approval_request",
        ["material_version_id", "kind"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Reverse — one-way lossy for material-target rows (greenfield: none).

    Re-imposing the three-way CHECK would fail on material-targeted rows,
    so those are deleted first — the same intentional one-way downgrade
    migrations 0032 / 0038 / 0046 use for their added-target rows.
    """
    op.execute("DELETE FROM approval_request WHERE material_version_id IS NOT NULL")
    op.drop_index("uq_approval_request_pending_material_kind", table_name="approval_request")
    with op.batch_alter_table("approval_request", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("ck_approval_request_exactly_one_target", type_="check")
        batch.create_check_constraint(
            "ck_approval_request_exactly_one_target", _TARGET_ONE_OF_THREE
        )
        batch.drop_index("ix_approval_request_material_version_id")
        batch.drop_constraint(
            op.f("fk_approval_request_material_version_id_material_version"),
            type_="foreignkey",
        )
        batch.drop_column("material_version_id")
