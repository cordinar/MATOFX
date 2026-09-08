"""Tests for the UI's in-process API client.

Reuses the API test fixtures (seeded super-user + storage location +
installed material_properties tool) so every assertion lands against a
real ``matofx.api`` app — exercising the same auth + access-control
path an external client would hit.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from matofx.ui.api_client import ApiClient, ApiError

# Shared fixtures (`app`, `seeded`, ...) come via tests/ui/conftest.py
# which re-exports tests/_api_fixtures.


@pytest.mark.asyncio
async def test_login_returns_bearer(app: FastAPI, seeded: dict[str, Any]) -> None:
    client = ApiClient(app)
    response = await client.login(username=seeded["username"], password=seeded["password"])
    assert response["token_type"] == "bearer"
    assert isinstance(response["access_token"], str)
    assert len(response["access_token"]) > 32


@pytest.mark.asyncio
async def test_login_bad_password_raises_api_error(app: FastAPI, seeded: dict[str, Any]) -> None:
    client = ApiClient(app)
    with pytest.raises(ApiError) as ei:
        await client.login(username=seeded["username"], password="wrong-password-1234")
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_create_user_with_capability_roles_via_client(
    app: FastAPI, seeded: dict[str, Any]
) -> None:
    """The UI client can create a user with capability roles and then replace them."""
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    client = ApiClient(app, token=auth["access_token"])

    created = await client.create_user(
        username="curatorx",
        email="curatorx@example.com",
        first_name="Cura",
        last_name="Tor",
        password="Curatorpass1!",
        role="regular_user",
        capability_roles=["materials_curator"],
    )
    assert created["role"] == "regular_user"
    assert created["capability_roles"] == ["materials_curator"]

    # Bulk-replace via the dedicated endpoint.
    updated = await client.set_user_capability_roles(
        int(created["id"]), role_names=["library_curator"]
    )
    assert updated["capability_roles"] == ["library_curator"]

    # And /me now surfaces the caller's permissions.
    me = await client.me()
    assert "roles.manage" in me["permissions"]


@pytest.mark.asyncio
async def test_list_tools_returns_installed(app: FastAPI, seeded: dict[str, Any]) -> None:
    auth_client = ApiClient(app)
    auth = await auth_client.login(username=seeded["username"], password=seeded["password"])
    client = ApiClient(app, token=auth["access_token"])
    tools = await client.list_tools()
    aliases = [t["alias"] for t in tools]
    assert seeded["tool_alias"] in aliases


@pytest.mark.asyncio
async def test_create_project_then_submit_job(app: FastAPI, seeded: dict[str, Any]) -> None:
    """End-to-end via the UI client: login → create project → create scenario → submit."""
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    client = ApiClient(app, token=auth["access_token"])

    project = await client.create_project(name="UI test project", description="from the client")
    assert project["name"] == "UI test project"

    scenario = await client.create_scenario(project["id"], name="UI test scenario", description="")
    assert scenario["project_id"] == project["id"]

    job = await client.submit_job(
        scenario["id"],
        name="from-ui",
        tool_alias=seeded["tool_alias"],
        inputs={
            "component_1": "Component A",
            "component_2": "Component A",
            "component_3": "Component A",
            "component_1_percent": 100,
            "component_2_percent": 0,
            "component_3_percent": 0,
        },
    )
    assert job["state"] == "queued"
    assert job["tool_alias"] == seeded["tool_alias"]

    fetched = await client.get_job(job["id"])
    assert fetched["id"] == job["id"]


@pytest.mark.asyncio
async def test_logout_revokes_token(app: FastAPI, seeded: dict[str, Any]) -> None:
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    client = ApiClient(app, token=auth["access_token"])
    await client.me()  # works pre-logout
    await client.logout()
    with pytest.raises(ApiError) as ei:
        await client.me()
    assert ei.value.status_code == 401
