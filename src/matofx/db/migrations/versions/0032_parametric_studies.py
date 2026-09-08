"""Parametric studies: parametric_study table + job dual-home + perm.

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-17

Backs ADR-0001 / PROJECT_BRIEF.md §G39a / designs/parametric-studies.md
§2 — the data-model foundation for parametric studies. Three changes,
all greenfield-safe (the v2 install has no rows that conflict):

- ``parametric_study`` — a single-tool, single-shot design-space
  sweep. **Child of ``project``, parallel to ``scenario``** (FK
  ``project_id``, ACL inherited from the project). Pins the tool
  ``(alias, version)`` at creation (``tool_alias`` +
  ``tool_version_id``) so a mid-study tool update can't split the
  response surface. ``state`` / ``mode`` are CHECK-constrained. The
  name is **deliberately not unique** (design §2) — no
  ``uq_parametric_study_project_name``.

- ``job`` gains ``parametric_study_id`` (nullable FK), ``scenario_id``
  is made **nullable**, and a new ``ck_job_exactly_one_home`` CHECK
  enforces that exactly one of the two is set (a job is an ad-hoc /
  workflow child *or* a study child, never neither, never both). The
  existing ``uq_job_scenario_tool_standalone`` partial unique index is
  left **unchanged**: NULL ``scenario_id`` rows are distinct in both
  SQLite and Postgres, so N same-tool study children never collide
  (design §2). SQLite can't ALTER a column's nullability or ADD a
  CHECK in place, so the change goes through a recreate-style
  ``batch_alter_table`` (same technique migrations 0010 / 0031 used).

- ``parametric_studies.create`` permission seeded onto
  ``administrator`` / ``beta_tester`` / ``regular_user`` (same ladder
  as ``custom_workflows.create``; ``super_user`` holds it via the
  closed-catalog sentinel).

The state/mode/home CHECK bodies are written as **frozen literals**
here — migrations are immutable historical records and must not import
``STUDY_STATES`` / ``STUDY_MODES`` (a later edit to those tuples must
not retroactively change what this revision does).
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | None = None
depends_on: str | None = None


# Frozen literals — do NOT replace with imports of STUDY_STATES /
# STUDY_MODES / the Job CheckConstraint.
_STUDY_STATE_CHECK = (
    "state IN ('draft','pending_approval','running','complete',"
    "'partial','failed','cancelled','rejected')"
)
_STUDY_MODE_CHECK = (
    "mode IN ('list','grid','frac_factorial','plackett_burman',"
    "'lhs','sobol','random','ccd','box_behnken')"
)
_STUDY_COUNTS_CHECK = "generated_count >= 0 AND skipped_invalid_count >= 0 AND run_cap >= 0"
_JOB_HOME_CHECK = "(scenario_id IS NOT NULL) <> (parametric_study_id IS NOT NULL)"

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

_NEW_PERMISSION = "parametric_studies.create"
_PERMISSION_ROLES = ("administrator", "beta_tester", "regular_user")


def upgrade() -> None:
    """Create parametric_study, dual-home the job table, seed the perm."""
    op.create_table(
        "parametric_study",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("project_id", _BIGINT, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_alias", sa.String(length=64), nullable=False),
        sa.Column("tool_version_id", _BIGINT, nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("factor_spec", sa.JSON(), nullable=False),
        sa.Column("sampling_params", sa.JSON(), nullable=False),
        sa.Column("run_cap", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("generated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "skipped_invalid_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("seed", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", _BIGINT, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state_changed_by_user_id", _BIGINT, nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_by_user_id", _BIGINT, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parametric_study")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_parametric_study_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tool_alias"],
            ["tool.alias"],
            name=op.f("fk_parametric_study_tool_alias_tool"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_version_id"],
            ["tool_version.id"],
            name=op.f("fk_parametric_study_tool_version_id_tool_version"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_parametric_study_created_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["state_changed_by_user_id"],
            ["user.id"],
            name=op.f("fk_parametric_study_state_changed_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["cancel_requested_by_user_id"],
            ["user.id"],
            name=op.f("fk_parametric_study_cancel_requested_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(_STUDY_STATE_CHECK, name=op.f("ck_parametric_study_state")),
        sa.CheckConstraint(_STUDY_MODE_CHECK, name=op.f("ck_parametric_study_mode")),
        sa.CheckConstraint(
            _STUDY_COUNTS_CHECK,
            name=op.f("ck_parametric_study_counts_non_negative"),
        ),
    )
    op.create_index("ix_parametric_study_project_id", "parametric_study", ["project_id"])
    op.create_index("ix_parametric_study_tool_alias", "parametric_study", ["tool_alias"])
    op.create_index(
        "ix_parametric_study_tool_version_id",
        "parametric_study",
        ["tool_version_id"],
    )

    # Job: add the study FK, relax scenario_id, add the home XOR CHECK.
    # Recreate-style batch so SQLite picks up the nullability change and
    # the new CHECK (it can't ALTER either in place). Postgres takes the
    # same batch API. The existing partial unique indexes
    # (uq_job_scenario_tool_standalone / uq_job_workflow_run_tool) and
    # all other constraints are preserved by the reflection-based
    # recreate, exactly as in migrations 0010 / 0031.
    with op.batch_alter_table("job", recreate="always") as batch:
        batch.add_column(sa.Column("parametric_study_id", _BIGINT, nullable=True))
        batch.alter_column("scenario_id", existing_type=_BIGINT, nullable=True)
        batch.create_foreign_key(
            op.f("fk_job_parametric_study_id_parametric_study"),
            "parametric_study",
            ["parametric_study_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index("ix_job_parametric_study_id", ["parametric_study_id"])
        batch.create_check_constraint("ck_job_exactly_one_home", _JOB_HOME_CHECK)

    _seed_permission()


def _seed_permission() -> None:
    """Grant ``parametric_studies.create`` to the built-in roles.

    Same idempotent pattern as migration 0023's ``custom_workflows.*``
    seed: skip on a greenfield install (built-in roles not yet seeded —
    the catalog defaults in bootstrap migration 0006 ship the grant)
    and skip any (role, perm) pair already present.
    """
    bind = op.get_bind()
    role_id_by_name: dict[str, int] = {
        row[1]: row[0]
        for row in bind.execute(
            sa.text(
                "SELECT id, name FROM role WHERE name IN "
                "('administrator','beta_tester','regular_user')"
            )
        ).fetchall()
    }
    if not role_id_by_name:
        return

    existing_role_ids: set[int] = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT role_id FROM role_permission WHERE permission = :p").bindparams(
                p=_NEW_PERMISSION
            )
        ).fetchall()
    }

    now = _dt.datetime.now(tz=_dt.UTC)
    rows = [
        {"role_id": role_id_by_name[name], "permission": _NEW_PERMISSION, "granted_at": now}
        for name in _PERMISSION_ROLES
        if name in role_id_by_name and role_id_by_name[name] not in existing_role_ids
    ]
    if rows:
        op.bulk_insert(
            sa.table(
                "role_permission",
                sa.column("role_id", sa.Integer()),
                sa.column("permission", sa.String()),
                sa.column("granted_at", sa.DateTime(timezone=True)),
            ),
            rows,
        )


def downgrade() -> None:
    """Reverse the schema additions.

    Re-imposing ``job.scenario_id NOT NULL`` would fail on any study
    child (those rows have ``scenario_id = NULL`` by design). Removing
    the feature means those jobs have no scenario home, so they are
    deleted first along with the studies. This is an intentionally
    one-way lossy downgrade, in the same spirit as migration 0031's
    state coercion — a greenfield install has none of these rows.
    """
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM role_permission WHERE permission = :p").bindparams(p=_NEW_PERMISSION)
    )

    op.execute("DELETE FROM job WHERE parametric_study_id IS NOT NULL")

    with op.batch_alter_table("job", recreate="always") as batch:
        batch.drop_constraint("ck_job_exactly_one_home", type_="check")
        batch.drop_index("ix_job_parametric_study_id")
        batch.drop_constraint(
            op.f("fk_job_parametric_study_id_parametric_study"),
            type_="foreignkey",
        )
        batch.drop_column("parametric_study_id")
        batch.alter_column("scenario_id", existing_type=_BIGINT, nullable=False)

    op.drop_index("ix_parametric_study_tool_version_id", table_name="parametric_study")
    op.drop_index("ix_parametric_study_tool_alias", table_name="parametric_study")
    op.drop_index("ix_parametric_study_project_id", table_name="parametric_study")
    op.drop_table("parametric_study")
