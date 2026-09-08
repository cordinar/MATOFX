"""File library: the four library tables (ADR-0006).

Revision ID: 0045
Revises: 0044
Create Date: 2026-05-24

Backs ``designs/file-library.md`` §2 + ADR-0006: a governed, reusable
tool-input file store with immutable, content-hashed versions. Four new
tables:

- ``library_file`` — the logical entry (mutable metadata, optimistic
  lock, current scope/state). Portable CHECKs tie ``scope`` / ``state``
  to their closed vocabularies and ``project_id`` to the ``project``
  scope.
- ``library_file_version`` — append-only immutable versions; UNIQUE
  ``(library_file_id, ordinal)``; sha-256 ``content_hash``.
- ``library_file_tool`` — file ↔ tool[, field] assignment; UNIQUE
  ``(library_file_id, tool_alias, field_name)``. ``tool_alias`` is a
  soft string reference (no FK) — tools are versioned by alias and the
  field set resolves at pick time.
- ``job_library_file_use`` — per-field provenance, composite PK
  ``(job_id, field_name)``; FKs *to* ``job`` + ``library_file_version``.

``LIBRARY_CURATE`` and the ``library_curator`` capability role were
already seeded by ADR-0005's migration ``0044``, so this migration is
**additive table creation only** — no CHECK/nullability change on an
existing table, so a plain ``op.create_table`` works on both SQLite and
Postgres (no recreate-style ``batch_alter_table``; cf. the 0041
template). CHECK bodies are **frozen literals** — migrations are
immutable historical records and must not import the library vocab
tuples.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# Frozen literals — do NOT replace with imports of the library vocab tuples.
_SCOPE_CHECK = "scope IN ('personal','project','global')"
_STATE_CHECK = "state IN ('active','pending','retired')"
_PROJECT_SCOPE_CHECK = (
    "(scope = 'project' AND project_id IS NOT NULL) OR (scope <> 'project' AND project_id IS NULL)"
)
_PUBLISH_STATE_CHECK = "publish_state IN ('draft','pending','published','rejected')"


def upgrade() -> None:
    """Create the four file-library tables."""
    op.create_table(
        "library_file",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("file_kind", sa.String(length=64), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("project_id", _BIGINT, nullable=True),
        sa.Column("owner_user_id", _BIGINT, nullable=False),
        sa.Column("handling_label", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_by_user_id", _BIGINT, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_library_file")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_library_file_project_id_project"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            name=op.f("fk_library_file_owner_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_library_file_created_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_SCOPE_CHECK, name=op.f("ck_library_file_scope")),
        sa.CheckConstraint(_STATE_CHECK, name=op.f("ck_library_file_state")),
        sa.CheckConstraint(_PROJECT_SCOPE_CHECK, name=op.f("ck_library_file_project_scope")),
    )
    op.create_index("ix_library_file_project_id", "library_file", ["project_id"])
    op.create_index("ix_library_file_owner_user_id", "library_file", ["owner_user_id"])

    op.create_table(
        "library_file_version",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("library_file_id", _BIGINT, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("stored_path", sa.String(length=1024), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("extension", sa.String(length=32), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("version_notes", sa.Text(), nullable=True),
        sa.Column("publish_state", sa.String(length=16), nullable=False),
        sa.Column("created_by_user_id", _BIGINT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_library_file_version")),
        sa.ForeignKeyConstraint(
            ["library_file_id"],
            ["library_file.id"],
            name=op.f("fk_library_file_version_library_file_id_library_file"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_library_file_version_created_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "library_file_id", "ordinal", name=op.f("uq_library_file_version_ordinal")
        ),
        sa.CheckConstraint(
            _PUBLISH_STATE_CHECK, name=op.f("ck_library_file_version_publish_state")
        ),
    )
    op.create_index(
        "ix_library_file_version_library_file_id",
        "library_file_version",
        ["library_file_id"],
    )

    op.create_table(
        "library_file_tool",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("library_file_id", _BIGINT, nullable=False),
        sa.Column("tool_alias", sa.String(length=64), nullable=False),
        sa.Column("field_name", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_library_file_tool")),
        sa.ForeignKeyConstraint(
            ["library_file_id"],
            ["library_file.id"],
            name=op.f("fk_library_file_tool_library_file_id_library_file"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "library_file_id",
            "tool_alias",
            "field_name",
            name=op.f("uq_library_file_tool_assignment"),
        ),
    )
    op.create_index(
        "ix_library_file_tool_library_file_id", "library_file_tool", ["library_file_id"]
    )
    op.create_index("ix_library_file_tool_tool_alias", "library_file_tool", ["tool_alias"])

    op.create_table(
        "job_library_file_use",
        sa.Column("job_id", _BIGINT, nullable=False),
        sa.Column("field_name", sa.String(length=128), nullable=False),
        sa.Column("library_file_version_id", _BIGINT, nullable=False),
        sa.PrimaryKeyConstraint("job_id", "field_name", name=op.f("pk_job_library_file_use")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_job_library_file_use_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["library_file_version_id"],
            ["library_file_version.id"],
            name=op.f("fk_job_library_file_use_library_file_version_id_library_file_version"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_job_library_file_use_library_file_version_id",
        "job_library_file_use",
        ["library_file_version_id"],
    )


def downgrade() -> None:
    """Drop the four file-library tables (children first)."""
    op.drop_index(
        "ix_job_library_file_use_library_file_version_id",
        table_name="job_library_file_use",
    )
    op.drop_table("job_library_file_use")

    op.drop_index("ix_library_file_tool_tool_alias", table_name="library_file_tool")
    op.drop_index("ix_library_file_tool_library_file_id", table_name="library_file_tool")
    op.drop_table("library_file_tool")

    op.drop_index(
        "ix_library_file_version_library_file_id",
        table_name="library_file_version",
    )
    op.drop_table("library_file_version")

    op.drop_index("ix_library_file_owner_user_id", table_name="library_file")
    op.drop_index("ix_library_file_project_id", table_name="library_file")
    op.drop_table("library_file")
