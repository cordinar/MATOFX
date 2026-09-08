"""Compute the report's usage figures for both licence types (ADR-0019).

Everything here is measured from data the install already keeps —
``auth_session`` rows and the audit chain - which is what lets slice 1
produce a substantive report before any enforcement code exists. The
figures that depend on enforcement (grants, denials, break-glass) count
chain events and legitimately read zero until slices 4-5 start emitting
them.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING

import sqlalchemy as sa

from matofx.audit.orm import AuditLog
from matofx.compliance.schema import CslUsage, FalUsage
from matofx.users.orm import AuthSession, User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def event_counts(
    session: AsyncSession,
    *,
    actions: tuple[str, ...],
    period_from: _dt.datetime,
    period_to: _dt.datetime,
) -> dict[str, int]:
    """Count audit records per action within the period.

    Every action in ``actions`` appears in the result, zero-filled, so
    the report's shape doesn't shift as later slices start emitting new
    events (:mod:`matofx.compliance.schema`).
    """
    stmt = (
        sa.select(AuditLog.action, sa.func.count(AuditLog.id))
        .where(
            AuditLog.action.in_(actions),
            AuditLog.timestamp >= period_from,
            AuditLog.timestamp <= period_to,
        )
        .group_by(AuditLog.action)
    )
    observed = {action: int(count) for action, count in (await session.execute(stmt)).all()}
    return {action: observed.get(action, 0) for action in actions}


async def csl_usage(
    session: AsyncSession,
    *,
    period_from: _dt.datetime,
    period_to: _dt.datetime,
    counts: dict[str, int],
) -> CslUsage:
    """Concurrent-session figures — the CSL-authoritative block.

    Peak concurrency comes from overlapping session intervals, clamped
    to the period. A session's end is its revocation time when it was
    revoked, else its last activity: that is the same convention the
    Usage Statistics page uses, so the two surfaces can't disagree.
    """
    intervals = await _session_intervals(session, period_from=period_from, period_to=period_to)
    started = await _sessions_started(session, period_from=period_from, period_to=period_to)
    return CslUsage(
        peak_concurrent_sessions=_peak_concurrency(intervals),
        sessions_started=started,
        slots_granted=counts.get("licence.slot.granted", 0),
        slots_denied=counts.get("licence.slot.denied", 0),
        breakglass_admissions=counts.get("licence.slot.breakglass", 0),
        peak_by_day=_peak_by_day(intervals, period_from, period_to),
    )


async def fal_usage(
    session: AsyncSession,
    *,
    counts: dict[str, int],
    enforced: bool = False,
) -> FalUsage:
    """Floating-assignment figures — the FAL-authoritative block.

    The *number* is the same either way: under FAL, a user who can sign
    in is exactly a user holding an assignment, so the count of enabled
    accounts is the assignment count. What changes at slice 5 is whether
    it is **enforced** against a cap.

    ``source`` records which it is, so Funis never compares a
    pre-enforcement proxy against a metered count without knowing:

    - ``enabled_accounts`` — counted, not capped (no FAL licence in
      force, or an install predating slice 5).
    - ``fal_assignments`` — metered: granting past the cap is refused.
    """
    enabled = int(
        (
            await session.execute(sa.select(sa.func.count(User.id)).where(User.is_active.is_(True)))
        ).scalar_one()
    )
    registered = int((await session.execute(sa.select(sa.func.count(User.id)))).scalar_one())
    return FalUsage(
        assignments_current=enabled,
        assignments_granted=counts.get("licence.assignment.granted", 0),
        assignments_revoked=counts.get("licence.assignment.revoked", 0),
        assignments_denied=counts.get("licence.assignment.denied", 0),
        registered_users=registered,
        source="fal_assignments" if enforced else "enabled_accounts",
    )


async def _session_intervals(
    session: AsyncSession,
    *,
    period_from: _dt.datetime,
    period_to: _dt.datetime,
) -> list[tuple[_dt.datetime, _dt.datetime]]:
    """``(start, end)`` per auth session, clamped to the period."""
    stmt = sa.select(
        AuthSession.created_at,
        AuthSession.last_activity_at,
        AuthSession.revoked_at,
    ).where(AuthSession.created_at <= period_to)
    intervals: list[tuple[_dt.datetime, _dt.datetime]] = []
    for created_at, last_activity_at, revoked_at in (await session.execute(stmt)).all():
        start = _aware(created_at)
        end = _aware(revoked_at or last_activity_at or created_at)
        end = max(end, start)
        if end < period_from or start > period_to:
            continue
        intervals.append((max(start, period_from), min(end, period_to)))
    return intervals


async def _sessions_started(
    session: AsyncSession,
    *,
    period_from: _dt.datetime,
    period_to: _dt.datetime,
) -> int:
    stmt = sa.select(sa.func.count(AuthSession.id)).where(
        AuthSession.created_at >= period_from,
        AuthSession.created_at <= period_to,
    )
    return int((await session.execute(stmt)).scalar_one())


def _peak_concurrency(intervals: list[tuple[_dt.datetime, _dt.datetime]]) -> int:
    """Maximum number of simultaneously-open sessions (sweep line).

    Ends are processed before starts at equal timestamps: a session that
    ends exactly as another begins is a handover, not concurrency.
    """
    if not intervals:
        return 0
    events: list[tuple[_dt.datetime, int]] = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort(key=lambda e: (e[0], e[1]))
    peak = current = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def _peak_by_day(
    intervals: list[tuple[_dt.datetime, _dt.datetime]],
    period_from: _dt.datetime,
    period_to: _dt.datetime,
) -> dict[str, int]:
    """Per-UTC-day peak concurrency across the period.

    Gives Funis a shape rather than a single number — a steady 9/10 and
    a one-afternoon spike to 14 are very different conversations.
    """
    peaks: dict[str, int] = {}
    day = period_from.astimezone(_dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    last = period_to.astimezone(_dt.UTC)
    while day <= last:
        day_end = day + _dt.timedelta(days=1)
        clipped = [
            (max(start, day), min(end, day_end))
            for start, end in intervals
            if start < day_end and end >= day
        ]
        peaks[day.date().isoformat()] = _peak_concurrency(clipped)
        day = day_end
    return peaks


def _aware(value: _dt.datetime) -> _dt.datetime:
    """Treat naive timestamps (SQLite round-trip) as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=_dt.UTC)
