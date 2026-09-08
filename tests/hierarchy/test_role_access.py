"""Hierarchy access checks across the built-in user roles (ADR-0002)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import NotFoundError, ProjectService
from matofx.users import UserRole, UserService


async def _make_user(
    factory,
    user_service: UserService,
    *,
    username: str,
    role: UserRole,
) -> int:
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username=username,
            email=f"{username}@example.com",
            password="securepassword1",
            full_name=username.title(),
            role=role,
        )
        return user.id


@pytest.mark.asyncio
async def test_administrator_sees_other_users_projects(
    hierarchy_engine: AsyncEngine,
    user_service: UserService,
    project_service: ProjectService,
) -> None:
    factory = make_session_factory(hierarchy_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")

    async with session_scope(factory) as session:
        await project_service.create(session, actor_user_id=alice_id, name="A")

    async with session_scope(factory) as session:
        seen = await project_service.list_for_user(session, actor_user_id=admin_id)
    assert {p.name for p in seen} == {"A"}


@pytest.mark.asyncio
async def test_beta_tester_sees_only_their_own(
    hierarchy_engine: AsyncEngine,
    user_service: UserService,
    project_service: ProjectService,
) -> None:
    factory = make_session_factory(hierarchy_engine)
    beta_id = await _make_user(factory, user_service, username="beth", role="beta_tester")
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")

    async with session_scope(factory) as session:
        await project_service.create(session, actor_user_id=alice_id, name="A")
        await project_service.create(session, actor_user_id=beta_id, name="B")

    async with session_scope(factory) as session:
        seen = await project_service.list_for_user(session, actor_user_id=beta_id)
    assert {p.name for p in seen} == {"B"}


@pytest.mark.asyncio
async def test_administrator_cannot_create_project_for_someone_else(
    hierarchy_engine: AsyncEngine,
    user_service: UserService,
    project_service: ProjectService,
) -> None:
    """Creating a project for another user is super-user-only, even for admins."""
    from matofx.hierarchy import AccessDeniedError  # noqa: PLC0415

    factory = make_session_factory(hierarchy_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")

    async with session_scope(factory) as session:
        with pytest.raises(AccessDeniedError):
            await project_service.create(
                session,
                actor_user_id=admin_id,
                name="X",
                creator_user_id=alice_id,
            )


@pytest.mark.asyncio
async def test_super_user_can_create_project_for_someone_else(
    hierarchy_engine: AsyncEngine,
    user_service: UserService,
    project_service: ProjectService,
) -> None:
    factory = make_session_factory(hierarchy_engine)
    root_id = await _make_user(factory, user_service, username="root", role="super_user")
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")

    async with session_scope(factory) as session:
        project = await project_service.create(
            session,
            actor_user_id=root_id,
            name="X",
            creator_user_id=alice_id,
        )
    assert project.creator_user_id == alice_id


@pytest.mark.asyncio
async def test_unknown_role_value_rejected_at_creation(
    hierarchy_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    from matofx.users import RoleNotFoundError  # noqa: PLC0415

    factory = make_session_factory(hierarchy_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        with pytest.raises(RoleNotFoundError):
            await user_service.create_user(
                session,
                username="bogus",
                email="bogus@example.com",
                password="securepassword1",
                full_name="Bogus",
                role="hacker",
            )


@pytest.mark.asyncio
async def test_regular_user_cannot_see_admin_project(
    hierarchy_engine: AsyncEngine,
    user_service: UserService,
    project_service: ProjectService,
) -> None:
    factory = make_session_factory(hierarchy_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")

    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=admin_id, name="A")

    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await project_service.get(session, project_id=project.id, actor_user_id=alice_id)
