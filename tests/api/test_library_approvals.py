"""End-to-end library approval flow through the approvals router (ADR-0006 §5).

The key behaviour mirrors the materials gate: a *library curator* who is
neither an admin nor a tool-approver (just holds the ``library_curator``
capability) can see a pending global-library publish in their inbox and
approve it — eligibility for a tool-less gate is the curate capability,
not the tool-approver flag (ADR-0005 / ADR-0006 §5).
"""

from __future__ import annotations

import json
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
        update={"features": FeatureSettings(enabled=[Feature.FILE_LIBRARY.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _seed_curator(
    api_factory: SessionFactory, api_settings: Settings, api_audit: AuditWriter, *, actor_id: int
) -> int:
    """Create 'rev' (regular user) + grant the library_curator capability."""
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
            role_names=["library_curator"],
        )
        return int(rev.id)


async def _login(client: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    resp = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_library_gate_visible_and_approvable_by_curator(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    api_audit: AuditWriter,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    alias = seeded["tool_alias"]
    rev_id = await _seed_curator(
        api_factory, api_settings, api_audit, actor_id=int(seeded["user_id"])
    )
    rev_headers = await _login(client, "rev", _REV_PASSWORD)

    # root creates a global library file naming rev as the approver
    # (the publish-approval toggle is ON by default).
    meta = {
        "name": "Standard inlet",
        "description": "Shared geometry.",
        "provenance": "CAD library",
        "scope": "global",
        "tool_assignments": [{"tool_alias": alias, "field_name": "geometry"}],
        "approver_user_id": rev_id,
    }
    created = await client.post(
        "/api/library/files",
        headers=auth_headers,
        data={"metadata": json.dumps(meta)},
        files={"file": ("inlet.stp", b"solid inlet", "application/octet-stream")},
    )
    assert created.status_code == 201, created.text
    file_id = created.json()["file"]["id"]
    version_id = created.json()["versions"][0]["id"]
    assert created.json()["file"]["state"] == "pending"
    assert created.json()["versions"][0]["publish_state"] == "pending"

    # rev (curator, not admin, not tool-approver) sees the gate in their inbox.
    inbox = await client.get("/api/users/me/approvals", headers=rev_headers)
    assert inbox.status_code == 200, inbox.text
    rows = inbox.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["target_kind"] == "library"
    assert row["tool_alias"] is None
    request_id = row["request_id"]

    # The detail panel renders the library target.
    detail = await client.get(f"/api/approvals/{request_id}", headers=rev_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["target_kind"] == "library"

    # rev approves → the file publishes + goes active.
    decision = await client.post(
        "/api/approvals/decisions",
        headers=rev_headers,
        json={"decisions": [{"request_id": request_id, "decision": "approve"}]},
    )
    assert decision.status_code == 200, decision.text
    result = decision.json()["results"][0]
    assert result["ok"] is True
    assert result["job_state"] == "published"

    # The file is now active + its version published + pickable.
    got = await client.get(f"/api/library/files/{file_id}", headers=auth_headers)
    assert got.json()["file"]["state"] == "active"
    published = next(v for v in got.json()["versions"] if v["id"] == version_id)
    assert published["publish_state"] == "published"

    picked = await client.get(
        "/api/library/picker",
        headers=auth_headers,
        params={"tool": alias, "field": "geometry"},
    )
    assert file_id in [c["file_id"] for c in picked.json()]
