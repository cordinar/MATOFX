"""Transition events + the seats cap (ADR-0019 slice 7).

The last two gaps in the report: the moments the site's posture actually
changed (which the per-tick ``validated.ok`` drum can't show), and the
licensed cap the Usage tab has been rendering as "—" since Phase 1.

The cap has a trap worth pinning: the two licence types bound **different
figures** — CSL bounds peak concurrency, FAL bounds enabled seats — so a
tab that showed the number without saying which would be quietly wrong.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.audit import ensure_chain_head
from matofx.audit.orm import AuditLog
from matofx.compliance.schema import LICENCE_EVENT_ACTIONS
from matofx.config import LicenceSettings, Settings
from matofx.licence import recheck_licence, resolve_licence
from matofx.usage.service import UsageService
from tests.licence.conftest import utcnow, write_licence


def _at(days_from_now: int) -> str:
    return (
        (utcnow() + _dt.timedelta(days=days_from_now)).isoformat().replace("+00:00", "Z")
    )


def _licensed(
    settings: Settings, path: Path, issuer: Ed25519PrivateKey, *, expires_in_days: int,
    grace: int = 30, licence_type: str = "csl", n_slots: int = 10,
) -> Settings:
    write_licence(
        path,
        issuer_private=issuer,
        licence_type=licence_type,
        n_slots=n_slots,
        issued_at="2020-01-01T00:00:00Z",
        expires_at=_at(expires_in_days),
        grace_period_days=grace,
    )
    return settings.model_copy(update={"licence": LicenceSettings(file=path)})


async def _actions(session: AsyncSession) -> list[str]:
    rows = (
        await session.execute(
            sa.select(AuditLog.action)
            .where(AuditLog.action.in_(LICENCE_EVENT_ACTIONS))
            .order_by(AuditLog.id)
        )
    ).scalars()
    return list(rows)


# ----- transition events ----------------------------------------------


@pytest.mark.asyncio
async def test_expiry_transition_is_recorded_once(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """active → grace is a row a compliance reader scans for."""
    await ensure_chain_head(session)
    active = _licensed(settings, tmp_path / "l.json", issuer, expires_in_days=10)
    first = await recheck_licence(session, settings=active)
    assert first.status is not None
    assert first.status.state == "active"

    # The same file, now expired but inside grace.
    grace = _licensed(settings, tmp_path / "l.json", issuer, expires_in_days=-1)
    second = await recheck_licence(session, settings=grace, previous=first)
    assert second.status is not None
    assert second.status.state == "grace"

    transitions = [a for a in await _actions(session) if a == "licence.expiry.transition"]
    assert len(transitions) == 1

    # A further tick in the same state adds nothing — transitions only.
    await recheck_licence(session, settings=grace, previous=second)
    transitions = [a for a in await _actions(session) if a == "licence.expiry.transition"]
    assert len(transitions) == 1


@pytest.mark.asyncio
async def test_readonly_entered_and_left_are_both_recorded(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """The day it went read-only, and the day a renewal lifted it."""
    await ensure_chain_head(session)
    active = _licensed(settings, tmp_path / "l.json", issuer, expires_in_days=10)
    state = await recheck_licence(session, settings=active)

    expired = _licensed(
        settings, tmp_path / "l.json", issuer, expires_in_days=-400, grace=30
    )
    state = await recheck_licence(session, settings=expired, previous=state)
    assert state.read_only is True

    renewed = _licensed(settings, tmp_path / "l.json", issuer, expires_in_days=365)
    state = await recheck_licence(session, settings=renewed, previous=state)
    assert state.read_only is False

    actions = await _actions(session)
    assert actions.count("licence.readonly.entered") == 1
    assert actions.count("licence.readonly.left") == 1


@pytest.mark.asyncio
async def test_transition_event_names_both_ends(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """``active -> grace`` beats "something changed"."""
    await ensure_chain_head(session)
    active = _licensed(settings, tmp_path / "l.json", issuer, expires_in_days=10)
    first = await recheck_licence(session, settings=active)
    grace = _licensed(settings, tmp_path / "l.json", issuer, expires_in_days=-1)
    await recheck_licence(session, settings=grace, previous=first)

    row = (
        await session.execute(
            sa.select(AuditLog).where(AuditLog.action == "licence.expiry.transition")
        )
    ).scalars().one()
    assert row.payload["from"] == "active"
    assert row.payload["to"] == "grace"
    assert row.reason == "active -> grace"


@pytest.mark.asyncio
async def test_first_check_records_no_transition(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """Nothing to compare against yet — a boot isn't a transition."""
    await ensure_chain_head(session)
    active = _licensed(settings, tmp_path / "l.json", issuer, expires_in_days=10)
    await recheck_licence(session, settings=active)
    actions = await _actions(session)
    assert "licence.expiry.transition" not in actions
    assert "licence.readonly.entered" not in actions


# ----- the seats cap --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("licence_type", ["csl", "fal"])
async def test_seats_cap_comes_from_the_licence(
    session: AsyncSession,
    settings: Settings,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
    licence_type: str,
) -> None:
    """``cap`` stops being ``None``, and says which figure it governs."""
    from matofx.usage.service import JobFilters  # noqa: PLC0415

    licensed = _licensed(
        settings,
        tmp_path / f"{licence_type}.json",
        issuer,
        expires_in_days=365,
        licence_type=licence_type,
        n_slots=17,
    )
    stats = await UsageService().seats(
        session,
        filters=JobFilters(since=utcnow() - _dt.timedelta(days=7), until=utcnow()),
        settings=licensed,
    )
    assert stats.cap == 17
    assert stats.licence_type == licence_type
    assert stats.licence_state == "active"


@pytest.mark.asyncio
async def test_seats_cap_is_none_without_a_licence(
    session: AsyncSession, settings: Settings
) -> None:
    """The tab shows "—" rather than inventing an entitlement."""
    from matofx.usage.service import JobFilters  # noqa: PLC0415

    unlicensed = settings.model_copy(update={"licence": LicenceSettings(file=None)})
    stats = await UsageService().seats(
        session,
        filters=JobFilters(since=utcnow() - _dt.timedelta(days=7), until=utcnow()),
        settings=unlicensed,
    )
    assert stats.cap is None
    assert stats.licence_type is None


@pytest.mark.asyncio
async def test_seats_survives_a_broken_licence(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """A usage tab must not be the thing that surfaces a licence problem."""
    from matofx.usage.service import JobFilters  # noqa: PLC0415

    path = write_licence(tmp_path / "bad.json", issuer_private=issuer, tamper=True)
    broken = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    assert resolve_licence(broken).status is None

    stats = await UsageService().seats(
        session,
        filters=JobFilters(since=utcnow() - _dt.timedelta(days=7), until=utcnow()),
        settings=broken,
    )
    assert stats.cap is None
    assert stats.registered_users >= 0


def test_cap_suffix_names_the_figure_it_governs() -> None:
    """The UI must not show a bare number next to two different lines."""
    from matofx.ui.pages.admin_usage import _cap_suffix  # noqa: PLC0415

    assert "peak concurrent sessions" in _cap_suffix(
        {"cap": 10, "licence_type": "csl", "licence_state": "active"}
    )
    assert "enabled seats" in _cap_suffix(
        {"cap": 25, "licence_type": "fal", "licence_state": "active"}
    )
    assert _cap_suffix({"cap": None}) == "no licence installed"
    # A non-active state is worth saying out loud on the tile.
    assert "(grace)" in _cap_suffix(
        {"cap": 10, "licence_type": "csl", "licence_state": "grace"}
    )
