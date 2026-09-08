"""Per-user ACL tests (ADR-0002): owner / member / viewer + creator.

(Filename retained from the superseded team-ACL suite; the model is
now the per-user ``project_acl`` — teams are not ACL principals.)
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import (
    AccessDeniedError,
    CreatorImmutableError,
    InvalidRoleError,
    NotFoundError,
    ProjectAccessLevel,
    ProjectService,
    UserAlreadyGrantedError,
    UserNotGrantedError,
)


async def _project(factory, project_service: ProjectService, creator_id: int) -> int:
    async with session_scope(factory) as session:
        p = await project_service.create(session, actor_user_id=creator_id, name="P")
        return p.id


@pytest.mark.asyncio
async def test_viewer_can_read_not_write(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)

    async with session_scope(factory) as session:
        await project_service.add_acl_entry(
            session, project_id=pid, user_id=bob_id, role="viewer", actor_user_id=alice_id
        )
    async with session_scope(factory) as session:
        level = await project_service.access_for(
            session, project_id=pid, actor_user_id=bob_id
        )
        assert level == ProjectAccessLevel.VIEW
        await project_service.get(session, project_id=pid, actor_user_id=bob_id)
        with pytest.raises(AccessDeniedError):
            await project_service.update(
                session, project_id=pid, actor_user_id=bob_id, name="x"
            )


@pytest.mark.asyncio
async def test_member_is_edit_tier_but_not_owner(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)

    async with session_scope(factory) as session:
        await project_service.add_acl_entry(
            session, project_id=pid, user_id=bob_id, role="member", actor_user_id=alice_id
        )
    async with session_scope(factory) as session:
        assert (
            await project_service.access_for(session, project_id=pid, actor_user_id=bob_id)
        ) == ProjectAccessLevel.EDIT
        # Member cannot edit the project itself (OWNER-gated)…
        with pytest.raises(AccessDeniedError):
            await project_service.update(
                session, project_id=pid, actor_user_id=bob_id, name="x"
            )
        # …nor manage the ACL.
        with pytest.raises(AccessDeniedError):
            await project_service.add_acl_entry(
                session, project_id=pid, user_id=alice_id, role="viewer",
                actor_user_id=bob_id,
            )


@pytest.mark.asyncio
async def test_owner_can_edit_and_manage_acl(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)

    async with session_scope(factory) as session:
        await project_service.add_acl_entry(
            session, project_id=pid, user_id=bob_id, role="owner", actor_user_id=alice_id
        )
    async with session_scope(factory) as session:
        await project_service.update(
            session, project_id=pid, actor_user_id=bob_id, name="renamed"
        )


@pytest.mark.asyncio
async def test_outsider_gets_not_found(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)
    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await project_service.get(session, project_id=pid, actor_user_id=bob_id)


@pytest.mark.asyncio
async def test_creator_cannot_be_acl_target(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, _ = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)
    async with session_scope(factory) as session:
        with pytest.raises(CreatorImmutableError):
            await project_service.add_acl_entry(
                session, project_id=pid, user_id=alice_id, role="viewer",
                actor_user_id=alice_id,
            )


@pytest.mark.asyncio
async def test_remove_revokes_access(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)
    async with session_scope(factory) as session:
        await project_service.add_acl_entry(
            session, project_id=pid, user_id=bob_id, role="member", actor_user_id=alice_id
        )
    async with session_scope(factory) as session:
        await project_service.remove_acl_entry(
            session, project_id=pid, user_id=bob_id, actor_user_id=alice_id
        )
    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await project_service.get(session, project_id=pid, actor_user_id=bob_id)


@pytest.mark.asyncio
async def test_set_acl_replaces_grants(
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
        carol_id = carol.id

    async with session_scope(factory) as session:
        await project_service.add_acl_entry(
            session, project_id=pid, user_id=bob_id, role="viewer", actor_user_id=alice_id
        )
    async with session_scope(factory) as session:
        await project_service.set_acl(
            session,
            project_id=pid,
            grants=[(bob_id, "owner"), (carol_id, "member")],
            actor_user_id=alice_id,
        )
    async with session_scope(factory) as session:
        entries = await project_service.list_acl(
            session, project_id=pid, actor_user_id=alice_id
        )
    assert {(e.user_id, e.role) for e in entries} == {
        (bob_id, "owner"),
        (carol_id, "member"),
    }


@pytest.mark.asyncio
async def test_invalid_role_rejected(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)
    async with session_scope(factory) as session:
        with pytest.raises(InvalidRoleError):
            await project_service.add_acl_entry(
                session, project_id=pid, user_id=bob_id, role="god",
                actor_user_id=alice_id,
            )


@pytest.mark.asyncio
async def test_duplicate_and_missing_grant_errors(
    hierarchy_engine: AsyncEngine,
    project_service: ProjectService,
    seed_users: tuple[int, int, int],
) -> None:
    _, alice_id, bob_id = seed_users
    factory = make_session_factory(hierarchy_engine)
    pid = await _project(factory, project_service, alice_id)
    async with session_scope(factory) as session:
        await project_service.add_acl_entry(
            session, project_id=pid, user_id=bob_id, role="viewer", actor_user_id=alice_id
        )
    async with session_scope(factory) as session:
        with pytest.raises(UserAlreadyGrantedError):
            await project_service.add_acl_entry(
                session, project_id=pid, user_id=bob_id, role="viewer",
                actor_user_id=alice_id,
            )
    async with session_scope(factory) as session:
        with pytest.raises(UserNotGrantedError):
            await project_service.remove_acl_entry(
                session, project_id=pid, user_id=99999, actor_user_id=alice_id
            )
