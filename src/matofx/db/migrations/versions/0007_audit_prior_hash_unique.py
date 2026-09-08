"""Unique constraint on audit_log.prior_hash.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-03

Defends against a direct ``INSERT`` (one that bypasses the writer's
``SELECT … FOR UPDATE`` on the chain head) introducing a forked chain.
With this constraint, two records can't claim the same ``prior_hash``;
attempts fail at the DB level. The genesis hash is unique because
each chain has exactly one first record.
"""

from __future__ import annotations

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the unique constraint."""
    with op.batch_alter_table("audit_log") as batch:
        batch.create_unique_constraint("uq_audit_log_prior_hash", ["prior_hash"])


def downgrade() -> None:
    """Drop the unique constraint."""
    with op.batch_alter_table("audit_log") as batch:
        batch.drop_constraint("uq_audit_log_prior_hash", type_="unique")
