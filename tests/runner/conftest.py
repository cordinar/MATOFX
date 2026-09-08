"""Shared fixtures for runner tests.

Reuses the alembic-up / alembic-down pattern from
``tests/tools/conftest.py`` so the runner exercises the real
schema, not a hand-rolled subset.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter
from matofx.config import DbSettings
from matofx.db import make_engine, make_session_factory
from matofx.db.session import SessionFactory
from matofx.kpi import orm as _kpi_orm  # noqa: F401  -- registers job_kpi_value
from matofx.vv import (
    orm as _vv_orm,  # noqa: F401  -- registers V&V tables (approval_request FK targets)
)
from tests._migrated_db import postgres_db_settings, sqlite_db_settings


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def runner_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    if request.param == "sqlite":
        # A byte copy of the session-scoped migrated template —
        # milliseconds, where migrating per test cost ~30 s.
        yield sqlite_db_settings(migrated_template, tmp_path, "r.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def runner_engine(runner_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(runner_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def runner_factory(runner_engine: AsyncEngine) -> SessionFactory:
    return make_session_factory(runner_engine)


@pytest.fixture
def runner_audit() -> AuditWriter:
    return AuditWriter(signing_key=None, key_id=None)
