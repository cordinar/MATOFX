"""UI api_client coverage for the surrogate Models surface (ADR-0008 §12).

NiceGUI page rendering isn't unit-tested; these exercise the model
client methods the catalogue + detail pages call, end-to-end against the
real app, plus the feature-flag behaviour the nav entry relies on.
Mirrors tests/ui/test_materials_client.py. ``models.models_dir`` is
overridden to a tmp path so bundles don't pollute the repo, and the
publish-approval gate is toggled off so the seeded super-user (a curator)
checks directly through the two gates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select

# Register every ORM the registry / approvals tables FK to.
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
from matofx.ui._features import surrogate_family_enabled
from matofx.ui.api_client import ApiClient, ApiError


def _enable(app: FastAPI, api_settings: Settings, tmp_path: Path) -> None:
    enabled = api_settings.model_copy(
        update={
            "features": FeatureSettings(enabled=[Feature.SURROGATE_FAMILY.value]),
            "models": api_settings.models.model_copy(
                update={"models_dir": tmp_path / "models_store"}
            ),
        }
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _set_gate(app: FastAPI, token: str, *, on: bool) -> None:
    """Toggle ``models_global_publish_approval`` via the admin API."""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://matofx") as c:
        resp = await c.post(
            "/api/admin/general-settings/models_global_publish_approval",
            headers={"Authorization": f"Bearer {token}"},
            json={"value": on},
        )
        assert resp.status_code == 200, resp.text


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
            name="uisrc",
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


@pytest.mark.asyncio
async def test_models_client_end_to_end(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    seeded: dict[str, Any],
    tmp_path: Path,
) -> None:
    _enable(app, api_settings, tmp_path)
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    token = str(auth["access_token"])
    client = ApiClient(app, token=token)
    await _set_gate(app, token, on=False)  # builder-curator checks directly

    tool = seeded["tool_alias"]
    proj = await client.create_project(name="UIModelSrc", description="")
    project_id = int(proj["id"])
    source_id = await _seed_source(api_factory, tool, seeded["user_id"], project_id)

    # Bake-off: compare two methods (no persistence) before committing.
    cmp = await client.compare_model_fits(
        tool_alias=tool,
        source_kind="parametric",
        source_id=source_id,
        response_names=["y"],
        methods=["gp", "poly"],
    )
    assert {m["method"] for m in cmp["methods"]} == {"gp", "poly"}
    assert cmp["best_overall"] in {"gp", "poly"}

    # Personal model — publishes directly.
    personal = await client.create_model(
        name="UI Personal GP",
        tool_alias=tool,
        scope="personal",
        source_kind="parametric",
        source_id=source_id,
        response_names=["y"],
    )
    pid = int(personal["id"])
    assert personal["scope"] == "personal"

    detail = await client.get_model(pid)
    assert detail["model"]["id"] == pid
    assert detail["versions"][0]["publish_state"] == "published"

    patched = await client.update_model(pid, name="UI Personal GP v2", tags=["demo"])
    assert patched["name"] == "UI Personal GP v2"

    added = await client.refit_model(pid)
    assert added["ordinal"] == 2

    listed = await client.list_models()
    assert pid in [int(m["id"]) for m in listed]

    # Global model — through the two gates (approval off ⇒ direct check).
    glob = await client.create_model(
        name="UI Global GP",
        tool_alias=tool,
        scope="global",
        source_kind="parametric",
        source_id=source_id,
        response_names=["y"],
    )
    gid = int(glob["id"])
    assert glob["state"] == "pending"
    g_detail = await client.get_model(gid)
    g_vid = int(g_detail["versions"][0]["id"])

    checked = await client.submit_model_review(gid, g_vid)
    assert checked["publish_state"] == "checked"
    released = await client.release_model_version(gid, g_vid)
    assert released["publish_state"] == "published"

    # The released global is now pickable.
    picked = await client.list_models(tool=tool, scope="global")
    assert gid in [int(m["id"]) for m in picked]

    exported = await client.export_model_as_tool(pid)
    assert exported["tool_state"] == "test"

    scoped = await client.set_model_scope(pid, scope="project", project_id=project_id)
    assert scoped["scope"] == "project"

    retired = await client.retire_model(gid)
    assert retired["state"] == "retired"

    # Approvers picker excludes the caller.
    assert seeded["user_id"] not in [a["user_id"] for a in await client.model_approvers()]


@pytest.mark.asyncio
async def test_models_routes_absent_when_feature_off(app: FastAPI, seeded: dict[str, Any]) -> None:
    """Default settings (flag off): the client gets 404 — the surface is
    absent, which is what hides the nav entry."""
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    client = ApiClient(app, token=str(auth["access_token"]))
    with pytest.raises(ApiError) as ei:
        await client.list_models()
    assert ei.value.status_code == 404


def test_models_helper_off_when_holder_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safe when the UI isn't mounted — returns False (suppressed)."""
    monkeypatch.setattr("matofx.ui._holder._app", None)
    assert surrogate_family_enabled() is False
