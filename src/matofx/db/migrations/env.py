"""Alembic environment for MATOFX migrations.

This module is imported by Alembic when running ``alembic upgrade``,
``alembic downgrade``, or ``alembic revision --autogenerate``. The
target metadata is :data:`matofx.db.metadata.metadata`, which collects
all SQLAlchemy ORM models registered in the application.

The database URL is sourced, in order:

1. ``-x url=...`` Alembic command-line override (used by tests).
2. The ``sqlalchemy.url`` set in ``alembic.ini`` (operators rarely use this).
3. :func:`matofx.config.load_settings` — the running configuration.

Migrations run **asynchronously** against the same drivers the
application uses (``aiosqlite`` for SQLite, ``asyncpg`` for Postgres).
Running async migrations through SQLAlchemy 2.x's
:class:`AsyncEngine` lets us keep the dependency footprint small —
no separate sync driver (``psycopg2``) is needed, which matters for
licence procurement (``psycopg2`` is LGPL-3; ``asyncpg`` is Apache-2).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from matofx.db.metadata import metadata

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False` is not cosmetic. `fileConfig`
    # defaults to **True**, which switches off every logger that already
    # exists and isn't named in alembic.ini — i.e. the entire `matofx.*`
    # tree, whenever migrations run in a process that has already
    # imported the app. `matofx migrate` and the demo launcher's
    # auto-upgrade both do exactly that, so a migration silently took the
    # application's logging out for the rest of the process.
    #
    # It surfaced as a test that only failed when it ran *after* anything
    # that migrated a database, which read as an ordering quirk in the
    # test rather than as the application-visible defect it is.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = metadata


def _resolved_url() -> str:
    """Resolve the URL to migrate against, preferring -x url=... when given."""
    x_args = context.get_x_argument(as_dictionary=True)
    if "url" in x_args:
        return _to_async_url(x_args["url"])

    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return _to_async_url(ini_url)

    from matofx.config import load_settings  # noqa: PLC0415 — lazy to avoid import cycles

    settings = load_settings()
    return _to_async_url(settings.db.url.get_secret_value())


def _to_async_url(url: str) -> str:
    """Translate friendly URLs into the async-driver form Alembic uses here."""
    if url.startswith("sqlite+aiosqlite:") or url.startswith("postgresql+asyncpg:"):
        return url
    if url.startswith("sqlite:"):
        return "sqlite+aiosqlite:" + url[len("sqlite:") :]
    if url.startswith("postgresql:"):
        return "postgresql+asyncpg:" + url[len("postgresql:") :]
    if url.startswith("postgres:"):
        return "postgresql+asyncpg:" + url[len("postgres:") :]
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a real database."""
    url = _resolved_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """Run migrations against a live async database connection."""
    url = _resolved_url()
    section: dict[str, Any] = dict(config.get_section(config.config_ini_section, {}))
    section["sqlalchemy.url"] = url
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Async-run-migrations dispatcher."""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
