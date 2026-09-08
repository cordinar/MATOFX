"""Visible-behaviour tests for the General Settings consumers.

The first build's tests verified the *storage* layer (rows save, role
gating, validation). They missed the bug class the user just hit: a
setting persists but no consumer reads it back, so the UI shows no
visible change. These tests ride the actual consumer pathways:

* **Branding cache** — saving the title/banner/footer/icons through
  the API, then asking the chrome's cache for them, must return the
  saved values.
* **Image serving** — uploaded bytes round-trip through the public
  ``/api/branding/{key}`` route with the right content type.
* **SMTP overlay** — saving smtp_host through the API, then asking
  the notifier for its effective settings, must reflect the new host.
* **Password complexity** — the catalog's all-required defaults are
  enforced at the API password-set endpoints.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from matofx.audit import AuditWriter
from matofx.config import EmailSettings
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.notify.email import EmailNotifier
from matofx.ui import branding_cache
from matofx.ui.branding_cache import refresh_branding_cache


@pytest.fixture(autouse=True)
def _reset_branding_cache() -> object:
    """Snapshot + restore the module-level branding cache around each test.

    The cache is process-wide so a test that saves a title would
    otherwise leak the new title into a later test that reads
    ``get_branding()`` (chrome unit tests, notify tests that load any
    UI module, etc.). Pinning before-and-after isolates each test.
    """
    saved = branding_cache._current  # noqa: SLF001 — test-isolation hook
    yield None
    branding_cache._current = saved  # noqa: SLF001


# Minimal valid 1×1 transparent PNG.
_TINY_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63000100000005000100"
    "0d0a2db40000000049454e44ae426082"
)


# ---- Branding cache reflects saved values ------------------------------


@pytest.mark.asyncio
async def test_saved_title_appears_in_branding_cache(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """Title save → next branding refresh → cache returns the new title."""
    response = await client.post(
        "/api/admin/general-settings/app_display_title",
        headers=auth_headers,
        json={"value": "Acme Test Suite"},
    )
    assert response.status_code == 200, response.text

    async with session_scope(api_factory) as session:
        snapshot = await refresh_branding_cache(session)
    assert snapshot.app_display_title == "Acme Test Suite"


@pytest.mark.asyncio
async def test_saved_banner_appears_in_branding_cache(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """Banner save → cache contains it for the chrome to render."""
    text = "Scheduled outage on 2026-05-20"
    response = await client.post(
        "/api/admin/general-settings/maintenance_banner_text",
        headers=auth_headers,
        json={"value": text},
    )
    assert response.status_code == 200, response.text

    async with session_scope(api_factory) as session:
        snapshot = await refresh_branding_cache(session)
    assert snapshot.maintenance_banner_text == text


@pytest.mark.asyncio
async def test_saved_support_email_appears_in_branding_cache(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    response = await client.post(
        "/api/admin/general-settings/support_email_footer",
        headers=auth_headers,
        json={"value": "help@example.com"},
    )
    assert response.status_code == 200, response.text

    async with session_scope(api_factory) as session:
        snapshot = await refresh_branding_cache(session)
    assert snapshot.support_email_footer == "help@example.com"


@pytest.mark.asyncio
async def test_branding_cache_refresh_runs_on_router_save(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """The router's `_maybe_refresh_branding` covers the chrome cache too.

    Saving a branding row should make :func:`get_branding` return the
    new value without an explicit ``refresh_branding_cache`` call — the
    API route does it. Important because the chrome reads the cache
    synchronously on every page render.
    """
    from matofx.ui.branding_cache import get_branding  # noqa: PLC0415

    title = "Acme via Router Refresh"
    response = await client.post(
        "/api/admin/general-settings/app_display_title",
        headers=auth_headers,
        json={"value": title},
    )
    assert response.status_code == 200, response.text
    assert get_branding().app_display_title == title


# ---- Image upload + serve round-trip -----------------------------------


@pytest.mark.asyncio
async def test_uploaded_header_icon_serves_back_correctly(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Upload bytes via PUT, fetch back via GET, compare byte-for-byte.

    Catches: image is saved but file doesn't land on disk; basename
    mismatch between DB row and served path; wrong content-type;
    storage path resolution off-by-one.
    """
    up = await client.put(
        "/api/admin/general-settings/header_icon_path/image",
        headers=auth_headers,
        files={"file": ("logo.png", _TINY_PNG, "image/png")},
    )
    assert up.status_code == 200, up.text

    fetched = await client.get("/api/branding/header_icon_path")
    assert fetched.status_code == 200, fetched.text
    assert fetched.content == _TINY_PNG
    assert fetched.headers.get("content-type", "").startswith("image/png")


@pytest.mark.asyncio
async def test_unset_branding_image_returns_404(
    client: httpx.AsyncClient,
) -> None:
    """No upload → /api/branding/header_icon_path returns 404.

    The chrome handles 404 by simply not rendering the ``<img>``; this
    test pins that the route doesn't 500 or leak stale paths.
    """
    response = await client.get("/api/branding/header_icon_path")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unknown_branding_key_returns_404(
    client: httpx.AsyncClient,
) -> None:
    """Non-branding keys can't be served — 404, not 500."""
    response = await client.get("/api/branding/session_idle_timeout_minutes")
    assert response.status_code == 404


# ---- SMTP overlay ------------------------------------------------------


@pytest.mark.asyncio
async def test_smtp_host_override_takes_effect_in_notifier(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """Saving smtp_host via API → notifier picks it up on next send.

    Catches: notifier reading from construction-time EmailSettings
    instead of the live DB snapshot — the v1 bug that left SMTP
    saves visible in the UI but ineffectual in actual job emails.
    """
    new_host = "smtp.override.example.com"
    response = await client.post(
        "/api/admin/general-settings/smtp_host",
        headers=auth_headers,
        json={"value": new_host},
    )
    assert response.status_code == 200, response.text

    notifier = EmailNotifier(
        settings=EmailSettings(enabled=True, smtp_host="from-toml"),
        audit=AuditWriter(signing_key=None, key_id=None),
    )
    from matofx.config.general_service import load_general_setting_overrides  # noqa: PLC0415
    from matofx.notify import resolve_effective_email_settings  # noqa: PLC0415

    async with session_scope(api_factory) as session:
        overrides = await load_general_setting_overrides(session)
    effective = resolve_effective_email_settings(notifier._settings, overrides)
    assert effective["smtp_host"] == new_host
    # Sibling SMTP settings without overrides fall back to the TOML baseline.
    assert effective["smtp_port"] == 25  # EmailSettings default


@pytest.mark.asyncio
async def test_smtp_settings_fall_back_to_toml_baseline(
    client: httpx.AsyncClient,
    api_factory: SessionFactory,
) -> None:
    """No DB rows → notifier uses TOML EmailSettings.

    Confirms the overlay isn't accidentally returning catalog defaults
    instead of the operator-supplied baseline.
    """
    del client
    notifier = EmailNotifier(
        settings=EmailSettings(
            enabled=True,
            smtp_host="baseline-host",
            smtp_port=2525,
            from_address="baseline@example.com",
        ),
        audit=AuditWriter(signing_key=None, key_id=None),
    )
    from matofx.config.general_service import load_general_setting_overrides  # noqa: PLC0415
    from matofx.notify import resolve_effective_email_settings  # noqa: PLC0415

    async with session_scope(api_factory) as session:
        overrides = await load_general_setting_overrides(session)
    effective = resolve_effective_email_settings(notifier._settings, overrides)
    # Both rows fall through to the baseline because no DB override is set.
    # (Catalog defaults differ — 'localhost'/25 — so a passthrough bug would
    # show up here.)
    assert effective["smtp_host"] == "baseline-host"
    assert effective["from_address"] == "baseline@example.com"


# ---- Password complexity enforced at the API boundary ------------------


@pytest.mark.asyncio
async def test_change_password_rejects_complexity_violation(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """A password that meets length but no other class is rejected.

    Confirms ``validate_password_complexity`` is wired into
    ``/api/auth/me/change-password`` against the live catalog snapshot
    (whose defaults require upper / lower / digit / symbol).
    """
    response = await client.post(
        "/api/auth/me/change-password",
        headers=auth_headers,
        json={
            "current_password": seeded["password"],
            "new_password": "alllowercase",  # no upper / digit / symbol
        },
    )
    assert response.status_code == 400, response.text
    body = response.json()
    detail = body.get("detail", "").lower()
    assert "uppercase" in detail or "digit" in detail or "symbol" in detail


@pytest.mark.asyncio
async def test_change_password_accepts_compliant_password(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """A password with all four classes passes."""
    response = await client.post(
        "/api/auth/me/change-password",
        headers=auth_headers,
        json={
            "current_password": seeded["password"],
            "new_password": "AcmeUser1!OK",
        },
    )
    assert response.status_code == 204, response.text


@pytest.mark.asyncio
async def test_admin_create_user_rejects_weak_password(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Admin creating a user via /api/admin/users hits the same complexity gate."""
    response = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "weakpwuser",
            "email": "weakpw@example.com",
            "first_name": "Weak",
            "last_name": "Password",
            "password": "alllowercase",
            "role": "regular_user",
        },
    )
    assert response.status_code == 400, response.text


# ---- Maintenance window appended to the banner --------------------------


@pytest.mark.asyncio
async def test_window_text_pairs_with_banner_when_set(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """Cache exposes window text alongside banner so chrome can render both."""
    await client.post(
        "/api/admin/general-settings/maintenance_banner_text",
        headers=auth_headers,
        json={"value": "Read-only mode for upgrade"},
    )
    await client.post(
        "/api/admin/general-settings/maintenance_window_iso",
        headers=auth_headers,
        json={"value": "2026-06-01T02:00:00Z"},
    )
    async with session_scope(api_factory) as session:
        snapshot = await refresh_branding_cache(session)
    assert snapshot.maintenance_banner_text == "Read-only mode for upgrade"
    assert snapshot.maintenance_window_iso == "2026-06-01T02:00:00Z"
