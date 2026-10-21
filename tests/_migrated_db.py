"""One migrated SQLite template per session, copied per test.

Every conftest in this tree used to run ``alembic upgrade head`` and then
``downgrade base`` **per test**. With 90 migrations that is ~20-40 s of
pure setup for a test that asserts one thing, which is why the licence /
compliance / api suites take 5-30 minutes and why long runs get killed
before they finish.

The migrations still run — once — into a template file. Each test gets a
**byte copy** of that file, which is milliseconds. The properties that
matter are preserved:

- **Migrations are still exercised.** ``metadata.create_all()`` would be
  faster still, but it would stop testing the migration chain *and* skip
  everything the migrations seed (roles, permissions, the World team),
  which most of these tests depend on.
- **Isolation is per test.** A copy is a fresh database; a test that
  corrupts it affects nothing else. No ``downgrade base`` is needed —
  the copy is simply thrown away with ``tmp_path``.

**Postgres is deliberately untouched.** There is no cheap file copy for
a server database, and those runs are skipped unless
``MATOFX_TEST_POSTGRES_URL`` is set, so each Postgres test still migrates
from empty. It is reset by dropping the schema rather than by
``downgrade base``: a downgrade can rightly refuse to run over the data a
test left behind (0092 will not invent a customer name), and a refusal
there used to leave the database half-migrated for every test after it.
The downgrade chain is tested on its own, in ``tests/db/test_alembic.py``.

Usage in a conftest::

    from tests._migrated_db import migrated_template, sqlite_db_settings

    @pytest.fixture(params=[...])
    def my_db_settings(request, tmp_path, migrated_template):
        if request.param == "sqlite":
            yield sqlite_db_settings(migrated_template, tmp_path, "my.db")
            return
        ...  # postgres: unchanged
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from matofx.config import DbSettings
from matofx.db import make_engine

if TYPE_CHECKING:
    from collections.abc import Iterator

#: SQLite in WAL mode (which :mod:`matofx.db.engine` enables) keeps part
#: of the database in sidecar files. Copying only the ``.db`` would
#: silently lose the tail of the migration run, so all three travel
#: together.
_SQLITE_SIDECARS = ("", "-wal", "-shm")


def alembic_config(url: str) -> Config:
    """An Alembic config pointed at ``url`` — the shape every conftest built."""
    repo_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "src" / "matofx" / "db" / "migrations"))
    cfg.cmd_opts = type("Args", (), {"x": [f"url={url}"]})()
    return cfg


def sync_url(settings: DbSettings) -> str:
    """The sync driver URL Alembic needs, from async-flavoured settings."""
    raw = settings.url.get_secret_value()
    if settings.dialect == "sqlite":
        if raw.startswith("sqlite+aiosqlite:"):
            return "sqlite:" + raw[len("sqlite+aiosqlite:") :]
        return raw
    if raw.startswith("postgresql+asyncpg:"):
        return "postgresql:" + raw[len("postgresql+asyncpg:") :]
    if raw.startswith("postgres:"):
        return "postgresql:" + raw[len("postgres:") :]
    return raw


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fully-migrated SQLite database, built once for the whole session.

    Session-scoped on purpose: this is the expensive thing, and it is
    read-only from here on — tests copy it, never open it.
    """
    template = tmp_path_factory.mktemp("matofx-template") / "template.db"
    command.upgrade(alembic_config(f"sqlite:///{template}"), "head")
    return template


def sqlite_db_settings(template: Path, tmp_path: Path, name: str = "test.db") -> DbSettings:
    """Copy the migrated template into ``tmp_path`` and point settings at it."""
    destination = tmp_path / name
    for suffix in _SQLITE_SIDECARS:
        source = template.with_name(template.name + suffix)
        if source.exists():
            shutil.copyfile(source, destination.with_name(destination.name + suffix))
    return DbSettings.from_url(f"sqlite:///{destination}", dialect="sqlite")


def copy_template_to(template: Path, destination: Path) -> None:
    """Copy the migrated template to an explicit path (schema only, no settings).

    For tests that need a migrated database *at a path they chose* —
    the cross-install migrate end-to-end test builds a source and a
    destination install side by side.
    """
    for suffix in _SQLITE_SIDECARS:
        source = template.with_name(template.name + suffix)
        if source.exists():
            shutil.copyfile(source, destination.with_name(destination.name + suffix))


def reset_postgres_schema(settings: DbSettings) -> None:
    """Drop everything in ``public`` — tables, types, Alembic's version row.

    Through the app's own async engine: asyncpg is the only Postgres driver
    installed (``psycopg2`` is LGPL-3, see ``migrations/env.py``).
    """

    async def _reset() -> None:
        engine = make_engine(settings)
        try:
            async with engine.begin() as conn:
                await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
                await conn.execute(sa.text("CREATE SCHEMA public"))
        finally:
            await engine.dispose()

    asyncio.run(_reset())


def postgres_db_settings() -> Iterator[DbSettings]:
    """Per-test migrate from empty against a real Postgres.

    Skips when ``MATOFX_TEST_POSTGRES_URL`` is unset, which is the normal
    case on a developer box. The schema is reset on the way in as well as
    on the way out, so a test whose teardown failed cannot leave its rows
    to the next one.
    """
    import os  # noqa: PLC0415

    pg_url = os.environ.get("MATOFX_TEST_POSTGRES_URL")
    if not pg_url:
        pytest.skip("MATOFX_TEST_POSTGRES_URL not set")
    settings = DbSettings.from_url(pg_url, dialect="postgres")
    reset_postgres_schema(settings)
    command.upgrade(alembic_config(sync_url(settings)), "head")
    try:
        yield settings
    finally:
        reset_postgres_schema(settings)
