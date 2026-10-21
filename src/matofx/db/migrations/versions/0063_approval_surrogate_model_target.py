"""Approvals: surrogate-model-version approval target (ADR-0008 §5.5/§6.3).

Revision ID: 0063
Revises: 0062
Create Date: 2026-06-02

Backs ``designs/surrogate-family.md`` §5.5 / §6.3: the global / general-use
publish of a surrogate model version runs through ``matofx.approvals`` for
the toggleable, directed-checker gate. One new approval target is added to
``approval_request`` and the exactly-one-target CHECK widens from "one of
six" (migration 0055) to "one of seven":

- ``surrogate_model_version_id`` (nullable FK → ``surrogate_model_version``,
  CASCADE).

Mechanically identical to the V&V widening (0055): a recreate-style
``batch_alter_table`` (SQLite can't drop-and-add a CHECK in place), a
pending-only partial unique index keeping at most one pending
``(version, kind)`` gate, frozen CHECK literals (no ORM import), and a
one-way downgrade that deletes any surrogate-target rows first (none at
this revision). No data backfill — every existing row is a job / study /
library / material / V&V gate, all valid under the widened CHECK.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# Frozen literals — do NOT import the ORM CHECK bodies.
_TARGET_ONE_OF_SEVEN = (
    "(CASE WHEN job_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN parametric_study_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN library_file_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN material_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN tool_vv_record_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN job_vv_record_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN surrogate_model_version_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)
_TARGET_ONE_OF_SIX = (
    "(CASE WHEN job_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN parametric_study_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN library_file_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN material_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN tool_vv_record_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN job_vv_record_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)


def upgrade() -> None:
    """Add the surrogate-model approval target; widen the exactly-one CHECK."""
    with op.batch_alter_table("approval_request", recreate=recreate_on_sqlite()) as batch:
        batch.add_column(sa.Column("surrogate_model_version_id", _BIGINT, nullable=True))
        batch.create_foreign_key(
            op.f("fk_approval_request_surrogate_model_version_id_surrogate_model_version"),
            "surrogate_model_version",
            ["surrogate_model_version_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index(
            "ix_approval_request_surrogate_model_version_id", ["surrogate_model_version_id"]
        )
        batch.drop_constraint("ck_approval_request_exactly_one_target", type_="check")
        batch.create_check_constraint(
            "ck_approval_request_exactly_one_target", _TARGET_ONE_OF_SEVEN
        )

    op.create_index(
        "uq_approval_request_pending_surrogate_kind",
        "approval_request",
        ["surrogate_model_version_id", "kind"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Reverse — one-way lossy for surrogate-target rows (greenfield: none)."""
    op.execute("DELETE FROM approval_request WHERE surrogate_model_version_id IS NOT NULL")
    op.drop_index("uq_approval_request_pending_surrogate_kind", table_name="approval_request")
    with op.batch_alter_table("approval_request", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("ck_approval_request_exactly_one_target", type_="check")
        batch.create_check_constraint("ck_approval_request_exactly_one_target", _TARGET_ONE_OF_SIX)
        batch.drop_index("ix_approval_request_surrogate_model_version_id")
        batch.drop_constraint(
            op.f("fk_approval_request_surrogate_model_version_id_surrogate_model_version"),
            type_="foreignkey",
        )
        batch.drop_column("surrogate_model_version_id")
