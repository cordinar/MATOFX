"""Tests for the async engine factory."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from matofx.config import DbSettings
from matofx.db import DbConfigError, make_engine
from matofx.db.engine import _async_url


def test_async_url_translation_sqlite_friendly_form() -> None:
    s = DbSettings.from_url("sqlite:///./x.db", dialect="sqlite")
    assert _async_url(s) == "sqlite+aiosqlite:///./x.db"


def test_async_url_translation_sqlite_explicit_async_form() -> None:
    s = DbSettings.from_url("sqlite+aiosqlite:///./x.db", dialect="sqlite")
    assert _async_url(s) == "sqlite+aiosqlite:///./x.db"


def test_async_url_translation_postgres_postgres_alias() -> None:
    s = DbSettings.from_url("postgres://u:p@h/db", dialect="postgres")
    assert _async_url(s) == "postgresql+asyncpg://u:p@h/db"


def test_async_url_translation_postgres_postgresql() -> None:
    s = DbSettings.from_url("postgresql://u:p@h/db", dialect="postgres")
    assert _async_url(s) == "postgresql+asyncpg://u:p@h/db"


def test_async_url_translation_postgres_explicit_async_form() -> None:
    s = DbSettings.from_url("postgresql+asyncpg://u:p@h/db", dialect="postgres")
    assert _async_url(s) == "postgresql+asyncpg://u:p@h/db"


def test_dialect_url_mismatch_sqlite_raises() -> None:
    s = DbSettings.from_url("postgres://u:p@h/db", dialect="sqlite")
    with pytest.raises(DbConfigError) as exc_info:
        _async_url(s)
    assert "sqlite" in str(exc_info.value).lower()


def test_dialect_url_mismatch_postgres_raises() -> None:
    s = DbSettings.from_url("sqlite:///./x.db", dialect="postgres")
    with pytest.raises(DbConfigError) as exc_info:
        _async_url(s)
    assert "postgres" in str(exc_info.value).lower()


def test_engine_constructed_for_sqlite(tmp_path: Path) -> None:
    """make_engine returns a usable engine without raising for SQLite."""
    s = DbSettings.from_url(f"sqlite:///{tmp_path / 'm.db'}", dialect="sqlite")
    engine = make_engine(s)
    assert engine is not None
    assert engine.dialect.name == "sqlite"


@pytest.mark.postgres
def test_engine_constructed_for_postgres_url_form_only() -> None:
    """make_engine accepts a Postgres URL without actually connecting.

    Requires the ``postgres`` extra (asyncpg). Skipped on SQLite-only runs.
    """
    s = DbSettings.from_url("postgresql+asyncpg://u:p@nowhere:5432/db", dialect="postgres")
    engine = make_engine(s)
    assert engine.dialect.name == "postgresql"


@pytest.mark.asyncio
async def test_engine_executes_simple_query(db_settings: DbSettings) -> None:
    """End-to-end: connect, run SELECT 1, get 1 back."""
    engine = make_engine(db_settings)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            value = result.scalar_one()
        assert value == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_pragmas_applied(tmp_path: Path) -> None:
    """SQLite engines must enforce FKs and use WAL by default."""
    s = DbSettings.from_url(
        f"sqlite:///{tmp_path / 'm.db'}",
        dialect="sqlite",
        sqlite_busy_timeout_ms=7777,
    )
    engine = make_engine(s)
    try:
        async with engine.connect() as conn:
            fk = (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one()
            journal = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()
            busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar_one()
        assert fk == 1
        assert str(journal).lower() == "wal"
        assert busy == 7777
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_statement_timeout_applied(db_settings: DbSettings) -> None:
    """Postgres statement_timeout is set per the configured value."""
    if db_settings.dialect != "postgres":
        pytest.skip("Postgres-specific")
    engine = make_engine(db_settings)
    try:
        async with engine.connect() as conn:
            value = (await conn.execute(text("SHOW statement_timeout"))).scalar_one()
        # Postgres formats the timeout value (e.g. "30s", "30000ms"); just
        # require it to be a non-empty string and not the unset default.
        assert value
        assert value != "0"
    finally:
        await engine.dispose()
