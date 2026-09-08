"""Tests for user_logon_event — multi-cycle grant/revoke history."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.users import UserService


@pytest.mark.asyncio
async def test_create_user_writes_initial_granted_event(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """A fresh account starts with exactly one ``granted`` event."""
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
        events = await user_service.list_logon_events(session, user_id=user.id)
    assert [e.event for e in events] == ["granted"]
    assert events[0].actor_user_id is None
    assert events[0].reason is None


@pytest.mark.asyncio
async def test_deactivate_appends_revoked_event(
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
        await user_service.deactivate(
            session, user_id=user.id, actor="user:admin", reason="left the team"
        )

    async with session_scope(factory) as session:
        events = await user_service.list_logon_events(session, user_id=user.id)
    assert [e.event for e in events] == ["granted", "revoked"]
    assert events[1].reason == "left the team"


@pytest.mark.asyncio
async def test_multiple_cycles_each_write_an_event(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """Three grant/revoke cycles → six events in chronological order."""
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

    for i in range(2):
        async with session_scope(factory) as session:
            await user_service.deactivate(
                session, user_id=user.id, actor="user:admin", reason=f"revoke {i}"
            )
        async with session_scope(factory) as session:
            await user_service.reactivate(
                session, user_id=user.id, actor="user:admin", reason=f"reactivate {i}"
            )

    async with session_scope(factory) as session:
        events = await user_service.list_logon_events(session, user_id=user.id)
    assert [e.event for e in events] == [
        "granted",
        "revoked",
        "granted",
        "revoked",
        "granted",
    ]
    assert events[1].reason == "revoke 0"
    assert events[2].reason == "reactivate 0"
    assert events[3].reason == "revoke 1"


@pytest.mark.asyncio
async def test_actor_user_id_recorded_when_supplied(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """An admin's user-id is captured on the event row's FK."""
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        admin = await user_service.create_user(
            session,
            username="admin",
            email="admin@example.com",
            password="securepassword1",
            first_name="Admin",
            role="administrator",
        )
        target = await user_service.create_user(
            session,
            username="alice",
            email="alice@example.com",
            password="securepassword1",
            first_name="Alice",
            actor=f"user:{admin.id}",
            actor_user_id=admin.id,
        )

    async with session_scope(factory) as session:
        await user_service.deactivate(
            session,
            user_id=target.id,
            actor=f"user:{admin.id}",
            actor_user_id=admin.id,
        )

    async with session_scope(factory) as session:
        events = await user_service.list_logon_events(session, user_id=target.id)
    assert [e.event for e in events] == ["granted", "revoked"]
    assert events[0].actor_user_id == admin.id
    assert events[1].actor_user_id == admin.id


@pytest.mark.asyncio
async def test_idempotent_deactivate_does_not_double_log(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """Calling deactivate on an already-disabled user is a no-op (no extra event)."""
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
        await user_service.deactivate(session, user_id=user.id, actor="user:admin")
    async with session_scope(factory) as session:
        await user_service.deactivate(session, user_id=user.id, actor="user:admin")

    async with session_scope(factory) as session:
        events = await user_service.list_logon_events(session, user_id=user.id)
    assert [e.event for e in events] == ["granted", "revoked"]


@pytest.mark.asyncio
async def test_idempotent_reactivate_does_not_double_log(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """Calling reactivate on an active user is a no-op (no extra event)."""
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
        await user_service.reactivate(session, user_id=user.id, actor="user:admin")

    async with session_scope(factory) as session:
        events = await user_service.list_logon_events(session, user_id=user.id)
    assert [e.event for e in events] == ["granted"]


@pytest.mark.asyncio
async def test_list_logon_events_orders_oldest_first(
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
        await user_service.deactivate(session, user_id=user.id, actor="user:admin")
    async with session_scope(factory) as session:
        await user_service.reactivate(session, user_id=user.id, actor="user:admin")

    async with session_scope(factory) as session:
        events = await user_service.list_logon_events(session, user_id=user.id)

    timestamps = [e.at for e in events]
    assert timestamps == sorted(timestamps)
