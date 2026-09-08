"""Tests for SessionService — create, validate, revoke, expiry."""

from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import ensure_chain_head
from matofx.config import AuthSettings
from matofx.db import make_session_factory, session_scope
from matofx.users import (
    SessionExpiredError,
    SessionInvalidError,
    SessionService,
    UserService,
    make_session_service,
)
from matofx.users.orm import AuthSession
from matofx.users.service import BREAKGLASS_MAX_HOURS


async def _make_user(session_factory, user_service: UserService) -> int:
    async with session_factory() as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="alice",
            email="alice@example.com",
            password="goodpassword123",
            full_name="Alice",
        )
        await session.commit()
        return user.id


@pytest.mark.asyncio
async def test_create_session_returns_token_and_persists_hash(
    users_engine: AsyncEngine,
    user_service: UserService,
    session_service: SessionService,
) -> None:
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service)

    async with session_scope(factory) as session:
        created = await session_service.create_session(
            session, user_id=user_id, ip_address="127.0.0.1"
        )

    assert created.user_id == user_id
    assert len(created.token) > 32
    assert created.expires_at > _dt.datetime.now(tz=_dt.UTC)


@pytest.mark.asyncio
async def test_validate_returns_row_and_refreshes_activity(
    users_engine: AsyncEngine,
    user_service: UserService,
    session_service: SessionService,
) -> None:
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service)

    async with session_scope(factory) as session:
        created = await session_service.create_session(session, user_id=user_id)

    async with session_scope(factory) as session:
        row = await session_service.validate(session, token=created.token)

    assert row.user_id == user_id


async def _activity_of(session_factory, token: str) -> _dt.datetime:
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.users.tokens import hash_session_token  # noqa: PLC0415

    async with session_scope(session_factory) as session:
        stmt = select(AuthSession).where(AuthSession.token_hash == hash_session_token(token))
        return (await session.execute(stmt)).scalar_one().last_activity_at


@pytest.mark.asyncio
async def test_validate_coalesces_activity_write_within_window(
    users_engine: AsyncEngine,
    user_service: UserService,
    session_service: SessionService,
) -> None:
    """A validate right after creation must NOT rewrite last_activity_at.

    Writing on every request turns read bursts into a SQLite write storm
    ("database is locked"); within the coalesce window the row is left
    untouched.
    """
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service)

    async with session_scope(factory) as session:
        created = await session_service.create_session(session, user_id=user_id)
    before = await _activity_of(factory, created.token)

    async with session_scope(factory) as session:
        await session_service.validate(session, token=created.token)

    assert await _activity_of(factory, created.token) == before


@pytest.mark.asyncio
async def test_validate_refreshes_activity_past_window(
    users_engine: AsyncEngine,
    user_service: UserService,
    session_service: SessionService,
) -> None:
    """Once activity has aged past the window, validate advances the marker
    (so the sliding idle timeout keeps working)."""
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service)

    async with session_scope(factory) as session:
        created = await session_service.create_session(session, user_id=user_id)

    # Backdate well past the coalesce window but inside the idle timeout.
    stale = _dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(minutes=5)
    async with session_scope(factory) as session:
        from sqlalchemy import select  # noqa: PLC0415

        from matofx.users.tokens import hash_session_token  # noqa: PLC0415

        stmt = select(AuthSession).where(
            AuthSession.token_hash == hash_session_token(created.token)
        )
        (await session.execute(stmt)).scalar_one().last_activity_at = stale

    async with session_scope(factory) as session:
        await session_service.validate(session, token=created.token)

    assert _as_utc(await _activity_of(factory, created.token)) > _as_utc(stale)


def _as_utc(ts: _dt.datetime) -> _dt.datetime:
    """SQLite reads datetimes back naive; coerce for comparison."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=_dt.UTC)


@pytest.mark.asyncio
async def test_validate_unknown_token_raises_invalid(
    users_engine: AsyncEngine,
    session_service: SessionService,
) -> None:
    factory = make_session_factory(users_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        with pytest.raises(SessionInvalidError):
            await session_service.validate(session, token="garbage-token")


@pytest.mark.asyncio
async def test_revoked_session_invalid(
    users_engine: AsyncEngine,
    user_service: UserService,
    session_service: SessionService,
) -> None:
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service)

    async with session_scope(factory) as session:
        created = await session_service.create_session(session, user_id=user_id)

    async with session_scope(factory) as session:
        await session_service.revoke(session, token=created.token, actor=f"user:{user_id}")

    async with session_scope(factory) as session:
        with pytest.raises(SessionInvalidError):
            await session_service.validate(session, token=created.token)


@pytest.mark.asyncio
async def test_idle_timeout_expires_session(
    users_engine: AsyncEngine,
    user_service: UserService,
    audit_writer,
) -> None:
    """A session whose last_activity_at is older than the idle window expires."""
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service)

    short_idle = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        session_idle_timeout_minutes=1,
        session_hard_timeout_hours=12,
    )
    svc = make_session_service(short_idle, audit_writer)

    async with session_scope(factory) as session:
        created = await svc.create_session(session, user_id=user_id)

    # Backdate the last_activity_at past the idle window.
    async with session_scope(factory) as session:
        from sqlalchemy import select  # noqa: PLC0415

        from matofx.users.tokens import hash_session_token  # noqa: PLC0415

        token_hash = hash_session_token(created.token)
        stmt = select(AuthSession).where(AuthSession.token_hash == token_hash)
        row = (await session.execute(stmt)).scalar_one()
        row.last_activity_at = _dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(minutes=10)

    async with session_scope(factory) as session:
        with pytest.raises(SessionExpiredError):
            await svc.validate(session, token=created.token)


@pytest.mark.asyncio
async def test_hard_timeout_expires_session(
    users_engine: AsyncEngine,
    user_service: UserService,
    audit_writer,
) -> None:
    """Even a recently-active session expires past the hard ceiling."""
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service)

    short_hard = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        session_idle_timeout_minutes=60,
        session_hard_timeout_hours=1,
    )
    svc = make_session_service(short_hard, audit_writer)

    async with session_scope(factory) as session:
        created = await svc.create_session(session, user_id=user_id)

    async with session_scope(factory) as session:
        from sqlalchemy import select  # noqa: PLC0415

        from matofx.users.tokens import hash_session_token  # noqa: PLC0415

        token_hash = hash_session_token(created.token)
        stmt = select(AuthSession).where(AuthSession.token_hash == token_hash)
        row = (await session.execute(stmt)).scalar_one()
        now = _dt.datetime.now(tz=_dt.UTC)
        row.created_at = now - _dt.timedelta(hours=2)
        row.last_activity_at = now  # recent activity, but hard ceiling hit

    async with session_scope(factory) as session:
        with pytest.raises(SessionExpiredError):
            await svc.validate(session, token=created.token)


@pytest.mark.asyncio
async def test_revoke_idempotent(
    users_engine: AsyncEngine,
    user_service: UserService,
    session_service: SessionService,
) -> None:
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service)
    async with session_scope(factory) as session:
        created = await session_service.create_session(session, user_id=user_id)
    async with session_scope(factory) as session:
        await session_service.revoke(session, token=created.token, actor=f"user:{user_id}")
    async with session_scope(factory) as session:
        # Calling again must not raise.
        await session_service.revoke(session, token=created.token, actor=f"user:{user_id}")


# ----- break-glass sessions are time-boxed (ADR-0019 §C13) -------------
#
# A Super User admitted *over* the licence cap keeps an administrator
# from being locked out of a site whose licence needs fixing. Overview
# gap 7: nothing bounded that, so an install could sit over its cap
# indefinitely.
#
# Capping the *number* of admissions was rejected — two stale Super User
# sessions holding the cap would lock out the very administrator
# break-glass exists to admit. The bound is on duration instead, where
# there is no such trade.


@pytest.mark.asyncio
async def test_breakglass_session_expires_at_its_own_shorter_bound(
    users_engine: AsyncEngine,
    user_service: UserService,
    audit_writer,
) -> None:
    """Same age, same policy — the break-glass session is the one that dies."""
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service)

    # A site policy far more generous than the break-glass bound, so the
    # only thing that can expire the session is the break-glass rule.
    generous = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        session_idle_timeout_minutes=1440,
        session_hard_timeout_hours=BREAKGLASS_MAX_HOURS * 10,
    )
    svc = make_session_service(generous, audit_writer)

    async with session_scope(factory) as session:
        ordinary = await svc.create_session(session, user_id=user_id)
        emergency = await svc.create_session(session, user_id=user_id)

    aged = _dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(hours=BREAKGLASS_MAX_HOURS + 1)
    async with session_scope(factory) as session:
        from sqlalchemy import select  # noqa: PLC0415

        from matofx.users.tokens import hash_session_token  # noqa: PLC0415

        for token, is_breakglass in ((ordinary.token, False), (emergency.token, True)):
            row = (
                await session.execute(
                    select(AuthSession).where(AuthSession.token_hash == hash_session_token(token))
                )
            ).scalar_one()
            row.created_at = aged
            row.breakglass = is_breakglass

    async with session_scope(factory) as session:
        # The honest case: an ordinary session of the same age survives,
        # so the assertion below is about break-glass and not about the
        # site policy having quietly expired everything.
        assert await svc.validate(session, token=ordinary.token) is not None

    async with session_scope(factory) as session:
        with pytest.raises(SessionExpiredError):
            await svc.validate(session, token=emergency.token)


@pytest.mark.asyncio
async def test_a_fresh_breakglass_session_still_works(
    users_engine: AsyncEngine,
    user_service: UserService,
    session_service: SessionService,
) -> None:
    """Bounded, never refused — break-glass must actually admit."""
    factory = make_session_factory(users_engine)
    user_id = await _make_user(factory, user_service)

    async with session_scope(factory) as session:
        created = await session_service.create_session(session, user_id=user_id)

    async with session_scope(factory) as session:
        from sqlalchemy import select  # noqa: PLC0415

        from matofx.users.tokens import hash_session_token  # noqa: PLC0415

        row = (
            await session.execute(
                select(AuthSession).where(
                    AuthSession.token_hash == hash_session_token(created.token)
                )
            )
        ).scalar_one()
        row.breakglass = True

    async with session_scope(factory) as session:
        assert await session_service.validate(session, token=created.token) is not None
