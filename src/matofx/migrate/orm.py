"""SQLAlchemy ORM for cross-install Tools/Workflows migration.

Currently a single table — :class:`MigrateTrustedPeer`, the destination
install's list of source-install Ed25519 fingerprints whose signed
bundles it will accept. Added/removed via the unified ``Admin →
Migrate Tools/Workflows`` page (or the equivalent CLI verbs).

The bundle pack/unpack code lives in :mod:`matofx.migrate.bundle`, the
signing/verification helpers in :mod:`matofx.migrate.signing`, and the
audited service surface in :mod:`matofx.migrate.service`. None of those
modules require additional tables — the bundle itself is the durable
record, and audit events go to the existing audit log.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class MigrateTrustedPeer(_Base):
    """One source install whose signed migration bundles are accepted here.

    Identity is the source install's Ed25519 public-key fingerprint,
    stored as a canonical lowercase hex string with no separators. The
    UI renders the human-friendly colon-delimited uppercase form
    (``9F:2E:A1:...``) from the same stored value; canonicalising on
    write keeps lookup-by-fingerprint robust against the various
    formats an admin might paste.

    Adding and removing rows is gated by
    :attr:`Permission.MIGRATE_TOOLS_WORKFLOWS` and audit-logged. The
    audit-log entry is the durable record of why a peer was trusted
    or untrusted — this row carries no soft-delete flag.

    :attr:`last_used_at` is updated by the import path each time a
    successful import verifies against this peer. Lets the admin spot
    stale entries (e.g. "we decommissioned Staging two years ago and
    nobody ever removed the trust row").
    """

    __tablename__ = "migrate_trusted_peer"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    fingerprint: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    """Canonical lowercase hex of the source install's Ed25519 public
    key fingerprint (no colons, no ``SHA256:`` prefix). The UI / CLI
    accept any common format on input and canonicalise via
    :func:`matofx.migrate.signing.canonicalise_fingerprint` before the
    write. The unique constraint guarantees one row per source key."""

    label: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    """Human-readable name for the source install (e.g. ``"Staging"``).
    Free text, admin's choice, not used in verification. The label
    surfaces in the trusted-peers panel, the import preview, and the
    audit-log payloads to make events readable without having to look
    up which fingerprint belonged to which environment."""

    added_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    """Who added the trust entry. ``NULL`` after that user's account
    is deleted — the trust row outlives the admin who created it."""

    added_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )

    last_used_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    """Set on every successful bundle import that verifies against
    this peer. ``NULL`` until the first import. Pure diagnostic — not
    consulted during verification."""

    notes: Mapped[str] = mapped_column(sa.Text, nullable=False, default="", server_default="")
    """Optional free-text note the admin enters when adding the
    peer (e.g. *"Staging install on prod-network, MTX-412"*). Visible
    in the trusted-peers panel and copied into the audit-log entry."""

    __table_args__: Any = (
        sa.CheckConstraint(
            "length(fingerprint) > 0",
            name="ck_migrate_trusted_peer_fingerprint_nonempty",
        ),
        sa.CheckConstraint(
            "length(label) > 0",
            name="ck_migrate_trusted_peer_label_nonempty",
        ),
    )
