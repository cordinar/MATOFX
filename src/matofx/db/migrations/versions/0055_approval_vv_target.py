"""Approvals: V&V record approval targets (ADR-0011 §3).

Revision ID: 0055
Revises: 0054
Create Date: 2026-05-27

Backs ADR-0011 §3 / ``designs/vv-tracking.md`` §2.3: the V&V tracking
layer reuses ``matofx.approvals`` for the toggleable, directed-approver
gate on V&V record promotion. **Two** new approval targets are added to
``approval_request`` in a single step — one per V&V record kind — and
the exactly-one-target CHECK widens from "one of four" (migration 0049)
to "one of six":

- ``tool_vv_record_id`` (nullable FK → ``tool_vv_record``, CASCADE).
- ``job_vv_record_id`` (nullable FK → ``job_vv_record``, CASCADE).

Two columns instead of a single polymorphic FK because tool-V&V and
job-V&V records live in distinct tables (design §2 splits them on
schema shape, not on lifecycle) — a single FK can't reference both
without a separate discriminator column, which would buy nothing over
the multi-FK pattern materials / library already use.

Two new study/library-style partial unique indexes keep at most one
pending ``(record, kind)`` gate per V&V record — same shape as
``uq_approval_request_pending_library_kind`` (0046) and
``uq_approval_request_pending_material_kind`` (0049).

``designated_approver_user_id`` and the nullable ``tool_*`` columns
already exist (migration 0046, shared with ADR-0006 / 0007); this
migration only adds the two new target columns + widens the CHECK +
adds the two pending-only partial unique indexes.

SQLite can't drop-and-add a CHECK in place, so the change goes through
a recreate-style ``batch_alter_table`` — the exact technique migrations
0038 / 0046 / 0049 used. CHECK bodies are **frozen literals** (a
migration must not import the ORM). No data backfill: every existing
row is a job / study / library / material gate and stays valid under
the widened CHECK and the new nullable columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# Frozen literals — do NOT import the ORM CHECK bodies.
_TARGET_ONE_OF_SIX = (
    "(CASE WHEN job_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN parametric_study_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN library_file_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN material_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN tool_vv_record_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN job_vv_record_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)
_TARGET_ONE_OF_FOUR = (
    "(CASE WHEN job_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN parametric_study_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN library_file_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN material_version_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)


def upgrade() -> None:
    """Add the two V&V approval targets; widen the exactly-one-target CHECK."""
    with op.batch_alter_table("approval_request", recreate="always") as batch:
        batch.add_column(sa.Column("tool_vv_record_id", _BIGINT, nullable=True))
        batch.add_column(sa.Column("job_vv_record_id", _BIGINT, nullable=True))
        batch.create_foreign_key(
            op.f("fk_approval_request_tool_vv_record_id_tool_vv_record"),
            "tool_vv_record",
            ["tool_vv_record_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_foreign_key(
            op.f("fk_approval_request_job_vv_record_id_job_vv_record"),
            "job_vv_record",
            ["job_vv_record_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index("ix_approval_request_tool_vv_record_id", ["tool_vv_record_id"])
        batch.create_index("ix_approval_request_job_vv_record_id", ["job_vv_record_id"])
        batch.drop_constraint("ck_approval_request_exactly_one_target", type_="check")
        batch.create_check_constraint("ck_approval_request_exactly_one_target", _TARGET_ONE_OF_SIX)

    op.create_index(
        "uq_approval_request_pending_tool_vv_kind",
        "approval_request",
        ["tool_vv_record_id", "kind"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_approval_request_pending_job_vv_kind",
        "approval_request",
        ["job_vv_record_id", "kind"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Reverse — one-way lossy for V&V-target rows (greenfield: none).

    Re-imposing the four-way CHECK would fail on V&V-targeted rows, so
    those are deleted first — the same intentional one-way downgrade
    migrations 0032 / 0038 / 0046 / 0049 use for their added-target rows.
    """
    op.execute(
        "DELETE FROM approval_request "
        "WHERE tool_vv_record_id IS NOT NULL OR job_vv_record_id IS NOT NULL"
    )
    op.drop_index("uq_approval_request_pending_job_vv_kind", table_name="approval_request")
    op.drop_index("uq_approval_request_pending_tool_vv_kind", table_name="approval_request")
    with op.batch_alter_table("approval_request", recreate="always") as batch:
        batch.drop_constraint("ck_approval_request_exactly_one_target", type_="check")
        batch.create_check_constraint("ck_approval_request_exactly_one_target", _TARGET_ONE_OF_FOUR)
        batch.drop_index("ix_approval_request_job_vv_record_id")
        batch.drop_index("ix_approval_request_tool_vv_record_id")
        batch.drop_constraint(
            op.f("fk_approval_request_job_vv_record_id_job_vv_record"),
            type_="foreignkey",
        )
        batch.drop_constraint(
            op.f("fk_approval_request_tool_vv_record_id_tool_vv_record"),
            type_="foreignkey",
        )
        batch.drop_column("job_vv_record_id")
        batch.drop_column("tool_vv_record_id")
