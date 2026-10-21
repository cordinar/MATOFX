"""Job Approvals: widen ck_job_state + the approval_request table.

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-15

Backs §16.15 (Job Approvals). Two changes:

- ``job.ck_job_state`` widens from the original five run-lifecycle
  states to also accept ``pending_approval`` / ``denied`` /
  ``pending_results`` / ``results_withheld``. SQLite can't ALTER a
  CHECK in place, so the change goes through a recreate-style
  ``batch_alter_table`` (same technique migration 0010 used for the
  timestamp CHECKs). The new state list is written as a **frozen
  literal** here — migrations are immutable historical records and
  must not import ``JOB_STATES`` (a later edit to that tuple must not
  retroactively change what this revision does).

- ``approval_request`` — one row per approval gate a job passes
  through. A ``pre`` row is created when a pre/both-policy job is
  submitted; a ``post`` row when a post/both-policy job finishes its
  run. Rows are **never deleted** (the legacy app deleted the
  approval row on decision, losing history); a ``both`` job accrues
  two rows over its lifetime. ``eligible_approvers`` is a JSON list
  of usernames snapshotted at request time so a later edit to the
  tool's approver list can't rewrite who was asked. A partial unique
  index allows at most one ``pending`` request per ``(job_id, kind)``.

No data backfill: the v2 install is greenfield and existing jobs are
in the original five states, all of which remain valid.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | None = None
depends_on: str | None = None


# Frozen literals — do NOT replace with an import of JOB_STATES.
_NEW_STATE_CHECK = (
    "state IN ('created','queued','running','complete','failed',"
    "'pending_approval','denied','pending_results','results_withheld')"
)
_OLD_STATE_CHECK = "state IN ('created','queued','running','complete','failed')"


def upgrade() -> None:
    """Widen ck_job_state and create the approval_request table."""
    # Recreate-style batch so SQLite picks up the replacement CHECK
    # (it can't ALTER a CHECK on an existing table). Postgres takes
    # the drop+create path through the same batch API.
    with op.batch_alter_table("job", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("ck_job_state", type_="check")
        batch.create_check_constraint("ck_job_state", _NEW_STATE_CHECK)

    op.create_table(
        "approval_request",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column(
            "job_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "requested_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("eligible_approvers", sa.JSON(), nullable=False),
        sa.Column(
            "justification",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "decided_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decided_via", sa.String(length=16), nullable=True),
        sa.Column("tool_alias", sa.String(length=64), nullable=False),
        sa.Column(
            "tool_version_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_request")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_approval_request_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user.id"],
            name=op.f("fk_approval_request_requested_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["user.id"],
            name=op.f("fk_approval_request_decided_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_alias"],
            ["tool.alias"],
            name=op.f("fk_approval_request_tool_alias_tool"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_version_id"],
            ["tool_version.id"],
            name=op.f("fk_approval_request_tool_version_id_tool_version"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "kind IN ('pre','post')",
            name=op.f("ck_approval_request_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','denied')",
            name=op.f("ck_approval_request_status"),
        ),
        sa.CheckConstraint(
            "decided_via IS NULL OR decided_via IN ('approver','self','admin_override')",
            name=op.f("ck_approval_request_decided_via"),
        ),
        # A non-pending row must carry the full decision tuple. Cheap
        # tamper guard in the same spirit as the job timestamp CHECKs.
        sa.CheckConstraint(
            "status = 'pending' "
            "OR (decided_at IS NOT NULL "
            "AND decided_by_user_id IS NOT NULL "
            "AND decided_via IS NOT NULL)",
            name=op.f("ck_approval_request_decided_coherent"),
        ),
    )
    op.create_index(
        op.f("ix_approval_request_job_id"),
        "approval_request",
        ["job_id"],
    )
    op.create_index(
        op.f("ix_approval_request_status"),
        "approval_request",
        ["status"],
    )
    # At most one pending request per (job, kind). A job can have a
    # historical denied pre row AND (after a hypothetical re-submit
    # flow) a new pending one, so the uniqueness is scoped to pending.
    op.create_index(
        "uq_approval_request_pending_job_kind",
        "approval_request",
        ["job_id", "kind"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Drop approval_request and restore the original five-state CHECK.

    Restoring the strict CHECK fails if any job sits in one of the
    four states this revision added, so those rows are first coerced
    back to the closest legacy state. This is intentionally a one-way
    lossy mapping — downgrading removes the approvals feature, so the
    gate semantics can't be preserved:

    - ``pending_approval`` → ``created``  (never ran; back to a draft)
    - ``denied``           → ``failed``   (never ran; terminal)
    - ``pending_results``  → ``complete`` (ran OK; gate gone, release)
    - ``results_withheld`` → ``failed``   (ran, but conservatively NOT
      auto-exposing previously-withheld outputs on a downgrade)
    """
    op.execute("UPDATE job SET state = 'created' WHERE state = 'pending_approval'")
    op.execute("UPDATE job SET state = 'failed' WHERE state = 'denied'")
    op.execute("UPDATE job SET state = 'complete' WHERE state = 'pending_results'")
    op.execute("UPDATE job SET state = 'failed' WHERE state = 'results_withheld'")

    op.drop_index("uq_approval_request_pending_job_kind", table_name="approval_request")
    op.drop_index(op.f("ix_approval_request_status"), table_name="approval_request")
    op.drop_index(op.f("ix_approval_request_job_id"), table_name="approval_request")
    op.drop_table("approval_request")

    with op.batch_alter_table("job", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("ck_job_state", type_="check")
        batch.create_check_constraint("ck_job_state", _OLD_STATE_CHECK)
