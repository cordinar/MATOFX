"""Slice 8 — the per-user summary-enrichment opt-out endpoint.

End-to-end: ``POST /api/auth/me/summary-preferences`` flips the flag and
``GET /api/auth/me`` reflects it (defaulting to opted-in). Also proves migration
0072 applied (the column exists on the seeded super-user).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.asyncio


async def test_default_is_opted_in(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.get("/api/auth/me", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["summary_enrichment_enabled"] is True


async def test_opt_out_then_back_in(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    off = await client.post(
        "/api/auth/me/summary-preferences",
        headers=auth_headers,
        json={"summary_enrichment_enabled": False},
    )
    assert off.status_code == 200
    assert off.json()["summary_enrichment_enabled"] is False

    me = await client.get("/api/auth/me", headers=auth_headers)
    assert me.json()["summary_enrichment_enabled"] is False

    on = await client.post(
        "/api/auth/me/summary-preferences",
        headers=auth_headers,
        json={"summary_enrichment_enabled": True},
    )
    assert on.json()["summary_enrichment_enabled"] is True


async def test_unauthenticated_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/auth/me/summary-preferences",
        json={"summary_enrichment_enabled": False},
    )
    assert response.status_code == 401


async def test_general_setting_present_in_catalog(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/api/admin/general-settings", headers=auth_headers)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    keys = {entry["key"] for entry in body["catalog"]}
    assert "llm_summaries_enabled" in keys
