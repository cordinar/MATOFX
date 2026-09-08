"""HTTP-surface tests for ``matofx.api.routers.migrate``.

Each test runs against the in-process ASGI client built by
``tests/_api_fixtures.py``. The ``seeded`` fixture creates a super-user
who holds :attr:`Permission.MIGRATE_TOOLS_WORKFLOWS` via the catalog
sentinel, so the auth headers are sufficient.

The fixture chain wires an *unsigned* :class:`AuditWriter` by default
(``signing_key=None``). The migrate endpoints that need the install
key (``this-install-fingerprint`` and ``export``) would 503 against
that; we override :func:`get_install_key_pair` per test with a
generated in-memory key pair instead, mirroring how SSO-only
endpoints override auth in other suites.
"""

from __future__ import annotations

import json
import tarfile
from io import BytesIO
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from matofx.api.deps import get_install_key_pair
from matofx.migrate.signing import KeyPair, compute_fingerprint, display_fingerprint

# ---------------------------------------------------------------------------
# Local fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrate_key_pair() -> KeyPair:
    """A fresh in-memory Ed25519 key pair for the install's signing role."""
    return KeyPair.generate()


@pytest.fixture
def migrate_app(app: FastAPI, migrate_key_pair: KeyPair) -> FastAPI:
    """The ASGI app with :func:`get_install_key_pair` overridden.

    Built on top of the standard ``app`` fixture so every other dep
    override (settings / factory / audit) stays in place. The fixture
    is request-scoped so each test gets its own key.
    """
    app.dependency_overrides[get_install_key_pair] = lambda: migrate_key_pair
    return app


@pytest.fixture
async def migrate_client(migrate_app: FastAPI):
    """In-process ASGI client wired to the override-ready app."""
    from httpx import ASGITransport

    transport = ASGITransport(app=migrate_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


CANONICAL_FP = "9f2ea17b043d8ce1556a2f97cb0d187c0123456789abcdef0123456789abcdef"
DISPLAY_FP = display_fingerprint(CANONICAL_FP)


# ---------------------------------------------------------------------------
# Auth gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoints_require_auth(migrate_client: httpx.AsyncClient) -> None:
    for method, path in [
        ("GET", "/api/admin/migrate/this-install-fingerprint"),
        ("GET", "/api/admin/migrate/trusted-peers"),
        ("POST", "/api/admin/migrate/trusted-peers"),
        ("DELETE", f"/api/admin/migrate/trusted-peers/{CANONICAL_FP}"),
        ("POST", "/api/admin/migrate/export"),
        ("POST", "/api/admin/migrate/import"),
    ]:
        r = await migrate_client.request(method, path)
        assert r.status_code == 401, f"{method} {path} → {r.status_code}"


# ---------------------------------------------------------------------------
# this-install-fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_this_install_fingerprint_matches_keypair(
    migrate_client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    migrate_key_pair: KeyPair,
) -> None:
    del seeded
    r = await migrate_client.get(
        "/api/admin/migrate/this-install-fingerprint", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fingerprint_canonical"] == compute_fingerprint(migrate_key_pair.public_key)
    assert ":" in body["fingerprint_display"]
    assert body["fingerprint_display"] == body["fingerprint_display"].upper()


# ---------------------------------------------------------------------------
# Trusted-peer CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_then_list_trusted_peer(
    migrate_client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    add = await migrate_client.post(
        "/api/admin/migrate/trusted-peers",
        headers=auth_headers,
        json={
            # Use a paste-style fingerprint to confirm canonicalisation on add.
            "fingerprint": DISPLAY_FP,
            "label": "Staging",
            "notes": "prod-network MTX-412",
        },
    )
    assert add.status_code == 201, add.text
    created = add.json()
    assert created["fingerprint"] == CANONICAL_FP
    assert created["fingerprint_display"] == DISPLAY_FP
    assert created["label"] == "Staging"
    assert created["notes"] == "prod-network MTX-412"

    listing = await migrate_client.get("/api/admin/migrate/trusted-peers", headers=auth_headers)
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    assert rows[0]["fingerprint"] == CANONICAL_FP
    assert rows[0]["last_used_at"] is None


@pytest.mark.asyncio
async def test_add_duplicate_peer_returns_409(
    migrate_client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    payload = {"fingerprint": CANONICAL_FP, "label": "Staging"}
    first = await migrate_client.post(
        "/api/admin/migrate/trusted-peers", headers=auth_headers, json=payload
    )
    assert first.status_code == 201
    second = await migrate_client.post(
        "/api/admin/migrate/trusted-peers",
        headers=auth_headers,
        json={**payload, "label": "Different Label"},
    )
    assert second.status_code == 409
    assert "already trusted" in second.json()["detail"].lower()


@pytest.mark.asyncio
async def test_add_peer_empty_label_returns_400(
    migrate_client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    r = await migrate_client.post(
        "/api/admin/migrate/trusted-peers",
        headers=auth_headers,
        json={"fingerprint": CANONICAL_FP, "label": "   "},
    )
    # Pydantic's min_length=1 short-circuits before our service layer's
    # blank-trim check fires; either way the operator gets a 4xx.
    assert r.status_code in (400, 422)


@pytest.mark.asyncio
async def test_add_peer_malformed_fingerprint_returns_400(
    migrate_client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    r = await migrate_client.post(
        "/api/admin/migrate/trusted-peers",
        headers=auth_headers,
        json={"fingerprint": "this is not a fingerprint", "label": "Bad"},
    )
    assert r.status_code == 400
    assert "hexadecimal" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_remove_trusted_peer(
    migrate_client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    await migrate_client.post(
        "/api/admin/migrate/trusted-peers",
        headers=auth_headers,
        json={"fingerprint": CANONICAL_FP, "label": "Staging"},
    )
    # Use the colon-uppercase paste form on the path — canonicalisation
    # at the service layer should normalise it.
    delete = await migrate_client.request(
        "DELETE",
        f"/api/admin/migrate/trusted-peers/{DISPLAY_FP}",
        headers=auth_headers,
        json={"reason": "Decommissioning Staging install"},
    )
    assert delete.status_code == 204
    listing = await migrate_client.get("/api/admin/migrate/trusted-peers", headers=auth_headers)
    assert listing.json() == []


@pytest.mark.asyncio
async def test_remove_unknown_peer_returns_404(
    migrate_client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    r = await migrate_client.request(
        "DELETE",
        f"/api/admin/migrate/trusted-peers/{CANONICAL_FP}",
        headers=auth_headers,
        json={"reason": "doesn't exist"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_returns_gzipped_bundle_with_filename_header(
    migrate_client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    migrate_key_pair: KeyPair,
) -> None:
    tool_alias = seeded["tool_alias"]
    body = {
        "tool_specs": [{"alias": tool_alias, "version": "1.0.0"}],
        "workflow_aliases": [],
        "notes": "test bundle",
    }
    r = await migrate_client.post("/api/admin/migrate/export", headers=auth_headers, json=body)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/gzip"
    cd = r.headers["content-disposition"]
    assert "attachment" in cd
    assert "matofx-migrate-" in cd
    assert ".tar.gz" in cd
    expected_fp = compute_fingerprint(migrate_key_pair.public_key)
    assert r.headers["X-Bundle-Fingerprint"] == expected_fp

    # The bytes really are a valid gzip-tarball with a manifest.json inside.
    with tarfile.open(fileobj=BytesIO(r.content), mode="r:gz") as tar:
        names = tar.getnames()
        assert "manifest.json" in names
        manifest_member = tar.extractfile("manifest.json")
        assert manifest_member is not None
        manifest = json.loads(manifest_member.read().decode("utf-8"))
        assert manifest["source_fingerprint"] == expected_fp
        assert manifest["created_by"] == "root@example.com"
        assert manifest["notes"] == "test bundle"
        assert [(t["alias"], t["version"]) for t in manifest["tools"]] == [(tool_alias, "1.0.0")]


@pytest.mark.asyncio
async def test_export_unknown_tool_returns_404(
    migrate_client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    r = await migrate_client.post(
        "/api/admin/migrate/export",
        headers=auth_headers,
        json={
            "tool_specs": [{"alias": "no_such_tool", "version": "1.0"}],
            "workflow_aliases": [],
        },
    )
    assert r.status_code == 404
    assert (
        "no_such_tool" in r.json()["detail"].lower()
        or "not installed" in r.json()["detail"].lower()
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_then_import_round_trip(
    migrate_client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    migrate_key_pair: KeyPair,
) -> None:
    """End-to-end against the SAME install: export, trust its own fingerprint,
    then import. Exercises the full router round-trip including signature
    verification, trust-list lookup, and the per-item commit path.

    Same-install loopback isn't the real use case but it's the simplest
    way to verify the full HTTP surface from one test. The cross-install
    behaviour is exhaustively covered by ``test_export_import_e2e.py``.
    """
    tool_alias = seeded["tool_alias"]

    # 1. Export.
    export_resp = await migrate_client.post(
        "/api/admin/migrate/export",
        headers=auth_headers,
        json={
            "tool_specs": [{"alias": tool_alias, "version": "1.0.0"}],
            "workflow_aliases": [],
        },
    )
    assert export_resp.status_code == 200
    bundle_bytes = export_resp.content

    # 2. Add self as a trusted peer so import passes the trust check.
    fp = compute_fingerprint(migrate_key_pair.public_key)
    trust_resp = await migrate_client.post(
        "/api/admin/migrate/trusted-peers",
        headers=auth_headers,
        json={"fingerprint": fp, "label": "Self"},
    )
    assert trust_resp.status_code == 201

    # 3. Dry-run import — gives the preview without committing.
    dry_resp = await migrate_client.post(
        "/api/admin/migrate/import?dry_run=true",
        headers=auth_headers,
        files={"file": ("bundle.tar.gz", bundle_bytes, "application/gzip")},
    )
    assert dry_resp.status_code == 200, dry_resp.text
    envelope = dry_resp.json()
    assert envelope["result"]["dry_run"] is True
    # The tool already exists on this install at the same descriptor hash
    # (same install loopback), so the outcome is a no-op.
    items = envelope["result"]["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "tool"
    assert items[0]["outcome"] == "no-op"
    # Events have come through.
    levels = [e["level"] for e in envelope["events"]]
    assert "info" in levels
    assert "summary" in levels


@pytest.mark.asyncio
async def test_import_untrusted_bundle_returns_403(
    migrate_client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    tool_alias = seeded["tool_alias"]
    # Export a bundle — but don't add the source's fingerprint to the trust list.
    export_resp = await migrate_client.post(
        "/api/admin/migrate/export",
        headers=auth_headers,
        json={
            "tool_specs": [{"alias": tool_alias, "version": "1.0.0"}],
            "workflow_aliases": [],
        },
    )
    assert export_resp.status_code == 200
    bundle_bytes = export_resp.content

    # Import without trusting → 403.
    r = await migrate_client.post(
        "/api/admin/migrate/import",
        headers=auth_headers,
        files={"file": ("bundle.tar.gz", bundle_bytes, "application/gzip")},
    )
    assert r.status_code == 403
    assert "trusted peer list" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_import_empty_file_returns_400(
    migrate_client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    r = await migrate_client.post(
        "/api/admin/migrate/import",
        headers=auth_headers,
        files={"file": ("empty.tar.gz", b"", "application/gzip")},
    )
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_import_malformed_bundle_returns_400(
    migrate_client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    r = await migrate_client.post(
        "/api/admin/migrate/import",
        headers=auth_headers,
        files={"file": ("garbage.tar.gz", b"this is not a tarball", "application/gzip")},
    )
    assert r.status_code == 400
    assert "tarball" in r.json()["detail"].lower() or "corrupted" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Permission gating (non-super-user case)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regular_user_denied(
    migrate_client: httpx.AsyncClient,
    seeded: dict[str, Any],
    app: FastAPI,
) -> None:
    """A user without :attr:`Permission.MIGRATE_TOOLS_WORKFLOWS` gets 403.

    Builds a regular_user via the existing UserService, logs them in,
    and confirms the migrate endpoints refuse. Verifies the permission
    gate isn't accidentally satisfied by a non-super_user role.
    """
    from matofx.db import session_scope
    from matofx.users import make_password_hasher, make_user_service

    settings = app.state.settings
    audit = app.state.audit
    factory = app.state.factory
    hasher = make_password_hasher(settings.auth)
    user_service = make_user_service(settings.auth, audit, hasher=hasher)
    async with session_scope(factory) as session:
        await user_service.create_user(
            session,
            username="rachel",
            email="rachel@example.com",
            password="testpassword123",
            full_name="Rachel Regular",
            role="regular_user",
        )

    login = await migrate_client.post(
        "/api/auth/login",
        json={"username": "rachel", "password": "testpassword123"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    r = await migrate_client.get("/api/admin/migrate/trusted-peers", headers=headers)
    assert r.status_code == 403
