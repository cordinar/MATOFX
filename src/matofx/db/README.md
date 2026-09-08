# `matofx.db` — async data access layer

Single source of database connectivity for everything that needs to outlive a process. SQLite for tiny / dev installs, Postgres for medium installs — same code path, driver chosen at config-load time.

## Why this module exists

Per §A3 / §B8 of the brief, the deployment range is 5 users (single box, SQLite is fine) to ~200 concurrent users (real Postgres). The application code must not care which is in use; the engine is selected at config-load time.

Per §A5, the DB is a logical component reachable over the network — no in-process trickery, no SQLite-specific assumptions baked into business logic.

## Public surface

Long-running processes (FastAPI app, worker pool) build the engine once at startup:

```python
from matofx.config import load_settings
from matofx.db import make_engine, make_session_factory, session_scope

settings = load_settings()
engine = make_engine(settings.db)
factory = make_session_factory(engine)

async with session_scope(factory) as session:
    ...  # commits on normal exit, rolls back on exception
# at shutdown:
await engine.dispose()
```

Short-lived processes (admin CLI, ad-hoc scripts) use the scope wrapper that guarantees disposal:

```python
from matofx.db import make_engine_scope, make_session_factory, session_scope

async with make_engine_scope(settings.db) as engine:
    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        ...
# engine.dispose() ran automatically; pool connections returned.
```

## Design notes

- **Async via SQLAlchemy 2.x.** SQLite uses ``aiosqlite``; Postgres uses ``asyncpg``. The customer writes the friendly URL form (``sqlite:///...`` or ``postgres://...``) in ``matofx.toml``; ``make_engine`` translates it.
- **One engine per process.** Build it at startup, dispose it at shutdown.
- **Session-per-unit-of-work.** ``session_scope`` is the canonical pattern. Don't share a session across requests.
- **No raw SQL outside this package.** Feature code goes through SQLAlchemy ORM models. Use raw ``text(...)`` only inside ``matofx.db`` (engine factory health checks) and inside Alembic migrations. Anywhere else, use ``select(...)`` / ``update(...)`` / ``delete(...)``.

## Connection-pool tightening (Postgres)

Settings in ``DbSettings`` (Postgres path; ignored for SQLite):

| Field | Default | Why |
|---|---|---|
| ``pool_size`` | 10 | Steady-state connection count |
| ``pool_timeout_seconds`` | 30 | How long callers wait for a free connection before failing fast — defends against pile-up behind a saturated DB |
| ``max_overflow`` | 10 | Connections allowed temporarily above ``pool_size`` during bursts |
| ``pool_recycle_seconds`` | 1800 | Recycle connections older than this — defends against idle-cut firewalls (typical default ~30 min) |
| ``connect_timeout_seconds`` | 10 | Initial-connect timeout for both dialects — stops a misconfigured DB from hanging app startup forever |

``pool_pre_ping=True`` is always on for Postgres so the engine recovers transparently from a server restart.

## Per-connection dialect setup

- **SQLite** — ``PRAGMA foreign_keys = ON``, ``PRAGMA journal_mode = WAL``, ``PRAGMA busy_timeout = <ms>`` (from ``DbSettings.sqlite_busy_timeout_ms``). Applied via SQLAlchemy ``connect`` event; runs every checkout.
- **Postgres** — ``SET statement_timeout = <ms>`` (from ``DbSettings.statement_timeout_ms``); 0 disables.

## Two-engine discipline

CI runs every test against both backends. Anything that works on SQLite but not Postgres (or vice versa) is a bug, not a "won't fix on the small backend." If a feature genuinely needs Postgres-only capability, gate it behind a config flag and have the SQLite path return a clear "not supported on this backend" error — never a silent degradation.

## Migrations

Alembic, configured via ``alembic.ini`` and the env in ``src/matofx/db/migrations/env.py``. The URL is sourced from the running ``matofx.toml`` configuration, or overridable per-invocation with ``-x url=...``. Run from ````:

```bash
alembic upgrade head                          # apply all migrations
alembic upgrade head -x url="sqlite:///./tmp.db"  # explicit URL override
alembic revision --autogenerate -m "add foo"
```

Alembic uses **sync drivers** (plain ``sqlite``, ``psycopg2`` for Postgres in CI) — async migrations buy nothing and complicate Alembic. The application stays async; only the migration runner is sync.

The shared :class:`sqlalchemy.MetaData` instance lives in :mod:`matofx.db.metadata` with an explicit naming convention so constraint names are stable and identical on both dialects.

## Phase 0 status

Engine factory, session factory, session-scope helper, Alembic scaffolding with a no-op baseline migration, tests for both dialects. Domain models (``models/``) and repository helpers (``repos/``) land in Phase 1 alongside the project / scenario / job hierarchy.
