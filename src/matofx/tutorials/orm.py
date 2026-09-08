"""SQLAlchemy ORM for tutorial uploads."""

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


MEDIA_TYPES: tuple[str, ...] = ("document", "video")
"""Closed vocabulary for :attr:`Tutorial.media_type`.

* ``document`` — PDFs (one extension, exactly: ``.pdf``).
* ``video`` — every common container the browser might play
  inline or offer to download (``.mp4``, ``.webm``, ``.mov`` …);
  see :data:`matofx.tutorials.storage.VIDEO_EXTENSIONS`.

A CHECK constraint pins the column to these two values so a typo
elsewhere can't slip past."""


class Tutorial(_Base):
    """One uploaded tutorial — PDF or video — for a tool, a workflow, or the general pool.

    Tutorials are append-only in spirit: admins upload new versions
    rather than overwriting old ones, and ``is_active`` decides which
    row the Home page surfaces to end users. The cascade rule
    "exactly one active per (scope, media_type)" is enforced in
    :meth:`matofx.tutorials.service.TutorialService.activate` rather
    than at the DB level — partial unique indexes work fine on
    Postgres but are awkward to express portably on SQLite.

    Scope is encoded by which of ``tool_alias`` / ``workflow_alias`` is
    set. A DB CHECK (``ck_tutorial_scope_exclusive``, added in
    migration 0025) forbids both being non-NULL on the same row. Both
    NULL ⇒ general.

    General tutorials carry **no** per-type activation cap so an
    install can ship "Getting started", "Power-user tips", and a
    safety brief all at once.
    """

    __tablename__ = "tutorial"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    tool_alias: Mapped[str | None] = mapped_column(
        sa.String(64),
        sa.ForeignKey("tool.alias", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    """``NULL`` ⇒ not tool-scoped. Otherwise references
    :attr:`matofx.tools.orm.Tool.alias` — RESTRICT so a tool can't be
    deleted out from under its tutorials. Tools are archived rather
    than deleted in practice, so the FK is mostly defence in depth.

    Mutually exclusive with :attr:`workflow_alias` (CHECK
    ``ck_tutorial_scope_exclusive``)."""

    workflow_alias: Mapped[str | None] = mapped_column(
        sa.String(64),
        sa.ForeignKey("workflow.alias", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    """``NULL`` ⇒ not workflow-scoped. Otherwise references
    :attr:`matofx.workflows.orm.Workflow.alias` — RESTRICT to match the
    tool-side rule. Mutually exclusive with :attr:`tool_alias`."""

    media_type: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    """``'document'`` or ``'video'``. CHECK-constrained."""

    version: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="1")
    """Free-form version label the admin types in (e.g. ``"v1.0"``,
    ``"Rev B"``). Sort order on the admin page is created-newest-first,
    so the version field is for human-readable history rather than
    machine ordering."""

    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")

    original_filename: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    """Filename the admin uploaded — used for the user-facing
    ``Content-Disposition`` header so saved files look familiar."""

    mime_type: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    """Absolute path on disk where the file was staged.

    Stored absolute so a future change to the tutorials root doesn't
    silently break existing rows. The runtime helper
    :func:`matofx.tutorials.storage.tutorials_dir` is what we *use*
    to decide where new uploads go; the path on a stored row stays
    authoritative for that row."""

    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    """Whether the Home page surfaces this row. The admin page is the
    only mutation seam; activation cascades inactive on conflicting
    (tool_alias, media_type) siblings. Always ``False`` once
    :attr:`superseded_at` is set (archived previous versions are never
    surfaced)."""

    lineage_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        nullable=True,
    )
    """Groups every version of the *same* tutorial. Set to the row's own
    id on first upload; an **update** creates a new row carrying the
    source row's ``lineage_id``, so a lineage is "current version + its
    archived previous versions". Migration 0082 backfilled existing rows
    to their own id."""

    superseded_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    """When set, this row is an **archived previous version** — replaced
    by a newer row in the same lineage. It's forced inactive, never shown
    to users, and kept for history (its file stays on disk). ``NULL`` ⇒
    the current version of its lineage."""

    uploaded_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    """Who uploaded the file. ``NULL`` after the uploader's account is
    deleted — the tutorial outlives the admin."""

    uploaded_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__: Any = (
        sa.CheckConstraint(
            "media_type IN ('document', 'video')",
            name="ck_tutorial_media_type",
        ),
        sa.CheckConstraint(
            "NOT (tool_alias IS NOT NULL AND workflow_alias IS NOT NULL)",
            name="ck_tutorial_scope_exclusive",
        ),
        sa.Index("ix_tutorial_tool_active", "tool_alias", "media_type", "is_active"),
        sa.Index(
            "ix_tutorial_workflow_active",
            "workflow_alias",
            "media_type",
            "is_active",
        ),
        sa.Index("ix_tutorial_lineage", "lineage_id"),
    )
