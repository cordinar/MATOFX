"""Seed the ``vv_curator`` capability role + ``vv.curate`` grants (ADR-0011).

Revision ID: 0056
Revises: 0055
Create Date: 2026-05-27

Backs ADR-0011 §4 / ``designs/vv-tracking.md`` §2.4 + §3.1: the V&V
tracking layer ships with a built-in capability role and a permission
grant on the ``administrator`` base role.

Two seeds:

1. **Capability role** — ``vv_curator``, carrying ``vv.curate``. Built-in
   (cannot be renamed/deleted; ADR-0005 invariant). Stacks on top of a
   user's base role via the ``user_role`` join, gaining the permission
   without any privilege rank. Mirrors the ``library_curator`` /
   ``materials_curator`` seeds from migration ``0044`` exactly.

2. **Administrator grant** — ``vv.curate`` granted to the seeded
   ``administrator`` role so an existing admin install picks up the
   V&V surface without manual fiddling. ``super_user`` holds it
   implicitly via the catalog sentinel
   (:func:`matofx.users.permissions.user_has_permission`). Mirrors the
   ``lineage.view`` seed from migration ``0052``.

**Not seeded here:** the ``vv_required_for_submit`` project-metadata
field row (design §3.3). ``project_metadata_field.created_by_user_id``
is NOT NULL with a RESTRICT FK to ``user``, and on a greenfield install
there are no users at migration time (the first one is created via
``matofx admin create-first-user`` *after* migrations run). The field
seed is deferred to runtime bootstrap (lands with slice 7 when the
submit-form gate actually consumes it).

CHECK / data shapes match the precedent migrations: capability-role
seed is greenfield-safe (skips when the ``role`` table has no rows
yet) and idempotent (skips rows that already exist).
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | None = None
depends_on: str | None = None


# Frozen literals — do NOT replace with imports of the Permission /
# role catalogs. The catalog can drift between heads; this migration is
# point-in-time.
_VV_CURATOR_ROLE: dict[str, object] = {
    "name": "vv_curator",
    "description": (
        "Capability role: curate the V&V (Verification & Validation) "
        "record ledger — promote draft V&V claims to approved on tools, "
        "jobs, and surrogate models, and stand in the directed-approver "
        "gate. Stacks on top of a base role; carries no privilege rank."
    ),
    "permissions": ("vv.curate",),
}

# Permissions granted onto the seeded ``administrator`` role.
_NEW_ADMIN_PERMISSIONS: tuple[str, ...] = ("vv.curate",)


def upgrade() -> None:
    """Seed ``vv_curator`` + grant ``vv.curate`` to administrator."""
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

    # 1. Seed the vv_curator capability role + its permission grant.
    name = _VV_CURATOR_ROLE["name"]
    existing = bind.execute(
        sa.text("SELECT id FROM role WHERE name = :n").bindparams(n=name)
    ).fetchone()
    if existing is None:
        op.bulk_insert(
            role_tbl,
            [
                {
                    "name": name,
                    "description": _VV_CURATOR_ROLE["description"],
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
        rows = [
            {"role_id": rid, "permission": perm, "granted_at": now}
            for perm in _VV_CURATOR_ROLE["permissions"]  # type: ignore[attr-defined]
            if perm not in already
        ]
        if rows:
            op.bulk_insert(perm_tbl, rows)

    # 2. Grant vv.curate onto the seeded administrator role (mirrors 0052).
    admin_id_row = bind.execute(
        sa.text("SELECT id FROM role WHERE name = :n").bindparams(n="administrator")
    ).fetchone()
    if admin_id_row is None:
        return  # Greenfield install: nothing seeded yet.
    admin_id: int = admin_id_row[0]

    existing_admin_perms = {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT permission FROM role_permission "
                "WHERE role_id = :rid AND permission IN :perms"
            ).bindparams(
                sa.bindparam("rid", admin_id),
                sa.bindparam("perms", list(_NEW_ADMIN_PERMISSIONS), expanding=True),
            )
        ).fetchall()
    }
    admin_rows = [
        {"role_id": admin_id, "permission": perm, "granted_at": now}
        for perm in _NEW_ADMIN_PERMISSIONS
        if perm not in existing_admin_perms
    ]
    if admin_rows:
        op.bulk_insert(perm_tbl, admin_rows)


def downgrade() -> None:
    """Drop the capability role + its grant, and the admin grant.

    Removing the capability role drops its permission grants and any
    capability memberships first (FKs would otherwise block the delete).
    A greenfield install has no memberships.
    """
    bind = op.get_bind()

    # 1. Strip the admin grant.
    for perm in _NEW_ADMIN_PERMISSIONS:
        bind.execute(
            sa.text(
                "DELETE FROM role_permission "
                "WHERE permission = :p AND role_id = "
                "(SELECT id FROM role WHERE name = 'administrator')"
            ).bindparams(p=perm)
        )

    # 2. Remove vv_curator + its permissions + its memberships.
    rid_row = bind.execute(
        sa.text("SELECT id FROM role WHERE name = :n").bindparams(n=_VV_CURATOR_ROLE["name"])
    ).fetchone()
    if rid_row is None:
        return
    rid = rid_row[0]
    bind.execute(sa.text("DELETE FROM role_permission WHERE role_id = :rid").bindparams(rid=rid))
    bind.execute(sa.text("DELETE FROM user_role WHERE role_id = :rid").bindparams(rid=rid))
    bind.execute(sa.text("DELETE FROM role WHERE id = :rid").bindparams(rid=rid))
