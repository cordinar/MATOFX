"""Home dashboard summary endpoint tests (home-redesign §8.6)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest


@pytest.mark.asyncio
async def test_home_summary_zeroed_for_fresh_install(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """With nothing submitted yet, every count is zero and the shape is stable."""
    del seeded
    resp = await client.get("/api/home/summary", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "active_jobs": 0,
        "running_optimisations": 0,
        "running_parametric_studies": 0,
        "pending_approvals": 0,
        # The seeded user is a super_user but not flagged as an approver.
        "can_approve": False,
    }


@pytest.mark.asyncio
async def test_home_summary_requires_auth(client: httpx.AsyncClient) -> None:
    """The summary is per-user, so an anonymous request is rejected."""
    resp = await client.get("/api/home/summary")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_search_tool_hit_routes_tools_manager_to_editor(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """A tool hit routes a user who can manage tools to the New/edit Tool page.

    The seeded user is a super_user (holds ``tools.install``), so the tool
    result deep-links to the editor rather than the tool-filtered Job Search.
    """
    alias = seeded["tool_alias"]
    resp = await client.get(
        "/api/search", params={"q": alias, "scope": "tool"}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    tool_hits = [h for h in resp.json() if h["type"] == "tool"]
    assert tool_hits, resp.json()
    assert tool_hits[0]["route"] == f"/admin/tools/{alias}/edit"
