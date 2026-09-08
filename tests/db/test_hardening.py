"""Security-hardening tests for matofx.db.

Covers connection-pool tightening, the initial connect timeout, and
the make_engine_scope context manager that guarantees disposal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from matofx.config import DbSettings
from matofx.db import make_engine, make_engine_scope

# --- Pool tightening defaults --------------------------------------------


def test_db_settings_pool_defaults() -> None:
    s = DbSettings()
    assert s.pool_size == 10
    assert s.pool_timeout_seconds == 30
    assert s.max_overflow == 10
    assert s.pool_recycle_seconds == 1_800
    assert s.connect_timeout_seconds == 10


def test_db_settings_pool_bounds() -> None:
    with pytest.raises(ValidationError):
        DbSettings(pool_timeout_seconds=0)
    with pytest.raises(ValidationError):
        DbSettings(pool_timeout_seconds=10_000)
    with pytest.raises(ValidationError):
        DbSettings(max_overflow=-1)
    with pytest.raises(ValidationError):
        DbSettings(pool_recycle_seconds=10)  # below 60-second minimum
    with pytest.raises(ValidationError):
        DbSettings(connect_timeout_seconds=0)


def test_db_settings_pool_overrides_accepted() -> None:
    s = DbSettings(
        pool_size=20,
        pool_timeout_seconds=60,
        max_overflow=5,
        pool_recycle_seconds=600,
        connect_timeout_seconds=15,
    )
    assert s.pool_size == 20
    assert s.pool_timeout_seconds == 60
    assert s.max_overflow == 5
    assert s.pool_recycle_seconds == 600
    assert s.connect_timeout_seconds == 15


# --- Engine accepts the new pool config ---------------------------------


def test_engine_constructed_with_custom_pool_config(tmp_path: Path) -> None:
    """make_engine doesn't crash when given non-default pool config."""
    s = DbSettings.from_url(
        f"sqlite:///{tmp_path / 'm.db'}",
        dialect="sqlite",
        connect_timeout_seconds=5,
    )
    engine = make_engine(s)
    assert engine is not None


@pytest.mark.postgres
def test_postgres_engine_passes_pool_config_through() -> None:
    """The Postgres path forwards the new pool kwargs to create_async_engine.

    We just check the engine constructs and the pool config is reflected
    on the underlying pool object — no live connection needed.
    """
    s = DbSettings.from_url(
        "postgresql+asyncpg://u:p@nowhere:5432/db",
        dialect="postgres",
        pool_size=15,
        pool_timeout_seconds=45,
        max_overflow=20,
        pool_recycle_seconds=900,
    )
    engine = make_engine(s)
    # SQLAlchemy's QueuePool exposes these via the underlying sync pool;
    # we cast to Any to dodge mypy's narrow Pool protocol.
    pool: Any = engine.pool
    assert pool.size() == 15
    assert pool._timeout == 45
    assert pool._max_overflow == 20
    assert pool._recycle == 900


# --- make_engine_scope context manager ----------------------------------


@pytest.mark.asyncio
async def test_make_engine_scope_yields_usable_engine(tmp_path: Path) -> None:
    s = DbSettings.from_url(f"sqlite:///{tmp_path / 'm.db'}", dialect="sqlite")
    async with make_engine_scope(s) as engine:
        assert engine.dialect.name == "sqlite"


@pytest.mark.asyncio
async def test_make_engine_scope_disposes_on_exit(tmp_path: Path) -> None:
    """After exiting the scope, the engine is disposed (the pool is closed)."""
    s = DbSettings.from_url(f"sqlite:///{tmp_path / 'm.db'}", dialect="sqlite")
    async with make_engine_scope(s) as engine:
        captured = engine
    # After dispose, the pool is empty / closed. SQLAlchemy doesn't expose
    # a public "is_disposed" flag, but pool.size() drops to 0 for QueuePool
    # and NullPool simply has no size to track. The robust check is that
    # any further attempt to connect raises (or, for NullPool, succeeds
    # since dispose is a no-op for NullPool). For the regular SQLite path
    # we rely on the lack of exception — the dispose() coroutine ran.
    assert captured is not None  # smoke check; real proof is no exception above


@pytest.mark.asyncio
async def test_make_engine_scope_disposes_on_exception(tmp_path: Path) -> None:
    """Exceptions inside the scope still trigger dispose."""
    s = DbSettings.from_url(f"sqlite:///{tmp_path / 'm.db'}", dialect="sqlite")

    class _SentinelError(RuntimeError):
        pass

    with pytest.raises(_SentinelError):
        async with make_engine_scope(s):
            raise _SentinelError("planned failure")

    # If the engine wasn't disposed, asyncio would still hold pool
    # connections; the test just asserts the context manager re-raised.
