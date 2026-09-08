"""SQLAlchemy ORM model for job approval requests.

One :class:`ApprovalRequest` row per approval *gate* a job passes
through (§16.15):

- a ``pre`` row is created when a job for a ``pre``/``both``-policy
  tool is submitted — the job is held in ``pending_approval`` until
  an approver decides;
- a ``post`` row is created when a job for a ``post``/``both``-policy
  tool finishes its run — the outputs are withheld
  (``pending_results``) until an approver releases them.

A ``both``-policy job therefore accrues two rows over its lifetime.
Rows are **never deleted** — the legacy app deleted the approval row
on decision and lost all history; here the decided row *is* the
durable record (alongside the hash-chained audit events the service
writes). ``eligible_approvers`` is the list of approver usernames
*snapshotted at request time* (canonical lowercase, matching
:attr:`matofx.users.orm.User.username`) so a later edit to the tool's
approver list cannot rewrite who was asked to decide.

The constraint vocabularies are derived from the module-level tuples
so the Python constants and the DB CHECKs can never drift; the
matching migration ``0031`` carries its own frozen literals (a
migration is an immutable historical record and must not import
these).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata

# Register the library_file_version / material_version tables in the
# shared MetaData so the approval_request.library_file_version_id (ADR-0006)
# and .material_version_id (ADR-0007) FKs resolve wherever ApprovalRequest
# is mapped — the runner maps it (runner/supervisor.py), as does every test
# conftest that touches approvals. Both ORM modules are leaves (they import
# only db.metadata), so these side-effect imports are acyclic and avoid the
# cross-package NoReferencedTableError footgun.
from matofx.library import orm as _library_orm  # noqa: F401
from matofx.materials import orm as _materials_orm  # noqa: F401

APPROVAL_KINDS: Final[tuple[str, ...]] = ("pre", "post")
"""Which gate this request represents. ``pre`` = before the run;
``post`` = before results are released. ``both``-policy tools produce
one of each, in order."""

APPROVAL_STATUSES: Final[tuple[str, ...]] = ("pending", "approved", "denied")
"""Lifecycle of a single request. ``pending`` until an approver acts,
then terminally ``approved`` or ``denied``."""

APPROVAL_DECIDED_VIA: Final[tuple[str, ...]] = (
    "approver",
    "self",
    "admin_override",
)
"""How the decision was reached, for audit/oversight:

- ``approver`` — a listed eligible approver (not the submitter).
- ``self`` — the submitter, who is also an eligible approver,
  explicitly approving their own job (allowed but never automatic).
- ``admin_override`` — an administrator acting outside the eligible
  list to unblock things; rendered visibly distinct in the UI.
"""


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class ApprovalRequest(_Base):
    """One approval gate for one job.

    Created ``pending``; an approver's decision moves it terminally to
    ``approved`` or ``denied`` and stamps ``decided_*``. The
    side-effects of a decision (queueing a pre-approved job, releasing
    or withholding post results) live in the job/runner services — this
    row only records *that* and *how* the decision was made.
    """

    __tablename__ = "approval_request"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    job_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("job.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    """The job this gate is for, or ``NULL`` for a parametric-study
    gate (study-level, never per-child — design §7). Exactly one of
    ``job_id`` / ``parametric_study_id`` is set
    (``ck_approval_request_exactly_one_target``)."""

    parametric_study_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("parametric_study.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    """The study this single gate covers (design §7), or ``NULL`` for
    an ordinary job gate. A study has at most one ``pre`` and one
    ``post`` row — never one per child job."""

    library_file_version_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("library_file_version.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    """The library-file version this gate covers (ADR-0006 §5 — a global
    publish), or ``NULL`` for a job / study / material gate. The third
    target in the ``ck_approval_request_exactly_one_target`` CHECK. Set
    together with :attr:`designated_approver_user_id` and with the
    ``tool_*`` columns left NULL (a library gate has no tool)."""

    material_version_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("material_version.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    """The material version this gate covers (ADR-0007 §5 — a material
    change), or ``NULL`` for a job / study / library gate. The fourth
    target in the ``ck_approval_request_exactly_one_target`` CHECK. Set
    together with :attr:`designated_approver_user_id` and with the
    ``tool_*`` columns left NULL (a material gate has no tool)."""

    tool_vv_record_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("tool_vv_record.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    """The tool V&V record this gate covers (ADR-0011 §3 — promoting a
    V&V claim about a tool version from ``draft`` to ``approved``), or
    ``NULL`` for any other target. The fifth target in the widened
    ``ck_approval_request_exactly_one_target`` CHECK (migration 0055).
    Set together with :attr:`designated_approver_user_id` and with the
    ``tool_*`` columns left NULL (a V&V gate's "tool" is the V&V record
    itself, not the simulation tool whose version it covers)."""

    job_vv_record_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("job_vv_record.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    """The job V&V record this gate covers (ADR-0011 §3 — promoting a
    V&V claim about a single job run from ``draft`` to ``approved``),
    or ``NULL`` for any other target. The sixth target in the widened
    ``ck_approval_request_exactly_one_target`` CHECK (migration 0055).
    Set together with :attr:`designated_approver_user_id` and with the
    ``tool_*`` columns left NULL."""

    surrogate_model_version_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        nullable=True,
        index=True,
    )
    """The surrogate model version this gate covers (ADR-0008 §6.3 — the
    technical-checker sign-off before a global / general-use release), or
    ``NULL`` for any other target. The seventh target in the widened
    ``ck_approval_request_exactly_one_target`` CHECK (migration 0063).
    Set together with :attr:`designated_approver_user_id` (the nominated
    checker) and with the ``tool_*`` columns left NULL.

    **Logical pointer** — no ORM ``ForeignKey`` (the DB FK + CASCADE live
    in migration 0063). Declaring an ORM FK from the central
    ``approval_request`` to ``surrogate_model_version`` would make *every*
    approval flush require ``matofx.models.orm`` to be imported (the
    mapper resolves FK targets during table-sort) — a fragile coupling to
    a feature module that needn't be loaded for an ordinary job gate. The
    same logical-pointer choice the registry's ``published_version_id`` /
    ``Job.surrogate_model_version_id`` use."""

    optimisation_study_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("optimisation_study.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    """The optimisation study whose **start gate** this covers (ADR-0009
    §14.1 — sign-off on the compute footprint before the campaign queues),
    or ``NULL`` for any other target. The eighth target in the widened
    ``ck_approval_request_exactly_one_target`` CHECK (migration 0070). Set
    with :attr:`designated_approver_user_id` (the nominated
    ``optimisation_approver``) and ``kind='pre'``."""

    kind: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    """``'pre'`` or ``'post'`` — constrained by ``ck_approval_request_kind``."""

    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="pending", server_default="pending"
    )
    """``'pending'`` | ``'approved'`` | ``'denied'``."""

    requested_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=sa.func.current_timestamp(),
    )
    requested_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    eligible_approvers: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False, default=list)
    """Canonical-lowercase approver usernames, snapshotted at request
    time. The decision-authorisation check matches against this list,
    not the tool's live ``approver_usernames``."""

    designated_approver_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    """The single named approver the submitter chose (ADR-0006/0007
    directed-approver model), or ``NULL`` for the open tool-approver-pool
    gates that jobs / studies use. When set, ``eligible_approvers`` holds
    exactly this user's username and self-approval is forbidden (the
    chooser is excluded from the eligible pool). ``SET NULL`` on user
    delete preserves the historical request row."""

    justification: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="", server_default=""
    )
    """Optional free-text the requestor supplied when submitting an
    approval-gated job. Empty string when none was given."""

    decided_by_user_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=True,
    )
    decided_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    decision_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    """Mandatory (enforced at the service layer) when ``status =
    'denied'`` — emailed verbatim to the requestor. Optional note on
    approve."""

    decided_via: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    """One of :data:`APPROVAL_DECIDED_VIA`; ``NULL`` while pending."""

    tool_alias: Mapped[str | None] = mapped_column(
        sa.String(64),
        sa.ForeignKey("tool.alias", ondelete="RESTRICT"),
        nullable=True,
    )
    """Denormalised from the job so the approver inbox lists without a
    join and the row survives tool metadata changes. ``NULL`` for a
    library / materials gate (ADR-0006/0007), which has no tool."""

    tool_version_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("tool_version.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """The captured snapshot the job runs against — the review panel
    renders input labels/units from this, not the live descriptor.
    ``NULL`` for a library / materials gate, which has no tool."""

    __table_args__: Any = (
        sa.Index("ix_approval_request_status", "status"),
        # At most one pending request per (job, kind). Scoped to
        # pending so a historical denied row doesn't block a future
        # re-request for the same job+kind.
        sa.Index(
            "uq_approval_request_pending_job_kind",
            "job_id",
            "kind",
            unique=True,
            sqlite_where=sa.text("status = 'pending'"),
            postgresql_where=sa.text("status = 'pending'"),
        ),
        # The study analogue: at most one pending (study, kind) gate.
        # NULL job_id rows are distinct under the index above, so this
        # is the constraint that actually enforces "one gate per
        # study" (never per child).
        sa.Index(
            "uq_approval_request_pending_study_kind",
            "parametric_study_id",
            "kind",
            unique=True,
            sqlite_where=sa.text("status = 'pending'"),
            postgresql_where=sa.text("status = 'pending'"),
        ),
        # The library analogue (ADR-0006): at most one pending
        # (library_file_version, kind) gate.
        sa.Index(
            "uq_approval_request_pending_library_kind",
            "library_file_version_id",
            "kind",
            unique=True,
            sqlite_where=sa.text("status = 'pending'"),
            postgresql_where=sa.text("status = 'pending'"),
        ),
        # The materials analogue (ADR-0007): at most one pending
        # (material_version, kind) gate.
        sa.Index(
            "uq_approval_request_pending_material_kind",
            "material_version_id",
            "kind",
            unique=True,
            sqlite_where=sa.text("status = 'pending'"),
            postgresql_where=sa.text("status = 'pending'"),
        ),
        # The surrogate-model analogue (ADR-0008): at most one pending
        # (surrogate_model_version, kind) gate.
        sa.Index(
            "uq_approval_request_pending_surrogate_kind",
            "surrogate_model_version_id",
            "kind",
            unique=True,
            sqlite_where=sa.text("status = 'pending'"),
            postgresql_where=sa.text("status = 'pending'"),
        ),
        # The optimisation start-gate analogue (ADR-0009 §14.1): at most
        # one pending (optimisation_study, kind) gate — one campaign-level
        # sign-off, never per child.
        sa.Index(
            "uq_approval_request_pending_optimisation_kind",
            "optimisation_study_id",
            "kind",
            unique=True,
            sqlite_where=sa.text("status = 'pending'"),
            postgresql_where=sa.text("status = 'pending'"),
        ),
        # Exactly one home: job XOR study XOR library XOR material XOR
        # tool-V&V XOR job-V&V XOR surrogate-model XOR optimisation-study
        # (widened by ADR-0006, 0007, 0011, 0008, then 0009). Portable
        # "exactly one non-null" via a CASE sum (SQLite + PG); the DB CHECK
        # is owned by the migrations (0055 → 0070), this mirrors the head.
        sa.CheckConstraint(
            "(CASE WHEN job_id IS NOT NULL THEN 1 ELSE 0 END "
            "+ CASE WHEN parametric_study_id IS NOT NULL THEN 1 ELSE 0 END "
            "+ CASE WHEN library_file_version_id IS NOT NULL THEN 1 ELSE 0 END "
            "+ CASE WHEN material_version_id IS NOT NULL THEN 1 ELSE 0 END "
            "+ CASE WHEN tool_vv_record_id IS NOT NULL THEN 1 ELSE 0 END "
            "+ CASE WHEN job_vv_record_id IS NOT NULL THEN 1 ELSE 0 END "
            "+ CASE WHEN surrogate_model_version_id IS NOT NULL THEN 1 ELSE 0 END "
            "+ CASE WHEN optimisation_study_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_approval_request_exactly_one_target",
        ),
        sa.CheckConstraint(
            f"kind IN ({','.join(repr(k) for k in APPROVAL_KINDS)})",
            name="ck_approval_request_kind",
        ),
        sa.CheckConstraint(
            f"status IN ({','.join(repr(s) for s in APPROVAL_STATUSES)})",
            name="ck_approval_request_status",
        ),
        sa.CheckConstraint(
            "decided_via IS NULL OR decided_via IN "
            f"({','.join(repr(v) for v in APPROVAL_DECIDED_VIA)})",
            name="ck_approval_request_decided_via",
        ),
        sa.CheckConstraint(
            "status = 'pending' "
            "OR (decided_at IS NOT NULL "
            "AND decided_by_user_id IS NOT NULL "
            "AND decided_via IS NOT NULL)",
            name="ck_approval_request_decided_coherent",
        ),
    )

    def __repr__(self) -> str:
        """Debug representation."""
        target = (
            f"job_id={self.job_id!r}"
            if self.job_id is not None
            else f"parametric_study_id={self.parametric_study_id!r}"
        )
        return (
            f"ApprovalRequest(id={self.id!r}, {target}, kind={self.kind!r}, status={self.status!r})"
        )
