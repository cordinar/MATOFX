"""Teams pending: an imported tool/workflow whose teams didn't resolve here.

A cross-install import (ADR-0023) matches teams by name. When a tool or
workflow named teams and **none** of them exist on the destination, it is
imported with no teams — but an empty team list means "visible to everyone",
so it must not be promotable until someone picks its teams. ``teams_pending``
records exactly that: set by import, cleared by the first edit that saves a
non-empty team list, and checked by ``promote_state``.

Revision ID: 0098
Revises: 0097
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ``teams_pending`` to tool and workflow; every existing row is resolved."""
    for table in ("tool", "workflow"):
        op.add_column(
            table,
            sa.Column("teams_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    """Drop the flag; a pending item simply becomes promotable again."""
    for table in ("workflow", "tool"):
        op.drop_column(table, "teams_pending")
