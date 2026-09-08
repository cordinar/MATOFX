"""SQLAlchemy ORM models for the project / scenario / job hierarchy.

Per ADR-0002 the project access model is a **per-user four-role ACL**
with an immutable Creator, the §D20 no-delete / ``state`` lifecycle,
and an admin-configurable typed metadata taxonomy.

- ``project`` — top-level container. ``creator_user_id`` is written
  once at creation and never changes (provenance + permanent authority
  floor). Carries the ``state`` lifecycle trio.
- ``project_acl`` — per-user grant of ``owner`` / ``member`` /
  ``viewer``. The Creator is *not* an ACL row (it is
  ``project.creator_user_id``, resolved to Owner-equivalent in code).
- ``scenario`` — child of project; inherits the project ACL wholesale;
  carries the same ``state`` trio.
- ``job`` — has **exactly one home**: a ``scenario`` (ad-hoc or
  workflow-run child) *or* a ``parametric_study`` (study child), never
  neither and never both. ``parametric_study`` lives in
  :mod:`matofx.studies.orm`. ``Job.state`` is the *run* lifecycle, a
  separate concept from the archive/no-delete lifecycle above.
- ``project_metadata_field`` / ``project_metadata_value`` /
  ``project_metadata`` — admin-configurable typed metadata taxonomy
  and the per-project selections.

Phase 1's team-keyed ``project_team`` ACL is superseded (ADR-0002);
teams revert to a bulk-pick UX convenience and are never an ACL
principal. Per-scenario ACL tightening and clone remain v1.x.
"""

from __future__ import annotations

import datetime as _dt
from enum import StrEnum
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata

JobState = Literal[
    "created",
    "queued",
    "running",
    "complete",
    "failed",
    "pending_approval",
    "denied",
    "pending_results",
    "results_withheld",
]
"""The closed vocabulary for ``Job.state``.

The first five are the original run lifecycle. The last four back
Job Approvals (§16.15):

- ``pending_approval`` — held before the run; a pre/both-policy tool
  is awaiting an approver. DB-only, no working dir staged yet.
- ``denied`` — pre-approval rejected. Terminal; the job never ran.
- ``pending_results`` — ran successfully but a post/both-policy tool
  is withholding the outputs until an approver releases them.
- ``results_withheld`` — post-approval rejected. Terminal; the run
  happened, outputs are retained on disk but never released to the
  requestor (approvers/admins keep access).
"""

JOB_STATES: tuple[JobState, ...] = (
    "created",
    "queued",
    "running",
    "complete",
    "failed",
    "pending_approval",
    "denied",
    "pending_results",
    "results_withheld",
)


ProjectState = Literal["active", "archived", "cancelled"]
"""The §D20 no-delete lifecycle for ``project`` / ``scenario``.

Nothing is ever hard-deleted. ``archived`` / ``cancelled`` hide a row
from default views but the data stays in the DB forever. Distinct from
``Job.state`` (the run lifecycle)."""

PROJECT_STATES: tuple[ProjectState, ...] = ("active", "archived", "cancelled")


class ProjectAccessLevel(StrEnum):
    """Effective capability tier a user has on a project.

    Resolved from Creator / ``projects.see_all`` / the per-user
    ``project_acl`` role (ADR-0002). The stored ACL *role* vocabulary
    is :data:`PROJECT_ACL_ROLES`; this enum is the resolved tier the
    gate checks compare against. Mapping: ``viewer → VIEW``,
    ``member → EDIT``, ``owner → OWNER``, Creator → ``OWNER``.
    """

    NONE = "none"
    VIEW = "view"
    EDIT = "edit"
    OWNER = "owner"


# Roles stored in ``project_acl.role``. ``creator`` is the
# ``project.creator_user_id`` column (immutable provenance + authority),
# never an ACL row, so it is deliberately absent here.
PROJECT_ACL_ROLES: tuple[str, ...] = ("owner", "member", "viewer")

# role string → resolved capability tier.
ACL_ROLE_TO_LEVEL: dict[str, ProjectAccessLevel] = {
    "owner": ProjectAccessLevel.OWNER,
    "member": ProjectAccessLevel.EDIT,
    "viewer": ProjectAccessLevel.VIEW,
}


ProjectMetadataFieldType = Literal["single_select", "multi_select", "date", "number"]
"""Admin-configurable project metadata field types (ADR-0002).

``single_select`` / ``multi_select`` draw from
``project_metadata_value``; ``date`` / ``number`` are typed scalars
with no value list. Free-text / file / user-ref types are v1.x."""

PROJECT_METADATA_FIELD_TYPES: tuple[ProjectMetadataFieldType, ...] = (
    "single_select",
    "multi_select",
    "date",
    "number",
)

SELECT_FIELD_TYPES: frozenset[str] = frozenset({"single_select", "multi_select"})

NumberFormat = Literal["integer", "decimal", "pattern"]
"""How a ``number`` metadata field is interpreted (UI sub-choice).

- ``integer`` — a whole number; stored in ``project_metadata.value_num``.
- ``decimal`` — any real number; stored in ``value_num``.
- ``pattern`` — a masked alphanumeric code (e.g. ``AB-123-4567``); the
  mask lives in :attr:`ProjectMetadataField.format_mask` and the entered
  value is stored as text in ``project_metadata.value_text`` (it isn't
  numeric). ``NULL`` ``number_format`` on a ``number`` field means
  ``decimal`` (the original behaviour before this column existed)."""

NUMBER_FORMATS: tuple[NumberFormat, ...] = ("integer", "decimal", "pattern")

DEFAULT_NUMBER_FORMAT: NumberFormat = "decimal"


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class Project(_Base):
    """A top-level container for scenarios and jobs.

    ``creator_user_id`` is written once at creation and **never
    updated** — it is both provenance ("who created this") and the
    permanent authority floor (resolves to Owner-equivalent). The
    service layer is the only writer and never sets it after insert;
    there is no DB trigger.

    ``version`` is incremented on every update via the SQLAlchemy
    ``version_id_col`` mapper option; concurrent writers see
    :class:`sqlalchemy.orm.exc.StaleDataError` on the second commit.
    """

    __tablename__ = "project"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    creator_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    state: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="active", server_default="active"
    )
    state_changed_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    state_changed_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
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
        sa.UniqueConstraint("creator_user_id", "name", name="uq_project_creator_name"),
        sa.CheckConstraint(
            f"state IN ({','.join(repr(s) for s in PROJECT_STATES)})",
            name="ck_project_state",
        ),
    )


class ProjectAcl(_Base):
    """Per-user grant of a role on a project (ADR-0002).

    Composite PK ``(project_id, user_id)`` guarantees at most one role
    per user per project. ``role`` is one of :data:`PROJECT_ACL_ROLES`
    — ``creator`` is *not* storable here (it is
    ``project.creator_user_id``). A user's effective access is the max
    of Creator-implied OWNER, ``projects.see_all``-implied OWNER, and
    this row's resolved tier.
    """

    __tablename__ = "project_acl"

    project_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("project.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    """``"owner"`` | ``"member"`` | ``"viewer"``. Creator is computed."""

    granted_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    granted_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__: Any = (
        sa.CheckConstraint(
            f"role IN ({','.join(repr(r) for r in PROJECT_ACL_ROLES)})",
            name="ck_project_acl_role",
        ),
        sa.Index("ix_project_acl_user_id", "user_id"),
    )


class Scenario(_Base):
    """A scenario within a project — a logical group of jobs.

    Inherits the project ACL wholesale (no per-scenario tightening in
    v1 — deferred per ADR-0002). Carries the same ``state`` lifecycle
    trio as :class:`Project`.
    """

    __tablename__ = "scenario"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    project_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    state: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="active", server_default="active"
    )
    state_changed_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    state_changed_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
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
        sa.UniqueConstraint("project_id", "name", name="uq_scenario_project_name"),
        sa.CheckConstraint(
            f"state IN ({','.join(repr(s) for s in PROJECT_STATES)})",
            name="ck_scenario_state",
        ),
    )


class Job(_Base):
    """A single tool invocation, homed under a scenario *or* a study.

    Exactly one of ``scenario_id`` / ``parametric_study_id`` is set
    (the ``ck_job_exactly_one_home`` CHECK enforces the XOR). Ad-hoc
    and workflow-run children carry ``scenario_id``; parametric-study
    children carry ``parametric_study_id`` with ``scenario_id = NULL``.
    NULL ``scenario_id`` rows are distinct in both SQLite and Postgres,
    so N same-tool study children never collide under the existing
    ``uq_job_scenario_tool_standalone`` partial index — which is left
    unchanged (design §2).

    ``tool_alias`` is a FK on ``tool.alias`` — the runner refuses to
    create a job for a tool that isn't installed, and uninstalling a
    tool that has jobs is refused at the DB level (RESTRICT).

    The ``state`` field uses the same vocabulary as the legacy app
    and is CHECK-constrained at the DB level. Timestamp CHECK
    constraints enforce ``created_at <= started_at <= completed_at``
    so a tampered DB row can't claim impossible orderings.
    """

    __tablename__ = "job"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    scenario_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("scenario.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    """The job's scenario home, or ``NULL`` for a parametric-study
    child (which is homed under ``parametric_study_id`` instead). The
    ``ck_job_exactly_one_home`` CHECK guarantees exactly one of the
    two is set. Ad-hoc and workflow-run children always set this."""
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    tool_alias: Mapped[str] = mapped_column(
        sa.String(64),
        sa.ForeignKey("tool.alias", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    tool_version_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("tool_version.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    """Captured snapshot of the tool at submit time.

    Pinning the version here means a metadata edit or bundle replace
    on the tool *after* the job was submitted leaves the job's bundle
    + descriptor exactly as they were when the operator clicked Submit.
    The runner reads ``tool_version.bundle_path`` (not
    ``tool.bundle_path``) to stage the working dir."""
    state: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="created")
    inputs: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
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
    started_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    storage_location_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("storage_location.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """Captured at submit time so future drive moves don't break old jobs."""

    working_dir_relative: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    """Path under ``storage_location.mount_path`` (e.g. ``jobs/job_0000123``)."""

    scheduler_job_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    """Slurm job id / MS HPC job id, populated on HPC submission."""

    heartbeat_state: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    """Last seen heartbeat-file state: ``"pending"`` / ``"running"`` /
    ``"complete"`` / ``"failed"``. Distinct from :attr:`state` because the
    job may be in DB ``running`` while the heartbeat shows ``failed`` for
    a brief window before the runner promotes the canonical state."""

    heartbeat_last_seen_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    exit_code: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    cancel_requested_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    cancel_requested_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    workflow_run_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("workflow_run.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    """When this job is part of a workflow run, the FK to the parent
    run. ``NULL`` for standalone jobs (every job submitted before
    Phase B + every direct tool job)."""

    workflow_position: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    """1-based slot index within the workflow run's member list — the
    tab ordering on the run page. ``NULL`` for standalone jobs."""

    parametric_study_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("parametric_study.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    """When this job is a parametric-study child, the FK to the parent
    study (defined in :mod:`matofx.studies.orm`). ``NULL`` for ad-hoc
    and workflow-run jobs. Exactly one of ``scenario_id`` /
    ``parametric_study_id`` / ``optimisation_study_id`` is set via the
    ``ck_job_exactly_one_home`` CHECK. Study children are viewed and
    acted on from the Parametric Studies surface, never My Jobs
    (design §9 — a study may have hundreds of children)."""

    optimisation_study_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("optimisation_study.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    """When this job is an optimisation-study child, the FK to the parent
    study (defined in :mod:`matofx.studies.optimisation.orm`, ADR-0009
    §12.2). ``NULL`` for ad-hoc / workflow-run / parametric jobs.
    ``RESTRICT`` on delete — studies are archived, never deleted (D20).
    Mutually exclusive with ``scenario_id`` / ``parametric_study_id`` via
    the ``ck_job_exactly_one_home`` 1-of-3 CHECK (migration 0066)."""

    optimisation_metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        sa.JSON, nullable=True
    )
    """Per-child optimisation selection metadata (ADR-0009 §12.4):
    ``{batch, proposed_by, acquisition_value?, feasible?}``. ``NULL`` for
    every non-optimisation job; read only by the optimisation study
    surfaces, so it stays off the hot ``Job`` path."""

    surrogate_model_version_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        nullable=True,
        index=True,
    )
    """Lineage tag for a surrogate-as-tool job (ADR-0008 §5.4): which
    ``surrogate_model_version`` produced this job's prediction. A
    *logical* pointer (no DB FK — adding one to ``job`` would force a
    batch-recreate of its partial unique indexes); the service writes a
    valid id. ``NULL`` for every real-solver job. ADR-0010 reads it to
    draw the surrogate provenance edge."""

    priority: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    """Skip-queue flag.

    When ``True``, the runner's pickup loop drains this job ahead of
    ``priority=False`` jobs (ordered by ``priority DESC, created_at
    ASC``). Set by admins / super-users via the "Skip Queue" button on
    the submit form — used to push urgent runs (e.g. a hot-fix retest
    or a customer demo) past whatever else is queued. Resource /
    licence checks still apply; this only changes the pickup *order*,
    not the gating."""

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
    __table_args__: Any = (
        sa.Index("ix_job_state", "state"),
        # One tool per scenario *for standalone jobs only* — locked by
        # the user 2026-05-08: "any given scenario cannot ever contain
        # the same tool more than once". Workflows relax the rule
        # because two different workflow runs in the same scenario can
        # legitimately each contain the same tool (see Phase B). The
        # partial unique index applies the rule only when
        # ``workflow_run_id IS NULL`` (standalone jobs); workflow
        # children get their own uniqueness via
        # ``uq_job_workflow_run_tool`` below.
        sa.Index(
            "uq_job_scenario_tool_standalone",
            "scenario_id",
            "tool_alias",
            unique=True,
            sqlite_where=sa.text("workflow_run_id IS NULL"),
            postgresql_where=sa.text("workflow_run_id IS NULL"),
        ),
        # One tool per workflow run — mirrors the workflow definition's
        # "no duplicate members" rule at the DB level.
        sa.Index(
            "uq_job_workflow_run_tool",
            "workflow_run_id",
            "tool_alias",
            unique=True,
            sqlite_where=sa.text("workflow_run_id IS NOT NULL"),
            postgresql_where=sa.text("workflow_run_id IS NOT NULL"),
        ),
        sa.CheckConstraint(
            # Derived from JOB_STATES so the DB vocabulary and the
            # Python tuple can never drift — adding a state means
            # editing JOB_STATES + shipping the matching migration
            # (which carries its own frozen literal). repr() yields
            # single-quoted literals: state IN ('created',...).
            f"state IN ({','.join(repr(s) for s in JOB_STATES)})",
            name="ck_job_state",
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR started_at >= created_at",
            name="ck_job_started_after_created",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR (started_at IS NOT NULL AND completed_at >= started_at)",
            name="ck_job_completed_after_started",
        ),
        sa.CheckConstraint(
            "heartbeat_state IS NULL "
            "OR heartbeat_state IN ('pending','running','complete','failed')",
            name="ck_job_heartbeat_state",
        ),
        # Every job has exactly one home: a scenario (ad-hoc / workflow
        # child), a parametric study (ADR-0001 child), or an optimisation
        # study (ADR-0009 child). The two-way XOR (migration 0032) was
        # widened to this exactly-one-of-three CASE-sum by migration 0066.
        # CASE-sum is portable: ``= 1`` evaluates identically on SQLite
        # and Postgres, where a bare ``::int`` cast would not. Frozen
        # identically in migration 0066.
        sa.CheckConstraint(
            "(CASE WHEN scenario_id IS NOT NULL THEN 1 ELSE 0 END"
            " + CASE WHEN parametric_study_id IS NOT NULL THEN 1 ELSE 0 END"
            " + CASE WHEN optimisation_study_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_job_exactly_one_home",
        ),
    )


# ---- metadata taxonomy (ADR-0002) ------------------------------------------


class ProjectMetadataField(_Base):
    """An admin-defined project metadata field.

    Select-type fields draw their allowed options from
    :class:`ProjectMetadataValue`; ``date`` / ``number`` fields have no
    value list. Retiring a field sets ``active = False`` (no-delete per
    §D20) — it is no longer offered on the create/edit form but stays
    on projects that already use it.
    """

    __tablename__ = "project_metadata_field"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    key: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    field_type: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    number_format: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    """For ``field_type == "number"``, one of :data:`NUMBER_FORMATS`
    (``NULL`` ⇒ ``decimal``). Always ``NULL`` for non-number fields."""
    format_mask: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    """The mask string when ``number_format == "pattern"`` (e.g.
    ``AA-000-0000``; ``0`` = digit, ``A`` = letter, other chars are
    literal). ``NULL`` otherwise."""
    required: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    display_order: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )
    show_in_table: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )
    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    created_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    state_changed_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    state_changed_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__: Any = (
        sa.CheckConstraint(
            f"field_type IN ({','.join(repr(t) for t in PROJECT_METADATA_FIELD_TYPES)})",
            name="ck_project_metadata_field_type",
        ),
    )


class ProjectMetadataValue(_Base):
    """An allowed value for a select-type metadata field.

    Retiring a value sets ``active = False`` (no-delete per §D20): it
    is no longer offered for new selections but is retained on projects
    that already reference it.
    """

    __tablename__ = "project_metadata_value"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    field_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("project_metadata_field.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    value: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    display_order: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )
    active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )

    __table_args__: Any = (
        sa.UniqueConstraint("field_id", "value", name="uq_project_metadata_value"),
    )


class ProjectMetadata(_Base):
    """A single typed metadata selection on a project.

    One row per scalar value, so a multi-select with N chosen values
    is N rows. Exactly one of ``value_id`` / ``value_num`` /
    ``value_date`` is populated, matching the field's
    :attr:`ProjectMetadataField.field_type`; the CHECK enforces
    exactly-one and the service validates the column↔type match.
    """

    __tablename__ = "project_metadata"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    project_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    field_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("project_metadata_field.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    value_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("project_metadata_value.id", ondelete="RESTRICT"),
        nullable=True,
    )
    value_num: Mapped[float | None] = mapped_column(sa.Numeric, nullable=True)
    value_text: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    """Set for ``number`` fields whose ``number_format == "pattern"``
    (a masked alphanumeric code that isn't numeric)."""
    value_date: Mapped[_dt.date | None] = mapped_column(sa.Date, nullable=True)

    __table_args__: Any = (
        sa.CheckConstraint(
            "( (CASE WHEN value_id IS NOT NULL THEN 1 ELSE 0 END) "
            "+ (CASE WHEN value_num IS NOT NULL THEN 1 ELSE 0 END) "
            "+ (CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END) "
            "+ (CASE WHEN value_date IS NOT NULL THEN 1 ELSE 0 END) ) = 1",
            name="ck_project_metadata_exactly_one_value",
        ),
        sa.Index("ix_project_metadata_field_id", "field_id"),
    )
