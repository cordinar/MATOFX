"""Compliance attestation: install identity + archived reports (ADR-0019 slice 1).

Revision ID: 0088
Revises: 0087
Create Date: 2026-08-09

Two tables:

- ``install_identity`` — singleton (``id = 1``) holding the Funis-issued
  install identity and the **monotonic report counter**. The counter is
  what makes a withheld reporting period visible to the vendor, so it is
  never reset — a re-issued identity for the same ``install_id`` keeps it.
  Key *ids* only; the private report key stays in the identity file on
  disk and is deliberately never persisted here.
- ``compliance_report`` — every generated report, archived verbatim, so a
  re-request is answerable and the install can prove what it asserted.

No data migration: both tables start empty and are populated by
``matofx compliance adopt`` / ``matofx compliance report``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0088"
down_revision: str | None = "0087"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create ``install_identity`` and ``compliance_report``."""
    op.create_table(
        "install_identity",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("install_id", sa.String(64), nullable=False, unique=True),
        sa.Column("customer", sa.String(255), nullable=False),
        sa.Column("environment", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("report_key_id", sa.String(32), nullable=False),
        sa.Column("issuer_key_id", sa.String(64), nullable=False),
        sa.Column("identity_sha256", sa.String(64), nullable=False),
        sa.Column("report_counter", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "compliance_report",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("counter", sa.Integer(), nullable=False, unique=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "head_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("head_hash", sa.String(64), nullable=False),
        sa.Column("chain_ok", sa.Boolean(), nullable=False),
        sa.Column("licence_type", sa.String(16), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("generated_by", sa.String(255), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_compliance_report_generated_at", "compliance_report", ["generated_at"]
    )


def downgrade() -> None:
    """Drop both tables."""
    op.drop_index("ix_compliance_report_generated_at", table_name="compliance_report")
    op.drop_table("compliance_report")
    op.drop_table("install_identity")
