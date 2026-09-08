"""Background sweeper that auto-disables long-inactive user accounts.

Threshold + cadence + grace-warning window are admin-tunable via the
General Settings catalog (`auto_disable_inactive_days`,
`auto_disable_sweep_interval_hours`, `auto_disable_grace_warning_days`).
The sweeper is wired into :class:`matofx.runner.supervisor.RunnerSupervisor`
as a sibling of the pickup / heartbeat / cancel / settings loops; it
runs once on daemon startup (so a long downtime doesn't let stale
accounts linger past the threshold) and then every
``auto_disable_sweep_interval_hours`` afterwards.

## Eligibility — what counts as "inactive"

A user qualifies for auto-disable when *all* of the following hold:

1. ``user.is_active = true`` (already-disabled rows are skipped).
2. The user is not a super-user. Super-users are the install's
   break-glass admins; auto-disabling the last active super-user
   would brick the install, and the rule explicitly preserves all
   of them regardless of activity. No per-user exemption flag is
   provided — customers with token-only service accounts either
   make them super-users, or accept that an admin needs to
   re-activate them periodically.
3. ``COALESCE(user.last_login_at, user.created_at)`` is older than
   ``now() - threshold_days``. The ``created_at`` fallback handles
   brand-new accounts whose holder has never logged in — measured
   from creation rather than the never-set login timestamp.

## Race protection

The eligible set is computed once per sweep, but each disable runs
in its own transaction with a WHERE clause that **re-checks** the
threshold. A user who logs in between the eligibility scan and the
per-row update has their ``last_login_at`` bumped forward; the
WHERE clause now matches zero rows and the disable is silently
skipped (an audit event records the skip so ops can confirm the
guard fired). This avoids a separate locking primitive while still
defeating the live-login race.

## Grace-warning email

When ``auto_disable_grace_warning_days`` is non-zero, users in the
band ``[threshold - grace_days, threshold)`` of inactivity get a
warning email on the next sweep — once per inactivity stretch
(deduped via ``user.last_grace_warning_at``). The warning column
is cleared on the next successful login so a re-stale user gets a
fresh warning later. Failures of the email send don't block the
sweep; the next sweep retries because ``last_grace_warning_at``
only gets set on a successful send.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.audit.schema import RecordInput
from matofx.config.general_service import load_general_settings
from matofx.users.orm import User
from matofx.users.role_orm import Role
from matofx.users.role_service import SUPER_USER_NAME

if TYPE_CHECKING:
    from matofx.audit import AuditWriter
    from matofx.db.session import SessionFactory
    from matofx.notify.email import Notifier

_log = logging.getLogger(__name__)


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


def _as_aware(value: _dt.datetime | None) -> _dt.datetime | None:
    """Coerce a stored datetime to UTC-aware.

    SQLAlchemy's ``DateTime(timezone=True)`` round-trips as a naive
    datetime on SQLite (no zoneinfo support in the storage layer);
    Postgres returns it aware. The sweeper compares timestamps with
    ``_dt.datetime.now(tz=_dt.UTC)`` so we lift any naive value to
    UTC-aware here to keep arithmetic + comparisons consistent
    across both engines. The column is documented as UTC, so the
    coercion is safe.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=_dt.UTC)
    return value


@dataclass(frozen=True)
class _PreviewUserRow:
    """One row in the preview-list response.

    Internal shape; the API layer maps it onto
    :class:`matofx.api.schemas.AdminAutoDisablePreviewRow`. Kept
    separate so the sweeper module has no API-layer dependency.
    """

    user_id: int
    username: str
    full_name: str
    last_login_at: _dt.datetime | None
    created_at: _dt.datetime
    days_inactive: int
    role: str


@dataclass(frozen=True)
class SweepResult:
    """Summary of one sweep run, used by the daemon log + tests.

    ``disabled_user_ids`` lists every account the sweep deactivated.
    ``raced_user_ids`` lists accounts the WHERE-clause race guard
    saved (they logged in mid-sweep). ``grace_warned_user_ids``
    lists accounts that received a warning email this run.
    """

    threshold_days: int
    grace_days: int
    eligible: int
    disabled_user_ids: tuple[int, ...]
    raced_user_ids: tuple[int, ...]
    grace_warned_user_ids: tuple[int, ...]


# --- public surface ----------------------------------------------------


async def preview_inactive_users(
    session: AsyncSession,
    *,
    threshold_days: int,
    limit: int = 100,
    as_of: _dt.datetime | None = None,
) -> tuple[list[_PreviewUserRow], int]:
    """Return up to ``limit`` users a sweep at ``threshold_days`` would disable, plus the total count.

    The list is ordered by "most inactive first" so the operator
    sees the most-overdue users without paginating. ``as_of`` is
    injectable for tests; in production callers pass ``None`` and
    the function reads ``datetime.now(UTC)``.
    """
    now = as_of or _utcnow()
    cutoff = now - _dt.timedelta(days=threshold_days)

    rows = await _select_eligible(session, cutoff)
    # Most inactive first (oldest effective activity at the top).
    # The select returns naive timestamps on SQLite — sort key
    # uses the same shape so comparisons stay consistent.
    rows.sort(key=lambda r: r[1])  # r[1] is effective_last_activity
    total = len(rows)
    sliced = rows[:limit]

    out: list[_PreviewUserRow] = []
    for user, effective, role_name in sliced:
        eff_aware = _as_aware(effective)
        assert eff_aware is not None  # narrowing — eligibility scan filters NULL
        out.append(
            _PreviewUserRow(
                user_id=user.id,
                username=user.username,
                full_name=user.full_name,
                last_login_at=_as_aware(user.last_login_at),
                created_at=_as_aware(user.created_at) or now,
                days_inactive=max((now - eff_aware).days, 0),
                role=role_name,
            )
        )
    return out, total


async def sweep_once(
    session: AsyncSession,
    *,
    notifier: Notifier | None,
    audit: AuditWriter,
    actor: str = "system:inactive_sweeper",
    as_of: _dt.datetime | None = None,
) -> SweepResult:
    """Run one inactivity sweep against ``session``'s database.

    Reads the live threshold + grace-window from General Settings,
    eligibility-scans the user table, and disables / warns users in
    a single transaction. Returns a :class:`SweepResult` summary;
    raises only on programmer errors (the per-user transaction
    catches its own DB errors and continues with the next row).

    Pass ``as_of`` in tests to pin the reference time; production
    callers leave it ``None``.
    """
    settings = await load_general_settings(session)
    threshold = int(settings.get("auto_disable_inactive_days") or 0)
    if threshold <= 0:
        # Feature off — short-circuit. Still return an empty result
        # so the supervisor can log the sweep ran.
        return SweepResult(
            threshold_days=0,
            grace_days=0,
            eligible=0,
            disabled_user_ids=(),
            raced_user_ids=(),
            grace_warned_user_ids=(),
        )
    grace_days = int(settings.get("auto_disable_grace_warning_days") or 0)
    now = as_of or _utcnow()
    cutoff = now - _dt.timedelta(days=threshold)

    eligible_rows = await _select_eligible(session, cutoff)
    eligible_count = len(eligible_rows)

    disabled: list[int] = []
    raced: list[int] = []
    for user, _, _ in eligible_rows:
        outcome = await _attempt_disable(
            session,
            user_id=user.id,
            cutoff=cutoff,
            actor=actor,
            audit=audit,
            now=now,
        )
        if outcome == "disabled":
            disabled.append(user.id)
        elif outcome == "raced":
            raced.append(user.id)

    # Grace warnings — separate scan against the WARNING band, not
    # the disabled set. A user in the band has effective_last_activity
    # within [now - threshold + grace_days, cutoff), so:
    #   warn_cutoff_far = cutoff           (oldest end of the band)
    #   warn_cutoff_near = now - (threshold - grace_days)
    grace_warned: list[int] = []
    if grace_days > 0:
        warn_far = cutoff
        warn_near = now - _dt.timedelta(days=max(threshold - grace_days, 0))
        if warn_near > warn_far:
            warned = await _send_grace_warnings(
                session,
                warn_far=warn_far,
                warn_near=warn_near,
                notifier=notifier,
                audit=audit,
                actor=actor,
                now=now,
                threshold_days=threshold,
            )
            grace_warned.extend(warned)

    return SweepResult(
        threshold_days=threshold,
        grace_days=grace_days,
        eligible=eligible_count,
        disabled_user_ids=tuple(disabled),
        raced_user_ids=tuple(raced),
        grace_warned_user_ids=tuple(grace_warned),
    )


async def run_inactive_sweeper(
    factory: SessionFactory,
    *,
    notifier: Notifier | None,
    audit: AuditWriter,
    stopping: asyncio.Event,
) -> None:
    """Long-running loop: sweep on startup, then every ``auto_disable_sweep_interval_hours``.

    Hosted by :class:`matofx.runner.supervisor.RunnerSupervisor`.
    The interval is re-read from General Settings between
    iterations so a change in
    ``auto_disable_sweep_interval_hours`` takes effect on the next
    loop without a daemon restart. The threshold + grace window
    are similarly re-read inside :func:`sweep_once`.

    Body errors are logged and swallowed so the loop never dies
    permanently — same posture every other supervisor loop uses.
    """
    from matofx.db.session import session_scope  # noqa: PLC0415 — avoid import-cycle at module load

    # Sweep-on-startup. Catches the "daemon was down for days,
    # threshold has been exceeded by several users since" case the
    # design called out.
    while not stopping.is_set():
        interval_h = 24  # safe default if the settings read fails
        try:
            async with session_scope(factory) as session:
                result = await sweep_once(session, notifier=notifier, audit=audit)
                _log.info(
                    "inactive_sweeper ran: threshold=%d days, eligible=%d, "
                    "disabled=%d, raced=%d, grace_warned=%d",
                    result.threshold_days,
                    result.eligible,
                    len(result.disabled_user_ids),
                    len(result.raced_user_ids),
                    len(result.grace_warned_user_ids),
                )
                # Re-read the interval in the SAME session we already
                # have open — cheap, and a delegated-admin change
                # between sweeps kicks in immediately.
                settings_snapshot = await load_general_settings(session)
                interval_h = max(
                    int(settings_snapshot.get("auto_disable_sweep_interval_hours") or 24),
                    1,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("inactive_sweeper sweep failed; continuing")
        try:
            await asyncio.wait_for(stopping.wait(), timeout=interval_h * 3600)
        except TimeoutError:
            continue


# --- internals ---------------------------------------------------------


async def _select_eligible(
    session: AsyncSession,
    cutoff: _dt.datetime,
) -> list[tuple[User, _dt.datetime, str]]:
    """Find every active non-super-user whose effective activity < cutoff.

    Returns tuples of ``(user, effective_last_activity, role_name)``
    so callers can compute days-inactive + show the role on the
    preview UI without a second query per row.
    """
    # ``COALESCE(last_login_at, created_at)`` is the effective
    # activity timestamp. SQLAlchemy's ``func.coalesce`` works on
    # both engines; on SQLite without zoneinfo we don't bother
    # with explicit timezones — the columns are stored UTC.
    effective = sa.func.coalesce(User.last_login_at, User.created_at)
    stmt = (
        sa.select(User, effective.label("effective"), Role.name.label("role_name"))
        .join(Role, Role.id == User.role_id)
        .where(
            User.is_active.is_(True),
            Role.name != SUPER_USER_NAME,
            effective < cutoff,
        )
    )
    result = await session.execute(stmt)
    return [(row[0], row[1], str(row[2])) for row in result.all()]


async def _attempt_disable(
    session: AsyncSession,
    *,
    user_id: int,
    cutoff: _dt.datetime,
    actor: str,
    audit: AuditWriter,
    now: _dt.datetime,
) -> str:
    """Disable one user inside a savepoint with a race-recheck.

    Returns ``"disabled"`` on success, ``"raced"`` when the
    WHERE-clause guard fired (the user logged in mid-sweep), or
    ``"skipped"`` if the user is not eligible (e.g. another sweep
    instance just disabled them). The savepoint isolates per-row
    failures: one row that fails its update never aborts the
    surrounding sweep transaction.
    """
    # Use a nested transaction so a per-row IntegrityError can be
    # rolled back without losing earlier disables.
    sp = await session.begin_nested()
    try:
        # Re-check the threshold inside the row update. A user who
        # logged in between the eligibility scan and this update has
        # last_login_at >= cutoff and matches zero rows.
        effective = sa.func.coalesce(User.last_login_at, User.created_at)
        result = await session.execute(
            sa.update(User)
            .where(
                User.id == user_id,
                User.is_active.is_(True),
                effective < cutoff,
            )
            .values(
                is_active=False,
                logon_revoked_at=now,
            )
        )
        if result.rowcount == 0:
            # Either the user just logged in (race) or another
            # sweeper instance got there first.
            await sp.rollback()
            await audit.record(
                session,
                RecordInput(
                    actor=actor,
                    actor_type="system",
                    action="auth.user.auto_disable_skipped",
                    resource_type="user",
                    resource_id=str(user_id),
                    outcome="skipped",
                    reason=(
                        "User logged in mid-sweep or another sweep instance "
                        "disabled them first."
                    ),
                ),
            )
            return "raced"

        # Append a ``revoked`` event for the logon-history table so the
        # auto-disable shows up alongside admin-initiated revokes.
        await session.execute(
            sa.text(
                """
                INSERT INTO user_logon_event (user_id, event, at, actor_user_id, reason)
                VALUES (:user_id, 'revoked', :at, NULL, :reason)
                """
            ),
            {
                "user_id": user_id,
                "at": now,
                "reason": "Auto-disabled by inactivity sweep.",
            },
        )
        # Cascade: revoke every live auth_session so a stale token
        # can't be used after the disable lands.
        await session.execute(
            sa.text(
                """
                UPDATE auth_session
                   SET revoked_at = :at
                 WHERE user_id = :user_id
                   AND revoked_at IS NULL
                """
            ),
            {"user_id": user_id, "at": now},
        )
        await sp.commit()
        await audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="system",
                action="auth.user.auto_disabled",
                resource_type="user",
                resource_id=str(user_id),
                outcome="success",
                reason="Inactivity threshold exceeded.",
            ),
        )
        return "disabled"
    except Exception:
        await sp.rollback()
        _log.exception("Failed to auto-disable user id=%s; continuing sweep", user_id)
        return "skipped"


async def _send_grace_warnings(
    session: AsyncSession,
    *,
    warn_far: _dt.datetime,
    warn_near: _dt.datetime,
    notifier: Notifier | None,
    audit: AuditWriter,
    actor: str,
    now: _dt.datetime,
    threshold_days: int,
) -> list[int]:
    """Email users whose inactivity falls in the warning band.

    Deduped per inactivity stretch via ``user.last_grace_warning_at``
    — once it's set, the user is skipped until a fresh login clears
    it. Returns the list of user ids the warning was sent to so the
    caller can include them in the :class:`SweepResult`.
    """
    if notifier is None:
        return []

    effective = sa.func.coalesce(User.last_login_at, User.created_at)
    stmt = (
        sa.select(User)
        .join(Role, Role.id == User.role_id)
        .where(
            User.is_active.is_(True),
            Role.name != SUPER_USER_NAME,
            effective >= warn_far,
            effective < warn_near,
            User.last_grace_warning_at.is_(None),
        )
    )
    rows = (await session.execute(stmt)).scalars().all()

    warned: list[int] = []
    notify_method = getattr(notifier, "notify_inactivity_grace_warning", None)
    if notify_method is None:
        # The injected notifier predates the grace-warning hook
        # (e.g. an older custom implementation). Audit-skip and
        # carry on rather than crash the sweep.
        return []

    for user in rows:
        # Compute days remaining at the user's effective activity.
        eff = _as_aware(user.last_login_at) or _as_aware(user.created_at)
        assert eff is not None  # created_at is NOT NULL on the User table
        days_inactive = max((now - eff).days, 0)
        days_remaining = max(threshold_days - days_inactive, 0)
        try:
            await notify_method(
                session,
                user=user,
                days_remaining=days_remaining,
                threshold_days=threshold_days,
            )
        except Exception:
            _log.exception(
                "grace-warning email failed for user id=%s; will retry next sweep",
                user.id,
            )
            continue

        user.last_grace_warning_at = now
        await session.flush()
        await audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type="system",
                action="auth.user.grace_warning_sent",
                resource_type="user",
                resource_id=str(user.id),
                outcome="success",
                payload={
                    "days_remaining": days_remaining,
                    "threshold_days": threshold_days,
                },
            ),
        )
        warned.append(user.id)

    return warned
