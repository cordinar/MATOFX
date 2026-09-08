"""DB integration tests for headless workflow orchestration.

The optimisation ``workflow`` target (ADR-0009 §3.4) needs a workflow run
that drives itself to completion with no human submitting tabs. These
cover :meth:`WorkflowRunService.start_headless_run` (queues the source
member) and :meth:`advance` (cascades the downstream member once the
upstream completes, transferring the wired value).

``JobService.queue_for_runner`` is faked (state flip + input capture, the
same monkeypatch the studies suite uses) so the test exercises the
orchestration wiring without real working-dir staging; the upstream
output read is faked too.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.tools import ToolService
from matofx.workflows import WorkflowService
from matofx.workflows.run_service import make_workflow_run_service
from matofx.workflows.service import WorkflowDraft

from .conftest import install_released_tool, make_tool_descriptor


async def _seed_project_scenario(factory: Any, *, owner_id: int) -> tuple[int, int]:
    async with session_scope(factory) as session:
        project = Project(name="hl-proj", description="", state="active", creator_user_id=owner_id)
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="hl-scn", state="active")
        session.add(scenario)
        await session.flush()
        return int(project.id), int(scenario.id)


async def _seed_chain(
    factory: Any, tool_service: ToolService, workflow_service: WorkflowService, *, admin_id: int
) -> str:
    """A 2-member chain: ``mat`` → ``heat`` (heat consumes mat's youngs_modulus)."""
    descriptors = [
        make_tool_descriptor(alias="mat", downstream_tools=["heat"], teams=["World"]),
        make_tool_descriptor(
            alias="heat",
            inputs=[
                {"name": "mesh_size", "label": "Mesh", "type": "float"},
                {
                    "name": "youngs_modulus",
                    "label": "E",
                    "type": "float",
                    "downstream_input": True,
                    "upstream_tool": "mat",
                },
            ],
            teams=["World"],
        ),
    ]
    for d in descriptors:
        await install_released_tool(factory, tool_service, actor_user_id=admin_id, descriptor=d)
    async with session_scope(factory) as session:
        wf = await workflow_service.install(
            session,
            actor_user_id=admin_id,
            draft=WorkflowDraft(
                alias="mat_heat",
                name="Mat→Heat",
                description="",
                tool_aliases=["mat", "heat"],
                teams=["World"],
            ),
        )
        return str(wf.alias)


def _fake_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _queue(_self: Any, session: Any, *, job_id: int, inputs: dict, **_kw: Any) -> Job:
        job = await session.get(Job, job_id)
        job.state = "queued"
        job.inputs = dict(inputs)
        await session.flush()
        return job

    monkeypatch.setattr("matofx.hierarchy.service.JobService.queue_for_runner", _queue)


@pytest.mark.asyncio
async def test_start_headless_queues_only_the_source(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    alias = await _seed_chain(factory, tool_service, workflow_service, admin_id=admin_id)
    _project_id, scenario_id = await _seed_project_scenario(factory, owner_id=admin_id)
    _fake_queue(monkeypatch)

    service = make_workflow_run_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        run = await service.start_headless_run(
            session,
            actor_user_id=admin_id,
            scenario_id=scenario_id,
            workflow_alias=alias,
            open_inputs={"heat": {"mesh_size": 1.5}},
        )
        run_id = run.id
        assert run.auto_advance is True
        assert run.open_inputs_json == {"heat": {"mesh_size": 1.5}}

    async with session_scope(factory) as session:
        children = list(
            (
                await session.execute(
                    select(Job).where(Job.workflow_run_id == run_id).order_by(Job.workflow_position)
                )
            ).scalars()
        )
        by_alias = {c.tool_alias: c for c in children}
        # The source (mat, no wired upstream) is queued; heat waits.
        assert by_alias["mat"].state == "queued"
        assert by_alias["heat"].state == "created"


@pytest.mark.asyncio
async def test_advance_cascades_downstream_with_wired_value(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    alias = await _seed_chain(factory, tool_service, workflow_service, admin_id=admin_id)
    _project_id, scenario_id = await _seed_project_scenario(factory, owner_id=admin_id)
    _fake_queue(monkeypatch)

    service = make_workflow_run_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        run = await service.start_headless_run(
            session,
            actor_user_id=admin_id,
            scenario_id=scenario_id,
            workflow_alias=alias,
            open_inputs={"heat": {"mesh_size": 1.5}},
        )
        run_id = run.id

    # Simulate the runner finishing `mat` with a youngs_modulus output.
    async def _fake_outputs(_self: Any, _session: Any, *, job_id: int) -> Any:  # noqa: ARG001
        return SimpleNamespace(
            outputs=[SimpleNamespace(name="youngs_modulus", value=210.0)],
            tool_alias="mat",
        )

    monkeypatch.setattr("matofx.hierarchy.service.JobService.get_output_document", _fake_outputs)
    async with session_scope(factory) as session:
        mat = (
            await session.execute(
                select(Job).where(Job.workflow_run_id == run_id, Job.tool_alias == "mat")
            )
        ).scalar_one()
        mat.state = "complete"
        await session.flush()
        await service.advance(session, run_id=run_id, actor_user_id=admin_id)

    async with session_scope(factory) as session:
        heat = (
            await session.execute(
                select(Job).where(Job.workflow_run_id == run_id, Job.tool_alias == "heat")
            )
        ).scalar_one()
        assert heat.state == "queued"
        # Open input + the wired youngs_modulus pulled from mat's output.
        assert heat.inputs == {"mesh_size": 1.5, "youngs_modulus": 210.0}
