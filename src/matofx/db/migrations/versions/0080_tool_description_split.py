"""Split the tool descriptor's ``description`` into short + long.

Revision ID: 0080
Revises: 0079
Create Date: 2026-06-16

The tool descriptor used to carry a single ``description`` string. It now
carries a mandatory ``short_description`` (card-sized, capped at 200 chars)
plus an optional ``long_description`` (fuller, run-page / info-dialog text).

The description is *not* a column — it lives inside the ``descriptor`` JSON
blob on ``tool`` and is snapshotted into ``tool_version.descriptor_snapshot``.
So there's no ``ALTER TABLE``; this migration rewrites those JSON blobs in
place, renaming the key:

* ``len <= 200`` → text becomes ``short_description``; no long form.
* ``len > 200``  → ``short_description`` is an ellipsised 200-char head and
  the full text moves to ``long_description`` (lossless).

It mirrors :meth:`ToolDescriptor._back_compat_description` exactly, so a blob
loads identically whether or not it was rewritten on disk (old export/import
bundles and any blob this migration misses still resolve via that validator).

The per-version ``changes`` audit map is left untouched — a historical
``{"description": {...}}`` entry is an accurate record of what changed at the
time and shouldn't be rewritten.

Downgrade folds the two keys back into a single ``description`` (preferring
the long form when present), so the schema round-trips.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0080"
down_revision: str | None = "0079"
branch_labels: str | None = None
depends_on: str | None = None

_SHORT_MAX = 200

# Lightweight typed-column views so SQLAlchemy (de)serialises the JSON blob
# per dialect on both read and write — no manual json.loads/dumps needed.
_tool = sa.table("tool", sa.column("id", sa.Integer), sa.column("descriptor", sa.JSON))
_tool_version = sa.table(
    "tool_version",
    sa.column("id", sa.Integer),
    sa.column("descriptor_snapshot", sa.JSON),
)


def _split(blob: Any) -> dict[str, Any] | None:
    """Return a rewritten copy when ``blob`` needs splitting, else ``None``."""
    if not isinstance(blob, dict):
        return None
    if "short_description" in blob or "description" not in blob:
        return None
    legacy = str(blob.get("description") or "")
    out = dict(blob)
    out.pop("description", None)
    if len(legacy) <= _SHORT_MAX:
        out["short_description"] = legacy
    else:
        out["short_description"] = legacy[: _SHORT_MAX - 1].rstrip() + "…"
        out.setdefault("long_description", legacy)
    return out


def _merge(blob: Any) -> dict[str, Any] | None:
    """Inverse of :func:`_split` — fold the split keys back to ``description``."""
    if not isinstance(blob, dict):
        return None
    if "short_description" not in blob and "long_description" not in blob:
        return None
    out = dict(blob)
    long = out.pop("long_description", None)
    short = out.pop("short_description", None)
    out["description"] = str(long or short or "")
    return out


def _rewrite(
    table: sa.TableClause,
    id_col: sa.ColumnClause[Any],
    json_col: sa.ColumnClause[Any],
    transform: Any,
) -> None:
    """Apply ``transform`` to every row's JSON blob, writing back only on change."""
    conn = op.get_bind()
    for row in conn.execute(sa.select(id_col, json_col)).fetchall():
        rewritten = transform(row[1])
        if rewritten is not None:
            conn.execute(table.update().where(id_col == row[0]).values({json_col.name: rewritten}))


def upgrade() -> None:
    """Rewrite stored descriptor blobs from ``description`` to short + long."""
    _rewrite(_tool, _tool.c.id, _tool.c.descriptor, _split)
    _rewrite(_tool_version, _tool_version.c.id, _tool_version.c.descriptor_snapshot, _split)


def downgrade() -> None:
    """Fold ``short_description`` / ``long_description`` back to ``description``."""
    _rewrite(_tool, _tool.c.id, _tool.c.descriptor, _merge)
    _rewrite(_tool_version, _tool_version.c.id, _tool_version.c.descriptor_snapshot, _merge)
