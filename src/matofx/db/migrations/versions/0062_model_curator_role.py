"""Seed the ``model_curator`` capability role + ``models.curate`` grants (ADR-0008 §5.7).

Revision ID: 0062
Revises: 0061
Create Date: 2026-06-02

Backs ``designs/surrogate-family.md`` §5.7 / §14: the surrogate Model
registry's governance ships with a built-in capability role and an
admin grant — exactly mirroring the ``vv_curator`` seed (migration 0056)
and the ``library_curator`` / ``materials_curator`` seeds (0044).

Two seeds:

1. **Capability role** — ``model_curator``, carrying ``models.curate``.
   Built-in (cannot be renamed/deleted; ADR-0005 invariant), install-global
   (per-tool curation is v1.x). A holder is *eligible* to be nominated as
   the technical checker for any tool's surrogate; release authority is
   separate (a super-user / admin action, not this permission).
2. **Administrator grant** — ``models.curate`` granted to the seeded
   ``administrator`` role so an existing admin install picks up the
   surface without manual fiddling. ``super_user`` holds it implicitly
   via the catalog sentinel.

Greenfield-safe (skips when the ``role`` table is empty) and idempotent
(skips rows that already exist) — the 0056 template exactly.
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen literals — do NOT import the Permission / role catalogs.
_MODEL_CURATOR_ROLE: dict[str, object] = {
    "name": "model_curator",
    "description": (
        "Capability role: act as the technical checker for global "
        "surrogate-model publishes — sign off a model version before a "
        "super-user releases it to general use. Install-global; stacks on "
        "a base role; carries no privilege rank."
    ),
    "permissions": ("models.curate",),
}
_NEW_ADMIN_PERMISSIONS: tuple[str, ...] = ("models.curate",)


def upgrade() -> None:
    """Seed ``model_curator`` + grant ``models.curate`` to administrator."""
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

    name = _MODEL_CURATOR_ROLE["name"]
    existing = bind.execute(
        sa.text("SELECT id FROM role WHERE name = :n").bindparams(n=name)
    ).fetchone()
    if existing is None:
        op.bulk_insert(
            role_tbl,
            [
                {
                    "name": name,
                    "description": _MODEL_CURATOR_ROLE["description"],
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
            for perm in _MODEL_CURATOR_ROLE["permissions"]  # type: ignore[attr-defined]
            if perm not in already
        ]
        if rows:
            op.bulk_insert(perm_tbl, rows)

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
    """Drop the capability role + its grant, and the admin grant."""
    bind = op.get_bind()

    for perm in _NEW_ADMIN_PERMISSIONS:
        bind.execute(
            sa.text(
                "DELETE FROM role_permission "
                "WHERE permission = :p AND role_id = "
                "(SELECT id FROM role WHERE name = 'administrator')"
            ).bindparams(p=perm)
        )

    name = _MODEL_CURATOR_ROLE["name"]
    rid_row = bind.execute(
        sa.text("SELECT id FROM role WHERE name = :n").bindparams(n=name)
    ).fetchone()
    if rid_row is not None:
        rid = rid_row[0]
        bind.execute(sa.text("DELETE FROM user_role WHERE role_id = :rid").bindparams(rid=rid))
        bind.execute(
            sa.text("DELETE FROM role_permission WHERE role_id = :rid").bindparams(rid=rid)
        )
        bind.execute(sa.text("DELETE FROM role WHERE id = :rid").bindparams(rid=rid))
