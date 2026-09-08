"""Seed the ``study_concurrency_cap`` runtime setting (design §6).

Revision ID: 0037
Revises: 0036
Create Date: 2026-05-18

Parametric studies add one admin-tunable runner knob — the per-study
concurrency cap (at most N children of one study running at once, so a
large sweep can't seize every worker; design §6 / ADR-0001). The
catalog + ``RuntimeSettings`` field land in code
(``matofx.runner.settings``); this revision seeds the persisted
``runtime_setting`` row so the value is editable from the admin UI
exactly like the other runner knobs (the loader already falls back to
the catalog default when the row is absent, so this is idempotent and
greenfield-safe).

``runtime_setting`` is keyed ``(domain, key)`` since migration 0026;
the row goes in the ``runner`` domain. Value is JSON-encoded like
every other row (migration 0012's seed convention).
"""

from __future__ import annotations

import datetime as _dt
import json

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | None = None
depends_on: str | None = None

_DOMAIN = "runner"
_KEY = "study_concurrency_cap"
_DEFAULT = 10


def upgrade() -> None:
    """Seed the study-concurrency-cap row if it isn't already present."""
    bind = op.get_bind()
    exists = bind.execute(
        sa.text("SELECT 1 FROM runtime_setting WHERE domain = :d AND key = :k").bindparams(
            d=_DOMAIN, k=_KEY
        )
    ).fetchone()
    if exists is not None:
        return
    op.bulk_insert(
        sa.table(
            "runtime_setting",
            sa.column("domain", sa.String()),
            sa.column("key", sa.String()),
            sa.column("value_json", sa.Text()),
            sa.column("updated_by_user_id", sa.Integer()),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        ),
        [
            {
                "domain": _DOMAIN,
                "key": _KEY,
                "value_json": json.dumps(_DEFAULT),
                "updated_by_user_id": None,
                "updated_at": _dt.datetime.now(tz=_dt.UTC),
            }
        ],
    )


def downgrade() -> None:
    """Remove the seeded row (the loader falls back to the catalog default)."""
    op.execute(
        sa.text("DELETE FROM runtime_setting WHERE domain = :d AND key = :k").bindparams(
            d=_DOMAIN, k=_KEY
        )
    )
