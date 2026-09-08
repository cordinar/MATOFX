"""Tests for the legacy-parity additions: teams, approver flag, lifecycle dates."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.users import (
    AlreadyInTeamError,
    DuplicateTeamError,
    NotInTeamError,
    Permission,
    PermissionDeniedError,
    TeamInUseError,
    TeamNotFoundError,
    TeamService,
    UserRole,
    UserService,
    make_team_service,
    user_has_permission,
)


async def _make_user(
    factory,
    user_service: UserService,
    *,
    username: str,
    role: UserRole,
    is_approver: bool = False,
    first_name: str = "",
    last_name: str = "",
) -> int:
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username=username,
            email=f"{username}@example.com",
            password="securepassword1",
            first_name=first_name or username.title(),
            last_name=last_name,
            role=role,
            is_approver=is_approver,
        )
        return user.id


@pytest.fixture
def team_service(audit_writer: AuditWriter) -> TeamService:
    return make_team_service(audit_writer)


# --- First / last name + full name -------------------------------------


@pytest.mark.asyncio
async def test_first_last_name_recorded_at_create(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="alice",
            email="alice@example.com",
            password="securepassword1",
            first_name="Alice",
            last_name="Example",
        )
    assert user.first_name == "Alice"
    assert user.last_name == "Example"
    assert user.full_name == "Alice Example"


@pytest.mark.asyncio
async def test_full_name_explicit_overrides_split(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """Legacy callers passing ``full_name`` keep working."""
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="alice",
            email="alice@example.com",
            password="securepassword1",
            full_name="Alice Example",
        )
    assert user.full_name == "Alice Example"


# --- is_approver flag --------------------------------------------------


@pytest.mark.asyncio
async def test_is_approver_default_false(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="alice",
            email="alice@example.com",
            password="securepassword1",
            first_name="Alice",
        )
    assert user.is_approver is False


@pytest.mark.asyncio
async def test_set_approver_toggles_flag(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="alice",
            email="alice@example.com",
            password="securepassword1",
            first_name="Alice",
        )

    async with session_scope(factory) as session:
        updated = await user_service.set_approver(
            session, user_id=user.id, is_approver=True, actor="user:admin"
        )
    assert updated.is_approver is True

    async with session_scope(factory) as session:
        updated = await user_service.set_approver(
            session, user_id=user.id, is_approver=False, actor="user:admin"
        )
    assert updated.is_approver is False


# --- logon_granted_at / logon_revoked_at -------------------------------


@pytest.mark.asyncio
async def test_logon_granted_at_set_on_create(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="alice",
            email="alice@example.com",
            password="securepassword1",
            first_name="Alice",
        )
    assert user.logon_granted_at is not None
    assert user.logon_revoked_at is None


@pytest.mark.asyncio
async def test_deactivate_stamps_logon_revoked_at(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="bob",
            email="bob@example.com",
            password="securepassword1",
            first_name="Bob",
        )

    async with session_scope(factory) as session:
        await user_service.deactivate(session, user_id=user.id, actor="user:admin")

    async with session_scope(factory) as session:
        from matofx.users.orm import User  # noqa: PLC0415

        loaded = await session.get(User, user.id)
        assert loaded is not None
        assert loaded.is_active is False
        assert loaded.logon_revoked_at is not None


@pytest.mark.asyncio
async def test_reactivate_refreshes_logon_granted_at_and_clears_revoked(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="bob",
            email="bob@example.com",
            password="securepassword1",
            first_name="Bob",
        )

    async with session_scope(factory) as session:
        await user_service.deactivate(session, user_id=user.id, actor="user:admin")
    async with session_scope(factory) as session:
        await user_service.reactivate(session, user_id=user.id, actor="user:admin")

    async with session_scope(factory) as session:
        from matofx.users.orm import User  # noqa: PLC0415

        loaded = await session.get(User, user.id)
        assert loaded is not None
        assert loaded.is_active is True
        assert loaded.logon_revoked_at is None


# --- Team CRUD --------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_creates_team(
    users_engine: AsyncEngine,
    user_service: UserService,
    team_service: TeamService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    async with session_scope(factory) as session:
        team = await team_service.create_team(
            session,
            actor_user_id=admin_id,
            name="R&D",
            description="Research and Development",
        )
    assert team.name == "R&D"


@pytest.mark.asyncio
async def test_regular_user_cannot_create_team(
    users_engine: AsyncEngine,
    user_service: UserService,
    team_service: TeamService,
) -> None:
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service, username="alice", role="regular_user")
    async with session_scope(factory) as session:
        with pytest.raises(PermissionDeniedError):
            await team_service.create_team(session, actor_user_id=user_id, name="R&D")


@pytest.mark.asyncio
async def test_duplicate_team_name_rejected(
    users_engine: AsyncEngine,
    user_service: UserService,
    team_service: TeamService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    async with session_scope(factory) as session:
        await team_service.create_team(session, actor_user_id=admin_id, name="R&D")
    async with session_scope(factory) as session:
        with pytest.raises(DuplicateTeamError):
            await team_service.create_team(session, actor_user_id=admin_id, name="R&D")


@pytest.mark.asyncio
async def test_admin_cannot_delete_team_with_members(
    users_engine: AsyncEngine,
    user_service: UserService,
    team_service: TeamService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")

    async with session_scope(factory) as session:
        team = await team_service.create_team(session, actor_user_id=admin_id, name="R&D")
        await team_service.add_user_to_team(
            session,
            actor_user_id=admin_id,
            user_id=alice_id,
            team_id=team.id,
        )

    async with session_scope(factory) as session:
        with pytest.raises(TeamInUseError):
            await team_service.delete_team(session, actor_user_id=admin_id, team_id=team.id)


# --- Team membership -------------------------------------------------


@pytest.mark.asyncio
async def test_user_can_belong_to_multiple_teams(
    users_engine: AsyncEngine,
    user_service: UserService,
    team_service: TeamService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")

    async with session_scope(factory) as session:
        rd = await team_service.create_team(session, actor_user_id=admin_id, name="R&D")
        it = await team_service.create_team(session, actor_user_id=admin_id, name="IT")
        await team_service.add_user_to_team(
            session, actor_user_id=admin_id, user_id=alice_id, team_id=rd.id
        )
        await team_service.add_user_to_team(
            session, actor_user_id=admin_id, user_id=alice_id, team_id=it.id
        )

    async with session_scope(factory) as session:
        teams = await team_service.list_user_teams(session, alice_id)
    assert {t.name for t in teams} == {"R&D", "IT"}


@pytest.mark.asyncio
async def test_already_in_team_rejected(
    users_engine: AsyncEngine,
    user_service: UserService,
    team_service: TeamService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")

    async with session_scope(factory) as session:
        team = await team_service.create_team(session, actor_user_id=admin_id, name="R&D")
        await team_service.add_user_to_team(
            session, actor_user_id=admin_id, user_id=alice_id, team_id=team.id
        )

    async with session_scope(factory) as session:
        with pytest.raises(AlreadyInTeamError):
            await team_service.add_user_to_team(
                session,
                actor_user_id=admin_id,
                user_id=alice_id,
                team_id=team.id,
            )


@pytest.mark.asyncio
async def test_remove_from_team_not_in_raises(
    users_engine: AsyncEngine,
    user_service: UserService,
    team_service: TeamService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")

    async with session_scope(factory) as session:
        team = await team_service.create_team(session, actor_user_id=admin_id, name="R&D")

    async with session_scope(factory) as session:
        with pytest.raises(NotInTeamError):
            await team_service.remove_user_from_team(
                session,
                actor_user_id=admin_id,
                user_id=alice_id,
                team_id=team.id,
            )


@pytest.mark.asyncio
async def test_set_user_teams_replaces_membership(
    users_engine: AsyncEngine,
    user_service: UserService,
    team_service: TeamService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")

    async with session_scope(factory) as session:
        rd = await team_service.create_team(session, actor_user_id=admin_id, name="R&D")
        it = await team_service.create_team(session, actor_user_id=admin_id, name="IT")
        prod = await team_service.create_team(session, actor_user_id=admin_id, name="Production")
        await team_service.add_user_to_team(
            session, actor_user_id=admin_id, user_id=alice_id, team_id=rd.id
        )

    async with session_scope(factory) as session:
        await team_service.set_user_teams(
            session,
            actor_user_id=admin_id,
            user_id=alice_id,
            team_ids=[it.id, prod.id],
        )

    async with session_scope(factory) as session:
        teams = await team_service.list_user_teams(session, alice_id)
    assert {t.name for t in teams} == {"IT", "Production"}


@pytest.mark.asyncio
async def test_unknown_team_raises_team_not_found(
    users_engine: AsyncEngine,
    user_service: UserService,
    team_service: TeamService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    async with session_scope(factory) as session:
        with pytest.raises(TeamNotFoundError):
            await team_service.delete_team(session, actor_user_id=admin_id, team_id=99999)


# --- Rename a team -----------------------------------------------------


@pytest.mark.asyncio
async def test_update_team_renames_and_keeps_members(
    users_engine: AsyncEngine,
    user_service: UserService,
    team_service: TeamService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")

    async with session_scope(factory) as session:
        team = await team_service.create_team(session, actor_user_id=admin_id, name="RnD")
        await team_service.add_user_to_team(
            session, actor_user_id=admin_id, user_id=alice_id, team_id=team.id
        )
        team_id = team.id

    async with session_scope(factory) as session:
        updated = await team_service.update_team(
            session,
            actor_user_id=admin_id,
            team_id=team_id,
            name="R&D",
            description="Research and Development",
        )
        assert updated.name == "R&D"
        assert updated.description == "Research and Development"

    # The rename must not disturb membership (it binds by team id).
    async with session_scope(factory) as session:
        members = await team_service.list_team_members(session, team_id)
    assert {m.username for m in members} == {"alice"}


@pytest.mark.asyncio
async def test_update_team_to_existing_name_rejected(
    users_engine: AsyncEngine,
    user_service: UserService,
    team_service: TeamService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    async with session_scope(factory) as session:
        await team_service.create_team(session, actor_user_id=admin_id, name="R&D")
        it = await team_service.create_team(session, actor_user_id=admin_id, name="IT")
        it_id = it.id

    async with session_scope(factory) as session:
        with pytest.raises(DuplicateTeamError):
            await team_service.update_team(
                session, actor_user_id=admin_id, team_id=it_id, name="R&D", description=""
            )


@pytest.mark.asyncio
async def test_regular_user_cannot_update_team(
    users_engine: AsyncEngine,
    user_service: UserService,
    team_service: TeamService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    user_id = await _make_user(factory, user_service, username="bob", role="regular_user")
    async with session_scope(factory) as session:
        team = await team_service.create_team(session, actor_user_id=admin_id, name="R&D")
        team_id = team.id
    async with session_scope(factory) as session:
        with pytest.raises(PermissionDeniedError):
            await team_service.update_team(
                session, actor_user_id=user_id, team_id=team_id, name="X", description=""
            )


# --- Bulk add to a team ------------------------------------------------


@pytest.mark.asyncio
async def test_add_users_to_team_bulk_and_skips_dupes(
    users_engine: AsyncEngine,
    user_service: UserService,
    team_service: TeamService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="admin", role="administrator")
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")
    bob_id = await _make_user(factory, user_service, username="bob", role="regular_user")
    carol_id = await _make_user(factory, user_service, username="carol", role="regular_user")

    async with session_scope(factory) as session:
        team = await team_service.create_team(session, actor_user_id=admin_id, name="R&D")
        team_id = team.id
        # Alice is already a member — the bulk call must skip her, not error.
        await team_service.add_user_to_team(
            session, actor_user_id=admin_id, user_id=alice_id, team_id=team_id
        )

    async with session_scope(factory) as session:
        added = await team_service.add_users_to_team(
            session,
            actor_user_id=admin_id,
            team_id=team_id,
            user_ids=[alice_id, bob_id, carol_id],
        )
        assert added == 2  # alice skipped

    async with session_scope(factory) as session:
        members = await team_service.list_team_members(session, team_id)
    assert {m.username for m in members} == {"alice", "bob", "carol"}


# --- Permission catalog wired up ---------------------------------------


@pytest.mark.asyncio
async def test_administrator_has_users_manage_and_teams_manage(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    admin_id = await _make_user(factory, user_service, username="ada", role="administrator")
    async with session_scope(factory) as session:
        assert await user_has_permission(session, admin_id, Permission.USERS_MANAGE)
        assert await user_has_permission(session, admin_id, Permission.TEAMS_MANAGE)


@pytest.mark.asyncio
async def test_regular_user_does_not_have_users_or_teams_manage(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    alice_id = await _make_user(factory, user_service, username="alice", role="regular_user")
    async with session_scope(factory) as session:
        assert not await user_has_permission(session, alice_id, Permission.USERS_MANAGE)
        assert not await user_has_permission(session, alice_id, Permission.TEAMS_MANAGE)
