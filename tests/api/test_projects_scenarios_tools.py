"""Projects + scenarios + tools router tests."""

from __future__ import annotations

from typing import Any

import httpx
import pytest


@pytest.mark.asyncio
async def test_list_tools_returns_installed_released_tool(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    response = await client.get("/api/tools", headers=auth_headers)
    assert response.status_code == 200
    aliases = [t["alias"] for t in response.json()]
    assert seeded["tool_alias"] in aliases


@pytest.mark.asyncio
async def test_get_tool_returns_full_descriptor(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    response = await client.get(f"/api/tools/{seeded['tool_alias']}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["alias"] == seeded["tool_alias"]
    descriptor = body["descriptor"]
    assert descriptor["alias"] == seeded["tool_alias"]
    assert "inputs" in descriptor
    assert any(f["name"] == "component_1" for f in descriptor["inputs"])


@pytest.mark.asyncio
async def test_get_unknown_tool_returns_404(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    response = await client.get("/api/tools/does_not_exist", headers=auth_headers)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_tool_version_options_returns_current(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """The non-admin versions list surfaces the released version, flagged current.

    Backs the V&V "record against a specific version" picker — reachable
    by any user who can see the tool (no admin gate).
    """
    alias = seeded["tool_alias"]
    response = await client.get(f"/api/tools/{alias}/versions", headers=auth_headers)
    assert response.status_code == 200, response.text
    rows = response.json()
    assert rows, "expected at least the released version"
    assert all("tool_version_id" in r and "version" in r for r in rows)
    current = [r for r in rows if r["is_current"]]
    assert len(current) == 1, "exactly one version is the current release"

    # The current version id matches the one GET /api/tools/{alias} resolves.
    detail = await client.get(f"/api/tools/{alias}", headers=auth_headers)
    assert current[0]["tool_version_id"] == detail.json()["tool_version_id"]


@pytest.mark.asyncio
async def test_list_tool_version_options_unknown_tool_404(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    response = await client.get("/api/tools/does_not_exist/versions", headers=auth_headers)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_project_scenario_round_trip(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    # 1. Initially no projects.
    initial = await client.get("/api/projects", headers=auth_headers)
    assert initial.status_code == 200
    assert initial.json() == []

    # 2. Create a project.
    create_p = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "Project A", "description": "First project"},
    )
    assert create_p.status_code == 201
    project_id = create_p.json()["id"]

    # 3. Now lists.
    listed = await client.get("/api/projects", headers=auth_headers)
    assert listed.status_code == 200
    assert [p["id"] for p in listed.json()] == [project_id]

    # 4. Fetch by id.
    fetched = await client.get(f"/api/projects/{project_id}", headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Project A"

    # 5. Create a scenario inside it.
    create_s = await client.post(
        f"/api/projects/{project_id}/scenarios",
        headers=auth_headers,
        json={"name": "Scenario X", "description": "First scenario"},
    )
    assert create_s.status_code == 201
    scenario_id = create_s.json()["id"]
    assert create_s.json()["project_id"] == project_id

    # 6. List scenarios.
    listed_s = await client.get(f"/api/projects/{project_id}/scenarios", headers=auth_headers)
    assert listed_s.status_code == 200
    assert [s["id"] for s in listed_s.json()] == [scenario_id]

    # 7. Fetch scenario by id.
    got_s = await client.get(f"/api/scenarios/{scenario_id}", headers=auth_headers)
    assert got_s.status_code == 200
    assert got_s.json()["name"] == "Scenario X"


@pytest.mark.asyncio
async def test_list_projects_resolves_owner_and_my_role(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    """The list endpoint resolves Owner + My role (home-redesign §8.1).

    The creator of a brand-new project is its owner and holds the
    ``owner`` role; ``owner_name`` resolves to a non-empty display name.
    """
    del seeded
    created = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "Owned project", "description": ""},
    )
    assert created.status_code == 201
    pid = created.json()["id"]
    # The Creator's own response doesn't resolve the meta fields …
    assert created.json()["owner_name"] == ""

    listed = await client.get("/api/projects", headers=auth_headers)
    assert listed.status_code == 200
    row = next(p for p in listed.json() if p["id"] == pid)
    assert row["my_role"] == "owner"
    assert row["owner_user_id"] == row["creator_user_id"]
    assert row["owner_name"]  # resolved to a non-empty display name


@pytest.mark.asyncio
async def test_get_unknown_project_returns_404(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    response = await client.get("/api/projects/9999", headers=auth_headers)
    assert response.status_code == 404
