"""Read-only mode: the middleware, and the two things that trigger it.

ADR-0019 slice 6. The rules under test are the maintainer's (§C12), and
they are narrower than "block all writes":

- every already-registered user may sign in and **view results**;
- no new registration, no access-rights changes, no submissions, no edits;
- work already running finishes (the runner isn't gated here at all).

The middleware fails **closed** by design, so the allow-list is the
risky part: each entry is something that must keep working, and a test
below pins the ones that would strand a user or an administrator.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from matofx.api.app import make_app
from matofx.audit import AuditWriter, VerifyFailure, VerifyResult
from matofx.compliance.canonical import canonical_bytes
from matofx.compliance.integrity import acknowledge, current_status, record_verify_result
from matofx.config import ComplianceSettings, LicenceSettings, Settings
from matofx.db import session_scope

_ISSUER_KEY_ID = "funis-test-2026"
_OK = 200
_NO_CONTENT = 204
_FORBIDDEN = 403
_UNAVAILABLE = 503
_UNSIGNED_AUDIT = AuditWriter(signing_key=None, key_id=None)


@pytest.fixture
def issuer() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _write_licence(path: Path, issuer: Ed25519PrivateKey, *, expires_at: str) -> Path:
    import json  # noqa: PLC0415

    payload: dict[str, Any] = {
        "schema_version": 1,
        "licence_id": "LIC-RO-0001",
        "customer": "Northwind Aerospace Ltd",
        "environment": "test",
        "licence_type": "csl",
        "n_slots": 50,
        "issued_at": "2020-01-01T00:00:00Z",
        "expires_at": expires_at,
        "grace_period_days": 30,
        "features": {},
    }
    document = {
        "licence": payload,
        "issuer": {
            "key_id": _ISSUER_KEY_ID,
            "alg": "ed25519",
            "signature": issuer.sign(canonical_bytes({"licence": payload})).hex(),
        },
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def _app_with(
    settings: Settings, *, factory: Any, audit: Any, licence: Path | None, issuer: Ed25519PrivateKey
) -> FastAPI:
    from matofx.api.deps import get_audit, get_factory, get_settings  # noqa: PLC0415

    public = (
        issuer.public_key()
        .public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
        .hex()
    )
    updated = settings.model_copy(
        update={
            "licence": LicenceSettings(file=licence),
            "compliance": ComplianceSettings(issuer_public_keys={_ISSUER_KEY_ID: public}),
        }
    )
    application = make_app(updated)
    application.state.settings = updated
    application.state.factory = factory
    application.state.audit = audit
    application.dependency_overrides[get_settings] = lambda: updated
    application.dependency_overrides[get_factory] = lambda: factory
    application.dependency_overrides[get_audit] = lambda: audit
    return application


def _expired_licence(tmp_path: Path, issuer: Ed25519PrivateKey) -> Path:
    past = (_dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(days=400)).isoformat()
    return _write_licence(tmp_path / "expired.json", issuer, expires_at=past.replace("+00:00", "Z"))


async def _sign_in(client: AsyncClient, seeded: dict[str, Any]) -> dict[str, str]:
    response = await client.post(
        "/api/auth/login", json={"username": seeded["username"], "password": seeded["password"]}
    )
    assert response.status_code == _OK, "signing in must keep working in read-only mode"
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# ----- licence-driven read-only ---------------------------------------


@pytest.mark.asyncio
async def test_expired_licence_blocks_writes_but_not_reads(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """The §C12 shape: sign in, read everything, write nothing."""
    app = _app_with(
        api_settings,
        factory=api_factory,
        audit=api_audit,
        licence=_expired_licence(tmp_path, issuer),
        issuer=issuer,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)

        # Reads are untouched.
        assert (await client.get("/api/admin/users", headers=headers)).status_code == _OK
        assert (await client.get("/api/projects", headers=headers)).status_code == _OK

        # The site says so, in a machine-readable way.
        mode = (await client.get("/api/site/mode", headers=headers)).json()
        assert mode["read_only"] is True
        assert mode["reason"] == "licence"
        assert mode["licence_state"] == "expired"
        assert "read-only" in (mode["banner"] or "")

        # Writes are refused with the reason attached.
        blocked = await client.post("/api/projects", headers=headers, json={"name": "New project"})
        assert blocked.status_code == _UNAVAILABLE
        assert blocked.json()["read_only_reason"] == "licence"

        # No new registrations (§C12).
        refused = await client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "username": "newcomer",
                "email": "newcomer@example.com",
                "first_name": "New",
                "last_name": "Comer",
                "password": "Newcomerpass1!",
                "role": "regular_user",
            },
        )
        assert refused.status_code == _UNAVAILABLE


@pytest.mark.asyncio
async def test_readonly_allows_the_ways_out(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """Logout, licence install and session revoke must survive read-only.

    Each of these is on the allow-list because blocking it would strand
    somebody: a user unable to sign out (holding a licence place), or an
    administrator unable to install the very licence that would lift the
    mode.
    """
    app = _app_with(
        api_settings,
        factory=api_factory,
        audit=api_audit,
        licence=_expired_licence(tmp_path, issuer),
        issuer=issuer,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)

        # Freeing a licence place still works.
        sessions = (await client.get("/api/admin/sessions", headers=headers)).json()
        other = [s for s in sessions["sessions"] if not s["is_you"]]
        if other:
            revoked = await client.post(
                f"/api/admin/sessions/{other[0]['id']}/revoke", headers=headers
            )
            assert revoked.status_code == _NO_CONTENT

        # Personal display preferences are view state, not governed data.
        theme = await client.post("/api/auth/me/theme", headers=headers, json={"theme": "light"})
        assert theme.status_code == _OK

        # And signing out is never blocked.
        assert (await client.post("/api/auth/logout", headers=headers)).status_code == _NO_CONTENT


@pytest.mark.asyncio
async def test_valid_licence_leaves_the_site_writable(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    app = _app_with(
        api_settings,
        factory=api_factory,
        audit=api_audit,
        licence=_write_licence(tmp_path / "ok.json", issuer, expires_at="2099-01-01T00:00:00Z"),
        issuer=issuer,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)
        mode = (await client.get("/api/site/mode", headers=headers)).json()
        assert mode["read_only"] is False
        assert mode["licence_state"] == "active"
        created = await client.post(
            "/api/projects", headers=headers, json={"name": "Writable project"}
        )
        assert created.status_code in (200, 201)


@pytest.mark.asyncio
async def test_a_never_licensed_install_stays_writable(
    app: FastAPI, auth_headers: dict[str, str]
) -> None:
    """No ``[licence].file`` at all = a dev checkout, not a lapsed customer.

    Without this exemption the middleware would make every developer
    checkout, CI run and test harness read-only out of the box. The
    loophole is closed from the other end: the config model refuses to
    load ``environment = "prod"`` without a licence path, so a customer
    site can't quietly run unlicensed for ever.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        mode = (await client.get("/api/site/mode", headers=auth_headers)).json()
        assert mode["read_only"] is False
        created = await client.post(
            "/api/projects", headers=auth_headers, json={"name": "Dev project"}
        )
    assert created.status_code in (200, 201)


def test_prod_refuses_to_load_without_a_licence(tmp_path: Path) -> None:
    """The other end of the dev exemption, asserted where it's enforced.

    Without this, "never configure [licence].file" would be a way to run
    a customer site unlicensed for ever.
    """
    from pydantic import ValidationError  # noqa: PLC0415

    from matofx.config import AuditSettings  # noqa: PLC0415

    keyfile = tmp_path / "key.pem"
    keyfile.write_text("placeholder", encoding="utf-8")
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            environment="prod",
            audit=AuditSettings(signing_key_path=keyfile, require_signing=True),
        )
    assert "licence.file is not set" in str(exc_info.value)


# ----- integrity-driven read-only -------------------------------------


@pytest.mark.asyncio
async def test_failed_chain_check_blocks_writes_until_acknowledged(
    api_factory: Any,
) -> None:
    """The §C14 path, end to end at the service level."""
    failure = VerifyResult(
        ok=False,
        records_checked=120,
        failures=[VerifyFailure(record_id=84, reason="hash mismatch: stored ab…, computed cd…")],
    )
    async with session_scope(api_factory) as db:
        status = await record_verify_result(db, result=failure, audit=_UNSIGNED_AUDIT)
        assert status.read_only is True
        assert "Record 84" in (status.detail or "")
        assert "read-only" in (status.banner or "")

        status = await acknowledge(
            db, audit=_UNSIGNED_AUDIT, actor="user:1", note="Restored from the 03:00 backup."
        )
        assert status.read_only is False
        assert status.acknowledged is True
        assert status.ok is False  # the failure stands in the record


@pytest.mark.asyncio
async def test_a_fresh_failure_clears_a_previous_acknowledgement(
    api_factory: Any,
) -> None:
    """Acknowledging one problem must not silently absolve the next."""
    first = VerifyResult(
        ok=False, records_checked=10, failures=[VerifyFailure(record_id=3, reason="bad link")]
    )
    async with session_scope(api_factory) as db:
        await record_verify_result(db, result=first, audit=_UNSIGNED_AUDIT)
        await acknowledge(db, audit=_UNSIGNED_AUDIT, actor="user:1", note="Known disk fault.")
        assert (await current_status(db)).read_only is False

        # It recovers…
        await record_verify_result(
            db, result=VerifyResult(ok=True, records_checked=12), audit=_UNSIGNED_AUDIT
        )
        assert (await current_status(db)).ok is True

        # …then fails again: read-only returns, unacknowledged.
        second = VerifyResult(
            ok=False, records_checked=14, failures=[VerifyFailure(record_id=9, reason="again")]
        )
        status = await record_verify_result(db, result=second, audit=_UNSIGNED_AUDIT)
        assert status.read_only is True
        assert status.acknowledged is False


@pytest.mark.asyncio
async def test_only_a_super_user_can_acknowledge(
    app: FastAPI, seeded: dict[str, Any], api_factory: Any
) -> None:
    """The one action that sets aside tamper-evidence is role-gated."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        admin_headers = await _sign_in(client, seeded)
        # Create the second admin *first*: once the integrity check has
        # failed, creating users is (correctly) blocked by read-only.
        created = await client.post(
            "/api/admin/users",
            headers=admin_headers,
            json={
                "username": "plainadmin",
                "email": "plainadmin@example.com",
                "first_name": "Plain",
                "last_name": "Admin",
                "password": "Plainadmin1!",
                "role": "administrator",
            },
        )
        assert created.status_code in (200, 201)
        admin_token = (
            await client.post(
                "/api/auth/login",
                json={"username": "plainadmin", "password": "Plainadmin1!"},
            )
        ).json()["access_token"]

        failure = VerifyResult(
            ok=False, records_checked=5, failures=[VerifyFailure(record_id=2, reason="broken")]
        )
        async with session_scope(api_factory) as db:
            await record_verify_result(db, result=failure, audit=_UNSIGNED_AUDIT)

        refused = await client.post(
            "/api/admin/integrity/acknowledge",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"note": "I would like to clear this please."},
        )
        assert refused.status_code == _FORBIDDEN
        assert "Super User" in refused.json()["detail"]

        allowed = await client.post(
            "/api/admin/integrity/acknowledge",
            headers=admin_headers,
            json={"note": "Restored from backup after the disk fault."},
        )
        assert allowed.status_code == _OK
        assert allowed.json()["integrity_acknowledged"] is True


@pytest.mark.asyncio
async def test_acknowledgement_note_is_required(app: FastAPI, auth_headers: dict[str, str]) -> None:
    """A one-word acknowledgement is worthless in a compliance report."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            "/api/admin/integrity/acknowledge", headers=auth_headers, json={"note": "ok"}
        )
    assert response.status_code == 422


# ----- a licence issued for another install (§A3, stress-test 2026-08-12) ---


def _licence_for_install(
    path: Path, issuer: Ed25519PrivateKey, *, install_id: str, n_slots: int
) -> Path:
    """A valid, correctly-signed licence naming ``install_id``."""
    import json  # noqa: PLC0415

    payload: dict[str, Any] = {
        "schema_version": 1,
        "licence_id": f"LIC-{install_id}",
        "customer": "Copycat Dynamics Ltd",
        "environment": "test",
        "install_id": install_id,
        "licence_type": "csl",
        "n_slots": n_slots,
        "issued_at": "2020-01-01T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
        "grace_period_days": 30,
        "features": {},
    }
    document = {
        "licence": payload,
        "issuer": {
            "key_id": _ISSUER_KEY_ID,
            "alg": "ed25519",
            "signature": issuer.sign(canonical_bytes({"licence": payload})).hex(),
        },
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_a_licence_for_another_install_blocks_writes_at_runtime(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """Copying another install's licence in must not grant its entitlements.

    `verify_install_binding` (§16.57) enforces §A3's one-licence-one-
    install rule, but only at API startup and on the runner's hourly
    tick. Neither is the request path, so dropping a validly-signed
    foreign licence over the configured file on a **running** server
    applied its slots and features until somebody restarted — a 10-slot
    install served 50 the moment the file changed. A multi-install
    customer already holds both files.

    The write gate now compares the manifest's ``install_id`` against
    the id cached at startup.
    """
    licence = _licence_for_install(
        tmp_path / "foreign.json", issuer, install_id="inst_SOMEONEELSE", n_slots=50
    )
    app = _app_with(
        api_settings, factory=api_factory, audit=api_audit, licence=licence, issuer=issuer
    )
    app.state.install_id = "inst_THISONE"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)

        blocked = await client.post("/api/projects", headers=headers, json={"name": "Nope"})
        assert blocked.status_code == _UNAVAILABLE
        assert blocked.json()["read_only_reason"] == "licence_invalid"

        mode = (await client.get("/api/site/mode", headers=headers)).json()
        assert mode["read_only"] is True
        assert "different MATOFX install" in (mode["banner"] or "")


@pytest.mark.asyncio
async def test_this_installs_own_licence_is_not_refused(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """The false-positive case, asserted as deliberately as the attack.

    A check that accuses needs its honest case pinned — otherwise a
    licence naming this very install, or a legacy licence naming none at
    all, could start being refused and nothing would notice.
    """
    mine = _licence_for_install(
        tmp_path / "mine.json", issuer, install_id="inst_THISONE", n_slots=50
    )
    app = _app_with(api_settings, factory=api_factory, audit=api_audit, licence=mine, issuer=issuer)
    app.state.install_id = "inst_THISONE"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)
        created = await client.post("/api/projects", headers=headers, json={"name": "Fine"})
        assert created.status_code in (200, 201)


@pytest.mark.asyncio
async def test_a_licence_naming_no_install_is_not_refused(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """A manifest predating install binding must keep working."""
    legacy = _write_licence(tmp_path / "legacy.json", issuer, expires_at="2099-01-01T00:00:00Z")
    app = _app_with(
        api_settings, factory=api_factory, audit=api_audit, licence=legacy, issuer=issuer
    )
    app.state.install_id = "inst_THISONE"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)
        created = await client.post("/api/projects", headers=headers, json={"name": "Legacy ok"})
        assert created.status_code in (200, 201)
