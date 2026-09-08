"""Tests for the Admin → Usage Statistics router (``usage.view``).

Covers the permission gate (401 unauthenticated / 403 without the
permission), the filter-options endpoint, the empty-window happy path,
and the Overview aggregation over seeded jobs.
"""

from __future__ import annotations

import datetime as _dt
import io
import zipfile
from typing import Any, ClassVar

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from matofx.api.deps import get_settings
from matofx.approvals.orm import ApprovalRequest
from matofx.audit import AuditWriter
from matofx.config import EmailSettings, Settings
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.tools.orm import ToolVersion
from matofx.users import make_password_hasher, make_user_service
from matofx.users.team_orm import Team


async def _make_regular_user(
    factory: SessionFactory, settings: Settings, audit: AuditWriter
) -> tuple[str, str]:
    """Create a plain regular_user account; return (username, password)."""
    hasher = make_password_hasher(settings.auth)
    svc = make_user_service(settings.auth, audit, hasher=hasher)
    pw = "regularpassword1"
    async with session_scope(factory) as session:
        await svc.create_user(
            session,
            username="reg",
            email="reg@example.com",
            password=pw,
            full_name="Reg",
            role="regular_user",
        )
    return "reg", pw


async def _login(client: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    r = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _seed_jobs(
    factory: SessionFactory,
    seeded: dict[str, Any],
    specs: list[tuple[str, int | None]],
) -> None:
    """Seed one job per spec (state, ran_minutes) under its own scenario.

    A fresh scenario per job sidesteps the (scenario, tool) uniqueness
    constraint so several same-tool jobs can coexist. Timestamps land
    inside the default 30-day window.
    """
    async with session_scope(factory) as session:
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
        project = Project(name="Usage test project", creator_user_id=seeded["user_id"])
        session.add(project)
        await session.flush()
        now = _dt.datetime.now(tz=_dt.UTC)
        for index, (state, ran_minutes) in enumerate(specs):
            scenario = Scenario(project_id=project.id, name=f"Usage scenario {index}")
            session.add(scenario)
            await session.flush()
            created = now - _dt.timedelta(days=1)
            started = completed = None
            if ran_minutes is not None:
                started = created
                completed = created + _dt.timedelta(minutes=ran_minutes)
            # Give terminal jobs a realistic exit code so the Reliability
            # tab's exit-code breakdown has something to count.
            exit_code = 0 if state == "complete" else (1 if state == "failed" else None)
            session.add(
                Job(
                    scenario_id=scenario.id,
                    name=f"usage-job-{index}",
                    tool_alias=seeded["tool_alias"],
                    tool_version_id=version_row.id,
                    state=state,
                    inputs={},
                    created_by_user_id=seeded["user_id"],
                    created_at=created,
                    updated_at=now,
                    started_at=started,
                    completed_at=completed,
                    exit_code=exit_code,
                )
            )
        await session.flush()


@pytest.mark.asyncio
async def test_overview_unauthenticated_returns_401(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/admin/usage/overview")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_overview_forbidden_for_regular_user(
    client: httpx.AsyncClient,
    api_factory: SessionFactory,
    api_settings: Settings,
    api_audit: AuditWriter,
) -> None:
    username, password = await _make_regular_user(api_factory, api_settings, api_audit)
    headers = await _login(client, username, password)
    r = await client.get("/api/admin/usage/overview", headers=headers)
    assert r.status_code == 403, r.text
    assert "usage.view" in r.json()["detail"]


@pytest.mark.asyncio
async def test_filters_lists_tools_targets_and_roles(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    r = await client.get("/api/admin/usage/filters", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert seeded["tool_alias"] in {t["alias"] for t in body["tools"]}
    assert body["execution_targets"] == ["local", "aos", "hpc"]
    assert "super_user" in body["roles"]


@pytest.mark.asyncio
async def test_overview_empty_window(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    r = await client.get("/api/admin/usage/overview", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jobs_total"]["value"] == 0
    assert body["registered_users"] >= 1
    assert body["by_state"] == {}
    assert body["top_tools"] == []


@pytest.mark.asyncio
async def test_overview_aggregates_seeded_jobs(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    await _seed_jobs(
        api_factory,
        seeded,
        specs=[("complete", 60), ("complete", 60), ("failed", None)],
    )
    r = await client.get("/api/admin/usage/overview", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jobs_total"]["value"] == 3
    assert body["by_state"] == {"complete": 2, "failed": 1}
    # 2 complete of 3 terminal jobs → 66.67%.
    assert body["success_rate"]["value"] == pytest.approx(200.0 / 3.0, rel=1e-3)
    # All three jobs are the same tool → one leaderboard row of count 3.
    assert body["top_tools"][0]["count"] == 3
    assert body["top_tools"][0]["tool_alias"] == seeded["tool_alias"]
    # Two jobs ran an hour with >= 1 core each → positive core-hours.
    assert body["core_hours"]["value"] > 0
    assert body["active_users"]["value"] == 1


@pytest.mark.asyncio
async def test_overview_tool_filter_is_applied(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    await _seed_jobs(api_factory, seeded, specs=[("complete", 60), ("failed", None)])
    # Filtering on the seeded tool keeps the jobs…
    match = await client.get(
        "/api/admin/usage/overview",
        headers=auth_headers,
        params={"tool_alias": [seeded["tool_alias"]]},
    )
    assert match.status_code == 200, match.text
    assert match.json()["jobs_total"]["value"] == 2
    # …filtering on a tool that ran nothing yields an empty window.
    miss = await client.get(
        "/api/admin/usage/overview",
        headers=auth_headers,
        params={"tool_alias": ["does-not-exist"]},
    )
    assert miss.status_code == 200, miss.text
    assert miss.json()["jobs_total"]["value"] == 0


@pytest.mark.asyncio
async def test_overview_world_team_filter_means_all(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """Selecting the 'World' team means everyone, not a (member-less) group.

    'World' is the universal-visibility marker — nobody is a *member* of
    it — so filtering on its id must behave like "all teams" rather than
    narrowing the result down to zero. A real team the seeded user isn't
    in still narrows as usual.
    """
    await _seed_jobs(api_factory, seeded, specs=[("complete", 60), ("failed", None)])

    async with session_scope(api_factory) as session:
        world_id = (
            await session.execute(select(Team.id).where(Team.name == "World"))
        ).scalar_one()
        empty_team = Team(name="Empty team", description="")
        session.add(empty_team)
        await session.flush()
        empty_team_id = empty_team.id

    # World ⇒ all jobs, exactly as if no team filter were set.
    world = await client.get(
        "/api/admin/usage/overview",
        headers=auth_headers,
        params={"team_id": [world_id]},
    )
    assert world.status_code == 200, world.text
    assert world.json()["jobs_total"]["value"] == 2

    # A real team with no matching members still narrows to nothing.
    empty = await client.get(
        "/api/admin/usage/overview",
        headers=auth_headers,
        params={"team_id": [empty_team_id]},
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["jobs_total"]["value"] == 0


@pytest.mark.asyncio
async def test_compute_unauthenticated_returns_401(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/admin/usage/compute")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_compute_aggregates_seeded_jobs(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    await _seed_jobs(
        api_factory,
        seeded,
        specs=[("complete", 60), ("complete", 120), ("failed", None)],
    )
    r = await client.get("/api/admin/usage/compute", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # All same tool → one consumption row covering all three jobs.
    assert body["by_tool"][0]["tool_alias"] == seeded["tool_alias"]
    assert body["by_tool"][0]["jobs"] == 3
    assert body["by_tool"][0]["core_hours"] > 0
    # Runtime distribution only counts the two completed runs.
    assert len(body["runtime"]) == 1
    assert body["runtime"][0]["runs"] == 2
    assert body["runtime"][0]["max_hours"] == pytest.approx(2.0)
    # Throughput has at least the submission day.
    assert body["throughput"]
    assert sum(p["submitted"] for p in body["throughput"]) == 3


@pytest.mark.asyncio
async def test_reliability_aggregates_seeded_jobs(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    await _seed_jobs(
        api_factory,
        seeded,
        specs=[("complete", 60), ("complete", 60), ("failed", None)],
    )
    r = await client.get("/api/admin/usage/reliability", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success_rate"]["value"] == pytest.approx(200.0 / 3.0, rel=1e-3)
    # One failing tool with a single failure.
    assert body["top_failing"][0]["tool_alias"] == seeded["tool_alias"]
    assert body["top_failing"][0]["failures"] == 1
    # Exit codes: two zeros (complete) + one 1 (failed).
    codes = {c["exit_code"]: c["count"] for c in body["exit_codes"]}
    assert codes == {0: 2, 1: 1}


async def _seed_approvals(factory: SessionFactory, seeded: dict[str, Any]) -> None:
    """Attach pre-approved, pre-denied, and post-pending approval rows to a job."""
    async with session_scope(factory) as session:
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
        project = Project(name="Approval test project", creator_user_id=seeded["user_id"])
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="Approval scenario")
        session.add(scenario)
        await session.flush()
        job = Job(
            scenario_id=scenario.id,
            name="approval-job",
            tool_alias=seeded["tool_alias"],
            tool_version_id=version_row.id,
            state="complete",
            inputs={},
            created_by_user_id=seeded["user_id"],
        )
        session.add(job)
        await session.flush()
        now = _dt.datetime.now(tz=_dt.UTC)
        common = {
            "job_id": job.id,
            "tool_alias": seeded["tool_alias"],
            "tool_version_id": version_row.id,
            "requested_by_user_id": seeded["user_id"],
            "requested_at": now,
            "eligible_approvers": [],
        }
        decided = {
            "decided_by_user_id": seeded["user_id"],
            "decided_at": now,
            "decided_via": "approver",
        }
        session.add(ApprovalRequest(kind="pre", status="approved", **common, **decided))
        session.add(ApprovalRequest(kind="pre", status="denied", **common, **decided))
        session.add(ApprovalRequest(kind="post", status="pending", **common))
        await session.flush()


@pytest.mark.asyncio
async def test_reliability_approval_funnel(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    await _seed_approvals(api_factory, seeded)
    r = await client.get("/api/admin/usage/reliability", headers=auth_headers)
    assert r.status_code == 200, r.text
    funnel = {f["kind"]: f for f in r.json()["approval_funnel"]}
    assert funnel["pre"]["requested"] == 2
    assert funnel["pre"]["approved"] == 1
    assert funnel["pre"]["denied"] == 1
    assert funnel["post"]["requested"] == 1
    assert funnel["post"]["pending"] == 1


@pytest.mark.asyncio
async def test_people_aggregates_seeded_jobs(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    await _seed_jobs(api_factory, seeded, specs=[("complete", 60), ("complete", 60)])
    r = await client.get("/api/admin/usage/people", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # Both jobs were created by the seeded super-user.
    assert body["active_users"]["value"] == 1
    assert body["registered_users"] >= 1
    assert body["by_user"][0]["count"] == 2
    # The owner is the super-user → role label is pretty-printed.
    assert body["by_role"][0]["name"] == "Super User"


@pytest.mark.asyncio
async def test_seats_reports_enabled_and_cap(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    r = await client.get("/api/admin/usage/seats", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled_seats"] >= 1
    # The login that produced auth_headers is an active session.
    assert body["peak_concurrency"] >= 1
    # Entitlement cap is Phase 2 — explicitly absent for now.
    assert body["cap"] is None


@pytest.mark.asyncio
async def test_infra_aggregates_seeded_jobs(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    await _seed_jobs(
        api_factory,
        seeded,
        specs=[("complete", 60), ("complete", 60), ("failed", None)],
    )
    r = await client.get("/api/admin/usage/infra", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert sum(t["jobs"] for t in body["by_target"]) == 3
    assert body["cpu_jobs"] + body["gpu_jobs"] == 3


@pytest.mark.asyncio
async def test_export_unauthenticated_returns_401(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/admin/usage/export")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_export_csv_zip_of_selected_tab(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    await _seed_jobs(api_factory, seeded, specs=[("complete", 60), ("failed", None)])
    r = await client.get(
        "/api/admin/usage/export",
        headers=auth_headers,
        params={"format": "csv", "tab": ["overview"]},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        names = set(archive.namelist())
    # Only the overview tab's tables — not the other tabs.
    assert "overview_kpis.csv" in names
    assert "overview_top_tools.csv" in names
    assert not any(n.startswith("compute_") for n in names)


@pytest.mark.asyncio
async def test_export_html_includes_selected_tabs(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    await _seed_jobs(api_factory, seeded, specs=[("complete", 60)])
    r = await client.get(
        "/api/admin/usage/export",
        headers=auth_headers,
        params={"format": "html", "tab": ["overview", "compute"]},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "Usage Statistics" in body
    assert "Overview — KPIs" in body
    assert "Compute — consumption by tool" in body


@pytest.mark.asyncio
async def test_export_rejects_bad_format(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    r = await client.get(
        "/api/admin/usage/export",
        headers=auth_headers,
        params={"format": "pdf"},
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_export_rejects_no_valid_tab(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    r = await client.get(
        "/api/admin/usage/export",
        headers=auth_headers,
        params={"format": "csv", "tab": ["does-not-exist"]},
    )
    assert r.status_code == 400, r.text


# ---- email digest ----------------------------------------------------------


class _FakeSmtp:
    """Stand-in for SmtpSession that records messages instead of sending."""

    sent: ClassVar[list[Any]] = []

    def __init__(self, _effective: dict[str, Any]) -> None:
        pass

    async def __aenter__(self) -> _FakeSmtp:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def send(self, message: Any) -> None:
        _FakeSmtp.sent.append(message)


def _enable_email(app: FastAPI, api_settings: Settings) -> None:
    """Override get_settings so the email subsystem is on for the request."""
    custom = api_settings.model_copy(
        update={
            "email": EmailSettings(
                enabled=True,
                smtp_host="smtp.invalid",
                from_address="matofx@example.com",
                use_tls=False,
                use_starttls=False,
            )
        }
    )
    app.dependency_overrides[get_settings] = lambda: custom


@pytest.mark.asyncio
async def test_digest_unauthenticated_returns_401(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/admin/usage/digest/send", json={"recipients": ["a@x.com"]})
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_digest_send_disabled_when_email_off(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    r = await client.post(
        "/api/admin/usage/digest/send",
        headers=auth_headers,
        json={"recipients": ["a@x.com"], "tabs": ["overview"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["disabled"] is True
    assert body["attempted"] == 0


@pytest.mark.asyncio
async def test_digest_rejects_bad_recipients(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    r = await client.post(
        "/api/admin/usage/digest/send",
        headers=auth_headers,
        json={"recipients": ["not-an-email"], "tabs": ["overview"]},
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_digest_delivers_to_recipients(
    client: httpx.AsyncClient,
    app: FastAPI,
    auth_headers: dict[str, str],
    api_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_email(app, api_settings)
    _FakeSmtp.sent = []
    monkeypatch.setattr("matofx.usage.digest.SmtpSession", _FakeSmtp)
    r = await client.post(
        "/api/admin/usage/digest/send",
        headers=auth_headers,
        json={"recipients": ["a@x.com", "b@y.com", "a@x.com"], "tabs": ["overview"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["disabled"] is False
    # Three given, but the duplicate is collapsed → two delivered.
    assert body["sent"] == 2
    assert body["failed"] == []
    assert len(_FakeSmtp.sent) == 2
