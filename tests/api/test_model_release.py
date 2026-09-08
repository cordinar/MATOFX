"""Two-gate global-model release flow through the API (ADR-0008 §6.3).

A global surrogate model is released to general use through two gates:

1. **Technical sign-off** (toggleable, ``models_global_publish_approval``).
   ON → the builder nominates a *Model Curator* checker (≠ builder); the
   draft version goes ``in_review`` and a directed approval request is
   opened, decided through the unified ``/api/approvals/decisions``
   endpoint. OFF → the builder (who must hold ``models.curate``) checks
   directly and the version skips to ``checked``.
2. **Release** (mandatory, non-toggleable). A super-user / admin flips
   the ``checked`` version to ``published``, advancing the model's
   published pointer and activating it.

These tests drive the full HTTP path: the model router's
``submit-review`` + ``release`` endpoints and the approvals router's
decide dispatch (``surrogate_model_version_id`` →
``ModelService.decide_check``). ``models.models_dir`` is overridden to a
tmp path so bundles don't pollute the repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy import select

# Register every ORM the registry / approvals tables FK to (CI registers
# these via their own suites; here we self-register).
import matofx.library.orm
import matofx.materials.orm
import matofx.vv.orm  # noqa: F401  -- registers vv tables
from matofx.api.deps import get_settings
from matofx.audit import AuditWriter
from matofx.config import FeatureSettings, Settings
from matofx.db import SessionFactory, session_scope
from matofx.features import Feature
from matofx.hierarchy.orm import Job
from matofx.studies.orm import ParametricStudy, StudyResult
from matofx.tools.orm import ToolVersion
from matofx.users import make_password_hasher, make_user_service
from matofx.users.role_service import make_role_service

_CHECKER_PASSWORD = "checkerpass1"


def _enable(app: FastAPI, api_settings: Settings, tmp_path: Path) -> None:
    """Flip ``surrogate_family`` on + point model bundles at a tmp dir."""
    enabled = api_settings.model_copy(
        update={
            "features": FeatureSettings(enabled=[Feature.SURROGATE_FAMILY.value]),
            "models": api_settings.models.model_copy(
                update={"models_dir": tmp_path / "models_store"}
            ),
        }
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _make_project(client: httpx.AsyncClient, headers: dict[str, str]) -> int:
    resp = await client.post(
        "/api/projects", headers=headers, json={"name": "RelSrc", "description": ""}
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def _seed_source(
    api_factory: SessionFactory, tool_alias: str, user_id: int, project_id: int
) -> int:
    """A parametric study + 6 completed children (x → y=2x). Returns its id."""
    async with session_scope(api_factory) as session:
        vid = (
            await session.execute(
                select(ToolVersion.id)
                .where(ToolVersion.alias == tool_alias)
                .order_by(ToolVersion.created_at.desc(), ToolVersion.id.desc())
                .limit(1)
            )
        ).scalar_one()
        study = ParametricStudy(
            project_id=project_id,
            name="relsrc",
            tool_alias=tool_alias,
            tool_version_id=int(vid),
            mode="grid",
            factor_spec={},
            sampling_params={},
            run_cap=100,
            created_by_user_id=user_id,
        )
        session.add(study)
        await session.flush()
        for x in range(1, 7):
            job = Job(
                parametric_study_id=study.id,
                scenario_id=None,
                name="run",
                tool_alias=tool_alias,
                tool_version_id=int(vid),
                state="complete",
                inputs={"x": float(x)},
                created_by_user_id=user_id,
            )
            session.add(job)
            await session.flush()
            session.add(
                StudyResult(
                    source_kind="parametric",
                    source_id=study.id,
                    job_id=job.id,
                    scalars={"y": 2.0 * x},
                    retained_files=[],
                )
            )
        await session.flush()
        return int(study.id)


async def _seed_curator(
    api_factory: SessionFactory,
    api_settings: Settings,
    api_audit: AuditWriter,
    *,
    actor_id: int,
) -> int:
    """Create 'checker' (regular user) + grant the model_curator capability."""
    hasher = make_password_hasher(api_settings.auth)
    user_service = make_user_service(api_settings.auth, api_audit, hasher=hasher)
    role_service = make_role_service(api_audit)
    async with session_scope(api_factory) as session:
        checker = await user_service.create_user(
            session,
            username="checker",
            email="checker@example.com",
            password=_CHECKER_PASSWORD,
            full_name="Mira Checker",
            role="regular_user",
        )
        await role_service.set_user_capability_roles(
            session,
            actor_user_id=actor_id,
            target_user_id=checker.id,
            role_names=["model_curator"],
        )
        return checker.id


async def _login(client: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    resp = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _create_global_model(
    client: httpx.AsyncClient, headers: dict[str, str], tool: str, source_id: int
) -> tuple[int, int]:
    """Create a global model; return ``(model_id, draft_version_id)``."""
    created = await client.post(
        "/api/models",
        headers=headers,
        json={
            "name": "Global Heat GP",
            "tool_alias": tool,
            "scope": "global",
            "source_kind": "parametric",
            "source_id": source_id,
            "response_names": ["y"],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["scope"] == "global"
    assert created.json()["state"] == "pending"
    mid = int(created.json()["id"])
    detail = await client.get(f"/api/models/{mid}", headers=headers)
    assert detail.status_code == 200, detail.text
    versions = detail.json()["versions"]
    assert len(versions) == 1
    assert versions[0]["publish_state"] == "draft"
    return mid, int(versions[0]["id"])


async def _surrogate_request_id(api_factory: SessionFactory, version_id: int) -> int:
    """The open approval_request directed at ``version_id``."""
    async with session_scope(api_factory) as session:
        return int(
            (
                await session.execute(
                    sa.text(
                        "SELECT id FROM approval_request WHERE surrogate_model_version_id = :vid"
                    ),
                    {"vid": version_id},
                )
            ).scalar_one()
        )


async def _set_approval_toggle(
    client: httpx.AsyncClient, headers: dict[str, str], *, value: bool
) -> None:
    resp = await client.post(
        "/api/admin/general-settings/models_global_publish_approval",
        headers=headers,
        json={"value": value},
    )
    assert resp.status_code == 200, resp.text


async def test_two_gate_release_with_directed_checker(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    api_audit: AuditWriter,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Approval ON: submit → checker approves via unified inbox → su releases.

    Exercises the whole cross-domain path: the builder (super-user)
    nominates a Model Curator checker; the draft goes ``in_review`` and a
    directed gate opens; the checker (neither admin nor tool-approver,
    just ``model_curator``) decides it through ``/api/approvals/decisions``
    → version ``checked``; the super-user releases → ``published``, model
    ``active``, published pointer advanced.
    """
    _enable(app, api_settings, tmp_path)
    tool = seeded["tool_alias"]
    project_id = await _make_project(client, auth_headers)
    source_id = await _seed_source(api_factory, tool, seeded["user_id"], project_id)
    checker_id = await _seed_curator(
        api_factory, api_settings, api_audit, actor_id=int(seeded["user_id"])
    )
    checker_headers = await _login(client, "checker", _CHECKER_PASSWORD)
    mid, vid = await _create_global_model(client, auth_headers, tool, source_id)

    # Gate 1 — builder submits, directing the checker.
    submitted = await client.post(
        f"/api/models/{mid}/versions/{vid}/submit-review",
        headers=auth_headers,
        json={"checker_user_id": checker_id},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["publish_state"] == "in_review"

    # The checker decides the gate through the unified decisions endpoint.
    request_id = await _surrogate_request_id(api_factory, vid)
    decided = await client.post(
        "/api/approvals/decisions",
        headers=checker_headers,
        json={"decisions": [{"request_id": request_id, "decision": "approve"}]},
    )
    assert decided.status_code == 200, decided.text
    result = decided.json()["results"][0]
    assert result["ok"] is True, result
    assert result["status"] == "approved"
    assert result["job_state"] == "checked"  # the version's publish_state

    # Gate 2 — the super-user releases the checked version.
    released = await client.post(f"/api/models/{mid}/versions/{vid}/release", headers=auth_headers)
    assert released.status_code == 200, released.text
    assert released.json()["publish_state"] == "published"

    detail = await client.get(f"/api/models/{mid}", headers=auth_headers)
    body = detail.json()
    assert body["model"]["state"] == "active"
    assert body["model"]["published_version_id"] == vid
    # The model now surfaces in the picker for general use.
    picked = await client.get(f"/api/models/picker?tool={tool}", headers=auth_headers)
    assert mid in {m["id"] for m in picked.json()}


async def test_checker_reject_marks_version_rejected(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    api_audit: AuditWriter,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A checker denial flips the version to ``rejected`` (gate 1 fails)."""
    _enable(app, api_settings, tmp_path)
    tool = seeded["tool_alias"]
    project_id = await _make_project(client, auth_headers)
    source_id = await _seed_source(api_factory, tool, seeded["user_id"], project_id)
    checker_id = await _seed_curator(
        api_factory, api_settings, api_audit, actor_id=int(seeded["user_id"])
    )
    checker_headers = await _login(client, "checker", _CHECKER_PASSWORD)
    mid, vid = await _create_global_model(client, auth_headers, tool, source_id)

    await client.post(
        f"/api/models/{mid}/versions/{vid}/submit-review",
        headers=auth_headers,
        json={"checker_user_id": checker_id},
    )
    request_id = await _surrogate_request_id(api_factory, vid)
    decided = await client.post(
        "/api/approvals/decisions",
        headers=checker_headers,
        json={
            "decisions": [
                {"request_id": request_id, "decision": "deny", "reason": "fit too sparse"}
            ]
        },
    )
    assert decided.status_code == 200, decided.text
    result = decided.json()["results"][0]
    assert result["ok"] is True
    assert result["status"] == "denied"
    assert result["job_state"] == "rejected"

    # A rejected version cannot be released.
    released = await client.post(f"/api/models/{mid}/versions/{vid}/release", headers=auth_headers)
    assert released.status_code == 400, released.text


async def test_direct_check_when_approval_off(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Approval OFF: the builder-curator checks directly, then su releases.

    With ``models_global_publish_approval`` off, ``submit-review`` skips
    the directed gate and lands the version at ``checked`` immediately
    (the seeded super-user holds ``models.curate``). The mandatory
    release gate still applies.
    """
    _enable(app, api_settings, tmp_path)
    tool = seeded["tool_alias"]
    project_id = await _make_project(client, auth_headers)
    source_id = await _seed_source(api_factory, tool, seeded["user_id"], project_id)
    await _set_approval_toggle(client, auth_headers, value=False)
    mid, vid = await _create_global_model(client, auth_headers, tool, source_id)

    submitted = await client.post(
        f"/api/models/{mid}/versions/{vid}/submit-review", headers=auth_headers, json={}
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["publish_state"] == "checked"  # skipped the gate

    released = await client.post(f"/api/models/{mid}/versions/{vid}/release", headers=auth_headers)
    assert released.status_code == 200, released.text
    assert released.json()["publish_state"] == "published"


async def test_submit_review_requires_checker_when_approval_on(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Approval ON but no checker nominated → 400 (config error)."""
    _enable(app, api_settings, tmp_path)
    tool = seeded["tool_alias"]
    project_id = await _make_project(client, auth_headers)
    source_id = await _seed_source(api_factory, tool, seeded["user_id"], project_id)
    mid, vid = await _create_global_model(client, auth_headers, tool, source_id)

    resp = await client.post(
        f"/api/models/{mid}/versions/{vid}/submit-review", headers=auth_headers, json={}
    )
    assert resp.status_code == 400, resp.text


async def test_release_requires_checked_version(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Releasing a still-``draft`` version is rejected (gate-2 precondition)."""
    _enable(app, api_settings, tmp_path)
    tool = seeded["tool_alias"]
    project_id = await _make_project(client, auth_headers)
    source_id = await _seed_source(api_factory, tool, seeded["user_id"], project_id)
    mid, vid = await _create_global_model(client, auth_headers, tool, source_id)

    resp = await client.post(f"/api/models/{mid}/versions/{vid}/release", headers=auth_headers)
    assert resp.status_code == 400, resp.text


async def test_version_must_belong_to_model(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A version id from another model 404s on submit-review (path integrity)."""
    _enable(app, api_settings, tmp_path)
    tool = seeded["tool_alias"]
    project_id = await _make_project(client, auth_headers)
    source_id = await _seed_source(api_factory, tool, seeded["user_id"], project_id)
    mid, vid = await _create_global_model(client, auth_headers, tool, source_id)

    resp = await client.post(
        f"/api/models/{mid + 999}/versions/{vid}/submit-review",
        headers=auth_headers,
        json={"checker_user_id": seeded["user_id"]},
    )
    assert resp.status_code == 404, resp.text
