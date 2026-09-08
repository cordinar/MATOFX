"""Tests for the session factory and session_scope helper."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from matofx.config import DbSettings
from matofx.db import SessionFactory, make_engine, make_session_factory, session_scope


@pytest.mark.asyncio
async def test_session_scope_commits_on_success(db_settings: DbSettings) -> None:
    engine = make_engine(db_settings)
    factory = make_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await engine.dispose()


class _SentinelError(RuntimeError):
    """Raised by the rollback test to exit the session scope."""


async def _raise_inside_scope(factory: SessionFactory) -> None:
    async with session_scope(factory) as session:
        await session.execute(text("SELECT 1"))
        raise _SentinelError("planned failure")


@pytest.mark.asyncio
async def test_session_scope_rolls_back_on_exception(db_settings: DbSettings) -> None:
    engine = make_engine(db_settings)
    factory = make_session_factory(engine)
    try:
        with pytest.raises(_SentinelError):
            await _raise_inside_scope(factory)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_factory_produces_distinct_sessions(db_settings: DbSettings) -> None:
    engine = make_engine(db_settings)
    factory = make_session_factory(engine)
    try:
        s1 = factory()
        s2 = factory()
        assert s1 is not s2
        await s1.close()
        await s2.close()
    finally:
        await engine.dispose()
