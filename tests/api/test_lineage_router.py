"""Tests for the lineage router (ADR-0010 §5).

Two endpoints:

- ``GET /api/lineage/{kind}/{id}`` — JSON LineageGraph.
- ``GET /api/lineage/{kind}/{id}/export?format=svg|json|graphml`` —
  attachment download as an image, JSON, or GraphML.

Covers the error contract (401 unauthenticated, 403 missing
``lineage.view``, 404 existence-hidden, 422 bad kind / bad format /
out-of-range depth), the happy-path response shape, and the
``Content-Disposition`` header on exports.

Tests run **without** the slice-2 ACL fixtures — the API conftest's
super-user seed (``seeded`` fixture) already has ``lineage.view`` via
the catalog sentinel, so a single authenticated session covers the
happy path. The 403 case spins up a fresh regular_user mid-test.
"""

from __future__ import annotations

import datetime as _dt
import xml.etree.ElementTree as ET
from typing import Any

import httpx
import pytest

from matofx.audit import AuditWriter
from matofx.config import Settings
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.tools.orm import ToolVersion
from matofx.users import make_password_hasher, make_user_service

pytestmark = pytest.mark.asyncio


_TEST_PASSWORD = "password12345"


async def _make_user(
    factory: SessionFactory,
    settings: Settings,
    audit: AuditWriter,
    *,
    username: str,
    role: str,
) -> tuple[str, str]:
    """Create a user with ``role``; return (username, password)."""
    svc = make_user_service(settings.auth, audit, hasher=make_password_hasher(settings.auth))
    async with session_scope(factory) as session:
        await svc.create_user(
            session,
            username=username,
            email=f"{username}@example.com",
            password=_TEST_PASSWORD,
            full_name=f"{username.title()} User",
            role=role,
        )
    return username, _TEST_PASSWORD


async def _login(client: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _seed_job(factory: SessionFactory, seeded: dict[str, Any]) -> int:
    """Seed one job under a fresh project/scenario; return its id."""
    async with session_scope(factory) as session:
        version = (await session.execute(_latest_version(seeded["tool_alias"]))).scalar_one()
        project = Project(name="Lineage project", creator_user_id=seeded["user_id"])
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="Lineage scenario")
        session.add(scenario)
        await session.flush()
        job = Job(
            scenario_id=scenario.id,
            name="lineage-job",
            tool_alias=seeded["tool_alias"],
            tool_version_id=version.id,
            state="complete",
            inputs={},
            created_by_user_id=seeded["user_id"],
            created_at=_dt.datetime.now(tz=_dt.UTC),
        )
        session.add(job)
        await session.flush()
        return int(job.id)


def _latest_version(alias: str):
    """Statement that selects the most recently inserted ToolVersion for ``alias``.

    The ``seeded`` fixture promotes the tool state from ``new`` →
    ``test`` → ``released``, and each promotion inserts a fresh
    snapshot — so a bare ``select(ToolVersion)`` returns three rows
    for ``material_properties``. ``.limit(1)`` keeps ``scalar_one``
    on the caller side correct.
    """
    from sqlalchemy import select  # noqa: PLC0415

    return (
        select(ToolVersion)
        .where(ToolVersion.alias == alias)
        .order_by(ToolVersion.id.desc())
        .limit(1)
    )


# ---- auth + permission gates --------------------------------------------


async def test_get_unauthenticated_returns_401(client: httpx.AsyncClient) -> None:
    """No bearer → 401."""
    response = await client.get("/api/lineage/job/1")
    assert response.status_code == 401, response.text


async def test_get_forbidden_for_regular_user_without_grant(
    client: httpx.AsyncClient,
    api_factory: SessionFactory,
    api_settings: Settings,
    api_audit: AuditWriter,
) -> None:
    """A regular user (no ``lineage.view`` in default set) → 403."""
    username, password = await _make_user(
        api_factory, api_settings, api_audit, username="reg", role="regular_user"
    )
    headers = await _login(client, username, password)
    response = await client.get("/api/lineage/job/1", headers=headers)
    assert response.status_code == 403, response.text
    assert "lineage" in response.json()["detail"].lower()


# ---- happy path ----------------------------------------------------------


async def test_get_returns_lineage_graph_centred_on_the_job(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """A super-user GET on an existing job returns the expected graph shape."""
    job_id = await _seed_job(api_factory, seeded)
    response = await client.get(
        f"/api/lineage/job/{job_id}?upstream=1&downstream=0", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["centre"] == f"job:{job_id}"
    by_kind = {n["kind"] for n in body["nodes"]}
    # The job centre + its three direct neighbours (user / scenario /
    # tool_version) all resolve at depth=1.
    assert NodeKindLiterals.JOB in by_kind
    assert NodeKindLiterals.USER in by_kind
    assert NodeKindLiterals.SCENARIO in by_kind
    assert NodeKindLiterals.TOOL_VERSION in by_kind
    assert body["soft_capped"] is False
    assert body["hidden_by_acl"] == 0


# ---- error contract ------------------------------------------------------


async def test_get_returns_404_for_missing_centre(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],  # noqa: ARG001 — auth_headers needs the seed
    auth_headers: dict[str, str],
) -> None:
    """Unknown job id → 404 (existence-hidden)."""
    response = await client.get("/api/lineage/job/9999999", headers=auth_headers)
    assert response.status_code == 404, response.text


async def test_get_returns_422_for_unknown_kind(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],  # noqa: ARG001
    auth_headers: dict[str, str],
) -> None:
    """Path ``kind`` not in :class:`NodeKind` → 422 with a helpful detail."""
    response = await client.get("/api/lineage/banana/1", headers=auth_headers)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "banana" in detail
    assert "job" in detail  # one of the accepted values is in the message


async def test_get_returns_422_for_depth_out_of_range(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """``upstream`` beyond the 0..20 range → 422 from the FastAPI Query validator."""
    job_id = await _seed_job(api_factory, seeded)
    response = await client.get(f"/api/lineage/job/{job_id}?upstream=100", headers=auth_headers)
    assert response.status_code == 422, response.text


# ---- export ---------------------------------------------------------------


async def test_export_returns_json_attachment(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """``format=json`` returns JSON with the right disposition header."""
    job_id = await _seed_job(api_factory, seeded)
    response = await client.get(
        f"/api/lineage/job/{job_id}/export?format=json&upstream=1",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert "application/json" in response.headers["content-type"]
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert f"lineage-job-{job_id}.json" in disposition
    body = response.json()
    assert body["centre"] == f"job:{job_id}"


async def test_export_returns_svg_attachment(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """``format=svg`` returns a self-contained SVG image attachment."""
    job_id = await _seed_job(api_factory, seeded)
    response = await client.get(
        f"/api/lineage/job/{job_id}/export?format=svg&upstream=1",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert "image/svg+xml" in response.headers["content-type"]
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert f"lineage-job-{job_id}.svg" in disposition
    # A real, parseable SVG root element.
    root = ET.fromstring(response.text)  # noqa: S314 — our own controlled serialiser output
    assert root.tag.endswith("svg")


async def test_export_returns_graphml_attachment(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """``format=graphml`` returns a well-formed GraphML attachment."""
    job_id = await _seed_job(api_factory, seeded)
    response = await client.get(
        f"/api/lineage/job/{job_id}/export?format=graphml&upstream=1",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert "graphml" in response.headers["content-type"]
    disposition = response.headers["content-disposition"]
    assert f"lineage-job-{job_id}.graphml" in disposition
    # Parses cleanly.
    root = ET.fromstring(response.text)  # noqa: S314 — input is our own controlled serialiser output
    assert root.tag.endswith("graphml")


async def test_export_returns_422_for_unknown_format(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
) -> None:
    """``format=cytoscape`` (not in the closed vocabulary) → 422."""
    job_id = await _seed_job(api_factory, seeded)
    response = await client.get(
        f"/api/lineage/job/{job_id}/export?format=cytoscape", headers=auth_headers
    )
    assert response.status_code == 422, response.text


# Tiny string-constant carrier so the assertion in
# test_get_returns_lineage_graph_centred_on_the_job stays self-contained
# without importing NodeKind into the test file.
class NodeKindLiterals:
    """Mirror of the NodeKind enum values the JSON wire uses (string)."""

    JOB = "job"
    USER = "user"
    SCENARIO = "scenario"
    TOOL_VERSION = "tool_version"
