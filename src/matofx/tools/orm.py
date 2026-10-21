"""SQLAlchemy ORM model for installed tools."""

from __future__ import annotations

import datetime as _dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata
from matofx.tools.states import DEFAULT_TOOL_STATE


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class Tool(_Base):
    """One row per installed tool.

    The ``descriptor`` column holds the validated descriptor JSON
    exactly as it was at install time; the runner re-loads and
    re-validates it on every job submission so a corrupted row can
    never reach the execution path.

    Tools are never deleted — instead they're archived (set
    ``archived_at``) when no further use is intended. Archived tools
    don't appear in the standard listing, can't be promoted, and
    can't accept new jobs, but the row stays so existing jobs'
    ``tool_alias`` FK keeps resolving. The ``row_version`` column is
    wired to SQLAlchemy's ``version_id_col`` mapper for optimistic
    locking on edits.
    """

    __tablename__ = "tool"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    alias: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default=DEFAULT_TOOL_STATE)
    descriptor: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    bundle_path: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    """Filesystem path to the tool's static bundle (where ``main_script`` lives)."""

    installed_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    archived_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    archived_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    archive_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    """Optional plain-English note shown in the archive history pane."""

    execution_target: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="local")
    """Where this tool runs: ``"local"``, ``"aos"``, or ``"hpc"`` (CHECK-pinned)."""

    aos_host_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("aos_host.id", ondelete="RESTRICT"),
        nullable=True,
    )
    hpc_cluster_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("hpc_cluster.id", ondelete="RESTRICT"),
        nullable=True,
    )
    python_env_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("python_env.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """Required at submit time; nullable on the row to keep tool-fixture
    tests that pre-date the execution backend trivially constructible."""

    resource_request: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    """Operator **override** of the descriptor's ``resources`` block.

    Keys: ``cores``, ``memory_mb``, ``walltime_minutes``, ``gpu_count``,
    ``queue_override``. Translated per-target into ``sbatch`` directives /
    MS HPC parameters at submit time by
    :func:`matofx.runner.supervisor._resource_request_for`.

    **Nullable, and normally NULL** — nothing on the install path writes
    it; only the cross-install ``migrate`` import and hand edits do. Where
    a key is absent the tool's own descriptor shows through, which is the
    usual case. Set a key here only to overrule the tool author for this
    install.

    (This docstring previously said ``memory_gb`` / ``walltime_hours``,
    which no code has ever read — the units are **MB** and **minutes**.
    Corrected 2026-08-13 with D-11.)"""

    requires_gpu: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    run_cap: Mapped[int | None] = mapped_column(sa.Integer, nullable=True, default=None)
    """Per-tool parametric-study run cap (admin/SU-editable on the Edit
    Tool Metadata tab). ``NULL`` falls through to the install-wide
    default (currently 500). Set to allow more for fast tools, or
    fewer for expensive ones. See ADR-0001 2026-05-20 revision."""

    optimisation_start_approval_required: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    """When set, an optimisation study targeting this tool (or any workflow
    chain containing it) routes through the compute-cost **start gate**
    (ADR-0009 §14.1 / migration 0070): ``configure`` parks it in
    ``pending_start_approval`` until a nominated ``optimisation_approver``
    signs off. A mutable per-tool operational lever (like ``run_cap``),
    distinct from the tool's own per-job ``approvals_policy``."""

    teams_pending: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    """Set by a cross-install import when none of the tool's teams exist here
    (ADR-0023 / migration 0098). The tool lands with no teams, which would mean
    "everyone" once released, so ``promote_state`` refuses to move it out of
    ``new`` until an edit saves a non-empty team list (which clears this)."""

    row_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    """Optimistic-lock counter incremented on every update.

    Distinct from :attr:`version` (which is the descriptor's tool
    version, e.g. ``"1.2.3"``). The :class:`Tool` row uses this for
    concurrent-edit detection via SQLAlchemy's ``version_id_col``."""

    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __mapper_args__ = {"version_id_col": row_version}  # noqa: RUF012
    __table_args__: Any = (
        sa.Index("ix_tool_state", "state"),
        sa.Index("ix_tool_archived_at", "archived_at"),
        sa.CheckConstraint(
            "state IN ('new','test','released')",
            name="ck_tool_state",
        ),
        sa.CheckConstraint(
            "execution_target IN ('local','aos','hpc')",
            name="ck_tool_execution_target",
        ),
        sa.CheckConstraint(
            "execution_target <> 'local' OR (aos_host_id IS NULL AND hpc_cluster_id IS NULL)",
            name="ck_tool_execution_target_local",
        ),
        sa.CheckConstraint(
            "execution_target <> 'aos' OR (aos_host_id IS NOT NULL AND hpc_cluster_id IS NULL)",
            name="ck_tool_execution_target_aos",
        ),
        sa.CheckConstraint(
            "execution_target <> 'hpc' OR (hpc_cluster_id IS NOT NULL AND aos_host_id IS NULL)",
            name="ck_tool_execution_target_hpc",
        ),
    )


class ToolVersion(_Base):
    """One immutable snapshot of a tool's state at a point in time.

    A new row is inserted on every install / update_descriptor /
    update_bundle / first-release-snap. Rows are **append-only** by
    code design — the service layer offers no method to UPDATE or
    DELETE them, and the ``Job.tool_version_id`` foreign key uses
    ``ON DELETE RESTRICT`` so the database itself refuses to remove a
    snapshot that's still referenced by a job. The audit log mirrors
    every snapshot insertion so even a DB-side mutation leaves a
    tamper-evident trail in the hash-chained chain.

    The pair (``alias``, ``version``) is unique — re-using a version
    number for the same alias would invite confusion. Bundle paths
    are *immutable* per snapshot too: a replace-bundle creates a fresh
    ``<bundles_dir>/<alias>/<new_version>/`` directory and points the
    new snapshot at it; the previous snapshot's ``bundle_path`` keeps
    resolving for any jobs still tied to that version.

    ``descriptor_snapshot`` is a verbatim copy of the descriptor JSON
    at the time of the snapshot. ``change_summary`` is a single
    operator-facing line (``"Metadata edit — patch bump 0.1.0 →
    0.1.1"``); ``changes`` is the structured per-field old/new map
    rendered as the modal's expansion bullets.
    """

    __tablename__ = "tool_version"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    alias: Mapped[str] = mapped_column(
        sa.String(64),
        sa.ForeignKey("tool.alias", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    descriptor_snapshot: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    bundle_path: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    created_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    change_summary: Mapped[str] = mapped_column(sa.String(512), nullable=False, default="")
    changes: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    """Structured per-field old / new map: ``{"name": {"old": "x", "new": "y"}}``.

    Rendered by the change-log modal as expansion bullets under the
    headline ``change_summary``. Long values are truncated by the
    builder before they land here so a description rewrite doesn't
    bloat the snapshot row."""

    bundle_hash: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, server_default="", default=""
    )
    """SHA-256 hex of the bundle directory contents at snapshot time.

    The V&V cohort key per ADR-0011 / ``designs/vv-tracking.md`` §6.5:
    two ``tool_version`` rows sharing the same ``(alias, bundle_hash)``
    are byte-equivalent installations, so a V&V record on either applies
    to both. Computed once at snapshot-write time by
    :func:`matofx.tools.versioning.compute_bundle_hash`; never recomputed
    on read.

    The empty string ``""`` is a deliberate sentinel meaning "not yet
    computed" — used as the server_default so the migration on a
    greenfield install never fails, and as the fallback in
    :meth:`matofx.tools.ToolService._record_snapshot` when the bundle
    directory isn't reachable on disk. The empty-string row is treated
    as a singleton by :meth:`matofx.vv.VVService.summarise_tool_status`
    (no inheritance to or from), so a "" leaks at most into the
    snapshot's own badge, never into another version's."""

    __table_args__: Any = (
        sa.UniqueConstraint("alias", "version", name="uq_tool_version_alias_version"),
        sa.Index("ix_tool_version_alias_created", "alias", "created_at"),
        sa.Index("ix_tool_version_alias_bundle_hash", "alias", "bundle_hash"),
    )


class ToolDraft(_Base):
    """A saved, in-progress *new tool* setup (not yet installed).

    Lets an author save partial work on the New Tool form and resume it
    later from the Tools page. Per the save-to-DB-only convention, a
    draft holds only the typed metadata plus the inputs/outputs wizard
    state (as a descriptor-shaped JSON blob) — never the uploaded bundle
    files; those are re-attached at the final install. A draft is
    deleted once the tool installs successfully, or explicitly by its
    owner. Drafts are private to their owner.

    Unlike :class:`Tool`, the ``alias`` here is just a remembered form
    value: it isn't unique, isn't a foreign key, and may be blank while
    the author is still deciding.
    """

    __tablename__ = "tool_draft"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    owner_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(sa.String(128), nullable=False, default="")
    """Display label for the drafts list — the tool's Name, or empty when
    the author hasn't typed one yet (the UI shows "(untitled draft)")."""

    alias: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="")
    """Remembered alias form value (not unique, not an FK)."""

    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    """The full draft: metadata fields + the wizard's descriptor-shaped
    contribution (``inputs`` / ``outputs`` / groups / presets / globs /
    backend). Round-trips through the same wizard helpers the Edit page
    uses, so resuming rebuilds the tabs exactly."""

    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__: Any = (sa.Index("ix_tool_draft_owner_updated", "owner_user_id", "updated_at"),)
