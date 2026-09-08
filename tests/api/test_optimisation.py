"""API tests for the optimisation-studies router (ADR-0009 §15).

Covers the feature-flag gating (routes *absent* when off), the
create/update/get/list/archive happy path, a halt on a draft (no-op),
and central error mapping (configuring an incomplete draft surfaces a
clean plain-English 400, not a 500).
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from matofx.api.deps import get_settings
from matofx.config import FeatureSettings, Settings
from matofx.db import SessionFactory, session_scope
from matofx.features import Feature
from matofx.tools.orm import ToolVersion
from matofx.users.orm import User
from matofx.workflows.orm import Workflow


def _enable_feature(app: FastAPI, api_settings: Settings) -> None:
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.OPTIMISATION_FRAMEWORK.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _make_project(client: httpx.AsyncClient, headers: dict[str, str]) -> int:
    resp = await client.post(
        "/api/projects", headers=headers, json={"name": "Opt P", "description": ""}
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def _tool_version_id(api_factory: SessionFactory, tool_alias: str) -> int:
    async with session_scope(api_factory) as session:
        vid = (
            await session.execute(
                select(ToolVersion.id)
                .where(ToolVersion.alias == tool_alias)
                .order_by(ToolVersion.created_at.desc(), ToolVersion.id.desc())
                .limit(1)
            )
        ).scalar_one()
        return int(vid)


async def test_routes_absent_when_feature_off(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    create = await client.post(
        "/api/projects/1/optimisation-studies",
        headers=auth_headers,
        json={"name": "x", "tool_alias": "t", "tool_version_id": 1},
    )
    assert create.status_code == 404
    listing = await client.get("/api/projects/1/optimisation-studies", headers=auth_headers)
    assert listing.status_code == 404


async def test_create_update_get_list_archive(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    pid = await _make_project(client, auth_headers)
    vid = await _tool_version_id(api_factory, seeded["tool_alias"])

    created = await client.post(
        f"/api/projects/{pid}/optimisation-studies",
        headers=auth_headers,
        json={
            "name": "  Lightweighting  ",
            "tool_alias": seeded["tool_alias"],
            "tool_version_id": vid,
        },
    )
    assert created.status_code == 201, created.text
    study = created.json()
    sid = study["id"]
    assert study["name"] == "Lightweighting"
    assert study["state"] == "draft"
    assert study["config_json"]["target"]["tool_alias"] == seeded["tool_alias"]

    patched = await client.patch(
        f"/api/optimisation-studies/{sid}",
        headers=auth_headers,
        json={"config_patch": {"strategy": "bo", "acquisition": "balanced", "batch_size": 4}},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["config_json"]["strategy"] == "bo"

    got = await client.get(f"/api/optimisation-studies/{sid}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["id"] == sid
    # Read paths are enriched with display fields (labels, not aliases/ids).
    assert got.json()["tool_name"]  # tool display label resolved
    assert got.json()["tool_version"]  # pinned version string, not just the id
    assert got.json()["project_name"]
    assert got.json()["created_by_name"]

    listing = await client.get(f"/api/projects/{pid}/optimisation-studies", headers=auth_headers)
    assert listing.status_code == 200
    listed = next(s for s in listing.json() if s["id"] == sid)
    assert listed["tool_name"]
    assert listed["created_by_name"]

    archived = await client.post(f"/api/optimisation-studies/{sid}/archive", headers=auth_headers)
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    after = await client.get(f"/api/projects/{pid}/optimisation-studies", headers=auth_headers)
    assert sid not in {s["id"] for s in after.json()}


async def test_halt_on_draft_is_noop(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    pid = await _make_project(client, auth_headers)
    vid = await _tool_version_id(api_factory, seeded["tool_alias"])
    created = await client.post(
        f"/api/projects/{pid}/optimisation-studies",
        headers=auth_headers,
        json={"name": "h", "tool_alias": seeded["tool_alias"], "tool_version_id": vid},
    )
    sid = created.json()["id"]
    halted = await client.post(f"/api/optimisation-studies/{sid}/halt", headers=auth_headers)
    assert halted.status_code == 200
    # A draft isn't running, so halt is a no-op (flag stays off).
    assert halted.json()["halt_requested"] is False


async def _make_workflow(api_factory: SessionFactory) -> str:
    """Insert a minimal admin workflow row directly; return its alias."""
    async with session_scope(api_factory) as session:
        uid = (await session.execute(select(User.id).limit(1))).scalar_one()
        session.add(
            Workflow(
                alias="wf_opt",
                name="WF Opt",
                version="1.0.0",
                descriptor={"description": ""},
                installed_by_user_id=int(uid),
            )
        )
    return "wf_opt"


async def test_create_workflow_target_study(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """A workflow-target draft is minted with target_kind='workflow' (§3.4)."""
    _enable_feature(app, api_settings)
    pid = await _make_project(client, auth_headers)
    alias = await _make_workflow(api_factory)
    created = await client.post(
        f"/api/projects/{pid}/optimisation-studies/workflow",
        headers=auth_headers,
        json={"name": "Chain opt", "workflow_alias": alias, "workflow_version_id": 1},
    )
    assert created.status_code == 201, created.text
    study = created.json()
    assert study["target_kind"] == "workflow"
    assert study["workflow_alias"] == alias
    assert study["tool_alias"] is None
    assert study["state"] == "draft"


async def test_recommendation_reflects_saved_config(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """The Navigator reads the draft's saved responses to pick a strategy (§5.6)."""
    _enable_feature(app, api_settings)
    pid = await _make_project(client, auth_headers)
    vid = await _tool_version_id(api_factory, seeded["tool_alias"])
    created = await client.post(
        f"/api/projects/{pid}/optimisation-studies",
        headers=auth_headers,
        json={"name": "nav", "tool_alias": seeded["tool_alias"], "tool_version_id": vid},
    )
    sid = created.json()["id"]
    # A fresh draft (no responses) → the single-objective default.
    first = await client.get(
        f"/api/optimisation-studies/{sid}/recommendation", headers=auth_headers
    )
    assert first.status_code == 200, first.text
    assert first.json()["strategy"] == "bo"
    assert first.json()["rationale"]

    # Saving two optimised responses flips the pick to a multi-objective EA.
    await client.patch(
        f"/api/optimisation-studies/{sid}",
        headers=auth_headers,
        json={
            "config_patch": {
                "responses": [
                    {"name": "a", "spec": {"kind": "scalar"}, "goal": "minimise"},
                    {"name": "b", "spec": {"kind": "scalar"}, "goal": "maximise"},
                ]
            }
        },
    )
    second = await client.get(
        f"/api/optimisation-studies/{sid}/recommendation", headers=auth_headers
    )
    assert second.json()["strategy"] == "nsga2"


async def test_cost_estimate_and_approvers_endpoints(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """The start-gate read endpoints: campaign footprint + the approver pool (§14.1)."""
    _enable_feature(app, api_settings)
    pid = await _make_project(client, auth_headers)
    vid = await _tool_version_id(api_factory, seeded["tool_alias"])
    created = await client.post(
        f"/api/projects/{pid}/optimisation-studies",
        headers=auth_headers,
        json={"name": "cost", "tool_alias": seeded["tool_alias"], "tool_version_id": vid},
    )
    sid = created.json()["id"]
    await client.patch(
        f"/api/optimisation-studies/{sid}",
        headers=auth_headers,
        json={
            "config_patch": {
                "batch_size": 4,
                "seed": {"kind": "lhs", "n": 6},
                "stopping_criteria": [{"kind": "max_runs", "value": 50}],
            }
        },
    )
    est = await client.get(f"/api/optimisation-studies/{sid}/cost-estimate", headers=auth_headers)
    assert est.status_code == 200, est.text
    assert est.json()["max_total_children"] == 56  # 50 budget + 6 seed

    approvers = await client.get("/api/optimisation-approvers", headers=auth_headers)
    assert approvers.status_code == 200
    assert isinstance(approvers.json(), list)


async def test_configure_incomplete_draft_returns_400(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    pid = await _make_project(client, auth_headers)
    vid = await _tool_version_id(api_factory, seeded["tool_alias"])
    created = await client.post(
        f"/api/projects/{pid}/optimisation-studies",
        headers=auth_headers,
        json={"name": "e", "tool_alias": seeded["tool_alias"], "tool_version_id": vid},
    )
    sid = created.json()["id"]
    # No factors/responses yet → validate_config rejects with a clean 400.
    resp = await client.post(f"/api/optimisation-studies/{sid}/configure", headers=auth_headers)
    assert resp.status_code == 400, resp.text
