"""Auth-router tests: login / logout / me / unauthenticated routes."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import select

from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.tools.orm import Tool
from matofx.users.orm import User


@pytest.mark.asyncio
async def test_login_happy_path(client: httpx.AsyncClient, seeded: dict[str, Any]) -> None:
    response = await client.post(
        "/api/auth/login",
        json={"username": seeded["username"], "password": seeded["password"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["user_id"] == seeded["user_id"]
    assert isinstance(body["access_token"], str)
    assert len(body["access_token"]) > 32


@pytest.mark.asyncio
async def test_login_bad_password_returns_401(
    client: httpx.AsyncClient, seeded: dict[str, Any]
) -> None:
    response = await client.post(
        "/api/auth/login",
        json={"username": seeded["username"], "password": "not-the-right-password"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_ip_throttle_returns_429_after_repeated_failures(
    app: Any, client: httpx.AsyncClient, seeded: dict[str, Any]
) -> None:
    """Repeated failed logins from one IP trip the per-IP throttle (Low 5).

    The block is independent of the targeted account: once tripped, even a
    request bearing the correct credentials is refused with 429.
    """
    from matofx.users.login_throttle import IpLoginThrottle  # noqa: PLC0415

    app.state.login_throttle = IpLoginThrottle(
        max_failures=3, window_seconds=100.0, block_seconds=100.0
    )
    for _ in range(3):
        bad = await client.post(
            "/api/auth/login",
            json={"username": seeded["username"], "password": "wrong-password"},
        )
        assert bad.status_code == 401

    blocked = await client.post(
        "/api/auth/login",
        json={"username": seeded["username"], "password": seeded["password"]},
    )
    assert blocked.status_code == 429


@pytest.mark.asyncio
async def test_login_unknown_user_returns_401(
    client: httpx.AsyncClient, seeded: dict[str, Any]
) -> None:
    del seeded
    response = await client.post(
        "/api/auth/login",
        json={"username": "does-not-exist", "password": "whatever-1234"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_unauthenticated_endpoint_returns_401(
    client: httpx.AsyncClient, seeded: dict[str, Any]
) -> None:
    del seeded
    response = await client.get("/api/projects")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_current_user(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    response = await client.get("/api/auth/me", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == seeded["username"]
    assert body["user_id"] == seeded["user_id"]
    assert body["role"] == "super_user"
    # Default for the new opt-in flag is True (preserves pre-0016 behaviour).
    assert body["email_notifications_enabled"] is True
    # Default theme is "dark" — keeps existing pilot users on the same look.
    assert body["theme_preference"] == "dark"
    assert body["is_approver"] is False
    assert body["approver_tools"] == []
    assert body["teams"] == []
    # last_login_at is set by authenticate(); created_at is always present.
    assert isinstance(body["created_at"], str)
    assert isinstance(body["last_login_at"], str)


@pytest.mark.asyncio
async def test_me_includes_effective_permissions(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """The caller's effective permission set is surfaced via /me (ADR-0005)."""
    del seeded
    response = await client.get("/api/auth/me", headers=auth_headers)
    assert response.status_code == 200
    perms = response.json()["permissions"]
    # Seeded user is super_user → holds the whole catalog, incl. the new
    # capability slugs (used by the UI to gate the capability-roles picker).
    assert "roles.manage" in perms
    assert "library.curate" in perms
    assert "materials.curate" in perms


@pytest.mark.asyncio
async def test_me_returns_team_membership(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Teams the user belongs to are surfaced via /me."""
    create = await client.post(
        "/api/admin/teams",
        headers=auth_headers,
        json={"name": "modellers", "description": ""},
    )
    assert create.status_code == 201, create.text
    team_id = int(create.json()["id"])
    add = await client.post(
        f"/api/admin/teams/{team_id}/members",
        headers=auth_headers,
        json={"user_id": seeded["user_id"]},
    )
    assert add.status_code == 204, add.text

    response = await client.get("/api/auth/me", headers=auth_headers)
    assert response.status_code == 200
    teams = response.json()["teams"]
    assert [t["name"] for t in teams] == ["modellers"]
    assert teams[0]["id"] == team_id


@pytest.mark.asyncio
async def test_me_lists_approver_tools(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """An approver sees the names of the tools they're listed on via /me."""
    async with session_scope(api_factory) as session:
        user = await session.get(User, seeded["user_id"])
        assert user is not None
        user.is_approver = True
        tool = (
            await session.execute(select(Tool).where(Tool.alias == seeded["tool_alias"]))
        ).scalar_one()
        descriptor = dict(tool.descriptor)
        descriptor["approver_usernames"] = [seeded["username"]]
        tool.descriptor = descriptor
        tool_name = tool.name

    response = await client.get("/api/auth/me", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["is_approver"] is True
    assert body["approver_tools"] == [tool_name]


@pytest.mark.asyncio
async def test_me_preferences_toggle_persists(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """POST /me/preferences flips the flag and the next /me reflects it."""
    del seeded
    off = await client.post(
        "/api/auth/me/preferences",
        headers=auth_headers,
        json={"email_notifications_enabled": False},
    )
    assert off.status_code == 200, off.text
    assert off.json()["email_notifications_enabled"] is False

    me = await client.get("/api/auth/me", headers=auth_headers)
    assert me.json()["email_notifications_enabled"] is False

    on = await client.post(
        "/api/auth/me/preferences",
        headers=auth_headers,
        json={"email_notifications_enabled": True},
    )
    assert on.status_code == 200
    assert on.json()["email_notifications_enabled"] is True


@pytest.mark.asyncio
async def test_me_theme_toggle_persists(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """POST /me/theme flips the value and the next /me reflects it."""
    del seeded
    light = await client.post(
        "/api/auth/me/theme",
        headers=auth_headers,
        json={"theme": "light"},
    )
    assert light.status_code == 200, light.text
    assert light.json()["theme_preference"] == "light"

    me = await client.get("/api/auth/me", headers=auth_headers)
    assert me.json()["theme_preference"] == "light"

    dark = await client.post(
        "/api/auth/me/theme",
        headers=auth_headers,
        json={"theme": "dark"},
    )
    assert dark.status_code == 200
    assert dark.json()["theme_preference"] == "dark"


@pytest.mark.asyncio
async def test_me_returns_ui_preferences_default_empty(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A fresh user's shell-preferences blob starts empty (home §9)."""
    del seeded
    me = await client.get("/api/auth/me", headers=auth_headers)
    assert me.json()["ui_preferences"] == {}


@pytest.mark.asyncio
async def test_me_ui_preferences_partial_merge_persists(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Partial writes merge: width then filter coexist; /me reflects the union."""
    del seeded
    # First write only the width.
    one = await client.post(
        "/api/auth/me/ui-preferences",
        headers=auth_headers,
        json={"sidebar_width": 300},
    )
    assert one.status_code == 200, one.text
    assert one.json()["ui_preferences"] == {"sidebar_width": 300}

    # A second, disjoint write must not clobber the first key.
    two = await client.post(
        "/api/auth/me/ui-preferences",
        headers=auth_headers,
        json={"quick_access_filter": "workflow", "sidebar_pinned": False},
    )
    assert two.status_code == 200, two.text
    prefs = two.json()["ui_preferences"]
    assert prefs == {
        "sidebar_width": 300,
        "quick_access_filter": "workflow",
        "sidebar_pinned": False,
    }

    me = await client.get("/api/auth/me", headers=auth_headers)
    assert me.json()["ui_preferences"] == prefs


@pytest.mark.asyncio
async def test_me_ui_preferences_rejects_empty_offlist_and_range(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Empty body, off-list filter, and out-of-range width all 422; nothing stored."""
    del seeded
    for bad_body in (
        {},
        {"quick_access_filter": "everything"},
        {"sidebar_width": 9999},
        {"sidebar_width": 10},
    ):
        resp = await client.post(
            "/api/auth/me/ui-preferences", headers=auth_headers, json=bad_body
        )
        assert resp.status_code == 422, f"{bad_body} -> {resp.text}"

    me = await client.get("/api/auth/me", headers=auth_headers)
    assert me.json()["ui_preferences"] == {}


@pytest.mark.asyncio
async def test_me_theme_rejects_unknown_value(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Off-list values fail validation at the wire layer (Literal type)."""
    del seeded
    response = await client.post(
        "/api/auth/me/theme",
        headers=auth_headers,
        json={"theme": "neon"},
    )
    assert response.status_code == 422, response.text

    me = await client.get("/api/auth/me", headers=auth_headers)
    # Invalid request must not have mutated the stored value.
    assert me.json()["theme_preference"] == "dark"


@pytest.mark.asyncio
async def test_change_password_self_happy_path(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """Self-service change-password updates the credential users sign in with."""
    response = await client.post(
        "/api/auth/me/change-password",
        headers=auth_headers,
        json={
            "current_password": seeded["password"],
            "new_password": "A-new-password-123!",
        },
    )
    assert response.status_code == 204, response.text

    # Old password no longer authenticates.
    fail = await client.post(
        "/api/auth/login",
        json={"username": seeded["username"], "password": seeded["password"]},
    )
    assert fail.status_code == 401

    # New password does.
    ok = await client.post(
        "/api/auth/login",
        json={"username": seeded["username"], "password": "A-new-password-123!"},
    )
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_change_password_rejects_wrong_current(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    response = await client.post(
        "/api/auth/me/change-password",
        headers=auth_headers,
        json={
            "current_password": "definitely-not-it",
            "new_password": "A-fresh-password-123!",
        },
    )
    assert response.status_code == 400
    assert "current password" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_change_password_rejects_weak_new(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/api/auth/me/change-password",
        headers=auth_headers,
        json={
            "current_password": seeded["password"],
            "new_password": "x",  # below the 8-char policy min in api_settings
        },
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_logout_revokes_token(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    logout = await client.post("/api/auth/logout", headers=auth_headers)
    assert logout.status_code == 204
    after = await client.get("/api/auth/me", headers=auth_headers)
    assert after.status_code == 401


@pytest.mark.asyncio
async def test_malformed_authorization_header_returns_401(
    client: httpx.AsyncClient, seeded: dict[str, Any]
) -> None:
    del seeded
    response = await client.get("/api/projects", headers={"Authorization": "BadScheme abcdef"})
    assert response.status_code == 401
