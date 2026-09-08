"""Tests for the Admin → Send Notifications router.

Covers the permission gate, the header/footer template (text +
image CRUD), broadcast validation, the email-disabled flag, and the
broadcast happy path (SMTP faked).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from matofx.api.deps import get_settings
from matofx.audit import AuditWriter
from matofx.config import BrandingSettings, EmailSettings, Settings
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.users import make_password_hasher, make_user_service

_PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


async def _make_regular_user(
    factory: SessionFactory, settings: Settings, audit: AuditWriter
) -> tuple[str, str]:
    hasher = make_password_hasher(settings.auth)
    svc = make_user_service(settings.auth, audit, hasher=hasher)
    pw = "regularpassword1"
    async with session_scope(factory) as session:
        await svc.create_user(
            session,
            username="reg",
            email="reg@example.com",
            password=pw,
            full_name="Reg",
            role="regular_user",
        )
    return "reg", pw


async def _login(client: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    r = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _enable_email(app: FastAPI, api_settings: Settings, tmp_path: Path) -> None:
    """Override get_settings so email is on and images land in tmp."""
    brand = tmp_path / "brand"
    custom = api_settings.model_copy(
        update={
            "email": EmailSettings(
                enabled=True,
                smtp_host="smtp.invalid",
                from_address="matofx@example.com",
                use_tls=False,
                use_starttls=False,
            ),
            "branding": BrandingSettings(icons_dir=brand),
        }
    )
    app.dependency_overrides[get_settings] = lambda: custom


# ---- template text ------------------------------------------------------


@pytest.mark.asyncio
async def test_template_text_roundtrip(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    get0 = await client.get("/api/admin/notifications/template", headers=auth_headers)
    assert get0.status_code == 200, get0.text
    assert get0.json() == {
        "header_text": "",
        "footer_text": "",
        "header_image": False,
        "footer_image": False,
    }

    saved = await client.post(
        "/api/admin/notifications/template",
        headers=auth_headers,
        json={"header_text": "Hi <b>all</b>", "footer_text": "Regards"},
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["header_text"] == "Hi <b>all</b>"
    assert body["footer_text"] == "Regards"


# ---- permission gate ----------------------------------------------------


@pytest.mark.asyncio
async def test_endpoints_require_notifications_send(
    client: httpx.AsyncClient,
    api_factory: SessionFactory,
    api_settings: Settings,
    api_audit: AuditWriter,
) -> None:
    username, pw = await _make_regular_user(api_factory, api_settings, api_audit)
    headers = await _login(client, username, pw)

    r1 = await client.get("/api/admin/notifications/template", headers=headers)
    assert r1.status_code == 403, r1.text

    r2 = await client.post(
        "/api/admin/notifications/broadcast",
        headers=headers,
        data={"audience": "all", "subject": "S", "body": "B"},
    )
    assert r2.status_code == 403, r2.text


# ---- broadcast validation + disabled flag -------------------------------


@pytest.mark.asyncio
async def test_broadcast_rejects_blank_subject(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    r = await client.post(
        "/api/admin/notifications/broadcast",
        headers=auth_headers,
        data={"audience": "all", "subject": "  ", "body": "B"},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_broadcast_rejects_unknown_audience(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    r = await client.post(
        "/api/admin/notifications/broadcast",
        headers=auth_headers,
        data={"audience": "nope", "subject": "S", "body": "B"},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_broadcast_group_audience_requires_selection(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    r = await client.post(
        "/api/admin/notifications/broadcast",
        headers=auth_headers,
        data={"audience": "active", "group_kind": "teams", "subject": "S", "body": "B"},
    )
    assert r.status_code == 422, r.text
    assert "team" in r.json()["detail"].lower()


# ---- audience options ---------------------------------------------------


@pytest.mark.asyncio
async def test_audience_options_lists_groups(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    r = await client.get("/api/admin/notifications/audience-options", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"teams", "roles", "curators"} <= set(body)
    # The curator/approver categories are install-independent.
    curator_ids = {c["id"] for c in body["curators"]}
    assert curator_ids == {
        "library",
        "materials",
        "vv",
        "models",
        "cohorts",
        "optimisation_approvers",
    }
    # "World" is the universal-visibility marker, not a real team.
    assert "World" not in {t["label"] for t in body["teams"]}
    # Seeded roles are present and rendered in plain English.
    role_label_list = [r["label"] for r in body["roles"]]
    role_labels = set(role_label_list)
    assert "Regular User" in role_labels
    # Curator/approver capability roles live in their own picker, not here.
    assert not any(label.endswith("Curator") for label in role_labels)
    assert "Optimisation Approver" not in role_labels
    # Built-in base roles lead, in descending-rights order.
    builtins_in_order = [
        label
        for label in role_label_list
        if label in {"Super User", "Administrator", "Beta Tester", "Regular User"}
    ]
    assert builtins_in_order == ["Super User", "Administrator", "Beta Tester", "Regular User"]


@pytest.mark.asyncio
async def test_audience_options_require_notifications_send(
    client: httpx.AsyncClient,
    api_factory: SessionFactory,
    api_settings: Settings,
    api_audit: AuditWriter,
) -> None:
    username, pw = await _make_regular_user(api_factory, api_settings, api_audit)
    headers = await _login(client, username, pw)
    r = await client.get("/api/admin/notifications/audience-options", headers=headers)
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_broadcast_reports_disabled_when_email_off(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    # Default api_settings has EmailSettings() → enabled is False.
    r = await client.post(
        "/api/admin/notifications/broadcast",
        headers=auth_headers,
        data={"audience": "all", "subject": "S", "body": "B"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["disabled"] is True
    assert body["sent"] == 0


# ---- image CRUD + broadcast happy path (email enabled) ------------------


@pytest.mark.asyncio
async def test_template_image_upload_and_clear(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    api_settings: Settings,
    tmp_path: Path,
) -> None:
    _enable_email(app, api_settings, tmp_path)

    up = await client.put(
        "/api/admin/notifications/template/header/image",
        headers=auth_headers,
        files={"file": ("banner.png", _PNG_1PX, "image/png")},
    )
    assert up.status_code == 200, up.text
    assert up.json()["header_image"] is True

    img = await client.get("/api/admin/notifications/template/header/image", headers=auth_headers)
    assert img.status_code == 200
    assert img.content == _PNG_1PX

    cleared = await client.delete(
        "/api/admin/notifications/template/header/image", headers=auth_headers
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["header_image"] is False

    missing = await client.get(
        "/api/admin/notifications/template/header/image", headers=auth_headers
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_broadcast_happy_path(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    api_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_email(app, api_settings, tmp_path)
    sent: list[Any] = []

    class _FakeSmtp:
        def __init__(self, effective: dict[str, Any]) -> None:
            self._e = effective

        async def __aenter__(self) -> _FakeSmtp:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def send(self, message: Any) -> None:
            sent.append(message["To"])

    monkeypatch.setattr("matofx.notify.broadcast.SmtpSession", _FakeSmtp)

    r = await client.post(
        "/api/admin/notifications/broadcast",
        headers=auth_headers,
        data={"audience": "all", "subject": "Maintenance", "body": "Down 2-3pm"},
        files=[("files", ("note.txt", b"hi", "text/plain"))],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["disabled"] is False
    assert body["sent"] == 1  # only the seeded super-user
    assert body["attempted"] == 1
    assert body["failed"] == []
    assert sent == ["root@example.com"]
