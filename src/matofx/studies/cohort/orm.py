"""SQLAlchemy ORM model for job cohorts (ADR-0008 §3 / §5.1).

A :class:`JobCohort` is the third study sibling (``designs/studies-engine.md``
§1): a **saved query** over completed jobs of one tool, parallel to
``Scenario`` / ``ParametricStudy`` / ``OptimisationStudy`` under a
``Project``. Membership is *not* stored — it is recomputed live every
time the cohort is read (surrogate §3.1, non-negotiable #1), so a new
matching job joins automatically and an archived job drops out. The only
write a cohort read triggers is the lazy ``study_result`` populate
(surrogate §3.3) which stamps :attr:`JobCohort.last_resolved_at`.

The row is keyed on one ``tool_alias`` and a curator-curated list of
tolerated ``tool_version_id``s (§3.2 — the default is the version the
cohort was anchored on; equivalent versions are added explicitly, with a
service-level divergence warning, never inferred). Every other filter
column is nullable = "no filter" (§3.1).

State is ``active ──archive──▶ archived`` (studies-engine §2): no
``running`` (a cohort runs nothing); archive hides the cohort from
default lists and never deletes it (D20 + ADR-0002).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata

CohortState = Literal["active", "archived"]
"""Closed vocabulary for ``JobCohort.state`` (studies-engine §2).

- ``active`` — the live saved query; appears in default lists.
- ``archived`` — hidden from default lists; never deleted (D20).
"""

COHORT_STATES: tuple[CohortState, ...] = ("active", "archived")


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class JobCohort(_Base):
    """One saved query over completed jobs of a single tool.

    ``version`` is incremented on every update via the SQLAlchemy
    ``version_id_col`` mapper option (the optimistic-lock pattern shared
    with ``Project`` / ``Scenario`` / ``Job`` / ``ParametricStudy``).

    The ``state`` field is CHECK-constrained at the DB level from
    :data:`COHORT_STATES` so the Python tuple and the DB vocabulary
    cannot drift (migration 0059 carries its own frozen literal).

    ``tolerated_tool_version_ids`` is a JSON list and is enforced
    **non-empty at the service layer** (§3.2) rather than via a
    portable-awkward JSON-length CHECK — the create / update service is
    the single guard.
    """

    __tablename__ = "job_cohort"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    """Display label; deliberately not unique (studies-engine §1)."""

    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")

    tool_alias: Mapped[str] = mapped_column(
        sa.String(64),
        sa.ForeignKey("tool.alias", ondelete="RESTRICT"),
        nullable=False,
    )
    """The single tool the cohort is keyed on (cross-tool cohorts are
    v1.x — surrogate §1)."""

    tolerated_tool_version_ids: Mapped[list[int]] = mapped_column(sa.JSON, nullable=False)
    """One or more ``tool_version.id``s the cohort accepts (§3.2) — the
    **last-known expansion** of :attr:`tolerated_version_bands` (or the
    explicit id set for a legacy/band-less cohort). Used for display,
    fixed-cohort member validation, and the id fallback path."""

    tolerated_version_bands: Mapped[list[str] | None] = mapped_column(sa.JSON, nullable=True)
    """Tolerated ``major.minor`` bands (e.g. ``["1.2", "2.0"]``, patches
    ignored — §3.2). When set, live membership expands these to the
    *current* matching ``tool_version.id``s on every read, so a future
    patch within a tolerated band joins automatically. ``NULL`` = the
    legacy id path (``tolerated_tool_version_ids`` is authoritative)."""

    project_ids: Mapped[list[int] | None] = mapped_column(sa.JSON, nullable=True)
    """Positive project filter (``designs/job-cohorts.md`` §3): ``NULL`` =
    search the **whole workspace**; an explicit list restricts the search
    to those projects. Every listed project needs the curator's Viewer+ at
    create. Membership is always intersected with the caller's per-project
    ACL at read time regardless (D7)."""

    scenario_ids: Mapped[list[int] | None] = mapped_column(sa.JSON, nullable=True)
    owner_user_ids: Mapped[list[int] | None] = mapped_column(sa.JSON, nullable=True)
    completed_after: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    completed_before: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    library_file_version_ids: Mapped[list[int] | None] = mapped_column(sa.JSON, nullable=True)
    material_version_ids: Mapped[list[int] | None] = mapped_column(sa.JSON, nullable=True)

    input_ranges: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    """``{factor_name: {min, max}}`` numeric-input bounds, evaluated by
    the service over each candidate's ``Job.inputs`` (§3.1)."""

    cross_field_predicate: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    """A ``safe_expr`` DSL string (the descriptor wizard's
    ``cross_field_constraints`` engine, §3.1) evaluated over each
    candidate's inputs."""

    auto_grow: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    """Whether membership re-resolves live (``designs/job-cohorts.md`` D4).

    ``True`` — the saved filters are re-run on every read, so new matching
    runs join automatically (the active-learning / surrogate-refit loop).
    ``False`` — membership is the frozen :attr:`member_job_ids` snapshot
    captured when the cohort was saved (an explicit, curated set).

    The snapshot-vs-live *resolution* branch ships with the membership
    slice; until then every cohort created or backfilled resolves live
    (``auto_grow=True``)."""

    member_job_ids: Mapped[list[int] | None] = mapped_column(sa.JSON, nullable=True)
    """Frozen membership for a non-auto-grow cohort (``NULL`` while
    ``auto_grow`` is ``True``) — the explicit job ids the curator picked
    from the filtered preview (D4)."""

    excluded_job_ids: Mapped[list[int] | None] = mapped_column(sa.JSON, nullable=True)

    source_kinds: Mapped[list[str] | None] = mapped_column(sa.JSON, nullable=True)
    """Which kinds of run count: ``scenario`` (standalone), ``workflow``,
    ``parametric_study``, ``optimisation_study``. ``NULL`` means all of them."""

    parametric_study_ids: Mapped[list[int] | None] = mapped_column(sa.JSON, nullable=True)
    optimisation_study_ids: Mapped[list[int] | None] = mapped_column(sa.JSON, nullable=True)
    """Named studies whose runs count, ORed together and with each other.

    Distinct from :attr:`auto_include_study_ids`, which *adds* a commissioned
    study's runs regardless of the filters. These **narrow**: "this cohort is
    the runs of these three studies" is a far better description of a curated
    set than eighty frozen job ids, and it is the one a live cohort can keep
    honouring as more runs land."""

    auto_include_study_ids: Mapped[list[int] | None] = mapped_column(sa.JSON, nullable=True)
    """Parametric studies this cohort commissioned to fill its own gaps (§11.5).

    Their completed jobs are unioned into the membership at resolution time,
    whichever mode the cohort is in. A fixed cohort would never have taken
    them otherwise; an auto-grow one usually would, but not if its own
    ``input_ranges``, ``project_ids`` or date window happen to reject the very
    runs it asked for -- a study finishing with the verdict unmoved and
    nothing on screen to explain it.

    ``excluded_job_ids`` still wins over this: a curator who threw a run out
    meant it, and a study link must not quietly put it back."""
    """Job ids the curator removed from an ``auto_grow`` cohort's live
    matches — permanently dropped even if they keep matching the filters
    (D4). ``NULL``/empty = no manual exclusions."""

    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="active")
    last_resolved_at: Mapped[_dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    """Freshness stamp touched by the lazy ``study_result`` populate
    (§3.3) so the UI can show when membership was last recomputed."""

    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    created_by_user_id: Mapped[int] = mapped_column(
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
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
    __table_args__: Any = (
        sa.CheckConstraint(
            f"state IN ({','.join(repr(s) for s in COHORT_STATES)})",
            name="state",
        ),
        sa.Index("ix_job_cohort_state", "state"),
        sa.Index("ix_job_cohort_tool_alias", "tool_alias"),
    )
