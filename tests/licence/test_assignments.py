"""FAL floating-assignment accounting (ADR-0019 slice 5).

The asymmetry with CSL is the thing to hold onto: **granting** is
metered, **revoking** never is. An install that has hit its cap must
always be able to get back under it, or an admin can lock the licence
into a state only the supplier can undo.
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
from matofx.licence import (
    admit_assignment,
    count_assignments,
    release_assignment,
    resolve_licence,
)
from matofx.users.orm import User
from matofx.users.role_orm import Role
from tests.licence.conftest import utcnow, write_licence

_UNSIGNED_AUDIT = AuditWriter(signing_key=None, key_id=None)


async def _make_users(session: AsyncSession, *, active: int, inactive: int = 0) -> None:
    role_id = (
        await session.execute(sa.select(Role.id).where(Role.name == "regular_user"))
    ).scalar_one()
    for index in range(active + inactive):
        session.add(
            User(
                username=f"user{index}",
                email=f"user{index}@example.com",
                first_name="Test",
                last_name=str(index),
                full_name=f"Test {index}",
                password_hash="not-a-real-hash",
                is_active=index < active,
                role_id=role_id,
            )
        )
    await session.flush()


def _fal(settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey, n_slots: int) -> Settings:
    path = write_licence(
        tmp_path / f"fal-{n_slots}.json",
        issuer_private=issuer,
        licence_type="fal",
        n_slots=n_slots,
    )
    return settings.model_copy(update={"licence": LicenceSettings(file=path)})


@pytest.mark.asyncio
async def test_only_active_accounts_hold_an_assignment(session: AsyncSession) -> None:
    """§C16: deactivated users still exist — they just can't sign in."""
    await _make_users(session, active=3, inactive=4)
    assert await count_assignments(session) == 3


@pytest.mark.asyncio
async def test_granted_below_the_cap(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    await ensure_chain_head(session)
    licensed = _fal(settings, tmp_path, issuer, n_slots=5)
    await _make_users(session, active=2)

    decision = await admit_assignment(
        session,
        resolution=resolve_licence(licensed),
        audit=_UNSIGNED_AUDIT,
        actor="user:1",
        target_username="newcomer",
    )

    assert decision.outcome == "granted"
    assert decision.message is None
    assert await _count(session, "licence.assignment.granted") == 1


@pytest.mark.asyncio
async def test_denied_at_the_cap_with_an_actionable_message(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    await ensure_chain_head(session)
    licensed = _fal(settings, tmp_path, issuer, n_slots=2)
    await _make_users(session, active=2)

    decision = await admit_assignment(
        session,
        resolution=resolve_licence(licensed),
        audit=_UNSIGNED_AUDIT,
        actor="user:1",
        target_username="newcomer",
    )

    assert decision.outcome == "denied"
    assert not decision.admitted
    message = decision.message or ""
    # The admin needs both options spelled out — this is a decision, not
    # something that resolves itself if they wait.
    assert "Remove access from someone" in message
    assert "extend the licence" in message
    assert await _count(session, "licence.assignment.denied") == 1


@pytest.mark.asyncio
async def test_revoking_is_never_gated(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """Getting back under the cap must always work, even when over it."""
    await ensure_chain_head(session)
    licensed = _fal(settings, tmp_path, issuer, n_slots=1)
    await _make_users(session, active=4)  # already over cap, e.g. after a downgrade

    await release_assignment(
        session,
        resolution=resolve_licence(licensed),
        audit=_UNSIGNED_AUDIT,
        actor="user:1",
        target_username="user0",
        target_user_id=1,
    )
    assert await _count(session, "licence.assignment.revoked") == 1


@pytest.mark.asyncio
async def test_csl_licence_does_not_meter_assignments(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """Under CSL the places are sessions, not accounts."""
    await ensure_chain_head(session)
    path = write_licence(
        tmp_path / "csl.json", issuer_private=issuer, licence_type="csl", n_slots=1
    )
    licensed = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    await _make_users(session, active=50)

    decision = await admit_assignment(
        session,
        resolution=resolve_licence(licensed),
        audit=_UNSIGNED_AUDIT,
        actor="user:1",
        target_username="newcomer",
    )
    assert decision.outcome == "unmetered"
    assert await _count(session, "licence.assignment.granted") == 0


@pytest.mark.asyncio
async def test_read_only_mode_is_unmetered(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """A site that can't submit work shouldn't be unable to re-enable an account."""
    await ensure_chain_head(session)
    past = (utcnow() - _dt.timedelta(days=400)).isoformat().replace("+00:00", "Z")
    path = write_licence(
        tmp_path / "expired.json",
        issuer_private=issuer,
        licence_type="fal",
        n_slots=1,
        issued_at="2020-01-01T00:00:00Z",
        expires_at=past,
    )
    expired = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    await _make_users(session, active=10)

    decision = await admit_assignment(
        session,
        resolution=resolve_licence(expired),
        audit=_UNSIGNED_AUDIT,
        actor="user:1",
        target_username="newcomer",
    )
    assert decision.outcome == "unmetered"


@pytest.mark.asyncio
async def test_no_licence_is_unmetered(session: AsyncSession, settings: Settings) -> None:
    await ensure_chain_head(session)
    unlicensed = settings.model_copy(update={"licence": LicenceSettings(file=None)})
    await _make_users(session, active=10)

    decision = await admit_assignment(
        session,
        resolution=resolve_licence(unlicensed),
        audit=_UNSIGNED_AUDIT,
        actor="user:1",
        target_username="newcomer",
    )
    assert decision.outcome == "unmetered"


async def _count(session: AsyncSession, action: str) -> int:
    return int(
        (
            await session.execute(
                sa.select(sa.func.count(AuditLog.id)).where(AuditLog.action == action)
            )
        ).scalar_one()
    )
