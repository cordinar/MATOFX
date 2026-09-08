"""Installing a licence, re-checking it, and feeding the compliance report.

The integration that matters for slice 2: once a licence is installed,
the compliance report stops saying ``not_installed`` and starts measuring
usage against a real cap — for **both** licence types — and the report
signing key comes from the manifest rather than the standalone identity
file.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.audit import ensure_chain_head, make_audit_writer
from matofx.audit.orm import AuditLog
from matofx.audit.schema import RecordInput
from matofx.compliance import adopt_identity, generate_report, verify_report
from matofx.config import LicenceSettings, Settings
from matofx.licence import (
    current_install,
    install_licence,
    licence_snapshot,
    load_licence_file,
    record_validation,
    report_identity,
    resolve_licence,
    verify_install_binding,
)
from matofx.licence.errors import LicenceInvalidError
from matofx.users.orm import AuthSession, User
from matofx.users.role_orm import Role
from tests.licence.conftest import ISSUER_KEY_ID, public_hex, utcnow, write_licence


def _load(path: Path, issuer: Ed25519PrivateKey) -> object:
    """Load a licence with the test issuer trusted."""
    return load_licence_file(path, extra_issuer_keys={ISSUER_KEY_ID: public_hex(issuer)})


async def _seed_chain(session: AsyncSession, settings: Settings, count: int = 3) -> None:
    await ensure_chain_head(session)
    writer = make_audit_writer(settings.audit)
    for index in range(count):
        await writer.record(
            session,
            RecordInput(
                actor="user:1",
                actor_type="user",
                action="login.success",
                resource_type="session",
                resource_id=f"s-{index}",
                outcome="success",
            ),
        )
    await session.flush()


async def _seed_people(session: AsyncSession, count: int = 2) -> None:
    """Users with overlapping, still-active sessions.

    Gives both licence types something to measure: CSL sees `count`
    concurrent sessions, FAL sees `count` enabled accounts.
    """
    role_id = (
        await session.execute(sa.select(Role.id).where(Role.name == "regular_user"))
    ).scalar_one()
    start = utcnow() - _dt.timedelta(hours=2)
    for index in range(count):
        user = User(
            username=f"user{index}",
            email=f"user{index}@example.com",
            first_name="Test",
            last_name=str(index),
            full_name=f"Test {index}",
            password_hash="not-a-real-hash",
            is_active=True,
            role_id=role_id,
        )
        session.add(user)
        await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=f"hash-{index}",
                created_at=start,
                last_activity_at=utcnow(),
            )
        )
    await session.flush()


async def _seed_identity(session: AsyncSession, install_id: str) -> None:
    """Give this install a known id, as enrolment would have done."""
    from matofx.compliance.identity import ensure_install_identity  # noqa: PLC0415

    row = await ensure_install_identity(session)
    row.install_id = install_id
    await session.flush()


@pytest.mark.asyncio
async def test_install_records_the_licence_and_audits_it(
    session: AsyncSession, settings: Settings, licence_file: Path, issuer: Ed25519PrivateKey
) -> None:
    await ensure_chain_head(session)
    # Enrolment happened first: the install has the id this licence names.
    await _seed_identity(session, "inst_TEST0001")
    loaded = _load(licence_file, issuer)

    row = await install_licence(session, settings=settings, loaded=loaded, actor="user:1")

    assert row.licence_id == "LIC-TEST-0001"
    assert row.licence_type == "csl"
    assert row.n_slots == 10
    assert row.issuer_verified is True
    stored = await current_install(session)
    assert stored is not None
    assert stored.manifest_sha256 == loaded.file_sha256

    event = (
        await session.execute(sa.select(AuditLog).where(AuditLog.action == "licence.installed"))
    ).scalars().one()
    assert event.payload["licence_id"] == "LIC-TEST-0001"
    assert event.payload["n_slots"] == 10
    assert event.payload["replaced_licence_id"] is None


@pytest.mark.asyncio
async def test_replacing_a_licence_names_what_it_replaced(
    session: AsyncSession,
    settings: Settings,
    licence_file: Path,
    issuer: Ed25519PrivateKey,
    tmp_path: Path,
) -> None:
    """A renewal is the common case — the audit trail should read as one."""
    await ensure_chain_head(session)
    await _seed_identity(session, "inst_TEST0001")
    await install_licence(
        session, settings=settings, loaded=_load(licence_file, issuer), actor="user:1"
    )
    upgraded = write_licence(
        tmp_path / "renewed.json",
        issuer_private=issuer,
        n_slots=25,
        overrides={"licence_id": "LIC-TEST-0002"},
    )
    row = await install_licence(
        session, settings=settings, loaded=_load(upgraded, issuer), actor="user:1"
    )

    assert row.n_slots == 25
    assert row.licence_id == "LIC-TEST-0002"
    events = (
        await session.execute(
            sa.select(AuditLog).where(AuditLog.action == "licence.installed").order_by(AuditLog.id)
        )
    ).scalars().all()
    assert len(events) == 2
    assert events[1].payload["replaced_licence_id"] == "LIC-TEST-0001"
    assert events[1].reason == "replaces LIC-TEST-0001"


@pytest.mark.asyncio
async def test_validation_outcomes_are_recorded_for_the_report(
    session: AsyncSession, settings: Settings
) -> None:
    """The counts a compliance report carries come from these events."""
    await ensure_chain_head(session)
    await record_validation(session, settings=settings, resolution=resolve_licence(settings))

    broken = settings.model_copy(
        update={"licence": LicenceSettings(file=Path("/nowhere/absent.json"))}
    )
    await record_validation(session, settings=broken, resolution=resolve_licence(broken))

    actions = [
        row.action
        for row in (
            await session.execute(
                sa.select(AuditLog).where(AuditLog.action.like("licence.validated%"))
            )
        ).scalars()
    ]
    assert actions == ["licence.validated.ok", "licence.validated.failed"]


def test_missing_licence_is_read_only_not_fail_closed(settings: Settings) -> None:
    broken = settings.model_copy(
        update={"licence": LicenceSettings(file=Path("/nowhere/absent.json"))}
    )
    resolution = resolve_licence(broken)
    assert resolution.status is None
    assert resolution.fail_closed is False
    assert resolution.read_only is True


def test_tampered_licence_fails_closed(
    settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """Someone edited the licence — that is not an availability problem."""
    path = write_licence(tmp_path / "bad.json", issuer_private=issuer, tamper=True)
    tampered = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    resolution = resolve_licence(tampered)
    assert resolution.status is None
    assert resolution.fail_closed is True
    assert "altered since it was signed" in (resolution.problem or "")


def test_no_licence_configured_is_read_only(settings: Settings) -> None:
    none_configured = settings.model_copy(update={"licence": LicenceSettings(file=None)})
    resolution = resolve_licence(none_configured)
    assert resolution.fail_closed is False
    assert resolution.read_only is True


# ----- the bridge into compliance -------------------------------------


def test_report_key_comes_from_the_licence(settings: Settings) -> None:
    identity = report_identity(resolve_licence(settings))
    assert identity is not None
    assert identity.install_id == "inst_TEST0001"
    assert len(identity.report_key_id) == 16


def test_licence_without_a_report_key_falls_back_cleanly(
    settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """Returns None rather than raising, so the caller can use the identity file."""
    path = write_licence(tmp_path / "nokey.json", issuer_private=issuer, with_report_key=False)
    without = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    assert report_identity(resolve_licence(without)) is None


def test_snapshot_is_none_when_the_licence_is_unusable(settings: Settings) -> None:
    """A report must not assert terms it cannot stand behind."""
    none_configured = settings.model_copy(update={"licence": LicenceSettings(file=None)})
    assert licence_snapshot(resolve_licence(none_configured)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("licence_type", "n_slots", "expect_over_cap"),
    [("csl", 1, True), ("csl", 50, False), ("fal", 1, True), ("fal", 50, False)],
)
async def test_report_measures_against_the_installed_licence(
    session: AsyncSession,
    settings: Settings,
    issuer: Ed25519PrivateKey,
    tmp_path: Path,
    licence_type: str,
    n_slots: int,
    expect_over_cap: bool,
) -> None:
    """Slice 2's payoff: the report's licence block and cap are real now.

    Runs for both licence types — CSL is judged on peak concurrency, FAL
    on assignments held, and the other type's figures stay observations.
    """
    path = write_licence(
        tmp_path / f"{licence_type}.json",
        issuer_private=issuer,
        licence_type=licence_type,
        n_slots=n_slots,
    )
    active = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    await _seed_chain(session, active)
    await _seed_people(session, count=2)
    resolution = resolve_licence(active)
    issued = report_identity(resolution)
    assert issued is not None
    await adopt_identity(session, issued, now=utcnow())
    await install_licence(
        session, settings=active, loaded=resolution.loaded, actor="user:1"
    )

    generated = await generate_report(
        session,
        settings=active,
        issued=issued,
        actor="cli",
        licence=licence_snapshot(resolution),
    )

    document = generated.document
    assert document["licence"]["state"] == "active"
    assert document["licence"]["licence_id"] == "LIC-TEST-0001"
    assert document["licence"]["licence_type"] == licence_type
    assert document["usage"]["licence_type"] == licence_type
    assert document["usage"][licence_type]["n_slots"] == n_slots
    assert document["usage"][licence_type]["over_cap"] is expect_over_cap
    other = "fal" if licence_type == "csl" else "csl"
    assert document["usage"][other]["n_slots"] is None

    verdict = verify_report(document, ledger=[], public_key_hex=issued.public_key_hex())
    assert next(c.status for c in verdict.checks if c.name == "signature") == "ok"
    assert bool([f for f in verdict.findings if f.severity == "breach"]) is expect_over_cap


@pytest.mark.asyncio
async def test_expired_licence_still_reports_and_says_so(
    session: AsyncSession, settings: Settings, issuer: Ed25519PrivateKey, tmp_path: Path
) -> None:
    """Expiry is a state, not a failure — the report carries it verbatim."""
    past = (utcnow() - _dt.timedelta(days=400)).isoformat().replace("+00:00", "Z")
    path = write_licence(
        tmp_path / "expired.json",
        issuer_private=issuer,
        issued_at="2020-01-01T00:00:00Z",
        expires_at=past,
        grace_period_days=30,
    )
    expired = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    await _seed_chain(session, expired)
    resolution = resolve_licence(expired)
    assert resolution.status is not None
    assert resolution.status.state == "expired"
    assert resolution.read_only is True

    issued = report_identity(resolution)
    assert issued is not None
    await adopt_identity(session, issued, now=utcnow())
    generated = await generate_report(
        session,
        settings=expired,
        issued=issued,
        actor="cli",
        licence=licence_snapshot(resolution),
    )
    assert generated.document["licence"]["state"] == "expired"


# ----- install binding (ADR-0019 §A3, Gap 2) --------------------------


async def _adopt(session: AsyncSession, tmp_path: Path, issuer: Ed25519PrivateKey,
                 install_id: str) -> None:
    """Adopt an identity so there is something to bind against."""
    from matofx.compliance import load_identity_file  # noqa: PLC0415
    from tests.compliance.conftest import write_identity_file  # noqa: PLC0415
    from tests.licence.conftest import ISSUER_KEY_ID, public_hex  # noqa: PLC0415

    path = write_identity_file(
        tmp_path / f"identity-{install_id}.json",
        issuer_key_id=ISSUER_KEY_ID,
        issuer_private=issuer,
        install_id=install_id,
    )
    issued = load_identity_file(
        path, extra_issuer_keys={ISSUER_KEY_ID: public_hex(issuer)}
    )
    await adopt_identity(session, issued, now=_dt.datetime.now(tz=_dt.UTC))


@pytest.mark.asyncio
async def test_a_licence_for_another_install_is_refused(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """Copying the licence file to a second install must not just work.

    Before this check existed, nothing compared the manifest's
    ``install_id`` against the install running it — so a copied licence
    started a second deployment with the same slots and features, and the
    only trace was a report-counter collision that needs *both* installs
    to eventually report.
    """
    await ensure_chain_head(session)
    await _adopt(session, tmp_path, issuer, "inst_REAL0001")

    path = write_licence(
        tmp_path / "copied.json", issuer_private=issuer, install_id="inst_OTHER999"
    )
    licensed = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    resolution = resolve_licence(licensed)
    assert resolution.status is not None, "the licence itself is valid — only its owner is wrong"

    with pytest.raises(LicenceInvalidError, match="was issued for install"):
        await verify_install_binding(session, resolution)


@pytest.mark.asyncio
async def test_the_matching_licence_is_accepted(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    await ensure_chain_head(session)
    await _adopt(session, tmp_path, issuer, "inst_REAL0001")

    path = write_licence(
        tmp_path / "ours.json", issuer_private=issuer, install_id="inst_REAL0001"
    )
    licensed = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    await verify_install_binding(session, resolve_licence(licensed))  # must not raise


@pytest.mark.asyncio
async def test_a_fresh_install_mints_its_own_id_and_refuses_a_foreign_licence(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """The aim: a second deployment cannot be stood up from a copied licence.

    A fresh install used to have *no* identity, so binding was skipped and
    a copied licence simply worked. Now it mints its own id at first
    boot, which no licence can name unless Funis issued one for it.
    """
    from matofx.compliance.identity import ensure_install_identity  # noqa: PLC0415

    await ensure_chain_head(session)
    identity = await ensure_install_identity(session)
    assert identity.install_id.startswith("inst_")
    assert identity.self_generated is True
    assert identity.customer is None, "nothing but a licence can say who the customer is"

    path = write_licence(
        tmp_path / "someone-elses.json", issuer_private=issuer, install_id="inst_OTHER999"
    )
    licensed = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    with pytest.raises(LicenceInvalidError, match="was issued for install"):
        await verify_install_binding(session, resolve_licence(licensed))


@pytest.mark.asyncio
async def test_the_id_is_stable_across_calls(session: AsyncSession) -> None:
    """Minting must happen once. A changing id would invalidate the licence."""
    from matofx.compliance.identity import ensure_install_identity  # noqa: PLC0415

    first = (await ensure_install_identity(session)).install_id
    second = (await ensure_install_identity(session)).install_id
    assert first == second


@pytest.mark.asyncio
async def test_the_go_live_licence_keeps_the_setup_install_id(
    session: AsyncSession, settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """Setup licence then go-live licence, same install: must stay working.

    The issuing rule this pins: a customer's second and later licences
    must carry the ``install_id`` established by their first. If Funis
    issued a fresh one at go-live the site would refuse, at the worst
    possible moment.
    """
    await ensure_chain_head(session)
    await _adopt(session, tmp_path, issuer, "inst_CUST0001")

    setup = write_licence(
        tmp_path / "setup.json", issuer_private=issuer,
        install_id="inst_CUST0001", expires_at="2027-01-14T00:00:00Z",
    )
    live = write_licence(
        tmp_path / "live.json", issuer_private=issuer,
        install_id="inst_CUST0001", expires_at="2099-01-14T00:00:00Z", n_slots=50,
    )
    for path in (setup, live):
        licensed = settings.model_copy(update={"licence": LicenceSettings(file=path)})
        await verify_install_binding(session, resolve_licence(licensed))
