"""Tests for the first-user bootstrap helper."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.users import UserService
from matofx.users.bootstrap import BootstrapError, create_first_user


@pytest.mark.asyncio
async def test_creates_super_user(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        user = await create_first_user(
            session,
            user_service,
            username="root",
            email="root@example.com",
            password="bootstrappassword",
            first_name="Root",
            last_name="Admin",
        )
    async with session_scope(factory) as session:
        from matofx.users import Role  # noqa: PLC0415

        role = await session.get(Role, user.role_id)
        assert role is not None
        assert role.name == "super_user"
    assert user.is_active
    assert user.username == "root"
    assert user.first_name == "Root"
    assert user.last_name == "Admin"
    assert user.full_name == "Root Admin"


@pytest.mark.asyncio
async def test_refuses_when_users_already_exist(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await create_first_user(
            session,
            user_service,
            username="root",
            email="root@example.com",
            password="bootstrappassword",
            first_name="Root",
            last_name="One",
        )

    async with session_scope(factory) as session:
        with pytest.raises(BootstrapError):
            await create_first_user(
                session,
                user_service,
                username="root2",
                email="root2@example.com",
                password="anotherpassword",
                first_name="Root",
                last_name="Two",
            )
