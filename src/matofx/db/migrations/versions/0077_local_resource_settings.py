"""Seed the local resource-budget + GPU-concurrency runtime settings.

Revision ID: 0077
Revises: 0076
Create Date: 2026-06-12

Part A of the execution-scheduling rework (``designs/execution-scheduling.md``)
adds four admin-tunable runner knobs so a super-user can shape local
admission without a restart:

- ``gpu_concurrency`` — max concurrent *local GPU* jobs (default 1).
- ``local_cpu_cores`` — CPU-core budget for local jobs (0 = auto-detect).
- ``local_memory_mb`` — memory budget for local jobs (0 = auto-detect).
- ``local_memory_reserve_mb`` — RAM held back from the auto-detected
  memory budget for the OS + MATOFX itself (default 2048).

The catalog + ``RuntimeSettings`` fields land in code
(``matofx.runner.settings``); this revision seeds the persisted
``runtime_setting`` rows so the values are editable from the admin UI
exactly like the other runner knobs. The loader already falls back to
the catalog default when a row is absent, so the seed is idempotent and
greenfield-safe — each row is inserted only if missing.

``runtime_setting`` is keyed ``(domain, key)`` since migration 0026; the
rows go in the ``runner`` domain. Values are JSON-encoded (migration
0012's seed convention).
"""

from __future__ import annotations

import datetime as _dt
import json

import sqlalchemy as sa
from alembic import op

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | None = None
depends_on: str | None = None

_DOMAIN = "runner"
_DEFAULTS: tuple[tuple[str, int], ...] = (
    ("gpu_concurrency", 1),
    ("local_cpu_cores", 0),
    ("local_memory_mb", 0),
    ("local_memory_reserve_mb", 2048),
)


def upgrade() -> None:
    """Seed each new runner setting row if it isn't already present."""
    bind = op.get_bind()
    table = sa.table(
        "runtime_setting",
        sa.column("domain", sa.String()),
        sa.column("key", sa.String()),
        sa.column("value_json", sa.Text()),
        sa.column("updated_by_user_id", sa.Integer()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = _dt.datetime.now(tz=_dt.UTC)
    for key, default in _DEFAULTS:
        exists = bind.execute(
            sa.text(
                "SELECT 1 FROM runtime_setting WHERE domain = :d AND key = :k"
            ).bindparams(d=_DOMAIN, k=key)
        ).fetchone()
        if exists is not None:
            continue
        op.bulk_insert(
            table,
            [
                {
                    "domain": _DOMAIN,
                    "key": key,
                    "value_json": json.dumps(default),
                    "updated_by_user_id": None,
                    "updated_at": now,
                }
            ],
        )


def downgrade() -> None:
    """Remove the seeded rows (the loader falls back to the catalog default)."""
    for key, _ in _DEFAULTS:
        op.execute(
            sa.text(
                "DELETE FROM runtime_setting WHERE domain = :d AND key = :k"
            ).bindparams(d=_DOMAIN, k=key)
        )
