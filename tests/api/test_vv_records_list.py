"""API tests for the cross-install V&V register (``GET /api/vv/records``).

The rework's backbone — the browsable "all records" list the id-keyed
endpoints never offered. Covers label enrichment (target / author /
decider resolved to names, never raw ids), the state / kind / search /
type filters, pagination, and that the single-record GET now carries the
same resolved labels.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from matofx.api.deps import get_settings
from matofx.config import FeatureSettings, Settings
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.features import Feature

pytestmark = pytest.mark.asyncio


def _enable_feature(app: FastAPI, api_settings: Settings) -> None:
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.VV_TRACKING.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _first_tool_version_id(api_factory: SessionFactory) -> int:
    async with session_scope(api_factory) as session:
        return int(
            (
                await session.execute(
                    sa.text("SELECT id FROM tool_version ORDER BY created_at ASC LIMIT 1")
                )
            ).scalar_one()
        )


async def _make_two_drafts(
    client: httpx.AsyncClient, headers: dict[str, str], tv_id: int
) -> tuple[int, int]:
    """Author a verification + a validation draft on the seeded tool version."""
    a = await client.post(
        "/api/vv/tool-records",
        headers=headers,
        json={"tool_version_id": tv_id, "kind": "verification", "notes": "alpha regression"},
    )
    assert a.status_code == 201, a.text
    b = await client.post(
        "/api/vv/tool-records",
        headers=headers,
        json={"tool_version_id": tv_id, "kind": "validation", "notes": "beta rig data"},
    )
    assert b.status_code == 201, b.text
    return int(a.json()["id"]), int(b.json()["id"])


async def test_register_lists_both_with_resolved_labels(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """The register returns every record with target + author names, not ids."""
    _enable_feature(app, api_settings)
    del seeded
    tv_id = await _first_tool_version_id(api_factory)
    await _make_two_drafts(client, auth_headers, tv_id)

    resp = await client.get("/api/vv/records", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    rows = body["rows"]
    assert {r["vv_kind"] for r in rows} == {"verification", "validation"}
    for r in rows:
        # target_label is the resolved "Name vX", not the "#id" fallback.
        assert r["target_label"]
        assert not r["target_label"].startswith("tool version #")
        assert " v" in r["target_label"]
        # author resolved to "Full Name (username)".
        assert "root" in r["recorded_by_label"]
        assert r["vv_kind_label"] in {"Verification", "Validation"}
        assert r["record_kind"] == "tool"


async def test_register_state_kind_and_search_filters(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """State / kind / search / type filters narrow the register."""
    _enable_feature(app, api_settings)
    del seeded
    tv_id = await _first_tool_version_id(api_factory)
    await _make_two_drafts(client, auth_headers, tv_id)

    # Both are drafts.
    drafts = await client.get("/api/vv/records", headers=auth_headers, params={"state": "draft"})
    assert drafts.status_code == 200
    assert drafts.json()["total"] == 2

    # No approved records yet.
    approved = await client.get(
        "/api/vv/records", headers=auth_headers, params={"state": "approved"}
    )
    assert approved.json()["total"] == 0

    # Kind filter.
    val = await client.get("/api/vv/records", headers=auth_headers, params={"kind": "validation"})
    assert {r["vv_kind"] for r in val.json()["rows"]} == {"validation"}

    # Search matches the author label; a nonsense term matches nothing.
    hit = await client.get("/api/vv/records", headers=auth_headers, params={"search": "root"})
    assert hit.json()["total"] == 2
    miss = await client.get(
        "/api/vv/records", headers=auth_headers, params={"search": "zzz-nomatch"}
    )
    assert miss.json()["total"] == 0

    # Type filter: no job records exist.
    jobs = await client.get("/api/vv/records", headers=auth_headers, params={"record_kind": "job"})
    assert jobs.json()["total"] == 0


async def test_register_pagination_and_bad_record_kind(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Pagination reports the true total; an unknown record_kind is 400."""
    _enable_feature(app, api_settings)
    del seeded
    tv_id = await _first_tool_version_id(api_factory)
    await _make_two_drafts(client, auth_headers, tv_id)

    page1 = await client.get(
        "/api/vv/records", headers=auth_headers, params={"page_size": 1, "page": 1}
    )
    assert page1.status_code == 200
    body = page1.json()
    assert body["total"] == 2
    assert len(body["rows"]) == 1
    assert body["page_size"] == 1

    bad = await client.get(
        "/api/vv/records", headers=auth_headers, params={"record_kind": "widget"}
    )
    assert bad.status_code == 400


async def test_approved_record_carries_decider_label(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """An approved record shows the decider's name in the register + detail."""
    _enable_feature(app, api_settings)
    del seeded
    tv_id = await _first_tool_version_id(api_factory)
    rec_a, _rec_b = await _make_two_drafts(client, auth_headers, tv_id)

    approver = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "approver",
            "email": "approver@example.com",
            "full_name": "Ada Approver",
            "password": "ApproverPwd!23",
            "role": "super_user",
        },
    )
    assert approver.status_code in (200, 201), approver.text
    await client.post(
        f"/api/vv/tool-records/{rec_a}/submit",
        headers=auth_headers,
        json={
            "designated_approver_user_id": approver.json()["id"],
            "designated_approver_username": "approver",
        },
    )
    login = await client.post(
        "/api/auth/login", json={"username": "approver", "password": "ApproverPwd!23"}
    )
    approver_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    approve = await client.post(
        f"/api/vv/tool-records/{rec_a}/approve",
        headers=approver_headers,
        json={"reason": None},
    )
    assert approve.status_code == 200, approve.text

    # Register, filtered to approved, carries the decider label.
    reg = await client.get("/api/vv/records", headers=auth_headers, params={"state": "approved"})
    rows = reg.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["record_id"] == rec_a
    assert "approver" in (rows[0]["decided_by_label"] or "")

    # Single-record GET now resolves the same labels.
    detail = await client.get(f"/api/vv/tool-records/{rec_a}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    d = detail.json()
    assert d["target_label"]
    assert " v" in d["target_label"]
    assert "root" in (d["recorded_by_label"] or "")
    assert "approver" in (d["decided_by_label"] or "")


async def test_create_with_multiple_evidence_items(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """A record can carry several evidence entries of the same or mixed type."""
    _enable_feature(app, api_settings)
    del seeded
    tv_id = await _first_tool_version_id(api_factory)
    create = await client.post(
        "/api/vv/tool-records",
        headers=auth_headers,
        json={
            "tool_version_id": tv_id,
            "kind": "verification",
            "evidence": [
                {"kind": "url", "url": "https://example.com/report-a"},
                {"kind": "url", "url": "https://example.com/report-b"},
            ],
        },
    )
    assert create.status_code == 201, create.text
    rec_id = create.json()["id"]

    # The single-record GET resolves the full list.
    detail = await client.get(f"/api/vv/tool-records/{rec_id}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    items = detail.json()["evidence_items"]
    assert len(items) == 2
    assert {i["url"] for i in items} == {
        "https://example.com/report-a",
        "https://example.com/report-b",
    }
    assert all(i["kind"] == "url" for i in items)

    # The register summarises the count in evidence_label (search matches the
    # target / author / kind / notes — not evidence URLs — so we list all).
    reg = await client.get("/api/vv/records", headers=auth_headers)
    rows = [r for r in reg.json()["rows"] if r["record_id"] == rec_id]
    assert rows
    assert rows[0]["evidence_label"] == "2 evidence items"
    assert len(rows[0]["evidence_items"]) == 2


async def test_evidence_item_validation_rejects_incomplete(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """A 'library' evidence item without a file_version_id is a 422."""
    _enable_feature(app, api_settings)
    del seeded
    tv_id = await _first_tool_version_id(api_factory)
    resp = await client.post(
        "/api/vv/tool-records",
        headers=auth_headers,
        json={
            "tool_version_id": tv_id,
            "kind": "verification",
            "evidence": [{"kind": "library"}],
        },
    )
    assert resp.status_code == 422, resp.text


async def test_records_absent_when_feature_off(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """With the flag off the register route is absent (404)."""
    resp = await client.get("/api/vv/records", headers=auth_headers)
    assert resp.status_code == 404
