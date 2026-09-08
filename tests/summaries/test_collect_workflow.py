"""Slice 2 — the workflow-run fact collector against a real DB row.

Seeds a Workflow + run + child jobs (mixed states) and asserts the collected
facts: rolled-up state, per-member labels in tab order, the pass/fail tally,
and refusal on a non-terminal run.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from matofx.audit import AuditWriter
from matofx.config import Settings
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.summaries import NonTerminalResultError
from matofx.summaries.collectors import collect_workflow_facts
from matofx.tools.loader import install_from_path
from matofx.tools.orm import Tool, ToolVersion
from matofx.tools.service import make_tool_service
from matofx.workflows.orm import Workflow, WorkflowRun

pytestmark = pytest.mark.asyncio

_EXAMPLE_TOOLS = Path(__file__).resolve().parents[1].parent / "example_tools"
# Distinct tools per member — a workflow run enforces one tool per run.
_MEMBER_TOOLS = ("material_properties", "heat_sink", "colour_check")


async def _install_member_tools(
    factory: SessionFactory, audit: AuditWriter, *, actor_user_id: int
) -> list[str]:
    """Install + release the example tools used as distinct workflow members."""
    tool_service = make_tool_service(audit)
    aliases: list[str] = []
    async with session_scope(factory) as session:
        for name in _MEMBER_TOOLS:
            existing = (
                await session.execute(select(Tool).where(Tool.alias == name))
            ).scalar_one_or_none()
            if existing is not None:
                aliases.append(name)
                continue
            installed = await install_from_path(
                session,
                tool_service,
                actor_user_id=actor_user_id,
                bundle_dir=_EXAMPLE_TOOLS / name,
            )
            for new_state in ("test", "released"):
                await tool_service.promote_state(
                    session,
                    actor_user_id=actor_user_id,
                    alias=installed.alias,
                    new_state=new_state,
                )
            aliases.append(installed.alias)
    return aliases


async def _seed_run(
    factory: SessionFactory,
    seeded: dict[str, Any],
    audit: AuditWriter,
    *,
    child_states: list[str],
) -> int:
    aliases = await _install_member_tools(factory, audit, actor_user_id=seeded["user_id"])
    async with session_scope(factory) as session:
        version_by_alias: dict[str, int] = {}
        for alias in aliases:
            version_by_alias[alias] = int(
                (
                    await session.execute(
                        select(ToolVersion.id)
                        .where(ToolVersion.alias == alias)
                        .order_by(ToolVersion.id.desc())
                        .limit(1)
                    )
                ).scalar_one()
            )
        project = Project(name="WF summary project", creator_user_id=seeded["user_id"])
        session.add(project)
        await session.flush()
        scenario = Scenario(project_id=project.id, name="WF summary scenario")
        session.add(scenario)
        await session.flush()
        workflow = Workflow(
            alias="wf_summary",
            name="Thermal chain",
            version="1.0.0",
            descriptor={},
            installed_by_user_id=seeded["user_id"],
        )
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(
            workflow_id=workflow.id,
            workflow_version_snapshot="1.0.0",
            scenario_id=scenario.id,
            name="Thermal chain run",
            submitted_by_user_id=seeded["user_id"],
        )
        session.add(run)
        await session.flush()
        base = _dt.datetime.now(tz=_dt.UTC)
        for position, child_state in enumerate(child_states, start=1):
            done = child_state == "complete"
            member_alias = aliases[position - 1]
            session.add(
                Job(
                    scenario_id=scenario.id,
                    workflow_run_id=run.id,
                    workflow_position=position,
                    name=f"step-{position}",
                    tool_alias=member_alias,
                    tool_version_id=version_by_alias[member_alias],
                    state=child_state,
                    inputs={},
                    created_by_user_id=seeded["user_id"],
                    created_at=base - _dt.timedelta(minutes=2),
                    started_at=base - _dt.timedelta(minutes=1) if done else None,
                    completed_at=base if done else None,
                )
            )
        await session.flush()
        return int(run.id)


async def _facts_for(factory: SessionFactory, run_id: int, settings: Settings):
    async with session_scope(factory) as session:
        run = await session.get(WorkflowRun, run_id)
        assert run is not None
        children = list(
            (
                await session.execute(
                    select(Job).where(Job.workflow_run_id == run_id).order_by(Job.workflow_position)
                )
            ).scalars()
        )
        return await collect_workflow_facts(session, run=run, children=children, settings=settings)


async def test_complete_run_facts(
    api_factory: SessionFactory,
    seeded: dict[str, Any],
    api_audit: AuditWriter,
    api_settings: Settings,
) -> None:
    run_id = await _seed_run(api_factory, seeded, api_audit, child_states=["complete", "complete"])
    facts = await _facts_for(api_factory, run_id, api_settings)
    assert facts.surface_kind == "workflow_run"
    assert facts.state == "complete"
    assert facts.details["member_count"] == 2
    assert facts.details["completed_count"] == 2
    # Each member carries the tool's display label, not its alias.
    tool_name = facts.details["members"][0]["label"]
    assert tool_name
    assert tool_name != _MEMBER_TOOLS[0]


async def test_failed_run_facts_flag_first_failure(
    api_factory: SessionFactory,
    seeded: dict[str, Any],
    api_audit: AuditWriter,
    api_settings: Settings,
) -> None:
    run_id = await _seed_run(
        api_factory, seeded, api_audit, child_states=["complete", "failed", "created"]
    )
    facts = await _facts_for(api_factory, run_id, api_settings)
    assert facts.state == "failed"
    assert facts.details["first_failed_position"] == 2
    assert facts.details["failed_count"] == 1


async def test_non_terminal_run_is_refused(
    api_factory: SessionFactory,
    seeded: dict[str, Any],
    api_audit: AuditWriter,
    api_settings: Settings,
) -> None:
    run_id = await _seed_run(api_factory, seeded, api_audit, child_states=["complete", "running"])
    with pytest.raises(NonTerminalResultError):
        await _facts_for(api_factory, run_id, api_settings)


async def test_tool_label_resolves_to_display_name(
    api_factory: SessionFactory,
    seeded: dict[str, Any],
    api_audit: AuditWriter,
    api_settings: Settings,
) -> None:
    run_id = await _seed_run(api_factory, seeded, api_audit, child_states=["complete"])
    async with session_scope(api_factory) as session:
        tool = (
            await session.execute(select(Tool).where(Tool.alias == _MEMBER_TOOLS[0]))
        ).scalar_one()
        expected = tool.name
    facts = await _facts_for(api_factory, run_id, api_settings)
    assert facts.details["members"][0]["label"] == expected
