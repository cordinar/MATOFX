"""Multi-role users & capability roles (ADR-0005).

Revision ID: 0044
Revises: 0043
Create Date: 2026-05-24

Backs ADR-0005 (``decisions/0005-multi-role-capabilities.md``): a user
keeps exactly one **base** role on ``user.role_id`` (the sole source of
privilege rank) and stacks zero or more permission-only **capability**
roles on top. Two schema changes plus a seed:

- ``role.kind`` — text, NOT NULL, server_default ``'base'``, CHECK
  ``kind IN ('base', 'capability')``. Existing rows (the four built-ins
  and any custom roles) backfill to ``'base'`` via the server default —
  every role created so far is a base role. SQLite can't ALTER-ADD a
  CHECK in place, so the column + constraint go through ``batch_alter_
  table`` (auto-recreate), the same technique migration 0018 used to
  add ``user.theme_preference``.

- ``user_role`` — the capability-role membership join: composite PK
  ``(user_id, role_id)``, ``granted_at``, and a nullable
  ``granted_by_user_id`` (SET NULL on actor deletion). Holds only
  ``capability``-kind roles; that invariant is enforced in the service
  layer, not the schema (a CHECK can't span tables).

- Seeds two built-in capability roles — ``library_curator`` (carries
  ``library.curate``) and ``materials_curator`` (carries
  ``materials.curate``) — and grants each its permission. The permission
  slugs are written as **frozen literals** here; migrations are
  immutable historical records and must not import the ``Permission``
  catalog (a later edit to that enum must not retroactively change what
  this revision does). Greenfield-safe: skips the seed when the role
  table has no built-ins yet, and is idempotent on the grant.

No data backfill beyond the ``role.kind`` server default: the v2 install
is greenfield and no capability memberships exist yet.
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | None = None
depends_on: str | None = None


# Frozen literals — do NOT replace with imports of the Permission / role
# catalogs. Each capability role is built-in (cannot be renamed/deleted)
# and carries exactly one curate permission.
_CAPABILITY_ROLES: tuple[dict[str, object], ...] = (
    {
        "name": "library_curator",
        "description": (
            "Capability role: curate the governed file library — add, version, assign, "
            "and retire reusable tool-input files. Stacks on top of a base role; carries "
            "no privilege rank."
        ),
        "permissions": ("library.curate",),
    },
    {
        "name": "materials_curator",
        "description": (
            "Capability role: curate the materials properties database — add, version, "
            "and retire materials. Stacks on top of a base role; carries no privilege rank."
        ),
        "permissions": ("materials.curate",),
    },
)


def upgrade() -> None:
    """Add role.kind, create user_role, seed the two capability roles."""
    # 1. role.kind column + CHECK. Auto-recreate batch handles SQLite,
    #    which can't ALTER-ADD a CHECK in place (mirrors 0018).
    with op.batch_alter_table("role") as batch:
        batch.add_column(
            sa.Column(
                "kind",
                sa.String(length=16),
                nullable=False,
                server_default="base",
            )
        )
        batch.create_check_constraint("ck_role_kind", "kind IN ('base', 'capability')")

    # 2. user_role capability-membership join.
    op.create_table(
        "user_role",
        sa.Column(
            "user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "granted_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("user_id", "role_id", name=op.f("pk_user_role")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_user_role_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["role.id"],
            name=op.f("fk_user_role_role_id_role"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"],
            ["user.id"],
            name=op.f("fk_user_role_granted_by_user_id_user"),
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_user_role_role_id", "user_role", ["role_id"])

    # 3. Seed the two capability roles + grant their permissions.
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

    for role in _CAPABILITY_ROLES:
        name = role["name"]
        existing = bind.execute(
            sa.text("SELECT id FROM role WHERE name = :n").bindparams(n=name)
        ).fetchone()
        if existing is None:
            op.bulk_insert(
                role_tbl,
                [
                    {
                        "name": name,
                        "description": role["description"],
                        "is_built_in": True,
                        "kind": "capability",
                        "created_by_user_id": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                ],
            )

        rid_row = bind.execute(
            sa.text("SELECT id FROM role WHERE name = :n").bindparams(n=name)
        ).fetchone()
        if rid_row is None:
            continue  # Greenfield: no role table content to attach to.
        rid = rid_row[0]

        already = {
            r[0]
            for r in bind.execute(
                sa.text("SELECT permission FROM role_permission WHERE role_id = :rid").bindparams(
                    rid=rid
                )
            ).fetchall()
        }
        rows = [
            {"role_id": rid, "permission": perm, "granted_at": now}
            for perm in role["permissions"]  # type: ignore[attr-defined]
            if perm not in already
        ]
        if rows:
            op.bulk_insert(perm_tbl, rows)


def downgrade() -> None:
    """Reverse — drop the seeded roles, the join, and role.kind.

    Removing a capability role drops its permission grants and any
    capability memberships first (the FKs would otherwise block the
    delete). A greenfield install has no memberships.
    """
    bind = op.get_bind()
    for role in _CAPABILITY_ROLES:
        rid_row = bind.execute(
            sa.text("SELECT id FROM role WHERE name = :n").bindparams(n=role["name"])
        ).fetchone()
        if rid_row is None:
            continue
        rid = rid_row[0]
        bind.execute(
            sa.text("DELETE FROM role_permission WHERE role_id = :rid").bindparams(rid=rid)
        )
        bind.execute(sa.text("DELETE FROM user_role WHERE role_id = :rid").bindparams(rid=rid))
        bind.execute(sa.text("DELETE FROM role WHERE id = :rid").bindparams(rid=rid))

    op.drop_index("ix_user_role_role_id", table_name="user_role")
    op.drop_table("user_role")

    with op.batch_alter_table("role") as batch:
        batch.drop_constraint("ck_role_kind", type_="check")
        batch.drop_column("kind")
