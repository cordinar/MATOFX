"""Async SQLAlchemy engine factory.

The same code path serves both supported dialects:

- **SQLite** (tiny / dev installs) — file or in-memory, async via aiosqlite.
- **Postgres** (medium installs) — async via asyncpg.

The customer's :class:`matofx.config.DbSettings` selects between them. The
engine factory translates the friendly URL forms (``sqlite:///path``,
``postgres://...``) into the async-driver URLs SQLAlchemy needs, applies
dialect-specific safety pragmas, and returns a ready-to-use engine.

Engine lifetime: one engine per process. Call :func:`make_engine` once at
process startup, store the result, and pass it into the session factory
(:func:`matofx.db.session.make_session_factory`). Call ``await engine.dispose()``
on shutdown — or use :func:`make_engine_scope` which guarantees disposal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

if TYPE_CHECKING:
    from matofx.config import DbSettings

_SQLITE_PREFIXES = ("sqlite:", "sqlite+aiosqlite:")
_POSTGRES_PREFIXES = ("postgres:", "postgresql:", "postgresql+asyncpg:")


class DbConfigError(ValueError):
    """Raised when database configuration is internally inconsistent.

    The message is plain English and tells the operator how to fix it.
    """


def make_engine(settings: DbSettings) -> AsyncEngine:
    """Create an async SQLAlchemy engine from validated DB settings.

    Args:
        settings: Validated database configuration. The dialect and URL
            must agree (see :func:`_async_url`).

    Returns:
        An :class:`AsyncEngine` ready for use with the session factory.

    Raises:
        DbConfigError: If the URL scheme doesn't match the declared dialect.
    """
    async_url = _async_url(settings)
    kwargs: dict[str, Any] = {
        "future": True,
        "echo": False,
        # Initial connect timeout (both dialects). Stops a misconfigured
        # DB from hanging app startup forever. ``timeout`` is the parameter
        # name accepted by both asyncpg (connection-attempt timeout) and
        # aiosqlite (lock-acquire timeout).
        "connect_args": {"timeout": settings.connect_timeout_seconds},
    }

    if settings.dialect == "sqlite":
        # SQLite has its own quirks. Per-connection setup happens via the
        # event listener installed below, so PRAGMAs apply to every checkout.
        kwargs["poolclass"] = NullPool if _is_sqlite_memory(async_url) else None
        # NullPool for in-memory because each connection sees its own DB
        # otherwise — pooling would defeat the test fixture.
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
    else:
        # Postgres pool tightening: defends against DB saturation,
        # idle-cut firewalls, and lost connections after the DB restarts.
        kwargs["pool_size"] = settings.pool_size
        kwargs["pool_timeout"] = settings.pool_timeout_seconds
        kwargs["max_overflow"] = settings.max_overflow
        kwargs["pool_recycle"] = settings.pool_recycle_seconds
        kwargs["pool_pre_ping"] = True

    engine = create_async_engine(async_url, **kwargs)

    if settings.dialect == "sqlite":
        _install_sqlite_pragmas(engine, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
    else:
        _install_postgres_setup(engine, statement_timeout_ms=settings.statement_timeout_ms)

    return engine


@asynccontextmanager
async def make_engine_scope(settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    """Async context manager wrapping :func:`make_engine` + guaranteed dispose.

    Use this in short-lived processes (admin CLI subcommands, ad-hoc
    scripts) where forgetting ``await engine.dispose()`` would leak
    pool connections. Long-running processes (the FastAPI app, the
    worker pool) build the engine once at startup and dispose at
    shutdown — for those, plain :func:`make_engine` is fine.

    Example:
        >>> async with make_engine_scope(settings.db) as engine:
        ...     factory = make_session_factory(engine)
        ...     async with session_scope(factory) as session:
        ...         ...
    """
    engine = make_engine(settings)
    try:
        yield engine
    finally:
        await engine.dispose()


def _async_url(settings: DbSettings) -> str:
    """Translate the friendly URL form into an async-driver URL.

    The URL is held as :class:`pydantic.SecretStr` in :class:`DbSettings`
    so it never leaks into ``repr()`` / ``model_dump()``; we unwrap it
    once here for the engine factory.
    """
    url = settings.url.get_secret_value()
    if settings.dialect == "sqlite":
        if not url.startswith(_SQLITE_PREFIXES):
            # Quote only the scheme prefix in the error so an embedded
            # password doesn't get echoed back to the operator's screen.
            scheme_end = url.find("://")
            prefix = url[: scheme_end + 3] if scheme_end != -1 else url[:32]
            raise DbConfigError(
                f"db.dialect is 'sqlite' but db.url ({prefix!r}) doesn't look like a "
                "SQLite URL. Expected something like 'sqlite:///./matofx.db'."
            )
        if url.startswith("sqlite+aiosqlite:"):
            return url
        return "sqlite+aiosqlite:" + url[len("sqlite:") :]

    # Postgres
    if not url.startswith(_POSTGRES_PREFIXES):
        scheme_end = url.find("://")
        prefix = url[: scheme_end + 3] if scheme_end != -1 else url[:32]
        raise DbConfigError(
            f"db.dialect is 'postgres' but db.url ({prefix!r}) doesn't look like a "
            "Postgres URL. Expected something like 'postgres://user:pass@host/db'."
        )
    if url.startswith("postgresql+asyncpg:"):
        return url
    if url.startswith("postgresql:"):
        return "postgresql+asyncpg:" + url[len("postgresql:") :]
    # bare 'postgres:' alias
    return "postgresql+asyncpg:" + url[len("postgres:") :]


def _is_sqlite_memory(async_url: str) -> bool:
    return ":memory:" in async_url or async_url.endswith("sqlite+aiosqlite://")


def _install_sqlite_pragmas(engine: AsyncEngine, *, busy_timeout_ms: int) -> None:
    """Apply per-connection PRAGMAs every time SQLite hands us a connection.

    - ``foreign_keys = ON`` — without this, SQLite silently ignores FK
      constraints. We rely on FKs everywhere; this is non-negotiable.
    - ``journal_mode = WAL`` — read concurrency under writes. The standard
      mode for any SQLite app with more than one writer or many readers.
    - ``busy_timeout`` — how long SQLite waits before raising "database is
      locked" on contention. Customer-tunable via
      :class:`DbSettings.sqlite_busy_timeout_ms`.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        finally:
            cursor.close()


def _install_postgres_setup(engine: AsyncEngine, *, statement_timeout_ms: int) -> None:
    """Apply per-session SET commands every time Postgres hands us a connection.

    - ``statement_timeout`` — server-side hard ceiling on query duration.
      Set to ``0`` in :class:`DbSettings` to disable.
    """
    if statement_timeout_ms <= 0:
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET statement_timeout = {statement_timeout_ms}")
        finally:
            cursor.close()
