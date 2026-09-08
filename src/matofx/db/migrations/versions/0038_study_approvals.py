"""Study approvals: dual-home approval_request (job XOR study).

Revision ID: 0038
Revises: 0037
Create Date: 2026-05-18

Backs designs/parametric-studies.md §7 — a parametric study has a
**single** approval gate (one ``pre`` and/or one ``post`` row for the
whole study, never one per child). ``approval_request`` was job-only;
this revision dual-homes it exactly like migration 0032 dual-homed
``job``:

- ``parametric_study_id`` (nullable FK → ``parametric_study``) is
  added; ``job_id`` is made **nullable**; a new
  ``ck_approval_request_exactly_one_target`` CHECK enforces exactly
  one of the two is set.
- A study-scoped partial unique index
  ``uq_approval_request_pending_study_kind`` mirrors the existing
  job-scoped one — at most one pending ``(study, kind)`` gate. (NULL
  ``job_id`` rows are distinct under the existing job index, so the
  new one is what actually enforces "one gate per study".)

SQLite can't ALTER a column's nullability / ADD a CHECK in place, so
the change goes through a recreate-style ``batch_alter_table`` (same
technique migrations 0010 / 0031 / 0032 use). The XOR CHECK body is a
**frozen literal** — migrations are immutable historical records and
must not import the ORM. No data backfill: the v2 install is
greenfield (every existing approval row is a job gate and stays valid).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_TARGET_XOR = "(job_id IS NOT NULL) <> (parametric_study_id IS NOT NULL)"


def upgrade() -> None:
    """Add parametric_study_id, relax job_id, add the XOR + study index."""
    with op.batch_alter_table("approval_request", recreate="always") as batch:
        batch.add_column(sa.Column("parametric_study_id", _BIGINT, nullable=True))
        batch.alter_column("job_id", existing_type=_BIGINT, nullable=True)
        batch.create_foreign_key(
            op.f("fk_approval_request_parametric_study_id_parametric_study"),
            "parametric_study",
            ["parametric_study_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index("ix_approval_request_parametric_study_id", ["parametric_study_id"])
        batch.create_check_constraint("ck_approval_request_exactly_one_target", _TARGET_XOR)

    op.create_index(
        "uq_approval_request_pending_study_kind",
        "approval_request",
        ["parametric_study_id", "kind"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Reverse — one-way lossy for any study gate rows (greenfield: none).

    Re-imposing ``job_id NOT NULL`` would fail on study-targeted rows
    (``job_id`` is NULL by design), so those are deleted first — the
    same intentional one-way downgrade migration 0032 used for study
    child jobs.
    """
    op.execute("DELETE FROM approval_request WHERE parametric_study_id IS NOT NULL")
    op.drop_index("uq_approval_request_pending_study_kind", table_name="approval_request")
    with op.batch_alter_table("approval_request", recreate="always") as batch:
        batch.drop_constraint("ck_approval_request_exactly_one_target", type_="check")
        batch.drop_index("ix_approval_request_parametric_study_id")
        batch.drop_constraint(
            op.f("fk_approval_request_parametric_study_id_parametric_study"),
            type_="foreignkey",
        )
        batch.drop_column("parametric_study_id")
        batch.alter_column("job_id", existing_type=_BIGINT, nullable=False)
