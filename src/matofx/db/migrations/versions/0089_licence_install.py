"""Licence subsystem: the installed-licence record (ADR-0019 slice 2).

Revision ID: 0089
Revises: 0088
Create Date: 2026-08-10

One singleton row (``id = 1``) caching the currently-installed licence.
The signed file on disk stays the authority and is re-verified on every
load; this row exists so the API and runner can read licence terms
without filesystem access, and so a *changed* licence is detectable.

Deliberately absent: the report key seed. Like the install-identity
file, the signing secret stays on disk rather than landing in every
database backup.

No data migration — the table starts empty and is populated by
``matofx licence install``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0089"
down_revision: str | None = "0088"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create ``licence_install``."""
    op.create_table(
        "licence_install",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("licence_id", sa.String(128), nullable=False),
        sa.Column("customer", sa.String(255), nullable=False),
        sa.Column("environment", sa.String(64), nullable=False),
        sa.Column("licence_type", sa.String(16), nullable=False),
        sa.Column("n_slots", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grace_period_days", sa.Integer(), nullable=False),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("machine_binding", sa.String(64), nullable=True),
        sa.Column("banner_text", sa.String(512), nullable=True),
        sa.Column("report_key_id", sa.String(32), nullable=True),
        sa.Column("issuer_key_id", sa.String(64), nullable=False),
        sa.Column("issuer_verified", sa.Boolean(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("source_path", sa.String(1024), nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("installed_by", sa.String(255), nullable=False),
        sa.CheckConstraint("licence_type IN ('csl', 'fal')", name="ck_licence_type"),
        sa.CheckConstraint("n_slots >= 1", name="ck_licence_n_slots_positive"),
    )


def downgrade() -> None:
    """Drop ``licence_install``."""
    op.drop_table("licence_install")
