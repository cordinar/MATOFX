"""Materials database: the six material tables (ADR-0007).

Revision ID: 0048
Revises: 0047
Create Date: 2026-05-25

Backs ``designs/materials-database.md`` §2 + ADR-0007: a global,
curator-governed materials properties database with immutable,
ordinal-versioned scalar property sets validated against a per-class
expected-property schema. Six new tables:

- ``material_class`` — class names (Fluid / Metal / Polymer / …);
  ``name`` UNIQUE; ``state`` CHECK ``('active','retired')``.
- ``material_class_property`` — expected ``(prop_name, unit, required)``
  per class; UNIQUE ``(class_id, prop_name)``.
- ``material`` — the logical card (mutable metadata, optimistic lock,
  current state). ``name`` is intentionally **not** unique. ``state``
  CHECK ``('active','pending','retired')``.
- ``material_version`` — append-only immutable versions; UNIQUE
  ``(material_id, ordinal)``; ``publish_state`` +
  optional ``data_pedigree`` CHECKs.
- ``material_property`` — per-version scalar property; UNIQUE
  ``(material_version_id, prop_name)``.
- ``job_material_use`` — per-field provenance, composite PK
  ``(job_id, field_name)``; FKs *to* ``job`` + ``material_version``.

``MATERIALS_CURATE`` and the ``materials_curator`` capability role were
already seeded by ADR-0005's migration ``0044``, so this migration is
**additive table creation only** — a plain ``op.create_table`` works on
both SQLite and Postgres (no recreate-style ``batch_alter_table``; cf.
the 0045 file-library template). CHECK bodies are **frozen literals** —
migrations are immutable historical records and must not import the
materials vocab tuples.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# Frozen literals — do NOT replace with imports of the materials vocab tuples.
_CLASS_STATE_CHECK = "state IN ('active','retired')"
_MATERIAL_STATE_CHECK = "state IN ('active','pending','retired')"
_PUBLISH_STATE_CHECK = "publish_state IN ('draft','pending','published','rejected')"
_DATA_PEDIGREE_CHECK = (
    "data_pedigree IS NULL OR data_pedigree IN ('measured','typical','estimated')"
)


def upgrade() -> None:
    """Create the six materials tables."""
    op.create_table(
        "material_class",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_material_class")),
        sa.UniqueConstraint("name", name=op.f("uq_material_class_name")),
        sa.CheckConstraint(_CLASS_STATE_CHECK, name=op.f("ck_material_class_state")),
    )

    op.create_table(
        "material_class_property",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("class_id", _BIGINT, nullable=False),
        sa.Column("prop_name", sa.String(length=64), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_material_class_property")),
        sa.ForeignKeyConstraint(
            ["class_id"],
            ["material_class.id"],
            name=op.f("fk_material_class_property_class_id_material_class"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("class_id", "prop_name", name=op.f("uq_material_class_property")),
    )
    op.create_index("ix_material_class_property_class_id", "material_class_property", ["class_id"])

    op.create_table(
        "material",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("class_id", _BIGINT, nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("steward_user_id", _BIGINT, nullable=False),
        sa.Column("confidentiality", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_by_user_id", _BIGINT, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_material")),
        sa.ForeignKeyConstraint(
            ["class_id"],
            ["material_class.id"],
            name=op.f("fk_material_class_id_material_class"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["steward_user_id"],
            ["user.id"],
            name=op.f("fk_material_steward_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_material_created_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_MATERIAL_STATE_CHECK, name=op.f("ck_material_state")),
    )
    op.create_index("ix_material_class_id", "material", ["class_id"])
    op.create_index("ix_material_steward_user_id", "material", ["steward_user_id"])

    op.create_table(
        "material_version",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("material_id", _BIGINT, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("data_pedigree", sa.String(length=32), nullable=True),
        sa.Column("conditions", sa.Text(), nullable=True),
        sa.Column("version_notes", sa.Text(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("publish_state", sa.String(length=16), nullable=False),
        sa.Column("created_by_user_id", _BIGINT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_material_version")),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["material.id"],
            name=op.f("fk_material_version_material_id_material"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_material_version_created_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("material_id", "ordinal", name=op.f("uq_material_version_ordinal")),
        sa.CheckConstraint(_PUBLISH_STATE_CHECK, name=op.f("ck_material_version_publish_state")),
        sa.CheckConstraint(_DATA_PEDIGREE_CHECK, name=op.f("ck_material_version_data_pedigree")),
    )
    op.create_index("ix_material_version_material_id", "material_version", ["material_id"])

    op.create_table(
        "material_property",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("material_version_id", _BIGINT, nullable=False),
        sa.Column("prop_name", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("uncertainty", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_material_property")),
        sa.ForeignKeyConstraint(
            ["material_version_id"],
            ["material_version.id"],
            name=op.f("fk_material_property_material_version_id_material_version"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("material_version_id", "prop_name", name=op.f("uq_material_property")),
    )
    op.create_index(
        "ix_material_property_material_version_id",
        "material_property",
        ["material_version_id"],
    )

    op.create_table(
        "job_material_use",
        sa.Column("job_id", _BIGINT, nullable=False),
        sa.Column("field_name", sa.String(length=128), nullable=False),
        sa.Column("material_version_id", _BIGINT, nullable=False),
        sa.PrimaryKeyConstraint("job_id", "field_name", name=op.f("pk_job_material_use")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_job_material_use_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["material_version_id"],
            ["material_version.id"],
            name=op.f("fk_job_material_use_material_version_id_material_version"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_job_material_use_material_version_id",
        "job_material_use",
        ["material_version_id"],
    )


def downgrade() -> None:
    """Drop the six materials tables (children first)."""
    op.drop_index("ix_job_material_use_material_version_id", table_name="job_material_use")
    op.drop_table("job_material_use")

    op.drop_index("ix_material_property_material_version_id", table_name="material_property")
    op.drop_table("material_property")

    op.drop_index("ix_material_version_material_id", table_name="material_version")
    op.drop_table("material_version")

    op.drop_index("ix_material_steward_user_id", table_name="material")
    op.drop_index("ix_material_class_id", table_name="material")
    op.drop_table("material")

    op.drop_index("ix_material_class_property_class_id", table_name="material_class_property")
    op.drop_table("material_class_property")

    op.drop_table("material_class")
