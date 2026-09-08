"""HTTP integration tests for ``/api/admin/general-settings``.

Covers the four route shapes:

* ``GET  /api/admin/general-settings`` — catalog + values, secrets redacted.
* ``POST /api/admin/general-settings/{key}`` — set a value.
* ``POST /api/admin/general-settings/{key}/reset`` — reset to default.
* ``PUT  /api/admin/general-settings/{key}/image`` — multipart upload.

Plus the role-gate edge: an administrator hitting a super-user-only
row gets a 403 with a plain-English message; a 403 from the service
layer maps to ``HTTP_403_FORBIDDEN`` via the registered exception
handler.

The seed fixture creates a super-user (``seeded``). For the 403 case
we additionally seed an administrator via ``UserService`` directly
against the same DB, then log in as them to grab a separate token.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from matofx.audit import AuditWriter
from matofx.config import Settings
from matofx.config.general_service import SECRET_REDACTED_SENTINEL
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.users import make_password_hasher, make_user_service

_ADMIN_PASSWORD = "adminpassword"


async def _seed_administrator(
    factory: SessionFactory, settings: Settings, audit: AuditWriter
) -> str:
    """Create an extra administrator user and return their username."""
    hasher = make_password_hasher(settings.auth)
    user_service = make_user_service(settings.auth, audit, hasher=hasher)
    async with session_scope(factory) as session:
        await user_service.create_user(
            session,
            username="adm",
            email="adm@example.com",
            password=_ADMIN_PASSWORD,
            full_name="Admin",
            role="administrator",
        )
    return "adm"


async def _seed_regular_user(
    factory: SessionFactory, settings: Settings, audit: AuditWriter
) -> str:
    """Create a plain regular user (no ``users.manage``) and return the username."""
    hasher = make_password_hasher(settings.auth)
    user_service = make_user_service(settings.auth, audit, hasher=hasher)
    async with session_scope(factory) as session:
        await user_service.create_user(
            session,
            username="reg",
            email="reg@example.com",
            password=_ADMIN_PASSWORD,
            full_name="Regular",
            role="regular_user",
        )
    return "reg"


async def _login(client: httpx.AsyncClient, *, username: str, password: str) -> str:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


# ---- GET ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_catalog_and_values(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/api/admin/general-settings", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "catalog" in body
    assert "values" in body
    # 35 page-surfaced settings + 3 auto-disable settings + 4 broadcast_*
    # rows (broadcasts managed only via Admin → Send Notifications; in
    # the catalog to reuse its validation/persistence, deliberately
    # absent from the GS page) + 1 lineage_max_nodes (ADR-0010 slice 7)
    # + 1 models_global_publish_approval (ADR-0008 §6.3)
    # + 1 llm_summaries_enabled (auto-summaries §6)
    # + 8 approval-notification rows (1 master + 7 per-type).
    # (No audit-log retention row — the audit log is a hash chain and is
    # retained in full.)
    assert len(body["catalog"]) == 53
    # Every catalog entry has the expected fields.
    entry = body["catalog"][0]
    for field in (
        "key",
        "kind",
        "label",
        "description",
        "default",
        "requires_role",
        "delegatable_to",
        "unit",
        "min_value",
        "max_value",
        "choices",
        "accepted_mime_types",
        "redacted",
        "pending_restart",
        "zero_means_never",
    ):
        assert field in entry, f"catalog entry missing '{field}'"
    # The response also exposes the per-row delegation map. Empty by
    # default — a fresh install has no super-user-granted delegations.
    assert "delegations" in body
    assert isinstance(body["delegations"], dict)


@pytest.mark.asyncio
async def test_get_redacts_secrets(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    # Save a real secret first.
    set_resp = await client.post(
        "/api/admin/general-settings/smtp_password",
        headers=auth_headers,
        json={"value": "hunter2"},
    )
    assert set_resp.status_code == 200, set_resp.text
    # Now read back via GET — must not contain the plaintext.
    get_resp = await client.get("/api/admin/general-settings", headers=auth_headers)
    body = get_resp.json()
    assert body["values"]["smtp_password"] == SECRET_REDACTED_SENTINEL


# ---- POST set + reset ---------------------------------------------------


@pytest.mark.asyncio
async def test_set_value_round_trips(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/api/admin/general-settings/session_idle_timeout_minutes",
        headers=auth_headers,
        json={"value": 17},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["values"]["session_idle_timeout_minutes"] == 17


@pytest.mark.asyncio
async def test_set_out_of_range_returns_400(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/api/admin/general-settings/session_idle_timeout_minutes",
        headers=auth_headers,
        json={"value": 99999},
    )
    assert response.status_code == 400, response.text
    assert "outside" in response.text.lower() or "maximum" in response.text.lower()


@pytest.mark.asyncio
async def test_set_unknown_key_returns_400(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/api/admin/general-settings/not_a_setting",
        headers=auth_headers,
        json={"value": 1},
    )
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_reset_restores_default(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    # Save a non-default value first.
    await client.post(
        "/api/admin/general-settings/session_idle_timeout_minutes",
        headers=auth_headers,
        json={"value": 17},
    )
    response = await client.post(
        "/api/admin/general-settings/session_idle_timeout_minutes/reset",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["values"]["session_idle_timeout_minutes"] == 30


# ---- Role gating --------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_blocked_on_super_user_only_row(
    client: httpx.AsyncClient,
    api_factory: SessionFactory,
    api_settings: Settings,
    api_audit: AuditWriter,
    seeded: dict[str, Any],
) -> None:
    """Administrator trying to set a super-user row gets 403 + clear message."""
    del seeded  # we just need the super-user to be created so the role table is populated
    username = await _seed_administrator(api_factory, api_settings, api_audit)
    admin_token = await _login(client, username=username, password=_ADMIN_PASSWORD)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    response = await client.post(
        "/api/admin/general-settings/session_idle_timeout_minutes",
        headers=admin_headers,
        json={"value": 20},
    )
    assert response.status_code == 403, response.text
    assert "super_user" in response.text


@pytest.mark.asyncio
async def test_admin_can_set_admin_row(
    client: httpx.AsyncClient,
    api_factory: SessionFactory,
    api_settings: Settings,
    api_audit: AuditWriter,
    seeded: dict[str, Any],
) -> None:
    """Administrator can save admin-level rows (e.g. SMTP host)."""
    del seeded
    username = await _seed_administrator(api_factory, api_settings, api_audit)
    admin_token = await _login(client, username=username, password=_ADMIN_PASSWORD)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    response = await client.post(
        "/api/admin/general-settings/smtp_host",
        headers=admin_headers,
        json={"value": "smtp.example.com"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["values"]["smtp_host"] == "smtp.example.com"


# ---- Inactive-users preview authorization ------------------------------


@pytest.mark.asyncio
async def test_inactive_users_preview_blocks_regular_user(
    client: httpx.AsyncClient,
    api_factory: SessionFactory,
    api_settings: Settings,
    api_audit: AuditWriter,
    seeded: dict[str, Any],
) -> None:
    """A regular user (no ``users.manage``) is refused with 403.

    The preview leaks dormant-account identity, so authentication alone
    must not be enough — the route enforces ``users.manage`` server-side.
    """
    del seeded
    username = await _seed_regular_user(api_factory, api_settings, api_audit)
    token = await _login(client, username=username, password=_ADMIN_PASSWORD)
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.get(
        "/api/admin/inactive-users/preview?threshold_days=365",
        headers=headers,
    )
    assert response.status_code == 403, response.text
    assert "users.manage" in response.text


@pytest.mark.asyncio
async def test_inactive_users_preview_allows_super_user(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """A super-user (holds ``users.manage``) still gets the preview."""
    response = await client.get(
        "/api/admin/inactive-users/preview?threshold_days=365",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "count" in body
    assert "users" in body


# ---- Image upload ------------------------------------------------------


# Minimal valid 1×1 transparent PNG.
_TINY_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63000100000005000100"
    "0d0a2db40000000049454e44ae426082"
)


@pytest.mark.asyncio
async def test_image_upload_accepts_valid_png(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.put(
        "/api/admin/general-settings/header_icon_path/image",
        headers=auth_headers,
        files={"file": ("logo.png", _TINY_PNG, "image/png")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["values"]["header_icon_path"] == "header_icon_path.png"


@pytest.mark.asyncio
async def test_image_upload_rejects_wrong_mime(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.put(
        "/api/admin/general-settings/favicon_path/image",
        headers=auth_headers,
        # favicon accepts ico + png; gif should be rejected.
        files={"file": ("logo.gif", b"GIF89a-data", "image/gif")},
    )
    assert response.status_code == 400, response.text
    assert "mime" in response.text.lower() or "expected" in response.text.lower()


# ---- Anonymous --------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_request_returns_401(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/admin/general-settings")
    assert response.status_code == 401
