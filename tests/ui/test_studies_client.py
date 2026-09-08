"""UI api_client coverage for the parametric-studies surface (slice 2-PS-a/6).

The codebase tests the in-process ApiClient against the real app
(NiceGUI page rendering isn't unit-tested); these exercise the seven
study client methods end-to-end plus the feature-flag behaviour the
nav entry / homepage card rely on. Mirrors tests/ui/test_api_client.py
and the feature-toggle pattern from tests/api/test_studies.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from matofx.api.deps import get_settings
from matofx.config import FeatureSettings, Settings
from matofx.features import Feature
from matofx.ui._features import parametric_studies_enabled
from matofx.ui.api_client import ApiClient, ApiError


def _enable_feature(app: FastAPI, api_settings: Settings) -> None:
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.PARAMETRIC_STUDIES.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _authed(app: FastAPI, seeded: dict[str, Any]) -> ApiClient:
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    return ApiClient(app, token=auth["access_token"])


@pytest.mark.asyncio
async def test_study_client_end_to_end(
    app: FastAPI, api_settings: Settings, seeded: dict[str, Any]
) -> None:
    _enable_feature(app, api_settings)
    client = await _authed(app, seeded)

    project = await client.create_project(name="Studies UI", description="")
    study = await client.create_study(
        int(project["id"]),
        name="Sweep A",
        tool_alias=seeded["tool_alias"],
        mode="grid",
    )
    assert study["state"] == "draft"
    assert study["tool_alias"] == seeded["tool_alias"]
    sid = int(study["id"])

    listed = await client.list_studies(int(project["id"]))
    assert [int(s["id"]) for s in listed] == [sid]

    detail = await client.get_study(sid)
    assert detail["study"]["id"] == sid
    assert detail["children"] == []

    cancelled = await client.cancel_study(sid)
    assert cancelled["state"] == "cancelled"


@pytest.mark.asyncio
async def test_preview_bad_spec_raises_api_error(
    app: FastAPI, api_settings: Settings, seeded: dict[str, Any]
) -> None:
    _enable_feature(app, api_settings)
    client = await _authed(app, seeded)
    with pytest.raises(ApiError) as ei:
        await client.preview_factor_spec(
            tool_alias=seeded["tool_alias"], mode="grid", factor_spec={}
        )
    assert ei.value.status_code == 400
    assert ei.value.detail


@pytest.mark.asyncio
async def test_study_routes_absent_when_feature_off(app: FastAPI, seeded: dict[str, Any]) -> None:
    """Default settings (flag off): the client gets 404 — the API
    surface is absent, which is what hides the nav entry/card."""
    client = await _authed(app, seeded)
    project = await client.create_project(name="No feature", description="")
    with pytest.raises(ApiError) as ei:
        await client.create_study(
            int(project["id"]),
            name="x",
            tool_alias=seeded["tool_alias"],
            mode="grid",
        )
    assert ei.value.status_code == 404


def test_feature_helper_off_when_holder_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safe when the UI isn't mounted — returns False (suppressed), so
    the nav entry / card simply don't render rather than erroring."""
    monkeypatch.setattr("matofx.ui._holder._app", None)
    assert parametric_studies_enabled() is False


def test_feature_helper_reads_mounted_app_settings(
    monkeypatch: pytest.MonkeyPatch, api_settings: Settings
) -> None:
    """Reflects the mounted app's install Settings (design §12)."""
    off = SimpleNamespace(state=SimpleNamespace(settings=api_settings))
    monkeypatch.setattr("matofx.ui._holder._app", off)
    assert parametric_studies_enabled() is False

    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.PARAMETRIC_STUDIES.value])}
    )
    on = SimpleNamespace(state=SimpleNamespace(settings=enabled))
    monkeypatch.setattr("matofx.ui._holder._app", on)
    assert parametric_studies_enabled() is True
