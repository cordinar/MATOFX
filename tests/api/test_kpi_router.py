"""API tests for the KPI router (ADR-0012 §7).

Covers the feature-flag gating (routes absent when off), the catalog
endpoint, the warehouse list + pagination + ACL filtering + job_ids
allow-list, the time-series endpoint, the CSV export streaming, and
input-validation 400s.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Any

import httpx
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


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


def _enable_feature(app: FastAPI, api_settings: Settings) -> None:
    """Flip ``features.kpi_extraction`` on for this app instance."""
    enabled = api_settings.model_copy(
        update={
            "features": FeatureSettings(enabled=[Feature.KPI_EXTRACTION.value])
        }
    )
    app.dependency_overrides[get_settings] = lambda: enabled


_KPI_DESCRIPTOR_OUTPUTS = [
    {
        "name": "max_temperature",
        "label": "Max temperature",
        "type": "number",
        "unit": "K",
        "kpi": True,
        "kpi_label": "Peak temp",
        "kpi_unit": "K",
        "kpi_category": "thermal",
    },
    {
        "name": "max_pressure",
        "label": "Max pressure",
        "type": "number",
        "unit": "Pa",
        "kpi": True,
    },
]


async def _patch_descriptor_with_kpis(
    api_factory: SessionFactory, *, tool_alias: str
) -> int:
    """Stamp the seeded tool's first ToolVersion snapshot with KPI outputs.

    Returns the tool_version_id so callers can FK jobs against it.
    """
    async with session_scope(api_factory) as session:
        tv = (
            await session.execute(
                sa.select(ToolVersion)
                .where(ToolVersion.alias == tool_alias)
                .order_by(ToolVersion.created_at.asc())
                .limit(1)
            )
        ).scalar_one()
        snapshot = dict(tv.descriptor_snapshot or {})
        snapshot["outputs"] = list(_KPI_DESCRIPTOR_OUTPUTS)
        tv.descriptor_snapshot = snapshot
        await session.flush()
        return int(tv.id)


async def _seed_completed_job_and_kpis(
    api_factory: SessionFactory,
    *,
    seeded: dict[str, Any],
    tool_version_id: int,
    project_name: str,
    scenario_name: str,
    job_name: str,
    max_temperature: float | None,
    max_pressure: float | None,
) -> tuple[int, int, int]:
    """Create a project + scenario + completed job + two KPI rows.

    Returns ``(project_id, scenario_id, job_id)`` for assertion shaping.
    """
    from matofx.hierarchy.orm import Job, Project, Scenario  # noqa: PLC0415

    async with session_scope(api_factory) as session:
        project = Project(
            name=project_name,
            creator_user_id=seeded["user_id"],
            state="active",
        )
        session.add(project)
        await session.flush()
        scenario = Scenario(
            project_id=project.id,
            name=scenario_name,
            state="active",
        )
        session.add(scenario)
        await session.flush()
        now = _utcnow()
        future = now + _dt.timedelta(seconds=1)
        job = Job(
            scenario_id=scenario.id,
            name=job_name,
            tool_alias=seeded["tool_alias"],
            tool_version_id=tool_version_id,
            state="complete",
            inputs={},
            created_by_user_id=seeded["user_id"],
            created_at=now,
            started_at=now,
            completed_at=future,
        )
        session.add(job)
        await session.flush()
        if max_temperature is not None:
            session.add(
                JobKpiValue(
                    job_id=job.id,
                    tool_alias=seeded["tool_alias"],
                    tool_version_id=tool_version_id,
                    kpi_name="max_temperature",
                    value_numeric=Decimal(str(max_temperature)),
                    unit="K",
                )
            )
        if max_pressure is not None:
            session.add(
                JobKpiValue(
                    job_id=job.id,
                    tool_alias=seeded["tool_alias"],
                    tool_version_id=tool_version_id,
                    kpi_name="max_pressure",
                    value_numeric=Decimal(str(max_pressure)),
                    unit="Pa",
                )
            )
        await session.flush()
        return int(project.id), int(scenario.id), int(job.id)


# ---- Feature flag (slice 5 §11 — 404-when-off pattern) ---------------


async def test_routes_absent_when_feature_off(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """With the flag off every KPI route 404s — surface is *absent*."""
    alias = seeded["tool_alias"]
    catalog = await client.get(f"/api/tools/{alias}/kpis", headers=auth_headers)
    assert catalog.status_code == 404
    results = await client.get(
        f"/api/tools/{alias}/results", headers=auth_headers
    )
    assert results.status_code == 404
    timeseries = await client.get(
        f"/api/tools/{alias}/results/timeseries?kpi_name=x",
        headers=auth_headers,
    )
    assert timeseries.status_code == 404
    csv_resp = await client.get(
        f"/api/tools/{alias}/results/export.csv", headers=auth_headers
    )
    assert csv_resp.status_code == 404


# ---- /kpis -----------------------------------------------------------


async def test_kpis_returns_declared_catalog(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Catalog response lists each declared KPI with label / unit / category."""
    _enable_feature(app, api_settings)
    await _patch_descriptor_with_kpis(api_factory, tool_alias=seeded["tool_alias"])
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/kpis", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tool_alias"] == seeded["tool_alias"]
    names = {e["kpi_name"] for e in body["entries"]}
    assert names == {"max_temperature", "max_pressure"}
    by_name = {e["kpi_name"]: e for e in body["entries"]}
    assert by_name["max_temperature"]["kpi_label"] == "Peak temp"
    assert by_name["max_temperature"]["kpi_unit"] == "K"
    assert by_name["max_temperature"]["kpi_category"] == "thermal"
    # max_pressure has no explicit kpi_unit; fallback is unit="Pa".
    assert by_name["max_pressure"]["kpi_unit"] == "Pa"


async def test_kpis_empty_for_tool_with_no_declared_kpis(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """A tool whose descriptor declares no KPIs gets an empty entries list."""
    _enable_feature(app, api_settings)
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/kpis", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["entries"] == []


# ---- /results --------------------------------------------------------


async def test_results_empty_when_no_jobs(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """A tool with KPIs declared but no completed jobs returns an empty page."""
    _enable_feature(app, api_settings)
    await _patch_descriptor_with_kpis(api_factory, tool_alias=seeded["tool_alias"])
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rows"] == []
    assert body["total"] == 0
    assert body["hidden_by_acl"] == 0
    assert body["page"] == 1
    assert body["page_size"] == 50


async def test_results_returns_pivoted_rows(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """A populated install returns one row per completed job with KPI values pivoted."""
    _enable_feature(app, api_settings)
    tv_id = await _patch_descriptor_with_kpis(
        api_factory, tool_alias=seeded["tool_alias"]
    )
    project_id, _scenario_id, job_id = await _seed_completed_job_and_kpis(
        api_factory,
        seeded=seeded,
        tool_version_id=tv_id,
        project_name="P1",
        scenario_name="S1",
        job_name="J1",
        max_temperature=372.15,
        max_pressure=101325,
    )

    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["job_id"] == job_id
    assert row["project_id"] == project_id
    assert row["tool_version_id"] == tv_id
    # Identity columns also carry human-readable labels, not just ids
    # (feedback_use_labels_not_names).
    assert row["project_name"] == "P1"
    assert row["scenario_name"] == "S1"
    assert row["requestor_name"]  # the seeded user's display name
    assert row["tool_version_label"] and row["tool_version_label"].startswith("v")
    assert row["kpi_values"] == {
        "max_temperature": pytest.approx(372.15),
        "max_pressure": pytest.approx(101325.0),
    }
    assert row["kpi_units"] == {"max_temperature": "K", "max_pressure": "Pa"}


async def test_results_pagination_walks_jobs(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Pagination edges — page=2&page_size=1 returns the second job."""
    _enable_feature(app, api_settings)
    tv_id = await _patch_descriptor_with_kpis(
        api_factory, tool_alias=seeded["tool_alias"]
    )
    job_ids: list[int] = []
    for i in range(3):
        _p, _s, j = await _seed_completed_job_and_kpis(
            api_factory,
            seeded=seeded,
            tool_version_id=tv_id,
            project_name=f"P{i}",
            scenario_name=f"S{i}",
            job_name=f"J{i}",
            max_temperature=300.0 + i,
            max_pressure=100.0 + i,
        )
        job_ids.append(j)

    # Newest-first by Job.id desc — page=2 of size 1 yields the 2nd-newest.
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results?page=2&page_size=1",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["page"] == 2
    assert body["page_size"] == 1
    assert len(body["rows"]) == 1
    assert body["rows"][0]["job_id"] == job_ids[1]  # 2nd-newest


async def test_results_version_filter_narrows_to_one_version(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """``?version_id=`` narrows to jobs of that specific tool_version."""
    _enable_feature(app, api_settings)
    tv_id_a = await _patch_descriptor_with_kpis(
        api_factory, tool_alias=seeded["tool_alias"]
    )
    # Seed a second ToolVersion sharing the alias.
    async with session_scope(api_factory) as session:
        tv_b = ToolVersion(
            alias=seeded["tool_alias"],
            version="9.9.9",
            descriptor_snapshot={
                "alias": seeded["tool_alias"],
                "outputs": list(_KPI_DESCRIPTOR_OUTPUTS),
            },
            bundle_path=f"bundle/{seeded['tool_alias']}/9.9.9",
            bundle_hash="bbbb" * 16,
            created_at=_utcnow(),
            created_by_user_id=seeded["user_id"],
            change_summary="seed b",
            changes={},
        )
        session.add(tv_b)
        await session.flush()
        tv_id_b = int(tv_b.id)

    # One job per version.
    _, _, job_a = await _seed_completed_job_and_kpis(
        api_factory,
        seeded=seeded,
        tool_version_id=tv_id_a,
        project_name="Pa",
        scenario_name="Sa",
        job_name="Ja",
        max_temperature=300.0,
        max_pressure=100.0,
    )
    _, _, job_b = await _seed_completed_job_and_kpis(
        api_factory,
        seeded=seeded,
        tool_version_id=tv_id_b,
        project_name="Pb",
        scenario_name="Sb",
        job_name="Jb",
        max_temperature=400.0,
        max_pressure=200.0,
    )

    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results?version_id={tv_id_b}",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["rows"][0]["job_id"] == job_b
    assert body["rows"][0]["tool_version_id"] == tv_id_b
    # job_a is *not* in the result.
    del job_a


async def test_results_job_ids_allow_list_intersects(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """``?job_ids=1,3`` returns only those jobs (intersected with ACL + filters)."""
    _enable_feature(app, api_settings)
    tv_id = await _patch_descriptor_with_kpis(
        api_factory, tool_alias=seeded["tool_alias"]
    )
    jobs = []
    for i in range(3):
        _, _, j = await _seed_completed_job_and_kpis(
            api_factory,
            seeded=seeded,
            tool_version_id=tv_id,
            project_name=f"P{i}",
            scenario_name=f"S{i}",
            job_name=f"J{i}",
            max_temperature=300.0 + i,
            max_pressure=100.0 + i,
        )
        jobs.append(j)

    keep = f"{jobs[0]},{jobs[2]}"
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results?job_ids={keep}",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {r["job_id"] for r in body["rows"]} == {jobs[0], jobs[2]}


async def test_results_job_ids_empty_returns_zero(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """``?job_ids=`` (empty allow-list) returns zero rows by design."""
    _enable_feature(app, api_settings)
    tv_id = await _patch_descriptor_with_kpis(
        api_factory, tool_alias=seeded["tool_alias"]
    )
    await _seed_completed_job_and_kpis(
        api_factory,
        seeded=seeded,
        tool_version_id=tv_id,
        project_name="P",
        scenario_name="S",
        job_name="J",
        max_temperature=300.0,
        max_pressure=100.0,
    )
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results?job_ids=",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


async def test_results_job_ids_garbage_rejected(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Non-integer ``job_ids`` triggers a 400."""
    _enable_feature(app, api_settings)
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results?job_ids=abc",
        headers=auth_headers,
    )
    assert resp.status_code == 400


async def test_results_completed_window_filter(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """``?completed_from`` and ``?completed_to`` honour the time window."""
    _enable_feature(app, api_settings)
    tv_id = await _patch_descriptor_with_kpis(
        api_factory, tool_alias=seeded["tool_alias"]
    )
    _, _, job_id = await _seed_completed_job_and_kpis(
        api_factory,
        seeded=seeded,
        tool_version_id=tv_id,
        project_name="P",
        scenario_name="S",
        job_name="J",
        max_temperature=300.0,
        max_pressure=100.0,
    )
    # Window in the future — excludes the job. Pass via params so the
    # ``+`` in the timezone offset isn't URL-decoded into a space.
    future = (_utcnow() + _dt.timedelta(days=1)).isoformat()
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results",
        headers=auth_headers,
        params={"completed_from": future},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 0
    # Window includes "now" — includes the job.
    past = (_utcnow() - _dt.timedelta(days=1)).isoformat()
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results",
        headers=auth_headers,
        params={"completed_from": past},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["rows"][0]["job_id"] == job_id


async def test_results_completed_from_garbage_rejected(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """A non-ISO ``completed_from`` triggers a 400."""
    _enable_feature(app, api_settings)
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results?completed_from=garbage",
        headers=auth_headers,
    )
    assert resp.status_code == 400


# ---- /results/timeseries ---------------------------------------------


async def test_timeseries_returns_points_for_kpi(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Time-series of one KPI lists ``(extracted_at, value, job_id)`` points."""
    _enable_feature(app, api_settings)
    tv_id = await _patch_descriptor_with_kpis(
        api_factory, tool_alias=seeded["tool_alias"]
    )
    jobs = []
    for i in range(3):
        _, _, j = await _seed_completed_job_and_kpis(
            api_factory,
            seeded=seeded,
            tool_version_id=tv_id,
            project_name=f"P{i}",
            scenario_name=f"S{i}",
            job_name=f"J{i}",
            max_temperature=300.0 + i,
            max_pressure=None,  # no max_pressure rows
        )
        jobs.append(j)

    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results/timeseries?kpi_name=max_temperature",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kpi_name"] == "max_temperature"
    assert len(body["points"]) == 3
    # Only max_temperature points come back (no max_pressure rows).
    values = sorted(p["value_numeric"] for p in body["points"])
    assert values == pytest.approx([300.0, 301.0, 302.0])


async def test_timeseries_empty_when_kpi_unknown(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Asking for a KPI no row matches returns an empty points list."""
    _enable_feature(app, api_settings)
    tv_id = await _patch_descriptor_with_kpis(
        api_factory, tool_alias=seeded["tool_alias"]
    )
    await _seed_completed_job_and_kpis(
        api_factory,
        seeded=seeded,
        tool_version_id=tv_id,
        project_name="P",
        scenario_name="S",
        job_name="J",
        max_temperature=300.0,
        max_pressure=None,
    )
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results/timeseries?kpi_name=nonexistent",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["points"] == []


# ---- /results/export.csv ---------------------------------------------


async def test_csv_export_returns_streaming_csv(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """The CSV stream has a header + one row per filtered job."""
    _enable_feature(app, api_settings)
    tv_id = await _patch_descriptor_with_kpis(
        api_factory, tool_alias=seeded["tool_alias"]
    )
    _p, _s, job_id = await _seed_completed_job_and_kpis(
        api_factory,
        seeded=seeded,
        tool_version_id=tv_id,
        project_name="P",
        scenario_name="S",
        job_name="J",
        max_temperature=372.15,
        max_pressure=101325,
    )
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results/export.csv",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    body = resp.text
    lines = body.splitlines()
    # Header + one data row.
    assert len(lines) == 2
    header_cells = lines[0].split(",")
    # Headers are human labels, not internal snake_case names
    # ([[feedback_use_labels_not_names]]).
    assert header_cells[0] == "Job ID"
    for col in ("Project", "Scenario", "Requestor", "Tool version"):
        assert col in header_cells
    # KPI columns use kpi_label, else the output's own label — *never*
    # the internal snake_case name ([[feedback_use_labels_not_names]]),
    # and the unit is appended. This assertion previously expected the
    # raw `max_pressure` fallback, which the labels rule removed.
    assert "Peak temp (K)" in lines[0]
    assert "Max pressure (Pa)" in lines[0]
    assert "max_pressure" not in lines[0], "internal names must not reach a header"
    # Data row: starts with the job id; carries the decimal value verbatim.
    data_cells = lines[1].split(",")
    assert int(data_cells[0]) == job_id
    assert "372.15" in lines[1]


async def test_csv_export_job_ids_filter_narrows_to_selection(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """``?job_ids=`` restricts the CSV to just the ticked rows."""
    _enable_feature(app, api_settings)
    tv_id = await _patch_descriptor_with_kpis(
        api_factory, tool_alias=seeded["tool_alias"]
    )
    jobs = []
    for i in range(3):
        _, _, j = await _seed_completed_job_and_kpis(
            api_factory,
            seeded=seeded,
            tool_version_id=tv_id,
            project_name=f"P{i}",
            scenario_name=f"S{i}",
            job_name=f"J{i}",
            max_temperature=300.0 + i,
            max_pressure=100.0 + i,
        )
        jobs.append(j)
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results/export.csv?job_ids={jobs[1]}",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    lines = resp.text.splitlines()
    # Header + 1 row.
    assert len(lines) == 2
    assert str(jobs[1]) == lines[1].split(",")[0]


# ---- /results/export.pdf (slice 7) ----------------------------------


async def test_pdf_export_absent_when_feature_off(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """Flag off → PDF endpoint 404s like the rest of the surface."""
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results/export.pdf",
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_pdf_export_returns_printable_html(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """PDF endpoint returns text/html with the table + print script."""
    _enable_feature(app, api_settings)
    tv_id = await _patch_descriptor_with_kpis(
        api_factory, tool_alias=seeded["tool_alias"]
    )
    _, _, job_id = await _seed_completed_job_and_kpis(
        api_factory,
        seeded=seeded,
        tool_version_id=tv_id,
        project_name="P",
        scenario_name="S",
        job_name="J",
        max_temperature=372.15,
        max_pressure=101325,
    )

    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results/export.pdf",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    # Print-on-load script + table + the captured value.
    assert "window.print" in body
    assert "<table>" in body
    assert "372.15" in body
    # The KPI column header uses the kpi_label.
    assert "Peak temp" in body
    # The job id shows in the table.
    assert f">{job_id}<" in body
    # The report title uses the tool's display name, not its internal alias
    # ([[feedback_use_labels_not_names]]).
    tool = (
        await client.get(f"/api/tools/{seeded['tool_alias']}", headers=auth_headers)
    ).json()
    assert f"Results — {tool['name']}" in body


async def test_pdf_export_job_ids_filter_narrows_to_selection(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """PDF respects the ``job_ids`` allow-list, like CSV."""
    _enable_feature(app, api_settings)
    tv_id = await _patch_descriptor_with_kpis(
        api_factory, tool_alias=seeded["tool_alias"]
    )
    jobs = []
    for i in range(3):
        _, _, j = await _seed_completed_job_and_kpis(
            api_factory,
            seeded=seeded,
            tool_version_id=tv_id,
            project_name=f"P{i}",
            scenario_name=f"S{i}",
            job_name=f"J{i}",
            max_temperature=300.0 + i,
            max_pressure=100.0 + i,
        )
        jobs.append(j)
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results/export.pdf?job_ids={jobs[1]}",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.text
    # One data row only. Header + 1 body row = 2 ``<tr>`` instances.
    assert body.count("<tr>") == 2
    # Footer carries the matched total.
    assert "1 row(s) matched these filters" in body


async def test_pdf_export_empty_state(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """No jobs → empty-state markup, no <table>."""
    _enable_feature(app, api_settings)
    await _patch_descriptor_with_kpis(api_factory, tool_alias=seeded["tool_alias"])
    resp = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results/export.pdf",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert "No rows matched" in resp.text
    assert "<table>" not in resp.text


# ---- KPI value-range + V&V-status filters (docs-promised filtering) ----


async def test_kpi_range_filter_narrows_by_value(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """``kpi_range=name:min:max`` keeps only jobs whose KPI value is in range."""
    _enable_feature(app, api_settings)
    tv_id = await _patch_descriptor_with_kpis(api_factory, tool_alias=seeded["tool_alias"])
    _p, _s, cold = await _seed_completed_job_and_kpis(
        api_factory,
        seeded=seeded,
        tool_version_id=tv_id,
        project_name="P",
        scenario_name="S",
        job_name="cold",
        max_temperature=300.0,
        max_pressure=None,
    )
    _p2, _s2, hot = await _seed_completed_job_and_kpis(
        api_factory,
        seeded=seeded,
        tool_version_id=tv_id,
        project_name="P2",
        scenario_name="S2",
        job_name="hot",
        max_temperature=500.0,
        max_pressure=None,
    )

    # Lower bound only: >= 400 keeps just the hot run.
    r = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results",
        headers=auth_headers,
        params={"kpi_range": "max_temperature:400:"},
    )
    assert r.status_code == 200, r.text
    ids = {row["job_id"] for row in r.json()["rows"]}
    assert ids == {hot}

    # Upper bound only: <= 400 keeps just the cold run.
    r2 = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results",
        headers=auth_headers,
        params={"kpi_range": "max_temperature::400"},
    )
    assert {row["job_id"] for row in r2.json()["rows"]} == {cold}

    # Both bounds bracketing only the cold run.
    r3 = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results",
        headers=auth_headers,
        params={"kpi_range": "max_temperature:250:350"},
    )
    assert {row["job_id"] for row in r3.json()["rows"]} == {cold}


async def test_kpi_range_filter_bad_bound_is_400(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """A non-numeric bound is a 400."""
    _enable_feature(app, api_settings)
    del seeded
    # The bad-bound 400 fires during filter parsing, before any tool lookup.
    r = await client.get(
        "/api/tools/anything/results",
        headers=auth_headers,
        params={"kpi_range": "max_temperature:abc:"},
    )
    assert r.status_code == 400


async def test_kpi_range_filter_flows_through_csv_export(
    app: FastAPI,
    api_settings: Settings,
    api_factory: SessionFactory,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """The CSV export honours the same KPI-range filter as the table."""
    _enable_feature(app, api_settings)
    tv_id = await _patch_descriptor_with_kpis(api_factory, tool_alias=seeded["tool_alias"])
    await _seed_completed_job_and_kpis(
        api_factory,
        seeded=seeded,
        tool_version_id=tv_id,
        project_name="P",
        scenario_name="S",
        job_name="cold",
        max_temperature=300.0,
        max_pressure=None,
    )
    _p2, _s2, hot = await _seed_completed_job_and_kpis(
        api_factory,
        seeded=seeded,
        tool_version_id=tv_id,
        project_name="P2",
        scenario_name="S2",
        job_name="hot",
        max_temperature=500.0,
        max_pressure=None,
    )
    r = await client.get(
        f"/api/tools/{seeded['tool_alias']}/results/export.csv",
        headers=auth_headers,
        params={"kpi_range": "max_temperature:400:"},
    )
    assert r.status_code == 200
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    # header + exactly the hot run.
    assert len(lines) == 2
    assert lines[1].split(",")[0] == str(hot)


async def test_bad_vv_status_is_400(
    app: FastAPI,
    api_settings: Settings,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded: dict[str, Any],
) -> None:
    """An out-of-vocabulary vv_status is rejected on the CSV export too."""
    _enable_feature(app, api_settings)
    del seeded
    r = await client.get(
        "/api/tools/anything/results/export.csv",
        headers=auth_headers,
        params={"vv_status": "sideways"},
    )
    assert r.status_code == 400
