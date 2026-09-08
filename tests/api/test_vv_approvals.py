"""End-to-end V&V approval flow through the unified approvals router (slice 6).

The key behaviour: a *V&V curator* who is neither an admin nor a
tool-approver (just holds the ``vv_curator`` capability) can see a
V&V record gate in their inbox and approve it via
``/api/approvals/decisions`` — eligibility for a V&V gate is the
``vv.curate`` capability, not the tool-approver flag. The approve path
must flip BOTH the ``approval_request`` row AND the underlying
``tool_vv_record``'s ``state`` to ``approved``.
"""

from __future__ import annotations

from typing import Any

import httpx
import sqlalchemy as sa
from fastapi import FastAPI

from matofx.api.deps import get_settings
from matofx.audit import AuditWriter
from matofx.config import FeatureSettings, Settings
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.features import Feature
from matofx.users import make_password_hasher, make_user_service
from matofx.users.role_service import make_role_service

_REV_PASSWORD = "revpassword1"


def _enable_features(app: FastAPI, api_settings: Settings) -> None:
    """Enable V&V tracking on the test app."""
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.VV_TRACKING.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _seed_curator(
    api_factory: SessionFactory,
    api_settings: Settings,
    api_audit: AuditWriter,
    *,
    actor_id: int,
) -> int:
    """Create 'rev' (regular user) + grant the vv_curator capability."""
    hasher = make_password_hasher(api_settings.auth)
    user_service = make_user_service(api_settings.auth, api_audit, hasher=hasher)
    role_service = make_role_service(api_audit)
    async with session_scope(api_factory) as session:
        rev = await user_service.create_user(
            session,
            username="rev",
            email="rev@example.com",
            password=_REV_PASSWORD,
            full_name="Rev Curator",
            role="regular_user",
        )
        await role_service.set_user_capability_roles(
            session,
            actor_user_id=actor_id,
            target_user_id=rev.id,
            role_names=["vv_curator"],
        )
        return rev.id


async def _login(client: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    resp = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


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


async def test_vv_gate_visible_and_approvable_by_curator_via_unified_inbox(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    api_audit: AuditWriter,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """A regular_user with vv_curator can decide a V&V gate via /api/approvals/decisions.

    Exercises the full cross-domain path: author submits a V&V draft;
    designated curator (who has neither admin nor tool-approver
    flags — just the capability) sees the gate in their inbox; approves
    via the unified decisions endpoint; both the approval_request and
    the V&V record's state flip to approved.
    """
    _enable_features(app, api_settings)
    rev_id = await _seed_curator(
        api_factory, api_settings, api_audit, actor_id=int(seeded["user_id"])
    )
    rev_headers = await _login(client, "rev", _REV_PASSWORD)
    tv_id = await _first_tool_version_id(api_factory)

    # root (super_user) authors a V&V draft directed at rev for review.
    created = await client.post(
        "/api/vv/tool-records",
        headers=auth_headers,
        json={"tool_version_id": tv_id, "kind": "verification"},
    )
    assert created.status_code == 201, created.text
    record_id = created.json()["id"]

    submitted = await client.post(
        f"/api/vv/tool-records/{record_id}/submit",
        headers=auth_headers,
        json={
            "designated_approver_user_id": rev_id,
            "designated_approver_username": "rev",
        },
    )
    assert submitted.status_code == 200, submitted.text

    # rev sees the gate in /api/users/me/approvals — kind discriminator
    # says vv_tool, target name reflects the V&V record's tool version.
    inbox = await client.get("/api/users/me/approvals", headers=rev_headers)
    assert inbox.status_code == 200, inbox.text
    rows = [r for r in inbox.json() if r["target_kind"] == "vv_tool"]
    assert len(rows) == 1, f"Expected one V&V gate in inbox, got {inbox.json()}"
    assert rows[0]["job_name"].startswith("Verification on")
    request_id = rows[0]["request_id"]

    # The rich detail endpoint surfaces the V&V record's review panel.
    detail = await client.get(f"/api/approvals/{request_id}", headers=rev_headers)
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert detail_body["target_kind"] == "vv_tool"
    row_names = {row["name"] for grp in detail_body["input_groups"] for row in grp["rows"]}
    assert {"tool", "kind", "envelope", "standards", "evidence"}.issubset(row_names)

    # rev approves via the unified decisions endpoint. The path forwards
    # through VVService.decide_tool_approval — both the approval_request
    # and the V&V record flip.
    decided = await client.post(
        "/api/approvals/decisions",
        headers=rev_headers,
        json={"decisions": [{"request_id": request_id, "decision": "approve"}]},
    )
    assert decided.status_code == 200, decided.text
    results = decided.json()["results"]
    assert len(results) == 1
    assert results[0]["ok"] is True, results[0]
    assert results[0]["status"] == "approved"
    assert results[0]["job_state"] == "approved"  # V&V record's state

    # Confirm the V&V record itself flipped (not just the approval_request).
    record_after = await client.get(f"/api/vv/tool-records/{record_id}", headers=auth_headers)
    assert record_after.status_code == 200
    body = record_after.json()
    assert body["state"] == "approved"
    assert body["decided_by_user_id"] == rev_id

    # Inbox no longer lists the now-decided gate.
    inbox_after = await client.get("/api/users/me/approvals", headers=rev_headers)
    remaining_vv = [r for r in inbox_after.json() if r["target_kind"] == "vv_tool"]
    assert remaining_vv == []


async def test_vv_gate_reject_via_unified_inbox_flips_record_state(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    api_audit: AuditWriter,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Reject via the unified inbox flips the V&V record to ``rejected``."""
    _enable_features(app, api_settings)
    rev_id = await _seed_curator(
        api_factory, api_settings, api_audit, actor_id=int(seeded["user_id"])
    )
    rev_headers = await _login(client, "rev", _REV_PASSWORD)
    tv_id = await _first_tool_version_id(api_factory)

    created = await client.post(
        "/api/vv/tool-records",
        headers=auth_headers,
        json={"tool_version_id": tv_id, "kind": "validation"},
    )
    record_id = created.json()["id"]
    submitted = await client.post(
        f"/api/vv/tool-records/{record_id}/submit",
        headers=auth_headers,
        json={"designated_approver_user_id": rev_id, "designated_approver_username": "rev"},
    )
    inbox = await client.get("/api/users/me/approvals", headers=rev_headers)
    request_id = next(r["request_id"] for r in inbox.json() if r["target_kind"] == "vv_tool")
    del submitted

    decided = await client.post(
        "/api/approvals/decisions",
        headers=rev_headers,
        json={
            "decisions": [
                {
                    "request_id": request_id,
                    "decision": "deny",
                    "reason": "evidence dataset is too narrow",
                }
            ]
        },
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["results"][0]["ok"] is True
    assert decided.json()["results"][0]["status"] == "denied"
    assert decided.json()["results"][0]["job_state"] == "rejected"

    record_after = await client.get(f"/api/vv/tool-records/{record_id}", headers=auth_headers)
    body = record_after.json()
    assert body["state"] == "rejected"
    assert body["decision_reason"] == "evidence dataset is too narrow"


async def test_vv_gate_reject_without_reason_is_rejected(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    api_audit: AuditWriter,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Rejecting a V&V gate via the unified inbox without a reason fails per-item.

    The decisions endpoint applies each item in its own savepoint, so
    one bad item is reported as ``ok=False`` and the rest of the batch
    still commits. We send only the one item here so the result is
    deterministic.
    """
    _enable_features(app, api_settings)
    rev_id = await _seed_curator(
        api_factory, api_settings, api_audit, actor_id=int(seeded["user_id"])
    )
    rev_headers = await _login(client, "rev", _REV_PASSWORD)
    tv_id = await _first_tool_version_id(api_factory)

    created = await client.post(
        "/api/vv/tool-records",
        headers=auth_headers,
        json={"tool_version_id": tv_id, "kind": "verification"},
    )
    record_id = created.json()["id"]
    await client.post(
        f"/api/vv/tool-records/{record_id}/submit",
        headers=auth_headers,
        json={"designated_approver_user_id": rev_id, "designated_approver_username": "rev"},
    )
    inbox = await client.get("/api/users/me/approvals", headers=rev_headers)
    request_id = next(r["request_id"] for r in inbox.json() if r["target_kind"] == "vv_tool")

    decided = await client.post(
        "/api/approvals/decisions",
        headers=rev_headers,
        json={"decisions": [{"request_id": request_id, "decision": "deny", "reason": ""}]},
    )
    assert decided.status_code == 200, decided.text
    result = decided.json()["results"][0]
    assert result["ok"] is False
    # The V&V record stays in pending (the savepoint rolled back).
    record_after = await client.get(f"/api/vv/tool-records/{record_id}", headers=auth_headers)
    assert record_after.json()["state"] == "pending"
