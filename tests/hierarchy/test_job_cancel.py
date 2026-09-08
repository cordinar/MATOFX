"""Tests for ``JobService.request_cancel`` (RBAC + state guards)."""

from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import (
    AccessDeniedError,
    Job,
    JobAlreadyTerminalError,
    JobService,
    NotFoundError,
    ProjectService,
    ScenarioService,
)
from matofx.users import TeamService


async def _make_scenario(
    factory,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    owner_id: int,
) -> int:
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=owner_id, name="P")
        scenario = await scenario_service.create(
            session, actor_user_id=owner_id, project_id=project.id, name="S"
        )
        return scenario.id


async def _make_team(factory, team_service: TeamService, admin_id: int, name: str) -> int:
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        t = await team_service.create_team(session, actor_user_id=admin_id, name=name)
        return t.id


@pytest.fixture
def team_service(audit_writer) -> TeamService:
    from matofx.users import make_team_service  # noqa: PLC0415

    return make_team_service(audit_writer)


@pytest.mark.asyncio
async def test_owner_can_cancel_their_own_job(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    async with session_scope(factory) as session:
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="j",
            tool_alias="demo",
        )

    async with session_scope(factory) as session:
        cancelled = await job_service.request_cancel(session, job_id=job.id, actor_user_id=alice_id)
        assert cancelled.cancel_requested_at is not None
        assert cancelled.cancel_requested_by_user_id == alice_id


@pytest.mark.asyncio
async def test_team_edit_member_can_cancel(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    team_service: TeamService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    admin_id, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    team_id = await _make_team(factory, team_service, admin_id, "engineering")
    async with session_scope(factory) as session:
        await team_service.add_user_to_team(
            session, actor_user_id=admin_id, user_id=bob_id, team_id=team_id
        )

    async with session_scope(factory) as session:
        scenario = await scenario_service.get(
            session, scenario_id=scenario_id, actor_user_id=alice_id
        )
        await project_service.add_acl_entry(
            session,
            project_id=scenario.project_id,
            user_id=bob_id,
            role="member",
            actor_user_id=alice_id,
        )

    async with session_scope(factory) as session:
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="j",
            tool_alias="demo",
        )

    async with session_scope(factory) as session:
        cancelled = await job_service.request_cancel(session, job_id=job.id, actor_user_id=bob_id)
        assert cancelled.cancel_requested_by_user_id == bob_id


@pytest.mark.asyncio
async def test_team_view_member_cannot_cancel(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    team_service: TeamService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    admin_id, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    team_id = await _make_team(factory, team_service, admin_id, "engineering")
    async with session_scope(factory) as session:
        await team_service.add_user_to_team(
            session, actor_user_id=admin_id, user_id=bob_id, team_id=team_id
        )

    async with session_scope(factory) as session:
        scenario = await scenario_service.get(
            session, scenario_id=scenario_id, actor_user_id=alice_id
        )
        await project_service.add_acl_entry(
            session,
            project_id=scenario.project_id,
            user_id=bob_id,
            role="viewer",
            actor_user_id=alice_id,
        )

    async with session_scope(factory) as session:
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="j",
            tool_alias="demo",
        )

    async with session_scope(factory) as session:
        with pytest.raises(AccessDeniedError):
            await job_service.request_cancel(session, job_id=job.id, actor_user_id=bob_id)


@pytest.mark.asyncio
async def test_unrelated_user_gets_not_found(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    """A user with no project access at all sees NotFound, not AccessDenied."""
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    async with session_scope(factory) as session:
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="j",
            tool_alias="demo",
        )

    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await job_service.request_cancel(session, job_id=job.id, actor_user_id=bob_id)


@pytest.mark.asyncio
async def test_super_user_with_cancel_any_can_cancel_anyones_job(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    """``jobs.cancel_any`` (super_user-only) bypasses the project ladder."""
    admin_id, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    async with session_scope(factory) as session:
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="j",
            tool_alias="demo",
        )

    async with session_scope(factory) as session:
        cancelled = await job_service.request_cancel(session, job_id=job.id, actor_user_id=admin_id)
        assert cancelled.cancel_requested_by_user_id == admin_id


@pytest.mark.asyncio
async def test_cancel_idempotent_keeps_first_request(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    admin_id, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    async with session_scope(factory) as session:
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="j",
            tool_alias="demo",
        )

    async with session_scope(factory) as session:
        first = await job_service.request_cancel(session, job_id=job.id, actor_user_id=alice_id)
        first_at = first.cancel_requested_at
        first_by = first.cancel_requested_by_user_id
        assert first_at is not None
        assert first_by == alice_id

    async with session_scope(factory) as session:
        second = await job_service.request_cancel(session, job_id=job.id, actor_user_id=admin_id)
        # Original request preserved; super-user re-call is a no-op.
        # SQLite drops tzinfo on round-trip, so compare the naive form.
        assert second.cancel_requested_at is not None
        assert second.cancel_requested_at.replace(tzinfo=None) == first_at.replace(tzinfo=None)
        assert second.cancel_requested_by_user_id == first_by


@pytest.mark.asyncio
async def test_cancel_refused_on_complete_job(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    async with session_scope(factory) as session:
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="j",
            tool_alias="demo",
        )

    # Drive job through running → complete to land in a terminal state.
    async with session_scope(factory) as session:
        await job_service._transition_state(
            session, job_id=job.id, actor_user_id=alice_id, new_state="running"
        )
    async with session_scope(factory) as session:
        await job_service._transition_state(
            session, job_id=job.id, actor_user_id=alice_id, new_state="complete"
        )

    async with session_scope(factory) as session:
        with pytest.raises(JobAlreadyTerminalError) as ei:
            await job_service.request_cancel(session, job_id=job.id, actor_user_id=alice_id)
        assert ei.value.state == "complete"


@pytest.mark.asyncio
async def test_cancel_refused_on_failed_job(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    async with session_scope(factory) as session:
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="j",
            tool_alias="demo",
        )

    async with session_scope(factory) as session:
        await job_service._transition_state(
            session, job_id=job.id, actor_user_id=alice_id, new_state="running"
        )
    async with session_scope(factory) as session:
        await job_service._transition_state(
            session, job_id=job.id, actor_user_id=alice_id, new_state="failed"
        )

    async with session_scope(factory) as session:
        with pytest.raises(JobAlreadyTerminalError):
            await job_service.request_cancel(session, job_id=job.id, actor_user_id=alice_id)


@pytest.mark.asyncio
async def test_cancel_records_audit_event(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.audit.orm import AuditLog  # noqa: PLC0415

    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    async with session_scope(factory) as session:
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="j",
            tool_alias="demo",
        )

    async with session_scope(factory) as session:
        await job_service.request_cancel(session, job_id=job.id, actor_user_id=alice_id)

    async with session_scope(factory) as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "hierarchy.job.cancel_requested")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].resource_id == str(job.id)
        assert rows[0].actor == f"user:{alice_id}"


@pytest.mark.asyncio
async def test_cancel_sets_recent_timestamp(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    async with session_scope(factory) as session:
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="j",
            tool_alias="demo",
        )

    before = _dt.datetime.now(tz=_dt.UTC)
    async with session_scope(factory) as session:
        cancelled = await job_service.request_cancel(session, job_id=job.id, actor_user_id=alice_id)
    after = _dt.datetime.now(tz=_dt.UTC)
    assert cancelled.cancel_requested_at is not None
    # Round-trip through SQLite drops timezone, so re-attach UTC for the
    # comparison; the column stores UTC by convention.
    ts = cancelled.cancel_requested_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.UTC)
    assert before <= ts <= after


@pytest.mark.asyncio
async def test_cancel_refuses_unknown_job(
    hierarchy_engine: AsyncEngine,
    job_service: JobService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await job_service.request_cancel(session, job_id=999999, actor_user_id=alice_id)


@pytest.mark.asyncio
async def test_cancel_persisted_to_row(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures `tool.alias='demo'` row exists for FK
) -> None:
    """Reload the row in a fresh session to confirm the cancel fields persisted."""
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    scenario_id = await _make_scenario(factory, project_service, scenario_service, alice_id)
    async with session_scope(factory) as session:
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario_id,
            name="j",
            tool_alias="demo",
        )

    async with session_scope(factory) as session:
        await job_service.request_cancel(session, job_id=job.id, actor_user_id=alice_id)

    async with session_scope(factory) as session:
        reloaded = await session.get(Job, job.id)
        assert reloaded is not None
        assert reloaded.cancel_requested_at is not None
        assert reloaded.cancel_requested_by_user_id == alice_id
