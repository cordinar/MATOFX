"""Admin-router tests: storage_locations + python_envs + runtime_settings."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

# --- storage locations -----------------------------------------------


@pytest.mark.asyncio
async def test_list_storage_locations_requires_auth(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/admin/storage-locations")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_storage_locations_includes_seeded(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    r = await client.get("/api/admin/storage-locations", headers=auth_headers)
    assert r.status_code == 200
    names = [row["name"] for row in r.json()]
    assert "local" in names


@pytest.mark.asyncio
async def test_create_storage_location_round_trip(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    create = await client.post(
        "/api/admin/storage-locations",
        headers=auth_headers,
        json={"name": "fsx-prod", "mount_path": "/mnt/fsx-prod"},
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["name"] == "fsx-prod"
    assert body["mount_path"] == "/mnt/fsx-prod"
    assert body["is_active"] is True

    listing = await client.get("/api/admin/storage-locations", headers=auth_headers)
    assert "fsx-prod" in [r["name"] for r in listing.json()]


@pytest.mark.asyncio
async def test_create_storage_location_duplicate_name(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    body = {"name": "scratch", "mount_path": "/mnt/scratch"}
    first = await client.post("/api/admin/storage-locations", headers=auth_headers, json=body)
    assert first.status_code == 201
    second = await client.post("/api/admin/storage-locations", headers=auth_headers, json=body)
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_retire_storage_location(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    create = await client.post(
        "/api/admin/storage-locations",
        headers=auth_headers,
        json={"name": "old", "mount_path": "/mnt/old"},
    )
    row_id = create.json()["id"]
    retire = await client.post(
        f"/api/admin/storage-locations/{row_id}/retire", headers=auth_headers
    )
    assert retire.status_code == 200
    body = retire.json()
    assert body["is_active"] is False
    assert body["retired_at"] is not None


# --- python envs -----------------------------------------------------


@pytest.mark.asyncio
async def test_python_envs_create_list_deactivate(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    storage_id = seeded["storage_location_id"]

    create = await client.post(
        "/api/admin/python-envs",
        headers=auth_headers,
        json={
            "name": "py312",
            "interpreter_path": "/opt/envs/py312/bin/python",
            "storage_location_id": storage_id,
        },
    )
    assert create.status_code == 201, create.text
    env_id = create.json()["id"]
    assert create.json()["is_active"] is True
    assert create.json()["storage_location_id"] == storage_id

    listing = await client.get("/api/admin/python-envs", headers=auth_headers)
    assert listing.status_code == 200
    assert "py312" in [r["name"] for r in listing.json()]

    deact = await client.post(f"/api/admin/python-envs/{env_id}/deactivate", headers=auth_headers)
    assert deact.status_code == 200
    assert deact.json()["is_active"] is False


@pytest.mark.asyncio
async def test_python_env_duplicate_name(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    body = {"name": "shared", "interpreter_path": "/opt/python"}
    first = await client.post("/api/admin/python-envs", headers=auth_headers, json=body)
    assert first.status_code == 201
    second = await client.post("/api/admin/python-envs", headers=auth_headers, json=body)
    assert second.status_code == 409


# --- runtime settings -----------------------------------------------


@pytest.mark.asyncio
async def test_get_runtime_settings_returns_all_catalog_keys(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    r = await client.get("/api/admin/runtime-settings", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "settings" in body
    keys = set(body["settings"].keys())
    # All catalog keys should be present (the loader fills defaults).
    assert {
        "local_concurrency",
        "heartbeat_local_aos_interval_s",
        "cancel_grace_s",
    } <= keys


@pytest.mark.asyncio
async def test_set_runtime_setting_then_reset(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    set_resp = await client.post(
        "/api/admin/runtime-settings/local_concurrency",
        headers=auth_headers,
        json={"value": 8},
    )
    assert set_resp.status_code == 200, set_resp.text
    assert set_resp.json()["settings"]["local_concurrency"] == 8

    reset = await client.post(
        "/api/admin/runtime-settings/local_concurrency/reset", headers=auth_headers
    )
    assert reset.status_code == 200
    # Default is 2 per the catalog.
    assert reset.json()["settings"]["local_concurrency"] == 2


@pytest.mark.asyncio
async def test_set_runtime_setting_unknown_key_400(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    r = await client.post(
        "/api/admin/runtime-settings/no_such_key",
        headers=auth_headers,
        json={"value": 1},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_set_runtime_setting_out_of_range_400(
    client: httpx.AsyncClient, seeded: dict[str, Any], auth_headers: dict[str, str]
) -> None:
    del seeded
    r = await client.post(
        "/api/admin/runtime-settings/local_concurrency",
        headers=auth_headers,
        json={"value": 99999},
    )
    assert r.status_code == 400
