"""API tests for the parametric-studies router (slice 2-PS-a/5).

Covers the design-§12 gating (feature flag makes the routes *absent*
when off), the create/get/list/cancel happy path, anti-escalation
(unknown tool -> 404), and central error mapping (a bad factor spec
surfaces as a clean plain-English 400, not a 500).
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI

from matofx.api.deps import get_settings
from matofx.config import FeatureSettings, Settings
from matofx.features import Feature


def _enable_feature(app: FastAPI, api_settings: Settings) -> None:
    """Flip ``features.parametric_studies`` on for this app instance."""
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.PARAMETRIC_STUDIES.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _make_project(client: httpx.AsyncClient, headers: dict[str, str]) -> int:
    resp = await client.post(
        "/api/projects", headers=headers, json={"name": "Study P", "description": ""}
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def test_routes_absent_when_feature_off(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Design §12: with the flag off the surface is *absent* (404)."""
    create = await client.post(
        "/api/projects/1/studies",
        headers=auth_headers,
        json={"name": "x", "tool_alias": "t", "mode": "grid"},
    )
    assert create.status_code == 404
    listing = await client.get("/api/projects/1/studies", headers=auth_headers)
    assert listing.status_code == 404


async def test_create_get_list_cancel(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    pid = await _make_project(client, auth_headers)

    created = await client.post(
        f"/api/projects/{pid}/studies",
        headers=auth_headers,
        json={
            "name": "  Sweep 1  ",
            "tool_alias": seeded["tool_alias"],
            "mode": "grid",
            "description": "first study",
        },
    )
    assert created.status_code == 201, created.text
    study = created.json()
    assert study["state"] == "draft"
    assert study["name"] == "Sweep 1"
    assert study["tool_alias"] == seeded["tool_alias"]
    assert study["tool_version_id"] > 0
    # Display fields are enriched on the read paths (labels, not aliases/ids).
    assert study["tool_name"]  # tool display label resolved
    assert study["tool_version"]  # version string, not just the id
    assert study["created_by_name"]  # creator resolved
    assert study["project_name"]  # owning project resolved
    sid = study["id"]

    detail = await client.get(f"/api/studies/{sid}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["study"]["id"] == sid
    assert body["study"]["tool_name"]
    assert body["children"] == []
    # Pinned-descriptor label map for the Progress list (labels, not names).
    assert "input_labels" in body
    assert "input_units" in body
    # Pinned-descriptor input grouping for the Study-settings layout.
    assert "input_groups" in body
    assert "input_group_of" in body
    assert isinstance(body["input_groups"], list)
    assert isinstance(body["input_group_of"], dict)

    listing = await client.get(f"/api/projects/{pid}/studies", headers=auth_headers)
    assert listing.status_code == 200
    listed = listing.json()
    assert [s["id"] for s in listed] == [sid]
    assert listed[0]["tool_name"]  # list path enriched too
    assert listed[0]["created_by_name"]

    cancelled = await client.post(f"/api/studies/{sid}/cancel", headers=auth_headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"


async def test_create_unknown_tool_is_404(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    _enable_feature(app, api_settings)
    pid = await _make_project(client, auth_headers)
    resp = await client.post(
        f"/api/projects/{pid}/studies",
        headers=auth_headers,
        json={"name": "x", "tool_alias": "ghost-tool", "mode": "grid"},
    )
    assert resp.status_code == 404
    assert "couldn't find" in resp.json()["detail"].lower()


async def test_submit_empty_factor_spec_is_plain_english_400(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """A draft with no factors -> 400 from the central handler, with
    the FactorSpecError message verbatim (never a 500)."""
    _enable_feature(app, api_settings)
    pid = await _make_project(client, auth_headers)
    created = await client.post(
        f"/api/projects/{pid}/studies",
        headers=auth_headers,
        json={"name": "empty", "tool_alias": seeded["tool_alias"], "mode": "grid"},
    )
    sid = created.json()["id"]
    resp = await client.post(f"/api/studies/{sid}/submit", headers=auth_headers)
    assert resp.status_code == 400, resp.text
    assert "non-empty" in resp.json()["detail"].lower()


async def test_preview_bad_spec_is_400(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    resp = await client.post(
        "/api/studies/preview",
        headers=auth_headers,
        json={"tool_alias": seeded["tool_alias"], "mode": "grid", "factor_spec": {}},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]


async def test_get_missing_study_is_404(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    _enable_feature(app, api_settings)
    resp = await client.get("/api/studies/999999", headers=auth_headers)
    assert resp.status_code == 404

async def test_results_carry_the_declared_options_of_choice_inputs(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """``input_options`` is what lets a swept ``select`` be plotted.

    The Explore plots place a choice-valued input as one named column per
    option, and keep a column for an option nobody ran. Without this field the
    UI can only see the levels that happen to appear in the runs, so the gap —
    the option the sweep never covered — becomes invisible, which is the one
    thing those columns exist to show.
    """
    _enable_feature(app, api_settings)
    pid = await _make_project(client, auth_headers)
    created = await client.post(
        f"/api/projects/{pid}/studies",
        headers=auth_headers,
        json={"name": "Options", "tool_alias": seeded["tool_alias"], "mode": "grid"},
    )
    assert created.status_code == 201, created.text

    results = await client.get(
        f"/api/studies/{created.json()['id']}/results", headers=auth_headers
    )
    assert results.status_code == 200, results.text
    options = results.json()["input_options"]

    assert options, "a tool with select inputs must expose their options"
    assert all(isinstance(v, list) and v for v in options.values())
    # Numeric inputs enumerate nothing and must not appear as choices.
    labels = results.json()["input_labels"]
    assert set(options) < set(labels)
