"""SQLAlchemy ORM for workflows (admin-released and user-owned custom).

Tables:

- :class:`Workflow` — one row per installed workflow (mirrors the
  ``tool`` row's shape: alias / name / version / state / archived_at /
  descriptor JSON). The ``kind`` discriminator splits ``admin`` rows
  (installed via the admin surface, ramp through new/test/released,
  visible by team membership) from ``custom`` rows (user-owned,
  always-released, visibility controlled by ``visibility`` +
  :class:`WorkflowShare`).
- :class:`WorkflowMember` — one row per (workflow, tool) pair, with
  an explicit ``position`` integer driving tab order. Unique on
  ``(workflow_id, position)`` and ``(workflow_id, tool_alias)``.
- :class:`WorkflowShare` — junction row per share recipient. Only
  populated for ``kind='custom'`` workflows in
  ``visibility='shared'`` state.
- :class:`WorkflowVersion` — append-only snapshot table. A new row
  lands on every install / edit / archive so a workflow's history
  is reconstructable even if member tools change underneath it.
"""

from __future__ import annotations

import datetime as _dt
from enum import StrEnum
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata
from matofx.tools.states import DEFAULT_TOOL_STATE


class WorkflowKind(StrEnum):
    """Discriminator on :class:`Workflow.kind`."""

    ADMIN = "admin"
    """Admin-released workflow: ramps through new/test/released,
    visibility driven by ``descriptor.teams``, no owner."""

    CUSTOM = "custom"
    """User-owned workflow: lives in the released state until
    archived, visibility driven by ``visibility`` + share list."""


class WorkflowVisibility(StrEnum):
    """Allowed values for :class:`Workflow.visibility` (custom rows only)."""

    PRIVATE = "private"
    """Only the owner can see / run this workflow."""

    SHARED = "shared"
    """Owner + every user in :class:`WorkflowShare` can see / run."""


# State a custom workflow always lives in until archived. Custom
# workflows do not ramp through ``new``/``test`` — they're either
# active (released) or archived.
CUSTOM_WORKFLOW_STATE: Final[str] = "released"


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class Workflow(_Base):
    """One installed workflow.

    Mirrors :class:`matofx.tools.orm.Tool` in shape so admins reuse
    the lifecycle they already know: ``new`` → ``test`` → ``released``,
    promotable in either direction by anyone holding
    ``workflows.manage``, archived (not deleted) when no longer
    wanted. Tool versions float — every run resolves member tools
    against whatever version is currently in the ``released`` state.

    The ``descriptor`` JSON carries the lighter "what makes this
    workflow this workflow" metadata (description, teams list,
    optional approver list for the future). The ordered tool list
    lives in :class:`WorkflowMember` so the archive-cascade query is
    a clean SQL join rather than a JSON-extract.
    """

    __tablename__ = "workflow"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    alias: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    """Globally unique. Custom-workflow aliases are synthesised in the
    service layer (``custom_<owner_id>_<slug>``) so two users picking
    the same display name don't collide on the unique constraint."""

    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    kind: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=WorkflowKind.ADMIN.value
    )
    """``'admin'`` or ``'custom'``. Discriminates row shape:
    ``admin`` rows leave ``owner_id``/``visibility`` NULL and use the
    full ``new``/``test``/``released`` state machine; ``custom`` rows
    require both columns and live in ``'released'`` until archived."""

    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default=DEFAULT_TOOL_STATE)
    descriptor: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    """Loosely-shaped JSON: ``description``, ``teams`` (list[str]), and
    any future workflow-level metadata. Member ordering lives in
    :class:`WorkflowMember` rows, not here.

    For custom workflows ``teams`` is unused; admin / share semantics
    handle visibility instead. The ``copied_from_workflow_id`` key may
    be set when the row was created via "Make a copy" so the admin
    inspector can trace lineage."""

    installed_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    """The user who installed (admin) or first created (custom) this
    workflow. Survives ownership transfers; see :attr:`owner_id` for
    the *current* owner of a custom workflow."""

    owner_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """NULL for admin workflows; NOT NULL for custom workflows. The
    owner is the only user who can edit / archive / share / change
    visibility. Enforced by the combined CHECK on (kind, owner_id)."""

    visibility: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    """``'private'`` or ``'shared'`` for custom rows; NULL for admin.

    ``'private'`` means owner-only access (the share list is empty);
    ``'shared'`` means owner plus every row in :class:`WorkflowShare`."""

    archived_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    archived_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    archive_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    """Plain-English note shown alongside the archive event in the
    admin history pane. Set programmatically when the archive is
    triggered by the tool-archive cascade — e.g. ``"Auto-archived:
    member tool 'thermal_sim' was archived"``. Also captured when an
    admin archives a custom workflow via the inspector (then required,
    not optional)."""

    teams_pending: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    """Mirror of :attr:`Tool.teams_pending` (ADR-0023): an imported workflow
    whose teams didn't resolve here can't leave ``new`` until teams are set."""

    row_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    """Optimistic-lock counter, matching :class:`Tool.row_version`."""

    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __mapper_args__ = {"version_id_col": row_version}  # noqa: RUF012
    __table_args__: Any = (
        sa.Index("ix_workflow_state", "state"),
        sa.Index("ix_workflow_archived_at", "archived_at"),
        sa.Index("ix_workflow_kind", "kind"),
        sa.Index("ix_workflow_owner_id", "owner_id"),
        sa.CheckConstraint(
            "kind IN ('admin','custom')",
            name="ck_workflow_kind",
        ),
        # Combined CHECK pinning the legal column combinations per kind.
        # Admin rows: full state machine, no owner, no visibility.
        # Custom rows: owner required, visibility required, state pinned
        # to 'released' (custom workflows don't ramp).
        sa.CheckConstraint(
            "("
            "(kind = 'admin' AND owner_id IS NULL AND visibility IS NULL "
            "AND state IN ('new','test','released'))"
            " OR "
            "(kind = 'custom' AND owner_id IS NOT NULL "
            "AND visibility IN ('private','shared') AND state = 'released')"
            ")",
            name="ck_workflow_kind_state_owner_visibility",
        ),
    )


class WorkflowMember(_Base):
    """One (workflow, tool) membership row.

    ``position`` is 1-based and dense per workflow: a workflow with
    three tools has positions 1, 2, 3 — no gaps. Reordering rewrites
    every row's position so the invariant holds.

    The tool reference uses the canonical ``alias`` (not the row id)
    because tools float across versions (the row id can change when
    an admin replaces a bundle). The alias is the durable identifier
    a workflow uses to resolve "whatever version of Tool X is currently
    in the ``released`` state" at run time.

    Unique on ``(workflow_id, position)`` and ``(workflow_id, tool_alias)`` —
    no tool appears twice in one workflow, and each position slot is
    filled exactly once.
    """

    __tablename__ = "workflow_member"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    workflow_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("workflow.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_alias: Mapped[str] = mapped_column(
        sa.String(64),
        sa.ForeignKey("tool.alias", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    """1-based slot order. Drives tab rendering on the run page."""

    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__: Any = (
        sa.UniqueConstraint(
            "workflow_id", "position", name="uq_workflow_member_workflow_id_position"
        ),
        sa.UniqueConstraint(
            "workflow_id", "tool_alias", name="uq_workflow_member_workflow_id_tool_alias"
        ),
        sa.CheckConstraint("position >= 1", name="ck_workflow_member_position_positive"),
    )


class WorkflowShare(_Base):
    """One share grant: ``(workflow, recipient_user)``.

    Junction row written when a custom workflow's owner adds a
    recipient to the share list. Only meaningful for
    ``kind='custom'`` workflows in ``visibility='shared'``: when an
    admin or custom workflow is in any other state the table is empty
    for that row. The service layer guards against orphaned shares
    when visibility flips back to ``'private'``.

    PK is the composite ``(workflow_id, user_id)`` — a user can be
    listed against a given workflow exactly once. ON DELETE CASCADE
    fires from both ``workflow`` and ``user`` so removing either end
    cleans up the share automatically. ``shared_by_user_id`` is the
    user who *added* the recipient (always equal to the workflow's
    ``owner_id`` at grant time, but captured here so an ownership
    transfer in the future doesn't rewrite history).
    """

    __tablename__ = "workflow_share"

    workflow_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("workflow.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    shared_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    shared_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )

    __table_args__: Any = (sa.Index("ix_workflow_share_user_id", "user_id"),)


class WorkflowRun(_Base):
    """One execution of a workflow inside a scenario.

    A run is created when the user clicks "Submit new workflow run".
    The service inserts this row plus one child :class:`Job` row per
    workflow member (each in ``created`` state with empty inputs).
    The user then walks the tabbed UI submitting each tool in turn;
    upstream-output values flow into downstream-input fields
    automatically as parent jobs complete.

    Unique on ``(scenario_id, workflow_id)`` — a scenario can host
    many workflow runs of *different* workflows, but never two runs
    of the same workflow (mirrors the one-tool-per-scenario rule
    that's already in place for standalone jobs).

    State is **derived** from the child jobs at read time rather than
    stored — there's exactly one source of truth (each child job's
    own ``state`` column) and no risk of drift. See
    :class:`matofx.workflows.run_service.WorkflowRunService.compute_state`.
    """

    __tablename__ = "workflow_run"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    workflow_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("workflow.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    workflow_version_snapshot: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    """Workflow's own ``version`` at the moment the run was started.
    Recorded so a later admin edit to the workflow definition doesn't
    rewrite history — the audit trail can answer "what shape was the
    workflow when this run started?" without joining against
    workflow_version."""

    scenario_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("scenario.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    """User-visible label for this run (e.g. ``"Heat Transfer for
    Project X, Scenario Y"``). Editable in Phase B+1 if needed."""

    submitted_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cancel_requested_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    cancel_requested_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    auto_advance: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    """When set, the runner queues each child as soon as its upstream
    members complete (``WorkflowRunService.advance``) — a headless run
    that drives itself to completion (migration 0067). Manual UI runs
    leave this ``False`` and are submitted tab-by-tab as before."""

    open_inputs_json: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    """Open (non-wired) input values for an auto-advancing run, keyed
    ``{tool_alias: {input_name: value}}``. For a ``workflow``-target
    optimisation candidate (ADR-0009 §3.4) this is the proposed factor
    vector distributed across the chain's members. ``NULL`` for manual
    runs (the user types each tab's inputs)."""

    optimisation_study_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("optimisation_study.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    """Run-level membership for the ``workflow`` optimisation target (§3.4
    / §12.2). The child ``Job`` rows keep their normal scenario home; only
    the run carries the study FK, so the job 1-of-3 home CHECK is
    unaffected. ``NULL`` for ordinary (non-optimisation) runs."""

    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
    __table_args__: Any = (
        # One run per workflow per scenario — but only for *manual* runs
        # (migration 0068). An optimisation study runs the same workflow
        # many times in its dedicated scenario (one run per candidate,
        # §3.4), so the uniqueness is partial: it skips rows that carry an
        # ``optimisation_study_id``.
        sa.Index(
            "uq_workflow_run_scenario_workflow",
            "scenario_id",
            "workflow_id",
            unique=True,
            sqlite_where=sa.text("optimisation_study_id IS NULL"),
            postgresql_where=sa.text("optimisation_study_id IS NULL"),
        ),
    )


class WorkflowVersion(_Base):
    """Append-only snapshot of one workflow at a point in time.

    A new row is written on every install / edit / archive event so
    the change-log modal can reconstruct what the workflow looked like
    each time the admin saved it. Rows are never updated or deleted by
    the service — the snapshot is the durable record of how the
    workflow evolved.

    ``descriptor_snapshot`` is a verbatim copy of the workflow's
    descriptor JSON at the time. ``members_snapshot`` is a list of
    ``{"tool_alias": "...", "position": int}`` dicts capturing the
    membership in order. ``changes`` is the structured per-field
    old/new map the modal renders as bullets under ``change_summary``.
    """

    __tablename__ = "workflow_version"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    alias: Mapped[str] = mapped_column(
        sa.String(64),
        sa.ForeignKey("workflow.alias", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    version: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    descriptor_snapshot: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    members_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(sa.JSON, nullable=False)
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

    __table_args__: Any = (
        sa.UniqueConstraint("alias", "version", name="uq_workflow_version_alias_version"),
        sa.Index("ix_workflow_version_alias_created", "alias", "created_at"),
    )
