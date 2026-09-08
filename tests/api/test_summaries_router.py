"""Slice 1 — ``GET /api/summaries/{kind}/{id}`` (job surface).

End-to-end through the real ASGI app: a completed job yields a templated
summary; access mirrors the report endpoint (404 collapses unknown /
non-terminal / not-visible); the collector is exercised against a real row.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.tools.orm import Tool, ToolVersion


@pytest.fixture
async def complete_job(
    api_factory: SessionFactory,
    seeded: dict[str, Any],
) -> dict[str, Any]:
    """Seed a completed Job under the seeded super-user + example tool."""
    async with session_scope(api_factory) as session:
        project = Project(name="Summary test project", creator_user_id=seeded["user_id"])
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="Summary test scenario")
        session.add(scenario)
        await session.flush()
        version_row = (
            (
                await session.execute(
                    select(ToolVersion)
                    .where(ToolVersion.alias == seeded["tool_alias"])
                    .order_by(ToolVersion.id.desc())
                )
            )
            .scalars()
            .first()
        )
        assert version_row is not None
        tool_row = (
            await session.execute(select(Tool).where(Tool.alias == seeded["tool_alias"]))
        ).scalar_one_or_none()
        assert tool_row is not None
        now = _dt.datetime.now(tz=_dt.UTC)
        job = Job(
            scenario_id=scenario.id,
            name="summary-slice1-job",
            tool_alias=seeded["tool_alias"],
            tool_version_id=version_row.id,
            state="complete",
            inputs={"foo": "bar"},
            created_by_user_id=seeded["user_id"],
            created_at=now - _dt.timedelta(minutes=10),
            updated_at=now,
            started_at=now - _dt.timedelta(minutes=9),
            completed_at=now,
        )
        session.add(job)
        await session.flush()
        return {
            "job_id": int(job.id),
            "tool_alias": seeded["tool_alias"],
            "tool_name": tool_row.name,
        }


@pytest.mark.asyncio
async def test_unauthenticated_returns_401(
    client: httpx.AsyncClient,
    complete_job: dict[str, Any],
) -> None:
    response = await client.get(f"/api/summaries/job/{complete_job['job_id']}")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_complete_job_returns_templated_summary(
    client: httpx.AsyncClient,
    complete_job: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    response = await client.get(
        f"/api/summaries/job/{complete_job['job_id']}", headers=auth_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["surface_kind"] == "job"
    assert body["status"] == "templated"
    assert body["enriched"] is None
    paragraphs = body["templated"]["paragraphs"]
    assert paragraphs
    joined = " ".join(paragraphs)
    assert complete_job["tool_name"] in joined
    assert "completed successfully" in joined


@pytest.mark.asyncio
async def test_unknown_job_returns_404(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.get("/api/summaries/job/999999999", headers=auth_headers)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unknown_kind_returns_404(
    client: httpx.AsyncClient,
    complete_job: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    response = await client.get(
        f"/api/summaries/widget/{complete_job['job_id']}", headers=auth_headers
    )
    assert response.status_code == 404
