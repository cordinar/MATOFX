"""Trusted-peer table for cross-install Tools/Workflows migration.

Revision ID: 0028
Revises: 0027
Create Date: 2026-05-15

Adds:

- ``migrate_trusted_peer`` — one row per source install whose signed
  bundles this install will accept. The destination admin manually
  adds a fingerprint here before any import from that source can
  succeed. Mirrors the SSH ``known_hosts`` pattern: signature
  authenticity is provable from the bundle alone, but *trust* is an
  explicit, audit-logged operator decision per pair.

The ``migrate.tools_workflows`` permission gates export, import, and
trust-list management. It is **not** seeded onto the
``administrator`` role by this migration — the user's call (recorded
in ``feedback_migrate_reset_to_new.md`` and the §Q5 amendment in
``PROJECT_BRIEF.md``) is that super-users hold it by default (via the
:data:`SUPER_USER_PERMISSIONS` sentinel) and may grant it to
administrators case-by-case through the standard role-edit UI. Adding
the permission slug to :class:`Permission` is enough; no row needed.

The table is intentionally lean: identity (fingerprint), human label,
who added it, when, optionally when it was last used (for "stale
trust entry" diagnostics), and a free-text note. Removing a trust
entry is a hard delete (no soft-delete) since the audit-log entry
for the removal is the durable record.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create ``migrate_trusted_peer`` with a unique fingerprint index."""
    op.create_table(
        "migrate_trusted_peer",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column(
            "fingerprint",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "label",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "added_by_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_migrate_trusted_peer")),
        sa.UniqueConstraint(
            "fingerprint",
            name=op.f("uq_migrate_trusted_peer_fingerprint"),
        ),
        sa.ForeignKeyConstraint(
            ["added_by_user_id"],
            ["user.id"],
            name=op.f("fk_migrate_trusted_peer_added_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "length(fingerprint) > 0",
            name=op.f("ck_migrate_trusted_peer_fingerprint_nonempty"),
        ),
        sa.CheckConstraint(
            "length(label) > 0",
            name=op.f("ck_migrate_trusted_peer_label_nonempty"),
        ),
    )


def downgrade() -> None:
    """Drop ``migrate_trusted_peer`` entirely."""
    op.drop_table("migrate_trusted_peer")
