"""Tool-version bundle fingerprint — V&V cohort key (ADR-0011).

Revision ID: 0053
Revises: 0052
Create Date: 2026-05-27

Adds ``tool_version.bundle_hash`` (str(64), NOT NULL, server_default
``''``) — the SHA-256 hex of the bundle directory contents at snapshot
time. Per ADR-0011 / ``designs/vv-tracking.md`` §6.5, two ``tool_version``
rows sharing the same ``(alias, bundle_hash)`` are byte-equivalent
installations, and a V&V record on either applies to both (the V&V
cohort).

The empty string is a deliberate sentinel meaning "not yet computed":

- Greenfield install: no ``tool_version`` rows exist at migration time,
  so the server_default never applies in practice; the bump path will
  always compute a real hash on the first version that lands.
- A future install with pre-existing rows: every row gets ``''`` until
  a fresh snapshot is taken. The V&V badge query
  (:meth:`matofx.vv.VVService.summarise_tool_status`) treats ``''`` as
  a singleton cohort (no inheritance to or from), so this is a safe
  fallback — V&V never silently over-shares.

Also adds the composite index ``ix_tool_version_alias_bundle_hash``
on ``(alias, bundle_hash)`` — the V&V badge query joins on this exact
key, so the index pays for itself on the first read.

No ``batch_alter_table(recreate="always")``: we're adding a column +
index, not changing CHECKs or column nullability of an existing column.
A plain ``add_column`` + ``create_index`` works on both SQLite and
Postgres — matches the technique of migration ``0051``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``tool_version.bundle_hash`` + composite index."""
    with op.batch_alter_table("tool_version", recreate="never") as batch:
        batch.add_column(
            sa.Column(
                "bundle_hash",
                sa.String(length=64),
                nullable=False,
                server_default="",
            )
        )
        batch.create_index(
            "ix_tool_version_alias_bundle_hash",
            ["alias", "bundle_hash"],
        )


def downgrade() -> None:
    """Drop the index and the column. Hashes are lost — recompute on next bump."""
    with op.batch_alter_table("tool_version", recreate="never") as batch:
        batch.drop_index("ix_tool_version_alias_bundle_hash")
        batch.drop_column("bundle_hash")
