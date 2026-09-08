"""End-to-end tests for UserService against a real database."""

from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.users import (
    AccountDisabledError,
    DuplicateUserError,
    InvalidCredentialsError,
    InvalidEmailError,
    UserService,
    WeakPasswordError,
)
from matofx.users.orm import User


@pytest.mark.asyncio
async def test_create_user_persists_with_hashed_password(
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
            password="averysecurepassword",
            full_name="Alice Example",
        )

    async with session_scope(factory) as session:
        loaded = await session.get(User, user.id)
        assert loaded is not None
        assert loaded.username == "alice"
        assert loaded.email == "alice@example.com"
        assert loaded.password_hash != "averysecurepassword"
        assert loaded.is_active
        from matofx.users import Role  # noqa: PLC0415

        role = await session.get(Role, loaded.role_id)
        assert role is not None
        assert role.name == "regular_user"


@pytest.mark.asyncio
async def test_create_user_rejects_short_password(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        with pytest.raises(WeakPasswordError):
            await user_service.create_user(
                session,
                username="alice",
                email="alice@example.com",
                password="short",
                full_name="Alice",
            )


@pytest.mark.asyncio
async def test_create_user_rejects_invalid_email(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        with pytest.raises(InvalidEmailError):
            await user_service.create_user(
                session,
                username="alice",
                email="not-an-email",
                password="averysecurepassword",
                full_name="Alice",
            )


@pytest.mark.asyncio
async def test_duplicate_username_rejected(
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
            password="averysecurepassword",
            full_name="Alice",
        )

    async with session_scope(factory) as session:
        with pytest.raises(DuplicateUserError):
            await user_service.create_user(
                session,
                username="alice",
                email="alice2@example.com",
                password="averysecurepassword",
                full_name="Alice 2",
            )


@pytest.mark.asyncio
async def test_authenticate_success(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await user_service.create_user(
            session,
            username="bob",
            email="bob@example.com",
            password="goodpassword123",
            full_name="Bob Example",
        )

    async with session_scope(factory) as session:
        user = await user_service.authenticate(session, username="bob", password="goodpassword123")
    assert user.username == "bob"
    assert user.last_login_at is not None


@pytest.mark.asyncio
async def test_authenticate_wrong_password_raises_invalid_credentials(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await user_service.create_user(
            session,
            username="bob",
            email="bob@example.com",
            password="goodpassword123",
            full_name="Bob",
        )

    async with session_scope(factory) as session:
        with pytest.raises(InvalidCredentialsError):
            await user_service.authenticate(session, username="bob", password="wrong")


@pytest.mark.asyncio
async def test_authenticate_unknown_user_indistinguishable(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        with pytest.raises(InvalidCredentialsError):
            await user_service.authenticate(session, username="ghost", password="anything")


@pytest.mark.asyncio
async def test_lockout_after_threshold_failures(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """After N failures the account locks; the right password also fails until cooldown."""
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await user_service.create_user(
            session,
            username="carol",
            email="carol@example.com",
            password="thisismypassword",
            full_name="Carol",
        )

    threshold = user_service.settings.failed_login_lockout_threshold
    for _ in range(threshold):
        async with session_scope(factory) as session:
            with pytest.raises(InvalidCredentialsError):
                await user_service.authenticate(session, username="carol", password="wrong")

    # Now even the correct password fails — account is locked.
    async with session_scope(factory) as session:
        with pytest.raises(InvalidCredentialsError):
            await user_service.authenticate(session, username="carol", password="thisismypassword")

    async with session_scope(factory) as session:
        user = await user_service.get_by_username(session, username="carol")
        assert user is not None
        assert user.locked_until is not None
        # SQLite drops tzinfo; coerce both sides to compare correctly.
        locked = user.locked_until
        if locked.tzinfo is None:
            locked = locked.replace(tzinfo=_dt.UTC)
        assert locked > _dt.datetime.now(tz=_dt.UTC)


@pytest.mark.asyncio
async def test_deactivated_account_raises_disabled(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        u = await user_service.create_user(
            session,
            username="dave",
            email="dave@example.com",
            password="goodpassword123",
            full_name="Dave",
        )
        await user_service.deactivate(session, user_id=u.id, actor="user:admin")

    async with session_scope(factory) as session:
        with pytest.raises(AccountDisabledError):
            await user_service.authenticate(session, username="dave", password="goodpassword123")


@pytest.mark.asyncio
async def test_disabled_account_wrong_password_is_indistinguishable(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """A disabled account must not leak its status to a wrong-password probe.

    The disabled state is revealed (``AccountDisabledError`` → 403) only
    once the correct password is supplied; a wrong password collapses to
    ``InvalidCredentialsError`` (401) exactly like an active account or an
    unknown user, so an attacker can't enumerate disabled accounts.
    """
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        u = await user_service.create_user(
            session,
            username="erin",
            email="erin@example.com",
            password="goodpassword123",
            full_name="Erin",
        )
        await user_service.deactivate(session, user_id=u.id, actor="user:admin")

    # Wrong password against the disabled account → 401-shaped, NOT 403.
    async with session_scope(factory) as session:
        with pytest.raises(InvalidCredentialsError):
            await user_service.authenticate(session, username="erin", password="wrong")


@pytest.mark.asyncio
async def test_set_theme_preference_persists_and_idempotent(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """Service write persists; flipping back to the existing value is a no-op."""
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="erin",
            email="erin@example.com",
            password="goodpassword123",
            full_name="Erin",
        )

    async with session_scope(factory) as session:
        updated = await user_service.set_theme_preference(
            session, user_id=user.id, theme="light", actor=f"user:{user.id}"
        )
    assert updated.theme_preference == "light"

    # Idempotent: flipping to the value already stored returns the same row
    # and writes nothing extra (no flush, no audit row).
    async with session_scope(factory) as session:
        again = await user_service.set_theme_preference(
            session, user_id=user.id, theme="light", actor=f"user:{user.id}"
        )
    assert again.theme_preference == "light"


@pytest.mark.asyncio
async def test_set_theme_preference_rejects_unknown_value(
    users_engine: AsyncEngine,
    user_service: UserService,
) -> None:
    """Defence-in-depth: anything outside ``{'light', 'dark'}`` raises."""
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="frank",
            email="frank@example.com",
            password="goodpassword123",
            full_name="Frank",
        )

    async with session_scope(factory) as session:
        with pytest.raises(ValueError, match="Unknown theme"):
            await user_service.set_theme_preference(
                session, user_id=user.id, theme="neon", actor=f"user:{user.id}"
            )
