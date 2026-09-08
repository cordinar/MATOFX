"""The admin compliance-report surface (ADR-0019 §B7, overview gap 5).

Until this shipped, producing the artefact a supplier asks for required
shell access to the server. The tests that matter here are less about
happy-path plumbing than about three properties the ADR is explicit
about, each of which is easy to break later without noticing:

* **Generation never fails closed** (§B9). A report recording bad news
  must still be produced — refusing at exactly the moment something looks
  wrong destroys the evidence needed to explain it.
* **It keeps working in read-only mode.** The two conditions that trigger
  read-only are the two a supplier is most likely to ask about, so a
  gate here would withhold the evidence precisely when it is wanted.
* **Downloads come from the archive**, byte-for-byte as signed, not
  regenerated.
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
from matofx.config import ComplianceSettings, LicenceSettings, Settings
from tests.compliance.conftest import issuer_public_hex, write_identity_file

_ISSUER_KEY_ID = "funis-test-2026"
_OK = 200
_CREATED = 201
_NOT_FOUND = 404
_CONFLICT = 409
_UNAVAILABLE = 503

pytestmark = pytest.mark.asyncio


@pytest.fixture
def issuer() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _app_with(
    settings: Settings,
    *,
    factory: Any,
    audit: Any,
    issuer: Ed25519PrivateKey,
    identity: Path | None,
    licence: Path | None = None,
) -> FastAPI:
    from matofx.api.deps import get_audit, get_factory, get_settings  # noqa: PLC0415

    updated = settings.model_copy(
        update={
            "licence": LicenceSettings(file=licence),
            "compliance": ComplianceSettings(
                identity_file=identity,
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


def _identity(tmp_path: Path, issuer: Ed25519PrivateKey) -> Path:
    return write_identity_file(
        tmp_path / "funis-install-identity.json",
        issuer_key_id=_ISSUER_KEY_ID,
        issuer_private=issuer,
    )


def _expired_licence(tmp_path: Path, issuer: Ed25519PrivateKey) -> Path:
    """A licence far enough past grace to put the site in read-only."""
    past = (_dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(days=400)).isoformat()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "licence_id": "LIC-CR-0001",
        "customer": "Northwind Aerospace Ltd",
        "environment": "test",
        "licence_type": "csl",
        "n_slots": 50,
        "issued_at": "2020-01-01T00:00:00Z",
        "expires_at": past.replace("+00:00", "Z"),
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
    path = tmp_path / "expired-licence.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


async def _sign_in(client: AsyncClient, seeded: dict[str, Any]) -> dict[str, str]:
    response = await client.post(
        "/api/auth/login",
        json={"username": seeded["username"], "password": seeded["password"]},
    )
    assert response.status_code == _OK
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# ----- the ordinary path ----------------------------------------------


async def test_generate_then_list_and_download(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """Generate a report, see it listed, download both halves of the pair."""
    app = _app_with(
        api_settings,
        factory=api_factory,
        audit=api_audit,
        issuer=issuer,
        identity=_identity(tmp_path, issuer),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)

        empty = await client.get("/api/admin/compliance/reports", headers=headers)
        assert empty.status_code == _OK
        assert empty.json()["reports"] == []
        assert empty.json()["can_generate"] is True

        created = await client.post(
            "/api/admin/compliance/reports", headers=headers, json={"since": None}
        )
        assert created.status_code == _CREATED, created.text
        body = created.json()
        assert body["counter"] == 1
        assert len(body["content_sha256"]) == 64

        listed = (await client.get("/api/admin/compliance/reports", headers=headers)).json()
        assert [row["counter"] for row in listed["reports"]] == [1]
        # The install names *itself* (§16.57): the id is minted locally,
        # not taken from the identity file, which is why this is not
        # `inst_TEST0001`. Asserting the shape rather than the value
        # pins the inversion instead of quietly depending on the file.
        assert str(listed["install_id"]).startswith("inst_")

        # Both halves of the pair, and the JSON is the archived document.
        as_json = await client.get(
            "/api/admin/compliance/reports/1/document", headers=headers, params={"fmt": "json"}
        )
        assert as_json.status_code == _OK
        assert "attachment" in as_json.headers["content-disposition"]
        assert json.loads(as_json.text)["counter"] == 1

        as_html = await client.get(
            "/api/admin/compliance/reports/1/document", headers=headers, params={"fmt": "html"}
        )
        assert as_html.status_code == _OK
        assert as_html.headers["content-type"].startswith("text/html")
        assert "<html" in as_html.text.lower()


async def test_the_counter_advances_across_reports(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """Newest first, and the counter only ever goes up."""
    app = _app_with(
        api_settings,
        factory=api_factory,
        audit=api_audit,
        issuer=issuer,
        identity=_identity(tmp_path, issuer),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)
        for _ in range(3):
            response = await client.post("/api/admin/compliance/reports", headers=headers, json={})
            assert response.status_code == _CREATED

        listed = (await client.get("/api/admin/compliance/reports", headers=headers)).json()
        assert [row["counter"] for row in listed["reports"]] == [3, 2, 1]


async def test_download_of_an_unknown_report_is_404(
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
        issuer=issuer,
        identity=_identity(tmp_path, issuer),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)
        missing = await client.get("/api/admin/compliance/reports/99/document", headers=headers)
        assert missing.status_code == _NOT_FOUND


# ----- the properties that are easy to break --------------------------


async def test_generation_keeps_working_while_read_only(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """The point of the exemption, asserted end to end.

    An expired licence is one of the two things that puts the site into
    read-only — and it is exactly the situation in which a supplier asks
    for a report. If the middleware ever gates this route, the customer
    cannot produce the evidence at the moment it is wanted.
    """
    app = _app_with(
        api_settings,
        factory=api_factory,
        audit=api_audit,
        issuer=issuer,
        identity=_identity(tmp_path, issuer),
        licence=_expired_licence(tmp_path, issuer),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)

        # The site really is read-only...
        mode = (await client.get("/api/site/mode", headers=headers)).json()
        assert mode["read_only"] is True
        blocked = await client.post("/api/projects", headers=headers, json={"name": "Nope"})
        assert blocked.status_code == _UNAVAILABLE

        # ...and a report is still generated, and downloadable.
        created = await client.post("/api/admin/compliance/reports", headers=headers, json={})
        assert created.status_code == _CREATED, (
            "report generation must never fail closed (ADR-0019 §B9)"
        )
        assert (
            await client.get("/api/admin/compliance/reports/1/document", headers=headers)
        ).status_code == _OK


async def test_without_a_signing_key_the_page_is_told_why(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    issuer: Ed25519PrivateKey,
) -> None:
    """No identity, no key — explain it rather than offering a dead button."""
    app = _app_with(
        api_settings,
        factory=api_factory,
        audit=api_audit,
        issuer=issuer,
        identity=None,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)

        listed = (await client.get("/api/admin/compliance/reports", headers=headers)).json()
        assert listed["can_generate"] is False
        assert "licence" in (listed["blocked_reason"] or "").lower()

        # And attempting anyway is a clean refusal, not a 500.
        refused = await client.post("/api/admin/compliance/reports", headers=headers, json={})
        assert refused.status_code == _CONFLICT


async def test_a_bad_since_date_is_a_422_not_a_crash(
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
        issuer=issuer,
        identity=_identity(tmp_path, issuer),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        headers = await _sign_in(client, seeded)
        response = await client.post(
            "/api/admin/compliance/reports", headers=headers, json={"since": "last Tuesday"}
        )
        assert response.status_code == 422
        assert "YYYY-MM-DD" in response.json()["detail"]


async def test_reports_require_authentication(
    api_settings: Settings,
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """A signed report is evidence about the install; it is not public."""
    app = _app_with(
        api_settings,
        factory=api_factory,
        audit=api_audit,
        issuer=issuer,
        identity=_identity(tmp_path, issuer),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.get("/api/admin/compliance/reports")).status_code in (401, 403)
        assert (await client.post("/api/admin/compliance/reports", json={})).status_code in (
            401,
            403,
        )
