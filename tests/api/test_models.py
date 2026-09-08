"""API tests for the surrogate Model registry router (ADR-0008 §11.2).

Covers the surrogate §14 gating (routes absent when the flag is off), the
create → detail → picker → retire lifecycle, and export-as-tool. The
``models.models_dir`` is overridden to a tmp path so model bundles +
exported-tool orchestrators don't pollute the repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select

# Register every ORM the registry / approvals tables FK to (the full CI
# registers these via their own suites; here we self-register).
import matofx.library.orm
import matofx.materials.orm
import matofx.vv.orm  # noqa: F401  -- registers vv tables
from matofx.api.deps import get_settings
from matofx.config import FeatureSettings, Settings
from matofx.db import SessionFactory, session_scope
from matofx.features import Feature
from matofx.hierarchy.orm import Job
from matofx.studies.orm import ParametricStudy, StudyResult
from matofx.tools.orm import ToolVersion


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
        "/api/projects", headers=headers, json={"name": "ModelSrc", "description": ""}
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
            name="src",
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


async def _seed_cohort(api_factory: SessionFactory, tool_alias: str, user_id: int) -> int:
    """An auto-grow cohort over ``tool_alias`` — no ``study_result`` rows yet.

    Deliberately leaves the cohort's ``(source_kind='cohort')`` results
    un-populated, mirroring a cohort whose Results page was never opened:
    the surrogate build path must populate them itself before fitting.
    """
    from matofx.studies.cohort.orm import JobCohort  # noqa: PLC0415

    async with session_scope(api_factory) as session:
        vid = (
            await session.execute(
                select(ToolVersion.id)
                .where(ToolVersion.alias == tool_alias)
                .order_by(ToolVersion.created_at.desc(), ToolVersion.id.desc())
                .limit(1)
            )
        ).scalar_one()
        cohort = JobCohort(
            name="src cohort",
            tool_alias=tool_alias,
            tolerated_tool_version_ids=[int(vid)],
            auto_grow=True,
            state="active",
            created_by_user_id=user_id,
        )
        session.add(cohort)
        await session.flush()
        return int(cohort.id)


async def test_build_from_cohort_populates_results_then_fits(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Building from a cohort whose results were never opened still fits (§3.3).

    Cohort ``study_result`` rows are materialised lazily — only a cohort
    *results* read writes them. The surrogate fit reads them directly, so the
    build path must trigger population itself; otherwise it would silently
    train on zero rows. Regression for that gap: a cohort with no populated
    results both compares and creates a fitted model.
    """
    _enable(app, api_settings, tmp_path)
    tool = seeded["tool_alias"]
    project_id = await _make_project(client, auth_headers)
    # The parametric jobs are the cohort's live members; the cohort itself
    # has no ``source_kind='cohort'`` results rows until the build populates them.
    await _seed_source(api_factory, tool, seeded["user_id"], project_id)
    cohort_id = await _seed_cohort(api_factory, tool, seeded["user_id"])

    compared = await client.post(
        "/api/models/compare-fits",
        headers=auth_headers,
        json={
            "tool_alias": tool,
            "source_kind": "cohort",
            "source_id": cohort_id,
            "response_names": ["y"],
            "methods": ["gp", "poly"],
        },
    )
    assert compared.status_code == 200, compared.text
    assert all(m["fitted"] for m in compared.json()["methods"]), compared.text

    created = await client.post(
        "/api/models",
        headers=auth_headers,
        json={
            "name": "Cohort GP",
            "tool_alias": tool,
            "scope": "personal",
            "source_kind": "cohort",
            "source_id": cohort_id,
            "response_names": ["y"],
        },
    )
    assert created.status_code == 201, created.text
    mid = created.json()["id"]

    detail = await client.get(f"/api/models/{mid}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    version = detail.json()["versions"][0]
    # All six cohort members trained the model — proof the rows were populated.
    assert version["n_training_points"] == 6
    # The model + catalogue are enriched with display fields (label, not alias).
    assert detail.json()["model"]["tool_name"]
    assert detail.json()["model"]["created_by_name"]
    listing = await client.get("/api/models", headers=auth_headers)
    assert listing.status_code == 200
    row = next(m for m in listing.json() if m["id"] == mid)
    assert row["tool_name"]  # catalogue row reads by label
    assert row["created_by_name"]


async def test_routes_absent_when_feature_off(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.get("/api/models/picker?tool=x", headers=auth_headers)
    assert resp.status_code == 404


async def test_create_detail_picker_retire_export(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
    tmp_path: Path,
) -> None:
    _enable(app, api_settings, tmp_path)
    tool = seeded["tool_alias"]
    project_id = await _make_project(client, auth_headers)
    source_id = await _seed_source(api_factory, tool, seeded["user_id"], project_id)

    created = await client.post(
        "/api/models",
        headers=auth_headers,
        json={
            "name": "Heat GP",
            "tool_alias": tool,
            "scope": "personal",
            "source_kind": "parametric",
            "source_id": source_id,
            "response_names": ["y"],
        },
    )
    assert created.status_code == 201, created.text
    model = created.json()
    assert model["scope"] == "personal"
    assert model["published_version_id"] is not None
    mid = model["id"]

    detail = await client.get(f"/api/models/{mid}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["model"]["id"] == mid
    assert len(body["versions"]) == 1
    assert body["versions"][0]["ordinal"] == 1

    picked = await client.get(f"/api/models/picker?tool={tool}", headers=auth_headers)
    assert mid in {m["id"] for m in picked.json()}

    exported = await client.post(f"/api/models/{mid}/export-as-tool", headers=auth_headers, json={})
    assert exported.status_code == 200, exported.text
    assert exported.json()["tool_state"] == "test"
    assert exported.json()["tool_alias"] == f"surrogate_{tool}_{mid}"

    retired = await client.post(f"/api/models/{mid}/retire", headers=auth_headers)
    assert retired.status_code == 200
    assert retired.json()["state"] == "retired"
    after = await client.get(f"/api/models/picker?tool={tool}", headers=auth_headers)
    assert mid not in {m["id"] for m in after.json()}


async def test_catalogue_lists_in_progress_models(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
    tmp_path: Path,
) -> None:
    """``GET /api/models`` surfaces the caller's own models, including drafts.

    The picker hides un-published globals; the catalogue must show them
    (badged) so the builder can drive them through review. Also exercises
    the ``scope`` filter and the ``approvers`` picker excluding self.
    """
    _enable(app, api_settings, tmp_path)
    tool = seeded["tool_alias"]
    project_id = await _make_project(client, auth_headers)
    source_id = await _seed_source(api_factory, tool, seeded["user_id"], project_id)

    personal = await client.post(
        "/api/models",
        headers=auth_headers,
        json={
            "name": "Personal GP",
            "tool_alias": tool,
            "scope": "personal",
            "source_kind": "parametric",
            "source_id": source_id,
            "response_names": ["y"],
        },
    )
    assert personal.status_code == 201, personal.text
    glob = await client.post(
        "/api/models",
        headers=auth_headers,
        json={
            "name": "Global GP",
            "tool_alias": tool,
            "scope": "global",
            "source_kind": "parametric",
            "source_id": source_id,
            "response_names": ["y"],
        },
    )
    assert glob.status_code == 201, glob.text
    global_id = glob.json()["id"]

    catalogue = await client.get("/api/models", headers=auth_headers)
    assert catalogue.status_code == 200, catalogue.text
    ids = {m["id"] for m in catalogue.json()}
    # The un-published global is in the catalogue but NOT the picker.
    assert global_id in ids
    picked = await client.get(f"/api/models/picker?tool={tool}", headers=auth_headers)
    assert global_id not in {m["id"] for m in picked.json()}

    only_global = await client.get("/api/models?scope=global", headers=auth_headers)
    assert {m["scope"] for m in only_global.json()} == {"global"}

    # The seeded super-user can't nominate themselves as checker.
    approvers = await client.get("/api/models/approvers", headers=auth_headers)
    assert approvers.status_code == 200, approvers.text
    assert seeded["user_id"] not in {a["user_id"] for a in approvers.json()}


async def test_compare_fits_returns_per_method_quality_and_best(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The bake-off fits each method (persisting none) and names the best (§4)."""
    _enable(app, api_settings, tmp_path)
    tool = seeded["tool_alias"]
    project_id = await _make_project(client, auth_headers)
    source_id = await _seed_source(api_factory, tool, seeded["user_id"], project_id)

    resp = await client.post(
        "/api/models/compare-fits",
        headers=auth_headers,
        json={
            "tool_alias": tool,
            "source_kind": "parametric",
            "source_id": source_id,
            "response_names": ["y"],
            "methods": ["gp", "poly"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {m["method"] for m in body["methods"]} == {"gp", "poly"}
    for m in body["methods"]:
        assert m["fitted"] is True
        # Held-out CV pairs are returned so the UI can overlay parity plots.
        assert m["quality"]["y"]["cv_actual"]
        assert m["quality"]["y"]["cv_predicted"]
    assert body["best_overall"] in {"gp", "poly"}
    assert body["best_per_response"]["y"] in {"gp", "poly"}

    # No model was persisted by the comparison.
    catalogue = await client.get("/api/models", headers=auth_headers)
    assert catalogue.json() == []


async def test_version_coverage_returns_training_ranges(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
    tmp_path: Path,
) -> None:
    """The coverage endpoint returns the version's frozen factor ranges (§7.3)."""
    _enable(app, api_settings, tmp_path)
    tool = seeded["tool_alias"]
    project_id = await _make_project(client, auth_headers)
    source_id = await _seed_source(api_factory, tool, seeded["user_id"], project_id)
    created = await client.post(
        "/api/models",
        headers=auth_headers,
        json={
            "name": "Cov GP",
            "tool_alias": tool,
            "scope": "personal",
            "source_kind": "parametric",
            "source_id": source_id,
            "response_names": ["y"],
        },
    )
    assert created.status_code == 201, created.text
    version_id = created.json()["published_version_id"]

    cov = await client.get(f"/api/models/versions/{version_id}/coverage", headers=auth_headers)
    assert cov.status_code == 200, cov.text
    body = cov.json()
    assert body["version_id"] == version_id
    # The source varied x over 1..6 → the single factor's range spans that.
    factors = {f["name"]: f for f in body["factors"]}
    assert "x" in factors
    assert factors["x"]["min"] <= 1.0
    assert factors["x"]["max"] >= 6.0


async def test_create_global_as_curator_lands_pending(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A non-curator can't create a global model — but the seeded super-user can.

    The seeded user is a super-user (holds ``models.curate`` via the
    sentinel), so a global create *succeeds* here — asserting the curator
    path is wired. (A negative test needs a plain user; covered at the
    service layer.)
    """
    _enable(app, api_settings, tmp_path)
    tool = seeded["tool_alias"]
    project_id = await _make_project(client, auth_headers)
    source_id = await _seed_source(api_factory, tool, seeded["user_id"], project_id)
    resp = await client.post(
        "/api/models",
        headers=auth_headers,
        json={
            "name": "Global GP",
            "tool_alias": tool,
            "scope": "global",
            "source_kind": "parametric",
            "source_id": source_id,
            "response_names": ["y"],
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["scope"] == "global"
    assert resp.json()["state"] == "pending"  # global lands pending (draft version)
