"""SQLAlchemy ORM models for V&V (Verification & Validation) tracking (ADR-0011).

A **V&V record** asserts that a tool version, or a single job run, has
been verified (the math is right) or validated (the tool agrees with
physical reality on representative cases). Per ``designs/vv-tracking.md``
the model is **global, curator-governed** — records are visible to any
authenticated user, but promoting a record from ``draft`` to ``approved``
requires the ``vv.curate`` permission and (default-on) review by a
designated curator other than the author.

Two tables, in two granularities:

- :class:`ToolVVRecord` — V&V claims about a tool version. The V&V
  cohort key for inheritance lives on the tool version itself
  (``tool_version.bundle_hash``, see ADR-0011 / design §6.5); this row
  carries the claim text — kind, state, evidence reference, applicable
  range, confidence band, standards cited, and the actor / decision
  audit trail. A surrogate model is just a tool version, so its V&V
  records live here too (ADR-0008 surrogate-as-tool).
- :class:`JobVVRecord` — V&V claims about a single job run (e.g. "this
  run was validated against test data X"). Job records do **not**
  inherit — a job is a specific run on a specific frozen tool version.

Both tables share:

- A closed five-state lifecycle: ``draft → pending → approved | rejected``
  plus ``retired`` (soft archive). Mirrors :class:`LibraryFileVersion` /
  :class:`MaterialVersion`'s publish flow exactly so the existing
  :mod:`matofx.approvals` machinery handles the gate with one new target
  kind on each table (the widening migration is ``0055``).
- Evidence-by-reference via :class:`LibraryFileVersion` — the V&V
  evidence document is uploaded to the file library and referenced
  here, never inlined. Immutable + content-hashed for free
  (ADR-0006).
- A decision-when-terminal CHECK: a record in a terminal state
  (``approved`` / ``rejected``) must have ``decided_by_user_id`` and
  ``decided_at`` populated.

No SQLAlchemy ``relationship()`` declarations: V&V is a *downstream*
concern — :class:`ToolVersion`, :class:`Job`, and :class:`LibraryFileVersion`
must not import this module (avoids the circular-import risk that
materials / library deliberately work around the same way). Service-
layer queries do explicit ``session.get(Related, id)`` calls when they
need the related object.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata

# ---- Closed vocabularies --------------------------------------------

ToolVVKind = Literal["verification", "validation"]
"""Closed vocabulary for ``tool_vv_record.kind`` (design §2.1).

- ``verification`` — the math is right (code passes its self-checks,
  numerical scheme converges as expected, regression suite is green).
- ``validation`` — the tool agrees with physical reality on the cases
  representative of its intended use.
"""

TOOL_VV_KINDS: tuple[ToolVVKind, ...] = ("verification", "validation")


JobVVKind = Literal["validated_against", "calibrated_from", "sanity_checked"]
"""Closed vocabulary for ``job_vv_record.kind`` (design §2.2).

- ``validated_against`` — this run was compared against an external
  reference dataset and the comparison was within tolerance.
- ``calibrated_from`` — this run's inputs were tuned to match an external
  reference dataset (the dataset is the source of truth, not the result).
- ``sanity_checked`` — a curator inspected this run for plausibility
  without a formal comparison.
"""

JOB_VV_KINDS: tuple[JobVVKind, ...] = (
    "validated_against",
    "calibrated_from",
    "sanity_checked",
)


VVState = Literal["draft", "pending", "approved", "rejected", "retired"]
"""Closed vocabulary for the V&V lifecycle (design §2.1 / §2.2).

- ``draft`` — author is still composing the claim; editable by the author
  alone; not yet visible in the curator inbox.
- ``pending`` — submitted for review; an ``approval_request`` row exists
  with a designated approver; visible in the curator inbox.
- ``approved`` — review granted; contributes to the badge for every tool
  version sharing the cohort hash.
- ``rejected`` — review declined with a reason; the prior approved
  record (if any) remains the authoritative one for badge computation.
- ``retired`` — soft archive; removes the record from every badge
  derivation. Reversible via re-record + re-submit, not via state flip.
"""

VV_STATES: tuple[VVState, ...] = (
    "draft",
    "pending",
    "approved",
    "rejected",
    "retired",
)

_TERMINAL_STATES: frozenset[VVState] = frozenset({"approved", "rejected", "retired"})


# Allowed forward transitions. Read-only; mutated only by editing this
# constant. The lifecycle is small enough that a transition table is
# cheaper than a graph: each state knows its allowed next states.
_ALLOWED_TRANSITIONS: dict[VVState, frozenset[VVState]] = {
    "draft": frozenset({"pending", "retired"}),
    "pending": frozenset({"approved", "rejected", "draft", "retired"}),
    # `pending → draft` is the recall path — author wants to edit before
    # the curator decides. Allowed for the author until the decision lands.
    # `pending → retired` lets a curator withdraw a record that's still in
    # review without first rejecting it; the service closes the open
    # approval gate as part of the retire (see VVService.retire_*).
    "approved": frozenset({"retired"}),
    "rejected": frozenset({"retired"}),
    "retired": frozenset(),
}


def is_terminal(state: str) -> bool:
    """``True`` if ``state`` is one of the terminal lifecycle states.

    Terminal means "no further transitions allowed except via re-record".
    The lifecycle-respecting service paths use this to guard mutation
    attempts on already-decided records.
    """
    return state in _TERMINAL_STATES


def can_transition(from_state: str, to_state: str) -> bool:
    """``True`` if ``from_state → to_state`` is an allowed V&V lifecycle move.

    Service-layer callers use this to validate transitions before writing
    the new state — a ``False`` result raises
    :class:`matofx.vv.errors.VVStateTransitionError` with a user-facing
    message naming the offending transition.
    """
    if from_state not in _ALLOWED_TRANSITIONS:
        return False
    return to_state in _ALLOWED_TRANSITIONS[from_state]


# ---- ORM ------------------------------------------------------------


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class ToolVVRecord(_Base):
    """A V&V claim about one tool version (design §2.1).

    The cohort-key for inheritance lives on ``tool_version.bundle_hash``
    (slice 0); this row carries the *claim* — what the V&V says, when,
    by whom, against what evidence. The badge query in slice 4 reads
    every approved record whose ``tool_version_id`` shares
    ``(alias, bundle_hash)`` with the target version (design §5.1 +
    §6.5).
    """

    __tablename__ = "tool_vv_record"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    tool_version_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("tool_version.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    """The tool version the claim is about. RESTRICT — a tool version
    cannot be deleted while V&V records reference it; the curator must
    retire the records first."""

    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    """One of :data:`TOOL_VV_KINDS`."""

    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="draft")
    """One of :data:`VV_STATES`. Starts at ``'draft'`` on author create."""

    evidence_file_version_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("library_file_version.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """The V&V evidence document (test report, hand-calculation, peer
    review) — stored in the file library, governed + immutable +
    content-hashed (ADR-0006). RESTRICT — a referenced evidence file
    can't be deleted while V&V records cite it; the curator retires the
    V&V record first or transitions evidence to a new file version."""

    evidence_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    """External evidence link — a published standards document, a
    customer's internal wiki page. Used when the evidence genuinely
    isn't file-library content. Legacy single-item mirror of the first
    URL entry in :attr:`evidence_json` (kept for back-compat)."""

    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(
        sa.JSON, nullable=False, default=list, server_default=sa.text("'[]'")
    )
    """The full evidence list — ``[{"kind": "library"|"url",
    "file_version_id": int|None, "url": str|None}, ...]`` — so a record can
    cite several files and/or links of mixed type (migration ``0087``). The
    two scalar ``evidence_*`` columns mirror the first library / URL entry
    for older readers. Empty ``[]`` on rows predating the column; the read
    path synthesises a list from the scalars in that case."""

    applicable_range_json: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict, server_default=sa.text("'{}'")
    )
    """``{factor_name: {"min": float, "max": float}}`` — the operating
    envelope the validation covers. Empty ``{}`` means unbounded /
    not-applicable (e.g. for a verification claim). The submit-time
    soft-warn (design §6.5 / slice 7) reads this."""

    confidence_band: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    """The ±% the validation demonstrates (e.g. 0.02 = ±2%). Display-
    only on v1.0; v1.x may surface as a quantitative filter."""

    standards_cited_json: Mapped[list[str]] = mapped_column(
        sa.JSON, nullable=False, default=list, server_default=sa.text("'[]'")
    )
    """List of free-text strings — ``["ASME V&V 10", "NAFEMS QSS-2023"]``.
    Free text on v1.0; a v1.x ADR may move to a controlled vocabulary
    seeded with the major standards bodies."""

    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    recorded_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    """The V&V record author. RESTRICT — a user with V&V history cannot
    be hard-deleted; deactivation is the path."""

    recorded_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )

    decided_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """The curator who approved / rejected. Populated when ``state`` is
    a terminal decision state (``approved`` or ``rejected``); see the
    decided-when-terminal CHECK."""

    decided_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    decision_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    """Required at the service layer for ``reject``; optional for
    ``approve``. The schema allows NULL because retired records — also
    terminal but not decisions — never carry a reason here."""

    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    """Optimistic-lock counter (SQLAlchemy ``version_id_col``). Starts
    at 1 on insert, increments on every UPDATE — bumped automatically by
    SQLAlchemy when any tracked column on this row changes. Same
    convention as :class:`Material` / :class:`LibraryFile`."""

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
    __table_args__: Any = (
        sa.CheckConstraint(
            "kind IN ('verification','validation')",
            name="ck_tool_vv_record_kind",
        ),
        sa.CheckConstraint(
            "state IN ('draft','pending','approved','rejected','retired')",
            name="ck_tool_vv_record_state",
        ),
        sa.CheckConstraint(
            "state NOT IN ('approved','rejected') "
            "OR (decided_by_user_id IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_tool_vv_record_decided_when_terminal",
        ),
        sa.Index("ix_tool_vv_record_state", "state"),
        sa.Index(
            "ix_tool_vv_record_tv_state_kind",
            "tool_version_id",
            "state",
            "kind",
        ),
    )


class JobVVRecord(_Base):
    """A V&V claim about one job run (design §2.2).

    Job claims do NOT inherit — a job has one ``tool_version_id`` and
    one frozen set of inputs. The claim attests to that specific run
    against a specific external reference dataset.
    """

    __tablename__ = "job_vv_record"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    job_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("job.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    """CASCADE — a claim about a job has no meaning without its
    subject; deleting the job removes the records."""

    kind: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    """One of :data:`JOB_VV_KINDS`."""

    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="draft")
    """One of :data:`VV_STATES`. Same lifecycle as ``tool_vv_record``."""

    evidence_file_version_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("library_file_version.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """Evidence dataset — e.g. the rig-test result the job is being
    validated against. Same governance + lock semantics as
    :attr:`ToolVVRecord.evidence_file_version_id`."""

    evidence_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(
        sa.JSON, nullable=False, default=list, server_default=sa.text("'[]'")
    )
    """The full evidence list (see :attr:`ToolVVRecord.evidence_json`)."""

    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    recorded_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    recorded_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )

    decided_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=True,
    )
    decided_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    decision_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
    __table_args__: Any = (
        sa.CheckConstraint(
            "kind IN ('validated_against','calibrated_from','sanity_checked')",
            name="ck_job_vv_record_kind",
        ),
        sa.CheckConstraint(
            "state IN ('draft','pending','approved','rejected','retired')",
            name="ck_job_vv_record_state",
        ),
        sa.CheckConstraint(
            "state NOT IN ('approved','rejected') "
            "OR (decided_by_user_id IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_job_vv_record_decided_when_terminal",
        ),
        sa.Index("ix_job_vv_record_state", "state"),
    )
