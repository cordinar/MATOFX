"""HTTP tests for ``GET /api/workflows`` — the user-facing summary list.

Focus: the ``member_chain`` field that drives the Workflow Quick Access
card's flow-ordered tool list (labels, in ``position`` order, with an
arrow-vs-dot ``flows_from_prev`` flag per adjacent pair). The flag logic
is cross-checked against the ``/graph`` endpoint's inferred edges so the
assertions hold regardless of whether the example tools happen to
declare wiring between them.
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
    """Install + release a second example tool so we have ≥2 to chain."""
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
    for new_state in ("test", "released"):
        async with session_scope(factory) as session:
            await tool_service.promote_state(
                session, actor_user_id=actor_user_id, alias=alias, new_state=new_state
            )
    return alias


@pytest.mark.asyncio
async def test_summary_requires_auth(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/workflows")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_summary_member_chain_matches_positions_and_graph_edges(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit,  # noqa: ANN001
) -> None:
    """member_chain lists tools in position order, labelled, arrow-vs-dot.

    The card shows *which* tools a workflow runs and how their data
    flows — not a bare count. We assert:

    * one entry per member, in workflow ``position`` order;
    * labels are tool **display names** (not aliases);
    * the first entry never flows from a (non-existent) predecessor;
    * each subsequent ``flows_from_prev`` matches whether the
      descriptor-inferred graph has an edge between that adjacent pair.
    """
    second_alias = await _install_extra_tool(
        api_factory,
        api_audit,
        actor_user_id=seeded["user_id"],
        bundle_name="heat_transfer",
    )
    create = await client.post(
        "/api/admin/workflows",
        headers=auth_headers,
        json={
            "alias": "chain_smoke",
            "name": "Chain smoke",
            "description": "member_chain wiring test.",
            "teams": [],
            "tool_aliases": [seeded["tool_alias"], second_alias],
        },
    )
    assert create.status_code == 201, create.text
    workflow_id = create.json()["id"]

    # The summary list the home page renders from.
    listing = await client.get("/api/workflows", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    summary = next(w for w in listing.json() if w["alias"] == "chain_smoke")

    aliases = summary["tool_aliases"]
    chain = summary["member_chain"]
    assert len(chain) == len(aliases) == 2

    # Cross-reference the structure graph for the canonical names + edges.
    graph = (
        await client.get(f"/api/workflows/{workflow_id}/graph", headers=auth_headers)
    ).json()
    name_by_alias = {
        n["tool_alias"]: (n["label"].split(". ", 1)[1] if ". " in n["label"] else n["label"])
        for n in graph["nodes"]
    }
    edge_set = {(e["source"], e["target"]) for e in graph["edges"]}

    # Labels are display names, in position order — never aliases.
    assert [c["label"] for c in chain] == [name_by_alias[a] for a in aliases]
    assert all(c["label"] != a for c, a in zip(chain, aliases, strict=True) if a in name_by_alias)

    # First entry: nothing precedes it → dot (flows_from_prev False).
    assert chain[0]["flows_from_prev"] is False
    # Every subsequent separator matches the inferred edge set: arrow
    # when the pair is wired, dot when it isn't.
    for idx in range(1, len(chain)):
        expected = (aliases[idx - 1], aliases[idx]) in edge_set
        assert chain[idx]["flows_from_prev"] is expected

    # No archived members in this fixture.
    assert all(c["archived"] is False for c in chain)
