"""Optimisation start-approval gate: per-tool flag + approval target + approver role.

Revision ID: 0070
Revises: 0069
Create Date: 2026-06-08

Phase 13 (ADR-0009 §14.1 / §12.7). A per-tool toggleable gate lets
operators require sign-off *before* an optimisation campaign starts
(it can queue hundreds of expensive children). Three additive changes:

- ``tool.optimisation_start_approval_required`` (bool, default false) — a
  mutable per-tool operational lever (mirrors ``run_cap``). A study whose
  target tool — or any tool in a workflow chain — has it set routes
  through the start gate at ``configure``.
- ``approval_request.optimisation_study_id`` — the eighth approval target
  (nullable FK, CASCADE); the exactly-one-of-target CHECK widens 7 → 8,
  plus the pending-only partial unique index. The directed-approver
  machinery is reused wholesale (``kind='pre'``).
- ``optimisation_approver`` capability role + the ``optimisation.approve``
  permission (ADR-0005 pattern), granted to ``administrator``. The
  eligible-approver pool for the start gate; no other assignment by
  default. (``OptimisationStudy.start_approver_user_id`` + the
  ``pending_start_approval`` state already exist from migration 0066.)

Frozen literals only — migrations never import the ORM tuples / catalog.
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

_TARGET_ONE_OF_EIGHT = (
    "(CASE WHEN job_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN parametric_study_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN library_file_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN material_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN tool_vv_record_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN job_vv_record_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN surrogate_model_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN optimisation_study_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)
_TARGET_ONE_OF_SEVEN = (
    "(CASE WHEN job_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN parametric_study_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN library_file_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN material_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN tool_vv_record_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN job_vv_record_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN surrogate_model_version_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)

_ROLE_NAME = "optimisation_approver"
_ROLE_DESCRIPTION = (
    "Approve the start of an optimisation campaign (ADR-0009 §14.1) — sign off on the "
    "compute footprint before hundreds of children queue."
)
_PERMISSION = "optimisation.approve"


def upgrade() -> None:
    """Add the per-tool flag + the optimisation approval target + the approver role."""
    op.add_column(
        "tool",
        sa.Column(
            "optimisation_start_approval_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    with op.batch_alter_table("approval_request", recreate=recreate_on_sqlite()) as batch:
        batch.add_column(sa.Column("optimisation_study_id", _BIGINT, nullable=True))
        batch.create_foreign_key(
            op.f("fk_approval_request_optimisation_study_id_optimisation_study"),
            "optimisation_study",
            ["optimisation_study_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index("ix_approval_request_optimisation_study_id", ["optimisation_study_id"])
        batch.drop_constraint("ck_approval_request_exactly_one_target", type_="check")
        batch.create_check_constraint(
            "ck_approval_request_exactly_one_target", _TARGET_ONE_OF_EIGHT
        )
    op.create_index(
        "uq_approval_request_pending_optimisation_kind",
        "approval_request",
        ["optimisation_study_id", "kind"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )

    _seed_approver_role()


def _seed_approver_role() -> None:
    bind = op.get_bind()
    now = _dt.datetime.now(tz=_dt.UTC)
    role_tbl = sa.table(
        "role",
        sa.column("name", sa.String()),
        sa.column("description", sa.String()),
        sa.column("is_built_in", sa.Boolean()),
        sa.column("kind", sa.String()),
        sa.column("created_by_user_id", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    perm_tbl = sa.table(
        "role_permission",
        sa.column("role_id", sa.Integer()),
        sa.column("permission", sa.String()),
        sa.column("granted_at", sa.DateTime(timezone=True)),
    )

    existing = bind.execute(
        sa.text("SELECT id FROM role WHERE name = :n").bindparams(n=_ROLE_NAME)
    ).fetchone()
    if existing is None:
        op.bulk_insert(
            role_tbl,
            [
                {
                    "name": _ROLE_NAME,
                    "description": _ROLE_DESCRIPTION,
                    "is_built_in": True,
                    "kind": "capability",
                    "created_by_user_id": None,
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )
    rid_row = bind.execute(
        sa.text("SELECT id FROM role WHERE name = :n").bindparams(n=_ROLE_NAME)
    ).fetchone()
    if rid_row is not None:
        rid = rid_row[0]
        already = {
            r[0]
            for r in bind.execute(
                sa.text("SELECT permission FROM role_permission WHERE role_id = :rid").bindparams(
                    rid=rid
                )
            ).fetchall()
        }
        if _PERMISSION not in already:
            op.bulk_insert(
                perm_tbl, [{"role_id": rid, "permission": _PERMISSION, "granted_at": now}]
            )

    admin_row = bind.execute(
        sa.text("SELECT id FROM role WHERE name = :n").bindparams(n="administrator")
    ).fetchone()
    if admin_row is None:
        return
    admin_id = admin_row[0]
    has_perm = bind.execute(
        sa.text(
            "SELECT 1 FROM role_permission WHERE role_id = :rid AND permission = :p"
        ).bindparams(rid=admin_id, p=_PERMISSION)
    ).fetchone()
    if has_perm is None:
        op.bulk_insert(
            perm_tbl, [{"role_id": admin_id, "permission": _PERMISSION, "granted_at": now}]
        )


def downgrade() -> None:
    """Reverse — drop the role/permission, the approval target, the tool flag."""
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM role_permission WHERE permission = :p").bindparams(p=_PERMISSION)
    )
    bind.execute(
        sa.text(
            "DELETE FROM user_role WHERE role_id IN (SELECT id FROM role WHERE name = :n)"
        ).bindparams(n=_ROLE_NAME)
    )
    bind.execute(sa.text("DELETE FROM role WHERE name = :n").bindparams(n=_ROLE_NAME))

    op.execute("DELETE FROM approval_request WHERE optimisation_study_id IS NOT NULL")
    op.drop_index("uq_approval_request_pending_optimisation_kind", table_name="approval_request")
    with op.batch_alter_table("approval_request", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("ck_approval_request_exactly_one_target", type_="check")
        batch.create_check_constraint(
            "ck_approval_request_exactly_one_target", _TARGET_ONE_OF_SEVEN
        )
        batch.drop_index("ix_approval_request_optimisation_study_id")
        batch.drop_constraint(
            op.f("fk_approval_request_optimisation_study_id_optimisation_study"),
            type_="foreignkey",
        )
        batch.drop_column("optimisation_study_id")

    op.drop_column("tool", "optimisation_start_approval_required")
