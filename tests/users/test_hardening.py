"""Security-hardening tests for matofx.users.

Covers:
- Session validation rejects deactivated users.
- ``deactivate`` cascade-revokes the user's active sessions.
- Username + email canonicalisation (lower-case dedup).
- Constant-time defence on the unknown-username authenticate path.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import ensure_chain_head
from matofx.config import AuthSettings
from matofx.db import make_session_factory, session_scope
from matofx.users import (
    AccountDisabledError,
    DuplicateUserError,
    SessionService,
    UserService,
    make_password_hasher,
)
from matofx.users.orm import AuthSession


def _create_alice(factory, user_service: UserService) -> int:
    async def _do() -> int:
        async with factory() as session:
            await ensure_chain_head(session)
            user = await user_service.create_user(
                session,
                username="alice",
                email="alice@example.com",
                password="securepassword1",
                full_name="Alice",
            )
            await session.commit()
            return user.id

    import asyncio  # noqa: PLC0415

    return asyncio.get_event_loop().run_until_complete(_do())


# --- Session validate refuses deactivated users -------------------------


@pytest.mark.asyncio
async def test_session_validate_refuses_deactivated_user(
    users_engine: AsyncEngine,
    user_service: UserService,
    session_service: SessionService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="alice",
            email="alice@example.com",
            password="securepassword1",
            full_name="Alice",
        )
        await session.commit()

    async with session_scope(factory) as session:
        created = await session_service.create_session(session, user_id=user.id)

    # Operator deactivates the user (without using deactivate's cascade —
    # simulating a race or direct DB update).
    async with session_scope(factory) as session:
        from matofx.users.orm import User  # noqa: PLC0415

        u = await session.get(User, user.id)
        assert u is not None
        u.is_active = False
        await session.flush()

    async with session_scope(factory) as session:
        with pytest.raises(AccountDisabledError):
            await session_service.validate(session, token=created.token)


# --- Deactivate cascade-revokes sessions --------------------------------


@pytest.mark.asyncio
async def test_deactivate_cascades_revoke_to_active_sessions(
    users_engine: AsyncEngine,
    user_service: UserService,
    session_service: SessionService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="bob",
            email="bob@example.com",
            password="securepassword1",
            full_name="Bob",
        )

    async with session_scope(factory) as session:
        await session_service.create_session(session, user_id=user.id)
        await session_service.create_session(session, user_id=user.id)

    async with session_scope(factory) as session:
        await user_service.deactivate(session, user_id=user.id, actor="user:admin")

    async with session_scope(factory) as session:
        rows = (
            (await session.execute(select(AuthSession).where(AuthSession.user_id == user.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert all(row.revoked_at is not None for row in rows)


# --- Username + email canonicalisation ----------------------------------


@pytest.mark.asyncio
async def test_username_lowercased_at_create(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="Alice",
            email="alice@example.com",
            password="securepassword1",
            full_name="Alice",
        )
    assert user.username == "alice"


@pytest.mark.asyncio
async def test_email_lowercased_at_create(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="alice",
            email="Alice@Example.COM",
            password="securepassword1",
            full_name="Alice",
        )
    assert user.email == "alice@example.com"


@pytest.mark.asyncio
async def test_case_variant_username_treated_as_duplicate(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await user_service.create_user(
            session,
            username="alice",
            email="alice@example.com",
            password="securepassword1",
            full_name="Alice",
        )
    async with session_scope(factory) as session:
        with pytest.raises(DuplicateUserError):
            await user_service.create_user(
                session,
                username="ALICE",
                email="alice2@example.com",
                password="securepassword1",
                full_name="Alice 2",
            )


@pytest.mark.asyncio
async def test_case_variant_email_treated_as_duplicate(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await user_service.create_user(
            session,
            username="alice",
            email="alice@example.com",
            password="securepassword1",
            full_name="Alice",
        )
    async with session_scope(factory) as session:
        with pytest.raises(DuplicateUserError):
            await user_service.create_user(
                session,
                username="alice2",
                email="ALICE@example.com",
                password="securepassword1",
                full_name="Alice 2",
            )


@pytest.mark.asyncio
async def test_authenticate_accepts_mixed_case_username(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await user_service.create_user(
            session,
            username="alice",
            email="alice@example.com",
            password="securepassword1",
            full_name="Alice",
        )

    async with session_scope(factory) as session:
        # User types "Alice" — same account.
        user = await user_service.authenticate(
            session, username="Alice", password="securepassword1"
        )
    assert user.username == "alice"


# --- Constant-time defence on unknown user ------------------------------


def test_password_hasher_has_dummy_hash(auth_settings: AuthSettings) -> None:
    """The dummy hash is computed at construction with the same parameters."""
    h = make_password_hasher(auth_settings)
    assert h.dummy_hash.startswith("$argon2")


def test_equalising_verify_returns_false_on_none(auth_settings: AuthSettings) -> None:
    """The unknown-user path goes through a real Argon2 verify."""
    h = make_password_hasher(auth_settings)
    # Should not raise; should return False.
    assert h.equalising_verify(None, "anything") is False


def test_equalising_verify_returns_true_on_match(auth_settings: AuthSettings) -> None:
    h = make_password_hasher(auth_settings)
    hashed = h.hash("real-password")
    assert h.equalising_verify(hashed, "real-password") is True
    assert h.equalising_verify(hashed, "wrong") is False
