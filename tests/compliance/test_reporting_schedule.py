"""When a compliance report falls due, and what the reminder says.

The rule the customer asked for: anyone who can generate a report is
reminded to, on a cadence, and the reminder clears for *everyone* as
soon as one is generated. Not generating one must never stop anybody
working.

The clearing-for-everyone part is why this is derived from the database
on each render rather than tracked as a per-user dismissal: there is no
"dismissed" state that could get stuck, and two administrators cannot
see different answers.

Seeded through the ORM, never raw SQL — SQLite compares datetimes
lexicographically, and a hand-written ``T``-separated timestamp does not
sort against the space-separated ones SQLAlchemy writes.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.compliance.orm import SINGLETON_ID, ComplianceReport, InstallIdentity
from matofx.compliance.schedule import add_months, banner_text, reporting_status

ADOPTED = _dt.datetime(2026, 1, 15, 9, 0, tzinfo=_dt.UTC)


async def _identity(session: AsyncSession, *, adopted_at: _dt.datetime = ADOPTED) -> None:
    session.add(
        InstallIdentity(
            id=SINGLETON_ID,
            install_id="inst_TEST0001",
            adopted_at=adopted_at,
            self_generated=True,
            report_counter=0,
        )
    )
    await session.flush()


async def _report(session: AsyncSession, *, counter: int, generated_at: _dt.datetime) -> None:
    session.add(
        ComplianceReport(
            counter=counter,
            generated_at=generated_at,
            period_from=generated_at - _dt.timedelta(days=90),
            period_to=generated_at,
            head_id=1,
            head_hash="0" * 64,
            tree_size=1,
            tree_root="1" * 64,
            chain_ok=True,
            content_sha256="2" * 64,
            generated_by="user:1",
            document={},
        )
    )
    await session.flush()


# ---- the calendar helper --------------------------------------------


def test_add_months_is_calendar_months_not_thirty_day_blocks() -> None:
    assert add_months(_dt.datetime(2026, 1, 15, tzinfo=_dt.UTC), 3) == _dt.datetime(
        2026, 4, 15, tzinfo=_dt.UTC
    )


def test_add_months_clamps_to_the_end_of_a_shorter_month() -> None:
    """Anchored on the 31st, a quarterly cadence must not skip a month."""
    assert add_months(_dt.datetime(2026, 1, 31, tzinfo=_dt.UTC), 3) == _dt.datetime(
        2026, 4, 30, tzinfo=_dt.UTC
    )


def test_add_months_handles_a_leap_year() -> None:
    assert add_months(_dt.datetime(2028, 1, 31, tzinfo=_dt.UTC), 1) == _dt.datetime(
        2028, 2, 29, tzinfo=_dt.UTC
    )


def test_add_months_crosses_a_year_boundary() -> None:
    assert add_months(_dt.datetime(2026, 11, 15, tzinfo=_dt.UTC), 3) == _dt.datetime(
        2027, 2, 15, tzinfo=_dt.UTC
    )


# ---- never reported -------------------------------------------------


async def test_a_new_install_is_not_due_before_the_first_cadence(session: AsyncSession) -> None:
    await _identity(session)
    status = await reporting_status(
        session, cadence_months=3, now=_dt.datetime(2026, 3, 1, tzinfo=_dt.UTC)
    )
    assert status.due is False
    assert status.due_at == _dt.datetime(2026, 4, 15, 9, 0, tzinfo=_dt.UTC)
    assert status.last_generated_at is None


async def test_an_install_that_never_reported_eventually_says_so(session: AsyncSession) -> None:
    """The clock runs from adoption, so a site that never reports is caught."""
    await _identity(session)
    status = await reporting_status(
        session, cadence_months=3, now=_dt.datetime(2026, 5, 1, tzinfo=_dt.UTC)
    )
    assert status.due is True
    assert status.last_generated_at is None
    assert "hasn't been produced yet" in (banner_text(status) or "")


async def test_no_identity_yet_means_no_clock_and_no_banner(session: AsyncSession) -> None:
    """An uncommissioned install has nothing to be late against."""
    status = await reporting_status(
        session, cadence_months=3, now=_dt.datetime(2030, 1, 1, tzinfo=_dt.UTC)
    )
    assert status.due is False
    assert status.due_at is None
    assert banner_text(status) is None


# ---- the cadence ----------------------------------------------------


async def test_due_three_months_after_the_last_report(session: AsyncSession) -> None:
    await _identity(session)
    await _report(session, counter=1, generated_at=_dt.datetime(2026, 6, 10, tzinfo=_dt.UTC))
    status = await reporting_status(
        session, cadence_months=3, now=_dt.datetime(2026, 9, 11, tzinfo=_dt.UTC)
    )
    assert status.due_at == _dt.datetime(2026, 9, 10, tzinfo=_dt.UTC)
    assert status.due is True
    assert status.days_overdue == 1


async def test_not_due_the_day_before(session: AsyncSession) -> None:
    await _identity(session)
    await _report(session, counter=1, generated_at=_dt.datetime(2026, 6, 10, tzinfo=_dt.UTC))
    status = await reporting_status(
        session, cadence_months=3, now=_dt.datetime(2026, 9, 9, tzinfo=_dt.UTC)
    )
    assert status.due is False
    assert banner_text(status) is None


async def test_generating_a_report_clears_it_for_everyone(session: AsyncSession) -> None:
    """The whole point: it is a database fact, not a per-user dismissal."""
    await _identity(session)
    await _report(session, counter=1, generated_at=_dt.datetime(2026, 6, 10, tzinfo=_dt.UTC))
    now = _dt.datetime(2026, 9, 20, tzinfo=_dt.UTC)
    assert (await reporting_status(session, cadence_months=3, now=now)).due is True

    await _report(session, counter=2, generated_at=now)

    after = await reporting_status(session, cadence_months=3, now=now)
    assert after.due is False
    assert after.last_generated_at == now
    assert after.due_at == _dt.datetime(2026, 12, 20, tzinfo=_dt.UTC)


async def test_the_latest_report_wins_even_if_a_clock_moved_backwards(
    session: AsyncSession,
) -> None:
    """Ordered on the monotonic counter, not on a timestamp that can lie."""
    await _identity(session)
    await _report(session, counter=1, generated_at=_dt.datetime(2026, 6, 10, tzinfo=_dt.UTC))
    await _report(session, counter=2, generated_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.UTC))
    status = await reporting_status(
        session, cadence_months=3, now=_dt.datetime(2026, 6, 20, tzinfo=_dt.UTC)
    )
    assert status.last_generated_at == _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)


@pytest.mark.parametrize("cadence", [1, 6, 12])
async def test_the_cadence_is_whatever_the_licence_says(
    session: AsyncSession, cadence: int
) -> None:
    await _identity(session)
    await _report(session, counter=1, generated_at=_dt.datetime(2026, 1, 10, tzinfo=_dt.UTC))
    status = await reporting_status(
        session, cadence_months=cadence, now=_dt.datetime(2026, 1, 11, tzinfo=_dt.UTC)
    )
    assert status.due_at == add_months(_dt.datetime(2026, 1, 10, tzinfo=_dt.UTC), cadence)


# ---- switched off ---------------------------------------------------


async def test_zero_months_turns_the_reminder_off_entirely(session: AsyncSession) -> None:
    """How a customer who does not want reminding is shipped."""
    await _identity(session)
    await _report(session, counter=1, generated_at=_dt.datetime(2020, 1, 1, tzinfo=_dt.UTC))
    status = await reporting_status(
        session, cadence_months=0, now=_dt.datetime(2030, 1, 1, tzinfo=_dt.UTC)
    )
    assert status.due is False
    assert status.due_at is None
    assert banner_text(status) is None
    # Still reports the last one, so the admin page can show it.
    assert status.last_generated_at == _dt.datetime(2020, 1, 1, tzinfo=_dt.UTC)


# ---- wording --------------------------------------------------------


async def test_due_today_does_not_read_as_overdue_by_zero_days(
    session: AsyncSession,
) -> None:
    await _identity(session)
    await _report(session, counter=1, generated_at=_dt.datetime(2026, 6, 10, tzinfo=_dt.UTC))
    status = await reporting_status(
        session, cadence_months=3, now=_dt.datetime(2026, 9, 10, tzinfo=_dt.UTC)
    )
    text = banner_text(status) or ""
    assert "is due today" in text
    assert "0 days" not in text


async def test_the_banner_says_nothing_stops_working(session: AsyncSession) -> None:
    """It sits next to two banners that mean the site is degraded.

    An administrator who reads this as "the site is about to lock" will
    either panic or resent it, and neither produces the report their
    supplier asked for.
    """
    await _identity(session)
    await _report(session, counter=1, generated_at=_dt.datetime(2026, 6, 10, tzinfo=_dt.UTC))
    status = await reporting_status(
        session, cadence_months=3, now=_dt.datetime(2026, 10, 1, tzinfo=_dt.UTC)
    )
    text = banner_text(status) or ""
    assert "Nothing stops working" in text
    assert "sends nothing anywhere" in text
