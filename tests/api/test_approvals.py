"""Phase 4: the approvals API end-to-end.

The service layer (ApprovalService / decide_approval /
ensure_outputs_accessible) is exhaustively unit-tested under
``tests/approvals``; here we pin the HTTP wiring the UI rides on:
the gate at submit, the inbox list / summary / rich-review detail,
eligibility hiding, and the per-item-isolated batch decisions.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import httpx
import pytest

from matofx.audit import AuditWriter
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.tools.descriptor import InputField, InputGroup, ToolDescriptor


async def _seed_pre_tool(
    factory: SessionFactory,
    *,
    alias: str,
    approver_username: str,
    bundle_dir: Path,
    installed_by: int,
) -> None:
    """Seed a released tool whose captured snapshot is pre-approval."""
    from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415

    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "m.py").write_text("print('noop')\n", encoding="utf-8")
    descriptor = ToolDescriptor(
        alias=alias,
        name="Pre Tool",
        version="2.0",
        description="needs sign-off",
        main_script="m.py",
        inputs=[
            InputField(
                name="temperature",
                label="Inlet temperature",
                type="float",
                unit="C",
                default=300,
                group="bc",
            ),
            InputField(name="mesh", label="Mesh size", type="float", default=2),
        ],
        input_groups=[InputGroup(name="bc", label="Boundary conditions", column=1, row=1)],
        approvals_policy="pre",
        approver_usernames=[approver_username],
    ).model_dump(mode="json")
    async with session_scope(factory) as session:
        session.add(
            Tool(
                alias=alias,
                name="Pre Tool",
                version="2.0",
                state="released",
                descriptor=descriptor,
                bundle_path=str(bundle_dir),
                installed_by_user_id=installed_by,
            )
        )
        session.add(
            ToolVersion(
                alias=alias,
                version="2.0",
                descriptor_snapshot=descriptor,
                bundle_path=str(bundle_dir),
                created_at=_dt.datetime.now(tz=_dt.UTC),
                created_by_user_id=installed_by,
                change_summary="seed",
                changes={},
            )
        )


async def _make_approver(
    factory: SessionFactory,
    api_settings: Any,
    api_audit: AuditWriter,
    *,
    username: str,
    is_approver: bool,
) -> None:
    from matofx.users import make_password_hasher, make_user_service  # noqa: PLC0415

    user_service = make_user_service(
        api_settings.auth,
        api_audit,
        hasher=make_password_hasher(api_settings.auth),
    )
    async with session_scope(factory) as session:
        await user_service.create_user(
            session,
            username=username,
            email=f"{username}@example.com",
            password="approverpass123",
            full_name=username.title(),
            is_approver=is_approver,
        )


async def _login(client: httpx.AsyncClient, username: str) -> dict[str, str]:
    resp = await client.post(
        "/api/auth/login",
        json={"username": username, "password": "approverpass123"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _project_scenario(
    client: httpx.AsyncClient, auth_headers: dict[str, str], name: str
) -> int:
    p = await client.post(
        "/api/projects", headers=auth_headers, json={"name": name, "description": ""}
    )
    assert p.status_code == 201, p.text
    s = await client.post(
        f"/api/projects/{p.json()['id']}/scenarios",
        headers=auth_headers,
        json={"name": "S", "description": ""},
    )
    assert s.status_code == 201, s.text
    return int(s.json()["id"])


async def _draft_and_hold(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    *,
    scenario_id: int,
    alias: str,
    inputs: dict[str, Any],
    justification: str | None = None,
) -> int:
    """Create a draft + queue it → held in pending_approval. Returns job id."""
    d = await client.post(
        f"/api/scenarios/{scenario_id}/draft-jobs",
        headers=auth_headers,
        json={"tool_alias": alias, "name": "run"},
    )
    assert d.status_code == 201, d.text
    job_id = int(d.json()["id"])
    queue_body: dict[str, Any] = {"inputs": inputs}
    if justification is not None:
        queue_body["justification"] = justification
    q = await client.post(
        f"/api/jobs/{job_id}/queue",
        headers=auth_headers,
        json=queue_body,
    )
    assert q.status_code == 200, q.text
    body = q.json()
    assert body["state"] == "pending_approval"
    assert body["storage_location_id"] is None  # DB-only hold, not staged
    return job_id


@pytest.mark.asyncio
async def test_pre_flow_end_to_end(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_settings: Any,
    api_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    await _make_approver(api_factory, api_settings, api_audit, username="appr", is_approver=True)
    await _seed_pre_tool(
        api_factory,
        alias="pretool",
        approver_username="appr",
        bundle_dir=tmp_path / "prebundle",
        installed_by=seeded["user_id"],
    )
    scenario_id = await _project_scenario(client, auth_headers, "P1")
    job_id = await _draft_and_hold(
        client,
        auth_headers,
        scenario_id=scenario_id,
        alias="pretool",
        inputs={"temperature": 450, "mesh": 2},
        justification="Q3 packaging review — hotter inlet",
    )

    appr = await _login(client, "appr")

    # Inbox + summary.
    lst = await client.get("/api/users/me/approvals", headers=appr)
    assert lst.status_code == 200, lst.text
    rows = lst.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "pre"
    assert row["job_id"] == job_id
    assert row["tool_name"] == "Pre Tool"
    request_id = row["request_id"]

    summ = await client.get("/api/users/me/approvals/summary", headers=appr)
    assert summ.json() == {"can_approve": True, "pending": 1}

    # Rich review detail.
    det = await client.get(f"/api/approvals/{request_id}", headers=appr)
    assert det.status_code == 200, det.text
    d = det.json()
    assert d["tool_name"] == "Pre Tool"
    assert d["tool_version"] == "2.0"
    group_labels = {g["label"] for g in d["input_groups"]}
    assert "Boundary conditions" in group_labels
    bc = next(g for g in d["input_groups"] if g["label"] == "Boundary conditions")
    temp_row = next(r for r in bc["rows"] if r["name"] == "temperature")
    assert temp_row["value"] == 450
    assert temp_row["default"] == 300
    assert temp_row["changed_from_default"] is True
    assert temp_row["unit"] == "C"
    assert d["justification"] == "Q3 packaging review — hotter inlet"

    # Approve → job queued.
    dec = await client.post(
        "/api/approvals/decisions",
        headers=appr,
        json={"decisions": [{"request_id": request_id, "decision": "approve"}]},
    )
    assert dec.status_code == 200, dec.text
    result = dec.json()["results"][0]
    assert result["ok"] is True
    assert result["status"] == "approved"
    assert result["job_state"] == "queued"

    job = await client.get(f"/api/jobs/{job_id}", headers=auth_headers)
    assert job.json()["state"] == "queued"

    # Inbox drains.
    summ2 = await client.get("/api/users/me/approvals/summary", headers=appr)
    assert summ2.json() == {"can_approve": True, "pending": 0}


@pytest.mark.asyncio
async def test_non_approver_sees_nothing_and_detail_is_hidden(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_settings: Any,
    api_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    await _make_approver(api_factory, api_settings, api_audit, username="appr", is_approver=True)
    await _make_approver(api_factory, api_settings, api_audit, username="nobody", is_approver=False)
    await _seed_pre_tool(
        api_factory,
        alias="pretool",
        approver_username="appr",
        bundle_dir=tmp_path / "prebundle",
        installed_by=seeded["user_id"],
    )
    scenario_id = await _project_scenario(client, auth_headers, "P1")
    await _draft_and_hold(
        client,
        auth_headers,
        scenario_id=scenario_id,
        alias="pretool",
        inputs={"temperature": 300},
    )

    appr = await _login(client, "appr")
    request_id = (await client.get("/api/users/me/approvals", headers=appr)).json()[0]["request_id"]

    nobody = await _login(client, "nobody")
    empty = await client.get("/api/users/me/approvals", headers=nobody)
    assert empty.status_code == 200
    assert empty.json() == []

    summ = await client.get("/api/users/me/approvals/summary", headers=nobody)
    assert summ.json() == {"can_approve": False, "pending": 0}

    # Detail is existence-hidden (404, not 403) from a non-eligible user.
    hidden = await client.get(f"/api/approvals/{request_id}", headers=nobody)
    assert hidden.status_code == 404


@pytest.mark.asyncio
async def test_batch_decisions_are_isolated(
    client: httpx.AsyncClient,
    seeded: dict[str, Any],
    auth_headers: dict[str, str],
    api_factory: SessionFactory,
    api_settings: Any,
    api_audit: AuditWriter,
    tmp_path: Path,
) -> None:
    """One good approve + one deny-without-reason in the same batch:
    the approve lands, the bad item fails in isolation."""
    await _make_approver(api_factory, api_settings, api_audit, username="appr", is_approver=True)
    await _seed_pre_tool(
        api_factory,
        alias="pretool",
        approver_username="appr",
        bundle_dir=tmp_path / "prebundle",
        installed_by=seeded["user_id"],
    )
    s1 = await _project_scenario(client, auth_headers, "P1")
    s2 = await _project_scenario(client, auth_headers, "P2")
    job1 = await _draft_and_hold(client, auth_headers, scenario_id=s1, alias="pretool", inputs={})
    job2 = await _draft_and_hold(client, auth_headers, scenario_id=s2, alias="pretool", inputs={})

    appr = await _login(client, "appr")
    rows = (await client.get("/api/users/me/approvals", headers=appr)).json()
    by_job = {r["job_id"]: r["request_id"] for r in rows}

    dec = await client.post(
        "/api/approvals/decisions",
        headers=appr,
        json={
            "decisions": [
                {"request_id": by_job[job1], "decision": "approve"},
                {"request_id": by_job[job2], "decision": "deny"},  # no reason
            ]
        },
    )
    assert dec.status_code == 200, dec.text
    results = {r["request_id"]: r for r in dec.json()["results"]}
    assert results[by_job[job1]]["ok"] is True
    assert results[by_job[job1]]["job_state"] == "queued"
    assert results[by_job[job2]]["ok"] is False
    assert "reason" in results[by_job[job2]]["error"].lower()

    # job1 queued; job2 untouched (still held), proving isolation.
    assert (await client.get(f"/api/jobs/{job1}", headers=auth_headers)).json()["state"] == "queued"
    assert (await client.get(f"/api/jobs/{job2}", headers=auth_headers)).json()[
        "state"
    ] == "pending_approval"
