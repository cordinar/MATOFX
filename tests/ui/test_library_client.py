"""UI api_client coverage for the file-library surface (ADR-0006 §8, slice 7).

NiceGUI page rendering isn't unit-tested; these exercise the library
client methods the workspace + detail pages call, end-to-end against the
real app, plus the feature-flag behaviour the nav entry relies on.
Mirrors tests/ui/test_studies_client.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from matofx.api.deps import get_settings
from matofx.config import FeatureSettings, Settings
from matofx.features import Feature
from matofx.ui._features import file_library_enabled
from matofx.ui.api_client import ApiClient, ApiError


def _enable_feature(app: FastAPI, api_settings: Settings) -> None:
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.FILE_LIBRARY.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


async def _authed(app: FastAPI, seeded: dict[str, Any]) -> ApiClient:
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    return ApiClient(app, token=auth["access_token"])


@pytest.mark.asyncio
async def test_library_client_end_to_end(
    app: FastAPI, api_settings: Settings, seeded: dict[str, Any]
) -> None:
    _enable_feature(app, api_settings)
    client = await _authed(app, seeded)
    alias = seeded["tool_alias"]

    created = await client.create_library_file(
        metadata={
            "name": "Inlet",
            "description": "An inlet duct.",
            "provenance": "CAD export",
            "scope": "personal",
            "tool_assignments": [{"tool_alias": alias, "field_name": "geometry"}],
        },
        filename="inlet.stp",
        content=b"solid inlet",
    )
    fid = int(created["file"]["id"])
    assert created["file"]["scope"] == "personal"
    assert created["versions"][0]["publish_state"] == "published"
    assert len(created["tools"]) == 1

    listed = await client.list_library_files()
    assert fid in [int(f["id"]) for f in listed]

    detail = await client.get_library_file(fid)
    assert int(detail["file"]["id"]) == fid

    added = await client.add_library_version(
        fid, metadata={"provenance": "CAD rev B"}, filename="inlet_v2.stp", content=b"solid v2"
    )
    assert [v["ordinal"] for v in added["versions"]] == [2, 1]
    v2_id = int(added["versions"][0]["id"])

    patched = await client.update_library_metadata(fid, body={"name": "Inlet (final)"})
    assert patched["file"]["name"] == "Inlet (final)"

    assigned = await client.assign_library_tool(fid, tool_alias=alias, field_name="mesh")
    assert len(assigned["tools"]) == 2

    # The published personal file is pickable by its owner; approvers is a list.
    picked = await client.library_picker(tool=alias, field="geometry")
    assert fid in [int(c["file_id"]) for c in picked]
    assert isinstance(await client.library_approvers(), list)

    content, filename = await client.download_library_version(fid, v2_id)
    assert content == b"solid v2"
    assert "inlet_v2.stp" in filename

    retired = await client.retire_library_file(fid)
    assert retired["state"] == "retired"

    restored = await client.restore_library_file(fid)
    assert restored["state"] == "active"


@pytest.mark.asyncio
async def test_library_routes_absent_when_feature_off(app: FastAPI, seeded: dict[str, Any]) -> None:
    """Default settings (flag off): the client gets 404 — the surface is
    absent, which is what hides the nav entry."""
    client = await _authed(app, seeded)
    with pytest.raises(ApiError) as ei:
        await client.list_library_files()
    assert ei.value.status_code == 404


def test_file_library_helper_off_when_holder_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safe when the UI isn't mounted — returns False (suppressed), so the
    nav entry simply doesn't render rather than erroring."""
    monkeypatch.setattr("matofx.ui._holder._app", None)
    assert file_library_enabled() is False
