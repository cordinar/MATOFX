"""Tests for the per-tool Job Search endpoints.

- ``POST /api/tools/{alias}/job-search`` — find completed jobs matching
  the supplied input filters (with ± numeric tolerance).
- ``GET  /api/tools/{alias}/distinct-input-values`` — distinct values
  previously submitted for one input field across visible runs.

Both back the v2 ``/job-search`` page (port of legacy
``functions/job_search.py``).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from matofx.audit import AuditWriter
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.hierarchy.service import make_job_service

# --- helpers ----------------------------------------------------------


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
    inputs: dict[str, Any],
) -> int:
    response = await client.post(
        f"/api/scenarios/{scenario_id}/jobs",
        headers=auth_headers,
        json={"name": name, "tool_alias": tool_alias, "inputs": inputs},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _force_complete(
    api_factory: SessionFactory,
    api_audit: AuditWriter,
    *,
    job_id: int,
    actor_user_id: int,
) -> None:
    """Drive a queued job to ``complete`` without standing up the runner.

    The Job Search endpoint only returns jobs in ``state == "complete"``,
    and the API submit path leaves jobs at ``queued``. Tests use the
    JobService transition primitive directly to advance state — much
    cheaper than spinning up the RunnerSupervisor.
    """
    job_service = make_job_service(api_audit)
    async with session_scope(api_factory) as session:
        await job_service._transition_state(
            session, job_id=job_id, actor_user_id=actor_user_id, new_state="running"
        )
    async with session_scope(api_factory) as session:
        await job_service._transition_state(
            session, job_id=job_id, actor_user_id=actor_user_id, new_state="complete"
        )


def _formulation_inputs(
    *,
    c1: str = "Component A",
    c2: str = "Component C",
    c3: str = "Component E",
    p1: float = 50.0,
    p2: float = 30.0,
    p3: float = 20.0,
) -> dict[str, Any]:
    """Default valid material-properties formulation; tweak per test."""
    return {
        "component_1": c1,
        "component_2": c2,
        "component_3": c3,
        "component_1_percent": p1,
        "component_2_percent": p2,
        "component_3_percent": p3,
    }


async def _seed_complete_job(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
    seeded: dict[str, Any],
    *,
    project_name: str,
    inputs: dict[str, Any],
) -> int:
    """Create a fresh project+scenario, submit one job, transition → complete."""
    _, sid = await _make_project_and_scenario(
        client, auth_headers, project_name=project_name, scenario_name="S"
    )
    job_id = await _submit_job(
        client,
        auth_headers,
        scenario_id=sid,
        tool_alias=seeded["tool_alias"],
        name=f"job-{project_name}",
        inputs=inputs,
    )
    await _force_complete(
        api_factory, api_audit, job_id=job_id, actor_user_id=int(seeded["user_id"])
    )
    return job_id


# --- POST /api/tools/{alias}/job-search ----------------------------------


@pytest.mark.asyncio
async def test_job_search_no_filters_returns_all_complete(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """Empty filters → every visible completed run of the tool."""
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="P1", inputs=_formulation_inputs(c1="Component A"),
    )
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="P2", inputs=_formulation_inputs(c1="Component B"),
    )
    response = await client.post(
        f"/api/tools/{seeded['tool_alias']}/job-search",
        headers=auth_headers,
        json={"filters": {}, "numeric_tolerance_pct": 25.0},
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 2
    assert {r["project_name"] for r in rows} == {"P1", "P2"}


@pytest.mark.asyncio
async def test_job_search_carries_submitter_name(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """Every result row carries the submitter's friendly display name."""
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="P", inputs=_formulation_inputs(),
    )
    response = await client.post(
        f"/api/tools/{seeded['tool_alias']}/job-search",
        headers=auth_headers,
        json={"filters": {}},
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    # The seeded fixture user is "Root User" (full_name).
    assert rows[0]["submitter_name"] == "Root User"
    assert rows[0]["submitter_user_id"] == int(seeded["user_id"])


@pytest.mark.asyncio
async def test_job_search_equality_filter_on_select(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """Filter on a select input matches by exact equality."""
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="A1", inputs=_formulation_inputs(c1="Component A"),
    )
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="B1", inputs=_formulation_inputs(c1="Component B"),
    )
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="A2", inputs=_formulation_inputs(c1="Component A"),
    )
    response = await client.post(
        f"/api/tools/{seeded['tool_alias']}/job-search",
        headers=auth_headers,
        json={"filters": {"component_1": "Component A"}},
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert {r["project_name"] for r in rows} == {"A1", "A2"}


@pytest.mark.asyncio
async def test_job_search_numeric_tolerance(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """Numeric filters apply ± tolerance (default 25%)."""
    # component_1_percent at 50 ± 25% ⇒ [37.5, 62.5]
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="lo", inputs=_formulation_inputs(p1=40.0, p2=40.0, p3=20.0),
    )
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="hi", inputs=_formulation_inputs(p1=60.0, p2=20.0, p3=20.0),
    )
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="out", inputs=_formulation_inputs(p1=70.0, p2=20.0, p3=10.0),
    )
    response = await client.post(
        f"/api/tools/{seeded['tool_alias']}/job-search",
        headers=auth_headers,
        json={
            "filters": {"component_1_percent": 50.0},
            "numeric_tolerance_pct": 25.0,
        },
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert {r["project_name"] for r in rows} == {"lo", "hi"}


@pytest.mark.asyncio
async def test_job_search_excludes_queued_jobs(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """A queued (not-yet-complete) job is invisible to job-search."""
    # Submit-only (no force_complete) → job stays in 'queued'.
    _, sid = await _make_project_and_scenario(
        client, auth_headers, project_name="Q", scenario_name="S"
    )
    await _submit_job(
        client, auth_headers,
        scenario_id=sid, tool_alias=seeded["tool_alias"],
        name="queued-only", inputs=_formulation_inputs(),
    )
    # Plus a completed job for contrast.
    completed_id = await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="C", inputs=_formulation_inputs(),
    )

    response = await client.post(
        f"/api/tools/{seeded['tool_alias']}/job-search",
        headers=auth_headers,
        json={"filters": {}},
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert [r["id"] for r in rows] == [completed_id]


@pytest.mark.asyncio
async def test_job_search_unknown_filter_field_is_ignored(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """Filters referencing fields not in the descriptor degrade gracefully."""
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="P", inputs=_formulation_inputs(),
    )
    response = await client.post(
        f"/api/tools/{seeded['tool_alias']}/job-search",
        headers=auth_headers,
        json={"filters": {"phantom_field": "anything"}},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1


@pytest.mark.asyncio
async def test_job_search_skips_empty_filter_values(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """``None`` / ``""`` / ``[]`` filter values mean 'no constraint'."""
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="X", inputs=_formulation_inputs(c1="Component A"),
    )
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="Y", inputs=_formulation_inputs(c1="Component B"),
    )
    response = await client.post(
        f"/api/tools/{seeded['tool_alias']}/job-search",
        headers=auth_headers,
        json={
            "filters": {"component_1": None, "component_2": "", "component_3": []},
        },
    )
    assert response.status_code == 200, response.text
    assert len(response.json()) == 2


@pytest.mark.asyncio
async def test_job_search_unknown_tool_returns_404(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """ToolService.get raises NotFound → API translates to 404."""
    del seeded
    response = await client.post(
        "/api/tools/no_such_tool/job-search",
        headers=auth_headers,
        json={"filters": {}},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_job_search_requires_auth(
    client: httpx.AsyncClient, seeded: dict[str, Any]
) -> None:
    response = await client.post(
        f"/api/tools/{seeded['tool_alias']}/job-search",
        json={"filters": {}},
    )
    assert response.status_code in (401, 403)


# --- POST /api/tools/{alias}/job-search/similar -------------------------


@pytest.mark.asyncio
async def test_find_similar_ranks_closest_first(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """Typed reference → completed runs ranked closest-first with a score."""
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="exact", inputs=_formulation_inputs(p1=50.0, p2=30.0, p3=20.0),
    )
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="near", inputs=_formulation_inputs(p1=55.0, p2=25.0, p3=20.0),
    )
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="far", inputs=_formulation_inputs(p1=80.0, p2=10.0, p3=10.0),
    )
    response = await client.post(
        f"/api/tools/{seeded['tool_alias']}/job-search/similar",
        headers=auth_headers,
        json={"reference_inputs": _formulation_inputs(p1=50.0, p2=30.0, p3=20.0)},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    # No study seeded for this tool → uniform weighting.
    assert payload["weighting"] == "uniform"
    rows = payload["results"]
    assert [r["project_name"] for r in rows] == ["exact", "near", "far"]
    assert rows[0]["closeness_pct"] == 100.0
    assert rows[0]["distance"] == 0.0
    # Closeness decreases monotonically with distance.
    assert rows[0]["closeness_pct"] >= rows[1]["closeness_pct"] >= rows[2]["closeness_pct"]


@pytest.mark.asyncio
async def test_find_similar_top_n_caps_results(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    for i, p1 in enumerate((40.0, 50.0, 60.0)):
        await _seed_complete_job(
            client, auth_headers, api_factory, api_audit, seeded,
            project_name=f"P{i}", inputs=_formulation_inputs(p1=p1, p2=100.0 - p1 - 20.0, p3=20.0),
        )
    response = await client.post(
        f"/api/tools/{seeded['tool_alias']}/job-search/similar",
        headers=auth_headers,
        json={"reference_inputs": _formulation_inputs(p1=50.0, p2=30.0, p3=20.0), "top_n": 2},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["results"]) == 2


@pytest.mark.asyncio
async def test_find_similar_seed_job_is_excluded(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """Seeding from an existing run drops that run from its own results."""
    seed_id = await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="seed", inputs=_formulation_inputs(p1=50.0, p2=30.0, p3=20.0),
    )
    other_id = await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="other", inputs=_formulation_inputs(p1=55.0, p2=25.0, p3=20.0),
    )
    response = await client.post(
        f"/api/tools/{seeded['tool_alias']}/job-search/similar",
        headers=auth_headers,
        json={"reference_job_id": seed_id},
    )
    assert response.status_code == 200, response.text
    ids = [r["id"] for r in response.json()["results"]]
    assert seed_id not in ids
    assert other_id in ids


@pytest.mark.asyncio
async def test_find_similar_categorical_gate(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """A categorical reference value gates results to exact matches."""
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="A", inputs=_formulation_inputs(c1="Component A"),
    )
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="B", inputs=_formulation_inputs(c1="Component B"),
    )
    response = await client.post(
        f"/api/tools/{seeded['tool_alias']}/job-search/similar",
        headers=auth_headers,
        json={"reference_inputs": {"component_1": "Component A"}},
    )
    assert response.status_code == 200, response.text
    rows = response.json()["results"]
    assert {r["project_name"] for r in rows} == {"A"}


@pytest.mark.asyncio
async def test_find_similar_unknown_tool_returns_404(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    response = await client.post(
        "/api/tools/no_such_tool/job-search/similar",
        headers=auth_headers,
        json={"reference_inputs": {}},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_find_similar_requires_auth(
    client: httpx.AsyncClient, seeded: dict[str, Any]
) -> None:
    response = await client.post(
        f"/api/tools/{seeded['tool_alias']}/job-search/similar",
        json={"reference_inputs": {}},
    )
    assert response.status_code in (401, 403)


# --- GET /api/tools/{alias}/distinct-input-values -----------------------


@pytest.mark.asyncio
async def test_distinct_input_values_returns_unique_set(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="P1", inputs=_formulation_inputs(c1="Component A"),
    )
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="P2", inputs=_formulation_inputs(c1="Component B"),
    )
    await _seed_complete_job(
        client, auth_headers, api_factory, api_audit, seeded,
        project_name="P3", inputs=_formulation_inputs(c1="Component A"),  # dup
    )
    response = await client.get(
        f"/api/tools/{seeded['tool_alias']}/distinct-input-values",
        headers=auth_headers,
        params={"field": "component_1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["field"] == "component_1"
    assert sorted(body["values"]) == ["Component A", "Component B"]


@pytest.mark.asyncio
async def test_distinct_input_values_empty_when_no_jobs(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    response = await client.get(
        f"/api/tools/{seeded['tool_alias']}/distinct-input-values",
        headers=auth_headers,
        params={"field": "component_1"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"field": "component_1", "values": []}


@pytest.mark.asyncio
async def test_distinct_input_values_unknown_tool_returns_404(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    del seeded
    response = await client.get(
        "/api/tools/no_such_tool/distinct-input-values",
        headers=auth_headers,
        params={"field": "x"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_file_output_candidates_route_wins_over_alias(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """The literal ``/file-output-candidates`` path is matched ahead of the
    ``/{alias}`` route and returns the picker's response shape (not a tool
    detail for alias='file-output-candidates')."""
    del seeded
    response = await client.get(
        "/api/tools/file-output-candidates",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"entries"}
    assert isinstance(body["entries"], list)


@pytest.mark.asyncio
async def test_file_output_candidates_accepts_type_filter_param(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """The repeated ``accepted_file_types`` query param parses cleanly."""
    del seeded
    response = await client.get(
        "/api/tools/file-output-candidates",
        headers=auth_headers,
        params=[("accepted_file_types", ".vtu"), ("accepted_file_types", ".vtp")],
    )
    assert response.status_code == 200, response.text
    assert isinstance(response.json()["entries"], list)
