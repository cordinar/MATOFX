"""Workflow file-wiring (ADR-0015): upstream file output → downstream file input.

Two layers:

- Pure/disk unit tests for the resolver helpers in
  :mod:`matofx.workflows.run_service` — file outputs resolve to absolute
  paths, a ``field_timesteps`` series expands, a missing artefact is omitted,
  and only file-typed wired inputs produce a source list.
- A DB integration test of :meth:`WorkflowRunService.advance`: a 2-member
  chain where the upstream produces a ``.vtp`` field and the downstream wires
  it as a file input. Advance must resolve the upstream's on-disk path, hand
  the downstream its staged value, and pass the per-field source list to
  ``queue_for_runner`` for the staging copy.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.tools import ToolService
from matofx.tools.exec_orm import StorageLocation
from matofx.workflows import WorkflowService
from matofx.workflows.run_service import (
    _descriptor_file_outputs,
    _MemberOutputs,
    _resolve_file_outputs,
    _wired_file_sources,
    make_workflow_run_service,
)
from matofx.workflows.service import WorkflowDraft

from .conftest import install_released_tool, make_tool_descriptor

# --- resolver helpers (pure + disk) -----------------------------------------


def test_descriptor_file_outputs_picks_only_file_entries() -> None:
    descriptor = {
        "outputs": [
            {"name": "stress", "type": "number"},
            {"name": "mesh", "type": "file", "filename": "mesh.vtp"},
            {
                "name": "temps",
                "type": "file",
                "filename": "temperature_final.npy",
                "field_timesteps": "outputs/temperature_*.npy",
            },
            {"name": "bad", "type": "file"},  # no filename → skipped
        ]
    }
    assert _descriptor_file_outputs(descriptor) == [
        ("mesh", "mesh.vtp", None),
        ("temps", "temperature_final.npy", "outputs/temperature_*.npy"),
    ]


def test_resolve_file_outputs_single_and_missing(tmp_path: Path) -> None:
    wd = tmp_path / "jobs" / "job_0000001"
    (wd / "outputs").mkdir(parents=True)
    (wd / "outputs" / "mesh.vtp").write_bytes(b"<vtp/>")

    resolved = _resolve_file_outputs(
        wd,
        [("mesh", "mesh.vtp", None), ("ghost", "missing.vtp", None)],
    )
    primary = str(wd / "outputs" / "mesh.vtp")
    # Present file resolves to (primary, [primary]); missing file omitted.
    assert resolved == {"mesh": (primary, [primary])}


def test_resolve_file_outputs_strips_outputs_prefix(tmp_path: Path) -> None:
    """A descriptor filename declared *with* the ``outputs/`` prefix (e.g.
    Thermal Soak's "outputs/temperature_final.vtu") resolves under
    ``<wd>/outputs/`` once — not a doubled ``outputs/outputs/`` path that
    never exists (which silently dropped the wired file hand-off)."""
    wd = tmp_path / "jobs" / "job_0000001"
    (wd / "outputs").mkdir(parents=True)
    (wd / "outputs" / "temperature_final.vtu").write_bytes(b"<vtu/>")

    resolved = _resolve_file_outputs(
        wd,
        [("temp", "outputs/temperature_final.vtu", None)],
    )
    primary = str(wd / "outputs" / "temperature_final.vtu")
    assert resolved == {"temp": (primary, [primary])}


def test_resolve_file_outputs_series_includes_primary(tmp_path: Path) -> None:
    wd = tmp_path / "jobs" / "job_0000001"
    (wd / "outputs").mkdir(parents=True)
    for i in range(3):
        (wd / "outputs" / f"temperature_{i}.npy").write_bytes(f"s{i}".encode())

    resolved = _resolve_file_outputs(
        wd,
        [("temps", "temperature_2.npy", "outputs/temperature_*.npy")],
    )
    primary, series = resolved["temps"]
    assert primary == str(wd / "outputs" / "temperature_2.npy")
    assert series == [
        str(wd / "outputs" / "temperature_0.npy"),
        str(wd / "outputs" / "temperature_1.npy"),
        str(wd / "outputs" / "temperature_2.npy"),
    ]


def test_wired_file_sources_only_for_file_inputs() -> None:
    descriptor = {
        "inputs": [
            {
                "name": "thermal_field",
                "type": "file",
                "downstream_input": True,
                "upstream_tool": "soak",
            },
            {
                "name": "scalar_load",
                "type": "float",
                "downstream_input": True,
                "upstream_tool": "soak",
            },
        ]
    }
    completed = {
        "soak": _MemberOutputs(
            values={"thermal_field": "/mnt/j/outputs/f.vtp", "scalar_load": 3.0},
            file_sources={"thermal_field": ["/mnt/j/outputs/f.vtp"]},
        )
    }
    sources = _wired_file_sources(
        descriptor=descriptor, member_aliases={"soak", "stress"}, completed=completed
    )
    # Only the file input gets a copy list; the scalar-wired input does not.
    assert sources == {"thermal_field": ["/mnt/j/outputs/f.vtp"]}


# --- advance integration ----------------------------------------------------


async def _seed_scenario(factory: Any, *, owner_id: int) -> int:
    async with session_scope(factory) as session:
        project = Project(name="fw-proj", description="", state="active", creator_user_id=owner_id)
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="fw-scn", state="active")
        session.add(scenario)
        await session.flush()
        return int(scenario.id)


async def _seed_file_chain(
    factory: Any, tool_service: ToolService, workflow_service: WorkflowService, *, admin_id: int
) -> str:
    """``soak`` emits a ``.vtp`` field; ``stress`` wires it as a file input."""
    descriptors = [
        make_tool_descriptor(
            alias="soak",
            downstream_tools=["stress"],
            outputs=[
                {
                    "name": "thermal_field",
                    "label": "Thermal field",
                    "type": "file",
                    "file_type": "field",
                    "filename": "thermal_field.vtp",
                    "field_format": "vtp",
                }
            ],
            teams=["World"],
        ),
        make_tool_descriptor(
            alias="stress",
            inputs=[
                {"name": "mesh_size", "label": "Mesh", "type": "float"},
                {
                    "name": "thermal_field",
                    "label": "Thermal field",
                    "type": "file",
                    "downstream_input": True,
                    "upstream_tool": "soak",
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
                alias="soak_stress",
                name="Soak→Stress",
                description="",
                tool_aliases=["soak", "stress"],
                teams=["World"],
            ),
        )
        return str(wf.alias)


@pytest.mark.asyncio
async def test_advance_wires_upstream_file_into_downstream(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    alias = await _seed_file_chain(factory, tool_service, workflow_service, admin_id=admin_id)
    scenario_id = await _seed_scenario(factory, owner_id=admin_id)

    # Capture what advance hands queue_for_runner (state flip + kwargs).
    captured: dict[str, Any] = {}

    async def _queue(_self: Any, session: Any, *, job_id: int, inputs: dict, **kw: Any) -> Job:
        job = await session.get(Job, job_id)
        job.state = "queued"
        job.inputs = dict(inputs)
        await session.flush()
        captured["inputs"] = dict(inputs)
        captured["wired_file_inputs"] = kw.get("wired_file_inputs")
        return job

    monkeypatch.setattr("matofx.hierarchy.service.JobService.queue_for_runner", _queue)

    # The upstream file output is conventionally None in outputs.json; the
    # resolver reads the path off disk, so the doc only needs to parse.
    async def _fake_outputs(_self: Any, _session: Any, *, job_id: int) -> Any:  # noqa: ARG001
        return SimpleNamespace(outputs=[], tool_alias="soak")

    monkeypatch.setattr("matofx.hierarchy.service.JobService.get_output_document", _fake_outputs)

    service = make_workflow_run_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        run = await service.start_headless_run(
            session,
            actor_user_id=admin_id,
            scenario_id=scenario_id,
            workflow_alias=alias,
            open_inputs={"stress": {"mesh_size": 2.0}},
        )
        run_id = run.id

    # Stand up a storage location + the upstream's retained working dir with
    # the produced .vtp on disk, then mark soak complete.
    mount = tmp_path / "mount"
    wd_rel = "jobs/dev/job_0000123"
    out_file = mount / wd_rel / "outputs" / "thermal_field.vtp"
    out_file.parent.mkdir(parents=True)
    out_file.write_bytes(b"<vtp>nodal temps</vtp>")

    async with session_scope(factory) as session:
        loc = StorageLocation(name="fsx", mount_path=str(mount), is_active=True)
        session.add(loc)
        await session.flush()
        soak = (
            await session.execute(
                select(Job).where(Job.workflow_run_id == run_id, Job.tool_alias == "soak")
            )
        ).scalar_one()
        soak.state = "complete"
        soak.storage_location_id = loc.id
        soak.working_dir_relative = wd_rel
        await session.flush()
        await service.advance(session, run_id=run_id, actor_user_id=admin_id)

    primary = str(out_file)
    # Downstream got the resolved upstream path as its file input value...
    assert captured["inputs"] == {"mesh_size": 2.0, "thermal_field": primary}
    # ...plus the per-field source list for the staging copy.
    assert captured["wired_file_inputs"] == {"thermal_field": [primary]}

    async with session_scope(factory) as session:
        stress = (
            await session.execute(
                select(Job).where(Job.workflow_run_id == run_id, Job.tool_alias == "stress")
            )
        ).scalar_one()
        assert stress.state == "queued"


@pytest.mark.asyncio
async def test_resolve_wired_files_for_member_manual_path(
    workflows_engine: AsyncEngine,
    tool_service: ToolService,
    workflow_service: WorkflowService,
    seed_roles: dict[str, int],
    tmp_path: Path,
) -> None:
    """The manual-run resolver picks up an upstream file output by path.

    ``queue_for_runner`` calls this for a workflow member queued via the manual
    tab-by-tab path (where the per-tab UI can only seed scalar wired values).
    Regression for "the wired file input arrives empty in a manual workflow run".
    """
    factory = make_session_factory(workflows_engine)
    admin_id = seed_roles["administrator"]
    alias = await _seed_file_chain(factory, tool_service, workflow_service, admin_id=admin_id)
    scenario_id = await _seed_scenario(factory, owner_id=admin_id)

    service = make_workflow_run_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        run = await service.start_run(  # manual run — children stay 'created'
            session, actor_user_id=admin_id, scenario_id=scenario_id, workflow_alias=alias
        )
        run_id = run.id

    mount = tmp_path / "mount"
    wd_rel = "jobs/dev/job_0000123"
    out_file = mount / wd_rel / "outputs" / "thermal_field.vtp"
    out_file.parent.mkdir(parents=True)
    out_file.write_bytes(b"<vtp>nodal temps</vtp>")

    async with session_scope(factory) as session:
        loc = StorageLocation(name="fsx", mount_path=str(mount), is_active=True)
        session.add(loc)
        await session.flush()
        soak = (
            await session.execute(
                select(Job).where(Job.workflow_run_id == run_id, Job.tool_alias == "soak")
            )
        ).scalar_one()
        soak.state = "complete"
        soak.storage_location_id = loc.id
        soak.working_dir_relative = wd_rel
        await session.flush()

        stress = (
            await session.execute(
                select(Job).where(Job.workflow_run_id == run_id, Job.tool_alias == "stress")
            )
        ).scalar_one()
        primaries, sources = await service.resolve_wired_files_for_member(session, stress)

    primary = str(out_file)
    assert primaries == {"thermal_field": primary}
    assert sources == {"thermal_field": [primary]}


@pytest.mark.asyncio
async def test_resolve_wired_files_for_non_member_is_empty(
    workflows_engine: AsyncEngine,
    seed_roles: dict[str, int],
) -> None:
    """A standalone (non-workflow) job resolves to nothing — the queue path
    must skip the workflow logic entirely for ordinary jobs."""
    factory = make_session_factory(workflows_engine)
    service = make_workflow_run_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        job = Job(
            scenario_id=1,
            name="standalone",
            tool_alias="x",
            tool_version_id=1,
            state="created",
            inputs={},
            created_by_user_id=seed_roles["administrator"],
        )
        primaries, sources = await service.resolve_wired_files_for_member(session, job)
    assert primaries == {}
    assert sources == {}
