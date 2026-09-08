"""Tests for the Live Jobs router (``live_jobs.view`` / ``live_jobs.cancel``).

Covers the two-tier permission split locked for this surface: viewing is
default-on for administrators (and super-users), while cancelling is
super-user-only by default and must be granted to administrators. Also
checks the in-flight list shape and that a bulk cancel actually stamps
the job.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from matofx.audit import AuditWriter
from matofx.config import Settings
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.tools.orm import ToolVersion
from matofx.users import make_password_hasher, make_user_service

pytestmark = pytest.mark.asyncio


async def _make_user(
    factory: SessionFactory, settings: Settings, audit: AuditWriter, *, username: str, role: str
) -> tuple[str, str]:
    """Create a user with ``role``; return (username, password)."""
    svc = make_user_service(settings.auth, audit, hasher=make_password_hasher(settings.auth))
    pw = "password12345"
    async with session_scope(factory) as session:
        await svc.create_user(
            session,
            username=username,
            email=f"{username}@example.com",
            password=pw,
            full_name=f"{username.title()} User",
            role=role,
        )
    return username, pw


async def _login(client: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    r = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _seed_queued_job(factory: SessionFactory, seeded: dict[str, Any]) -> int:
    """Seed one queued standalone job under a fresh project/scenario."""
    async with session_scope(factory) as session:
        version = (
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
        assert version is not None
        project = Project(name="Live project", creator_user_id=seeded["user_id"])
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="Live scenario")
        session.add(scenario)
        await session.flush()
        job = Job(
            scenario_id=scenario.id,
            name="live-job",
            tool_alias=seeded["tool_alias"],
            tool_version_id=version.id,
            state="queued",
            inputs={},
            created_by_user_id=seeded["user_id"],
            created_at=_dt.datetime.now(tz=_dt.UTC),
        )
        session.add(job)
        await session.flush()
        return int(job.id)


async def test_list_unauthenticated_returns_401(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/admin/live-jobs")
    assert r.status_code == 401, r.text


async def test_list_forbidden_for_regular_user(
    client: httpx.AsyncClient,
    api_factory: SessionFactory,
    api_settings: Settings,
    api_audit: AuditWriter,
) -> None:
    username, password = await _make_user(
        api_factory, api_settings, api_audit, username="reg", role="regular_user"
    )
    headers = await _login(client, username, password)
    r = await client.get("/api/admin/live-jobs", headers=headers)
    assert r.status_code == 403, r.text
    assert "live_jobs.view" in r.json()["detail"]


async def test_list_super_user_sees_inflight_and_can_cancel(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    job_id = await _seed_queued_job(api_factory, seeded)
    r = await client.get("/api/admin/live-jobs", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["can_cancel"] is True
    rows = {(i["kind"], i["id"]): i for i in body["items"]}
    assert ("job", job_id) in rows
    assert rows[("job", job_id)]["cancellable"] is True
    assert seeded["user_id"] in {s["user_id"] for s in body["submitters"]}


async def test_admin_can_view_but_not_cancel_by_default(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    api_factory: SessionFactory,
    api_settings: Settings,
    api_audit: AuditWriter,
) -> None:
    """Administrator holds live_jobs.view by default but not live_jobs.cancel."""
    job_id = await _seed_queued_job(api_factory, seeded)
    username, password = await _make_user(
        api_factory, api_settings, api_audit, username="ada", role="administrator"
    )
    headers = await _login(client, username, password)

    listing = await client.get("/api/admin/live-jobs", headers=headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["can_cancel"] is False

    blocked = await client.post(
        "/api/admin/live-jobs/cancel", headers=headers, json={"job_ids": [job_id]}
    )
    assert blocked.status_code == 403, blocked.text
    assert "live_jobs.cancel" in blocked.json()["detail"]


async def test_super_user_bulk_cancel_stamps_job(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    job_id = await _seed_queued_job(api_factory, seeded)
    r = await client.post(
        "/api/admin/live-jobs/cancel", headers=auth_headers, json={"job_ids": [job_id]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cancelled"] == 1
    assert body["outcomes"][0] == {
        "kind": "job",
        "id": job_id,
        "cancelled": True,
        "reason": "",
    }
    async with session_scope(api_factory) as session:
        row = await session.get(Job, job_id)
        assert row is not None
        assert row.cancel_requested_at is not None


async def test_cancel_unauthenticated_returns_401(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/admin/live-jobs/cancel", json={"job_ids": [1]})
    assert r.status_code == 401, r.text
