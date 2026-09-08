"""UI api_client coverage for the optimisation surface (ADR-0009 §15).

The codebase tests the in-process ApiClient against the real app
(NiceGUI page rendering isn't unit-tested); these exercise the nine
optimisation client methods the workspace + wizard + live view drive,
plus the feature-flag behaviour the nav entry relies on. Mirrors
tests/ui/test_studies_client.py and tests/api/test_optimisation.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from matofx.api.deps import get_settings
from matofx.config import FeatureSettings, Settings
from matofx.db import SessionFactory, session_scope
from matofx.features import Feature
from matofx.tools.orm import ToolVersion
from matofx.ui._features import optimisation_framework_enabled
from matofx.ui.api_client import ApiClient, ApiError
from matofx.users.orm import User
from matofx.workflows.orm import Workflow


def _enable_feature(app: FastAPI, api_settings: Settings) -> None:
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.OPTIMISATION_FRAMEWORK.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _authed(app: FastAPI, seeded: dict[str, Any]) -> ApiClient:
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    return ApiClient(app, token=auth["access_token"])


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


@pytest.mark.asyncio
async def test_optimisation_client_draft_flow(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    seeded: dict[str, Any],
) -> None:
    """Create → patch config (the wizard steps) → get → list → archive."""
    _enable_feature(app, api_settings)
    client = await _authed(app, seeded)
    vid = await _tool_version_id(api_factory, seeded["tool_alias"])

    project = await client.create_project(name="Opt UI", description="")
    study = await client.create_optimisation_study(
        int(project["id"]),
        name="Lightweighting",
        tool_alias=seeded["tool_alias"],
        tool_version_id=vid,
    )
    assert study["state"] == "draft"
    assert study["tool_alias"] == seeded["tool_alias"]
    sid = int(study["id"])

    # The wizard patches one top-level config key per step.
    patched = await client.update_optimisation_study(
        sid, {"strategy": "bo", "acquisition": "balanced", "batch_size": 2}
    )
    assert patched["config_json"]["strategy"] == "bo"
    assert patched["config_json"]["acquisition"] == "balanced"

    got = await client.get_optimisation_study(sid)
    assert got["id"] == sid

    listed = await client.list_optimisation_studies(int(project["id"]))
    assert sid in {int(s["id"]) for s in listed}

    archived = await client.archive_optimisation_study(sid)
    assert archived["archived_at"] is not None
    after = await client.list_optimisation_studies(int(project["id"]))
    assert sid not in {int(s["id"]) for s in after}


@pytest.mark.asyncio
async def test_optimisation_client_halt_and_convergence_on_draft(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    seeded: dict[str, Any],
) -> None:
    """A draft isn't running: halt is a no-op and the trace is empty."""
    _enable_feature(app, api_settings)
    client = await _authed(app, seeded)
    vid = await _tool_version_id(api_factory, seeded["tool_alias"])
    project = await client.create_project(name="Opt halt", description="")
    study = await client.create_optimisation_study(
        int(project["id"]),
        name="h",
        tool_alias=seeded["tool_alias"],
        tool_version_id=vid,
    )
    sid = int(study["id"])

    halted = await client.halt_optimisation_study(sid)
    assert halted["halt_requested"] is False

    conv = await client.get_optimisation_convergence(sid)
    assert conv["state"] == "draft"
    assert conv["trace"] == []
    assert conv["current_best_json"] is None

    # Continue only applies to a *completed* study — a draft is refused (the
    # endpoint + client wiring + the state guard).
    with pytest.raises(ApiError):
        await client.continue_optimisation_study(sid, additional_runs=10)


@pytest.mark.asyncio
async def test_workflow_target_create_via_client(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    seeded: dict[str, Any],
) -> None:
    """The create form's workflow branch: list workflows + mint a workflow draft (§3.4)."""
    _enable_feature(app, api_settings)
    client = await _authed(app, seeded)
    async with session_scope(api_factory) as session:
        uid = (await session.execute(select(User.id).limit(1))).scalar_one()
        session.add(
            Workflow(
                alias="wf_ui",
                name="WF UI",
                version="1.0.0",
                descriptor={"description": ""},
                installed_by_user_id=int(uid),
            )
        )

    workflows = await client.list_workflows()
    assert "wf_ui" in {w["alias"] for w in workflows}

    project = await client.create_project(name="Opt wf UI", description="")
    study = await client.create_optimisation_workflow_study(
        int(project["id"]), name="Chain", workflow_alias="wf_ui"
    )
    assert study["target_kind"] == "workflow"
    assert study["workflow_alias"] == "wf_ui"
    assert study["tool_alias"] is None


@pytest.mark.asyncio
async def test_recommendation_via_client(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    seeded: dict[str, Any],
) -> None:
    """The wizard's strategy step fetches the Navigator's pick (§5.6)."""
    _enable_feature(app, api_settings)
    client = await _authed(app, seeded)
    vid = await _tool_version_id(api_factory, seeded["tool_alias"])
    project = await client.create_project(name="Opt nav", description="")
    study = await client.create_optimisation_study(
        int(project["id"]),
        name="n",
        tool_alias=seeded["tool_alias"],
        tool_version_id=vid,
    )
    rec = await client.get_optimisation_recommendation(int(study["id"]))
    assert rec["strategy"] == "bo"  # fresh draft → single-objective default
    assert rec["rationale"]


@pytest.mark.asyncio
async def test_configure_incomplete_draft_raises_400(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    seeded: dict[str, Any],
) -> None:
    """The wizard's Launch surfaces a clean plain-English 400, not a 500."""
    _enable_feature(app, api_settings)
    client = await _authed(app, seeded)
    vid = await _tool_version_id(api_factory, seeded["tool_alias"])
    project = await client.create_project(name="Opt cfg", description="")
    study = await client.create_optimisation_study(
        int(project["id"]),
        name="e",
        tool_alias=seeded["tool_alias"],
        tool_version_id=vid,
    )
    with pytest.raises(ApiError) as ei:
        await client.configure_optimisation_study(int(study["id"]))
    assert ei.value.status_code == 400
    assert ei.value.detail


@pytest.mark.asyncio
async def test_optimisation_routes_absent_when_feature_off(
    app: FastAPI, seeded: dict[str, Any]
) -> None:
    """Default settings (flag off): the client gets 404 — surface absent."""
    client = await _authed(app, seeded)
    project = await client.create_project(name="No opt feature", description="")
    with pytest.raises(ApiError) as ei:
        await client.create_optimisation_study(
            int(project["id"]),
            name="x",
            tool_alias=seeded["tool_alias"],
            tool_version_id=1,
        )
    assert ei.value.status_code == 404


def test_feature_helper_off_when_holder_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safe when the UI isn't mounted — returns False so the nav entry hides."""
    monkeypatch.setattr("matofx.ui._holder._app", None)
    assert optimisation_framework_enabled() is False


def test_feature_helper_reads_mounted_app_settings(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """Reflects the mounted app's install Settings (design §12 / ADR-0009 §15)."""
    off = SimpleNamespace(state=SimpleNamespace(settings=api_settings))
    monkeypatch.setattr("matofx.ui._holder._app", off)
    assert optimisation_framework_enabled() is False

    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.OPTIMISATION_FRAMEWORK.value])}
    )
    on = SimpleNamespace(state=SimpleNamespace(settings=enabled))
    monkeypatch.setattr("matofx.ui._holder._app", on)
    assert optimisation_framework_enabled() is True
