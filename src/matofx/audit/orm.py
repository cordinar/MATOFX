"""SQLAlchemy ORM models for the audit log.

Two tables:

- ``audit_log`` — append-only chain of records. Indexed on timestamp
  for time-range queries and on (resource_type, resource_id) for
  per-resource lookups (rendering "what happened to job 42?").
- ``audit_chain_head`` — single-row table holding the latest record's
  id and hash. Writers ``SELECT … FOR UPDATE`` this row to serialise
  appends; on Postgres this enforces ordering across processes, on
  SQLite the writer-lock model already serialises writes.

Both tables register against :data:`matofx.db.metadata.metadata` so
Alembic autogenerate sees them.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata


class _Base(DeclarativeBase):
    """Declarative base bound to the shared MetaData."""

    metadata = shared_metadata


class AuditLog(_Base):
    """One row per audit event, immutable once written."""

    __tablename__ = "audit_log"

    # BIGINT for headroom on Postgres; INTEGER on SQLite is required for
    # the ROWID-aliased autoincrement to work.
    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    timestamp: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    actor_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(128), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    outcome: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    request_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    source_ip: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    prior_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    signature: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    key_id: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    schema_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True, index=True)

    __table_args__ = (
        sa.Index("ix_audit_log_resource", "resource_type", "resource_id"),
        # Belt-and-braces against direct INSERTs that bypass the writer's
        # FOR UPDATE on the chain head: any direct attempt to introduce
        # a forked chain (two records claiming the same prior link) is
        # rejected at the DB level. The genesis hash is unique because
        # the chain has exactly one first record.
        sa.UniqueConstraint("prior_hash", name="uq_audit_log_prior_hash"),
    )


class AuditChainHead(_Base):
    """Single-row mutable head pointer for chain serialisation.

    Always exactly one row, with ``id = 1``. Writers select this row
    ``FOR UPDATE`` before computing the new record's prior_hash, so the
    chain mutates serially even under concurrent writers.
    """

    __tablename__ = "audit_chain_head"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    last_record_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        nullable=True,
    )
    last_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
