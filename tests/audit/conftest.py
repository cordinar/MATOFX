"""Shared fixtures for matofx.audit tests.

Provides:

- ``tmp_signing_key_path`` — a fresh Ed25519 keyfile per test.
- ``audit_engine`` — an async SQLite engine with the audit tables created
  via Alembic migrations. Postgres equivalents inherit ``db_settings``
  from ``tests/db/conftest.py`` and run their own migration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import generate_signing_key
from matofx.config import DbSettings
from matofx.db import make_engine
from tests._migrated_db import postgres_db_settings, sqlite_db_settings


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def audit_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    """Per-dialect DbSettings with a migrated DB.

    SQLite copies the session-scoped template (milliseconds); Postgres
    migrates per test as before. See :mod:`tests._migrated_db`.
    """
    if request.param == "sqlite":
        yield sqlite_db_settings(migrated_template, tmp_path, "audit.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def audit_engine(audit_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(audit_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def tmp_signing_key_path(tmp_path: Path) -> Path:
    keyfile = tmp_path / "signing.key"
    generate_signing_key(keyfile)
    return keyfile
