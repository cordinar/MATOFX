"""HTTP smoke tests for ``GET /api/workflows/{id}/graph`` (ADR-0013).

The unit-level service tests
(``tests/workflows/test_graph_service.py``) cover the projection and
visibility logic. These tests verify the wiring: schemas serialise
correctly, route auth is enforced, and the error contract maps the
expected status codes.
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
async def test_graph_endpoint_requires_auth(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/workflows/1/graph")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_graph_endpoint_unknown_workflow_is_404(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    r = await client.get("/api/workflows/999999/graph", headers=auth_headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_graph_endpoint_returns_nodes_and_edges_for_visible_workflow(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit,  # noqa: ANN001
) -> None:
    """Create a 2-tool custom workflow → /graph returns matching nodes + payload."""
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
            "name": "Graph smoke",
            "description": "",
            "tool_aliases": [seeded["tool_alias"], second_alias],
        },
    )
    assert create.status_code == 201, create.text
    workflow_id = create.json()["id"]

    r = await client.get(f"/api/workflows/{workflow_id}/graph", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # Wire-shape sanity: every contract field exists.
    assert body["workflow_id"] == workflow_id
    assert body["workflow_name"] == "Graph smoke"
    assert body["run_id"] is None
    assert body["run_state"] is None
    # Both members surface, with positions 1 + 2 and no run_state in
    # structure-only mode.
    aliases = {n["tool_alias"] for n in body["nodes"]}
    assert aliases == {seeded["tool_alias"], second_alias}
    positions = sorted(n["position"] for n in body["nodes"])
    assert positions == [1, 2]
    assert all(n["run_state"] is None for n in body["nodes"])
    assert all(n["child_job_id"] is None for n in body["nodes"])
    # Edges shape: each edge has source / target / field_labels. The
    # actual edge count depends on whether the example tools declare
    # any wiring between them — both shapes are valid here.
    for edge in body["edges"]:
        assert isinstance(edge["source"], str)
        assert isinstance(edge["target"], str)
        assert isinstance(edge["field_labels"], list)


@pytest.mark.asyncio
async def test_preview_endpoint_requires_auth(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/workflows/graph/preview", json={"tool_aliases": ["x"]})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_preview_endpoint_empty_aliases_rejected(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """Pydantic catches the empty list before the service sees it."""
    del seeded
    r = await client.post(
        "/api/workflows/graph/preview", headers=auth_headers, json={"tool_aliases": []}
    )
    assert r.status_code == 422  # min_length=1 violation


@pytest.mark.asyncio
async def test_preview_endpoint_returns_ephemeral_graph(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit,  # noqa: ANN001
) -> None:
    """Preview against two visible tools → graph with workflow_id=None."""
    second_alias = await _install_extra_tool(
        api_factory,
        api_audit,
        actor_user_id=seeded["user_id"],
        bundle_name="heat_transfer",
    )
    r = await client.post(
        "/api/workflows/graph/preview",
        headers=auth_headers,
        json={"tool_aliases": [seeded["tool_alias"], second_alias]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workflow_id"] is None
    assert body["run_id"] is None
    assert {n["tool_alias"] for n in body["nodes"]} == {seeded["tool_alias"], second_alias}
    assert sorted(n["position"] for n in body["nodes"]) == [1, 2]


@pytest.mark.asyncio
async def test_preview_endpoint_unknown_alias_is_400(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """An alias the DB doesn't know about → 400 (UnknownToolMemberError)."""
    r = await client.post(
        "/api/workflows/graph/preview",
        headers=auth_headers,
        json={"tool_aliases": [seeded["tool_alias"], "ghost_alias_xyz"]},
    )
    assert r.status_code == 400
