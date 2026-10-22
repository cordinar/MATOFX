"""HTTP integration tests for ``/api/me/favourites``.

Covers the three route shapes (GET / PUT / DELETE), the
enabled/disabled feature-flag gating, idempotent semantics, kind
validation, and the cascade-on-user-delete behaviour the migration
relies on.

The favourites feature defaults to **off**, so each mutation test
first flips ``favourites_enabled`` on via the General Settings
admin route to mirror real-world setup.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select, text

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import AuthSettings
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.favourites.orm import UserFavourite
from matofx.users import make_password_hasher, make_user_service


async def _enable_favourites(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    """Flip the admin toggle on so mutation routes accept writes."""
    response = await client.post(
        "/api/admin/general-settings/favourites_enabled",
        headers=headers,
        json={"value": True},
    )
    assert response.status_code == 200, response.text


# ---- GET ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_empty_disabled_by_default(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Fresh install: feature off, no favourites — GET still succeeds."""
    response = await client.get("/api/me/favourites", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"enabled": False, "tools": [], "workflows": []}


@pytest.mark.asyncio
async def test_get_after_enable_still_empty(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Enabling the feature flips the flag but doesn't fabricate rows."""
    await _enable_favourites(client, auth_headers)
    response = await client.get("/api/me/favourites", headers=auth_headers)
    body = response.json()
    assert body == {"enabled": True, "tools": [], "workflows": []}


# ---- PUT (add) ----------------------------------------------------------


@pytest.mark.asyncio
async def test_put_adds_favourite_when_enabled(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    await _enable_favourites(client, auth_headers)
    response = await client.put("/api/me/favourites/tool/material_properties", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True
    assert body["tools"] == ["material_properties"]
    assert body["workflows"] == []


@pytest.mark.asyncio
async def test_put_is_idempotent(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    """Re-favouriting the same item is a no-op upsert, never a duplicate."""
    await _enable_favourites(client, auth_headers)
    for _ in range(3):
        response = await client.put("/api/me/favourites/tool/alpha", headers=auth_headers)
        assert response.status_code == 200, response.text
        assert response.json()["tools"] == ["alpha"]


@pytest.mark.asyncio
async def test_put_when_disabled_returns_409(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Feature off → mutation is rejected with a 409 + plain-English reason."""
    response = await client.put("/api/me/favourites/tool/material_properties", headers=auth_headers)
    assert response.status_code == 409, response.text
    assert "turned off" in response.text.lower()


@pytest.mark.asyncio
async def test_put_rejects_unknown_kind(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    await _enable_favourites(client, auth_headers)
    response = await client.put("/api/me/favourites/scenario/anything", headers=auth_headers)
    assert response.status_code == 400, response.text
    assert "scenario" in response.text


@pytest.mark.asyncio
async def test_put_accepts_workflow_kind(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    await _enable_favourites(client, auth_headers)
    response = await client.put("/api/me/favourites/workflow/heat_transfer", headers=auth_headers)
    body = response.json()
    assert body["workflows"] == ["heat_transfer"]
    assert body["tools"] == []


@pytest.mark.asyncio
async def test_put_keeps_both_lists_alphabetical(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Tools and workflows are returned alphabetical regardless of insert order."""
    await _enable_favourites(client, auth_headers)
    for kind, alias in (
        ("tool", "zeta"),
        ("workflow", "yankee"),
        ("tool", "alpha"),
        ("workflow", "xray"),
    ):
        await client.put(f"/api/me/favourites/{kind}/{alias}", headers=auth_headers)
    body = (await client.get("/api/me/favourites", headers=auth_headers)).json()
    assert body["tools"] == ["alpha", "zeta"]
    assert body["workflows"] == ["xray", "yankee"]


# ---- DELETE (remove) ----------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_favourite(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    await _enable_favourites(client, auth_headers)
    await client.put("/api/me/favourites/tool/alpha", headers=auth_headers)
    response = await client.delete("/api/me/favourites/tool/alpha", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["tools"] == []


@pytest.mark.asyncio
async def test_delete_is_idempotent(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Deleting a non-existent favourite is a no-op, never an error."""
    await _enable_favourites(client, auth_headers)
    response = await client.delete("/api/me/favourites/tool/never_added", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["tools"] == []


@pytest.mark.asyncio
async def test_delete_when_disabled_returns_409(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.delete("/api/me/favourites/tool/alpha", headers=auth_headers)
    assert response.status_code == 409, response.text


# ---- Auth ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/me/favourites")
    assert response.status_code == 401


# ---- Disable behaviour preserves rows -----------------------------------


@pytest.mark.asyncio
async def test_disabling_preserves_rows(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Toggling the feature off keeps existing rows; GET still returns them."""
    await _enable_favourites(client, auth_headers)
    await client.put("/api/me/favourites/tool/alpha", headers=auth_headers)
    await client.put("/api/me/favourites/workflow/beta", headers=auth_headers)
    # Now flip the feature off.
    off = await client.post(
        "/api/admin/general-settings/favourites_enabled",
        headers=auth_headers,
        json={"value": False},
    )
    assert off.status_code == 200, off.text
    body = (await client.get("/api/me/favourites", headers=auth_headers)).json()
    assert body["enabled"] is False
    assert body["tools"] == ["alpha"]
    assert body["workflows"] == ["beta"]


# ---- Cascade on user delete --------------------------------------------


@pytest.mark.asyncio
async def test_user_delete_cascades_favourites(
    api_factory: SessionFactory,
) -> None:
    """Deleting a user wipes their favourites via the FK ON DELETE CASCADE.

    Uses a throwaway user with **no** tools / projects / jobs. The
    shared ``seeded`` super-user can't be used here: it installs
    ``material_properties``, and ``tool.installed_by_user_id`` /
    ``tool_version.created_by_user_id`` are deliberately
    ``ON DELETE RESTRICT`` (provenance / no-delete integrity), so a
    raw ``DELETE FROM user`` on that user is correctly blocked before
    the favourites cascade — unrelated to what this test verifies.
    """
    auth = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )
    audit = AuditWriter(signing_key=None, key_id=None)
    user_service = make_user_service(auth, audit, hasher=make_password_hasher(auth))

    async with session_scope(api_factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="fav_only",
            email="favonly@example.com",
            password="testpassword123",
            full_name="Fav Only",
        )
        user_id = user.id
        session.add(UserFavourite(user_id=user_id, kind="tool", alias="alpha"))

    async with session_scope(api_factory) as session:
        rows = (await session.execute(select(UserFavourite))).scalars().all()
        assert len(rows) == 1

    # Raw DELETE — the API delete-user route is admin-only; this test
    # asserts the DB-level cascade in isolation.
    async with session_scope(api_factory) as session:
        await session.execute(text('DELETE FROM "user" WHERE id = :id').bindparams(id=user_id))

    async with session_scope(api_factory) as session:
        rows = (await session.execute(select(UserFavourite))).scalars().all()
        assert rows == []
