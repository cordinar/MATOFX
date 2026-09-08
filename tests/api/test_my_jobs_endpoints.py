"""Tests for the My Jobs workspace's two new API endpoints.

- ``GET /api/users/me/visible-jobs`` — cross-project ACL-filtered job
  list with project / scenario / tool name resolution.
- ``GET /api/tools/{alias}/presets`` — preset bundle loader.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

# --- helpers ------------------------------------------------------------


async def _make_project_and_scenario(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    *,
    project_name: str,
    scenario_name: str,
) -> tuple[int, int]:
    p = await client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": project_name, "description": ""},
    )
    assert p.status_code == 201, p.text
    project_id = p.json()["id"]
    s = await client.post(
        f"/api/projects/{project_id}/scenarios",
        headers=auth_headers,
        json={"name": scenario_name, "description": ""},
    )
    assert s.status_code == 201, s.text
    return project_id, s.json()["id"]


async def _submit_job(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    *,
    scenario_id: int,
    tool_alias: str,
    name: str,
) -> int:
    response = await client.post(
        f"/api/scenarios/{scenario_id}/jobs",
        headers=auth_headers,
        json={
            "name": name,
            "tool_alias": tool_alias,
            "inputs": {
                "component_1": "Component A",
                "component_2": "Component C",
                "component_3": "Component E",
                "component_1_percent": 50,
                "component_2_percent": 30,
                "component_3_percent": 20,
            },
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


# --- /api/users/me/visible-jobs ----------------------------------------


@pytest.mark.asyncio
async def test_visible_jobs_returns_empty_when_user_has_none(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    response = await client.get("/api/users/me/visible-jobs", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json() == []


@pytest.mark.asyncio
async def test_visible_jobs_lists_jobs_with_friendly_names(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    _, sid = await _make_project_and_scenario(
        client, auth_headers, project_name="Hot Project", scenario_name="Variant A"
    )
    job_id = await _submit_job(
        client, auth_headers, scenario_id=sid, tool_alias=seeded["tool_alias"], name="First run"
    )
    response = await client.get("/api/users/me/visible-jobs", headers=auth_headers)
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == job_id
    assert row["name"] == "First run"
    # Tool name (NOT alias) — workspace requirement.
    assert row["tool_name"] != row["tool_alias"]
    assert row["tool_alias"] == seeded["tool_alias"]
    # Friendly project + scenario labels for sidebar back-fill.
    assert row["project_name"] == "Hot Project"
    assert row["scenario_name"] == "Variant A"


@pytest.mark.asyncio
async def test_visible_jobs_orders_newest_first(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    # Each job needs its own scenario — the unique constraint on
    # (scenario_id, tool_alias) only allows one job per tool per
    # scenario (locked 2026-05-08).
    _, sid_a = await _make_project_and_scenario(
        client, auth_headers, project_name="Pa", scenario_name="Sa"
    )
    _, sid_b = await _make_project_and_scenario(
        client, auth_headers, project_name="Pb", scenario_name="Sb"
    )
    job_a = await _submit_job(
        client, auth_headers, scenario_id=sid_a, tool_alias=seeded["tool_alias"], name="A"
    )
    job_b = await _submit_job(
        client, auth_headers, scenario_id=sid_b, tool_alias=seeded["tool_alias"], name="B"
    )
    response = await client.get("/api/users/me/visible-jobs", headers=auth_headers)
    assert response.status_code == 200, response.text
    ids = [r["id"] for r in response.json()]
    assert ids == [job_b, job_a]  # newest first


@pytest.mark.asyncio
async def test_visible_jobs_filters_by_q_against_job_name(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    # Two scenarios — same constraint as the previous test.
    _, sid_glass = await _make_project_and_scenario(
        client, auth_headers, project_name="Pg", scenario_name="Sg"
    )
    _, sid_steel = await _make_project_and_scenario(
        client, auth_headers, project_name="Ps", scenario_name="Ss"
    )
    await _submit_job(
        client,
        auth_headers,
        scenario_id=sid_glass,
        tool_alias=seeded["tool_alias"],
        name="Glass calibration run",
    )
    await _submit_job(
        client,
        auth_headers,
        scenario_id=sid_steel,
        tool_alias=seeded["tool_alias"],
        name="Steel preliminary",
    )
    # ``q`` is case-insensitive substring match against Job.name.
    response = await client.get("/api/users/me/visible-jobs?q=glass", headers=auth_headers)
    assert response.status_code == 200, response.text
    names = [r["name"] for r in response.json()]
    assert names == ["Glass calibration run"]


@pytest.mark.asyncio
async def test_visible_jobs_filters_by_tool_alias(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    _, sid = await _make_project_and_scenario(
        client, auth_headers, project_name="P", scenario_name="S"
    )
    await _submit_job(
        client, auth_headers, scenario_id=sid, tool_alias=seeded["tool_alias"], name="Job"
    )
    # Match → returned.
    r1 = await client.get(
        f"/api/users/me/visible-jobs?tool_alias={seeded['tool_alias']}",
        headers=auth_headers,
    )
    assert r1.status_code == 200
    assert len(r1.json()) == 1
    # No-match → empty.
    r2 = await client.get(
        "/api/users/me/visible-jobs?tool_alias=nonexistent_tool", headers=auth_headers
    )
    assert r2.status_code == 200
    assert r2.json() == []


@pytest.mark.asyncio
async def test_visible_jobs_limit_is_capped_to_500(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    # An over-cap limit query is rejected by FastAPI's Query(le=500)
    # validation rather than silently clamped — the workspace shouldn't
    # be inadvertently asking for huge pages.
    response = await client.get("/api/users/me/visible-jobs?limit=10000", headers=auth_headers)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_visible_jobs_requires_auth(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
) -> None:
    del seeded
    response = await client.get("/api/users/me/visible-jobs")
    assert response.status_code == 401


# --- /api/tools/{alias}/presets ----------------------------------------


@pytest.mark.asyncio
async def test_presets_returns_empty_for_tool_with_no_preset_file(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    # The seeded ``material_properties`` example tool ships without a
    # presets file — endpoint should respond cleanly with [].
    response = await client.get(f"/api/tools/{seeded['tool_alias']}/presets", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json() == []


# Note: the toml-parsing happy-path is covered exhaustively by
# ``tests/tools/forms/test_seeders.py``. We don't repeat that at the
# API layer here — the API just delegates to ``ToolService.list_presets``,
# which delegates to ``load_presets``.


@pytest.mark.asyncio
async def test_presets_404_for_unknown_tool(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    response = await client.get("/api/tools/no_such_tool/presets", headers=auth_headers)
    # ToolNotFoundError → 404 via the registered domain-error handler.
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_presets_requires_auth(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
) -> None:
    response = await client.get(f"/api/tools/{seeded['tool_alias']}/presets")
    assert response.status_code == 401


# --- one-tool-per-scenario constraint ----------------------------------


@pytest.mark.asyncio
async def test_submitting_same_tool_twice_in_a_scenario_returns_409(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    # Locked 2026-05-08: any given scenario cannot ever contain the
    # same tool more than once. The first submit succeeds; the second
    # comes back as a 409 with the existing job's id surfaced so the
    # workspace can navigate the user to it.
    _, sid = await _make_project_and_scenario(
        client, auth_headers, project_name="P", scenario_name="S"
    )
    first = await _submit_job(
        client, auth_headers, scenario_id=sid, tool_alias=seeded["tool_alias"], name="First"
    )
    response = await client.post(
        f"/api/scenarios/{sid}/jobs",
        headers=auth_headers,
        json={
            "name": "Second attempt",
            "tool_alias": seeded["tool_alias"],
            "inputs": {
                "component_1": "Component A",
                "component_2": "Component C",
                "component_3": "Component E",
                "component_1_percent": 50,
                "component_2_percent": 30,
                "component_3_percent": 20,
            },
        },
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert "already has a job" in body["detail"]
    assert body["existing_job_id"] == first
