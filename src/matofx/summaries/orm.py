"""ORM for the auto-summary cache (``designs/auto-summaries.md`` §7).

One table — :class:`ResultSummary` — caches the rendered narrative for one
terminal result. It is **derived data, not a source of truth**: it can be
truncated and rebuilt from the underlying result at any time. The cache key is
``facts_hash`` (a SHA-256 of the canonical :class:`SummaryFacts`), so any change
to the underlying facts (a KPI re-extraction, a new child job) invalidates the
row on the next view.

No foreign key on ``surface_id``: the id is polymorphic across the job /
workflow_run / parametric_study / optimisation_study tables, so referential
integrity is enforced by the cache being rebuildable rather than by a constraint.
The composite ``UNIQUE(surface_kind, surface_id)`` keeps it one row per result;
both the templated and (later) enriched texts live on that row, and the per-user
opt-out selects which to render — it is *not* part of the key.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata
from matofx.summaries.facts import SURFACE_KINDS

_SURFACE_KINDS_SQL = ", ".join(f"'{k}'" for k in SURFACE_KINDS)
_ENRICHMENT_STATUSES_SQL = "'templated', 'enriched', 'fallback'"


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class ResultSummary(_Base):
    """The cached narrative for one terminal result."""

    __tablename__ = "result_summary"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    surface_kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    """One of the closed :data:`SURFACE_KINDS` vocabulary (CHECK-constrained)."""

    surface_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
    )
    """The result's id within ``surface_kind``'s own table (polymorphic, no FK)."""

    facts_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    """SHA-256 of the canonical ``SummaryFacts`` — the invalidation key."""

    templated_text: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    """The deterministic baseline ``SummaryText`` as ``{title, paragraphs}``."""

    enriched_text: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    """The guard-passed LLM prose, present only after a successful enrichment
    (Slice 7). ``NULL`` otherwise — the templated text is then the only render."""

    enrichment_status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="templated"
    )
    """``templated`` (no enrichment attempted / configured), ``enriched``
    (LLM prose stored), or ``fallback`` (enrichment attempted + rejected)."""

    enrichment_model: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    """``provider:model`` recorded for audit/debug when enrichment ran."""

    generated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "surface_kind", "surface_id", name="uq_result_summary_surface"
        ),
        sa.CheckConstraint(
            f"surface_kind IN ({_SURFACE_KINDS_SQL})",
            name="ck_result_summary_surface_kind",
        ),
        sa.CheckConstraint(
            f"enrichment_status IN ({_ENRICHMENT_STATUSES_SQL})",
            name="ck_result_summary_enrichment_status",
        ),
    )
