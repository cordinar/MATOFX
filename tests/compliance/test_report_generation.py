"""End-to-end report generation against a real chain (ADR-0019 slice 1).

These are the tests that prove the two halves fit: an install generates a
report from its own database, and the vendor-side verifier — given only
the ledger — reaches the right verdict about it. Including the case that
matters most: an install that rewrites its chain between reports.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.audit import RecordInput, ensure_chain_head, make_audit_writer
from matofx.audit.orm import AuditLog
from matofx.compliance import (
    CONSISTENT,
    CONTRADICTED,
    LicenceSnapshot,
    adopt_identity,
    entry_from_report,
    generate_report,
    latest_for,
    load_identity_file,
    verify_report,
)
from matofx.config import Settings
from matofx.users.orm import AuthSession, User
from matofx.users.role_orm import Role
from tests.compliance.conftest import utcnow


async def _seed_chain(session: AsyncSession, settings: Settings, *, count: int = 5) -> None:
    """Append ``count`` ordinary audit records, as a live install would."""
    await ensure_chain_head(session)
    writer = make_audit_writer(settings.audit)
    for index in range(count):
        await writer.record(
            session,
            RecordInput(
                actor=f"user:{index % 3 + 1}",
                actor_type="user",
                action="login.success",
                resource_type="session",
                resource_id=f"s-{index}",
                outcome="success",
            ),
        )
    await session.flush()


async def _seed_sessions(session: AsyncSession, *, overlapping: int = 3) -> None:
    """Users + overlapping auth sessions, so peak concurrency is non-trivial."""
    role_id = (
        await session.execute(sa.select(Role.id).where(Role.name == "regular_user"))
    ).scalar_one()
    start = utcnow() - _dt.timedelta(hours=2)
    for index in range(overlapping):
        user = User(
            username=f"user{index}",
            email=f"user{index}@example.com",
            first_name="Test",
            last_name=f"User{index}",
            full_name=f"Test User{index}",
            password_hash="x",
            is_active=True,
            role_id=role_id,
        )
        session.add(user)
        await session.flush()
        # Opened two hours ago and still active, so every session overlaps
        # the reporting window (which starts at the first audit record).
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=f"hash-{index}",
                created_at=start,
                last_activity_at=utcnow(),
            )
        )
    await session.flush()


async def _adopt(session: AsyncSession, settings: Settings, identity_file: Path) -> object:
    issued = load_identity_file(
        identity_file,
        extra_issuer_keys=dict(settings.compliance.issuer_public_keys),
        require_issuer=False,
    )
    await adopt_identity(session, issued, now=utcnow())
    return issued


@pytest.mark.asyncio
async def test_first_report_shape_and_signature(
    session: AsyncSession, settings: Settings, identity_file: Path
) -> None:
    await _seed_chain(session, settings)
    await _seed_sessions(session)
    issued = await _adopt(session, settings, identity_file)

    generated = await generate_report(session, settings=settings, issued=issued, actor="cli")
    document = generated.document

    assert generated.counter == 1
    assert document["install"]["install_id"] == "inst_TEST0001"
    assert document["licence"]["state"] == "not_installed"
    assert document["chain_verification"]["ok"] is True
    assert document["chain_verification"]["records_checked"] == 5
    assert document["anchor"]["previous_report"] is None
    assert document["anchor"]["head"]["id"] == 5
    assert document["anchor"]["genesis_record"]["id"] == 1
    # Both licence types are always present; neither is authoritative yet.
    assert document["usage"]["licence_type"] is None
    assert document["usage"]["csl"]["peak_concurrent_sessions"] == 3
    assert document["usage"]["fal"]["assignments_current"] == 3
    assert document["usage"]["fal"]["source"] == "enabled_accounts"

    # The signature verifies against the issued key, over what was written.
    verdict = verify_report(document, ledger=[], public_key_hex=issued.public_key_hex())
    assert next(c.status for c in verdict.checks if c.name == "signature") == "ok"


@pytest.mark.asyncio
async def test_generation_is_recorded_in_the_chain(
    session: AsyncSession, settings: Settings, identity_file: Path
) -> None:
    """Taking a report is itself an auditable event (and lands after the head)."""
    await _seed_chain(session, settings)
    issued = await _adopt(session, settings, identity_file)

    generated = await generate_report(session, settings=settings, issued=issued, actor="user:1")

    row = (
        (
            await session.execute(
                sa.select(AuditLog).where(AuditLog.action == "compliance.report.generated")
            )
        )
        .scalars()
        .one()
    )
    assert row.payload["counter"] == generated.counter
    assert row.payload["content_sha256"] == generated.content_sha256
    # Recorded *after* the reported head, so it belongs to the next period.
    assert row.id > int(generated.document["anchor"]["head"]["id"])


@pytest.mark.asyncio
async def test_consecutive_reports_anchor_and_verify_clean(
    session: AsyncSession, settings: Settings, identity_file: Path
) -> None:
    """The honest path: report 2 extends the chain report 1 attested to."""
    await _seed_chain(session, settings)
    issued = await _adopt(session, settings, identity_file)
    first = await generate_report(session, settings=settings, issued=issued, actor="cli")

    ledger = [
        entry_from_report(
            first.document,
            public_key=issued.public_key_hex(),
            content_sha256=first.content_sha256,
        )
    ]

    await _seed_chain(session, settings, count=4)
    second = await generate_report(session, settings=settings, issued=issued, actor="cli")

    assert second.counter == 2
    anchor = second.document["anchor"]
    assert anchor["previous_report"]["counter"] == 1
    assert anchor["previous_head_rehashed_now"] == anchor["previous_report"]["head_hash"]

    verdict = verify_report(second.document, ledger=ledger)
    assert verdict.verdict == CONSISTENT
    # Periods tile: report 2 starts where report 1 was generated.
    assert second.document["period"]["from"] == first.document["generated_at"]


@pytest.mark.asyncio
async def test_rewritten_history_is_caught_by_the_ledger(
    session: AsyncSession, settings: Settings, identity_file: Path
) -> None:
    """The scenario the design exists for.

    Between reports, the install edits the record report 1 attested to.
    Its *next* report is honestly generated and correctly signed — but it
    cannot make record N hash what Funis wrote down.
    """
    await _seed_chain(session, settings)
    issued = await _adopt(session, settings, identity_file)
    first = await generate_report(session, settings=settings, issued=issued, actor="cli")
    ledger = [
        entry_from_report(
            first.document,
            public_key=issued.public_key_hex(),
            content_sha256=first.content_sha256,
        )
    ]
    attested_head = latest_for(ledger, "inst_TEST0001")
    assert attested_head is not None

    # Tamper: rewrite the attested record's hash in place.
    await session.execute(
        sa.update(AuditLog).where(AuditLog.id == attested_head.head_id).values(hash="ff" * 32)
    )
    await session.flush()

    await _seed_chain(session, settings, count=2)
    second = await generate_report(session, settings=settings, issued=issued, actor="cli")

    verdict = verify_report(second.document, ledger=ledger)
    assert verdict.verdict == CONTRADICTED
    anchoring = next(c for c in verdict.checks if c.name == "anchoring")
    assert anchoring.status == "fail"
    assert "does not verify" in anchoring.detail
    assert "rewritten, not merely extended" in anchoring.detail


@pytest.mark.asyncio
async def test_broken_chain_still_produces_a_report_that_says_so(
    session: AsyncSession, settings: Settings, identity_file: Path
) -> None:
    """Generation never fails closed — refusing would destroy the evidence."""
    await _seed_chain(session, settings)
    issued = await _adopt(session, settings, identity_file)
    await session.execute(sa.update(AuditLog).where(AuditLog.id == 3).values(hash="ee" * 32))
    await session.flush()

    generated = await generate_report(session, settings=settings, issued=issued, actor="cli")

    chain = generated.document["chain_verification"]
    assert chain["ok"] is False
    assert any(f["record_id"] == 3 for f in chain["failures"])
    verdict = verify_report(generated.document, ledger=[], public_key_hex=issued.public_key_hex())
    assert next(c.status for c in verdict.checks if c.name == "chain") == "fail"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("licence_type", "n_slots", "expect_over_cap"),
    [
        ("csl", 2, True),  # 3 overlapping sessions against 2 slots
        ("csl", 10, False),
        ("fal", 2, True),  # 3 enabled accounts against 2 assignments
        ("fal", 10, False),
    ],
)
async def test_authoritative_block_applies_the_cap(
    session: AsyncSession,
    settings: Settings,
    identity_file: Path,
    licence_type: str,
    n_slots: int,
    expect_over_cap: bool,
) -> None:
    """Both licence types measure their own figure against the cap.

    The *other* type's block is still emitted, but with ``n_slots: None``
    and ``over_cap: None`` — an observation, never a breach.
    """
    await _seed_chain(session, settings)
    await _seed_sessions(session)
    issued = await _adopt(session, settings, identity_file)

    licence = LicenceSnapshot(
        licence_id="LIC-TEST-1",
        licence_type=licence_type,  # type: ignore[arg-type]
        n_slots=n_slots,
        issued_at=utcnow() - _dt.timedelta(days=30),
        expires_at=utcnow() + _dt.timedelta(days=300),
        state="active",
        manifest_sha256="cd" * 32,
        features_enabled=["parametric_studies"],
    )
    generated = await generate_report(
        session, settings=settings, issued=issued, actor="cli", licence=licence
    )

    usage = generated.document["usage"]
    other = "fal" if licence_type == "csl" else "csl"
    assert usage["licence_type"] == licence_type
    assert usage[licence_type]["n_slots"] == n_slots
    assert usage[licence_type]["over_cap"] is expect_over_cap
    assert usage[other]["n_slots"] is None
    assert usage[other]["over_cap"] is None

    verdict = verify_report(generated.document, ledger=[], public_key_hex=issued.public_key_hex())
    breaches = [f for f in verdict.findings if f.severity == "breach"]
    assert bool(breaches) is expect_over_cap


@pytest.mark.asyncio
async def test_licence_event_counts_are_always_present(
    session: AsyncSession, settings: Settings, identity_file: Path
) -> None:
    """Zero-filled from slice 1, so a later missing key is a signal not a schema change."""
    await _seed_chain(session, settings)
    issued = await _adopt(session, settings, identity_file)
    generated = await generate_report(session, settings=settings, issued=issued, actor="cli")

    events = generated.document["licence_events"]
    for action in ("licence.slot.denied", "licence.assignment.granted", "audit.verify.failed"):
        assert events[action] == 0
