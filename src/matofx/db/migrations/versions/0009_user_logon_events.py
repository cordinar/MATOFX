"""User logon-access event log (multiple grant/revoke cycles).

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-03

Adds ``user_logon_event`` — append-only history of every time a user's
logon access was granted or revoked. The denormalised
``user.logon_granted_at`` / ``user.logon_revoked_at`` columns stay as
"latest cycle" pointers for fast reads; this table is the durable
history that a user may move through any number of times.

Backfill: for every existing user row, insert one ``granted`` event
at ``logon_granted_at`` and (if non-null) one ``revoked`` event at
``logon_revoked_at``. Both events have ``actor_user_id = NULL`` since
the original transitions pre-date the event log.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the event table and backfill from existing user rows."""
    op.create_table(
        "user_logon_event",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("event", sa.String(length=16), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_logon_event")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_user_logon_event_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["user.id"],
            name=op.f("fk_user_logon_event_actor_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "event IN ('granted', 'revoked')",
            name=op.f("ck_user_logon_event_event"),
        ),
    )
    op.create_index(
        "ix_user_logon_event_user_id",
        "user_logon_event",
        ["user_id"],
    )
    op.create_index(
        "ix_user_logon_event_user_at",
        "user_logon_event",
        ["user_id", "at"],
    )

    bind = op.get_bind()

    # Backfill one 'granted' event per user from logon_granted_at.
    bind.execute(
        sa.text(
            "INSERT INTO user_logon_event (user_id, event, at, actor_user_id, reason) "
            "SELECT id, 'granted', logon_granted_at, NULL, NULL "
            'FROM "user" '
            "WHERE logon_granted_at IS NOT NULL"
        )
    )

    # Backfill one 'revoked' event per deactivated user from logon_revoked_at.
    bind.execute(
        sa.text(
            "INSERT INTO user_logon_event (user_id, event, at, actor_user_id, reason) "
            "SELECT id, 'revoked', logon_revoked_at, NULL, NULL "
            'FROM "user" '
            "WHERE logon_revoked_at IS NOT NULL"
        )
    )


def downgrade() -> None:
    """Drop the event table. The denormalised pointers on ``user`` survive."""
    op.drop_index("ix_user_logon_event_user_at", table_name="user_logon_event")
    op.drop_index("ix_user_logon_event_user_id", table_name="user_logon_event")
    op.drop_table("user_logon_event")
