"""UI api_client coverage for the lineage surface (ADR-0010, slice 4a).

NiceGUI page rendering isn't unit-tested in this repo; this file
exercises the :meth:`matofx.ui.api_client.ApiClient.lineage_graph`
method end-to-end against the real app + verifies the error mapping
the :func:`matofx.ui.lineage_view.render_lineage_view` component
relies on (404 collapses to a friendly inline notice). Mirrors
``tests/ui/test_library_client.py``.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest
from fastapi import FastAPI

from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.tools.orm import ToolVersion
from matofx.ui.api_client import ApiClient, ApiError


async def _authed(app: FastAPI, seeded: dict[str, Any]) -> ApiClient:
    """Log in via the API client and return one with the bearer token."""
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    return ApiClient(app, token=auth["access_token"])


async def _seed_job(factory: SessionFactory, seeded: dict[str, Any]) -> int:
    """Seed one job under a fresh project/scenario; return its id."""
    from sqlalchemy import select  # noqa: PLC0415

    async with session_scope(factory) as session:
        version = (
            await session.execute(
                select(ToolVersion)
                .where(ToolVersion.alias == seeded["tool_alias"])
                .order_by(ToolVersion.id.desc())
                .limit(1)
            )
        ).scalar_one()
        project = Project(name="UI lineage project", creator_user_id=seeded["user_id"])
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="UI lineage scenario")
        session.add(scenario)
        await session.flush()
        job = Job(
            scenario_id=scenario.id,
            name="ui-lineage-job",
            tool_alias=seeded["tool_alias"],
            tool_version_id=version.id,
            state="complete",
            inputs={},
            created_by_user_id=seeded["user_id"],
            created_at=_dt.datetime.now(tz=_dt.UTC),
        )
        session.add(job)
        await session.flush()
        return int(job.id)


@pytest.mark.asyncio
async def test_lineage_graph_returns_centred_dict(
    app: FastAPI,
    seeded: dict[str, Any],
    api_factory: SessionFactory,
) -> None:
    """The wire dict matches the component's expectations (kind:id centre)."""
    client = await _authed(app, seeded)
    job_id = await _seed_job(api_factory, seeded)

    graph = await client.lineage_graph(kind="job", obj_id=job_id, upstream=1, downstream=0)

    assert graph["centre"] == f"job:{job_id}"
    assert isinstance(graph["nodes"], list)
    assert isinstance(graph["edges"], list)
    # Every node carries the dict-shape ``render_lineage_view`` reads.
    for node in graph["nodes"]:
        assert {"kind", "id", "label", "state", "archived", "deep_link", "payload"} <= node.keys()
    # Every edge carries the source/target shape.
    for edge in graph["edges"]:
        assert isinstance(edge["source"], list)
        assert len(edge["source"]) == 2
        assert isinstance(edge["target"], list)
        assert len(edge["target"]) == 2


@pytest.mark.asyncio
async def test_lineage_graph_404_surfaces_as_api_error(
    app: FastAPI, seeded: dict[str, Any]
) -> None:
    """Unknown id → :class:`ApiError(status=404)`; the view layer catches this."""
    client = await _authed(app, seeded)
    with pytest.raises(ApiError) as excinfo:
        await client.lineage_graph(kind="job", obj_id=99_999_999, upstream=1)
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_lineage_graph_unknown_kind_surfaces_as_api_error(
    app: FastAPI, seeded: dict[str, Any]
) -> None:
    """Bad ``kind`` → 422 with a helpful detail."""
    client = await _authed(app, seeded)
    with pytest.raises(ApiError) as excinfo:
        await client.lineage_graph(kind="banana", obj_id=1)
    assert excinfo.value.status_code == 422
