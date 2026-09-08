"""The slot gate through the real login endpoint (ADR-0019 slice 4).

`tests/licence/test_slots.py` covers the decision logic directly. These
go through `POST /api/auth/login` and the admin endpoints, because the
wiring is where this could still be wrong in ways the unit tests can't
see: a refusal that leaves a session behind, a 401 where a 503 belongs,
or an admin who can sign themselves out of the page they're fixing the
licence from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from matofx.api.app import make_app
from matofx.compliance.canonical import canonical_bytes
from matofx.config import ComplianceSettings, LicenceSettings, Settings
from matofx.users.orm import AuthSession

_ISSUER_KEY_ID = "funis-test-2026"
_HTTP_OK = 200
_HTTP_NO_CONTENT = 204
_HTTP_BAD_REQUEST = 400
_HTTP_UNAVAILABLE = 503


def _write_csl_licence(path: Path, issuer: Ed25519PrivateKey, *, n_slots: int) -> Path:
    import json  # noqa: PLC0415

    payload: dict[str, Any] = {
        "schema_version": 1,
        "licence_id": "LIC-API-0001",
        "customer": "Northwind Aerospace Ltd",
        "environment": "test",
        "licence_type": "csl",
        "n_slots": n_slots,
        "issued_at": "2026-01-01T00:00:00Z",
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


def _licensed_app(
    settings: Settings,
    path: Path,
    issuer: Ed25519PrivateKey,
    *,
    factory: Any,
    audit: Any,
) -> FastAPI:
    """The standard test app, but built against a real CSL licence.

    Mirrors the ``app`` fixture's manual state wiring — the lifespan
    never runs under the ASGI test transport, so ``state.factory`` /
    ``state.audit`` have to be set by hand.
    """
    from matofx.api.deps import get_audit, get_factory, get_settings  # noqa: PLC0415

    public = issuer.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    ).hex()
    licensed = settings.model_copy(
        update={
            "licence": LicenceSettings(file=path),
            "compliance": ComplianceSettings(issuer_public_keys={_ISSUER_KEY_ID: public}),
        }
    )
    application = make_app(licensed)
    application.state.settings = licensed
    application.state.factory = factory
    application.state.audit = audit
    application.dependency_overrides[get_settings] = lambda: licensed
    application.dependency_overrides[get_factory] = lambda: factory
    application.dependency_overrides[get_audit] = lambda: audit
    return application


async def _login(app: FastAPI, username: str, password: str) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )


@pytest.fixture
def issuer() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.mark.asyncio
async def test_login_under_the_cap_reports_capacity(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    app = _licensed_app(
        api_settings,
        _write_csl_licence(tmp_path / "l.json", issuer, n_slots=5),
        issuer,
        factory=api_factory,
        audit=api_audit,
    )
    response = await _login(app, seeded["username"], seeded["password"])

    assert response.status_code == _HTTP_OK
    body = response.json()
    assert body["n_slots"] == 5
    assert body["slots_in_use"] == 1
    assert body["breakglass"] is False


@pytest.mark.asyncio
async def test_super_user_over_the_cap_is_flagged_breakglass(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """The seeded account is a super user, so it gets in and is told so."""
    app = _licensed_app(
        api_settings,
        _write_csl_licence(tmp_path / "l.json", issuer, n_slots=1),
        issuer,
        factory=api_factory,
        audit=api_audit,
    )

    first = await _login(app, seeded["username"], seeded["password"])
    assert first.status_code == _HTTP_OK
    assert first.json()["breakglass"] is False

    second = await _login(app, seeded["username"], seeded["password"])
    assert second.status_code == _HTTP_OK
    assert second.json()["breakglass"] is True


@pytest.mark.asyncio
async def test_regular_user_at_the_cap_gets_503_and_no_session(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """A refusal must be a capacity error and must leave nothing behind."""
    from matofx.db import session_scope  # noqa: PLC0415

    app = _licensed_app(
        api_settings,
        _write_csl_licence(tmp_path / "l.json", issuer, n_slots=1),
        issuer,
        factory=api_factory,
        audit=api_audit,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        token = (
            await client.post(
                "/api/auth/login",
                json={"username": seeded["username"], "password": seeded["password"]},
            )
        ).json()["access_token"]
        created = await client.post(
            "/api/admin/users",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "username": "engineer",
                "email": "engineer@example.com",
                "first_name": "En",
                "last_name": "Gineer",
                "password": "Engineerpass1!",
                "role": "regular_user",
            },
        )
        assert created.status_code in (200, 201)

        async with session_scope(api_factory) as db:
            before = int(
                (await db.execute(sa.select(sa.func.count(AuthSession.id)))).scalar_one()
            )

        refused = await client.post(
            "/api/auth/login", json={"username": "engineer", "password": "Engineerpass1!"}
        )

    assert refused.status_code == _HTTP_UNAVAILABLE
    detail = refused.json()["detail"]
    assert "places are in use" in detail
    async with session_scope(api_factory) as db:
        after = int((await db.execute(sa.select(sa.func.count(AuthSession.id)))).scalar_one())
    assert after == before, "a refused login must not create a session row"


@pytest.mark.asyncio
async def test_unlicensed_install_reports_no_cap(
    app: FastAPI, seeded: dict[str, Any]
) -> None:
    """The default test app has no licence — logins stay unmetered."""
    response = await _login(app, seeded["username"], seeded["password"])
    body = response.json()
    assert response.status_code == _HTTP_OK
    assert body["n_slots"] is None
    assert body["slots_in_use"] is None
    assert body["breakglass"] is False


@pytest.mark.asyncio
async def test_admin_can_list_and_revoke_sessions(
    app: FastAPI, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        # A second session to revoke (the first is the caller's).
        other = await client.post(
            "/api/auth/login",
            json={"username": seeded["username"], "password": seeded["password"]},
        )
        assert other.status_code == _HTTP_OK

        listing = await client.get("/api/admin/sessions", headers=auth_headers)
        assert listing.status_code == _HTTP_OK
        body = listing.json()
        assert body["metered"] is False  # no licence in the default test app
        assert body["in_use"] >= 2
        target = body["sessions"][0]["id"]

        revoked = await client.post(
            f"/api/admin/sessions/{target}/revoke", headers=auth_headers
        )
        assert revoked.status_code == _HTTP_NO_CONTENT

        again = await client.post(
            f"/api/admin/sessions/{target}/revoke", headers=auth_headers
        )
        assert again.status_code == 404


@pytest.mark.asyncio
async def test_revoking_a_missing_session_is_a_clear_404(
    app: FastAPI, auth_headers: dict[str, str]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post("/api/admin/sessions/999999/revoke", headers=auth_headers)
    assert response.status_code == 404
    assert "already ended" in response.json()["detail"]


# ----- FAL assignments (slice 5) --------------------------------------


def _write_fal_licence(path: Path, issuer: Ed25519PrivateKey, *, n_slots: int) -> Path:
    import json  # noqa: PLC0415

    payload: dict[str, Any] = {
        "schema_version": 1,
        "licence_id": "LIC-API-FAL",
        "customer": "Northwind Aerospace Ltd",
        "environment": "test",
        "licence_type": "fal",
        "n_slots": n_slots,
        "issued_at": "2026-01-01T00:00:00Z",
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


def _new_user_body(username: str) -> dict[str, Any]:
    return {
        "username": username,
        "email": f"{username}@example.com",
        "first_name": "En",
        "last_name": "Gineer",
        "password": "Engineerpass1!",
        "role": "regular_user",
    }


@pytest.mark.asyncio
async def test_fal_cap_refuses_a_new_user_and_frees_on_deactivate(
    api_settings: Settings,
    seeded: dict[str, Any],
    api_factory: Any,
    api_audit: Any,
    tmp_path: Path,
    issuer: Ed25519PrivateKey,
) -> None:
    """The whole FAL round trip: fill the cap, get refused, free one, succeed.

    ``n_slots=2`` and the seeded super-user already holds one, so exactly
    one more account fits.
    """
    app = _licensed_app(
        api_settings,
        _write_fal_licence(tmp_path / "fal.json", issuer, n_slots=2),
        issuer,
        factory=api_factory,
        audit=api_audit,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        token = (
            await client.post(
                "/api/auth/login",
                json={"username": seeded["username"], "password": seeded["password"]},
            )
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        capacity = (await client.get("/api/admin/licence/capacity", headers=headers)).json()
        assert capacity["metered"] is True
        assert capacity["n_slots"] == 2
        assert capacity["remaining"] == 1

        first = await client.post("/api/admin/users", headers=headers, json=_new_user_body("eng1"))
        assert first.status_code in (200, 201)

        # Cap reached — the next grant is refused, and must not create it.
        refused = await client.post(
            "/api/admin/users", headers=headers, json=_new_user_body("eng2")
        )
        assert refused.status_code == 409
        detail = refused.json()["detail"]
        assert "Remove access from someone" in detail
        listing = (await client.get("/api/admin/users", headers=headers)).json()
        assert not any(u["username"] == "eng2" for u in listing)

        # Free a place, and the same request now succeeds.
        eng1_id = next(u["id"] for u in listing if u["username"] == "eng1")
        freed = await client.post(f"/api/admin/users/{eng1_id}/deactivate", headers=headers)
        assert freed.status_code == 200
        after = (await client.get("/api/admin/licence/capacity", headers=headers)).json()
        assert after["remaining"] == 1

        allowed = await client.post(
            "/api/admin/users", headers=headers, json=_new_user_body("eng2")
        )
        assert allowed.status_code in (200, 201)

        # And reactivating the first one is now refused — the cap is full again.
        blocked = await client.post(f"/api/admin/users/{eng1_id}/reactivate", headers=headers)
        assert blocked.status_code == 409


@pytest.mark.asyncio
async def test_capacity_is_unmetered_without_a_fal_licence(
    app: FastAPI, auth_headers: dict[str, str]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        body = (await client.get("/api/admin/licence/capacity", headers=auth_headers)).json()
    assert body["metered"] is False
    assert body["n_slots"] is None
    assert body["in_use"] >= 1


@pytest.mark.asyncio
async def test_admin_cannot_revoke_their_own_session(
    app: FastAPI, auth_headers: dict[str, str]
) -> None:
    """The self-revoke guard, which silently did nothing until fixed.

    ``request.state.session_id`` is set by ``get_current_user`` — the
    only place that resolves a bearer token to a row. Without it the
    guard compared against ``None``, so an administrator could sign
    themselves out of the very page they opened to rescue a full
    licence, and the listing offered them the button to do it.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        listing = (await client.get("/api/admin/sessions", headers=auth_headers)).json()
        mine = [s for s in listing["sessions"] if s["is_you"]]
        assert len(mine) == 1, "exactly one row must be marked as the caller's own"

        refused = await client.post(
            f"/api/admin/sessions/{mine[0]['id']}/revoke", headers=auth_headers
        )

    assert refused.status_code == _HTTP_BAD_REQUEST
    assert "the session you're using" in refused.json()["detail"]
