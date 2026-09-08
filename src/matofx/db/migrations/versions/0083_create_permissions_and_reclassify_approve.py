"""Base-tier create permissions + reclassify optimisation.approve + scrub ADRs.

Introduces the create-vs-curate split across the studies + governed
surfaces:

- Grants the new base-tier ``*.create`` permissions
  (``optimisation.create`` / ``models.create`` / ``cohorts.create`` /
  ``vv.create`` / ``library.create`` / ``materials.create``) to **every**
  base role (``super_user`` holds them via the catalog sentinel, no row
  needed). These are ordinary base-tier grants — any base role may hold
  them and an admin tunes them per role via the standard role editor.
  Before this split these surfaces were ungated, so granting to every
  base role preserves create access for all existing users (regular /
  beta / custom) rather than regressing them to 403, and seeds the same
  broad default on a fresh install.
- Removes ``optimisation.approve`` from every base role: it is now
  classified capability-tier (the ``.approve`` suffix), so a base role may
  no longer hold it. Migration 0070 granted it to the ``administrator``
  base role (and seeded the ``optimisation_approver`` capability pool
  **empty**), so approvers held it solely through their base role. Before
  stripping the base grants, every user who currently holds approve via a
  base role is migrated into the ``optimisation_approver`` capability pool
  so they keep their start-approval authority.
- Scrubs ``ADR-####`` references from every ``role.description`` so the
  Teams / Access Rights Groups page (which renders these) shows no internal
  ADR citations. Runs after the seeding migrations, so fresh installs end
  up clean too.
"""

from __future__ import annotations

import datetime as _dt
import re

import sqlalchemy as sa
from alembic import op

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels: str | None = None
depends_on: str | None = None


_NEW_CREATE_PERMISSIONS: tuple[str, ...] = (
    "optimisation.create",
    "models.create",
    "cohorts.create",
    "vv.create",
    "library.create",
    "materials.create",
)
_RECLASSIFIED_PERMISSION = "optimisation.approve"
_APPROVER_ROLE_NAME = "optimisation_approver"

_ADR_PAREN = re.compile(r"\s*\(ADR-\d+[^)]*\)")
_ADR_BARE = re.compile(r"\s*ADR-\d+(?:\s*§[\d.]+)?")


def _perm_table() -> sa.Table:
    return sa.table(
        "role_permission",
        sa.column("role_id", sa.Integer()),
        sa.column("permission", sa.String()),
        sa.column("granted_at", sa.DateTime(timezone=True)),
    )


def _member_table() -> sa.Table:
    return sa.table(
        "user_role",
        sa.column("user_id", sa.Integer()),
        sa.column("role_id", sa.Integer()),
        sa.column("granted_at", sa.DateTime(timezone=True)),
        sa.column("granted_by_user_id", sa.Integer()),
    )


def _strip_adr(text: str) -> str:
    """Remove ``(ADR-#### …)`` parentheticals and bare ``ADR-####`` mentions."""
    text = _ADR_PAREN.sub("", text)
    text = _ADR_BARE.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _role_id(bind: sa.engine.Connection, name: str) -> int | None:
    row = bind.execute(sa.text("SELECT id FROM role WHERE name = :n").bindparams(n=name)).fetchone()
    return None if row is None else int(row[0])


def upgrade() -> None:
    """Grant the new create perms to every base role, migrate approve to the
    capability pool, scrub ADRs."""
    bind = op.get_bind()
    now = _dt.datetime.now(tz=_dt.UTC)
    perm_tbl = _perm_table()

    # The new *.create permissions are ordinary base-tier grants: any base
    # role may hold them. Grant them to every base role except super_user
    # (which holds the whole catalog via the sentinel — no row needed). On an
    # existing install these surfaces were previously ungated, so granting to
    # every base role preserves create access for all current users
    # (regular / beta / custom) instead of regressing them to 403; on a fresh
    # install it seeds the same broad default. Admins can revoke per role via
    # the editable role editor.
    base_role_ids = [
        int(r[0])
        for r in bind.execute(
            sa.text("SELECT id FROM role WHERE kind = 'base' AND name != 'super_user'")
        ).fetchall()
    ]
    for rid in base_role_ids:
        held = {
            r[0]
            for r in bind.execute(
                sa.text("SELECT permission FROM role_permission WHERE role_id = :rid").bindparams(
                    rid=rid
                )
            ).fetchall()
        }
        new_rows = [
            {"role_id": rid, "permission": slug, "granted_at": now}
            for slug in _NEW_CREATE_PERMISSIONS
            if slug not in held
        ]
        if new_rows:
            op.bulk_insert(perm_tbl, new_rows)

    # optimisation.approve is now capability-tier (the ".approve" suffix), so a
    # base role may no longer hold it. Migration 0070 granted it to the
    # administrator base role (and seeded the optimisation_approver capability
    # pool EMPTY), so approvers held it solely through their base role. Before
    # stripping the base grants, migrate every user who currently holds approve
    # via a base role into the optimisation_approver capability pool so they
    # keep their start-approval authority. (super_user holds it via the
    # sentinel, not a row, so it is correctly excluded from the join.)
    approver_role_id = _role_id(bind, _APPROVER_ROLE_NAME)
    if approver_role_id is not None:
        holder_user_ids = [
            int(r[0])
            for r in bind.execute(
                sa.text(
                    "SELECT u.id FROM user u "
                    "JOIN role_permission rp ON rp.role_id = u.role_id "
                    "WHERE rp.permission = :p"
                ).bindparams(p=_RECLASSIFIED_PERMISSION)
            ).fetchall()
        ]
        existing_members = {
            int(r[0])
            for r in bind.execute(
                sa.text("SELECT user_id FROM user_role WHERE role_id = :rid").bindparams(
                    rid=approver_role_id
                )
            ).fetchall()
        }
        member_rows = [
            {
                "user_id": uid,
                "role_id": approver_role_id,
                "granted_at": now,
                "granted_by_user_id": None,
            }
            for uid in holder_user_ids
            if uid not in existing_members
        ]
        if member_rows:
            op.bulk_insert(_member_table(), member_rows)

    # Now drop optimisation.approve from EVERY base role. While it was
    # base-tier an admin could grant it to any base role; left in place those
    # grants violate the base/capability non-overlap invariant and make the
    # role uneditable (set_permissions raises PermissionKindMismatchError). The
    # capability pool (optimisation_approver) owns the permission now.
    bind.execute(
        sa.text(
            "DELETE FROM role_permission WHERE permission = :p AND role_id IN "
            "(SELECT id FROM role WHERE kind = 'base')"
        ).bindparams(p=_RECLASSIFIED_PERMISSION)
    )

    # Scrub ADR references from every role description.
    for rid, desc in bind.execute(
        sa.text("SELECT id, description FROM role WHERE description IS NOT NULL")
    ).fetchall():
        cleaned = _strip_adr(str(desc))
        if cleaned != desc:
            bind.execute(
                sa.text("UPDATE role SET description = :d WHERE id = :rid").bindparams(
                    d=cleaned, rid=rid
                )
            )


def downgrade() -> None:
    """Reverse the permission grants (the ADR description scrub is not restored)."""
    bind = op.get_bind()
    now = _dt.datetime.now(tz=_dt.UTC)
    perm_tbl = _perm_table()

    # Remove the new base-tier create grants from EVERY base role (upgrade
    # granted them to every base role, so the inverse must too).
    for slug in _NEW_CREATE_PERMISSIONS:
        bind.execute(
            sa.text(
                "DELETE FROM role_permission WHERE permission = :p AND role_id IN "
                "(SELECT id FROM role WHERE kind = 'base')"
            ).bindparams(p=slug)
        )

    # Restore optimisation.approve to the administrator base role (the state
    # migration 0070 left it in). The optimisation_approver capability pool
    # memberships added by upgrade() are left in place: we can't distinguish
    # migration-added approvers from admin-assigned ones, and a redundant pool
    # membership is harmless (effective permissions are a union). This mirrors
    # the ADR description scrub, which is likewise not reversed.
    admin_id = _role_id(bind, "administrator")
    if admin_id is not None:
        already = bind.execute(
            sa.text(
                "SELECT 1 FROM role_permission WHERE role_id = :rid AND permission = :p"
            ).bindparams(rid=admin_id, p=_RECLASSIFIED_PERMISSION)
        ).fetchone()
        if already is None:
            op.bulk_insert(
                perm_tbl,
                [{"role_id": admin_id, "permission": _RECLASSIFIED_PERMISSION, "granted_at": now}],
            )
