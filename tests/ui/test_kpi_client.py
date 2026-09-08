"""UI api_client coverage for the KPI surface (ADR-0012 §8).

NiceGUI page rendering isn't unit-tested; these exercise the KPI
client methods the warehouse page calls, end-to-end against the
real app, plus the feature-flag behaviour the nav entry relies on.
Mirrors tests/ui/test_materials_client.py.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from matofx.api.deps import get_settings
from matofx.config import FeatureSettings, Settings
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.features import Feature
from matofx.kpi import JobKpiValue
from matofx.tools.orm import ToolVersion
from matofx.ui._features import kpi_extraction_enabled
from matofx.ui.api_client import ApiClient, _build_results_export_url


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


def _enable_feature(app: FastAPI, api_settings: Settings) -> None:
    enabled = api_settings.model_copy(
        update={"features": FeatureSettings(enabled=[Feature.KPI_EXTRACTION.value])}
    )
    app.dependency_overrides[get_settings] = lambda: enabled


_KPI_OUTPUTS = [
    {
        "name": "max_temperature",
        "label": "Max temperature",
        "type": "number",
        "unit": "K",
        "kpi": True,
        "kpi_label": "Peak temp",
        "kpi_unit": "K",
    },
    {
        "name": "max_pressure",
        "label": "Max pressure",
        "type": "number",
        "unit": "Pa",
        "kpi": True,
    },
]


async def _seed_kpi_data(
    api_factory: SessionFactory, *, seeded: dict[str, Any]
) -> tuple[int, list[int]]:
    """Stamp KPI descriptor + seed 3 completed jobs with KPI rows.

    Returns ``(tool_version_id, [job_ids])``.
    """
    from matofx.hierarchy.orm import Job, Project, Scenario  # noqa: PLC0415

    async with session_scope(api_factory) as session:
        tv = (
            await session.execute(
                sa.select(ToolVersion)
                .where(ToolVersion.alias == seeded["tool_alias"])
                .order_by(ToolVersion.created_at.asc())
                .limit(1)
            )
        ).scalar_one()
        snapshot = dict(tv.descriptor_snapshot or {})
        snapshot["outputs"] = list(_KPI_OUTPUTS)
        tv.descriptor_snapshot = snapshot
        await session.flush()
        tv_id = int(tv.id)

        job_ids: list[int] = []
        for i in range(3):
            project = Project(
                name=f"P{i}",
                creator_user_id=seeded["user_id"],
                state="active",
            )
            session.add(project)
            await session.flush()
            scenario = Scenario(
                project_id=project.id,
                name=f"S{i}",
                state="active",
            )
            session.add(scenario)
            await session.flush()
            now = _utcnow()
            future = now + _dt.timedelta(seconds=1)
            job = Job(
                scenario_id=scenario.id,
                name=f"J{i}",
                tool_alias=seeded["tool_alias"],
                tool_version_id=tv_id,
                state="complete",
                inputs={},
                created_by_user_id=seeded["user_id"],
                created_at=now,
                started_at=now,
                completed_at=future,
            )
            session.add(job)
            await session.flush()
            session.add(
                JobKpiValue(
                    job_id=job.id,
                    tool_alias=seeded["tool_alias"],
                    tool_version_id=tv_id,
                    kpi_name="max_temperature",
                    value_numeric=Decimal(str(300.0 + i)),
                    unit="K",
                )
            )
            session.add(
                JobKpiValue(
                    job_id=job.id,
                    tool_alias=seeded["tool_alias"],
                    tool_version_id=tv_id,
                    kpi_name="max_pressure",
                    value_numeric=Decimal(str(100.0 + i)),
                    unit="Pa",
                )
            )
            job_ids.append(int(job.id))
        await session.flush()
        return tv_id, job_ids


# ---- Feature flag --------------------------------------------------


def test_kpi_extraction_enabled_helper_safe_default(
    seeded: dict[str, Any],
) -> None:
    """Without an app context, the helper defaults to ``False``."""
    del seeded
    assert kpi_extraction_enabled() is False


# ---- End-to-end client surface --------------------------------------


@pytest.mark.asyncio
async def test_list_tool_kpis_returns_catalog(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    seeded: dict[str, Any],
) -> None:
    """The catalog endpoint surfaces declared KPIs through the client."""
    _enable_feature(app, api_settings)
    await _seed_kpi_data(api_factory, seeded=seeded)
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    client = ApiClient(app, token=str(auth["access_token"]))
    catalog = await client.list_tool_kpis(seeded["tool_alias"])
    names = {e["kpi_name"] for e in catalog["entries"]}
    assert names == {"max_temperature", "max_pressure"}


@pytest.mark.asyncio
async def test_list_tool_results_returns_pivoted_rows(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    seeded: dict[str, Any],
) -> None:
    """The warehouse list endpoint round-trips through the client."""
    _enable_feature(app, api_settings)
    _, job_ids = await _seed_kpi_data(api_factory, seeded=seeded)
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    client = ApiClient(app, token=str(auth["access_token"]))
    page = await client.list_tool_results(seeded["tool_alias"], page_size=10)
    assert page["total"] == 3
    assert {r["job_id"] for r in page["rows"]} == set(job_ids)


@pytest.mark.asyncio
async def test_list_tool_results_with_job_ids_narrows(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    seeded: dict[str, Any],
) -> None:
    """The ``job_ids`` allow-list propagates correctly through the client."""
    _enable_feature(app, api_settings)
    _, job_ids = await _seed_kpi_data(api_factory, seeded=seeded)
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    client = ApiClient(app, token=str(auth["access_token"]))
    page = await client.list_tool_results(seeded["tool_alias"], job_ids=[job_ids[0], job_ids[2]])
    assert {r["job_id"] for r in page["rows"]} == {job_ids[0], job_ids[2]}


@pytest.mark.asyncio
async def test_tool_results_timeseries_returns_points(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    seeded: dict[str, Any],
) -> None:
    """The timeseries endpoint surfaces ``(extracted_at, value, job_id)`` points."""
    _enable_feature(app, api_settings)
    await _seed_kpi_data(api_factory, seeded=seeded)
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    client = ApiClient(app, token=str(auth["access_token"]))
    body = await client.tool_results_timeseries(seeded["tool_alias"], kpi_name="max_temperature")
    assert body["kpi_name"] == "max_temperature"
    assert len(body["points"]) == 3


def test_results_export_url_builds_no_params() -> None:
    """When no filters are set, the export URL has no query string."""
    assert _build_results_export_url("heat_transfer", "csv") == (
        "/api/tools/heat_transfer/results/export.csv"
    )


def test_results_export_url_encodes_job_ids() -> None:
    """``job_ids`` lands as a comma-separated string."""
    url = _build_results_export_url("heat_transfer", "csv", job_ids=[3, 1, 2])
    assert "job_ids=3%2C1%2C2" in url


def test_results_export_url_encodes_version_id() -> None:
    """``version_id`` is forwarded as a query param."""
    url = _build_results_export_url("heat_transfer", "pdf", version_id=42)
    assert url.startswith("/api/tools/heat_transfer/results/export.pdf")
    assert "version_id=42" in url


@pytest.mark.asyncio
async def test_download_tool_results_csv_returns_real_csv(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    seeded: dict[str, Any],
) -> None:
    """The api_client CSV download (bearer-carrying) returns real CSV bytes.

    This is the path the page uses (fetch server-side → ``ui.download.content``)
    after the bare-``<a href>`` links — which a browser can't authenticate —
    were removed.
    """
    _enable_feature(app, api_settings)
    await _seed_kpi_data(api_factory, seeded=seeded)
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    api_client = ApiClient(app, token=str(auth["access_token"]))
    data = await api_client.download_tool_results_csv(seeded["tool_alias"])
    assert isinstance(data, bytes)
    lines = data.decode("utf-8").splitlines()
    assert len(lines) >= 1  # header row at least
