"""When the next compliance report is due, and whether it is overdue.

**One answer, two surfaces.** The amber banner at the top of every page
and the "next due" line on Admin → Licence & compliance must never
disagree about whether a report is owed — a banner that says *due* over
a page that says *not yet* is worse than no reminder at all. So the
question is answered here, once, and both surfaces read the result.

**The reminder never blocks anything.** It is a banner and nothing else:
no read-only mode, no refused writes, no degraded features. A customer
who never generates a report keeps working exactly as before, because
withholding a report is a conversation to have with them, not something
the software should punish. That is a deliberate contrast with the
licence and integrity paths, which sit in the same banner strip and *do*
mean the site is degraded.

**Cadence comes from the licence** (:attr:`~matofx.licence.models.
LicenceManifest.compliance_report_months`), not from site settings. How
often a customer reports is a term of the agreement; leaving it in the
customer's own configuration would let an administrator quietly extend
the interval that exists to prompt them. ``0`` turns it off.

The clock runs from the last report *generated*, because that is the
only fact this install can observe — whether it was ever sent is not
knowable here, and guessing would produce a reminder nobody can act on.
Before the first report it runs from the moment the install adopted its
identity, so a site that has never reported eventually says so.
"""

from __future__ import annotations

import calendar
import datetime as _dt
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.compliance.orm import ComplianceReport, InstallIdentity


def add_months(start: _dt.datetime, months: int) -> _dt.datetime:
    """``start`` plus ``months`` calendar months, clamped to the month end.

    Calendar months, not 30-day blocks: a quarterly cadence anchored on
    the 31st should land on the 30th of a 30-day month, not drift a day
    earlier every quarter.
    """
    index = start.month - 1 + months
    year = start.year + index // 12
    month = index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


@dataclass(frozen=True)
class ReportingStatus:
    """Where this install stands against its reporting cadence."""

    cadence_months: int
    """0 when reminders are switched off for this customer."""

    last_generated_at: _dt.datetime | None
    """None when no report has ever been generated here."""

    due_at: _dt.datetime | None
    """When the next report falls due. None when reminders are off, or
    when the install has no identity yet and so no clock to run."""

    due: bool
    """True once ``due_at`` has passed. The banner shows on this alone."""

    days_overdue: int
    """Whole days past ``due_at``; 0 when not yet due. Drives the wording,
    so "due today" doesn't read as "overdue by 0 days"."""


async def reporting_status(
    session: AsyncSession,
    *,
    cadence_months: int,
    now: _dt.datetime,
) -> ReportingStatus:
    """Answer the reporting question for this install.

    Returns a not-due status rather than raising when there is nothing to
    measure against — reminders switched off, or an install that has not
    adopted an identity yet. A chrome banner must never be the thing that
    stops a page rendering.
    """
    if cadence_months <= 0:
        last = await _last_generated_at(session)
        return ReportingStatus(
            cadence_months=0,
            last_generated_at=last,
            due_at=None,
            due=False,
            days_overdue=0,
        )

    last = await _last_generated_at(session)
    anchor = last if last is not None else await _adopted_at(session)
    if anchor is None:
        # No identity and no report: the install has not been
        # commissioned, so there is no clock to run yet.
        return ReportingStatus(
            cadence_months=cadence_months,
            last_generated_at=None,
            due_at=None,
            due=False,
            days_overdue=0,
        )

    due_at = add_months(anchor, cadence_months)
    overdue = max(0, (now - due_at).days) if now >= due_at else 0
    return ReportingStatus(
        cadence_months=cadence_months,
        last_generated_at=last,
        due_at=due_at,
        due=now >= due_at,
        days_overdue=overdue,
    )


def banner_text(status: ReportingStatus) -> str | None:
    """The amber banner's wording, or None when there is nothing to say.

    Written for someone who has never done this before: what is being
    asked, that it is safe, and that nothing breaks if they ignore it.
    An administrator who is nervous about pressing the button will
    instead not produce the report their supplier asked for.
    """
    if not status.due:
        return None
    if status.last_generated_at is None:
        return (
            "A compliance report for your software supplier hasn't been produced yet. "
            "Generating one takes a few seconds, sends nothing anywhere, and you can "
            "read it before deciding to share it. Nothing stops working until you do."
        )
    when = f"{status.last_generated_at:%d %B %Y}"
    if status.days_overdue == 0:
        timing = "is due today"
    elif status.days_overdue == 1:
        timing = "was due yesterday"
    else:
        timing = f"was due {status.days_overdue} days ago"
    return (
        f"A compliance report {timing} — the last one was generated on {when}. "
        "Generating one takes a few seconds and sends nothing anywhere. "
        "Nothing stops working until you do."
    )


async def _last_generated_at(session: AsyncSession) -> _dt.datetime | None:
    """Timestamp of the most recent report, by counter.

    Ordered on ``counter`` rather than ``generated_at`` because the
    counter is the monotonic thing — a clock that moved backwards must
    not be able to make an older report look like the latest one.
    """
    stmt = (
        sa.select(ComplianceReport.generated_at)
        .order_by(ComplianceReport.counter.desc())
        .limit(1)
    )
    value = (await session.execute(stmt)).scalar_one_or_none()
    return _aware(value) if value is not None else None


async def _adopted_at(session: AsyncSession) -> _dt.datetime | None:
    stmt = sa.select(InstallIdentity.adopted_at).limit(1)
    value = (await session.execute(stmt)).scalar_one_or_none()
    return _aware(value) if value is not None else None


def _aware(value: _dt.datetime) -> _dt.datetime:
    """SQLite hands back naive datetimes; treat them as the UTC they are."""
    return value if value.tzinfo else value.replace(tzinfo=_dt.UTC)
