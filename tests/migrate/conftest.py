"""Shared fixtures for ``matofx.migrate`` integration tests.

Provides:

- Parametrised SQLite + Postgres engine fixtures (Postgres skips when
  ``MATOFX_TEST_POSTGRES_URL`` isn't set), matching the convention
  used by every other domain's tests in this tree.
- An unsigned :class:`AuditWriter`.
- :class:`TrustedPeerService` wired to that writer.
- ``seed_admin_user_id`` — a single administrator user seeded into the
  freshly migrated DB so the ``added_by_user_id`` FK on
  ``migrate_trusted_peer`` has a real row to reference.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import AuthSettings, DbSettings
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.migrate.trust import TrustedPeerService, make_trusted_peer_service
from matofx.users import UserService, make_password_hasher, make_user_service
from tests._migrated_db import postgres_db_settings, sqlite_db_settings


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def migrate_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    if request.param == "sqlite":
        # Byte copy of the session template (see tests/_migrated_db.py).
        yield sqlite_db_settings(migrated_template, tmp_path, "m.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def migrate_engine(
    migrate_db_settings: DbSettings,
) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(migrate_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def audit_writer() -> AuditWriter:
    return AuditWriter(signing_key=None, key_id=None)


@pytest.fixture
def auth_settings() -> AuthSettings:
    return AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )


@pytest.fixture
def user_service(auth_settings: AuthSettings, audit_writer: AuditWriter) -> UserService:
    return make_user_service(
        auth_settings, audit_writer, hasher=make_password_hasher(auth_settings)
    )


@pytest.fixture
def trust_service(audit_writer: AuditWriter) -> TrustedPeerService:
    return make_trusted_peer_service(audit_writer)


@pytest.fixture
async def seed_admin_user_id(migrate_engine: AsyncEngine, user_service: UserService) -> int:
    """One administrator user seeded so the ``added_by_user_id`` FK is satisfiable."""
    factory = make_session_factory(migrate_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="ada",
            email="ada@example.com",
            password="securepassword1",
            full_name="Ada Admin",
            role="administrator",
        )
        return user.id
