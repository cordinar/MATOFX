"""HTTP smoke tests for the /api/workflows/custom* endpoints.

The unit-level service tests already cover the business logic
exhaustively (see ``tests/workflows/test_*.py``). The point of these
tests is to verify the wiring: schemas serialise correctly, error
mapping picks the right status code, and the routes are registered on
the app.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from matofx.audit import ensure_chain_head
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.tools import make_tool_service
from matofx.tools.loader import install_from_path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE_TOOLS = _REPO_ROOT / "example_tools"


async def _install_extra_tool(
    factory: SessionFactory,
    audit,  # noqa: ANN001
    actor_user_id: int,
    bundle_name: str,
) -> str:
    """Install + release a second example tool so we have ≥2 to play with."""
    tool_service = make_tool_service(audit)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        installed = await install_from_path(
            session,
            tool_service,
            actor_user_id=actor_user_id,
            bundle_dir=_EXAMPLE_TOOLS / bundle_name,
        )
        alias = installed.alias
    async with session_scope(factory) as session:
        await tool_service.promote_state(
            session, actor_user_id=actor_user_id, alias=alias, new_state="test"
        )
    async with session_scope(factory) as session:
        await tool_service.promote_state(
            session, actor_user_id=actor_user_id, alias=alias, new_state="released"
        )
    return alias


@pytest.mark.asyncio
async def test_list_custom_workflows_requires_auth(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/api/workflows/custom")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_custom_workflow_too_few_members(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """One-tool request is rejected — Pydantic enforces min_length=2."""
    r = await client.post(
        "/api/workflows/custom",
        headers=auth_headers,
        json={
            "name": "Lonely",
            "description": "",
            "tool_aliases": [seeded["tool_alias"]],
        },
    )
    # Pydantic catches it first → 422.
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_then_list_then_archive_round_trip(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit,  # noqa: ANN001
) -> None:
    """End-to-end: create with two tools → list shows it → archive flips state."""
    second_alias = await _install_extra_tool(
        api_factory,
        api_audit,
        actor_user_id=seeded["user_id"],
        bundle_name="heat_transfer",
    )

    create = await client.post(
        "/api/workflows/custom",
        headers=auth_headers,
        json={
            "name": "Smoke pair",
            "description": "API round-trip test.",
            "tool_aliases": [seeded["tool_alias"], second_alias],
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["name"] == "Smoke pair"
    assert body["visibility"] == "private"
    assert body["is_owner"] is True
    assert [m["tool_alias"] for m in body["members"]] == [
        seeded["tool_alias"],
        second_alias,
    ] or [m["tool_alias"] for m in body["members"]] == [
        second_alias,
        seeded["tool_alias"],
    ]
    workflow_id = body["id"]

    listing = await client.get("/api/workflows/custom", headers=auth_headers)
    assert listing.status_code == 200
    rows = listing.json()
    ids = [row["id"] for row in rows]
    assert workflow_id in ids

    archive = await client.post(
        f"/api/workflows/custom/{workflow_id}/archive",
        headers=auth_headers,
        json={"reason": ""},
    )
    assert archive.status_code == 200, archive.text
    assert archive.json()["archived_at"] is not None

    # Default listing drops archived rows.
    re_listing = await client.get("/api/workflows/custom", headers=auth_headers)
    assert workflow_id not in [row["id"] for row in re_listing.json()]

    # Opt-in flag brings them back.
    re_listing_all = await client.get(
        "/api/workflows/custom?include_archived=true", headers=auth_headers
    )
    assert workflow_id in [row["id"] for row in re_listing_all.json()]


@pytest.mark.asyncio
async def test_get_unknown_workflow_is_404(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    r = await client.get("/api/workflows/custom/999999", headers=auth_headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_update_round_trip(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit,  # noqa: ANN001
) -> None:
    """Create → update name → confirm new name and bumped version."""
    second_alias = await _install_extra_tool(
        api_factory,
        api_audit,
        actor_user_id=seeded["user_id"],
        bundle_name="heat_transfer",
    )
    create = await client.post(
        "/api/workflows/custom",
        headers=auth_headers,
        json={
            "name": "Before",
            "description": "",
            "tool_aliases": [seeded["tool_alias"], second_alias],
        },
    )
    workflow_id = create.json()["id"]
    initial_version = create.json()["version"]

    update = await client.post(
        f"/api/workflows/custom/{workflow_id}/update",
        headers=auth_headers,
        json={
            "name": "After",
            "description": "",
            "tool_aliases": [seeded["tool_alias"], second_alias],
        },
    )
    assert update.status_code == 200, update.text
    body = update.json()
    assert body["name"] == "After"
    assert body["version"] != initial_version


@pytest.mark.asyncio
async def test_admin_list_endpoint_returns_every_row(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit,  # noqa: ANN001
) -> None:
    """Inspector listing returns every custom workflow (any owner).

    Seeded user is super_user, so the inspect permission resolves via
    the catalog sentinel.
    """
    second_alias = await _install_extra_tool(
        api_factory,
        api_audit,
        actor_user_id=seeded["user_id"],
        bundle_name="heat_transfer",
    )
    create = await client.post(
        "/api/workflows/custom",
        headers=auth_headers,
        json={
            "name": "Inspector visible",
            "description": "",
            "tool_aliases": [seeded["tool_alias"], second_alias],
        },
    )
    wf_id = create.json()["id"]

    admin_list = await client.get("/api/admin/custom-workflows", headers=auth_headers)
    assert admin_list.status_code == 200, admin_list.text
    assert wf_id in [row["id"] for row in admin_list.json()]


@pytest.mark.asyncio
async def test_copy_round_trip(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit,  # noqa: ANN001
) -> None:
    """Create → copy → confirm new id with '(copy)' name."""
    second_alias = await _install_extra_tool(
        api_factory,
        api_audit,
        actor_user_id=seeded["user_id"],
        bundle_name="heat_transfer",
    )
    create = await client.post(
        "/api/workflows/custom",
        headers=auth_headers,
        json={
            "name": "Original",
            "description": "",
            "tool_aliases": [seeded["tool_alias"], second_alias],
        },
    )
    workflow_id = create.json()["id"]

    copy = await client.post(
        f"/api/workflows/custom/{workflow_id}/copy",
        headers=auth_headers,
    )
    assert copy.status_code == 201, copy.text
    body = copy.json()
    assert body["id"] != workflow_id
    assert body["name"] == "Original (copy)"
    assert body["copied_from_workflow_id"] == workflow_id
