"""Tests for ``GET /api/jobs/{job_id}/report`` (ADR-0004 slice 1).

Pins down:

- 401 when the request carries no bearer token.
- 404 when the job doesn't exist (deny-by-default, no existence leak).
- 404 when the job exists but isn't in ``complete`` state — slice 1
  doesn't ship partial / failed-job reports.
- 200 happy path: ``text/html`` body, ``Content-Disposition`` attachment
  with the expected filename pattern, and the body carries the
  metadata fields the slice-1 skeleton emits.
- One ``job.report.generate`` audit row written per successful
  download (mirrors the audit conventions used elsewhere in the API).

The router-level happy path needs a job in the ``complete`` state.
We seed one directly via the test session rather than driving the
runner end-to-end — slice 1 is a metadata-only render, so the
absence of a real working dir is fine.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from matofx.audit.orm import AuditLog
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.tools.orm import Tool, ToolVersion


@pytest.fixture
async def complete_job(
    api_factory: SessionFactory,
    seeded: dict[str, Any],
) -> dict[str, Any]:
    """Seed a Project / Scenario / Job(state='complete') under the super-user.

    Uses the tool installed by the ``seeded`` fixture so the job's
    ``tool_alias`` / ``tool_version_id`` FKs resolve. Returns the job
    id and the captured tool version string so the test can assert
    on the rendered metadata.
    """
    async with session_scope(api_factory) as session:
        project = Project(name="Reports test project", creator_user_id=seeded["user_id"])
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="Reports test scenario")
        session.add(scenario)
        await session.flush()
        # Resolve the latest tool_version captured by the install in
        # the seeded fixture so the FK and the rendered version match.
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
            name="reports-slice1-job",
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
            "tool_version": version_row.version,
        }


@pytest.mark.asyncio
async def test_unauthenticated_request_returns_401(
    client: httpx.AsyncClient,
    complete_job: dict[str, Any],
) -> None:
    """No Authorization header → 401, no body."""
    response = await client.get(f"/api/jobs/{complete_job['job_id']}/report")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_unknown_job_returns_404(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """A job id that doesn't exist resolves to 404 (deny-by-default)."""
    del seeded
    response = await client.get("/api/jobs/999999999/report", headers=auth_headers)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_non_complete_job_returns_404(
    client: httpx.AsyncClient,
    api_factory: SessionFactory,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """A job in a non-``complete`` state collapses to 404 — slice 1 doesn't
    ship partial reports and the response must not leak progress."""
    async with session_scope(api_factory) as session:
        project = Project(name="P-running", creator_user_id=seeded["user_id"])
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="S-running")
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
        job = Job(
            scenario_id=scenario.id,
            name="still-running",
            tool_alias=seeded["tool_alias"],
            tool_version_id=version_row.id,
            state="running",
            inputs={},
            created_by_user_id=seeded["user_id"],
        )
        session.add(job)
        await session.flush()
        running_job_id = int(job.id)

    response = await client.get(f"/api/jobs/{running_job_id}/report", headers=auth_headers)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_happy_path_returns_html_attachment(
    client: httpx.AsyncClient,
    complete_job: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """Authenticated owner of a ``complete`` job downloads the report."""
    response = await client.get(f"/api/jobs/{complete_job['job_id']}/report", headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    disposition = response.headers.get("content-disposition", "")
    assert disposition.startswith("attachment; filename=")
    assert ".html" in disposition
    # The padded job id appears in the filename so directory listings sort.
    padded = f"{complete_job['job_id']:07d}"
    assert padded in disposition
    assert complete_job["tool_alias"] in disposition

    body = response.text
    assert body.startswith("<!DOCTYPE html>")
    assert str(complete_job["job_id"]) in body
    assert complete_job["tool_name"] in body
    assert complete_job["tool_alias"] in body
    assert f"v{complete_job['tool_version']}" in body


@pytest.mark.asyncio
async def test_successful_download_writes_audit_row(
    client: httpx.AsyncClient,
    api_factory: SessionFactory,
    complete_job: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """One ``job.report.generate`` audit row is written per successful download."""
    response = await client.get(f"/api/jobs/{complete_job['job_id']}/report", headers=auth_headers)
    assert response.status_code == 200

    async with session_scope(api_factory) as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "job.report.generate")
                    .where(AuditLog.resource_type == "job")
                    .where(AuditLog.resource_id == str(complete_job["job_id"]))
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].outcome == "success"
