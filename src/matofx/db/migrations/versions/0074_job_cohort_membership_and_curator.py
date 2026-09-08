"""Job Cohorts: membership columns + the ``cohort_curator`` capability role.

Revision ID: 0074
Revises: 0073
Create Date: 2026-06-11

Backs ``designs/job-cohorts.md`` §5-§6 (slice 2 — schema + governance).
Two independent concerns, both greenfield-safe and idempotent:

1. **Membership columns** on ``job_cohort`` (D4):
   - ``auto_grow`` (BOOL) — live re-resolve vs frozen snapshot;
   - ``member_job_ids`` (JSON) — the frozen set when not auto-grow;
   - ``excluded_job_ids`` (JSON) — manual drops from a live cohort.
   Every existing cohort is a live saved query, so the backfill sets
   ``auto_grow = TRUE`` (``member_job_ids`` stays NULL) — no cohort
   changes membership. New raw inserts default to FALSE via the
   server-default; the create *service* sets ``auto_grow = TRUE``
   explicitly until the snapshot-resolution branch ships (the membership
   slice), so nothing is left as a static cohort with an empty frozen
   list.

   SQLite can't add a NOT NULL column with no default to a populated
   table, so ``auto_grow`` goes in nullable, is backfilled, then tightened
   to NOT NULL via ``batch_alter_table`` — the 0058 technique.

2. **Governance** (D6): seed the built-in ``cohort_curator`` capability
   role carrying ``cohorts.curate``, and grant ``cohorts.curate`` to the
   ``administrator`` role — the exact ``model_curator`` seed shape
   (migration 0062). ``super_user`` holds it implicitly via the catalog
   sentinel. Greenfield installs (empty ``role`` table) skip the seed.
"""

from __future__ import annotations

import datetime as _dt

import sqlalchemy as sa
from alembic import op

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen literals — a migration must not import the ORM / Permission catalogs.
_COHORT_CURATOR_ROLE: dict[str, object] = {
    "name": "cohort_curator",
    "description": (
        "Capability role: create, edit, and archive Job Cohorts — the "
        "saved-query batches of completed runs that feed a surrogate fit. "
        "Install-global; stacks on a base role; carries no privilege rank. "
        "Viewing cohorts is open to everyone."
    ),
    "permissions": ("cohorts.curate",),
}
_NEW_ADMIN_PERMISSIONS: tuple[str, ...] = ("cohorts.curate",)


def upgrade() -> None:
    """Add the membership columns (+ backfill) and seed the curator role."""
    # --- 1. membership columns -------------------------------------------
    op.add_column("job_cohort", sa.Column("auto_grow", sa.Boolean(), nullable=True))
    op.add_column("job_cohort", sa.Column("member_job_ids", sa.JSON(), nullable=True))
    op.add_column("job_cohort", sa.Column("excluded_job_ids", sa.JSON(), nullable=True))

    # Every existing cohort is a live saved query.
    op.execute("UPDATE job_cohort SET auto_grow = 1")

    with op.batch_alter_table("job_cohort") as batch:
        batch.alter_column(
            "auto_grow",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        )

    # --- 2. cohort_curator capability role + grants ----------------------
    _seed_curator_role()


def _seed_curator_role() -> None:
    """Seed ``cohort_curator`` + grant ``cohorts.curate`` to administrator."""
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

    name = _COHORT_CURATOR_ROLE["name"]
    existing = bind.execute(
        sa.text("SELECT id FROM role WHERE name = :n").bindparams(n=name)
    ).fetchone()
    if existing is None:
        op.bulk_insert(
            role_tbl,
            [
                {
                    "name": name,
                    "description": _COHORT_CURATOR_ROLE["description"],
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
            for perm in _COHORT_CURATOR_ROLE["permissions"]  # type: ignore[attr-defined]
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
    """Drop the curator role + grants, then the membership columns."""
    bind = op.get_bind()

    for perm in _NEW_ADMIN_PERMISSIONS:
        bind.execute(
            sa.text(
                "DELETE FROM role_permission "
                "WHERE permission = :p AND role_id = "
                "(SELECT id FROM role WHERE name = 'administrator')"
            ).bindparams(p=perm)
        )

    name = _COHORT_CURATOR_ROLE["name"]
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

    op.drop_column("job_cohort", "excluded_job_ids")
    op.drop_column("job_cohort", "member_job_ids")
    op.drop_column("job_cohort", "auto_grow")
