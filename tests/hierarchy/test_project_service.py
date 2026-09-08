"""Tests for ProjectService — create / read / update / archive (ADR-0002)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import (
    AccessDeniedError,
    DuplicateNameError,
    InvalidStateError,
    NotFoundError,
    ProjectService,
)


@pytest.mark.asyncio
async def test_create_and_get_round_trip(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        project = await project_service.create(
            session, actor_user_id=alice_id, name="Alpha", description="first"
        )

    async with session_scope(factory) as session:
        loaded = await project_service.get(session, project_id=project.id, actor_user_id=alice_id)
    assert loaded.name == "Alpha"
    assert loaded.creator_user_id == alice_id
    assert loaded.state == "active"


@pytest.mark.asyncio
async def test_creator_is_immutable_across_update(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="P")
    async with session_scope(factory) as session:
        await project_service.update(
            session, project_id=project.id, actor_user_id=alice_id, name="P2"
        )
    async with session_scope(factory) as session:
        loaded = await project_service.get(session, project_id=project.id, actor_user_id=alice_id)
    assert loaded.creator_user_id == alice_id  # never rewritten


@pytest.mark.asyncio
async def test_list_for_user_returns_only_visible(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        await project_service.create(session, actor_user_id=alice_id, name="A1")
        await project_service.create(session, actor_user_id=alice_id, name="A2")
        await project_service.create(session, actor_user_id=bob_id, name="B1")

    async with session_scope(factory) as session:
        alices = await project_service.list_for_user(session, actor_user_id=alice_id)
        bobs = await project_service.list_for_user(session, actor_user_id=bob_id)
    assert {p.name for p in alices} == {"A1", "A2"}
    assert {p.name for p in bobs} == {"B1"}


@pytest.mark.asyncio
async def test_super_user_sees_all(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    admin_id, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        await project_service.create(session, actor_user_id=alice_id, name="A")
        await project_service.create(session, actor_user_id=bob_id, name="B")

    async with session_scope(factory) as session:
        seen = await project_service.list_for_user(session, actor_user_id=admin_id)
    assert {p.name for p in seen} == {"A", "B"}


@pytest.mark.asyncio
async def test_other_user_cannot_see_project(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="A")

    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await project_service.get(session, project_id=project.id, actor_user_id=bob_id)


@pytest.mark.asyncio
async def test_duplicate_name_for_same_creator_rejected(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        await project_service.create(session, actor_user_id=alice_id, name="Same")
    async with session_scope(factory) as session:
        with pytest.raises(DuplicateNameError):
            await project_service.create(session, actor_user_id=alice_id, name="Same")


@pytest.mark.asyncio
async def test_same_name_for_different_creators_allowed(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        await project_service.create(session, actor_user_id=alice_id, name="Shared")
    async with session_scope(factory) as session:
        await project_service.create(session, actor_user_id=bob_id, name="Shared")


@pytest.mark.asyncio
async def test_creator_can_update_fields(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        project = await project_service.create(
            session, actor_user_id=alice_id, name="Original", description="x"
        )
    async with session_scope(factory) as session:
        updated = await project_service.update(
            session,
            project_id=project.id,
            actor_user_id=alice_id,
            name="Renamed",
            description="y",
        )
    assert updated.name == "Renamed"
    assert updated.description == "y"


@pytest.mark.asyncio
async def test_non_member_cannot_update(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="A")
    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await project_service.update(
                session, project_id=project.id, actor_user_id=bob_id, name="X"
            )


@pytest.mark.asyncio
async def test_create_for_other_user_rejected_when_not_super(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        with pytest.raises(AccessDeniedError):
            await project_service.create(
                session, actor_user_id=alice_id, name="Foo", creator_user_id=bob_id
            )


@pytest.mark.asyncio
async def test_archive_hides_from_default_list(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="A")

    async with session_scope(factory) as session:
        archived = await project_service.set_state(
            session, project_id=project.id, actor_user_id=alice_id, state="archived"
        )
        assert archived.state == "archived"
        assert archived.state_changed_by_user_id == alice_id

    async with session_scope(factory) as session:
        default = await project_service.list_for_user(session, actor_user_id=alice_id)
        assert default == []
        with_archived = await project_service.list_for_user(
            session, actor_user_id=alice_id, include_archived=True
        )
        assert {p.name for p in with_archived} == {"A"}


@pytest.mark.asyncio
async def test_archive_rejects_invalid_targets(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="A")

    async with session_scope(factory) as session:
        with pytest.raises(InvalidStateError):
            await project_service.set_state(
                session, project_id=project.id, actor_user_id=alice_id, state="active"
            )
    async with session_scope(factory) as session:
        await project_service.set_state(
            session, project_id=project.id, actor_user_id=alice_id, state="archived"
        )
    async with session_scope(factory) as session:
        with pytest.raises(InvalidStateError):
            await project_service.set_state(
                session, project_id=project.id, actor_user_id=alice_id, state="archived"
            )
