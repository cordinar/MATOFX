"""V&V tracking: the two V&V record tables (ADR-0011).

Revision ID: 0054
Revises: 0053
Create Date: 2026-05-27

Backs ``designs/vv-tracking.md`` §2.1 + §2.2: governed V&V (Verification
& Validation) claims about tool versions and individual job runs. Two
new tables, both with the same five-state lifecycle
(``draft → pending → approved | rejected | retired``) mirroring
``library_file_version`` so the existing ``matofx.approvals`` machinery
handles the gate with one new target kind per table (the widening
migration is ``0055``):

- ``tool_vv_record`` — claims about a ``tool_version``. The V&V cohort
  key for inheritance lives on ``tool_version.bundle_hash`` (slice 0,
  migration ``0053``); this table carries the claim text (kind, state,
  evidence reference, applicable range, confidence band, standards
  cited) and the actor / decision audit trail.
- ``job_vv_record`` — claims about a specific ``job`` run. Job claims
  do NOT inherit (a job is a frozen run on a frozen tool version);
  schema mirrors ``tool_vv_record`` minus the inheritance-only fields
  (``applicable_range_json`` / ``confidence_band`` / ``standards_cited_json``).

The ``decided_when_terminal`` CHECK enforces that any record in a
decision state (``approved`` or ``rejected``) has both
``decided_by_user_id`` and ``decided_at`` populated. ``retired`` is
also terminal but is NOT a decision — retire keeps ``decided_*`` NULL
unless the record had been decided before.

``VV_CURATE`` and the ``vv_curator`` capability role are seeded by
migration ``0056`` — this migration is additive table creation only,
so a plain ``op.create_table`` works on both SQLite and Postgres (no
recreate-style ``batch_alter_table``; cf. the 0048 materials template).
CHECK bodies are **frozen literals** — migrations are immutable
historical records and must not import the V&V vocab tuples (the
catalog can drift between heads; the migration is point-in-time).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# Frozen literals — do NOT replace with imports of the V&V vocab tuples.
_TOOL_VV_KIND_CHECK = "kind IN ('verification','validation')"
_JOB_VV_KIND_CHECK = "kind IN ('validated_against','calibrated_from','sanity_checked')"
_VV_STATE_CHECK = "state IN ('draft','pending','approved','rejected','retired')"
_DECIDED_WHEN_TERMINAL_CHECK = (
    "state NOT IN ('approved','rejected') "
    "OR (decided_by_user_id IS NOT NULL AND decided_at IS NOT NULL)"
)


def upgrade() -> None:
    """Create the two V&V record tables."""
    op.create_table(
        "tool_vv_record",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("tool_version_id", _BIGINT, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("evidence_file_version_id", _BIGINT, nullable=True),
        sa.Column("evidence_url", sa.Text(), nullable=True),
        sa.Column(
            "applicable_range_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("confidence_band", sa.Float(), nullable=True),
        sa.Column(
            "standards_cited_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("recorded_by_user_id", _BIGINT, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by_user_id", _BIGINT, nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_vv_record")),
        sa.ForeignKeyConstraint(
            ["tool_version_id"],
            ["tool_version.id"],
            name=op.f("fk_tool_vv_record_tool_version_id_tool_version"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_file_version_id"],
            ["library_file_version.id"],
            name=op.f("fk_tool_vv_record_evidence_file_version_id_library_file_version"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_user_id"],
            ["user.id"],
            name=op.f("fk_tool_vv_record_recorded_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["user.id"],
            name=op.f("fk_tool_vv_record_decided_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_TOOL_VV_KIND_CHECK, name=op.f("ck_tool_vv_record_kind")),
        sa.CheckConstraint(_VV_STATE_CHECK, name=op.f("ck_tool_vv_record_state")),
        sa.CheckConstraint(
            _DECIDED_WHEN_TERMINAL_CHECK,
            name=op.f("ck_tool_vv_record_decided_when_terminal"),
        ),
    )
    op.create_index(
        "ix_tool_vv_record_tool_version_id",
        "tool_vv_record",
        ["tool_version_id"],
    )
    op.create_index("ix_tool_vv_record_state", "tool_vv_record", ["state"])
    op.create_index(
        "ix_tool_vv_record_tv_state_kind",
        "tool_vv_record",
        ["tool_version_id", "state", "kind"],
    )

    op.create_table(
        "job_vv_record",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("job_id", _BIGINT, nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("evidence_file_version_id", _BIGINT, nullable=True),
        sa.Column("evidence_url", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("recorded_by_user_id", _BIGINT, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by_user_id", _BIGINT, nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_vv_record")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_job_vv_record_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_file_version_id"],
            ["library_file_version.id"],
            name=op.f("fk_job_vv_record_evidence_file_version_id_library_file_version"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_user_id"],
            ["user.id"],
            name=op.f("fk_job_vv_record_recorded_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["user.id"],
            name=op.f("fk_job_vv_record_decided_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_JOB_VV_KIND_CHECK, name=op.f("ck_job_vv_record_kind")),
        sa.CheckConstraint(_VV_STATE_CHECK, name=op.f("ck_job_vv_record_state")),
        sa.CheckConstraint(
            _DECIDED_WHEN_TERMINAL_CHECK,
            name=op.f("ck_job_vv_record_decided_when_terminal"),
        ),
    )
    op.create_index("ix_job_vv_record_job_id", "job_vv_record", ["job_id"])
    op.create_index("ix_job_vv_record_state", "job_vv_record", ["state"])


def downgrade() -> None:
    """Drop the two V&V record tables (children first — neither has children)."""
    op.drop_index("ix_job_vv_record_state", table_name="job_vv_record")
    op.drop_index("ix_job_vv_record_job_id", table_name="job_vv_record")
    op.drop_table("job_vv_record")
    op.drop_index("ix_tool_vv_record_tv_state_kind", table_name="tool_vv_record")
    op.drop_index("ix_tool_vv_record_state", table_name="tool_vv_record")
    op.drop_index("ix_tool_vv_record_tool_version_id", table_name="tool_vv_record")
    op.drop_table("tool_vv_record")
