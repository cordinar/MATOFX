"""API tests for the materials router (ADR-0007 §6).

Covers the feature-flag gating (routes *absent* when off), the
class-schema + material lifecycle happy path, the picker, and the
approval gate (create without an approver is blocked while the
``materials_change_approval`` toggle is on; direct-publish once it's
off). The ``seeded`` super-user is a materials curator (sentinel) and
can edit the toggle.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI

from matofx.api.deps import get_settings
from matofx.config import FeatureSettings, Settings
from matofx.features import Feature


def _enable_feature(app: FastAPI, api_settings: Settings) -> None:
    """Flip ``features.materials_database`` on for this app instance."""
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.MATERIALS_DATABASE.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _fluid_class(client: httpx.AsyncClient, headers: dict[str, str]) -> int:
    """Create a 'Fluid' class requiring thermal_conductivity; return its id."""
    created = await client.post(
        "/api/materials/classes",
        headers=headers,
        json={"name": "Fluid", "description": "liquids + gases"},
    )
    assert created.status_code == 201, created.text
    class_id = int(created.json()["id"])
    prop = await client.post(
        f"/api/materials/classes/{class_id}/properties",
        headers=headers,
        json={"prop_name": "thermal_conductivity", "unit": "W/m/K", "required": True},
    )
    assert prop.status_code == 201, prop.text
    return class_id


async def _set_approval(client: httpx.AsyncClient, headers: dict[str, str], *, on: bool) -> None:
    resp = await client.post(
        "/api/admin/general-settings/materials_change_approval",
        headers=headers,
        json={"value": on},
    )
    assert resp.status_code == 200, resp.text


async def test_routes_absent_when_feature_off(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """With the flag off the materials surface is *absent* (404)."""
    listing = await client.get("/api/materials", headers=auth_headers)
    assert listing.status_code == 404
    classes = await client.get("/api/materials/classes", headers=auth_headers)
    assert classes.status_code == 404


async def test_class_material_lifecycle_and_picker(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    del seeded
    class_id = await _fluid_class(client, auth_headers)

    # Gate ON (default): creating without an approver is blocked.
    blocked = await client.post(
        "/api/materials",
        headers=auth_headers,
        json={
            "name": "Water",
            "class_id": class_id,
            "provenance": "handbook",
            "properties": [{"prop_name": "thermal_conductivity", "value": 0.6, "unit": "W/m/K"}],
        },
    )
    assert blocked.status_code == 400, blocked.text

    # Turn the gate off → curator publishes directly.
    await _set_approval(client, auth_headers, on=False)
    created = await client.post(
        "/api/materials",
        headers=auth_headers,
        json={
            "name": "Water",
            "class_id": class_id,
            "description": "H2O",
            "tags": ["coolant"],
            "provenance": "handbook",
            "properties": [
                {"prop_name": "thermal_conductivity", "value": 0.6, "unit": "W/m/K"},
                {"prop_name": "density", "value": 998.0, "unit": "kg/m3"},
            ],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    material_id = body["material"]["id"]
    assert body["material"]["class_name"] == "Fluid"
    assert body["material"]["state"] == "active"
    assert body["versions"][0]["publish_state"] == "published"

    # Detail + list.
    detail = await client.get(f"/api/materials/{material_id}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["material"]["name"] == "Water"
    listing = await client.get("/api/materials?tag=coolant", headers=auth_headers)
    assert listing.status_code == 200
    assert [m["id"] for m in listing.json()] == [material_id]

    # Picker offers the published material for its class.
    picker = await client.get(
        "/api/materials/picker?class=Fluid&required_props=thermal_conductivity",
        headers=auth_headers,
    )
    assert picker.status_code == 200, picker.text
    candidates = picker.json()
    assert [c["material_id"] for c in candidates] == [material_id]
    assert candidates[0]["properties"]["thermal_conductivity"]["value"] == 0.6


async def test_create_material_missing_required_property_is_400(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    del seeded
    class_id = await _fluid_class(client, auth_headers)
    await _set_approval(client, auth_headers, on=False)
    resp = await client.post(
        "/api/materials",
        headers=auth_headers,
        json={
            "name": "Air",
            "class_id": class_id,
            "provenance": "handbook",
            "properties": [{"prop_name": "density", "value": 1.2, "unit": "kg/m3"}],
        },
    )
    assert resp.status_code == 400, resp.text
