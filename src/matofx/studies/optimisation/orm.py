"""SQLAlchemy ORM model for optimisation studies (ADR-0009 §12.1).

An :class:`OptimisationStudy` is the **second study sibling**
(``designs/studies-engine.md`` §1): a first-class, adaptive
design-space search that sits under a ``Project`` next to ``Scenario``,
parallel to ``ParametricStudy`` (ADR-0001) and ``JobCohort`` (ADR-0008).
Where a parametric study fans a tool over a *fixed* generated matrix, an
optimisation study drives an *adaptive* outer loop (propose → run →
learn → repeat) until a stopping criterion fires.

This row holds the **frozen configuration** (``config_json`` — the §3.2
target / factors / responses / constraints / strategy / run-mode /
stopping shape) plus the **durable optimiser state** (§10) so a runner
restart can rehydrate and continue. Its child jobs are ordinary
:class:`matofx.hierarchy.orm.Job` rows carrying
``optimisation_study_id`` with ``scenario_id = NULL`` (the
``ck_job_exactly_one_home`` 1-of-3 CHECK enforces the split — migration
0066).

State machine (§3.1)::

    draft → pending_start_approval → running
          → [pending_results_approval] → complete | cancelled | failed

``draft`` exists because configuring is multi-step and partial progress
is saved DB-only (no disk staging until the first batch queues,
[[feedback_save_db_only]]). ``pending_start_approval`` is the
compute-cost gate (§14.1): ``configure`` parks the study here when the
target tool (or any tool in a workflow chain) sets
``optimisation_start_approval_required``, and a nominated approver
holding ``OPTIMISATION_APPROVE`` releases it to ``running``. The
terminal *reason* (`converged` /
`budget_exhausted` / `target_met` / `user_halted` / …) is recorded in
``stop_reason``; ``state`` collapses to ``complete`` / ``cancelled`` /
``failed``.

**Phase 1 scope note.** The ``tool`` evaluation target is fully wired
(``tool_alias`` + ``tool_version_id`` FKs). The ``workflow`` target
(§3.4) is build Phase 11; its ``workflow_alias`` / ``workflow_version_id``
columns exist here so the table shape is final, but they are **logical
pointers without a DB FK for now** (the surrogate-version tag on ``Job``
sets the precedent) — the FK lands with the workflow-target build, when
the referenced workflow tables can be wired without guessing names.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata

OptimisationStudyState = Literal[
    "draft",
    "pending_start_approval",
    "running",
    "pending_results_approval",
    "complete",
    "cancelled",
    "failed",
]
"""Closed vocabulary for ``OptimisationStudy.state`` (design §3.1).

- ``draft`` — being configured; reaped/edited freely, DB-only.
- ``pending_start_approval`` — the per-tool compute-cost gate is open
  (§14.1); a nominated ``OPTIMISATION_APPROVE`` approver releases it to
  ``running`` or rejects it to ``cancelled``.
- ``running`` — the outer loop owns it (build Phase 2+).
- ``pending_results_approval`` — the run reached a stopping criterion but
  the tool's ``post`` / ``both`` policy holds the campaign's results until
  an approver releases them (the results-release gate; migration 0079).
  Results stay withheld (the state is non-terminal) until release flips it
  to ``complete``.
- ``complete`` — a stopping criterion fired (reason in ``stop_reason``).
- ``cancelled`` — user-halted, start-rejected, or results-rejected.
- ``failed`` — every child failed.
"""

OPTIMISATION_STUDY_STATES: tuple[OptimisationStudyState, ...] = (
    "draft",
    "pending_start_approval",
    "running",
    "pending_results_approval",
    "complete",
    "cancelled",
    "failed",
)

TargetKind = Literal["tool", "workflow"]
"""Closed vocabulary for ``OptimisationStudy.target_kind`` (design §3.4).

``tool`` — each candidate is one tool :class:`Job`. ``workflow`` — each
candidate is a full ``WorkflowRun`` (process-chain optimisation; build
Phase 11).
"""

TARGET_KINDS: tuple[TargetKind, ...] = ("tool", "workflow")


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class OptimisationStudy(_Base):
    """One adaptive design-space search within a project (ADR-0009 §3).

    ``version`` is incremented on every update via the SQLAlchemy
    ``version_id_col`` mapper option — the optimistic-lock pattern shared
    with ``Project`` / ``Scenario`` / ``Job`` / ``ParametricStudy`` /
    ``JobCohort``; concurrent writers see
    :class:`sqlalchemy.orm.exc.StaleDataError` on the second commit.

    The ``state`` and ``target_kind`` fields are CHECK-constrained at the
    DB level from :data:`OPTIMISATION_STUDY_STATES` /
    :data:`TARGET_KINDS` so the Python tuple and the DB vocabulary cannot
    drift (migration 0066 carries its own frozen literal copies — a
    migration must never import the ORM).

    ``config_json`` is the frozen §3.2 configuration; it is mutated only
    while ``draft`` and frozen at ``configure``. The optimiser-state
    columns (``optimiser_state_json`` ≤1 MB inline, else
    ``optimiser_state_blob_path`` + ``optimiser_state_sha256``) and
    ``convergence_trace`` are written by the runtime (build Phase 2/3);
    they are nullable / empty for a freshly-configured study.
    """

    __tablename__ = "optimisation_study"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    project_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("project.id", ondelete="RESTRICT"),
        nullable=False,
    )
    """Home project — the ACL source (studies-engine §7). Read = Viewer+,
    mutate = Member+; there is no per-study ACL (inherited from the
    project, exactly as a scenario's is)."""

    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    """Display label; deliberately **not** unique (studies-engine §1)."""

    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")

    target_kind: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="tool")
    """``tool`` or ``workflow`` (§3.4). CHECK-constrained from
    :data:`TARGET_KINDS`."""

    tool_alias: Mapped[str | None] = mapped_column(
        sa.String(64),
        sa.ForeignKey("tool.alias", ondelete="RESTRICT"),
        nullable=True,
    )
    """Set iff ``target_kind='tool'``. The single tool the study searches
    over."""

    tool_version_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("tool_version.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """Tool version pinned at ``configure`` (design §3.1 / §13). Set iff
    ``target_kind='tool'``; mid-study tool drift is rejected. Mirrors
    ``Job.tool_version_id``."""

    workflow_alias: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    """Set iff ``target_kind='workflow'`` (§3.4, build Phase 11). A
    logical pointer for now — the DB FK lands with the workflow-target
    build (see the module docstring)."""

    workflow_version_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        nullable=True,
    )
    """Workflow version pinned at ``configure`` (workflow target). Logical
    pointer for now (build Phase 11)."""

    scenario_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("scenario.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """Dedicated scenario auto-created at ``configure`` for a ``workflow``
    target (§3.4, build Phase 11 / migration 0068) — every candidate
    :class:`WorkflowRun` and its child jobs live here so the run + the job
    1-of-3 home CHECK are satisfied. ``NULL`` for a ``tool`` target (whose
    children are scenario-less, homed on the study itself)."""

    config_json: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    """The frozen §3.2 configuration — target / factors / responses /
    constraints / strategy / run_mode / mop / batch / seed / stopping.
    Mutated only while ``draft``; frozen at ``configure`` after
    ``validate_config`` passes (§3.3)."""

    pinned_refs_json: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    """Library / material / surrogate version pins, written at first child
    queue (studies-engine §4). ``NULL`` until the runtime queues a
    batch (build Phase 2)."""

    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="draft")

    start_approver_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    """Nominated approver when the per-tool start gate fires (§14.1); must
    hold ``OPTIMISATION_APPROVE`` and cannot be the submitter. ``NULL``
    when no gate applies."""

    stop_reason: Mapped[str | None] = mapped_column(sa.String(24), nullable=True)
    """Terminal reason — ``converged`` / ``budget_exhausted`` /
    ``target_met`` / ``user_halted`` / ``grid_exhausted`` /
    ``space_exhausted`` / ``all_children_failed`` — for the convergence-trace
    header + audit (§3.1). ``NULL`` until the study finishes."""

    optimiser_state_json: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    """Inline optimiser state when serialised ≤1 MB (§10). ``NULL`` until
    the first batch persists (build Phase 3)."""

    optimiser_state_blob_path: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    """Disk-blob path when the state would exceed 1 MB (§10)."""

    optimiser_state_sha256: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    """Integrity check for the disk blob (§10 — verified on load, joblib
    only, never raw ``pickle.load`` in the web process)."""

    convergence_trace: Mapped[list[dict[str, Any]]] = mapped_column(
        sa.JSON, nullable=False, default=list
    )
    """Append-only ``[{batch, metric, n_feasible, n_total, wall_clock_s}]``
    (§10) — always JSON (never blob); the source for the Convergence tab
    even when the heavy optimiser state is blob-backed."""

    current_best_json: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    """Best-so-far (single-objective) or Pareto-front summary
    (multi-objective) for the list view (§12.1)."""

    mop_state_json: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    """Latest MOP/CoP analysis + the frozen-factor decision (§5.7, build
    Phase 12 / migration 0069): ``{best_kind, q2, cop: [...], kind_q2,
    cutoff, frozen_factors: [...], updated_at}``. ``NULL`` until the first
    MOP analysis runs (and for every non-MOP study)."""

    last_batch_number: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    """Monotonic batch counter; ``0`` = the seed batch (§11)."""

    halt_requested: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    """Set by the Halt control; read at the top of the loop (§14)."""

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
    archived_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    archived_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
    __table_args__: Any = (
        sa.CheckConstraint(
            # Derived from OPTIMISATION_STUDY_STATES so the DB vocabulary
            # and the Python tuple can never drift (migration 0066 carries
            # its own frozen literal copy).
            f"state IN ({','.join(repr(s) for s in OPTIMISATION_STUDY_STATES)})",
            name="ck_optimisation_study_state",
        ),
        sa.CheckConstraint(
            f"target_kind IN ({','.join(repr(k) for k in TARGET_KINDS)})",
            name="ck_optimisation_study_target_kind",
        ),
        sa.Index("ix_optimisation_study_project_id_state", "project_id", "state"),
        sa.Index("ix_optimisation_study_tool_alias", "tool_alias"),
        sa.Index("ix_optimisation_study_state", "state"),
    )
