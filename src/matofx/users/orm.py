"""SQLAlchemy ORM models for users and authentication sessions.

Two tables register against the shared MATOFX metadata so Alembic
autogenerate sees them.

- ``user`` — one row per local account.
- ``auth_session`` — one row per active session, identified by the
  SHA-256 of a never-stored client-side token.
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


class User(_Base):
    """Local user account.

    The user's role is referenced by ``role_id`` (foreign key into the
    ``role`` table — see :mod:`matofx.users.role_orm`). The four
    built-in roles are seeded by migration ``0006_custom_roles``;
    admins can create custom roles at runtime without touching code.
    """

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    username: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(sa.String(254), nullable=False, unique=True)
    first_name: Mapped[str] = mapped_column(sa.String(128), nullable=False, default="")
    last_name: Mapped[str] = mapped_column(sa.String(128), nullable=False, default="")
    full_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    """Display name; auto-derived from ``first_name`` + ``last_name`` at create time."""

    password_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    is_approver: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    """Whether the user may be selected as an approver on a tool's
    ``approver_usernames`` list. Independent of role; admins can flip
    this on any user (subject to ``users.manage`` permission)."""

    email_notifications_enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )
    """Per-user opt-in for job-state notification emails. Layered with
    the tool descriptor's ``email_notifications`` flag: an email is
    sent only when both the tool and the recipient are opted in. The
    user toggles this themselves on the Account page; admins do not
    set it on behalf of users."""

    summary_enrichment_enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )
    """Per-user opt-in for AI-enriched result summaries
    (``designs/auto-summaries.md``). When ``False`` the user always sees
    the deterministic templated baseline, even where the install has enrichment
    on. It does *not* gate whether enrichment runs (that is the install-wide
    ``llm_summaries_enabled`` setting): enrichment runs once per result and both
    texts are cached; this flag only selects which one to render for this user.
    The user toggles it on the Account page."""

    theme_preference: Mapped[str] = mapped_column(
        sa.String(8), nullable=False, default="dark", server_default="dark"
    )
    """Dashboard theme — ``'dark'`` or ``'light'``. Read by the chrome
    on every page render so a user signing in from a different machine
    keeps their chosen look. Validated by a CHECK constraint at the DB
    level; the Account page is the only surface that flips it."""

    locale_preference: Mapped[str] = mapped_column(
        sa.String(35), nullable=False, default="auto", server_default="auto"
    )
    """BCP-47 locale tag the UI renders numbers / dates / times in
    (``'en-GB'``, ``'fr-FR'``, …) or the literal string ``'auto'`` to
    inherit whatever the browser reports. Surfaced live in the top-
    banner clock and in every :func:`matofx.ui.format.format_user_datetime`
    rendering. The wire format and audit log are unaffected — those
    stay UTC + ISO-8601."""

    timezone_preference: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, default="auto", server_default="auto"
    )
    """IANA timezone identifier (``'Europe/London'``, ``'America/New_York'``,
    …) or the literal string ``'auto'`` to inherit the browser's
    detected zone. ``'auto'`` lets a single user signing in from
    different locations see local time without re-configuring."""

    ui_preferences: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict, server_default=sa.text("'{}'")
    )
    """Per-user dashboard-shell preferences blob (home-redesign §9). A
    single JSON object so new "what to show" toggles can be added
    without a migration per setting. Like ``theme_preference`` it is
    read on every page render and written through on change, so it
    survives logout and follows the user across devices. Current keys:

    - ``sidebar_pinned`` (bool) — sidebar pinned open vs. icon-rail.
    - ``sidebar_width`` (int, clamped 188–460) — pinned rail width.
    - ``sidebar_groups_collapsed`` (dict[str, bool]) — per-group
      collapse state, keyed by group label.
    - ``quick_access_filter`` (``'tool'`` | ``'workflow'`` | ``'all'``)
      — the Quick Access card's kind filter.

    The shape is validated + clamped in
    :meth:`matofx.users.service.UserService.set_ui_preferences`; unknown
    keys are dropped so a stale client can't poison the blob."""

    role_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("role.id", ondelete="RESTRICT"),
        nullable=False,
    )
    failed_login_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    locked_until: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_grace_warning_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    """When the inactivity grace-warning email was last sent to this
    user for their *current* inactivity stretch. Cleared on the next
    successful login so a re-stale user gets a fresh warning later
    rather than being silently skipped. Read by
    :mod:`matofx.users.inactive_sweeper`; ``None`` means "no warning
    sent for the current stretch"."""
    logon_granted_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    """When the user was granted login access. Updated on reactivate
    so a deactivate / reactivate cycle records the new grant date."""

    logon_revoked_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    """When the user's login access was last revoked (deactivated).
    Cleared on reactivate."""

    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__: Any = (
        sa.CheckConstraint(
            "theme_preference IN ('light', 'dark')",
            name="ck_user_theme_preference",
        ),
    )

    def __repr__(self) -> str:
        """Debug representation; never logged in user-facing surfaces."""
        return f"User(id={self.id!r}, username={self.username!r}, role_id={self.role_id!r})"


class UserLogonEvent(_Base):
    """One row per logon-access state transition for a user.

    Append-only, never updated. ``user.logon_granted_at`` and
    ``user.logon_revoked_at`` are denormalised "latest cycle"
    pointers — this table is the durable history. A user may be
    granted and revoked any number of times.

    Events:

    - ``granted`` — written on initial create and on every
      :meth:`UserService.reactivate` call.
    - ``revoked`` — written on every :meth:`UserService.deactivate`
      call.
    """

    __tablename__ = "user_logon_event"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    """``"granted"`` or ``"revoked"``."""

    at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    """Who triggered the transition. ``None`` for the initial
    'granted' event written by the bootstrap CLI / system actor."""

    reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    """Optional plain-English note shown alongside the event in the
    admin UI's user-history pane."""

    __table_args__: Any = (
        sa.CheckConstraint(
            "event IN ('granted', 'revoked')",
            name="ck_user_logon_event_event",
        ),
        sa.Index("ix_user_logon_event_user_at", "user_id", "at"),
    )


class AuthSession(_Base):
    """One active sign-in session. Lookup is by ``token_hash``, never by id."""

    __tablename__ = "auth_session"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ip_address: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    last_activity_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    revoked_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    breakglass: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default=sa.false()
    )
    """Admitted *over* the licence cap as a Super User (ADR-0019 §C13).

    Marked so the over-cap sessions can be shown on Admin → Active
    sessions, and so they can be time-boxed: break-glass is never
    refused, but it must not quietly become a permanent excess."""

    __table_args__: Any = (sa.Index("ix_auth_session_last_activity", "last_activity_at"),)
