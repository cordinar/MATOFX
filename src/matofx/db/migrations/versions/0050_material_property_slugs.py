"""Materials: immutable snake_case slugs for class + version properties.

Revision ID: 0050
Revises: 0049
Create Date: 2026-05-26

Introduces a separate **immutable identifier** for every material-class
property (and the per-version property rows that snapshot them).
``prop_name`` stays as the **editable display label** ("Thermal
Conductivity"); the new ``slug`` is the **stable snake_case key**
("thermal_conductivity") that every tool descriptor's
``required_properties`` references and that keys the frozen
``properties`` dict in ``inputs.json``. Decouples wire format from UI
copy: a curator can rename "Specific Heat" → "Heat Capacity" without
breaking any tool that already looked up the slug.

Backfill: derives each existing row's slug from its ``prop_name`` via a
small in-Python slugifier (lowercase + non-alphanumeric runs → ``_`` +
strip + suffix on collision). The migration must not import the ORM, so
the slugify is inlined.

SQLite can't add a UNIQUE constraint in place, so the unique pair is
created via a recreate-style ``batch_alter_table``.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | None = None
depends_on: str | None = None


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Lowercase + collapse non-alphanumeric runs to underscores + trim.

    Empty / all-punctuation inputs fall back to ``"property"`` so the
    backfill never produces an empty slug (the column will be NOT NULL).
    The de-duplicator that calls this then suffixes ``_2`` / ``_3`` /
    … on collision within a group.
    """
    base = _NON_ALNUM.sub("_", (name or "").strip().lower()).strip("_")
    return (base or "property")[:60]  # leave 4 chars for "_NN" suffix


def _backfill_slugs(conn: sa.Connection, table: str, group_col: str) -> None:
    """Compute a unique slug per row within each ``group_col`` bucket."""
    # ``table`` / ``group_col`` are migration-internal literals, never user input.
    rows = conn.execute(
        sa.text(f"SELECT id, {group_col}, prop_name FROM {table} ORDER BY {group_col}, id")  # noqa: S608
    ).fetchall()
    used_per_group: dict[int, set[str]] = {}
    for row in rows:
        row_id, group_id, prop_name = row[0], int(row[1]), str(row[2] or "")
        taken = used_per_group.setdefault(group_id, set())
        base = _slugify(prop_name)
        slug = base
        n = 2
        while slug in taken:
            slug = f"{base}_{n}"
            n += 1
        taken.add(slug)
        conn.execute(
            sa.text(f"UPDATE {table} SET slug = :slug WHERE id = :id"),  # noqa: S608
            {"slug": slug, "id": row_id},
        )


def upgrade() -> None:
    """Add ``slug`` to material_class_property + material_property and backfill."""
    # 1. Add nullable slug columns first so the backfill can populate them.
    with op.batch_alter_table("material_class_property", recreate="never") as batch:
        batch.add_column(sa.Column("slug", sa.String(length=64), nullable=True))
    with op.batch_alter_table("material_property", recreate="never") as batch:
        batch.add_column(sa.Column("slug", sa.String(length=64), nullable=True))

    # 2. Backfill from prop_name, with per-group de-dup.
    conn = op.get_bind()
    _backfill_slugs(conn, "material_class_property", group_col="class_id")
    _backfill_slugs(conn, "material_property", group_col="material_version_id")

    # 3. Make NOT NULL + add UNIQUE per group. SQLite can't ALTER an
    #    existing column to NOT NULL or add a UNIQUE in place, so a
    #    recreate-style batch is used (mirrors migration 0049's pattern).
    with op.batch_alter_table("material_class_property", recreate=recreate_on_sqlite()) as batch:
        batch.alter_column("slug", existing_type=sa.String(length=64), nullable=False)
        batch.create_unique_constraint("uq_material_class_property_slug", ["class_id", "slug"])
    with op.batch_alter_table("material_property", recreate=recreate_on_sqlite()) as batch:
        batch.alter_column("slug", existing_type=sa.String(length=64), nullable=False)
        batch.create_unique_constraint("uq_material_property_slug", ["material_version_id", "slug"])


def downgrade() -> None:
    """Drop the slug columns + the UNIQUE constraints they back."""
    with op.batch_alter_table("material_property", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("uq_material_property_slug", type_="unique")
        batch.drop_column("slug")
    with op.batch_alter_table("material_class_property", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("uq_material_class_property_slug", type_="unique")
        batch.drop_column("slug")
