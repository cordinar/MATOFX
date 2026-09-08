"""SQLAlchemy ORM model for per-user favourites.

One row per (user, kind, alias) the user has starred. The
``user_favourite`` table is the persistence side of the Quick
Access favourites feature; see :mod:`matofx.favourites` for the
package-level summary and :class:`FavouritesService` for the
mutation surface.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata

FAVOURITE_KINDS: Final[tuple[str, ...]] = ("tool", "workflow")
"""Allowed values for :attr:`UserFavourite.kind`.

Mirrored by the CHECK constraint created in migration ``0027``.
Adding a third Quick Access surface (e.g. ``'project'``) means
loosening both the CHECK and this tuple in a single migration so
they can never drift.
"""


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class UserFavourite(_Base):
    """One starred tool or workflow for one user.

    The composite ``(user_id, kind, alias)`` primary key makes
    ``PUT /api/me/favourites/{kind}/{alias}`` naturally idempotent —
    re-favouriting is a no-op upsert rather than an INSERT that
    needs to dodge a unique-constraint violation.

    The alias column intentionally has no foreign key. Tools and
    workflows live in different tables, and an archived item's
    alias should not orphan the user's preference: render-time
    visibility is what decides whether a star appears, so a stale
    favourite is simply invisible until access returns.
    """

    __tablename__ = "user_favourite"

    user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    kind: Mapped[str] = mapped_column(sa.String(16), primary_key=True)
    """Discriminator: ``'tool'`` or ``'workflow'``. Constrained at
    the DB level by ``ck_user_favourite_kind`` so a typo in
    service code can't smuggle a third value into the table."""

    alias: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    """Tool or workflow alias as it appears in the respective
    catalog. Stored verbatim — render-time lookup decides
    visibility."""

    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=sa.func.current_timestamp(),
    )
    """When the user first starred this item. Kept for audit /
    troubleshooting; the Quick Access sort order is alphabetical
    by display name, not chronological."""

    __table_args__: Any = (
        sa.CheckConstraint(
            f"kind IN ({','.join(repr(k) for k in FAVOURITE_KINDS)})",
            name="ck_user_favourite_kind",
        ),
    )

    def __repr__(self) -> str:
        """Debug representation."""
        return f"UserFavourite(user_id={self.user_id!r}, kind={self.kind!r}, alias={self.alias!r})"
