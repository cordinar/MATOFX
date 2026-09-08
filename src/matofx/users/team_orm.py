"""ORM models for teams and team membership.

Per §I46 / §D20 of the brief, "flat teams" are v1.0 must-ship.
A team is a named set of users. Multi-team membership is supported
(legacy parity — the legacy app stored each user's teams as a JSON
list; v2 normalises into a join table).

Two tables:

- ``team`` — the catalog of teams in the install. Customer-specific;
  no built-in seeds (legacy hardcoded ``World`` / ``R&D`` / etc., but
  different customers want different team taxonomies).
- ``user_team`` — the many-to-many join. Composite primary key on
  ``(user_id, team_id)``. Carries ``joined_at`` and ``joined_by_user_id``
  for audit-adjacent context.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class Team(_Base):
    """A named group of users."""

    __tablename__ = "team"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class UserTeam(_Base):
    """User → team membership. One row per (user, team) pair."""

    __tablename__ = "user_team"

    user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    team_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("team.id", ondelete="CASCADE"),
        primary_key=True,
    )
    joined_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    joined_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__: Any = (sa.Index("ix_user_team_team_id", "team_id"),)
