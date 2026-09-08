"""SQLAlchemy ORM models for KPI extraction (ADR-0012).

One table — :class:`JobKpiValue` — stores the captured value of every
declared KPI for every completed job. Per ``designs/kpi-extraction.md``
§4 the model is deliberately minimal: no relationships back to
:class:`Job` or :class:`ToolVersion`, no audit columns on the row
itself (the post-completion extraction is audited in
``matofx.audit``), and a wide ``Numeric(38, 12)`` value column so
engineering units across all reasonable orders of magnitude land
without binary-rounding loss.

The row is written by :class:`KpiService.extract_for_job` (slice 3)
from the runner's already-loaded ``OutputsDocument`` and re-written
by :meth:`KpiService.reextract_tool_version` (slice 4) on descriptor
promotion. Reads come from the warehouse query path (slice 5) and the
ADR-0004 report headline block (slice 8).

Why no ``relationship()``:

- The warehouse query is range-scanned on the
  ``(tool_alias, kpi_name)`` index plus an explicit ACL-composed
  filter against ``job`` — letting SQLAlchemy autoload either related
  object would invite per-row N+1 traps the warehouse can't tolerate
  at the populated-install scale (millions of rows).
- The post-completion hook only needs ``job.id`` / ``tool_alias`` /
  ``tool_version_id`` — all already in scope.
- The retroactive ADR-0003 retention sweep deletes ``outputs.json``
  but not the ``Job`` row, so KPI rows persist; relationships would
  add no expressive power.

Why per-row ``unit`` storage: a descriptor edit that changes
``kpi_unit`` (or drops the override so ``unit`` resurfaces) must not
retroactively mislabel historic values. The captured unit is part of
the audit record — see ``designs/kpi-extraction.md`` §15.

The post-completion hook contract is **never-raise** — extraction
failures degrade to ``value_numeric = NULL`` + a WARNING log, not an
exception that rolls back the job-completion transaction (design
§5.1).
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class JobKpiValue(_Base):
    """One captured KPI value for one completed job.

    Composite UNIQUE on ``(job_id, kpi_name)`` — re-extraction (slice 4)
    UPSERTs against this key; a job never has two rows for the same KPI.
    Migration ``0057`` creates the table; the two performance indexes
    on ``(tool_alias, kpi_name)`` + ``extracted_at`` are added by the
    same migration.

    :attr:`value_numeric` is ``NULL`` when the captured output was
    missing, non-numeric, NaN, or ±Inf. That sentinel lets the
    warehouse answer *"did this job complete with KPI X populated?"*
    without joining back to the bulk ``outputs.json`` document (which
    may itself have aged out per ADR-0003 retention).
    """

    __tablename__ = "job_kpi_value"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    job_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("job.id", ondelete="CASCADE"),
        nullable=False,
    )
    """The job whose ``outputs.json`` produced this value. CASCADE —
    a deleted job leaves no semantic claim about its KPIs."""

    tool_alias: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    """Denormalised from ``job.tool_alias`` for index-scan speed on
    the warehouse query path. Single source of truth for the alias
    remains :attr:`Job.tool_alias`; the denormalised copy is written
    once, never reconciled — a tool alias is immutable post-install."""

    tool_version_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("tool_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    """Denormalised from ``job.tool_version_id``. RESTRICT — retiring
    a tool version that still owns KPI rows should fail loudly rather
    than silently orphan them. v1.x: a "force re-extract" admin
    action will be the path for descriptor-set rewrites."""

    kpi_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    """The :attr:`OutputField.name` the value came from. Validated
    against the live descriptor at write time (slice 3); not enforced
    at schema time because the closed vocabulary is the descriptor,
    not a static enum."""

    value_numeric: Mapped[Decimal | None] = mapped_column(
        sa.Numeric(precision=38, scale=12), nullable=True
    )
    """The captured scalar value. ``NULL`` is the sentinel for
    extraction failure (missing output key, non-numeric, NaN, ±Inf) —
    the warehouse displays "—" and the time-series chart skips the
    point. Wide precision so heat-transfer K, electronics nA, and
    parts-per-million all fit without quantisation loss.

    Read consumers (warehouse API, plot, CSV export) cast to
    ``float`` for transport; the ``Decimal`` storage is the durable
    truth and the only form the audit chain references."""

    unit: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    """The captured unit (``kpi_unit`` when set, else ``unit``).
    Stored per-row so a later descriptor edit doesn't retroactively
    mislabel historic values."""

    extracted_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    """When the post-completion hook (or re-extraction job) wrote this
    row. Drives the time-series trend chart's range scan via
    ``ix_job_kpi_value_extracted_at``."""

    __table_args__ = (
        sa.UniqueConstraint("job_id", "kpi_name", name="uq_job_kpi_value_job_kpi"),
        sa.Index("ix_job_kpi_value_alias_kpi", "tool_alias", "kpi_name"),
        sa.Index("ix_job_kpi_value_extracted_at", "extracted_at"),
    )
