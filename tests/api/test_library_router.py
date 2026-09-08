"""API tests for the file-library router (ADR-0006 §7).

Covers feature-flag gating (routes *absent* when off), the
create / version / metadata / tool-assignment lifecycle, the global
publish gate (blocked without an approver while the
``library_global_publish_approval`` toggle is on; curator direct-publish
once it's off), optimistic locking, the picker (published files visible,
pending-global invisible), download, and the approver list. The
``seeded`` super-user is a library curator (sentinel) and can edit the
toggle.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import FastAPI

from matofx.api.deps import get_settings
from matofx.config import FeatureSettings, Settings
from matofx.db import session_scope
from matofx.features import Feature
from matofx.users import make_password_hasher, make_user_service
from matofx.users.role_service import make_role_service


def _enable_feature(app: FastAPI, api_settings: Settings) -> None:
    """Flip ``features.file_library`` on for this app instance."""
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.FILE_LIBRARY.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _set_approval(client: httpx.AsyncClient, headers: dict[str, str], *, on: bool) -> None:
    resp = await client.post(
        "/api/admin/general-settings/library_global_publish_approval",
        headers=headers,
        json={"value": on},
    )
    assert resp.status_code == 200, resp.text


async def _create_file(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    tool_alias: str,
    name: str = "Inlet geometry",
    scope: str = "personal",
    field_name: str | None = "geometry",
    filename: str = "inlet.stp",
    content: bytes = b"solid inlet",
    approver_user_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> httpx.Response:
    meta: dict[str, Any] = {
        "name": name,
        "description": "An inlet duct.",
        "provenance": "CAD export 2026-05",
        "scope": scope,
        "tool_assignments": [{"tool_alias": tool_alias, "field_name": field_name}],
    }
    if approver_user_id is not None:
        meta["approver_user_id"] = approver_user_id
    if extra:
        meta.update(extra)
    return await client.post(
        "/api/library/files",
        headers=headers,
        data={"metadata": json.dumps(meta)},
        files={"file": (filename, content, "application/octet-stream")},
    )


async def test_routes_absent_when_feature_off(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """With the flag off the library surface is *absent* (404)."""
    listing = await client.get("/api/library/files", headers=auth_headers)
    assert listing.status_code == 404
    picker = await client.get(
        "/api/library/picker", headers=auth_headers, params={"tool": "t", "field": "f"}
    )
    assert picker.status_code == 404


async def test_create_versions_metadata_and_download(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    alias = seeded["tool_alias"]

    created = await _create_file(client, auth_headers, tool_alias=alias)
    assert created.status_code == 201, created.text
    detail = created.json()
    file_id = detail["file"]["id"]
    assert detail["file"]["scope"] == "personal"
    assert detail["file"]["state"] == "active"
    assert len(detail["versions"]) == 1
    # Personal scope auto-publishes (no gate).
    assert detail["versions"][0]["publish_state"] == "published"
    assert detail["versions"][0]["ordinal"] == 1
    assert len(detail["tools"]) == 1
    assert detail["tools"][0]["tool_alias"] == alias

    # Append an immutable version.
    add = await client.post(
        f"/api/library/files/{file_id}/versions",
        headers=auth_headers,
        data={"metadata": json.dumps({"provenance": "CAD export rev B"})},
        files={"file": ("inlet_v2.stp", b"solid inlet v2", "application/octet-stream")},
    )
    assert add.status_code == 201, add.text
    versions = add.json()["versions"]
    assert [v["ordinal"] for v in versions] == [2, 1]
    v2_id = next(v["id"] for v in versions if v["ordinal"] == 2)

    # Edit entry metadata.
    patched = await client.patch(
        f"/api/library/files/{file_id}",
        headers=auth_headers,
        json={"name": "Inlet geometry (final)", "tags": ["cad", "inlet"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["file"]["name"] == "Inlet geometry (final)"
    assert patched.json()["file"]["tags"] == ["cad", "inlet"]

    # Download v2 → exact bytes back.
    dl = await client.get(
        f"/api/library/files/{file_id}/versions/{v2_id}/download", headers=auth_headers
    )
    assert dl.status_code == 200, dl.text
    assert dl.content == b"solid inlet v2"
    assert "inlet_v2.stp" in dl.headers["content-disposition"]


async def test_list_files_includes_tool_assignments(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """The browse list carries each file's tool assignments (for the cards)."""
    _enable_feature(app, api_settings)
    alias = seeded["tool_alias"]
    created = await _create_file(client, auth_headers, tool_alias=alias, field_name="geometry")
    assert created.status_code == 201, created.text
    file_id = created.json()["file"]["id"]

    listing = await client.get("/api/library/files", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    row = next(f for f in listing.json() if f["id"] == file_id)
    assert row["tools"] == [
        {"id": row["tools"][0]["id"], "tool_alias": alias, "field_name": "geometry"}
    ]


async def test_global_publish_gate(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    alias = seeded["tool_alias"]

    # Gate ON (default): landing in global with no approver is blocked.
    blocked = await _create_file(client, auth_headers, tool_alias=alias, scope="global")
    assert blocked.status_code == 400, blocked.text

    # Toggle the gate off → curator (sentinel super-user) publishes directly.
    await _set_approval(client, auth_headers, on=False)
    created = await _create_file(client, auth_headers, tool_alias=alias, scope="global")
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["file"]["scope"] == "global"
    assert body["file"]["state"] == "active"
    assert body["versions"][0]["publish_state"] == "published"


async def test_tool_assignment_last_one_guard(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    alias = seeded["tool_alias"]
    created = await _create_file(client, auth_headers, tool_alias=alias, field_name="geometry")
    file_id = created.json()["file"]["id"]

    # Assign a second mapping (same tool, different field).
    assigned = await client.post(
        f"/api/library/files/{file_id}/tools",
        headers=auth_headers,
        json={"tool_alias": alias, "field_name": "mesh"},
    )
    assert assigned.status_code == 201, assigned.text
    tools = assigned.json()["tools"]
    assert len(tools) == 2

    # Unassign one → fine.
    mesh_map_id = next(t["id"] for t in tools if t["field_name"] == "mesh")
    removed = await client.delete(
        f"/api/library/files/{file_id}/tools/{mesh_map_id}", headers=auth_headers
    )
    assert removed.status_code == 200, removed.text
    assert len(removed.json()["tools"]) == 1

    # Removing the last assignment is refused (every file stays pickable).
    # ``ToolAssignmentRequiredError`` → 400 (shared with create-without-tool).
    last_map_id = removed.json()["tools"][0]["id"]
    refused = await client.delete(
        f"/api/library/files/{file_id}/tools/{last_map_id}", headers=auth_headers
    )
    assert refused.status_code == 400, refused.text


async def test_optimistic_lock(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    alias = seeded["tool_alias"]
    created = await _create_file(client, auth_headers, tool_alias=alias)
    file_id = created.json()["file"]["id"]
    current_version = created.json()["file"]["version"]

    # A stale expected_version is rejected.
    stale = await client.patch(
        f"/api/library/files/{file_id}",
        headers=auth_headers,
        json={"name": "x", "expected_version": current_version + 5},
    )
    assert stale.status_code == 409, stale.text


async def test_picker_shows_published_hides_pending(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    alias = seeded["tool_alias"]

    # A published personal file is pickable by its owner.
    pub = await _create_file(
        client, auth_headers, tool_alias=alias, name="Pickable", field_name="geometry"
    )
    assert pub.status_code == 201, pub.text
    pub_id = pub.json()["file"]["id"]

    picked = await client.get(
        "/api/library/picker",
        headers=auth_headers,
        params={"tool": alias, "field": "geometry"},
    )
    assert picked.status_code == 200, picked.text
    ids = [c["file_id"] for c in picked.json()]
    assert pub_id in ids

    # A pending global file (gate on + approver) is NOT in the picker.
    rev_id = await _seed_curator(app, api_settings, seeded)
    pending = await _create_file(
        client,
        auth_headers,
        tool_alias=alias,
        name="Pending global",
        scope="global",
        field_name="geometry",
        approver_user_id=rev_id,
    )
    assert pending.status_code == 201, pending.text
    assert pending.json()["versions"][0]["publish_state"] == "pending"
    pending_id = pending.json()["file"]["id"]

    picked2 = await client.get(
        "/api/library/picker",
        headers=auth_headers,
        params={"tool": alias, "field": "geometry"},
    )
    assert pending_id not in [c["file_id"] for c in picked2.json()]


async def test_approvers_excludes_self(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    rev_id = await _seed_curator(app, api_settings, seeded)
    resp = await client.get(
        "/api/library/approvers", headers=auth_headers, params={"scope": "global"}
    )
    assert resp.status_code == 200, resp.text
    by_id = {o["user_id"]: o for o in resp.json()}
    assert rev_id in by_id  # the curator is offered
    assert int(seeded["user_id"]) not in by_id  # never offer self


async def _seed_curator(app: FastAPI, api_settings: Settings, seeded: dict[str, Any]) -> int:
    """Create a 'rev' regular user holding the ``library_curator`` capability."""
    audit = app.state.audit
    factory = app.state.factory
    hasher = make_password_hasher(api_settings.auth)
    user_service = make_user_service(api_settings.auth, audit, hasher=hasher)
    role_service = make_role_service(audit)
    async with session_scope(factory) as session:
        rev = await user_service.create_user(
            session,
            username="rev",
            email="rev@example.com",
            password="revpassword1",
            full_name="Rev Curator",
            role="regular_user",
        )
        await role_service.set_user_capability_roles(
            session,
            actor_user_id=int(seeded["user_id"]),
            target_user_id=rev.id,
            role_names=["library_curator"],
        )
        return int(rev.id)
