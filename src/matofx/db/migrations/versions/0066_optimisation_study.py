"""Optimisation framework: optimisation_study + job 1-of-3 home + study_result cols.

Revision ID: 0066
Revises: 0065
Create Date: 2026-06-07

Backs ADR-0009 / ``designs/optimisation-framework.md`` §12 — the
data-model foundation for the second study sibling (build Phase 1).
Four changes, all greenfield-safe (the v2 install has no rows that
conflict):

- ``optimisation_study`` — an adaptive design-space search. **Child of
  ``project``, parallel to ``scenario``** (FK ``project_id`` RESTRICT,
  ACL inherited from the project). Pins the tool ``(alias, version)`` at
  configure. ``state`` / ``target_kind`` are CHECK-constrained; the name
  is **deliberately not unique** (studies-engine §1). Holds the frozen
  ``config_json`` plus the durable optimiser-state columns (§10). The
  ``workflow_*`` columns are present (so the table shape is final) but
  carry **no FK** yet — the ``workflow`` target lands in build Phase 11
  (the ``surrogate_model_version_id`` tag on ``job`` sets the
  logical-pointer precedent).

- ``job`` gains ``optimisation_study_id`` (nullable FK RESTRICT) +
  ``optimisation_metadata_json`` (§12.2/§12.4), and the
  ``ck_job_exactly_one_home`` CHECK is **widened from the two-way XOR
  (migration 0032) to an exactly-one-of-three CASE-sum** so a job is an
  ad-hoc/workflow child, a parametric child, **or** an optimisation
  child — never two, never none. CASE-sum ``= 1`` is portable across
  SQLite + Postgres where a bare ``::int`` cast would not be. SQLite
  can't ADD a column / ADD a CHECK / DROP a CHECK in place, so the
  change goes through a recreate-style ``batch_alter_table`` (the
  technique migrations 0010 / 0031 / 0032 used).

- ``study_result`` gains ``evaluated_on_surrogate`` (nullable bool,
  defaulted false) + ``feasible`` (nullable bool) (§12.5/§12.6) — shared
  with the surrogate family but owned by this ADR's first migration
  regardless of ship order. Both nullable, so a plain ADD COLUMN works
  on both engines.

The state / target / home CHECK bodies are **frozen literals** here —
migrations are immutable historical records and must not import
``OPTIMISATION_STUDY_STATES`` / ``TARGET_KINDS`` / the Job CheckConstraint
(a later edit to those must not retroactively change what this revision
does).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# Frozen literals — do NOT replace with imports of the ORM tuples / CHECK.
_OPT_STATE_CHECK = (
    "state IN ('draft','pending_start_approval','running','complete','cancelled','failed')"
)
_OPT_TARGET_CHECK = "target_kind IN ('tool','workflow')"

# The widened exactly-one-of-three home CHECK (this revision) and the
# original two-way XOR (migration 0032, restored on downgrade).
_JOB_HOME_CHECK_3 = (
    "(CASE WHEN scenario_id IS NOT NULL THEN 1 ELSE 0 END"
    " + CASE WHEN parametric_study_id IS NOT NULL THEN 1 ELSE 0 END"
    " + CASE WHEN optimisation_study_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)
_JOB_HOME_CHECK_2 = "(scenario_id IS NOT NULL) <> (parametric_study_id IS NOT NULL)"


def upgrade() -> None:
    """Create optimisation_study, widen the job home CHECK, add result cols."""
    op.create_table(
        "optimisation_study",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("project_id", _BIGINT, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("target_kind", sa.String(length=16), nullable=False, server_default="tool"),
        sa.Column("tool_alias", sa.String(length=64), nullable=True),
        sa.Column("tool_version_id", _BIGINT, nullable=True),
        sa.Column("workflow_alias", sa.String(length=64), nullable=True),
        sa.Column("workflow_version_id", _BIGINT, nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("pinned_refs_json", sa.JSON(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("start_approver_user_id", _BIGINT, nullable=True),
        sa.Column("stop_reason", sa.String(length=24), nullable=True),
        sa.Column("optimiser_state_json", sa.JSON(), nullable=True),
        sa.Column("optimiser_state_blob_path", sa.String(length=512), nullable=True),
        sa.Column("optimiser_state_sha256", sa.String(length=64), nullable=True),
        sa.Column("convergence_trace", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("current_best_json", sa.JSON(), nullable=True),
        sa.Column("last_batch_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "halt_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("created_by_user_id", _BIGINT, nullable=False),
        sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state_changed_by_user_id", _BIGINT, nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by_user_id", _BIGINT, nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_optimisation_study")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_optimisation_study_project_id_project"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_alias"],
            ["tool.alias"],
            name=op.f("fk_optimisation_study_tool_alias_tool"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_version_id"],
            ["tool_version.id"],
            name=op.f("fk_optimisation_study_tool_version_id_tool_version"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["start_approver_user_id"],
            ["user.id"],
            name=op.f("fk_optimisation_study_start_approver_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_optimisation_study_created_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["state_changed_by_user_id"],
            ["user.id"],
            name=op.f("fk_optimisation_study_state_changed_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["archived_by_user_id"],
            ["user.id"],
            name=op.f("fk_optimisation_study_archived_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(_OPT_STATE_CHECK, name=op.f("ck_optimisation_study_state")),
        sa.CheckConstraint(_OPT_TARGET_CHECK, name=op.f("ck_optimisation_study_target_kind")),
    )
    op.create_index(
        "ix_optimisation_study_project_id_state",
        "optimisation_study",
        ["project_id", "state"],
    )
    op.create_index("ix_optimisation_study_tool_alias", "optimisation_study", ["tool_alias"])
    op.create_index("ix_optimisation_study_state", "optimisation_study", ["state"])

    # Job: add the optimisation FK + metadata column, widen the home
    # CHECK to 1-of-3. Recreate-style batch so SQLite picks up the new
    # column + the CHECK swap (it can't ALTER either in place). Postgres
    # takes the same batch API. The partial unique indexes and all other
    # constraints are preserved by the reflection-based recreate.
    with op.batch_alter_table("job", recreate="always") as batch:
        batch.add_column(sa.Column("optimisation_study_id", _BIGINT, nullable=True))
        batch.add_column(sa.Column("optimisation_metadata_json", sa.JSON(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_job_optimisation_study_id_optimisation_study"),
            "optimisation_study",
            ["optimisation_study_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_job_optimisation_study_id", ["optimisation_study_id"])
        batch.drop_constraint("ck_job_exactly_one_home", type_="check")
        batch.create_check_constraint("ck_job_exactly_one_home", _JOB_HOME_CHECK_3)

    # study_result: the two optimisation-owned columns (§12.5/§12.6).
    # Both nullable → a plain ADD COLUMN works on SQLite and Postgres.
    op.add_column(
        "study_result",
        sa.Column("evaluated_on_surrogate", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    op.add_column("study_result", sa.Column("feasible", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Reverse the additions.

    Optimisation-study children have ``scenario_id = NULL`` +
    ``parametric_study_id = NULL`` by design, so they can't satisfy the
    restored two-way XOR home CHECK — they are deleted first (with their
    studies). Intentionally one-way lossy, in the same spirit as
    migration 0032's downgrade; a greenfield install has none of these
    rows.
    """
    op.drop_column("study_result", "feasible")
    op.drop_column("study_result", "evaluated_on_surrogate")

    op.execute("DELETE FROM job WHERE optimisation_study_id IS NOT NULL")

    with op.batch_alter_table("job", recreate="always") as batch:
        batch.drop_constraint("ck_job_exactly_one_home", type_="check")
        batch.create_check_constraint("ck_job_exactly_one_home", _JOB_HOME_CHECK_2)
        batch.drop_index("ix_job_optimisation_study_id")
        batch.drop_constraint(
            op.f("fk_job_optimisation_study_id_optimisation_study"),
            type_="foreignkey",
        )
        batch.drop_column("optimisation_metadata_json")
        batch.drop_column("optimisation_study_id")

    op.drop_index("ix_optimisation_study_state", table_name="optimisation_study")
    op.drop_index("ix_optimisation_study_tool_alias", table_name="optimisation_study")
    op.drop_index("ix_optimisation_study_project_id_state", table_name="optimisation_study")
    op.drop_table("optimisation_study")
