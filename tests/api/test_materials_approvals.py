"""End-to-end materials approval flow through the approvals router (slice 8a).

The key behaviour: a *materials curator* who is neither an admin nor a
tool-approver (just holds the ``materials_curator`` capability) can see a
material gate in their inbox and approve it — eligibility for a tool-less
gate is the curate capability, not the tool-approver flag (ADR-0007 §5).
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI

from matofx.api.deps import get_settings
from matofx.audit import AuditWriter
from matofx.config import FeatureSettings, Settings
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.features import Feature
from matofx.users import make_password_hasher, make_user_service
from matofx.users.role_service import make_role_service

_REV_PASSWORD = "revpassword1"


def _enable_feature(app: FastAPI, api_settings: Settings) -> None:
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.MATERIALS_DATABASE.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _seed_curator(
    api_factory: SessionFactory, api_settings: Settings, api_audit: AuditWriter, *, actor_id: int
) -> int:
    """Create 'rev' (regular user) + grant the materials_curator capability."""
    hasher = make_password_hasher(api_settings.auth)
    user_service = make_user_service(api_settings.auth, api_audit, hasher=hasher)
    role_service = make_role_service(api_audit)
    async with session_scope(api_factory) as session:
        rev = await user_service.create_user(
            session,
            username="rev",
            email="rev@example.com",
            password=_REV_PASSWORD,
            full_name="Rev Curator",
            role="regular_user",
        )
        await role_service.set_user_capability_roles(
            session,
            actor_user_id=actor_id,
            target_user_id=rev.id,
            role_names=["materials_curator"],
        )
        return rev.id


async def _login(client: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    resp = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_material_gate_visible_and_approvable_by_curator(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    api_audit: AuditWriter,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    rev_id = await _seed_curator(
        api_factory, api_settings, api_audit, actor_id=int(seeded["user_id"])
    )
    rev_headers = await _login(client, "rev", _REV_PASSWORD)

    # root creates a Fluid class + a gated material naming rev as approver.
    created = await client.post(
        "/api/materials/classes",
        headers=auth_headers,
        json={"name": "Fluid", "description": ""},
    )
    assert created.status_code == 201, created.text
    class_id = created.json()["id"]
    await client.post(
        f"/api/materials/classes/{class_id}/properties",
        headers=auth_headers,
        json={"prop_name": "thermal_conductivity", "unit": "W/m/K", "required": True},
    )
    material = await client.post(
        "/api/materials",
        headers=auth_headers,
        json={
            "name": "Water",
            "class_id": class_id,
            "provenance": "handbook",
            "properties": [{"prop_name": "thermal_conductivity", "value": 0.6, "unit": "W/m/K"}],
            "approver_user_id": rev_id,
        },
    )
    assert material.status_code == 201, material.text
    material_id = material.json()["material"]["id"]
    assert material.json()["material"]["state"] == "pending"

    # rev (curator, not admin, not tool-approver) sees the gate in their inbox.
    inbox = await client.get("/api/users/me/approvals", headers=rev_headers)
    assert inbox.status_code == 200, inbox.text
    rows = inbox.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["target_kind"] == "material"
    assert row["tool_alias"] is None
    request_id = row["request_id"]

    # The detail panel renders the material + its properties.
    detail = await client.get(f"/api/approvals/{request_id}", headers=rev_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["target_kind"] == "material"

    # rev approves → the material publishes + goes active.
    decision = await client.post(
        "/api/approvals/decisions",
        headers=rev_headers,
        json={"decisions": [{"request_id": request_id, "decision": "approve"}]},
    )
    assert decision.status_code == 200, decision.text
    result = decision.json()["results"][0]
    assert result["ok"] is True
    assert result["job_state"] == "published"

    got = await client.get(f"/api/materials/{material_id}", headers=auth_headers)
    assert got.json()["material"]["state"] == "active"
    assert got.json()["versions"][0]["publish_state"] == "published"
