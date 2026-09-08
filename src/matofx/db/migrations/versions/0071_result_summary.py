"""Auto-summaries: the ``result_summary`` cache table.

Revision ID: 0071
Revises: 0070
Create Date: 2026-06-10

Backs ``designs/auto-summaries.md`` §7: a per-result cache of the
rendered narrative (templated baseline + optional enrichment). Derived data —
``DROP TABLE`` downgrade is loss-free because every row rebuilds on the next
view of the underlying result.

One row per result, keyed by ``UNIQUE(surface_kind, surface_id)``. No FK on
``surface_id`` (it is polymorphic across the job / workflow_run /
parametric_study / optimisation_study tables); integrity is provided by the
cache being rebuildable. Two CHECK constraints pin the closed ``surface_kind``
and ``enrichment_status`` vocabularies.

Additive table creation only; no rewrite of existing tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | None = None
depends_on: str | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

_SURFACE_KINDS = (
    "job",
    "workflow_run",
    "parametric_study",
    "optimisation_study",
)
_ENRICHMENT_STATUSES = ("templated", "enriched", "fallback")


def _in_clause(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    """Create the ``result_summary`` cache table + its UNIQUE + CHECKs."""
    op.create_table(
        "result_summary",
        sa.Column("id", _BIGINT, nullable=False, autoincrement=True),
        sa.Column("surface_kind", sa.String(length=32), nullable=False),
        sa.Column("surface_id", _BIGINT, nullable=False),
        sa.Column("facts_hash", sa.String(length=64), nullable=False),
        sa.Column("templated_text", sa.JSON(), nullable=False),
        sa.Column("enriched_text", sa.JSON(), nullable=True),
        sa.Column("enrichment_status", sa.String(length=16), nullable=False),
        sa.Column("enrichment_model", sa.String(length=128), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_result_summary")),
        sa.UniqueConstraint("surface_kind", "surface_id", name="uq_result_summary_surface"),
        sa.CheckConstraint(
            f"surface_kind IN ({_in_clause(_SURFACE_KINDS)})",
            name="ck_result_summary_surface_kind",
        ),
        sa.CheckConstraint(
            f"enrichment_status IN ({_in_clause(_ENRICHMENT_STATUSES)})",
            name="ck_result_summary_enrichment_status",
        ),
    )


def downgrade() -> None:
    """Drop the ``result_summary`` cache table."""
    op.drop_table("result_summary")
