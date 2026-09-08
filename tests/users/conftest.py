"""Shared fixtures for matofx.users tests.

Provides:

- ``users_db_settings`` — parametrised over both dialects, with the
  full migration chain (0001 → 0003) applied.
- ``users_engine`` — async engine on top of that.
- ``audit_writer`` — unsigned, in-memory writer suitable for tests.
- ``auth_settings`` — a fast :class:`AuthSettings` (low Argon2 cost,
  short timeouts) so tests run quickly without losing realism.
- ``user_service``, ``session_service`` — wired up against the fixtures.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter
from matofx.config import AuthSettings, DbSettings
from matofx.db import make_engine
from matofx.users import (
    SessionService,
    UserService,
    make_password_hasher,
    make_session_service,
    make_user_service,
)
from tests._migrated_db import postgres_db_settings, sqlite_db_settings


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def users_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    if request.param == "sqlite":
        # A byte copy of the session-scoped migrated template —
        # milliseconds, where migrating per test cost ~30 s.
        yield sqlite_db_settings(migrated_template, tmp_path, "users.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def users_engine(users_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(users_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def auth_settings() -> AuthSettings:
    """Test-friendly AuthSettings — low Argon2 cost so the suite runs fast."""
    return AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
        session_idle_timeout_minutes=30,
        session_hard_timeout_hours=12,
        failed_login_lockout_threshold=3,
        failed_login_lockout_minutes=5,
    )


@pytest.fixture
def audit_writer() -> AuditWriter:
    return AuditWriter(signing_key=None, key_id=None)


@pytest.fixture
def user_service(auth_settings: AuthSettings, audit_writer: AuditWriter) -> UserService:
    return make_user_service(
        auth_settings, audit_writer, hasher=make_password_hasher(auth_settings)
    )


@pytest.fixture
def session_service(auth_settings: AuthSettings, audit_writer: AuditWriter) -> SessionService:
    return make_session_service(auth_settings, audit_writer)
