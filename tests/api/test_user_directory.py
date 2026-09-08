"""API test for the share-picker user directory (ADR-0002, task 11)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest


@pytest.mark.asyncio
async def test_directory_lists_active_users_minimal_fields(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    r = await client.get("/api/users/directory", headers=auth_headers)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) >= 1
    me = next(row for row in rows if row["username"] == seeded["username"])
    # Minimal identity only — no email/role leakage.
    assert set(me.keys()) == {"user_id", "username", "full_name"}
    assert isinstance(me["user_id"], int)
    assert me["full_name"]


@pytest.mark.asyncio
async def test_directory_requires_auth(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/users/directory")
    assert r.status_code in (401, 403)
