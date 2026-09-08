"""SQLAlchemy ORM models for the governed file library (ADR-0006).

A **library file** is a reusable, governed tool-input file (geometry,
mesh, boundary-condition CSV, …) with immutable, content-hashed
versions. Per ``designs/file-library.md`` §2 the model has two
independent axes:

- **Scope** (`library_file.scope`) — tiered visibility / governance:
  ``personal`` → ``project`` → ``global``.
- **Tool assignment** (`library_file_tool`) — what the file is *for*:
  one or more ``(tool_alias[, field_name])`` mappings. ``tool_alias`` is
  a **soft reference** (plain string, not an FK), because tools are
  immutably versioned by alias and the field set is resolved against the
  tool *version* chosen at pick time, not at assignment time.

Four tables:

- :class:`LibraryFile` — the logical entry (mutable metadata, optimistic
  lock, current scope/state). No hard delete — ``state='retired'``.
- :class:`LibraryFileVersion` — append-only immutable versions; each
  stores a sha-256 ``content_hash`` (integrity + a "same bytes" signal).
- :class:`LibraryFileTool` — the tool-assignment layer (file ↔ tool[, field]).
- :class:`JobLibraryFileUse` — per-field provenance written at submit
  ("which library version fed which field of which job"). It FKs *to*
  ``job`` (the runner maps no new FK *from* ``job``), so no
  ``runner/supervisor.py`` side-effect import is needed.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata

LibraryScope = Literal["personal", "project", "global"]
"""Closed vocabulary for ``library_file.scope`` (design §2.1).

- ``personal`` — visible only to the owner.
- ``project`` — visible to the project ACL (ADR-0002 Viewer+);
  ``project_id`` is non-null iff scope is ``project``.
- ``global`` — the org-wide shared library; visible to all
  authenticated users. Landing in global may be approval-gated (§5).
"""

LIBRARY_SCOPES: tuple[LibraryScope, ...] = ("personal", "project", "global")

LibraryFileState = Literal["active", "pending", "retired"]
"""Closed vocabulary for ``library_file.state`` (design §2.1).

- ``active`` — normal, pickable (subject to version publish state).
- ``pending`` — a brand-new global file awaiting approval (§5); not
  pickable by regular users until approved.
- ``retired`` — soft-deleted; never hard-deleted, reversible by state.
"""

LIBRARY_FILE_STATES: tuple[LibraryFileState, ...] = ("active", "pending", "retired")

VersionPublishState = Literal["draft", "pending", "published", "rejected"]
"""Closed vocabulary for ``library_file_version.publish_state`` (design §2.2).

- ``draft`` — created, not yet published (personal/project go straight
  to ``published``; a global addition under the approval toggle waits).
- ``pending`` — awaiting a directed approver's decision (§5).
- ``published`` — pickable in tool forms.
- ``rejected`` — the approval was declined; the file keeps its prior
  published version (if any) and scope.
"""

VERSION_PUBLISH_STATES: tuple[VersionPublishState, ...] = (
    "draft",
    "pending",
    "published",
    "rejected",
)


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class LibraryFile(_Base):
    """The logical library entry — mutable metadata + current scope/state.

    ``version`` is the optimistic-lock counter (``version_id_col``),
    matching ``Project`` / ``Scenario`` / ``ParametricStudy``. The
    entry's ``scope`` is its *current* scope; scope changes are audited
    transitions, never deletions. ``state`` and ``scope`` are
    CHECK-constrained from the closed vocabularies so the Python tuples
    and the DB can't drift; a portable CHECK ties ``project_id`` to the
    ``project`` scope.
    """

    __tablename__ = "library_file"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    file_kind: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    """Free-ish category, e.g. ``geometry`` / ``mesh`` /
    ``boundary_conditions`` (optional)."""

    tags: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False, default=list)
    scope: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="personal")
    project_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("project.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    """Set iff ``scope='project'`` (enforced by ``ck_library_file_project_scope``)."""

    owner_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    """The steward; defaults to the creator."""

    handling_label: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    """e.g. ``proprietary`` / ``export-controlled`` (optional, display-only)."""

    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="active")
    created_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
    __table_args__: Any = (
        sa.CheckConstraint(
            f"scope IN ({','.join(repr(s) for s in LIBRARY_SCOPES)})",
            name="ck_library_file_scope",
        ),
        sa.CheckConstraint(
            f"state IN ({','.join(repr(s) for s in LIBRARY_FILE_STATES)})",
            name="ck_library_file_state",
        ),
        sa.CheckConstraint(
            "(scope = 'project' AND project_id IS NOT NULL) "
            "OR (scope <> 'project' AND project_id IS NULL)",
            name="ck_library_file_project_scope",
        ),
    )


class LibraryFileVersion(_Base):
    """One immutable version of a library file (design §2.2).

    Append-only: an edit mints a new version with ``ordinal + 1`` rather
    than mutating an existing row. ``content_hash`` (sha-256 hex) is the
    integrity check and the "these two versions are byte-identical"
    signal. The bytes live at ``stored_path`` on the configured
    ``StorageLocation`` mount.
    """

    __tablename__ = "library_file_version"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    library_file_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("library_file.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    """1, 2, 3 … per file; UNIQUE with ``library_file_id``."""

    stored_path: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    original_filename: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    extension: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="")
    """Lower-case, no leading dot."""

    provenance: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    """Source / origin of the bytes (required at the service layer)."""

    reference: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    """Drawing / spec / doc id, contract, URL (optional)."""

    version_notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    publish_state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="draft")
    created_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__: Any = (
        sa.UniqueConstraint("library_file_id", "ordinal", name="uq_library_file_version_ordinal"),
        sa.CheckConstraint(
            f"publish_state IN ({','.join(repr(s) for s in VERSION_PUBLISH_STATES)})",
            name="ck_library_file_version_publish_state",
        ),
    )


class LibraryFileTool(_Base):
    """A file ↔ tool[, field] assignment (design §2.3).

    A file may be assigned to many tools (N rows). ``tool_alias`` is a
    soft string reference (tools are versioned by alias; the field set is
    resolved at pick time against the selected tool version). A NULL
    ``field_name`` means "any file field on this tool whose
    ``accepted_file_types`` the file's extension satisfies".
    """

    __tablename__ = "library_file_tool"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    library_file_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("library_file.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_alias: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    field_name: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)

    __table_args__: Any = (
        sa.UniqueConstraint(
            "library_file_id",
            "tool_alias",
            "field_name",
            name="uq_library_file_tool_assignment",
        ),
    )


class JobLibraryFileUse(_Base):
    """Per-field provenance: which library version fed which job field.

    Written at submit / promotion time (the API process) for
    traceability ("which jobs used version X?"). Composite PK on
    ``(job_id, field_name)`` — at most one library version per field per
    job. FKs *to* ``job`` and ``library_file_version``; nothing FKs
    *from* ``job`` to here, so the runner needs no new side-effect
    import.
    """

    __tablename__ = "job_library_file_use"

    job_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("job.id", ondelete="CASCADE"),
        primary_key=True,
    )
    field_name: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    library_file_version_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("library_file_version.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
