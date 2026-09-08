"""Merkle tree size + root per compliance report (ADR-0019 anchoring rework).

Revision ID: 0091
Revises: 0090
Create Date: 2026-08-11

Anchoring used to compare the install's self-reported "previous head"
against the head the vendor last recorded, and fail when they differed.
They differ whenever a report is generated and never delivered — a
preview, a failed send, a dry run — so an honest install could be
permanently CONTRADICTED with no way back.

Consistency proofs (RFC 6962) replace that comparison, and they are
indexed by **tree size** rather than by "the previous report". To build a
proof from whichever head the vendor holds, an install has to remember
the tree size and root of every report it ever generated — hence these
two columns.

Existing rows are backfilled from ``head_id``: the tree has one leaf per
audit record, so a report whose head was record N attested a tree of size
N. The root is left NULL for those rows, which the report generator reads
as "recompute it" — the tree is derived from ``audit_log`` and is not
independent state, so nothing is lost.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0091"
down_revision: str | None = "0090"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``tree_size`` / ``tree_root`` to ``compliance_report``."""
    op.add_column(
        "compliance_report",
        sa.Column(
            "tree_size",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column("compliance_report", sa.Column("tree_root", sa.String(64), nullable=True))
    # One leaf per audit record, so the tree size a report attested is
    # exactly its head record id.
    op.execute(sa.text("UPDATE compliance_report SET tree_size = head_id"))


def downgrade() -> None:
    """Drop the Merkle columns."""
    op.drop_column("compliance_report", "tree_root")
    op.drop_column("compliance_report", "tree_size")
