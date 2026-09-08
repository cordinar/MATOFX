"""Custom workflows: kind/owner_id/visibility on workflow + workflow_share + perms.

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-11

Extends the existing ``workflow`` table to host both admin-released and
user-owned ("custom") workflows behind a single ``kind`` discriminator,
adds a ``workflow_share`` junction table for per-user sharing, and seeds
the two new permission slugs.

Schema additions on ``workflow``:

- ``kind`` (``'admin'`` | ``'custom'``, NOT NULL, server_default
  ``'admin'`` so every existing row backfills cleanly).
- ``owner_id`` (FK ``user.id``, nullable; NOT NULL for ``custom`` rows
  per the CHECK constraint; NULL for ``admin`` rows).
- ``visibility`` (``'private'`` | ``'shared'``, nullable; NOT NULL for
  ``custom`` rows; NULL for ``admin`` rows).

Constraint changes on ``workflow``:

- Replaces the old ``state IN ('new','test','released')`` check with a
  combined check covering both kinds:

  - ``kind='admin'``  → ``state IN ('new','test','released')``,
    ``owner_id IS NULL``, ``visibility IS NULL``.
  - ``kind='custom'`` → ``state = 'released'`` (custom rows live only
    in the released state — they don't ramp through new/test like
    admin workflows), ``owner_id IS NOT NULL``,
    ``visibility IN ('private','shared')``.

``alias`` stays globally unique. Custom-workflow aliases are
synthesised with an owner-derived prefix in the service layer
(``custom_<owner_id>_<slug>``) so two users picking the same name
don't collide.

New ``workflow_share`` table: junction row per share recipient. Bears
its own ``shared_at`` + ``shared_by_user_id`` so the admin inspector
can show "Alice shared this with Bob on 2026-05-11". ON DELETE CASCADE
from both the workflow and the user so revoking access is just a row
delete.

Permissions seeded:

- ``custom_workflows.create`` → granted to ``administrator``,
  ``beta_tester``, ``regular_user`` (super_user gets it via the
  catalog sentinel).
- ``custom_workflows.inspect`` → granted to ``administrator`` only
  (super_user via sentinel).
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | None = None
depends_on: str | None = None


_OLD_STATE_CHECK = "state IN ('new','test','released')"
_NEW_COMBINED_CHECK = (
    "("
    "(kind = 'admin' AND owner_id IS NULL AND visibility IS NULL "
    "AND state IN ('new','test','released'))"
    " OR "
    "(kind = 'custom' AND owner_id IS NOT NULL "
    "AND visibility IN ('private','shared') AND state = 'released')"
    ")"
)


def upgrade() -> None:
    """Add custom-workflow columns + workflow_share table + seed perms."""
    # ---- workflow: add three columns + replace the state CHECK ----------
    with op.batch_alter_table("workflow") as batch:
        batch.add_column(
            sa.Column(
                "kind",
                sa.String(length=16),
                nullable=False,
                server_default="admin",
            )
        )
        batch.add_column(
            sa.Column(
                "owner_id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "visibility",
                sa.String(length=16),
                nullable=True,
            )
        )
        batch.create_foreign_key(
            op.f("fk_workflow_owner_id_user"),
            "user",
            ["owner_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_workflow_kind", ["kind"])
        batch.create_index("ix_workflow_owner_id", ["owner_id"])
        # The legacy state CHECK is now too narrow. Drop + re-add via the
        # batch rebuild so the combined kind/state/owner/visibility check
        # takes its place.
        batch.drop_constraint("ck_workflow_state", type_="check")
        batch.create_check_constraint(
            "ck_workflow_kind_state_owner_visibility",
            _NEW_COMBINED_CHECK,
        )
        batch.create_check_constraint("ck_workflow_kind", "kind IN ('admin','custom')")

    # ---- workflow_share junction table ----------------------------------
    op.create_table(
        "workflow_share",
        sa.Column(
            "workflow_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("shared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "shared_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("workflow_id", "user_id", name=op.f("pk_workflow_share")),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflow.id"],
            name=op.f("fk_workflow_share_workflow_id_workflow"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_workflow_share_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shared_by_user_id"],
            ["user.id"],
            name=op.f("fk_workflow_share_shared_by_user_id_user"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_workflow_share_user_id", "workflow_share", ["user_id"])

    # ---- seed the two new permissions on built-in roles -----------------
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
        # Greenfield install — built-in roles not seeded yet. The
        # bootstrap migration (0006) ships these via the catalog
        # defaults, so we don't need to retro-seed.
        return

    new_grants: list[tuple[str, str]] = []
    for role_name in ("administrator", "beta_tester", "regular_user"):
        if role_name in role_id_by_name:
            new_grants.append((role_name, "custom_workflows.create"))
    if "administrator" in role_id_by_name:
        new_grants.append(("administrator", "custom_workflows.inspect"))

    # Skip any (role, perm) already present.
    existing_pairs: set[tuple[int, str]] = {
        (row[0], row[1])
        for row in bind.execute(
            sa.text(
                "SELECT role_id, permission FROM role_permission "
                "WHERE permission IN "
                "('custom_workflows.create','custom_workflows.inspect')"
            )
        ).fetchall()
    }

    now = _dt.datetime.now(tz=_dt.UTC)
    rows = [
        {"role_id": role_id_by_name[role_name], "permission": perm, "granted_at": now}
        for role_name, perm in new_grants
        if (role_id_by_name[role_name], perm) not in existing_pairs
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
    """Drop the new perms + workflow_share + revert workflow columns."""
    bind = op.get_bind()
    for perm in ("custom_workflows.create", "custom_workflows.inspect"):
        bind.execute(
            sa.text("DELETE FROM role_permission WHERE permission = :p").bindparams(p=perm)
        )

    op.drop_index("ix_workflow_share_user_id", table_name="workflow_share")
    op.drop_table("workflow_share")

    with op.batch_alter_table("workflow") as batch:
        batch.drop_constraint("ck_workflow_kind", type_="check")
        batch.drop_constraint("ck_workflow_kind_state_owner_visibility", type_="check")
        batch.create_check_constraint("ck_workflow_state", _OLD_STATE_CHECK)
        batch.drop_index("ix_workflow_owner_id")
        batch.drop_index("ix_workflow_kind")
        batch.drop_constraint(op.f("fk_workflow_owner_id_user"), type_="foreignkey")
        batch.drop_column("visibility")
        batch.drop_column("owner_id")
        batch.drop_column("kind")
