"""Shared helper for the migration scripts' ``batch_alter_table`` calls."""

from __future__ import annotations

from typing import Literal

from alembic import op


def recreate_on_sqlite() -> Literal["always", "auto"]:
    """``recreate=`` for a batch that must rebuild the table on SQLite only.

    SQLite can't add a CHECK or a constraint to an existing table, so those
    batches rebuild it. Postgres can, with ``ALTER TABLE``, and rebuilding
    there fails instead: dropping the old table's constraints trips over the
    foreign keys that point at them.
    """
    return "always" if op.get_bind().dialect.name == "sqlite" else "auto"
