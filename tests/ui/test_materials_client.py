"""UI api_client coverage for the materials catalog surface (ADR-0007 §8).

NiceGUI page rendering isn't unit-tested; these exercise the materials
client methods the catalog + detail pages call, end-to-end against the
real app, plus the feature-flag behaviour the nav entry relies on.
Mirrors tests/ui/test_library_client.py. The publish gate is toggled off
so the seeded super-user (a curator) publishes directly.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from matofx.api.deps import get_settings
from matofx.config import FeatureSettings, Settings
from matofx.features import Feature
from matofx.ui._features import materials_database_enabled
from matofx.ui.api_client import ApiClient, ApiError


def _enable_feature(app: FastAPI, api_settings: Settings) -> None:
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.MATERIALS_DATABASE.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _set_gate(app: FastAPI, token: str, *, on: bool) -> None:
    """Toggle the ``materials_change_approval`` gate via the admin API."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://matofx") as c:
        resp = await c.post(
            "/api/admin/general-settings/materials_change_approval",
            headers={"Authorization": f"Bearer {token}"},
            json={"value": on},
        )
        assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_materials_client_end_to_end(
    app: FastAPI, api_settings: Settings, seeded: dict[str, Any]
) -> None:
    _enable_feature(app, api_settings)
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    token = str(auth["access_token"])
    client = ApiClient(app, token=token)
    await _set_gate(app, token, on=False)  # curator direct-publish

    cls = await client.create_material_class(name="Fluid", description="liquids + gases")
    cid = int(cls["id"])
    cls = await client.add_material_class_property(
        cid, prop_name="thermal_conductivity", unit="W/m/K", required=True
    )
    assert any(p["prop_name"] == "thermal_conductivity" for p in cls["properties"])

    detail = await client.create_material(
        body={
            "name": "Water",
            "class_id": cid,
            "description": "H2O",
            "tags": ["coolant"],
            "provenance": "handbook",
            "properties": [{"prop_name": "thermal_conductivity", "value": 0.6, "unit": "W/m/K"}],
        }
    )
    mid = int(detail["material"]["id"])
    assert detail["material"]["state"] == "active"
    assert detail["versions"][0]["publish_state"] == "published"

    listed = await client.list_materials()
    assert mid in [int(m["id"]) for m in listed]

    got = await client.get_material(mid)
    assert got["material"]["name"] == "Water"

    patched = await client.update_material(mid, body={"name": "Water (DI)"})
    assert patched["material"]["name"] == "Water (DI)"

    added = await client.add_material_version(
        mid,
        body={
            "provenance": "rev B",
            "properties": [{"prop_name": "thermal_conductivity", "value": 0.61, "unit": "W/m/K"}],
        },
    )
    assert [v["ordinal"] for v in added["versions"]] == [2, 1]

    picked = await client.materials_picker(material_class="Fluid")
    assert mid in [int(c["material_id"]) for c in picked]
    assert isinstance(await client.materials_approvers(), list)

    retired = await client.retire_material(mid)
    assert retired["state"] == "retired"


@pytest.mark.asyncio
async def test_materials_routes_absent_when_feature_off(
    app: FastAPI, seeded: dict[str, Any]
) -> None:
    """Default settings (flag off): the client gets 404 — the surface is
    absent, which is what hides the nav entry."""
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    client = ApiClient(app, token=str(auth["access_token"]))
    with pytest.raises(ApiError) as ei:
        await client.list_materials()
    assert ei.value.status_code == 404


def test_materials_helper_off_when_holder_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safe when the UI isn't mounted — returns False (suppressed)."""
    monkeypatch.setattr("matofx.ui._holder._app", None)
    assert materials_database_enabled() is False
