"""Project metadata: number formats (int/decimal/pattern) + value_text.

Revision ID: 0042
Revises: 0041
Create Date: 2026-05-21

Lets a ``number`` metadata field declare how it is entered:

- ``project_metadata_field.number_format`` — ``integer`` / ``decimal``
  / ``pattern`` (``NULL`` ⇒ ``decimal`` for the fields that predate
  this column). Validity is enforced in the service layer (no DB CHECK,
  to avoid recreating a table that has inbound foreign keys).
- ``project_metadata_field.format_mask`` — the mask for a ``pattern``
  field (e.g. ``AA-000-0000``).
- ``project_metadata.value_text`` — where a masked code is stored (it
  is alphanumeric, not numeric). The exactly-one-value CHECK widens
  from three typed columns to four. SQLite can't ALTER a CHECK in
  place, so the swap goes through a recreate-style ``batch_alter_table``
  (the migration 0031 template). The new literal is frozen here.

No data backfill: greenfield install; existing number fields have
``number_format = NULL`` which the service reads as ``decimal``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from matofx.db.migrations._batch import recreate_on_sqlite

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | None = None
depends_on: str | None = None


# Frozen literals — do NOT replace with imports of app constants.
_OLD_VALUE_CHECK = (
    "( (CASE WHEN value_id IS NOT NULL THEN 1 ELSE 0 END) "
    "+ (CASE WHEN value_num IS NOT NULL THEN 1 ELSE 0 END) "
    "+ (CASE WHEN value_date IS NOT NULL THEN 1 ELSE 0 END) ) = 1"
)
_NEW_VALUE_CHECK = (
    "( (CASE WHEN value_id IS NOT NULL THEN 1 ELSE 0 END) "
    "+ (CASE WHEN value_num IS NOT NULL THEN 1 ELSE 0 END) "
    "+ (CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END) "
    "+ (CASE WHEN value_date IS NOT NULL THEN 1 ELSE 0 END) ) = 1"
)


def upgrade() -> None:
    """Add the format columns + value_text and widen the value CHECK."""
    op.add_column(
        "project_metadata_field",
        sa.Column("number_format", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "project_metadata_field",
        sa.Column("format_mask", sa.String(length=64), nullable=True),
    )
    with op.batch_alter_table("project_metadata", recreate=recreate_on_sqlite()) as batch:
        batch.add_column(sa.Column("value_text", sa.String(length=255), nullable=True))
        batch.drop_constraint("ck_project_metadata_exactly_one_value", type_="check")
        batch.create_check_constraint("ck_project_metadata_exactly_one_value", _NEW_VALUE_CHECK)


def downgrade() -> None:
    """Reverse the upgrade (drops value_text + the format columns).

    Pattern selections live only in ``value_text``; the restored 3-way
    CHECK can't represent them, so those rows are removed first (a
    downgrade that drops the column necessarily drops its data).
    """
    op.execute("DELETE FROM project_metadata WHERE value_text IS NOT NULL")
    with op.batch_alter_table("project_metadata", recreate=recreate_on_sqlite()) as batch:
        batch.drop_constraint("ck_project_metadata_exactly_one_value", type_="check")
        batch.create_check_constraint("ck_project_metadata_exactly_one_value", _OLD_VALUE_CHECK)
        batch.drop_column("value_text")
    op.drop_column("project_metadata_field", "format_mask")
    op.drop_column("project_metadata_field", "number_format")
