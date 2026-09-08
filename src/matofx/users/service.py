"""User and session services.

These services are the only path through which user accounts and
sessions are created, queried, or mutated. Every state-changing
operation records an audit event through the supplied
:class:`AuditWriter`.

Build a single :class:`UserService` and a single :class:`SessionService`
at process startup, share them across requests. Both are stateless
beyond the dependencies they hold (hasher, audit writer, settings).
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from matofx.audit.schema import RecordInput
from matofx.users.errors import (
    AccountDisabledError,
    DuplicateUserError,
    IncorrectCurrentPasswordError,
    InvalidCredentialsError,
    InvalidEmailError,
    RoleNotFoundError,
    SessionExpiredError,
    SessionInvalidError,
    SuperUserRoleChangeRequiresSuperUserError,
    WeakPasswordError,
)
from matofx.users.orm import AuthSession, User, UserLogonEvent
from matofx.users.passwords import (
    PasswordHasher,
    make_password_hasher,
    validate_password,
)
from matofx.users.role_orm import Role
from matofx.users.roles import DEFAULT_ROLE
from matofx.users.tokens import hash_session_token, new_session_token

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter
    from matofx.config import AuthSettings


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


# How stale ``last_activity_at`` must be before ``validate()`` rewrites it.
# Every authenticated request runs ``validate()``; writing on each one turns
# a read burst (e.g. the parametric-study results page firing the page poll
# plus a timer per plot card, all at once) into a SQLite write storm that
# trips "database is locked". Coalescing to at most one write per window per
# session collapses the storm while leaving sliding-idle semantics intact —
# the window is tiny next to the minutes-scale idle timeout, so the recorded
# activity marker is never more than this stale.
_ACTIVITY_WRITE_COALESCE = _dt.timedelta(seconds=30)

BREAKGLASS_MAX_HOURS = 12
"""Longest a break-glass session lives, however generous the site policy.

A Super User admitted *over* the licence cap (ADR-0019 §C13) is being
given emergency access to fix a licence, not a permanent extra seat. The
number of such admissions is deliberately unlimited — capping it would
lock out exactly the administrator break-glass exists to admit, which is
the failure it was built to prevent — so the bound is on duration
instead, where there is no such trade.

Twelve hours covers any plausible working session on the problem and
still expires within a day. The site's own hard timeout wins if it is
shorter; this only ever tightens.
"""


def _as_aware(ts: _dt.datetime) -> _dt.datetime:
    """Coerce a possibly-naive datetime to UTC-aware.

    SQLite drops timezone information on DateTime columns; values read
    back are naive. Postgres preserves tz. Comparing the two raises
    ``TypeError``, so every DB-loaded datetime that we compare against
    ``_utcnow()`` goes through this helper.
    """
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=_dt.UTC)


@dataclass(frozen=True)
class CreatedSession:
    """Result of a successful sign-in.

    The plaintext ``token`` is returned to the caller exactly once;
    only its hash is persisted. Lose the plaintext, you lose the session.
    """

    user_id: int
    token: str
    expires_at: _dt.datetime
    # The ``auth_session`` row id. Needed by the CSL slot re-check, which
    # has to identify *this* session among the live ones to decide
    # whether it is inside the cap (ADR-0019 slice 4).
    session_id: int = 0


@dataclass(frozen=True)
class UserService:
    """User CRUD + authentication."""

    settings: AuthSettings
    hasher: PasswordHasher
    audit: AuditWriter

    async def create_user(
        self,
        session: AsyncSession,
        *,
        username: str,
        email: str,
        password: str,
        first_name: str = "",
        last_name: str = "",
        full_name: str | None = None,
        role: str = DEFAULT_ROLE,
        is_approver: bool = False,
        actor: str = "system",
        actor_user_id: int | None = None,
        reason: str | None = None,
    ) -> User:
        """Create a new user account; raises on validation failure.

        Identity fields:

        - ``first_name`` and ``last_name`` are stored separately for
          UX (sorting, salutations, name-on-output documents). Pass
          one or both; ``full_name`` is auto-derived as
          ``f"{first_name} {last_name}".strip()`` when not supplied
          explicitly.
        - ``full_name`` may be supplied for legacy parity (single
          display string); when set, the split form is left empty.
        - ``username`` and ``email`` are canonicalised to lower-case
          so ``"Alice"`` and ``"alice"`` are the same account, and
          emails differing only by case can't slip past the unique
          constraint.

        Defaults:

        - ``role``: :data:`matofx.users.roles.DEFAULT_ROLE`
          (``"regular_user"``).
        - ``is_approver``: ``False`` — flip on (via this field at
          create or via :meth:`set_approver` later) when the user
          should be eligible to be selected as a tool approver.
        - ``logon_granted_at``: now; ``logon_revoked_at``: ``None``.
        """
        username = _canonicalise_username(username)
        email = _normalise_email(email)
        first_name = first_name.strip()
        last_name = last_name.strip()
        full_name = f"{first_name} {last_name}".strip() if full_name is None else full_name.strip()

        from matofx.users.role_service import (  # noqa: PLC0415
            SUPER_USER_NAME,
            user_is_super_user,
        )

        role_row = await _resolve_role_by_name(session, role)
        if role_row is None:
            raise RoleNotFoundError(role)

        # Creating a super_user account is a grant of super_user and is
        # reserved to super-users themselves — an administrator holding
        # users.create_with_role can't mint one. The system bootstrap path
        # (no acting user) is exempt: it seeds the install's first super-user.
        if (
            role_row.name == SUPER_USER_NAME
            and actor_user_id is not None
            and not await user_is_super_user(session, actor_user_id)
        ):
            await self.audit.record(
                session,
                RecordInput(
                    actor=actor,
                    actor_type="system" if actor == "system" else "user",
                    action="auth.user.create",
                    resource_type="user",
                    resource_id=username,
                    outcome="denied",
                    reason="Only a super-user can create a super_user account.",
                    payload={"username": username, "attempted_role": role_row.name},
                ),
            )
            raise SuperUserRoleChangeRequiresSuperUserError

        reason = validate_password(password, self.settings)
        if reason is not None:
            raise WeakPasswordError(reason)

        now = _utcnow()
        user = User(
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
            full_name=full_name,
            password_hash=self.hasher.hash(password),
            is_active=True,
            is_approver=is_approver,
            role_id=role_row.id,
            logon_granted_at=now,
        )
        session.add(user)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            field = "username" if "username" in str(exc).lower() else "email"
            raise DuplicateUserError(field) from exc

        session.add(
            UserLogonEvent(
                user_id=user.id,
                event="granted",
                at=now,
                actor_user_id=actor_user_id,
                reason=reason,
            )
        )
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="system" if actor == "system" else "user",
                action="auth.user.create",
                resource_type="user",
                resource_id=str(user.id),
                outcome="success",
                payload={
                    "username": username,
                    "role": role_row.name,
                    "role_id": role_row.id,
                    "is_approver": is_approver,
                },
            ),
        )
        return user

    async def authenticate(
        self,
        session: AsyncSession,
        *,
        username: str,
        password: str,
    ) -> User:
        """Verify credentials. Raises on every failure mode.

        Failures collapse into :class:`InvalidCredentialsError` to prevent
        username enumeration. The account-disabled state is revealed
        (:class:`AccountDisabledError`) **only once the correct password
        has been supplied** — checking it before the password would let an
        attacker enumerate disabled accounts by status code (a disabled
        account would answer differently to a wrong password than an
        unknown user). A caller who already knows the password isn't
        enumerating, so naming the disabled state for them is safe.
        """
        username = _canonicalise_username(username)
        user = await _by_username(session, username)
        now = _utcnow()

        if user is None:
            # Equalise timing with the known-user path: an Argon2 verify
            # against a precomputed dummy hash takes the same wall time
            # as a real verify, so an attacker can't tell from timing
            # whether a username exists.
            self.hasher.equalising_verify(None, password)
            await self._audit_login_failure(session, username, "unknown_user")
            raise InvalidCredentialsError

        if user.locked_until is not None and _as_aware(user.locked_until) > now:
            await self._audit_login_failure(session, username, "locked", user_id=user.id)
            raise InvalidCredentialsError

        if not self.hasher.verify(user.password_hash, password):
            await self._record_failed_attempt(session, user)
            await self._audit_login_failure(session, username, "wrong_password", user_id=user.id)
            raise InvalidCredentialsError

        # Password is correct. Only now is it safe to surface the
        # account's disabled state — doing so above would leak it to an
        # attacker who never supplied the right password.
        if not user.is_active:
            await self._audit_login_failure(session, username, "disabled", user_id=user.id)
            raise AccountDisabledError

        # Success — clear failure counters, optionally rehash, audit.
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now
        # A fresh successful login resets the inactivity clock: clear
        # the grace-warning dedup so the next inactivity stretch
        # earns a new warning rather than being silently skipped.
        user.last_grace_warning_at = None
        if self.hasher.needs_rehash(user.password_hash):
            user.password_hash = self.hasher.hash(password)
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{user.id}",
                actor_type="user",
                action="auth.login.success",
                resource_type="user",
                resource_id=str(user.id),
                outcome="success",
            ),
        )
        return user

    async def deactivate(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        actor: str,
        actor_user_id: int | None = None,
        reason: str | None = None,
    ) -> None:
        """Deactivate an account, stamp ``logon_revoked_at``, revoke sessions.

        Appends a ``revoked`` row to ``user_logon_event`` so the full
        grant/revoke history of the account is preserved across many
        cycles. The denormalised ``logon_revoked_at`` on ``user`` is
        the latest-cycle pointer.

        Approver-list cascade: when the user appears in any tool's
        ``approver_usernames``, those entries are removed
        automatically (and audited). If the user is the *only*
        approver on any tool the call is refused with
        :class:`SoleApproverError` so the admin gets a friendly
        prompt to add another approver first.

        Defence-in-depth: ``SessionService.validate`` already refuses
        sessions for inactive users, but cascading the revoke here
        means the deactivation is reflected in the ``auth_session``
        table immediately rather than on next request.
        """
        from matofx.tools.approvers import remove_approver_from_tools  # noqa: PLC0415
        from matofx.users.errors import LastSuperUserError  # noqa: PLC0415
        from matofx.users.role_service import (  # noqa: PLC0415
            SUPER_USER_NAME,
            assert_not_last_super_user,
        )

        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            return

        # Last-super-user invariant: deactivating the only active super-user
        # would leave the install with no break-glass admin. Refuse it for the
        # same reason a role demotion is refused (role_service).
        if user.role_id is not None:
            role = await session.get(Role, user.role_id)
            if role is not None and role.name == SUPER_USER_NAME:
                try:
                    await assert_not_last_super_user(session, user_id)
                except LastSuperUserError:
                    await self.audit.record(
                        session,
                        RecordInput(
                            actor=actor,
                            actor_type="user",
                            action="auth.user.deactivate",
                            resource_type="user",
                            resource_id=str(user_id),
                            outcome="denied",
                            reason="Would leave the install without an active super-user.",
                        ),
                    )
                    raise

        await remove_approver_from_tools(
            session,
            username=user.username,
            actor_user_id=actor_user_id if actor_user_id is not None else user_id,
            audit=self.audit,
            audit_action="tools.tool.approver.cascade_remove_on_deactivate",
        )

        revoked_at = _utcnow()
        user.is_active = False
        user.logon_revoked_at = revoked_at

        await session.execute(
            update(AuthSession)
            .where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )
        session.add(
            UserLogonEvent(
                user_id=user_id,
                event="revoked",
                at=revoked_at,
                actor_user_id=actor_user_id,
                reason=reason,
            )
        )
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="user",
                action="auth.user.deactivate",
                resource_type="user",
                resource_id=str(user_id),
                outcome="success",
            ),
        )

    async def reactivate(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        actor: str,
        actor_user_id: int | None = None,
        reason: str | None = None,
    ) -> None:
        """Re-enable a previously deactivated account.

        Stamps a fresh ``logon_granted_at``, clears
        ``logon_revoked_at``, and appends a ``granted`` event to
        ``user_logon_event``. Existing revoked sessions stay revoked
        — the user signs in fresh.
        """
        user = await session.get(User, user_id)
        if user is None or user.is_active:
            return
        granted_at = _utcnow()
        user.is_active = True
        user.logon_granted_at = granted_at
        user.logon_revoked_at = None
        user.failed_login_count = 0
        user.locked_until = None
        # Reset the inactivity clock so the next sweep doesn't
        # immediately re-disable a just-reactivated user. The
        # ``last_login_at`` semantics widen here to "last time the
        # account was usable" — which is what the sweeper compares
        # against. Audited as ``auth.user.reactivate`` with a
        # ``clock_reset`` payload note so the change is visible.
        user.last_login_at = granted_at
        user.last_grace_warning_at = None
        session.add(
            UserLogonEvent(
                user_id=user_id,
                event="granted",
                at=granted_at,
                actor_user_id=actor_user_id,
                reason=reason,
            )
        )
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="user",
                action="auth.user.reactivate",
                resource_type="user",
                resource_id=str(user_id),
                outcome="success",
                payload={"inactivity_clock_reset": True},
            ),
        )

    async def update_identity(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        first_name: str,
        last_name: str,
        email: str,
        full_name: str | None = None,
        actor: str,
    ) -> User:
        """Update the user's identity fields (name + email).

        ``email`` is canonicalised + validated; a duplicate raises
        :class:`DuplicateUserError` so the API can map cleanly to 409.
        ``full_name`` is auto-derived from first + last when not
        supplied, mirroring :meth:`create_user`. Username is *not*
        editable here — usernames are referential keys (audit
        ``actor`` strings, tool approver lists) and renaming them is
        a separate Phase 2 concern.
        """
        user = await session.get(User, user_id)
        if user is None:
            raise InvalidCredentialsError
        first_name = first_name.strip()
        last_name = last_name.strip()
        derived = (
            full_name.strip()
            if full_name and full_name.strip()
            else f"{first_name} {last_name}".strip()
        )
        new_email = _normalise_email(email)

        if (
            user.first_name == first_name
            and user.last_name == last_name
            and user.full_name == derived
            and user.email == new_email
        ):
            return user

        user.first_name = first_name
        user.last_name = last_name
        user.full_name = derived
        user.email = new_email
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateUserError("email") from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="user",
                action="auth.user.update_identity",
                resource_type="user",
                resource_id=str(user_id),
                outcome="success",
                payload={
                    "first_name": first_name,
                    "last_name": last_name,
                    "full_name": derived,
                    "email": new_email,
                },
            ),
        )
        return user

    async def set_password(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        password: str,
        actor: str,
    ) -> User:
        """Admin-initiated password reset.

        Runs :func:`validate_password` against the current settings
        (length, character classes, etc.) and rejects weak passwords
        with :class:`WeakPasswordError`. The reset is recorded via the
        audit log; the new plaintext is *not* stored anywhere — only
        its Argon2 hash. Existing sessions are *not* revoked
        automatically: that's a separate "force log-out" decision the
        admin can take via deactivate / reactivate if they want.
        """
        user = await session.get(User, user_id)
        if user is None:
            raise InvalidCredentialsError
        weakness = validate_password(password, self.settings)
        if weakness is not None:
            raise WeakPasswordError(weakness)
        user.password_hash = self.hasher.hash(password)
        user.failed_login_count = 0
        user.locked_until = None
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="user",
                action="auth.user.set_password",
                resource_type="user",
                resource_id=str(user_id),
                outcome="success",
            ),
        )
        return user

    async def change_password_self(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        current_password: str,
        new_password: str,
    ) -> User:
        """Self-service password change.

        Verifies the supplied ``current_password`` against the stored
        hash before validating + persisting ``new_password``. Audited
        as ``auth.user.change_password`` with the user as the actor.

        Distinct from :meth:`set_password` (admin-initiated) so the
        audit trail records *who* changed the password — self vs
        admin — and so we can require proof of possession on the
        self path without forcing it on the admin path.
        """
        user = await session.get(User, user_id)
        if user is None:
            raise InvalidCredentialsError

        if not self.hasher.verify(user.password_hash, current_password):
            await self.audit.record(
                session,
                RecordInput(
                    actor=f"user:{user_id}",
                    actor_type="user",
                    action="auth.user.change_password",
                    resource_type="user",
                    resource_id=str(user_id),
                    outcome="failure",
                    reason="incorrect_current_password",
                ),
            )
            raise IncorrectCurrentPasswordError

        weakness = validate_password(new_password, self.settings)
        if weakness is not None:
            raise WeakPasswordError(weakness)

        user.password_hash = self.hasher.hash(new_password)
        user.failed_login_count = 0
        user.locked_until = None
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{user_id}",
                actor_type="user",
                action="auth.user.change_password",
                resource_type="user",
                resource_id=str(user_id),
                outcome="success",
            ),
        )
        return user

    async def set_email_notifications_enabled(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        enabled: bool,
        actor: str,
    ) -> User:
        """Toggle the user's per-account email-notification opt-in.

        Self-service: the Account page calls this with the user as the
        actor. Idempotent — flipping to the value the row already
        carries is a no-op (no audit row, no flush).
        """
        user = await session.get(User, user_id)
        if user is None:
            raise InvalidCredentialsError
        if user.email_notifications_enabled == enabled:
            return user
        user.email_notifications_enabled = enabled
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="user",
                action="auth.user.set_email_notifications",
                resource_type="user",
                resource_id=str(user_id),
                outcome="success",
                payload={"enabled": enabled},
            ),
        )
        return user

    async def set_summary_enrichment_enabled(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        enabled: bool,
        actor: str,
    ) -> User:
        """Toggle the user's per-account AI-summary enrichment opt-in.

        Self-service: the Account page calls this with the user as the actor.
        Idempotent — flipping to the value the row already carries is a no-op.
        """
        user = await session.get(User, user_id)
        if user is None:
            raise InvalidCredentialsError
        if user.summary_enrichment_enabled == enabled:
            return user
        user.summary_enrichment_enabled = enabled
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="user",
                action="auth.user.set_summary_enrichment",
                resource_type="user",
                resource_id=str(user_id),
                outcome="success",
                payload={"enabled": enabled},
            ),
        )
        return user

    async def set_theme_preference(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        theme: str,
        actor: str,
    ) -> User:
        """Persist the user's chosen dashboard theme.

        ``theme`` must be ``'light'`` or ``'dark'``; any other value
        raises ``ValueError`` (defence-in-depth — the API schema's
        ``Literal`` typing already gates the wire format). Idempotent
        in the same shape as :meth:`set_email_notifications_enabled`:
        a no-change call writes nothing to the audit chain.
        """
        if theme not in ("light", "dark"):
            raise ValueError(f"Unknown theme: {theme!r}; expected 'light' or 'dark'.")
        user = await session.get(User, user_id)
        if user is None:
            raise InvalidCredentialsError
        if user.theme_preference == theme:
            return user
        user.theme_preference = theme
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="user",
                action="auth.user.set_theme_preference",
                resource_type="user",
                resource_id=str(user_id),
                outcome="success",
                payload={"theme": theme},
            ),
        )
        return user

    async def set_regional_preferences(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        locale: str,
        timezone: str,
        actor: str,
    ) -> User:
        """Persist the user's locale + timezone preferences.

        Both fields accept the literal ``'auto'`` so a user can opt to
        inherit the browser's locale / timezone. Any other value is
        validated: locales against a permissive BCP-47 pattern,
        timezones against :class:`zoneinfo.ZoneInfo`. A no-change call
        is a no-op (no flush, no audit row), matching the shape of
        :meth:`set_theme_preference`.
        """
        locale = _normalise_locale(locale)
        timezone = _normalise_timezone(timezone)
        user = await session.get(User, user_id)
        if user is None:
            raise InvalidCredentialsError
        if user.locale_preference == locale and user.timezone_preference == timezone:
            return user
        user.locale_preference = locale
        user.timezone_preference = timezone
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="user",
                action="auth.user.set_regional_preferences",
                resource_type="user",
                resource_id=str(user_id),
                outcome="success",
                payload={"locale": locale, "timezone": timezone},
            ),
        )
        return user

    async def set_ui_preferences(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        sidebar_pinned: bool | None = None,
        sidebar_width: int | None = None,
        sidebar_groups_collapsed: dict[str, bool] | None = None,
        quick_access_filter: str | None = None,
        actor: str,
    ) -> User:
        """Merge a partial set of dashboard-shell preferences (home §9).

        Only the supplied (non-``None``) fields are merged onto the
        stored blob; the rest are left as they were. Each value is
        validated / clamped by :func:`_normalise_ui_preferences`, which
        also drops any unknown keys already in the blob so a stale
        client can't poison it. The JSON column is reassigned (not
        mutated in place) so SQLAlchemy detects the change. A no-change
        merge is a no-op — no flush, no audit row — matching the shape
        of :meth:`set_theme_preference`.
        """
        user = await session.get(User, user_id)
        if user is None:
            raise InvalidCredentialsError
        changes: dict[str, Any] = {}
        if sidebar_pinned is not None:
            changes["sidebar_pinned"] = sidebar_pinned
        if sidebar_width is not None:
            changes["sidebar_width"] = sidebar_width
        if sidebar_groups_collapsed is not None:
            changes["sidebar_groups_collapsed"] = sidebar_groups_collapsed
        if quick_access_filter is not None:
            changes["quick_access_filter"] = quick_access_filter
        merged = _normalise_ui_preferences(user.ui_preferences or {}, changes)
        if merged == (user.ui_preferences or {}):
            return user
        user.ui_preferences = merged
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="user",
                action="auth.user.set_ui_preferences",
                resource_type="user",
                resource_id=str(user_id),
                outcome="success",
                payload={"changed": sorted(changes)},
            ),
        )
        return user

    async def set_approver(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        is_approver: bool,
        actor: str,
        actor_user_id: int | None = None,
    ) -> User:
        """Toggle the user's approver eligibility.

        Independent of role — admins can flip this on any user.

        Turning the flag *off* triggers the same approver-list
        cascade as :meth:`deactivate`: the user is removed from
        every tool's ``approver_usernames``, and the call is refused
        with :class:`SoleApproverError` if they're the only approver
        on any tool. Turning the flag back *on* has no cascade.
        """
        from matofx.tools.approvers import remove_approver_from_tools  # noqa: PLC0415

        user = await session.get(User, user_id)
        if user is None:
            return user  # type: ignore[return-value]
        if user.is_approver == is_approver:
            return user

        if not is_approver:
            await remove_approver_from_tools(
                session,
                username=user.username,
                actor_user_id=(actor_user_id if actor_user_id is not None else user_id),
                audit=self.audit,
                audit_action="tools.tool.approver.cascade_remove_on_disable",
            )

        user.is_approver = is_approver
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="user",
                action="auth.user.set_approver",
                resource_type="user",
                resource_id=str(user_id),
                outcome="success",
                payload={"is_approver": is_approver},
            ),
        )
        return user

    async def get_by_username(self, session: AsyncSession, *, username: str) -> User | None:
        """Look up a user by username; returns ``None`` if no match."""
        return await _by_username(session, _canonicalise_username(username))

    async def get_by_id(self, session: AsyncSession, *, user_id: int) -> User | None:
        """Look up a user by id; returns ``None`` if no match."""
        return await session.get(User, user_id)

    async def list_all(self, session: AsyncSession) -> list[User]:
        """Return every user row, ordered by username.

        Privileged: callers must enforce ``users.manage`` themselves.
        Read-only — no audit row is recorded.
        """
        stmt = select(User).order_by(User.username)
        return list((await session.execute(stmt)).scalars())

    async def list_logon_events(
        self, session: AsyncSession, *, user_id: int
    ) -> list[UserLogonEvent]:
        """Return every grant/revoke event for the user, oldest first.

        Used by the user-edit page to render the full access history
        alongside the current ``is_active`` toggle. Append-only — there
        is no edit / delete path.
        """
        stmt = (
            select(UserLogonEvent)
            .where(UserLogonEvent.user_id == user_id)
            .order_by(UserLogonEvent.at, UserLogonEvent.id)
        )
        return list((await session.execute(stmt)).scalars())

    async def _record_failed_attempt(self, session: AsyncSession, user: User) -> None:
        from matofx.users.settings_overlay import effective_lockout_policy  # noqa: PLC0415

        user.failed_login_count += 1
        # DB-overlay aware: a super-user can tune the threshold + duration
        # from Admin → General Settings without restarting the API.
        policy = await effective_lockout_policy(session, self.settings)
        if user.failed_login_count >= policy.threshold:
            user.locked_until = _utcnow() + _dt.timedelta(minutes=policy.minutes)
        await session.flush()

    async def _audit_login_failure(
        self,
        session: AsyncSession,
        username: str,
        reason: str,
        *,
        user_id: int | None = None,
    ) -> None:
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{user_id}" if user_id is not None else "anonymous",
                actor_type="user" if user_id is not None else "anonymous",
                action="auth.login.failure",
                resource_type="user",
                resource_id=str(user_id) if user_id is not None else username,
                outcome="failure",
                reason=reason,
                payload={"username": username},
            ),
        )


@dataclass(frozen=True)
class SessionService:
    """Authentication-session lifecycle: create / validate / revoke."""

    settings: AuthSettings
    audit: AuditWriter

    async def create_session(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> CreatedSession:
        """Issue a new session for ``user_id``; returns the plaintext token."""
        token = new_session_token()
        token_hash = hash_session_token(token)
        now = _utcnow()
        row = AuthSession(
            token_hash=token_hash,
            user_id=user_id,
            ip_address=ip_address,
            user_agent=user_agent,
            created_at=now,
            last_activity_at=now,
        )
        session.add(row)
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{user_id}",
                actor_type="user",
                action="auth.session.create",
                resource_type="session",
                resource_id=str(row.id),
                outcome="success",
                source_ip=ip_address,
            ),
        )
        return CreatedSession(
            user_id=user_id,
            token=token,
            expires_at=now + _dt.timedelta(hours=self.settings.session_hard_timeout_hours),
            session_id=int(row.id),
        )

    async def validate(self, session: AsyncSession, *, token: str) -> AuthSession:
        """Confirm a session is live; refresh ``last_activity_at`` and return the row.

        Raises:
            SessionInvalidError: unknown / revoked tokens.
            SessionExpiredError: tokens past either timeout.
            AccountDisabledError: the underlying user has been
                deactivated — defence-in-depth, since
                ``UserService.deactivate`` already cascade-revokes
                sessions.
        """
        row = await self._fetch(session, token)
        if row is None or row.revoked_at is not None:
            raise SessionInvalidError

        # Defence-in-depth: even if a deactivation didn't get to cascade-revoke
        # this session (race window, DB write failure, etc.), block the
        # next validate.
        user = await session.get(User, row.user_id)
        if user is None or not user.is_active:
            raise AccountDisabledError

        now = _utcnow()
        # DB-overlay aware: a super-user edit on the Admin → General
        # Settings page takes effect on the next validate() without an
        # API restart. Falls back to the TOML baseline when no DB row
        # exists or the loader couldn't decode one.
        from matofx.users.settings_overlay import effective_session_policy  # noqa: PLC0415

        policy = await effective_session_policy(session, self.settings)
        idle_cutoff = now - _dt.timedelta(minutes=policy.idle_timeout_minutes)

        # A break-glass session was admitted *over* the licence cap so an
        # administrator is never locked out while the licence is broken
        # (ADR-0019 §C13). Admission is deliberately unlimited in number
        # — capping it would lock out the very person it exists to admit
        # — so it is bounded in *time* instead: emergency access that
        # lasts a fortnight is not emergency access, it is an extra seat.
        hard_hours = policy.hard_timeout_hours
        if getattr(row, "breakglass", False):
            hard_hours = min(hard_hours, BREAKGLASS_MAX_HOURS)
        hard_cutoff = now - _dt.timedelta(hours=hard_hours)

        last_activity = _as_aware(row.last_activity_at)
        if last_activity < idle_cutoff or _as_aware(row.created_at) < hard_cutoff:
            raise SessionExpiredError

        # Coalesce the activity write: only touch the row (and flush) once the
        # marker has actually aged past the window. Skipping the otherwise
        # per-request no-op write is what keeps concurrent reads from
        # serialising into a SQLite write storm — see _ACTIVITY_WRITE_COALESCE.
        if now - last_activity >= _ACTIVITY_WRITE_COALESCE:
            row.last_activity_at = now
            await session.flush()
        return row

    async def revoke(self, session: AsyncSession, *, token: str, actor: str) -> None:
        """Mark a session revoked. Idempotent: revoking twice is a no-op."""
        row = await self._fetch(session, token)
        if row is None or row.revoked_at is not None:
            return
        row.revoked_at = _utcnow()
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="user",
                action="auth.session.revoke",
                resource_type="session",
                resource_id=str(row.id),
                outcome="success",
            ),
        )

    async def peek(self, session: AsyncSession, *, token: str) -> AuthSession | None:
        """The session row for ``token`` without touching or validating it.

        Used by the logout path, which needs the row's id to record the
        licence-slot release *before* revoking it (ADR-0019 slice 4).
        Deliberately not a validate(): a session on the brink of its
        idle timeout should still log out cleanly.
        """
        return await self._fetch(session, token)

    async def _fetch(self, session: AsyncSession, token: str) -> AuthSession | None:
        token_hash = hash_session_token(token)
        stmt = select(AuthSession).where(AuthSession.token_hash == token_hash)
        return (await session.execute(stmt)).scalar_one_or_none()


def make_user_service(
    settings: AuthSettings,
    audit: AuditWriter,
    hasher: PasswordHasher | None = None,
) -> UserService:
    """Wire up a :class:`UserService` from validated settings."""
    return UserService(
        settings=settings,
        hasher=hasher if hasher is not None else make_password_hasher(settings),
        audit=audit,
    )


def make_session_service(settings: AuthSettings, audit: AuditWriter) -> SessionService:
    """Wire up a :class:`SessionService` from validated settings."""
    return SessionService(settings=settings, audit=audit)


def _canonicalise_username(username: str) -> str:
    """Strip + lower-case the username.

    Stored and looked up in canonical form so ``"Alice"`` and
    ``"alice"`` are the same account. Display names are carried
    separately on ``User.full_name``.
    """
    return username.strip().lower()


async def _by_username(session: AsyncSession, username: str) -> User | None:
    stmt = select(User).where(User.username == username)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _resolve_role_by_name(session: AsyncSession, name: str) -> Role | None:
    stmt = select(Role).where(Role.name == name)
    return (await session.execute(stmt)).scalar_one_or_none()


_EMAIL_MAX_LENGTH = 254
"""RFC 5321 limit on the total length of an email address."""

_EMAIL_LOCAL_MAX_LENGTH = 64
"""RFC 5321 limit on the local part of an email address."""

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
"""Built-in email syntax check.

The previous design pulled in the ``email-validator`` package, which
is licensed CC0 (public domain). Strict procurement teams sometimes
reject anything outside the OSI-approved licence list, so we trade
the third-party dependency for a small in-house validator.

Our use is limited to syntax + length checks. We don't need IDN, MX
verification, or RFC 5322 round-tripping at this layer; the SMTP
adapter (when it lands) handles deliverability concerns.
"""


_BCP47_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$")
"""Permissive BCP-47 tag check.

Real BCP-47 is much richer (extlangs, variants, private-use); we only
need enough to keep junk out of the column. ``'en'``, ``'en-GB'``,
``'zh-Hans-CN'`` all pass; ``'definitely; not a tag'`` fails. Production
locales the Account page surfaces are pre-vetted, so this regex is
just defence in depth for callers hitting the API directly.
"""


def _normalise_locale(locale: str) -> str:
    """Strip + validate a BCP-47 locale tag (or the literal ``'auto'``)."""
    text = locale.strip()
    if text.lower() == "auto":
        return "auto"
    if not _BCP47_RE.match(text):
        raise ValueError(
            f"Unknown locale: {locale!r}; expected 'auto' or a BCP-47 tag like 'en-GB'."
        )
    return text


def _normalise_timezone(timezone: str) -> str:
    """Strip + validate an IANA timezone id (or the literal ``'auto'``)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # noqa: PLC0415

    text = timezone.strip()
    if text.lower() == "auto":
        return "auto"
    try:
        ZoneInfo(text)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"Unknown timezone: {timezone!r}; expected 'auto' or an IANA name like 'Europe/London'."
        ) from exc
    return text


_UI_PREF_SIDEBAR_MIN_WIDTH = 188
_UI_PREF_SIDEBAR_MAX_WIDTH = 460
_UI_PREF_QUICK_ACCESS_FILTERS = frozenset({"tool", "workflow", "all"})


def _normalise_ui_preferences(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Merge ``changes`` onto ``current`` and return a clean blob.

    Only the four known shell-preference keys survive — any unknown
    key already in ``current`` is dropped, so a forward-compatible
    client that wrote an experimental key can't leave junk in the
    column once it rolls back. Each value is coerced to its expected
    type and clamped:

    - ``sidebar_pinned`` → bool (anything truthy).
    - ``sidebar_width`` → int clamped to 188–460 (the design's resize
      range); a non-numeric value drops the key.
    - ``sidebar_groups_collapsed`` → ``{str: bool}`` (non-str keys /
      non-bool values filtered out).
    - ``quick_access_filter`` → one of ``tool`` / ``workflow`` / ``all``;
      an off-list value drops the key.

    The result is a fresh dict (never the input objects) so the caller
    can reassign it onto the JSON column for change detection.
    """
    merged: dict[str, Any] = dict(current)
    merged.update(changes)
    out: dict[str, Any] = {}

    if "sidebar_pinned" in merged:
        out["sidebar_pinned"] = bool(merged["sidebar_pinned"])

    if "sidebar_width" in merged:
        try:
            width = int(merged["sidebar_width"])
        except (TypeError, ValueError):
            width = None
        if width is not None:
            out["sidebar_width"] = max(
                _UI_PREF_SIDEBAR_MIN_WIDTH, min(_UI_PREF_SIDEBAR_MAX_WIDTH, width)
            )

    if "sidebar_groups_collapsed" in merged:
        raw = merged["sidebar_groups_collapsed"]
        if isinstance(raw, dict):
            out["sidebar_groups_collapsed"] = {
                str(k): bool(v) for k, v in raw.items() if isinstance(k, str)
            }

    if "quick_access_filter" in merged:
        value = str(merged["quick_access_filter"])
        if value in _UI_PREF_QUICK_ACCESS_FILTERS:
            out["quick_access_filter"] = value

    return out


def _normalise_email(email: str) -> str:
    """Validate + canonicalise an email address; raises on failure.

    Checks:

    - Non-empty after stripping whitespace.
    - Total length at most 254 (RFC 5321 limit).
    - Local part length at most 64 (RFC 5321 limit).
    - Matches a conservative ASCII pattern (IDN handling lands in v1.x
      alongside the full i18n catalog).

    The whole address is lower-cased on output. RFC 5321 says the
    local part is technically case-sensitive, but every major email
    provider (Gmail, Outlook, Apple, etc.) treats it case-insensitively
    in practice, so lower-casing stops a customer accidentally
    creating two accounts with addresses that differ only in case
    (``Alice@x`` vs ``alice@x``).
    """
    email = email.strip()
    if not email:
        raise InvalidEmailError("an email address is required")
    if len(email) > _EMAIL_MAX_LENGTH:
        raise InvalidEmailError("the address is too long")
    if not _EMAIL_RE.match(email):
        raise InvalidEmailError("the format isn't recognised")
    local, _, _domain = email.partition("@")
    if len(local) > _EMAIL_LOCAL_MAX_LENGTH:
        raise InvalidEmailError("the part before the @ is too long")
    return email.lower()
