"""Approvals: directed approver + library-file-version target (ADR-0006).

Revision ID: 0046
Revises: 0045
Create Date: 2026-05-24

Backs ADR-0006 §5 (shared with ADR-0007): the file library reuses
``matofx.approvals`` for the toggleable, directed-approver gate on global
library additions. Two capabilities are added to ``approval_request``:

- ``designated_approver_user_id`` (nullable FK → ``user``, SET NULL) —
  the single named approver the submitter chose. ``NULL`` for the open
  tool-approver-pool gates that jobs / studies use.
- ``library_file_version_id`` (nullable FK → ``library_file_version``,
  CASCADE) — a **third approval target**, alongside ``job_id`` /
  ``parametric_study_id``. A library gate has no tool, so ``tool_alias``
  and ``tool_version_id`` are relaxed to **nullable**, and the
  exactly-one-target CHECK widens from the two-way XOR (migration 0038)
  to "exactly one of the three is set".

A study-style partial unique index keeps at most one pending
``(library_file_version, kind)`` gate.

SQLite can't ALTER nullability / drop-and-add a CHECK in place, so the
change goes through a recreate-style ``batch_alter_table`` — the exact
technique migration 0038 used to dual-home this table. CHECK bodies are
**frozen literals** (a migration must not import the ORM). No data
backfill: every existing approval row is a job / study gate with its
tool columns set, so it stays valid under the widened CHECK and the
nullable columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# Frozen literals — do NOT import the ORM CHECK bodies.
_TARGET_ONE_OF_THREE = (
    "(CASE WHEN job_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN parametric_study_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN library_file_version_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)
_TARGET_XOR_TWO = "(job_id IS NOT NULL) <> (parametric_study_id IS NOT NULL)"


def upgrade() -> None:
    """Add the directed approver + library target; widen the target CHECK."""
    with op.batch_alter_table("approval_request", recreate=recreate_on_sqlite()) as batch:
        batch.add_column(sa.Column("library_file_version_id", _BIGINT, nullable=True))
        batch.add_column(sa.Column("designated_approver_user_id", _BIGINT, nullable=True))
        batch.alter_column("tool_alias", existing_type=sa.String(length=64), nullable=True)
        batch.alter_column("tool_version_id", existing_type=_BIGINT, nullable=True)
        batch.create_foreign_key(
            op.f("fk_approval_request_library_file_version_id_library_file_version"),
            "library_file_version",
            ["library_file_version_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_foreign_key(
            op.f("fk_approval_request_designated_approver_user_id_user"),
            "user",
            ["designated_approver_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_approval_request_library_file_version_id", ["library_file_version_id"]
        )
        batch.drop_constraint("ck_approval_request_exactly_one_target", type_="check")
        batch.create_check_constraint(
            "ck_approval_request_exactly_one_target", _TARGET_ONE_OF_THREE
        )

    op.create_index(
        "uq_approval_request_pending_library_kind",
        "approval_request",
        ["library_file_version_id", "kind"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Reverse — one-way lossy for any library-target rows (greenfield: none).

    Re-imposing the two-way XOR + ``tool_*`` NOT NULL would fail on
    library-targeted rows (no job/study, no tool), so those are deleted
    first — the same intentional one-way downgrade migrations 0032 / 0038
    use for their dual-home rows.
    """
    op.execute("DELETE FROM approval_request WHERE library_file_version_id IS NOT NULL")
    op.drop_index("uq_approval_request_pending_library_kind", table_name="approval_request")
    with op.batch_alter_table("approval_request", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("ck_approval_request_exactly_one_target", type_="check")
        batch.create_check_constraint("ck_approval_request_exactly_one_target", _TARGET_XOR_TWO)
        batch.drop_index("ix_approval_request_library_file_version_id")
        batch.drop_constraint(
            op.f("fk_approval_request_library_file_version_id_library_file_version"),
            type_="foreignkey",
        )
        batch.drop_constraint(
            op.f("fk_approval_request_designated_approver_user_id_user"),
            type_="foreignkey",
        )
        batch.drop_column("library_file_version_id")
        batch.drop_column("designated_approver_user_id")
        batch.alter_column("tool_alias", existing_type=sa.String(length=64), nullable=False)
        batch.alter_column("tool_version_id", existing_type=_BIGINT, nullable=False)
