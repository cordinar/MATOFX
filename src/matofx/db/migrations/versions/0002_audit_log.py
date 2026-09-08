"""Audit log tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-03

Creates ``audit_log`` (the immutable record chain) and
``audit_chain_head`` (the singleton head pointer used to serialise
appends). Both register against the shared MATOFX metadata; constraint
names follow the project's naming convention.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

GENESIS_HASH = "0" * 64


def upgrade() -> None:
    """Create audit_log + audit_chain_head, seed the head row."""
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("source_ip", sa.String(length=64), nullable=True),
        sa.Column("prior_hash", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.String(length=128), nullable=True),
        sa.Column("key_id", sa.String(length=32), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        sa.UniqueConstraint("hash", name=op.f("uq_audit_log_hash")),
    )
    op.create_index(op.f("ix_audit_log_timestamp"), "audit_log", ["timestamp"])
    op.create_index(op.f("ix_audit_log_action"), "audit_log", ["action"])
    op.create_index(op.f("ix_audit_log_correlation_id"), "audit_log", ["correlation_id"])
    op.create_index("ix_audit_log_resource", "audit_log", ["resource_type", "resource_id"])

    op.create_table(
        "audit_chain_head",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "last_record_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column("last_hash", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_chain_head")),
    )
    op.bulk_insert(
        sa.table(
            "audit_chain_head",
            sa.column("id", sa.Integer()),
            sa.column("last_record_id", sa.BigInteger()),
            sa.column("last_hash", sa.String()),
        ),
        [{"id": 1, "last_record_id": None, "last_hash": GENESIS_HASH}],
    )


def downgrade() -> None:
    """Drop the audit tables."""
    op.drop_table("audit_chain_head")
    op.drop_index("ix_audit_log_resource", table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_correlation_id"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_action"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_timestamp"), table_name="audit_log")
    op.drop_table("audit_log")
