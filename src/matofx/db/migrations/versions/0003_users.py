"""User accounts and authentication sessions.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-03

Creates ``user`` (local accounts) and ``auth_session`` (live sessions
keyed by hashed token).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create user + auth_session tables."""
    op.create_table(
        "user",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("failed_login_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user")),
        sa.UniqueConstraint("username", name=op.f("uq_user_username")),
        sa.UniqueConstraint("email", name=op.f("uq_user_email")),
        sa.CheckConstraint(
            "role IN ('super_user','administrator','beta_tester','regular_user')",
            name="ck_user_role",
        ),
    )

    op.create_table(
        "auth_session",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
            autoincrement=True,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=False,
        ),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_session_token_hash")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_auth_session_user_id_user"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(op.f("ix_auth_session_user_id"), "auth_session", ["user_id"])
    op.create_index("ix_auth_session_last_activity", "auth_session", ["last_activity_at"])


def downgrade() -> None:
    """Drop auth_session + user."""
    op.drop_index("ix_auth_session_last_activity", table_name="auth_session")
    op.drop_index(op.f("ix_auth_session_user_id"), table_name="auth_session")
    op.drop_table("auth_session")
    op.drop_table("user")
