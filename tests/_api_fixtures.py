"""Shared fixtures for the HTTP API + UI tests.

Defined in a regular module (not a conftest) so multiple sibling
conftests can re-export them via ``from tests._api_fixtures import *``
without pytest registering the same conftest twice.

Builds an alembic-migrated SQLite (or Postgres, when configured), a
super-user, an active StorageLocation, and an installed copy of the
``material_properties`` example tool — the minimum to exercise every
API surface end-to-end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.api.app import make_app
from matofx.api.deps import get_audit, get_factory, get_settings
from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import (
    AuditSettings,
    AuthSettings,
    DbSettings,
    LogSettings,
    Settings,
    ToolsSettings,
)
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.db.session import SessionFactory
from matofx.studies import (
    orm as _studies_orm,  # noqa: F401  -- registers ParametricStudy in the shared MetaData so Job.parametric_study_id's FK resolves regardless of collection order
)
from matofx.tools.exec_orm import StorageLocation
from matofx.tools.loader import install_from_path
from matofx.tools.service import make_tool_service
from matofx.users import make_password_hasher, make_user_service
from matofx.workflows import (
    orm as _workflow_orm,  # noqa: F401  -- registers WorkflowRun in the shared MetaData so Job.workflow_run_id's FK resolves regardless of collection order
)
from tests._migrated_db import postgres_db_settings, sqlite_db_settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE_TOOLS = _REPO_ROOT / "example_tools"

_TEST_PASSWORD = "testpassword123"


def _sync_url(s: DbSettings) -> str:
    raw = s.url.get_secret_value()
    if s.dialect == "sqlite":
        return raw
    if raw.startswith("postgresql+asyncpg:"):
        return "postgresql:" + raw[len("postgresql+asyncpg:") :]
    if raw.startswith("postgres:"):
        return "postgresql:" + raw[len("postgres:") :]
    return raw


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def api_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    if request.param == "sqlite":
        # A byte copy of the session-scoped migrated template —
        # milliseconds, where migrating per test cost ~30 s.
        yield sqlite_db_settings(migrated_template, tmp_path, "api.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
def api_settings(api_db_settings: DbSettings, tmp_path: Path) -> Settings:
    # Tools-bundle staging needs a writable directory; per-test
    # tmp_path keeps each test's uploads isolated.
    bundles_dir = tmp_path / "tool-bundles"
    bundles_dir.mkdir()
    return Settings(
        environment="test",
        log=LogSettings(),
        db=api_db_settings,
        audit=AuditSettings(),
        auth=AuthSettings(
            argon2_time_cost=1,
            argon2_memory_cost_kib=8192,
            argon2_parallelism=1,
            password_min_length=8,
        ),
        tools=ToolsSettings(bundles_dir=bundles_dir),
    )


@pytest.fixture
async def api_engine(api_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(api_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def api_factory(api_engine: AsyncEngine) -> SessionFactory:
    return make_session_factory(api_engine)


@pytest.fixture
def api_audit() -> AuditWriter:
    return AuditWriter(signing_key=None, key_id=None)


@pytest.fixture
async def seeded(
    api_factory: SessionFactory,
    api_settings: Settings,
    api_audit: AuditWriter,
    tmp_path: Path,
) -> dict[str, Any]:
    """Seed super-user + StorageLocation + installed material_properties tool."""
    hasher = make_password_hasher(api_settings.auth)
    user_service = make_user_service(api_settings.auth, api_audit, hasher=hasher)
    tool_service = make_tool_service(api_audit)
    mount = tmp_path / "mount"
    mount.mkdir()

    async with session_scope(api_factory) as session:
        await ensure_chain_head(session)
        user = await user_service.create_user(
            session,
            username="root",
            email="root@example.com",
            password=_TEST_PASSWORD,
            full_name="Root User",
            role="super_user",
        )
        location = StorageLocation(name="local", mount_path=str(mount))
        session.add(location)
        await session.flush()
        location_id = int(location.id)
        installed = await install_from_path(
            session,
            tool_service,
            actor_user_id=user.id,
            bundle_dir=_EXAMPLE_TOOLS / "material_properties",
        )
        await tool_service.promote_state(
            session,
            actor_user_id=user.id,
            alias=installed.alias,
            new_state="test",
        )
        await tool_service.promote_state(
            session,
            actor_user_id=user.id,
            alias=installed.alias,
            new_state="released",
        )
        return {
            "user_id": user.id,
            "username": user.username,
            "password": _TEST_PASSWORD,
            "tool_alias": installed.alias,
            "storage_location_id": location_id,
            "mount_path": mount,
        }


@pytest.fixture
def app(
    api_settings: Settings,
    api_factory: SessionFactory,
    api_audit: AuditWriter,
) -> FastAPI:
    """Construct the app and override the deps to use the test fixtures."""
    application = make_app(api_settings)
    application.state.settings = api_settings
    application.state.factory = api_factory
    application.state.audit = api_audit
    application.dependency_overrides[get_settings] = lambda: api_settings
    application.dependency_overrides[get_factory] = lambda: api_factory
    application.dependency_overrides[get_audit] = lambda: api_audit
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """In-process httpx client speaking ASGI directly to the app."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def auth_token(client: httpx.AsyncClient, seeded: dict[str, Any]) -> str:
    """Log in via the API and return a bearer token."""
    response = await client.post(
        "/api/auth/login",
        json={"username": seeded["username"], "password": seeded["password"]},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


@pytest.fixture
def auth_headers(auth_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_token}"}
