"""SQLAlchemy ORM model for parametric studies.

A :class:`ParametricStudy` fans a single tool out over a generated
input matrix. Per ADR-0001 / ``designs/parametric-studies.md`` §2 it
is a **child of project, parallel to ``Scenario``** — *not* a child of
a scenario. Its child jobs are ordinary :class:`matofx.hierarchy.orm.Job`
rows that carry ``parametric_study_id`` with ``scenario_id = NULL``
(the home-exactly-one CHECK on ``job`` enforces the split).

The study row pins the tool ``(alias, version)`` at creation
(``tool_alias`` + ``tool_version_id``, mirroring ``Job``) so a
mid-study tool update can't split the response surface across
versions (design §5 / §13.1). ACL is inherited from the parent
project exactly as a scenario's is — there is no per-study ACL.

Per design §2 the study **name is deliberately not unique** (unlike
``scenario``): it is auto-suggested, user-editable, and only
project-scoped for display. No-delete (D20) applies — studies are
archived, never deleted; draft studies are reaped on the existing
``draft_jobs_days`` timer with their child draft jobs.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata

StudyState = Literal[
    "draft",
    "pending_approval",
    "running",
    "complete",
    "partial",
    "failed",
    "cancelled",
    "rejected",
]
"""Closed vocabulary for ``ParametricStudy.state`` (design §8).

- ``draft`` — created, factor spec being edited; reaped on the
  ``draft_jobs_days`` timer like a draft job.
- ``pending_approval`` — submitted against an approval-gated tool;
  the single study-level gate is open (design §7).
- ``running`` — children queued/running.
- ``complete`` — every child completed.
- ``partial`` — ≥1 child completed and ≥1 failed/cancelled, none
  outstanding.
- ``failed`` — every child failed.
- ``cancelled`` — study-level cancel cascaded to the children.
- ``rejected`` — the pre-approval gate was rejected; frozen and
  immutable (D20); the user clones a new study to retry.
"""

STUDY_STATES: tuple[StudyState, ...] = (
    "draft",
    "pending_approval",
    "running",
    "complete",
    "partial",
    "failed",
    "cancelled",
    "rejected",
)

StudyMode = Literal[
    "table",
    "list",
    "grid",
    "frac_factorial",
    "plackett_burman",
    "lhs",
    "sobol",
    "random",
    "ccd",
    "box_behnken",
]
"""Closed vocabulary for ``ParametricStudy.mode`` (design §1 / §4).

``table`` user-authored run matrix (exact named configurations,
distinct from the cross-product ``list``) · ``list`` explicit value
sets · ``grid`` full-factorial · ``frac_factorial`` /
``plackett_burman`` screening designs · ``lhs`` / ``sobol`` /
``random`` space-filling · ``ccd`` / ``box_behnken``
response-surface (RSM) designs.
"""

STUDY_MODES: tuple[StudyMode, ...] = (
    "table",
    "list",
    "grid",
    "frac_factorial",
    "plackett_burman",
    "lhs",
    "sobol",
    "random",
    "ccd",
    "box_behnken",
)

SourceKind = Literal["parametric", "optimisation", "cohort"]
"""Closed vocabulary for ``StudyResult.source_kind`` (studies-engine §6).

The discriminator that lets one ``study_result`` store feed the three
study siblings — ``parametric`` (ADR-0001, the only producer today),
``optimisation`` (ADR-0009), and ``cohort`` (ADR-0008). ``source_id``
is the logical FK into the matching sibling table; the pair is *not* a
hard FK (the kind decides the target), the same un-FK'd discriminator
pattern the approval-target columns use.
"""

SOURCE_KINDS: tuple[SourceKind, ...] = ("parametric", "optimisation", "cohort")


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class ParametricStudy(_Base):
    """One single-tool, single-shot design-space sweep within a project.

    ``version`` is incremented on every update via the SQLAlchemy
    ``version_id_col`` mapper option; concurrent writers see
    :class:`sqlalchemy.orm.exc.StaleDataError` on the second commit
    (same optimistic-lock pattern as ``Project`` / ``Scenario`` /
    ``Job``).

    The ``state`` and ``mode`` fields are CHECK-constrained at the DB
    level; the literals are derived from :data:`STUDY_STATES` /
    :data:`STUDY_MODES` so the Python tuple and the DB vocabulary
    cannot drift (the matching migration carries its own frozen
    literal — adding a value means editing the tuple *and* shipping a
    migration).
    """

    __tablename__ = "parametric_study"

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
    """Auto-suggested, user-editable. Deliberately **not** unique
    (design §2): unlike ``scenario`` there is no
    ``uq_*_project_name`` — two studies in a project may share a name."""

    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")

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
    """Tool version pinned at study creation (design §5 / §13.1).

    Every child job is pinned to this same snapshot so an admin tool
    update mid-study can't split the response surface across versions.
    Mirrors ``Job.tool_version_id``."""

    mode: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    factor_spec: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    """Per-input role + vary spec, derived from the frozen descriptor
    (design §3). Validated point-by-point against the descriptor's
    cross-field constraints by the generator (design §4)."""

    sampling_params: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    """Mode-specific knobs (N points, resolution, alpha type, ...)."""

    run_cap: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    """Hard ceiling on generated points, resolved from the install
    setting at creation and **frozen on the row** (design §2 / §4)."""

    state: Mapped[str] = mapped_column(sa.String(20), nullable=False, default="draft")
    generated_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    """Points the generator produced (post cross-field filtering)."""

    skipped_invalid_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    """Points dropped by the descriptor's cross-field validation (design §4)."""

    seed: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    """RNG seed, persisted for the stochastic modes (lhs/sobol/random)
    so a study is reproducible and a future "extend" can continue the
    sequence. ``NULL`` for the deterministic modes."""

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
    state_changed_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    state_changed_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    cancel_requested_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    cancel_requested_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
    __table_args__: Any = (
        sa.CheckConstraint(
            # Derived from STUDY_STATES so the DB vocabulary and the
            # Python tuple can never drift. repr() yields single-quoted
            # literals: state IN ('draft',...).
            f"state IN ({','.join(repr(s) for s in STUDY_STATES)})",
            name="ck_parametric_study_state",
        ),
        sa.CheckConstraint(
            f"mode IN ({','.join(repr(m) for m in STUDY_MODES)})",
            name="ck_parametric_study_mode",
        ),
        sa.CheckConstraint(
            "generated_count >= 0 AND skipped_invalid_count >= 0 AND run_cap >= 0",
            name="ck_parametric_study_counts_non_negative",
        ),
    )


class StudyResult(_Base):
    """Per-child results row for the unified studies results store.

    Design ``designs/studies-engine.md`` §6 + ``designs/parametric-studies.md``
    §10.0 (2026-05-19 amendment) + ADR-0001 revision + ADR-0003. The
    runner writes one row per child job at terminal completion
    **before** ``clean_directory`` runs, so the results matrix survives
    cleanup + working-dir reaping regardless of the descriptor's
    ``retain_files``. Read by the 2-PS-b0 Results table + response
    picker; 2-PS-b plots + sensitivity + 2-PS-c surrogate are pure
    read-side over the same store.

    Renamed from ``parametric_study_result`` (migration 0058) and given
    a ``(source_kind, source_id)`` discriminator so the three study
    siblings — parametric / optimisation / cohort — all feed one store
    read by one Results explorer. ``source_id`` is a *logical* FK whose
    target table the ``source_kind`` decides (an un-FK'd discriminator
    pair, the approval-target pattern). The legacy
    ``parametric_study_id`` FK column was dropped in migration 0060 once
    every consumer read via the discriminator (studies-engine §6).

    UNIQUE is on ``(source_kind, source_id, job_id)`` (migration 0060) so
    **one job may appear under several sources** — a completed job that
    is both a parametric child and a cohort member gets one row per
    source. ``job_id`` still cascades on delete (drafts get reaped per
    D20).
    """

    __tablename__ = "study_result"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    source_kind: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="parametric")
    """Which study sibling produced this row (:data:`SOURCE_KINDS`).
    CHECK-constrained at the DB level from :data:`SOURCE_KINDS` so the
    Python tuple and the DB vocabulary can't drift."""

    source_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        nullable=False,
    )
    """Logical FK into the sibling table named by ``source_kind`` — the
    ``parametric_study`` / ``optimisation_study`` / ``job_cohort`` id.
    Not a hard FK (the kind picks the target table)."""

    job_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("job.id", ondelete="CASCADE"),
        nullable=False,
    )
    """The child job whose ``OutputsDocument`` seeded this row.
    Cascades on delete because draft-job reaping (D20) removes a
    study's drafts — the matching results row must go with them."""

    scalars: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    """``{output_name: scalar_value}`` from the child's
    ``OutputsDocument``. Both numeric *and* string scalars are
    stored (string scalars usable as filters / labels in 2-PS-b;
    only numeric is eligible as a plot/response axis). Failed /
    cancelled children carry an empty dict (the partial-data
    banner, D3)."""

    retained_files: Mapped[list[dict[str, Any]]] = mapped_column(
        sa.JSON, nullable=False, default=list
    )
    """References to descriptor-retained file outputs that survived
    cleanup (ADR-0003 retention floor + descriptor ``retain_files``).
    Shape: ``[{"filename": "outputs/results.csv", "kind": "csv_plot"}, ...]``.
    The ``kind`` mirrors the descriptor's ``OutputField.file_type``;
    ``csv_plot`` files are the candidates for the 2-PS-b curve-overlay
    + the 2-PS-b0 response-from-column picker."""

    evaluated_on_surrogate: Mapped[bool | None] = mapped_column(
        sa.Boolean, nullable=True, server_default=sa.false()
    )
    """``True`` when this row is an optimisation candidate scored against
    an attached surrogate in-process rather than by a real child job
    (ADR-0009 §12.5 — the ``surrogate`` run mode). ``False`` / ``NULL``
    for every real-job row (parametric, cohort, and ``real``-mode
    optimisation children). Added by migration 0066; shared-harmless for
    the other source kinds."""

    feasible: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    """Whether this candidate satisfied the optimisation study's
    constraints (ADR-0009 §7). ``NULL`` = not applicable / unknown (the
    only meaningful value for parametric + cohort rows, which have no
    constraints). Added by migration 0066."""

    validity: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, server_default="valid", default="valid"
    )
    """Output-validity verdict (ADR-0017): ``valid`` | ``invalid``. Stamped at
    record time from the child's scalars vs the descriptor's finiteness floor +
    declared ``min``/``max`` bounds; consumers (study matrix, surrogates,
    optimisation) exclude ``invalid`` rows. Defaults ``valid`` — pre-0085 rows
    and un-evaluated rows read as valid until the backfill (slice 5) runs.
    Added by migration 0085."""

    validity_reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        sa.JSON, nullable=False, server_default=sa.text("'[]'"), default=list
    )
    """Why a row is invalid: ``[{"output", "code", "detail"}, ...]`` with codes
    ``non_finite`` / ``below_min`` / ``above_max``. Empty when valid. Added by
    migration 0085."""

    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__: Any = (
        sa.CheckConstraint(
            # Derived from SOURCE_KINDS so the DB vocabulary and the
            # Python tuple can never drift (migration 0058 carries its
            # own frozen literal copy).
            f"source_kind IN ({','.join(repr(k) for k in SOURCE_KINDS)})",
            name="source_kind",
        ),
        sa.Index("ix_study_result_source_kind_source_id", "source_kind", "source_id"),
        sa.UniqueConstraint(
            "source_kind", "source_id", "job_id", name="uq_study_result_source_job"
        ),
    )


# Backward-compat alias: the table + ORM were ``parametric_study_result``
# / ``ParametricStudyResult`` before the studies-engine §6 rename
# (migration 0058). Existing imports keep working while consumers migrate
# to the ``StudyResult`` name + the ``(source_kind, source_id)`` API.
ParametricStudyResult = StudyResult


# ``Job`` (matofx.hierarchy.orm) carries a foreign key to
# ``optimisation_study`` (ADR-0009, migration 0066). Importing the
# optimisation ORM here — at the bottom, for its registration side
# effect — puts that table in the shared MetaData, so every consumer of
# the studies ORMs (the runner supervisor, the API routers) can configure
# Job's mapper without a NoReferencedTableError. This mirrors how
# ``approvals.orm`` imports the library / materials ORMs, and keeps the
# studies-engine sibling tables registered together. The optimisation
# package's ``__init__`` does not import the Optuna-backed strategy
# engine, so this stays a cheap, dependency-light import.
from matofx.studies.optimisation import orm as _optimisation_orm  # noqa: E402, F401
