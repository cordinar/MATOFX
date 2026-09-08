"""API tests for the Job-Cohort router (ADR-0008 §11.1 / job-cohorts §6).

Covers the surrogate §14 gating (the feature flag makes the routes
*absent* when off), the workspace-level create/get/list/archive happy
path, the preview endpoint, the resolve endpoint (empty membership for a
fresh cohort), and central error mapping (a tolerated version of the
wrong tool surfaces as a clean plain-English 400, not a 500).
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select, update

from matofx.api.deps import get_settings
from matofx.audit.writer import AuditWriter
from matofx.config import FeatureSettings, Settings
from matofx.db import SessionFactory, session_scope
from matofx.features import Feature
from matofx.hierarchy.orm import Job
from matofx.hierarchy.service import make_project_service
from matofx.studies.cohort.orm import JobCohort
from matofx.studies.orm import ParametricStudy
from matofx.tools.orm import ToolVersion


def _enable_feature(app: FastAPI, api_settings: Settings) -> None:
    """Flip ``features.surrogate_family`` on for this app instance."""
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.SURROGATE_FAMILY.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _tool_version_id(api_factory: SessionFactory, tool_alias: str) -> int:
    async with session_scope(api_factory) as session:
        vid = (
            await session.execute(
                select(ToolVersion.id)
                .where(ToolVersion.alias == tool_alias)
                .order_by(ToolVersion.created_at.desc(), ToolVersion.id.desc())
                .limit(1)
            )
        ).scalar_one()
        return int(vid)


async def test_routes_absent_when_feature_off(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Surrogate §14: with the flag off the surface is *absent* (404)."""
    create = await client.post(
        "/api/cohorts",
        headers=auth_headers,
        json={"name": "x", "tool_alias": "t", "tolerated_tool_version_ids": [1]},
    )
    assert create.status_code == 404
    listing = await client.get("/api/cohorts", headers=auth_headers)
    assert listing.status_code == 404
    preview = await client.post(
        "/api/cohorts/preview", headers=auth_headers, json={"tool_alias": "t"}
    )
    assert preview.status_code == 404


async def test_create_get_list_archive(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    _enable_feature(app, api_settings)
    vid = await _tool_version_id(api_factory, seeded["tool_alias"])

    created = await client.post(
        "/api/cohorts",
        headers=auth_headers,
        json={
            "name": "  Past runs  ",
            "tool_alias": seeded["tool_alias"],
            "tolerated_tool_version_ids": [vid],
            "description": "first cohort",
        },
    )
    assert created.status_code == 201, created.text
    cohort = created.json()
    assert cohort["name"] == "Past runs"  # trimmed
    assert cohort["state"] == "active"
    assert cohort["auto_grow"] is True
    assert "project_id" not in cohort  # workspace-level — no home project
    assert cohort["tolerated_tool_version_ids"] == [vid]
    cid = cohort["id"]

    detail = await client.get(f"/api/cohorts/{cid}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == cid

    listing = await client.get("/api/cohorts", headers=auth_headers)
    assert listing.status_code == 200
    assert [c["id"] for c in listing.json()] == [cid]

    # Resolve a fresh cohort: no completed jobs → empty membership, no error.
    resolved = await client.post(f"/api/cohorts/{cid}/resolve", headers=auth_headers)
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["job_ids"] == []
    assert body["acl_hidden"] == 0
    assert body["last_resolved_at"] is not None

    archived = await client.post(f"/api/cohorts/{cid}/archive", headers=auth_headers)
    assert archived.status_code == 200
    assert archived.json()["state"] == "archived"

    after = await client.get("/api/cohorts", headers=auth_headers)
    assert [c["id"] for c in after.json()] == []  # archived hidden by default
    incl = await client.get("/api/cohorts?include_archived=true", headers=auth_headers)
    assert [c["id"] for c in incl.json()] == [cid]


async def test_preview_empty_workspace(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Preview with no completed runs returns an empty, error-free result."""
    _enable_feature(app, api_settings)
    resp = await client.post(
        "/api/cohorts/preview",
        headers=auth_headers,
        json={"tool_alias": seeded["tool_alias"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rows"] == []
    assert body["acl_hidden"] == 0


async def test_create_fixed_without_members_is_400(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """A fixed (auto_grow=False) cohort needs an explicit run set."""
    _enable_feature(app, api_settings)
    resp = await client.post(
        "/api/cohorts",
        headers=auth_headers,
        json={"name": "fixed", "tool_alias": seeded["tool_alias"], "auto_grow": False},
    )
    assert resp.status_code == 400, resp.text


async def test_create_defaults_to_latest_version(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Omitting the version list anchors on the tool's latest version (§3.2)."""
    _enable_feature(app, api_settings)
    latest = await _tool_version_id(api_factory, seeded["tool_alias"])
    resp = await client.post(
        "/api/cohorts",
        headers=auth_headers,
        json={"name": "auto", "tool_alias": seeded["tool_alias"]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["tolerated_tool_version_ids"] == [latest]


async def test_build_meta_and_edit(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Build-meta returns version bands; PATCH edits an existing cohort."""
    _enable_feature(app, api_settings)
    alias = seeded["tool_alias"]

    meta = await client.get(f"/api/cohorts/tools/{alias}/build-meta", headers=auth_headers)
    assert meta.status_code == 200, meta.text
    bands = meta.json()["version_bands"]
    assert bands  # at least one major.minor band
    assert all("major_minor" in b and b["version_ids"] for b in bands)

    created = await client.post(
        "/api/cohorts", headers=auth_headers, json={"name": "e", "tool_alias": alias}
    )
    cid = created.json()["id"]
    patched = await client.patch(
        f"/api/cohorts/{cid}",
        headers=auth_headers,
        json={"name": "edited", "completed_after": "2026-01-01T00:00:00Z"},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["name"] == "edited"
    assert body["completed_after"] is not None


async def test_a_filter_can_be_cleared_once_it_has_been_set(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """PATCH semantics: an explicit ``null`` clears, an absent key does not.

    Three layers used to drop ``None`` before the service saw it -- the edit
    form, the API client and this router -- and the service reads absence as
    "leave unchanged". A filter could therefore be added but never removed:
    the user emptied the boxes, Save reported success, and the cohort went on
    filtering. On the demo archive that pinned a heat-sink cohort at 19 of
    its 531 runs with nothing on screen to explain why.
    """
    _enable_feature(app, api_settings)
    alias = seeded["tool_alias"]
    created = await client.post(
        "/api/cohorts",
        headers=auth_headers,
        json={
            "name": "filtered",
            "tool_alias": alias,
            "input_ranges": {"width": {"min": 1.0, "max": 2.0}},
            "completed_after": "2026-01-01T00:00:00Z",
        },
    )
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    assert created.json()["input_ranges"] == {"width": {"min": 1.0, "max": 2.0}}

    # A key that is simply absent leaves the filter alone.
    untouched = await client.patch(
        f"/api/cohorts/{cid}", headers=auth_headers, json={"name": "renamed"}
    )
    assert untouched.status_code == 200, untouched.text
    assert untouched.json()["input_ranges"] == {"width": {"min": 1.0, "max": 2.0}}
    assert untouched.json()["completed_after"] is not None

    # An explicit null clears it.
    cleared = await client.patch(
        f"/api/cohorts/{cid}",
        headers=auth_headers,
        json={"input_ranges": None, "completed_after": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert not cleared.json()["input_ranges"]
    assert cleared.json()["completed_after"] is None


async def test_a_commissioned_studys_runs_reach_the_cohort(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """ "Create a parametric study and add to Cohort" has to mean it (§11.5).

    The button commissions a study to fill *this* cohort's gaps, so its
    results have to come back to it. Nothing carried them before:

    - a **fixed** cohort is frozen to ``member_job_ids`` and would never take
      them at all;
    - an **auto-grow** cohort usually would, but its own ``input_ranges`` /
      ``project_ids`` / date window can perfectly well reject the very runs it
      asked for — a study finishing with the verdict unmoved and nothing on
      screen to say why.

    Resolved at read time, so a run appears once it is complete, with no hook
    to fire and no race to lose.
    """

    _enable_feature(app, api_settings)
    alias = seeded["tool_alias"]
    vid = await _tool_version_id(api_factory, alias)

    created = await client.post(
        "/api/cohorts",
        headers=auth_headers,
        json={"name": "commissioner", "tool_alias": alias},
    )
    assert created.status_code == 201, created.text
    cid = int(created.json()["id"])
    assert created.json()["auto_include_study_ids"] is None

    # A study of three completed runs, exactly as the button would commission.
    async with api_factory() as session:
        # A Job needs exactly one home, and the study needs a project of its
        # own — the fixture archive ships none.
        project = await make_project_service(AuditWriter(signing_key=None, key_id=None)).create(
            session, actor_user_id=seeded["user_id"], name="Commissioned"
        )
        study = ParametricStudy(
            project_id=project.id,
            name="fill the gaps",
            tool_alias=alias,
            tool_version_id=vid,
            mode="table",
            run_cap=10,
            created_by_user_id=seeded["user_id"],
        )
        session.add(study)
        await session.flush()
        for i in range(3):
            session.add(
                Job(
                    parametric_study_id=study.id,
                    name=f"gap-filler-{i}",
                    tool_alias=alias,
                    tool_version_id=vid,
                    state="complete",
                    inputs={"width": float(i)},
                    created_by_user_id=seeded["user_id"],
                )
            )
        # Freeze the cohort: the mode that could never have adopted them.
        await session.execute(
            update(JobCohort).where(JobCohort.id == cid).values(auto_grow=False, member_job_ids=[])
        )
        await session.commit()
        study_id = int(study.id)

    frozen = await client.get(f"/api/cohorts/{cid}/members", headers=auth_headers)
    assert frozen.status_code == 200, frozen.text
    assert not frozen.json()["job_ids"], "a fixed cohort must start frozen"

    async with api_factory() as session:
        await session.execute(
            update(JobCohort).where(JobCohort.id == cid).values(auto_include_study_ids=[study_id])
        )
        await session.commit()

    adopted = await client.get(f"/api/cohorts/{cid}/members", headers=auth_headers)
    assert len(adopted.json()["job_ids"]) == 3, "the commissioned runs never arrived"

    # ...and a curator who threw a run out meant it.
    async with api_factory() as session:
        await session.execute(
            update(JobCohort)
            .where(JobCohort.id == cid)
            .values(excluded_job_ids=[adopted.json()["job_ids"][0]])
        )
        await session.commit()

    trimmed = await client.get(f"/api/cohorts/{cid}/members", headers=auth_headers)
    assert len(trimmed.json()["job_ids"]) == 2, "an excluded run was put back"


async def test_create_with_version_band(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Creating with a major.minor band stores the band + an expanded snapshot."""
    _enable_feature(app, api_settings)
    alias = seeded["tool_alias"]
    meta = await client.get(f"/api/cohorts/tools/{alias}/build-meta", headers=auth_headers)
    band = meta.json()["version_bands"][0]["major_minor"]
    resp = await client.post(
        "/api/cohorts",
        headers=auth_headers,
        json={"name": "b", "tool_alias": alias, "tolerated_version_bands": [band]},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["tolerated_version_bands"] == [band]
    assert body["tolerated_tool_version_ids"]  # expanded snapshot, non-empty


async def test_list_get_and_results_carry_display_fields(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """List / get carry tool label + creator + run count; results carries columns."""
    _enable_feature(app, api_settings)
    created = await client.post(
        "/api/cohorts",
        headers=auth_headers,
        json={"name": "display", "tool_alias": seeded["tool_alias"]},
    )
    assert created.status_code == 201, created.text
    cid = created.json()["id"]

    listing = await client.get("/api/cohorts", headers=auth_headers)
    row = next(c for c in listing.json() if c["id"] == cid)
    assert row["tool_name"]  # the display label resolved (not blank)
    assert row["created_by_name"]  # the creator resolved
    assert row["member_count"] == 0  # no completed runs yet, ACL-correct
    # Tolerated versions resolve to actual version strings, not bare ids.
    assert row["tolerated_versions"]
    assert len(row["tolerated_versions"]) == len(row["tolerated_tool_version_ids"])

    detail = await client.get(f"/api/cohorts/{cid}", headers=auth_headers)
    assert detail.json()["tool_name"] == row["tool_name"]
    assert detail.json()["member_count"] == 0

    results = await client.get(f"/api/cohorts/{cid}/results", headers=auth_headers)
    assert results.status_code == 200, results.text
    body = results.json()
    assert body["rows"] == []
    assert body["columns"] == []  # no member rows → no response columns yet
    assert body["input_columns"] == []  # …and no input columns yet


async def test_create_bad_tolerated_version_is_400(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """A tolerated version that doesn't exist → clean plain-English 400."""
    _enable_feature(app, api_settings)
    resp = await client.post(
        "/api/cohorts",
        headers=auth_headers,
        json={
            "name": "c",
            "tool_alias": seeded["tool_alias"],
            "tolerated_tool_version_ids": [9_999_999],
        },
    )
    assert resp.status_code == 400, resp.text
    assert "9999999" in resp.json()["detail"] or "doesn't exist" in resp.json()["detail"]


# --- preview-only filters belong to the preview request only ---------------


def test_the_source_filters_reach_every_cohort_request() -> None:
    """They must travel on all four shapes, or a save or a re-open loses them.

    Two regressions live here. They were first added to the *create* request by
    mistake, so every preview failed with ``extra_forbidden``; then they were
    preview-only, so re-opening a cohort showed a blank form and a live cohort
    could not be "the runs of these studies" at all (migration 0097).
    """
    from matofx.api.schemas import (  # noqa: PLC0415
        CohortCreateRequest,
        CohortPreviewRequest,
        CohortResponse,
        CohortUpdateRequest,
    )

    filters = {"source_kinds", "parametric_study_ids", "optimisation_study_ids"}
    assert filters <= set(CohortPreviewRequest.model_fields)
    assert filters <= set(CohortCreateRequest.model_fields)
    assert filters <= set(CohortUpdateRequest.model_fields)
    # And back out again, or the editor has nothing to pre-fill from.
    assert filters <= set(CohortResponse.model_fields)
