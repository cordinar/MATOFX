"""Add ``domain`` column to ``runtime_setting``.

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-14

Widens ``runtime_setting`` so it can hold more than one catalog of
admin-tunable knobs. Until now the table carried only the runner
daemon's five rows (``local_concurrency``, heartbeat intervals,
cancel grace). The forthcoming General Settings page introduces a
parallel catalog (session timeouts, password policy, email,
housekeeping retention, branding). Both catalogs share the same
key/value/JSON shape, so a discriminator column is the cheapest
way to separate them.

Changes:

- Add ``domain`` ``VARCHAR(16) NOT NULL DEFAULT 'runner'``. Existing
  rows are backfilled by the column default during the column-add
  step, so no separate UPDATE statement is needed.
- Widen the primary key from ``(key)`` to ``(domain, key)``. Two
  catalogs can now coexist even if they happen to share a key name.
- Add a CHECK constraint pinning ``domain`` to the known catalog
  names. Adding a third catalog later is a migration.

No new rows are seeded here. The General Settings catalog falls
back to its catalog-defined defaults when a row is missing, so a
fresh install needs no seeding for the new domain; rows appear when
an admin first edits a value. This matches how a new key would be
added to the runner catalog in a later release.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | None = None
depends_on: str | None = None


_KNOWN_DOMAINS: tuple[str, ...] = ("runner", "general")


def upgrade() -> None:
    """Add ``domain`` column and reshape the PK to ``(domain, key)``."""
    with op.batch_alter_table("runtime_setting", recreate=recreate_on_sqlite()) as batch:
        batch.add_column(
            sa.Column(
                "domain",
                sa.String(length=16),
                nullable=False,
                server_default="runner",
            )
        )
        batch.drop_constraint(op.f("pk_runtime_setting"), type_="primary")
        batch.create_primary_key(
            op.f("pk_runtime_setting"),
            ["domain", "key"],
        )
        batch.create_check_constraint(
            "ck_runtime_setting_domain",
            f"domain IN ({','.join(repr(d) for d in _KNOWN_DOMAINS)})",
        )


def downgrade() -> None:
    """Reverse 0026.

    The downgrade drops every row outside the ``runner`` domain
    before shrinking the PK back — leaving ``general`` rows in
    place would orphan them under a single-column PK that no
    longer distinguishes catalogs.
    """
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM runtime_setting WHERE domain <> :d").bindparams(d="runner"))

    with op.batch_alter_table("runtime_setting", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("ck_runtime_setting_domain", type_="check")
        batch.drop_constraint(op.f("pk_runtime_setting"), type_="primary")
        batch.create_primary_key(op.f("pk_runtime_setting"), ["key"])
        batch.drop_column("domain")
