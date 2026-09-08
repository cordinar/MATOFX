"""Surrogate family: the Model registry tables (ADR-0008 §5.2/§5.3, Phase 5a).

Revision ID: 0061
Revises: 0060
Create Date: 2026-06-02

Backs ``designs/surrogate-family.md`` §5.2 / §5.3 — the governed registry
a fitted surrogate is persisted into:

- ``surrogate_model`` — the logical entry (mutable metadata): name, tags,
  the single ``tool_alias`` it predicts for, the scope tier
  (``personal`` / ``project`` / ``global``), steward, lifecycle state,
  and a pointer to the currently-published version.
- ``surrogate_model_version`` — the immutable per-fit record: ordinal,
  scalar/field kind, frozen factors + responses, the source it was
  fitted from, the on-disk bundle path + its sha256 (verified on every
  load, non-negotiable #2), CV quality, and the publish state.

``Job.surrogate_model_version_id`` is added as the §5.4 lineage tag a
surrogate-as-tool job carries.

**Logical pointers, not hard FKs (Phase 5a scope):** ``published_version_id``
(model ↔ version is a mutual reference — a hard FK pair needs deferred
constraints SQLite can't add in place) and ``Job.surrogate_model_version_id``
(adding an FK column to the heavily-constrained ``job`` table would force
a full batch-recreate of its partial unique indexes) are plain integer
columns whose integrity the service layer enforces — the same un-FK'd
pointer pattern the ``study_result`` ``source_id`` discriminator uses.
The version → model FK *is* a hard CASCADE (no cycle there).

The governance widening (the ``MODELS_CURATE`` permission, the
``model_curator`` capability role, the ``surrogate_model_version``
approval target, and the ``models_global_publish_approval`` setting)
lands in the Phase-5b governance migration — this one is the data model
only. Greenfield ``create_table`` + a plain ``add_column`` on ``job``
(nullable, no FK) work on both SQLite and Postgres with no recreate.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# Frozen literals — do NOT import the ORM CHECK bodies.
_SCOPE_CHECK = "scope IN ('personal', 'project', 'global')"
_MODEL_STATE_CHECK = "state IN ('active', 'pending', 'retired')"
_PROJECT_IFF_SCOPE = "(scope = 'project') = (project_id IS NOT NULL)"
_KIND_CHECK = "kind IN ('scalar', 'field')"
_PUBLISH_STATE_CHECK = "publish_state IN ('draft', 'in_review', 'checked', 'published', 'rejected')"


def upgrade() -> None:
    """Create ``surrogate_model`` + ``surrogate_model_version``; tag ``job``."""
    op.create_table(
        "surrogate_model",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("tool_alias", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("project_id", _BIGINT, nullable=True),
        sa.Column("owner_user_id", _BIGINT, nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("published_version_id", _BIGINT, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("created_by_user_id", _BIGINT, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_surrogate_model")),
        sa.ForeignKeyConstraint(
            ["tool_alias"],
            ["tool.alias"],
            name=op.f("fk_surrogate_model_tool_alias_tool"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_surrogate_model_project_id_project"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            name=op.f("fk_surrogate_model_owner_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_surrogate_model_created_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_SCOPE_CHECK, name=op.f("ck_surrogate_model_scope")),
        sa.CheckConstraint(_MODEL_STATE_CHECK, name=op.f("ck_surrogate_model_state")),
        sa.CheckConstraint(_PROJECT_IFF_SCOPE, name=op.f("ck_surrogate_model_project_iff_scope")),
    )
    op.create_index(
        "ix_surrogate_model_tool_alias_scope", "surrogate_model", ["tool_alias", "scope"]
    )

    op.create_table(
        "surrogate_model_version",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("surrogate_model_id", _BIGINT, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("factors", sa.JSON(), nullable=False),
        sa.Column("responses", sa.JSON(), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", _BIGINT, nullable=False),
        sa.Column("tool_version_ids_at_fit", sa.JSON(), nullable=False),
        sa.Column("n_training_points", sa.Integer(), nullable=False),
        sa.Column("bundle_path", sa.String(1024), nullable=False),
        sa.Column("bundle_sha256", sa.String(64), nullable=False),
        sa.Column("quality", sa.JSON(), nullable=False),
        sa.Column("keep_training_data", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("publish_state", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "fitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("fitted_by_user_id", _BIGINT, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_surrogate_model_version")),
        sa.ForeignKeyConstraint(
            ["surrogate_model_id"],
            ["surrogate_model.id"],
            name=op.f("fk_surrogate_model_version_surrogate_model_id_surrogate_model"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["fitted_by_user_id"],
            ["user.id"],
            name=op.f("fk_surrogate_model_version_fitted_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "surrogate_model_id", "ordinal", name="uq_surrogate_model_version_model_ordinal"
        ),
        sa.CheckConstraint(_KIND_CHECK, name=op.f("ck_surrogate_model_version_kind")),
        sa.CheckConstraint(
            _PUBLISH_STATE_CHECK, name=op.f("ck_surrogate_model_version_publish_state")
        ),
    )
    op.create_index(
        "ix_surrogate_model_version_surrogate_model_id",
        "surrogate_model_version",
        ["surrogate_model_id"],
    )

    op.add_column("job", sa.Column("surrogate_model_version_id", _BIGINT, nullable=True))
    op.create_index("ix_job_surrogate_model_version_id", "job", ["surrogate_model_version_id"])


def downgrade() -> None:
    """Drop the registry tables + the job tag."""
    op.drop_index("ix_job_surrogate_model_version_id", table_name="job")
    op.drop_column("job", "surrogate_model_version_id")
    op.drop_index(
        "ix_surrogate_model_version_surrogate_model_id", table_name="surrogate_model_version"
    )
    op.drop_table("surrogate_model_version")
    op.drop_index("ix_surrogate_model_tool_alias_scope", table_name="surrogate_model")
    op.drop_table("surrogate_model")
