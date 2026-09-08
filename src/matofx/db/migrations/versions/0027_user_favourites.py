"""Per-user favourites for Quick Access tools and workflows.

Revision ID: 0027
Revises: 0026
Create Date: 2026-05-15

Adds:

- ``user_favourite`` — one row per (user, kind, alias) the user has
  starred. ``kind`` discriminates ``'tool'`` vs ``'workflow'`` so the
  same table backs both Quick Access sections on the Home page; the
  alias is stored as a free-form string (no FK) because tools and
  workflows live in different tables and an archived item's alias
  should not orphan the user's preference. Render-time visibility is
  what gates whether a star surfaces — favourites for items the user
  can no longer see simply don't render, and reappear if access
  returns.

Primary key is the ``(user_id, kind, alias)`` triple, which makes the
``PUT /api/me/favourites/{kind}/{alias}`` endpoint naturally
idempotent (a re-favourite is a no-op upsert) without needing a
separate UNIQUE index.

A CHECK constraint pins ``kind`` to the known discriminator values.
Adding a third Quick Access surface later is a migration.

No data is seeded: a fresh install has zero favourites and the table
fills as users click stars.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | None = None
depends_on: str | None = None


_KNOWN_KINDS: tuple[str, ...] = ("tool", "workflow")


def upgrade() -> None:
    """Create ``user_favourite`` with a composite PK and a CHECK on ``kind``."""
    op.create_table(
        "user_favourite",
        sa.Column(
            "user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey(
                "user.id", ondelete="CASCADE", name=op.f("fk_user_favourite_user_id_user")
            ),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("alias", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.PrimaryKeyConstraint("user_id", "kind", "alias", name=op.f("pk_user_favourite")),
        sa.CheckConstraint(
            f"kind IN ({','.join(repr(k) for k in _KNOWN_KINDS)})",
            name=op.f("ck_user_favourite_kind"),
        ),
    )


def downgrade() -> None:
    """Drop ``user_favourite`` entirely."""
    op.drop_table("user_favourite")
