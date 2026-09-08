"""Surrogate family: the ``run_cohort`` saved-query table (ADR-0008 §3/§5.1).

Revision ID: 0059
Revises: 0058
Create Date: 2026-06-02

Backs ``designs/surrogate-family.md`` §3 + §5.1 / ``designs/studies-engine.md``
§1-§2 — the third study sibling. A cohort is a **saved query** over
completed jobs of one tool; membership is computed live (non-negotiable
#1, surrogate §16), so this table holds only the query definition, never
a materialised member list. The lazy ``study_result`` populate
(surrogate §3.3) is the only write a cohort read triggers.

The filter columns mirror §3.1: tolerated tool versions (curator-curated,
non-empty), optional cross-project / scenario / owner / time-window /
library / material narrowing (all nullable = "no filter"), plus the
``input_ranges`` JSON map and a ``cross_field_predicate`` ``safe_expr``
string the service evaluates over each candidate's inputs.

State is the ``active ──archive──▶ archived`` machine (studies-engine
§2) — no ``running`` (a cohort runs nothing); archive hides from default
lists and never deletes (D20 + ADR-0002), recorded with
``archived_at`` / ``archived_by_user_id``. CHECK bodies are **frozen
literals** (a migration must not import the ORM). Greenfield
``create_table`` — no data backfill, no CHECK changes on existing
tables — so a plain create works on both SQLite and Postgres.

The ``tolerated_tool_version_ids`` non-empty invariant is enforced at
the service layer (portable across SQLite / Postgres JSON), not a DB
CHECK — the same call the create / update service makes (§3.2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# Frozen literal — do NOT import the ORM CHECK body (studies-engine §2).
_STATE_CHECK = "state IN ('active', 'archived')"


def upgrade() -> None:
    """Create the ``run_cohort`` table + its listing indexes."""
    op.create_table(
        "run_cohort",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("project_id", _BIGINT, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_alias", sa.String(64), nullable=False),
        sa.Column("tolerated_tool_version_ids", sa.JSON(), nullable=False),
        sa.Column("project_ids", sa.JSON(), nullable=True),
        sa.Column("scenario_ids", sa.JSON(), nullable=True),
        sa.Column("owner_user_ids", sa.JSON(), nullable=True),
        sa.Column("completed_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("library_file_version_ids", sa.JSON(), nullable=True),
        sa.Column("material_version_ids", sa.JSON(), nullable=True),
        sa.Column("input_ranges", sa.JSON(), nullable=True),
        sa.Column("cross_field_predicate", sa.Text(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("last_resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("created_by_user_id", _BIGINT, nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by_user_id", _BIGINT, nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_cohort")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_run_cohort_project_id_project"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_alias"],
            ["tool.alias"],
            name=op.f("fk_run_cohort_tool_alias_tool"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_run_cohort_created_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["archived_by_user_id"],
            ["user.id"],
            name=op.f("fk_run_cohort_archived_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(_STATE_CHECK, name=op.f("ck_run_cohort_state")),
    )
    op.create_index("ix_run_cohort_project_id_state", "run_cohort", ["project_id", "state"])
    op.create_index("ix_run_cohort_tool_alias", "run_cohort", ["tool_alias"])


def downgrade() -> None:
    """Drop the ``run_cohort`` table and its indexes."""
    op.drop_index("ix_run_cohort_tool_alias", table_name="run_cohort")
    op.drop_index("ix_run_cohort_project_id_state", table_name="run_cohort")
    op.drop_table("run_cohort")
