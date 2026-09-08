"""Tests for ScenarioService."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import (
    DuplicateNameError,
    NotFoundError,
    ProjectService,
    ScenarioService,
)


async def _make_project(factory, project_service: ProjectService, owner_id: int) -> int:
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=owner_id, name="P1")
        return project.id


@pytest.mark.asyncio
async def test_create_and_list_scenarios(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    project_id = await _make_project(factory, project_service, alice_id)

    async with session_scope(factory) as session:
        await scenario_service.create(
            session, actor_user_id=alice_id, project_id=project_id, name="S1"
        )
        await scenario_service.create(
            session, actor_user_id=alice_id, project_id=project_id, name="S2"
        )

    async with session_scope(factory) as session:
        scenarios = await scenario_service.list_for_project(
            session, project_id=project_id, actor_user_id=alice_id
        )
    assert {s.name for s in scenarios} == {"S1", "S2"}


@pytest.mark.asyncio
async def test_other_user_cannot_create_scenario_in_someone_elses_project(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    project_id = await _make_project(factory, project_service, alice_id)

    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await scenario_service.create(
                session, actor_user_id=bob_id, project_id=project_id, name="X"
            )


@pytest.mark.asyncio
async def test_duplicate_scenario_name_in_project_rejected(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    project_id = await _make_project(factory, project_service, alice_id)

    async with session_scope(factory) as session:
        await scenario_service.create(
            session, actor_user_id=alice_id, project_id=project_id, name="Same"
        )
    async with session_scope(factory) as session:
        with pytest.raises(DuplicateNameError):
            await scenario_service.create(
                session, actor_user_id=alice_id, project_id=project_id, name="Same"
            )


@pytest.mark.asyncio
async def test_get_scenario_enforces_project_access(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    project_id = await _make_project(factory, project_service, alice_id)
    async with session_scope(factory) as session:
        scenario = await scenario_service.create(
            session, actor_user_id=alice_id, project_id=project_id, name="S"
        )
    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await scenario_service.get(session, scenario_id=scenario.id, actor_user_id=bob_id)


@pytest.mark.asyncio
async def test_super_user_can_list_others_scenarios(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    seed_users: tuple[int, int, int],
) -> None:
    admin_id, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    project_id = await _make_project(factory, project_service, alice_id)
    async with session_scope(factory) as session:
        await scenario_service.create(
            session, actor_user_id=alice_id, project_id=project_id, name="S"
        )
    async with session_scope(factory) as session:
        scenarios = await scenario_service.list_for_project(
            session, project_id=project_id, actor_user_id=admin_id
        )
    assert len(scenarios) == 1
