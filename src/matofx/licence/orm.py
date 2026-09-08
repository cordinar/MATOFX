"""ORM model for the installed licence (ADR-0019 slice 2).

One singleton row (``id = 1``) recording which licence is currently
installed. The **file on disk stays the authority** — it is re-verified
on every load — so this row is a cache with two jobs the file can't do:

- Let the API and runner processes read licence terms without file
  access (they may not share a filesystem with the admin's copy).
- Notice *change*: a different ``licence_id`` or file digest from what
  was last recorded is a `licence.installed` event worth auditing, and
  eventually worth reporting.

Deliberately not stored: the report key seed. Like the install-identity
file, the signing secret stays on disk (:mod:`matofx.compliance.orm`
explains why).
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata

SINGLETON_ID = 1
"""Primary key of the one and only ``licence_install`` row."""


class _Base(DeclarativeBase):
    """Declarative base bound to the shared MetaData."""

    metadata = shared_metadata


class LicenceInstall(_Base):
    """The currently-installed licence, as last verified."""

    __tablename__ = "licence_install"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=False)
    licence_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    customer: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    environment: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    licence_type: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    n_slots: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    issued_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    expires_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    grace_period_days: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    machine_binding: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    banner_text: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    report_key_id: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    issuer_key_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    issuer_verified: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    manifest_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_path: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    installed_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    installed_by: Mapped[str] = mapped_column(sa.String(255), nullable=False)
