"""API tests for the V&V router (ADR-0011 §7).

Covers the feature-flag gating (routes absent when off), the badge
endpoint (the most important — proves the bundle-hash inheritance
query works through the HTTP surface), the full lifecycle through the
API (create → submit → approve), the curator inbox + picker, and the
not-found / permission cases. The bundle-hash inheritance test
inserts an extra ``tool_version`` row sharing the seeded tool's hash
and asserts the badge for the new row reflects an approval on the
first.
"""

from __future__ import annotations

import datetime as _dt
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
from matofx.tools.orm import ToolVersion


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


def _enable_feature(app: FastAPI, api_settings: Settings) -> None:
    """Flip ``features.vv_tracking`` on for this app instance."""
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.VV_TRACKING.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _first_tool_version_id(api_factory: SessionFactory) -> int:
    """Return the seeded tool's first ``tool_version`` row id."""
    async with session_scope(api_factory) as session:
        return int(
            (
                await session.execute(
                    sa.text("SELECT id FROM tool_version ORDER BY created_at ASC LIMIT 1")
                )
            ).scalar_one()
        )


async def _seed_cohort_mate(
    api_factory: SessionFactory,
    *,
    alias: str,
    bundle_hash: str,
    created_by_user_id: int,
    version: str = "9.9.9",
) -> int:
    """Insert an extra ``tool_version`` sharing the given alias + bundle_hash."""
    async with session_scope(api_factory) as session:
        tv = ToolVersion(
            alias=alias,
            version=version,
            descriptor_snapshot={"alias": alias},
            bundle_path=f"bundle/{alias}/{version}",
            bundle_hash=bundle_hash,
            created_at=_utcnow(),
            created_by_user_id=created_by_user_id,
            change_summary="test seed",
            changes={},
        )
        session.add(tv)
        await session.flush()
        return int(tv.id)


# ---- Feature flag --------------------------------------------------


async def test_routes_absent_when_feature_off(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """With the flag off the V&V surface is *absent* (404)."""
    badge = await client.get("/api/vv/status/tool/1", headers=auth_headers)
    assert badge.status_code == 404
    create = await client.post(
        "/api/vv/tool-records",
        headers=auth_headers,
        json={"tool_version_id": 1, "kind": "verification"},
    )
    assert create.status_code == 404


# ---- Badge endpoint (the inheritance query through HTTP) ----------


async def test_badge_endpoint_empty_when_no_records(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """``GET /api/vv/status/tool/{id}`` returns ``kind=none`` with no records."""
    _enable_feature(app, api_settings)
    del seeded
    tv_id = await _first_tool_version_id(api_factory)

    resp = await client.get(f"/api/vv/status/tool/{tv_id}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tool_version_id"] == tv_id
    assert body["kind"] == "none"
    assert body["has_approved_verification"] is False
    assert body["has_approved_validation"] is False
    assert body["inherited"] is False
    assert body["contributing_version_ids"] == []


async def test_numeric_factors_lists_numeric_inputs_by_label(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """``GET /api/vv/numeric-factors/{id}`` returns only numeric inputs, labelled."""
    _enable_feature(app, api_settings)
    del seeded
    # Seed a tool version whose descriptor carries a mix of input types.
    async with session_scope(api_factory) as session:
        from matofx.tools.orm import Tool  # noqa: PLC0415

        author_id = int(
            (await session.execute(sa.text("SELECT id FROM \"user\" LIMIT 1"))).scalar_one()
        )
        # tool_version.alias FKs the tool table — seed the parent tool first.
        session.add(
            Tool(
                alias="ranged_tool",
                name="Ranged tool",
                version="1.0",
                state="released",
                descriptor={"alias": "ranged_tool"},
                bundle_path="bundle/ranged_tool",
                installed_by_user_id=author_id,
            )
        )
        await session.flush()
        tv = ToolVersion(
            alias="ranged_tool",
            version="1.0.0",
            descriptor_snapshot={
                "alias": "ranged_tool",
                "inputs": [
                    {"name": "temperature", "label": "Soak temperature", "type": "float",
                     "unit": "K", "minimum": 250.0, "maximum": 800.0},
                    {"name": "cycles", "label": "Load cycles", "type": "integer"},
                    {"name": "material", "label": "Material", "type": "select",
                     "options": ["steel", "alu"]},
                ],
            },
            bundle_path="bundle/ranged_tool/1.0.0",
            bundle_hash="eeee" * 16,
            created_at=_utcnow(),
            created_by_user_id=author_id,
            change_summary="test seed",
            changes={},
        )
        session.add(tv)
        await session.flush()
        tv_id = int(tv.id)

    resp = await client.get(f"/api/vv/numeric-factors/{tv_id}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    factors = resp.json()
    by_name = {f["name"]: f for f in factors}
    assert set(by_name) == {"temperature", "cycles"}  # the select is excluded
    assert by_name["temperature"]["label"] == "Soak temperature"
    assert by_name["temperature"]["unit"] == "K"
    assert by_name["temperature"]["minimum"] == 250.0
    assert by_name["cycles"]["label"] == "Load cycles"


async def test_full_lifecycle_through_api(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """create → submit → approve through the HTTP surface.

    The seeded user is ``super_user``; the super_user catalog sentinel
    grants every Permission, so they're a curator. Submit ON also
    requires a designated approver — we seed a second curator user via
    the user-admin endpoint to satisfy the directed-approver gate.
    """
    _enable_feature(app, api_settings)
    del seeded
    tv_id = await _first_tool_version_id(api_factory)

    # Create a second super_user so the directed-approver gate has an
    # eligible "other" curator. Use the admin user endpoint.
    second_user_resp = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "approver",
            "email": "approver@example.com",
            "full_name": "Approver",
            "password": "ApproverPwd!23",
            "role": "super_user",
        },
    )
    assert second_user_resp.status_code in (200, 201), second_user_resp.text

    # 1. Create a draft tool V&V record.
    create_resp = await client.post(
        "/api/vv/tool-records",
        headers=auth_headers,
        json={
            "tool_version_id": tv_id,
            "kind": "verification",
            "notes": "regression suite passes",
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    record_id = create_resp.json()["id"]
    assert create_resp.json()["state"] == "draft"

    # 2. Submit it for review (directed to the second curator).
    submit_resp = await client.post(
        f"/api/vv/tool-records/{record_id}/submit",
        headers=auth_headers,
        json={
            "designated_approver_user_id": second_user_resp.json()["id"],
            "designated_approver_username": "approver",
        },
    )
    assert submit_resp.status_code == 200, submit_resp.text
    assert submit_resp.json()["state"] == "pending"

    # 3. Log in as the approver and approve.
    approver_login = await client.post(
        "/api/auth/login",
        json={"username": "approver", "password": "ApproverPwd!23"},
    )
    assert approver_login.status_code == 200, approver_login.text
    approver_token = approver_login.json()["access_token"]
    approver_headers = {"Authorization": f"Bearer {approver_token}"}
    approve_resp = await client.post(
        f"/api/vv/tool-records/{record_id}/approve",
        headers=approver_headers,
        json={"reason": None},
    )
    assert approve_resp.status_code == 200, approve_resp.text
    body = approve_resp.json()
    assert body["state"] == "approved"
    assert body["decided_by_user_id"] == second_user_resp.json()["id"]

    # 4. Badge now reflects the approval.
    badge_resp = await client.get(f"/api/vv/status/tool/{tv_id}", headers=auth_headers)
    assert badge_resp.status_code == 200
    badge = badge_resp.json()
    assert badge["has_approved_verification"] is True
    assert badge["kind"] == "verified_only"


async def test_badge_inherits_across_cohort_mate(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """The badge for a cohort-mate inherits via HTTP (the Option C invariant)."""
    _enable_feature(app, api_settings)
    # Resolve the seeded tool's bundle_hash + alias to build a cohort-mate.
    async with session_scope(api_factory) as session:
        row = (
            await session.execute(
                sa.text(
                    "SELECT id, alias, bundle_hash FROM tool_version "
                    "ORDER BY created_at ASC LIMIT 1"
                )
            )
        ).first()
        assert row is not None
        tv_a_id, alias, bundle_hash = row[0], row[1], row[2]
    # If the seeded tool's bundle_hash is empty (the "unknown" sentinel),
    # inheritance doesn't apply and the test would be vacuous — skip it.
    if not bundle_hash:
        pytest.skip("Seeded tool has empty bundle_hash — inheritance test n/a")

    cohort_mate_id = await _seed_cohort_mate(
        api_factory,
        alias=alias,
        bundle_hash=bundle_hash,
        created_by_user_id=seeded["user_id"],
    )

    # Record an approval on tv_a directly via the API.
    second_user_resp = await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "approver",
            "email": "approver@example.com",
            "full_name": "Approver",
            "password": "ApproverPwd!23",
            "role": "super_user",
        },
    )
    assert second_user_resp.status_code in (200, 201)
    create_resp = await client.post(
        "/api/vv/tool-records",
        headers=auth_headers,
        json={"tool_version_id": tv_a_id, "kind": "validation"},
    )
    record_id = create_resp.json()["id"]
    await client.post(
        f"/api/vv/tool-records/{record_id}/submit",
        headers=auth_headers,
        json={
            "designated_approver_user_id": second_user_resp.json()["id"],
            "designated_approver_username": "approver",
        },
    )
    approver_login = await client.post(
        "/api/auth/login",
        json={"username": "approver", "password": "ApproverPwd!23"},
    )
    approver_headers = {"Authorization": f"Bearer {approver_login.json()['access_token']}"}
    approve_resp = await client.post(
        f"/api/vv/tool-records/{record_id}/approve",
        headers=approver_headers,
        json={"reason": None},
    )
    assert approve_resp.status_code == 200, approve_resp.text

    # Badge for the cohort-mate reflects the approval on tv_a — inheritance.
    mate_badge = await client.get(f"/api/vv/status/tool/{cohort_mate_id}", headers=auth_headers)
    assert mate_badge.status_code == 200, mate_badge.text
    body = mate_badge.json()
    assert body["has_approved_validation"] is True
    assert body["inherited"] is True
    assert tv_a_id in body["contributing_version_ids"]
    assert body["cohort_bundle_hash"] == bundle_hash


# ---- Reads ---------------------------------------------------------


async def test_get_tool_record_404_for_missing(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """A request for a non-existent record returns 404."""
    _enable_feature(app, api_settings)
    del seeded
    resp = await client.get("/api/vv/tool-records/9999", headers=auth_headers)
    assert resp.status_code == 404


async def test_list_tool_records_filtered_by_tool_version(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """``GET /api/vv/tool-records?tool_version_id=…`` returns matching rows only."""
    _enable_feature(app, api_settings)
    del seeded
    tv_id = await _first_tool_version_id(api_factory)
    resp = await client.get(f"/api/vv/tool-records?tool_version_id={tv_id}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


async def test_curator_picker_returns_other_curators(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """``GET /api/vv/curators`` returns other curators, excluding self."""
    _enable_feature(app, api_settings)
    del seeded
    # Seed a second user so the picker has something to return.
    await client.post(
        "/api/admin/users",
        headers=auth_headers,
        json={
            "username": "approver",
            "email": "approver@example.com",
            "full_name": "Approver",
            "password": "ApproverPwd!23",
            "role": "super_user",
        },
    )
    resp = await client.get("/api/vv/curators", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    usernames = {row["username"] for row in resp.json()}
    assert "approver" in usernames
    # Self is excluded — the seeded root user shouldn't appear.
    assert "root" not in usernames


async def test_envelope_check_silent_when_no_validation(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """No approved validation → envelope-check returns ``null`` (no warn)."""
    _enable_feature(app, api_settings)
    del seeded
    tv_id = await _first_tool_version_id(api_factory)
    resp = await client.post(
        "/api/vv/envelope-check",
        headers=auth_headers,
        json={"tool_version_id": tv_id, "form_values": {"temperature": 99999.0}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() is None


async def test_pending_inbox_requires_authentication(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
) -> None:
    """Unauthenticated request to the curator inbox returns 401.

    The curator-only-permission gate (403 for an authenticated
    non-curator) is exercised at the service layer; the API surface
    just relays whatever ``get_current_user`` does — an unauthenticated
    caller never reaches the router body.
    """
    _enable_feature(app, api_settings)
    del seeded
    resp = await client.get("/api/vv/pending")
    assert resp.status_code == 401
