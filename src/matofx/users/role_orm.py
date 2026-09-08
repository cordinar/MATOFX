"""ORM models for the role and role-permission tables.

Kept in a separate module from :mod:`matofx.users.orm` so the User
table can take a ForeignKey to ``role`` without a circular import.
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


class Role(_Base):
    """A named set of permissions.

    Built-in roles (``super_user``, ``administrator``, ``beta_tester``,
    ``regular_user``) carry ``is_built_in = True`` and cannot be
    deleted or renamed. Their permission sets are editable at runtime
    (except ``super_user``, whose permissions are sentinel-managed in
    :func:`matofx.users.permissions.user_has_permission`).
    """

    __tablename__ = "role"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    is_built_in: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    kind: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="base", server_default="base"
    )
    """Discriminates the two role flavours (ADR-0005).

    ``'base'`` roles are assigned exactly one-per-user via
    ``User.role_id`` and are the sole source of privilege rank and all
    hierarchy / ACL semantics. ``'capability'`` roles are additive,
    permission-only grants stacked on top of the base role via the
    :class:`UserCapabilityRole` join — they carry no rank and no
    data-access semantics. Effective permissions are the union of the
    base role and every joined capability role (see
    :func:`matofx.users.permissions.user_has_permission`). Constrained
    by ``ck_role_kind``.
    """
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

    __table_args__: Any = (
        sa.CheckConstraint(
            "kind IN ('base', 'capability')",
            name="ck_role_kind",
        ),
    )


class RolePermission(_Base):
    """One permission granted to one role. Composite PK on (role_id, permission)."""

    __tablename__ = "role_permission"

    role_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("role.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    granted_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__: Any = (sa.Index("ix_role_permission_role_id", "role_id"),)


class UserCapabilityRole(_Base):
    """Membership of one user in one stackable capability role (ADR-0005).

    Composite PK on ``(user_id, role_id)``. This join holds **only**
    ``capability``-kind roles — the constraint is enforced in the
    service layer, not the schema, because a CHECK can't span tables.
    The user's single base role stays on ``User.role_id``; this table
    is purely additive.

    Named to avoid colliding with :data:`matofx.users.roles.UserRole`
    (the ``Literal`` of base-role names); the table itself is
    ``user_role`` per ADR-0005.
    """

    __tablename__ = "user_role"

    user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("role.id", ondelete="CASCADE"),
        primary_key=True,
    )
    granted_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    granted_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    """Admin who granted the membership. ``None`` when granted by the
    system (e.g. an in-migration seed)."""

    __table_args__: Any = (sa.Index("ix_user_role_role_id", "role_id"),)
