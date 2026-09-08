"""Async session factory and session-scope helper.

A *session factory* is the application's anchor point for short-lived
database conversations. Build one at startup with :func:`make_session_factory`,
then use :func:`session_scope` (or the factory directly) anywhere a unit
of work needs the database.

The session-scope context manager handles the rollback/commit dance: a
session that exits normally is committed; a session that exits via an
exception is rolled back; the underlying connection is always returned
to the pool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


SessionFactory = async_sessionmaker[AsyncSession]


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    """Build the per-process async session factory."""
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )


@asynccontextmanager
async def session_scope(factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """Run a unit of work in a managed session.

    Commits on normal exit, rolls back on exception, always closes.

    Example:
        >>> async with session_scope(factory) as session:
        ...     session.add(some_record)
    """
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
