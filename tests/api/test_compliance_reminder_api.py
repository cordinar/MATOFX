"""The reminder reaches the chrome — and only the people who can act.

``/api/site/mode`` is fetched once per page render by the chrome and is
open to every signed-in user, so it is the right carrier for the banner:
no second round-trip on every page. But that means the *permission* gate
lives here rather than on a separate admin endpoint, which is exactly
the thing worth testing.

The other property under test is that the admin page and the banner
agree. They are computed by the same function on purpose; a test that
only ever looked at one of them could not see them disagree.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from matofx.api.app import make_app
from matofx.compliance.canonical import canonical_bytes
from matofx.compliance.orm import SINGLETON_ID, ComplianceReport, InstallIdentity
from matofx.config import ComplianceSettings, LicenceSettings, Settings
from matofx.db import session_scope
from matofx.users.passwords import make_password_hasher
from matofx.users.permissions import Permission
from matofx.users.role_service import RoleService
from matofx.users.service import make_user_service
from tests.compliance.conftest import issuer_public_hex, write_identity_file

_ISSUER_KEY_ID = "funis-test-2026"
_OK = 200

pytestmark = pytest.mark.asyncio


@pytest.fixture
def issuer() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _licence(tmp_path: Path, issuer: Ed25519PrivateKey, *, months: int | None) -> Path:
    """A live licence, optionally carrying a reporting cadence."""
    future = (_dt.datetime.now(tz=_dt.UTC) + _dt.timedelta(days=400)).isoformat()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "licence_id": "LIC-CRM-0001",
        "customer": "Northwind Aerospace Ltd",
        "environment": "test",
        "licence_type": "csl",
        "n_slots": 50,
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": future.replace("+00:00", "Z"),
        "grace_period_days": 30,
        "features": {},
    }
    if months is not None:
        payload["compliance_report_months"] = months
    document = {
        "licence": payload,
        "issuer": {
            "key_id": _ISSUER_KEY_ID,
            "alg": "ed25519",
            "signature": issuer.sign(canonical_bytes({"licence": payload})).hex(),
        },
    }
    path = tmp_path / "licence.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def _app_with(
    settings: Settings,
    *,
    factory: Any,
    audit: Any,
    issuer: Ed25519PrivateKey,
    tmp_path: Path,
    months: int | None = 3,
) -> FastAPI:
    from matofx.api.deps import get_audit, get_factory, get_settings  # noqa: PLC0415

    updated = settings.model_copy(
        update={
            "licence": LicenceSettings(file=_licence(tmp_path, issuer, months=months)),
            "compliance": ComplianceSettings(
                identity_file=write_identity_file(
                    tmp_path / "funis-install-identity.json",
                    issuer_key_id=_ISSUER_KEY_ID,
                    issuer_private=issuer,
                ),
                issuer_public_keys={_ISSUER_KEY_ID: issuer_public_hex(issuer)},
            ),
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


async def _sign_in(client: AsyncClient, seeded: dict[str, Any]) -> dict[str, str]:
    response = await client.post(
        "/api/auth/login",
        json={"username": seeded["username"], "password": seeded["password"]},
    )
    assert response.status_code == _OK
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _old_report(factory: Any, *, days_ago: int, counter: int = 1) -> None:
    """Seed a report far enough back that the cadence has elapsed.

    Also puts the install's report counter at ``counter``, so a real
    ``generate`` afterwards allocates the next one rather than colliding
    with this row on the unique counter. The identity is created here if
    the install has not minted one yet — the app does that lazily, and
    ``ASGITransport`` does not run the startup lifespan.
    """
    when = _dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(days=days_ago)
    async with session_scope(factory) as session:
        identity = await session.get(InstallIdentity, SINGLETON_ID)
        if identity is None:
            session.add(
                InstallIdentity(
                    id=SINGLETON_ID,
                    install_id="inst_SEEDED00001",
                    adopted_at=when,
                    self_generated=True,
                    report_counter=counter,
                )
            )
        elif identity.report_counter < counter:
            identity.report_counter = counter
        session.add(
            ComplianceReport(
                counter=counter,
                generated_at=when,
                period_from=when - _dt.timedelta(days=90),
                period_to=when,
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


# ---- the banner reaches the chrome ----------------------------------


async def test_an_overdue_report_shows_in_site_mode(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    await _old_report(api_factory, days_ago=200)
    app = _app_with(
        api_settings, factory=api_factory, audit=api_audit, issuer=issuer, tmp_path=tmp_path
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)
        mode = (await client.get("/api/site/mode", headers=headers)).json()

    assert mode["compliance_due"] is True
    assert "compliance report" in str(mode["compliance_banner"]).lower()
    assert mode["compliance_due_at"] is not None


async def test_a_recent_report_leaves_the_chrome_quiet(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    await _old_report(api_factory, days_ago=5)
    app = _app_with(
        api_settings, factory=api_factory, audit=api_audit, issuer=issuer, tmp_path=tmp_path
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)
        mode = (await client.get("/api/site/mode", headers=headers)).json()

    assert mode["compliance_due"] is False
    assert mode["compliance_banner"] is None


async def test_a_zero_cadence_never_reminds(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """How a customer who does not want the banner is shipped."""
    await _old_report(api_factory, days_ago=2000)
    app = _app_with(
        api_settings,
        factory=api_factory,
        audit=api_audit,
        issuer=issuer,
        tmp_path=tmp_path,
        months=0,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)
        mode = (await client.get("/api/site/mode", headers=headers)).json()

    assert mode["compliance_due"] is False


# ---- the permission gate --------------------------------------------


async def test_a_user_without_audit_read_is_not_reminded(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """Telling someone a report is overdue when the page is closed to
    them is a dead end, not a prompt."""
    await _old_report(api_factory, days_ago=200)
    roles = RoleService(api_audit)
    async with session_scope(api_factory) as session:
        await roles.create_role(
            session,
            actor_user_id=seeded["user_id"],
            name="viewer_no_audit",
            description="A role deliberately without audit.read",
            permissions=[Permission.LIVE_JOBS_VIEW],
        )

    users = make_user_service(
        api_settings.auth, api_audit, hasher=make_password_hasher(api_settings.auth)
    )
    async with session_scope(api_factory) as session:
        await users.create_user(
            session,
            username="viewer",
            email="viewer@example.com",
            password=seeded["password"],
            full_name="View Only",
            role="viewer_no_audit",
        )

    app = _app_with(
        api_settings, factory=api_factory, audit=api_audit, issuer=issuer, tmp_path=tmp_path
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            "/api/auth/login", json={"username": "viewer", "password": seeded["password"]}
        )
        assert response.status_code == _OK
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        mode = (await client.get("/api/site/mode", headers=headers)).json()

    assert mode["compliance_due"] is False
    assert mode["compliance_banner"] is None


# ---- the two surfaces agree -----------------------------------------


async def test_the_admin_page_and_the_banner_never_disagree(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """A page saying "not due yet" under a banner saying "overdue" is
    worse than having no reminder at all."""
    await _old_report(api_factory, days_ago=200)
    app = _app_with(
        api_settings, factory=api_factory, audit=api_audit, issuer=issuer, tmp_path=tmp_path
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)
        mode = (await client.get("/api/site/mode", headers=headers)).json()
        listing = (await client.get("/api/admin/compliance/reports", headers=headers)).json()

    assert listing["reporting_due"] == mode["compliance_due"]
    assert listing["reporting_due_at"] == mode["compliance_due_at"]
    assert listing["reporting_cadence_months"] == 3


async def test_generating_a_report_clears_the_banner_for_everyone(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """No dismissal state to get stuck: it is derived from the database."""
    await _old_report(api_factory, days_ago=200)
    app = _app_with(
        api_settings, factory=api_factory, audit=api_audit, issuer=issuer, tmp_path=tmp_path
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)
        assert (await client.get("/api/site/mode", headers=headers)).json()["compliance_due"]

        created = await client.post(
            "/api/admin/compliance/reports", headers=headers, json={"since": None}
        )
        assert created.status_code == 201, created.text

        after = (await client.get("/api/site/mode", headers=headers)).json()

    assert after["compliance_due"] is False
    assert after["compliance_banner"] is None
