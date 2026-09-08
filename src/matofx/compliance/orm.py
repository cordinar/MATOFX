"""SQLAlchemy ORM models for compliance attestation (ADR-0019).

Two tables:

- ``install_identity`` — singleton row (``id = 1``) holding this
  install's stable identity and the monotonic report counter. The
  identity is *adopted* from Funis-issued signed material; the row is
  the install's memory of it, and the counter is the thing anchoring
  depends on.
- ``compliance_report`` — every report ever generated, archived whole so
  a re-request is answerable and so the install can prove what it
  previously asserted.

**The private report key is deliberately NOT stored here.** It lives in
the Funis-issued identity file on disk (like ``audit.signing_key_path``),
and is read only at signing time. Copying a signing secret into the
application database would put it in every backup, every DB export, and
the admin DB explorer.

Both tables register against :data:`matofx.db.metadata.metadata` so
Alembic autogenerate sees them.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata

SINGLETON_ID = 1
"""Primary key of the one and only ``install_identity`` row."""


class _Base(DeclarativeBase):
    """Declarative base bound to the shared MetaData."""

    metadata = shared_metadata


class InstallIdentity(_Base):
    """This install's identity + report counter. Exactly one row.

    ``install_id`` is what every compliance report attests to. It is
    deliberately a DB-persisted value rather than a host fingerprint
    (ADR-0019 §A3): it survives container rebuilds and host migrations,
    which is the normal case, while a *cloned* deployment is caught by
    chain-head anchoring rather than by binding.
    """

    __tablename__ = "install_identity"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=False)
    install_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    # Everything below is Funis-supplied and therefore NULL until a
    # licence arrives (migration 0092). The install has an identity from
    # first boot; who the customer is, and which report key they were
    # given, are things only a licence can say.
    customer: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    environment: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    issued_at: Mapped[Any | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # Key ids only — never the key material itself (see module docstring).
    report_key_id: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    issuer_key_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    # SHA-256 of the identity file as adopted, so a swapped identity file
    # is detectable without keeping the file itself.
    identity_sha256: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    # True when this install minted its own id at first boot — the normal
    # case from 0092 on. False for identities adopted from Funis-signed
    # material before that, which stay valid.
    self_generated: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    # Monotonic, never reset. Gaps in the sequence Funis receives are the
    # signal that a reporting period was skipped or withheld.
    report_counter: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    adopted_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ComplianceReport(_Base):
    """One generated compliance report, archived verbatim."""

    __tablename__ = "compliance_report"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    counter: Mapped[int] = mapped_column(sa.Integer, nullable=False, unique=True)
    generated_at: Mapped[Any] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, index=True
    )
    period_from: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    period_to: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    head_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
    )
    head_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # The Merkle tree this report attested (migration 0091). Remembered
    # per report because a consistency proof is built *from* a tree size,
    # and the install cannot know which of its past reports the vendor
    # actually received — so it must be able to prove from any of them.
    tree_size: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False, default=0
    )
    # Nullable only for rows backfilled by 0091; the tree is derived from
    # ``audit_log``, so a missing root is recomputed rather than lost.
    tree_root: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    chain_ok: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    licence_type: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    content_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    generated_by: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    # The whole signed artefact, exactly as written to disk.
    document: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)


class IntegrityState(_Base):
    """Chain-verification state. Exactly one row (ADR-0019 §C14).

    Persisted rather than recomputed per request: a full chain walk is
    far too expensive to run on the hot path, and read-only mode has to
    be decided on every mutating request.
    """

    __tablename__ = "integrity_state"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=False)
    chain_ok: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    checked_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    records_checked: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    failed_at: Mapped[Any | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    detail: Mapped[str | None] = mapped_column(sa.String(1024), nullable=True)
    # Set by a Super User to lift read-only. Cleared by any fresh
    # failure — acknowledging one problem must not absolve the next.
    acknowledged_at: Mapped[Any | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    acknowledgement_note: Mapped[str | None] = mapped_column(sa.String(1024), nullable=True)
