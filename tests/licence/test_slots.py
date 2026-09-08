"""CSL concurrent-session slot accounting (ADR-0019 slice 4).

The behaviours that matter, and the ways each could go wrong:

- **Liveness must match ``validate()``.** If the counter includes rows
  the session validator would reject, the licence refuses logins on
  behalf of sessions that are already dead — the worst possible bug
  here, because it looks like the customer is over-licensed.
- **Break-glass admits Super Users, and only them**, and says so.
- **Read-only mode is unmetered** — "every registered user may sign in
  and view results" cannot hold if the cap still turns people away.
- **A refusal must never leave a session behind.**
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.audit.orm import AuditLog
from matofx.config import LicenceSettings, Settings
from matofx.licence import resolve_licence
from matofx.licence.slots import (
    admit_session,
    confirm_admission,
    count_live_sessions,
    release_slot,
)
from matofx.users.orm import AuthSession, User
from matofx.users.role_orm import Role
from tests.licence.conftest import utcnow, write_licence

_UNSIGNED_AUDIT = AuditWriter(signing_key=None, key_id=None)


async def _make_user(session: AsyncSession, *, username: str, role: str) -> User:
    role_id = (
        await session.execute(sa.select(Role.id).where(Role.name == role))
    ).scalar_one()
    user = User(
        username=username,
        email=f"{username}@example.com",
        first_name="Test",
        last_name=username,
        full_name=f"Test {username}",
        password_hash="not-a-real-hash",
        is_active=True,
        role_id=role_id,
    )
    session.add(user)
    await session.flush()
    return user


async def _open_sessions(
    session: AsyncSession,
    *,
    count: int,
    idle_minutes: int = 0,
    revoked: bool = False,
    age_hours: int = 0,
    prefix: str = "s",
) -> None:
    """Create ``count`` session rows with a given liveness shape."""
    now = utcnow()
    for index in range(count):
        session.add(
            AuthSession(
                user_id=1,
                token_hash=f"{prefix}-{index}-{idle_minutes}-{age_hours}",
                created_at=now - _dt.timedelta(hours=age_hours),
                last_activity_at=now - _dt.timedelta(minutes=idle_minutes),
                revoked_at=now if revoked else None,
            )
        )
    await session.flush()


async def _confirm(
    session: AsyncSession,
    *,
    settings: Settings,
    user: User,
    decision,
    token: str = "confirm",  # noqa: S107 - a session token_hash fixture, not a password
):
    """Insert the session row and confirm the provisional decision.

    Admission is two-phase since ADR-0019 §6.1: ``admit_session`` decides
    before the row exists, ``confirm_admission`` re-checks after it does
    and records the outcome. Tests that assert on the audit event have to
    run both halves, because that is what a real login does.
    """
    row = AuthSession(
        user_id=user.id,
        token_hash=token,
        created_at=utcnow(),
        last_activity_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    return await confirm_admission(
        session,
        settings=settings,
        resolution=resolve_licence(settings),
        user=user,
        decision=decision,
        session_id=int(row.id),
        audit=_UNSIGNED_AUDIT,
    )


def _csl(settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey, n_slots: int) -> Settings:
    path = write_licence(
        tmp_path / f"csl-{n_slots}.json", issuer_private=issuer, licence_type="csl",
        n_slots=n_slots,
    )
    return settings.model_copy(update={"licence": LicenceSettings(file=path)})


# ----- liveness -------------------------------------------------------


@pytest.mark.asyncio
async def test_only_live_sessions_hold_a_place(
    session: AsyncSession, settings: Settings
) -> None:
    """Revoked, idle-timed-out and hard-timed-out rows must not count.

    Same rule as ``SessionService.validate`` — anything else and the
    licence starts refusing logins for sessions that are already dead.
    """
    await _make_user(session, username="alice", role="regular_user")
    await _open_sessions(session, count=2, prefix="live")
    await _open_sessions(session, count=3, revoked=True, prefix="revoked")
    # Default idle timeout is 30 minutes; 90 minutes idle is long gone.
    await _open_sessions(session, count=4, idle_minutes=90, prefix="idle")
    # Default hard timeout is 12 hours.
    await _open_sessions(session, count=5, age_hours=20, prefix="old")

    assert await count_live_sessions(session, settings=settings) == 2


# ----- the four outcomes ----------------------------------------------


@pytest.mark.asyncio
async def test_granted_below_the_cap(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    await ensure_chain_head(session)
    licensed = _csl(settings, tmp_path, issuer, n_slots=3)
    user = await _make_user(session, username="alice", role="regular_user")
    await _open_sessions(session, count=1)

    decision = await admit_session(
        session,
        settings=licensed,
        resolution=resolve_licence(licensed),
        user=user,
        role_name="regular_user",
        audit=_UNSIGNED_AUDIT,
    )

    assert decision.outcome == "granted"
    assert decision.admitted
    assert decision.message is None

    final = await _confirm(session, settings=licensed, user=user, decision=decision)
    assert final.outcome == "granted"
    assert await _action_count(session, "licence.slot.granted") == 1


@pytest.mark.asyncio
async def test_denied_at_the_cap(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    await ensure_chain_head(session)
    licensed = _csl(settings, tmp_path, issuer, n_slots=2)
    user = await _make_user(session, username="alice", role="regular_user")
    await _open_sessions(session, count=2)

    decision = await admit_session(
        session,
        settings=licensed,
        resolution=resolve_licence(licensed),
        user=user,
        role_name="regular_user",
        audit=_UNSIGNED_AUDIT,
    )

    assert decision.outcome == "denied"
    assert not decision.admitted
    # The message must name the cap and a way out — this is what an
    # engineer reads at 9am with a correct password.
    assert "All 2 MATOFX places are in use" in (decision.message or "")
    assert "administrator" in (decision.message or "")
    assert await _action_count(session, "licence.slot.denied") == 1


@pytest.mark.asyncio
async def test_super_user_gets_in_over_the_cap(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """Break-glass — otherwise an admin is locked out of their own install."""
    await ensure_chain_head(session)
    licensed = _csl(settings, tmp_path, issuer, n_slots=2)
    user = await _make_user(session, username="root", role="super_user")
    await _open_sessions(session, count=2)

    decision = await admit_session(
        session,
        settings=licensed,
        resolution=resolve_licence(licensed),
        user=user,
        role_name="super_user",
        audit=_UNSIGNED_AUDIT,
    )

    assert decision.outcome == "breakglass"
    assert decision.admitted

    final = await _confirm(session, settings=licensed, user=user, decision=decision)
    assert final.outcome == "breakglass"
    event = (
        await session.execute(
            sa.select(AuditLog).where(AuditLog.action == "licence.slot.breakglass")
        )
    ).scalars().one()
    # The excess must be explained, not merely permitted.
    assert event.reason == "admitted over the cap as a Super User"
    assert event.payload["in_use"] == 2
    assert event.payload["n_slots"] == 2


@pytest.mark.asyncio
async def test_administrator_is_not_break_glass(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """Only Super Users. An Administrator at the cap waits like everyone else."""
    await ensure_chain_head(session)
    licensed = _csl(settings, tmp_path, issuer, n_slots=1)
    user = await _make_user(session, username="admin", role="administrator")
    await _open_sessions(session, count=1)

    decision = await admit_session(
        session,
        settings=licensed,
        resolution=resolve_licence(licensed),
        user=user,
        role_name="administrator",
        audit=_UNSIGNED_AUDIT,
    )
    assert decision.outcome == "denied"


@pytest.mark.asyncio
async def test_no_licence_is_unmetered(session: AsyncSession, settings: Settings) -> None:
    await ensure_chain_head(session)
    unlicensed = settings.model_copy(update={"licence": LicenceSettings(file=None)})
    user = await _make_user(session, username="alice", role="regular_user")
    await _open_sessions(session, count=50)

    decision = await admit_session(
        session,
        settings=unlicensed,
        resolution=resolve_licence(unlicensed),
        user=user,
        role_name="regular_user",
        audit=_UNSIGNED_AUDIT,
    )

    assert decision.outcome == "unmetered"
    assert decision.admitted
    # Unmetered logins write no slot events — the chain shouldn't fill
    # with records about a cap that doesn't exist.
    assert await _action_count(session, "licence.slot.granted") == 0


@pytest.mark.asyncio
async def test_fal_licence_does_not_meter_sessions(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """Under FAL the places are assignments, not sessions."""
    await ensure_chain_head(session)
    path = write_licence(
        tmp_path / "fal.json", issuer_private=issuer, licence_type="fal", n_slots=1
    )
    licensed = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    user = await _make_user(session, username="alice", role="regular_user")
    await _open_sessions(session, count=5)

    decision = await admit_session(
        session,
        settings=licensed,
        resolution=resolve_licence(licensed),
        user=user,
        role_name="regular_user",
        audit=_UNSIGNED_AUDIT,
    )
    assert decision.outcome == "unmetered"


@pytest.mark.asyncio
async def test_read_only_mode_is_unmetered(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """§C12: every registered user may sign in and view results.

    That cannot hold if an expired licence still turns people away at
    the cap, so slot accounting suspends in read-only mode.
    """
    await ensure_chain_head(session)
    past = (utcnow() - _dt.timedelta(days=400)).isoformat().replace("+00:00", "Z")
    path = write_licence(
        tmp_path / "expired.json",
        issuer_private=issuer,
        licence_type="csl",
        n_slots=1,
        issued_at="2020-01-01T00:00:00Z",
        expires_at=past,
    )
    expired = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    user = await _make_user(session, username="alice", role="regular_user")
    await _open_sessions(session, count=5)

    resolution = resolve_licence(expired)
    assert resolution.read_only is True
    decision = await admit_session(
        session,
        settings=expired,
        resolution=resolution,
        user=user,
        role_name="regular_user",
        audit=_UNSIGNED_AUDIT,
    )
    assert decision.outcome == "unmetered"


# ----- release --------------------------------------------------------


@pytest.mark.asyncio
async def test_release_records_under_csl(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    await ensure_chain_head(session)
    licensed = _csl(settings, tmp_path, issuer, n_slots=3)
    await release_slot(
        session,
        audit=_UNSIGNED_AUDIT,
        resolution=resolve_licence(licensed),
        user_id=1,
        session_id=7,
        actor="user:1",
        reason="signed out",
    )
    assert await _action_count(session, "licence.slot.released") == 1


@pytest.mark.asyncio
async def test_release_is_silent_without_csl(
    session: AsyncSession, settings: Settings
) -> None:
    """No cap, no meaning — don't fill the chain with empty events."""
    await ensure_chain_head(session)
    unlicensed = settings.model_copy(update={"licence": LicenceSettings(file=None)})
    await release_slot(
        session,
        audit=_UNSIGNED_AUDIT,
        resolution=resolve_licence(unlicensed),
        user_id=1,
        session_id=7,
        actor="user:1",
        reason="signed out",
    )
    assert await _action_count(session, "licence.slot.released") == 0


async def _action_count(session: AsyncSession, action: str) -> int:
    return int(
        (
            await session.execute(
                sa.select(sa.func.count(AuditLog.id)).where(AuditLog.action == action)
            )
        ).scalar_one()
    )


# ----- the admission race (ADR-0019 §6.1) -----------------------------


@pytest.mark.asyncio
async def test_concurrent_logins_at_the_boundary_cannot_exceed_the_cap(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """N logins that all pass the pre-check must not all keep their place.

    ``admit_session`` counts and the caller inserts — two statements with
    no lock between them. Every existing test logs in sequentially, so
    the window was invisible: each request saw the previous one's row and
    behaved. Here every request takes its decision *before* any of them
    inserts, which is what a real burst does.

    Without :func:`confirm_admission` this ends with 5 live sessions
    against 3 slots.
    """
    await ensure_chain_head(session)
    licensed = _csl(settings, tmp_path, issuer, n_slots=3)
    resolution = resolve_licence(licensed)
    users = [
        await _make_user(session, username=f"racer{i}", role="regular_user")
        for i in range(5)
    ]

    # Every request decides first — nobody has inserted yet.
    decisions = [
        await admit_session(
            session,
            settings=licensed,
            resolution=resolution,
            user=user,
            role_name="regular_user",
            audit=_UNSIGNED_AUDIT,
        )
        for user in users
    ]
    assert all(d.outcome == "granted" for d in decisions), (
        "the pre-check is expected to admit everyone — that is the race"
    )

    # Then each inserts and confirms, as the login route now does.
    admitted = 0
    for user, decision in zip(users, decisions, strict=True):
        row = AuthSession(
            user_id=user.id,
            token_hash=f"race-{user.id}",
            created_at=utcnow(),
            last_activity_at=utcnow(),
        )
        session.add(row)
        await session.flush()
        final = await confirm_admission(
            session,
            settings=licensed,
            resolution=resolution,
            user=user,
            decision=decision,
            session_id=int(row.id),
            audit=_UNSIGNED_AUDIT,
        )
        if final.admitted:
            admitted += 1

    assert admitted == 3, f"admitted {admitted} logins against a 3-slot licence"
    assert await count_live_sessions(session, settings=licensed) == 3


@pytest.mark.asyncio
async def test_a_withdrawn_login_leaves_no_granted_event(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """A bounced login must not claim in the chain that it was admitted.

    The compliance report counts ``licence.slot.granted``; a provisional
    grant that was withdrawn would inflate the customer's own usage
    evidence against them.
    """
    await ensure_chain_head(session)
    licensed = _csl(settings, tmp_path, issuer, n_slots=1)
    resolution = resolve_licence(licensed)
    winner = await _make_user(session, username="winner", role="regular_user")
    loser = await _make_user(session, username="loser", role="regular_user")

    decisions = {}
    for user in (winner, loser):
        decisions[user.username] = await admit_session(
            session, settings=licensed, resolution=resolution, user=user,
            role_name="regular_user", audit=_UNSIGNED_AUDIT,
        )

    for user in (winner, loser):
        row = AuthSession(
            user_id=user.id,
            token_hash=f"bounce-{user.id}",
            created_at=utcnow(),
            last_activity_at=utcnow(),
        )
        session.add(row)
        await session.flush()
        await confirm_admission(
            session, settings=licensed, resolution=resolution, user=user,
            decision=decisions[user.username], session_id=int(row.id),
            audit=_UNSIGNED_AUDIT,
        )

    actions = list(
        (await session.execute(sa.select(AuditLog.action, AuditLog.payload))).all()
    )
    granted = [a for a, _ in actions if a == "licence.slot.granted"]
    denied = [a for a, _ in actions if a == "licence.slot.denied"]
    assert len(granted) == 1, f"expected exactly one grant, got {granted}"
    assert len(denied) == 1, f"expected exactly one denial, got {denied}"


@pytest.mark.asyncio
async def test_break_glass_is_never_withdrawn(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """A Super User admitted over the cap keeps their place (§C13).

    Racing away the break-glass admission would remove the only way back
    into an install whose licence needs fixing.
    """
    await ensure_chain_head(session)
    licensed = _csl(settings, tmp_path, issuer, n_slots=1)
    resolution = resolve_licence(licensed)
    boss = await _make_user(session, username="boss", role="super_user")
    await _open_sessions(session, count=1, prefix="occupied")

    decision = await admit_session(
        session, settings=licensed, resolution=resolution, user=boss,
        role_name="super_user", audit=_UNSIGNED_AUDIT,
    )
    assert decision.outcome == "breakglass"

    row = AuthSession(
        user_id=boss.id, token_hash="bg", created_at=utcnow(), last_activity_at=utcnow()
    )
    session.add(row)
    await session.flush()
    final = await confirm_admission(
        session, settings=licensed, resolution=resolution, user=boss,
        decision=decision, session_id=int(row.id), audit=_UNSIGNED_AUDIT,
    )
    assert final.outcome == "breakglass"
    assert final.admitted
