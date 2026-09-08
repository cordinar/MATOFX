"""Ownership transfer (ADR-0002 semantics) + optimistic locking."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import (
    AccessDeniedError,
    ConcurrentModificationError,
    NotFoundError,
    ProjectAccessLevel,
    ProjectService,
)


async def _project(factory, project_service: ProjectService, creator_id: int) -> int:
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        p = await project_service.create(session, actor_user_id=creator_id, name="P")
        return p.id


@pytest.mark.asyncio
async def test_creator_transfer_grants_successor_owner_keeps_creator(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)

    async with session_scope(factory) as session:
        await project_service.transfer_ownership(
            session, project_id=pid, actor_user_id=alice_id, new_owner_user_id=bob_id
        )

    async with session_scope(factory) as session:
        # Successor now an Owner.
        assert (
            await project_service.access_for(session, project_id=pid, actor_user_id=bob_id)
        ) == ProjectAccessLevel.OWNER
        # Creator is frozen + still authoritative.
        proj = await project_service.get(session, project_id=pid, actor_user_id=alice_id)
        assert proj.creator_user_id == alice_id
        assert (
            await project_service.access_for(session, project_id=pid, actor_user_id=alice_id)
        ) == ProjectAccessLevel.OWNER


@pytest.mark.asyncio
async def test_transfer_revokes_other_non_creator_owners_only(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    user_service,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)
    async with session_scope(factory) as session:
        carol = await user_service.create_user(
            session,
            username="carol",
            email="carol@example.com",
            password="carolpassword",
            full_name="Carol",
        )
        dave = await user_service.create_user(
            session,
            username="dave",
            email="dave@example.com",
            password="davepassword",
            full_name="Dave",
        )
        carol_id, dave_id = carol.id, dave.id

    async with session_scope(factory) as session:
        await project_service.add_acl_entry(
            session, project_id=pid, user_id=carol_id, role="owner", actor_user_id=alice_id
        )
        await project_service.add_acl_entry(
            session, project_id=pid, user_id=dave_id, role="viewer", actor_user_id=alice_id
        )

    async with session_scope(factory) as session:
        await project_service.transfer_ownership(
            session, project_id=pid, actor_user_id=alice_id, new_owner_user_id=bob_id
        )

    async with session_scope(factory) as session:
        entries = {
            e.user_id: e.role
            for e in await project_service.list_acl(session, project_id=pid, actor_user_id=alice_id)
        }
    assert entries == {bob_id: "owner", dave_id: "viewer"}  # carol's owner revoked


@pytest.mark.asyncio
async def test_super_user_can_transfer_anyones_project(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    admin_id, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)
    async with session_scope(factory) as session:
        await project_service.transfer_ownership(
            session, project_id=pid, actor_user_id=admin_id, new_owner_user_id=bob_id
        )
    async with session_scope(factory) as session:
        assert (
            await project_service.access_for(session, project_id=pid, actor_user_id=bob_id)
        ) == ProjectAccessLevel.OWNER


@pytest.mark.asyncio
async def test_random_user_cannot_transfer(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)
    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await project_service.transfer_ownership(
                session, project_id=pid, actor_user_id=bob_id, new_owner_user_id=bob_id
            )


@pytest.mark.asyncio
async def test_admin_without_transfer_perm_cannot_transfer(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    user_service,
    seed_users: tuple[int, int, int],
) -> None:
    """administrator has projects.see_all but not transfer_ownership.

    see_all alone must NOT authorise transfer (ADR-0002 keeps the
    admin transfer path behind the dedicated permission).
    """
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)
    async with session_scope(factory) as session:
        adm2 = await user_service.create_user(
            session,
            username="adm2",
            email="adm2@example.com",
            password="adminpassword",
            full_name="Admin Two",
            role="administrator",
        )
        adm2_id = adm2.id

    async with session_scope(factory) as session:
        with pytest.raises(AccessDeniedError):
            await project_service.transfer_ownership(
                session, project_id=pid, actor_user_id=adm2_id, new_owner_user_id=adm2_id
            )


@pytest.mark.asyncio
async def test_optimistic_lock_rejects_stale_update(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)

    async with session_scope(factory) as session:
        loaded = await project_service.get(session, project_id=pid, actor_user_id=alice_id)
        seen_version = loaded.version

    async with session_scope(factory) as session:
        await project_service.update(
            session,
            project_id=pid,
            actor_user_id=alice_id,
            name="rename-1",
            expected_version=seen_version,
        )

    async with session_scope(factory) as session:
        with pytest.raises(ConcurrentModificationError):
            await project_service.update(
                session,
                project_id=pid,
                actor_user_id=alice_id,
                name="rename-2",
                expected_version=seen_version,
            )


@pytest.mark.asyncio
async def test_update_without_expected_version_is_last_writer_wins(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)

    async with session_scope(factory) as session:
        await project_service.update(session, project_id=pid, actor_user_id=alice_id, name="v1")
    async with session_scope(factory) as session:
        await project_service.update(session, project_id=pid, actor_user_id=alice_id, name="v2")
    async with session_scope(factory) as session:
        loaded = await project_service.get(session, project_id=pid, actor_user_id=alice_id)
    assert loaded.name == "v2"
    assert loaded.version >= 3
