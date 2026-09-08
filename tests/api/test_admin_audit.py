"""Tests for the Admin → Audit log HTTP surface.

Covers the reworked filter bar's server side: the User filter resolving
id / name / username to actor rows, the multi-value Action / Resource
type filters, the Actor type narrow, actor-label resolution on returned
rows, the ``/filters`` dropdown-options endpoint, and the CSV export.
The permission gate is shared with Explore Database and covered there.
"""

from __future__ import annotations

import csv
import io
from typing import Any

import httpx
import pytest

from matofx.audit import AuditWriter
from matofx.audit.schema import RecordInput
from matofx.db import session_scope
from matofx.db.session import SessionFactory


async def _seed_records(factory: SessionFactory, audit: AuditWriter, user_id: int) -> None:
    """Append a handful of chained records spanning users / system / services."""
    records = [
        RecordInput(
            actor=f"user:{user_id}",
            actor_type="user",
            action="login.success",
            resource_type="session",
            resource_id="1",
            outcome="success",
        ),
        RecordInput(
            actor=f"user:{user_id}",
            actor_type="user",
            action="job.submit",
            resource_type="job",
            resource_id="42",
            outcome="success",
        ),
        RecordInput(
            actor="system",
            actor_type="system",
            action="retention.sweep",
            resource_type="job",
            resource_id="7",
            outcome="success",
        ),
        RecordInput(
            actor="service:gpu-scheduler",
            actor_type="service",
            action="job.dispatch",
            resource_type="job",
            resource_id="42",
            outcome="failure",
        ),
    ]
    async with session_scope(factory) as session:
        for rec in records:
            await audit.record(session, rec)


@pytest.mark.asyncio
async def test_filters_endpoint_lists_present_values(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """``/filters`` returns only actions / resource types / actor types in use."""
    await _seed_records(api_factory, api_audit, seeded["user_id"])
    r = await client.get("/api/admin/audit/filters", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "job.submit" in body["actions"]
    assert "retention.sweep" in body["actions"]
    assert "job" in body["resource_types"]
    assert {"user", "system", "service"} <= set(body["actor_types"])
    # Sorted for stable rendering.
    assert body["actions"] == sorted(body["actions"])


@pytest.mark.asyncio
async def test_user_filter_resolves_by_username(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """Typing the username returns only that user's rows, with a resolved label."""
    await _seed_records(api_factory, api_audit, seeded["user_id"])
    r = await client.get(
        "/api/admin/audit/log",
        headers=auth_headers,
        params={"user": seeded["username"]},
    )
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert rows, "expected the seeded user's rows"
    assert all(row["actor"] == f"user:{seeded['user_id']}" for row in rows)
    # actor_label is the friendly rendering, not the raw token.
    assert all(seeded["username"] in (row["actor_label"] or "") for row in rows)


@pytest.mark.asyncio
async def test_user_filter_by_id_and_no_match(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """Numeric id resolves to that user's rows; an unknown user yields zero rows.

    (Exact counts aren't asserted — the ``seeded`` fixture and the login
    both write their own rows under this user; the point is that *only*
    this actor's rows come back, and a non-matching term returns none.)
    """
    await _seed_records(api_factory, api_audit, seeded["user_id"])
    by_id = await client.get(
        "/api/admin/audit/log",
        headers=auth_headers,
        params={"user": str(seeded["user_id"]), "page_size": "500"},
    )
    assert by_id.status_code == 200, by_id.text
    rows = by_id.json()["rows"]
    assert rows
    assert all(row["actor"] == f"user:{seeded['user_id']}" for row in rows)
    seeded_actions = {row["action"] for row in rows}
    assert {"login.success", "job.submit"} <= seeded_actions

    missing = await client.get(
        "/api/admin/audit/log",
        headers=auth_headers,
        params={"user": "nobody-xyz"},
    )
    assert missing.status_code == 200, missing.text
    assert missing.json()["total_rows"] == 0


@pytest.mark.asyncio
async def test_action_multi_and_actor_type_filters(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """Repeated ``action`` params OR together; ``actor_type`` narrows to a class."""
    await _seed_records(api_factory, api_audit, seeded["user_id"])
    multi = await client.get(
        "/api/admin/audit/log",
        headers=auth_headers,
        params=[("action", "login.success"), ("action", "retention.sweep")],
    )
    assert multi.status_code == 200, multi.text
    actions = {row["action"] for row in multi.json()["rows"]}
    assert actions == {"login.success", "retention.sweep"}

    system_only = await client.get(
        "/api/admin/audit/log",
        headers=auth_headers,
        params={"actor_type": "system"},
    )
    assert system_only.status_code == 200, system_only.text
    rows = system_only.json()["rows"]
    assert rows
    assert all(row["actor_type"] == "system" for row in rows)


@pytest.mark.asyncio
async def test_bad_actor_type_is_400(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
) -> None:
    """An out-of-vocabulary actor_type is rejected."""
    del seeded
    r = await client.get(
        "/api/admin/audit/log",
        headers=auth_headers,
        params={"actor_type": "robot"},
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_export_csv_reflects_filter_and_resolves_label(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> None:
    """CSV export honours the filter and carries the resolved user label."""
    await _seed_records(api_factory, api_audit, seeded["user_id"])
    r = await client.get(
        "/api/admin/audit/export",
        headers=auth_headers,
        params={"resource_type": "job"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")

    reader = csv.DictReader(io.StringIO(r.text))
    rows = list(reader)
    # Three of the four seeded records target resource_type "job"; the
    # export is unpaginated, so every row must be a job row.
    assert len(rows) >= 3
    assert {row["resource_type"] for row in rows} == {"job"}
    assert {row["resource_id"] for row in rows} >= {"42", "7"}
    # The user row carries the friendly label alongside the raw actor token.
    user_rows = [row for row in rows if row["actor"] == f"user:{seeded['user_id']}"]
    assert user_rows
    assert all(seeded["username"] in row["actor_label"] for row in user_rows)
