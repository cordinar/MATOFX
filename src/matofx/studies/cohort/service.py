"""CohortService — the job-cohort lifecycle + live membership (ADR-0008 §3).

A cohort is a **saved query** over completed jobs of one tool. This
service owns:

- the lifecycle — ``create_cohort`` / ``update_cohort`` (DB-only, the
  no-disk rule [[feedback_save_db_only]] applies trivially — a cohort
  never stages anything), capability-gated writes + open ``get`` /
  ``list_all`` reads, and ``archive_cohort`` (no-delete, D20). A cohort is
  a workspace-level object (D5): no home project;
- membership — ``resolve_members`` honours ``auto_grow`` (D4): a live
  cohort runs the §3.1 query workspace-wide (or over ``project_ids``),
  joins each candidate job to its owning project (via its scenario,
  parametric study, *or* optimisation study — the three job homes,
  exactly as :func:`matofx.hierarchy.service._job_project_id` does),
  applies the Python ``input_ranges`` + ``cross_field_predicate`` filters
  and drops ``excluded_job_ids``; a fixed cohort returns its frozen
  ``member_job_ids``. Either way the result **intersects with the
  caller's per-project ACL** so only jobs the caller can see surface
  (hidden ones become a footnote count, never an error — D7). The same
  query powers ``preview_candidates`` for the builder;
- the curator-explicit tolerated-version contract — adding a version
  whose descriptor diverges (input set / type / unit) surfaces a
  plain-English warning, never silent inference (§3.2 / non-negotiable
  #6);
- the lazy ``study_result`` populate (``populate_study_results``, §3.3)
  — resolves membership then writes one ``(source_kind='cohort',
  source_id=cohort.id)`` row per member that lacks one (the cheap diff),
  copying scalars from an existing row or reading the job's retained
  ``outputs.json``. Enabled by the studies-engine §6 second rename step
  (migration 0060: dropped ``parametric_study_id`` + the composite
  ``(source_kind, source_id, job_id)`` uniqueness so one job can appear
  under several sources).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json as _json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, delete, exists, func, or_, select
from sqlalchemy.orm import aliased

from matofx.audit.schema import RecordInput
from matofx.hierarchy.errors import AccessDeniedError, NotFoundError
from matofx.hierarchy.orm import Job, Project, ProjectAccessLevel, Scenario
from matofx.hierarchy.service import _require_project_access, _resolve_access
from matofx.studies.cohort.errors import CohortConfigError, CohortStateError
from matofx.studies.cohort.orm import JobCohort
from matofx.studies.coverage.errors import CoverageError
from matofx.studies.orm import ParametricStudy, StudyResult
from matofx.users.permissions import (
    Permission,
    user_has_any_permission,
    user_has_permission,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


_COVERAGE_CACHE_MAX = 32
"""Cohort-preview reports held per service instance (ADR-0020 §9.1).

Small on purpose: entries are a few kB, the working set is whichever
cohorts someone is currently looking at, and a dict keyed on a moving
membership has no natural ceiling of its own."""

_COVERAGE_SUGGEST_SEED = 20_020
"""Fixed, and deliberately the **same** seed the report measures its covering
radius with (report._CANDIDATE_SEED): the greedy then fills the holes the
headline number actually counts. It is also fixed so the same cohort proposes
the same batch twice — a list of runs that
shuffled between page loads would be impossible to review before committing
solver time to it."""


def _coverage_study_spec(
    suggested: tuple[Any, ...],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A ``table``-mode ``factor_spec`` + run rows for the suggested points.

    Every input that **differs** across the batch becomes a run-table
    ``column`` — ``table`` mode requires exactly that, since values come from
    the rows rather than a per-factor domain. Every input the batch holds
    **the same** becomes a ``constant``: it was pinned in the cohort these runs
    are filling in, and a batch that quietly varied it would not be comparable
    with what it is meant to complete.
    """
    names: list[str] = []
    for point in suggested:
        for key in point:
            if key not in names:
                names.append(key)

    factor_spec: dict[str, Any] = {}
    varied: list[str] = []
    for name in names:
        values = [p.get(name) for p in suggested]
        if all(v == values[0] for v in values):
            factor_spec[name] = {"role": "constant", "value": values[0]}
            continue
        varied.append(name)
        factor_spec[name] = {
            "role": "vary",
            "type": _coverage_factor_type(values),
            "vary": {"kind": "column"},
        }
    runs = [{name: point[name] for name in varied} for point in suggested]
    return factor_spec, runs


def _coverage_factor_type(values: list[Any]) -> str:
    """The ``FactorSpec`` type for a suggested column's values."""
    if all(isinstance(v, bool) for v in values):
        return "bool"
    if all(not isinstance(v, bool) and isinstance(v, int | float) for v in values):
        return "numeric"
    return "select"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


# Optional filter columns settable at create / update. ``None`` means
# "leave unchanged" on update and "no filter" on create.
_FILTER_FIELDS: tuple[str, ...] = (
    "project_ids",
    "scenario_ids",
    "owner_user_ids",
    "completed_after",
    "completed_before",
    "library_file_version_ids",
    "material_version_ids",
    "input_ranges",
    "cross_field_predicate",
    # Source filters (migration 0097). They shipped preview-only because there
    # was nowhere to keep them, which lost the curator's selections the moment
    # they re-opened the cohort — and left a live cohort unable to say "the runs
    # of these three studies" at all.
    "source_kinds",
    "parametric_study_ids",
    "optimisation_study_ids",
    # The studies this cohort commissioned to fill its own gaps. It was on
    # ``CohortUpdateRequest`` from the start but missing from this tuple *and*
    # from the router's copy, so the API accepted it and dropped it on the
    # floor: once a study had been commissioned there was no way — through the
    # UI, the client or the REST surface — to detach it again. A curator who
    # narrowed a fixed cohort to one study still got the commissioned runs
    # back, with the summary reporting the picked count and the coverage
    # panels reporting the resolved one.
    "auto_include_study_ids",
)


def _validate_study_ids(value: Any, field: str) -> None:
    """A list of positive study ids, or nothing.

    Detaching is expressed as an empty list, which the update path turns into
    ``None`` — so this must accept ``[]`` and reject junk rather than treating
    "empty" as "not supplied".
    """
    if value is None:
        return
    if not isinstance(value, list):
        raise CohortConfigError(f"{field} must be a list of study ids.")
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
            raise CohortConfigError(f"{field} must contain positive study ids, not {item!r}.")


def _validate_source_kinds(kinds: Any) -> None:
    """Reject a run source this platform cannot label.

    Silently ignoring a typo'd kind would match nothing and read as "no runs
    qualify", which is indistinguishable from a correct empty result.
    """
    if kinds is None:
        return
    unknown = sorted(set(map(str, kinds)) - _SOURCE_KINDS)
    if unknown:
        raise CohortConfigError(
            f"Unknown run source {unknown[0]!r}. Use any of: {', '.join(sorted(_SOURCE_KINDS))}."
        )


_SOURCE_KINDS: frozenset[str] = frozenset(
    {"scenario", "workflow", "parametric_study", "optimisation_study"}
)
"""The run origins ``_candidate_rows`` can label, and so the only ones a
``source_kinds`` filter can mean. Validated rather than silently ignored: a
typo'd kind would otherwise match nothing and read as "no runs qualify"."""


@dataclass(frozen=True, slots=True)
class ResolvedMembers:
    """The membership of a cohort for one caller (§3.1).

    ``job_ids`` are the completed jobs the caller may see; ``acl_hidden``
    is how many *members* sit in projects the caller can't access (the
    "(N jobs hidden by access controls)" footnote — never an error).
    """

    job_ids: list[int]
    acl_hidden: int
    last_resolved_at: _dt.datetime


@dataclass(frozen=True, slots=True)
class CandidateRow:
    """One candidate run for the cohort builder's preview (``designs/job-cohorts.md`` §3).

    ``source`` is the run's origin — ``scenario`` (standalone),
    ``workflow``, ``parametric_study``, or ``optimisation_study`` — so the
    builder can show where each run came from.

    ``source_name`` names *which* study, because the kind alone does not
    identify it: an archive with six parametric studies showed six identical
    "Parametric study" rows, and picking runs out of one of them meant
    cross-referencing job ids by hand. ``None`` for a standalone run, which has
    no study to name.

    ``created_by_user_id`` is who launched the run — the other thing a curator
    sorts by when deciding what belongs in a cohort.
    """

    job_id: int
    project_id: int
    source: str
    tool_version_id: int
    completed_at: _dt.datetime | None
    inputs: dict[str, Any]
    source_name: str | None = None
    created_by_user_id: int | None = None


@dataclass(frozen=True, slots=True)
class PreviewResult:
    """A filtered candidate preview for one curator (workspace-wide).

    ``rows`` are the matching completed runs the curator may see;
    ``acl_hidden`` counts matches in projects they can't access.
    """

    rows: list[CandidateRow]
    acl_hidden: int


@dataclass(frozen=True, slots=True)
class Facet:
    """One selectable value for a builder filter — an id and what to call it."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class VersionBand:
    """A tool's versions grouped by ``major.minor`` (patches ignored, §3.2).

    The builder offers these as the tolerated-version choices: picking a
    band tolerates every patch release within it. ``version_ids`` are the
    concrete ``tool_version.id``s the band expands to (stored on the
    cohort as ``tolerated_tool_version_ids``).
    """

    major_minor: str
    latest_version: str
    version_ids: list[int]


@dataclass(frozen=True, slots=True)
class NumericInput:
    """A numeric tool input the builder can offer a min/max range filter on."""

    name: str
    label: str


@dataclass(frozen=True, slots=True)
class OutputColumn:
    """One response column of the cohort results matrix (label-first).

    ``name`` is the descriptor output's internal name (the bare scalar key
    the rows carry); ``label`` + ``unit`` are its human descriptor label
    and unit, so the matrix headers read ``"Heat Dissipation (W)"`` rather
    than the internal name ([[feedback_use_labels_not_names]]).
    """

    name: str
    label: str
    unit: str


@dataclass(frozen=True, slots=True)
class ToolBuildMeta:
    """Everything the cohort builder needs once a tool is chosen.

    ``version_bands`` newest-first; ``numeric_inputs`` are the int/float
    descriptor fields (label, not internal name — [[feedback_use_labels_not_names]]).
    """

    version_bands: list[VersionBand]
    numeric_inputs: list[NumericInput]
    launchers: list[Facet] = field(default_factory=list)
    """Users who actually launched a visible run of this tool.

    Not the whole user directory: a picker offering people who never ran this
    tool makes the reader hunt for the few names that do anything."""
    parametric_studies: list[Facet] = field(default_factory=list)
    optimisation_studies: list[Facet] = field(default_factory=list)
    """The studies that produced visible runs of this tool, by name."""


@dataclass(frozen=True, slots=True)
class _CandidateFilter:
    """The SQL-expressible half of a cohort query (§3.1).

    Shared by live membership (``from_cohort``) and the builder preview
    (constructed from raw params). ``target_project_ids = None`` means
    **search the whole workspace** (the D5 workspace-level default);
    a list restricts the search to those projects.
    """

    tool_alias: str
    tolerated_tool_version_ids: list[int]
    target_project_ids: list[int] | None = None
    scenario_ids: list[int] | None = None
    owner_user_ids: list[int] | None = None
    completed_after: _dt.datetime | None = None
    completed_before: _dt.datetime | None = None
    library_file_version_ids: list[int] | None = None
    material_version_ids: list[int] | None = None
    source_kinds: list[str] | None = None
    """``scenario`` / ``workflow`` / ``parametric_study`` / ``optimisation_study``."""
    parametric_study_ids: list[int] | None = None
    optimisation_study_ids: list[int] | None = None

    @classmethod
    def from_cohort(
        cls, cohort: JobCohort, tolerated_tool_version_ids: list[int]
    ) -> _CandidateFilter:
        return cls(
            tool_alias=cohort.tool_alias,
            tolerated_tool_version_ids=list(tolerated_tool_version_ids),
            target_project_ids=(
                list(cohort.project_ids) if cohort.project_ids is not None else None
            ),
            scenario_ids=cohort.scenario_ids,
            owner_user_ids=cohort.owner_user_ids,
            completed_after=cohort.completed_after,
            completed_before=cohort.completed_before,
            library_file_version_ids=cohort.library_file_version_ids,
            material_version_ids=cohort.material_version_ids,
            source_kinds=cohort.source_kinds,
            parametric_study_ids=cohort.parametric_study_ids,
            optimisation_study_ids=cohort.optimisation_study_ids,
        )


class CohortService:
    """Lifecycle + live-membership service for job cohorts.

    Governance is capability-based (``designs/job-cohorts.md`` D6): a
    cohort is a workspace-level object, so **writes** (create / edit /
    archive) require the ``cohorts.curate`` permission (the
    ``cohort_curator`` role; ``super_user`` / ``administrator`` hold it)
    and **reads** are open to any authenticated caller. Cross-project
    membership still requires the curator's Viewer+ on every referenced
    project at create; and every member resolution intersects with the
    caller's per-project ACL so results from projects the caller can't
    see are hidden (a count, never an error — D7).
    """

    def __init__(self, audit: AuditWriter) -> None:
        """Compose over the shared hash-chained audit writer."""
        self.audit = audit
        self._coverage_cache: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        """Cohort-preview coverage reports, keyed per ADR-0020 §9.1.

        An ``auto_grow`` cohort re-resolves its query on **every read**, so
        recomputing coverage per page load would violate
        ``feedback_performance_and_security_equal``. Bounded and LRU-evicted:
        an unbounded dict keyed on a moving membership has no ceiling."""
        self._coverage_computations = 0

    @property
    def coverage_cache_computations(self) -> int:
        """How many coverage analyses this service has actually run.

        Exposed so the cache is testable *as a cache*: that an unchanged read
        does not recompute, and that a changed one does, are the two things
        worth asserting and neither is visible from the report alone.
        """
        return self._coverage_computations

    @property
    def coverage_cache_size(self) -> int:
        """Entries currently held (bounded by ``_COVERAGE_CACHE_MAX``)."""
        return len(self._coverage_cache)

    # --- lifecycle -------------------------------------------------------

    async def create_cohort(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        name: str,
        tool_alias: str,
        tolerated_tool_version_ids: list[int],
        description: str = "",
        tolerated_version_bands: list[str] | None = None,
        auto_grow: bool = True,
        member_job_ids: list[int] | None = None,
        excluded_job_ids: list[int] | None = None,
        **filters: Any,
    ) -> JobCohort:
        """Create an ``active`` workspace-level cohort (requires ``cohorts.curate``).

        A cohort has no home project (D5): it is keyed on a single tool +
        its tolerated versions and gathers runs across the whole workspace
        (or the projects named in ``project_ids``). Membership follows
        ``auto_grow`` (D4):

        - ``auto_grow=True`` (default) — the saved filters re-resolve live;
          ``excluded_job_ids`` permanently drops specific live matches.
        - ``auto_grow=False`` — ``member_job_ids`` is the frozen, curated
          set; each must be a completed run of ``tool_alias`` at a tolerated
          version that the curator can see.

        Validates the tolerated-version list (non-empty, every version
        belongs to ``tool_alias``) and the optional filters. When
        ``project_ids`` names specific projects, the curator must hold
        Viewer+ on each (D7).
        """
        await self._require_create(session, actor_user_id)
        cleaned_name = name.strip()
        if not cleaned_name:
            raise CohortConfigError("A cohort needs a name.")
        bands, tolerated_tool_version_ids = await self._resolve_tolerance(
            session, tool_alias, bands=tolerated_version_bands, ids=tolerated_tool_version_ids
        )
        await self._validate_tolerated_versions(session, tool_alias, tolerated_tool_version_ids)
        clean_filters = await self._validate_filters(session, actor_user_id, filters)

        frozen_members: list[int] | None = None
        if not auto_grow:
            frozen_members = await self._validate_member_jobs(
                session,
                actor_user_id,
                tool_alias=tool_alias.strip(),
                tolerated_tool_version_ids=list(tolerated_tool_version_ids),
                job_ids=member_job_ids or [],
            )

        cohort = JobCohort(
            name=cleaned_name,
            description=description.strip(),
            tool_alias=tool_alias.strip(),
            tolerated_tool_version_ids=list(tolerated_tool_version_ids),
            tolerated_version_bands=bands,
            auto_grow=auto_grow,
            member_job_ids=frozen_members,
            excluded_job_ids=list(excluded_job_ids) if excluded_job_ids else None,
            state="active",
            created_by_user_id=actor_user_id,
            **clean_filters,
        )
        session.add(cohort)
        await session.flush()
        await self._audit(
            session,
            cohort,
            "study.cohort.created",
            actor_user_id,
            payload={
                "name": cohort.name,
                "tool_alias": cohort.tool_alias,
                "tolerated_tool_version_ids": cohort.tolerated_tool_version_ids,
                "auto_grow": cohort.auto_grow,
                "member_count": len(frozen_members) if frozen_members is not None else None,
                "cross_project": cohort.project_ids is not None,
            },
        )
        return cohort

    async def update_cohort(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        cohort_id: int,
        name: str | None = None,
        description: str | None = None,
        tolerated_tool_version_ids: list[int] | None = None,
        tolerated_version_bands: list[str] | None = None,
        auto_grow: bool | None = None,
        member_job_ids: list[int] | None = None,
        excluded_job_ids: list[int] | None = None,
        **filters: Any,
    ) -> JobCohort:
        """Edit an ``active`` cohort's filters / membership / metadata (``cohorts.curate``).

        Optimistic-locked via the ORM ``version`` column. Archived
        cohorts are immutable (D20). A filter keyword **not passed at all** is
        left unchanged; one passed as ``None`` is **cleared**. The caller
        decides which to send -- the API router derives it from the request
        body's set fields, so an emptied box in the edit form clears the
        filter instead of silently keeping it.

        ``auto_grow`` may be flipped: switching to a fixed cohort
        (``False``) requires a ``member_job_ids`` set (or an existing one);
        switching to live (``True``) clears the frozen set.
        """
        await self._require_curate(session, actor_user_id)
        cohort = await self._get(session, cohort_id)
        if cohort.state != "active":
            raise CohortStateError(
                f"This cohort is {cohort.state!r}; only an active cohort can be edited."
            )
        if name is not None:
            cleaned = name.strip()
            if not cleaned:
                raise CohortConfigError("A cohort needs a name.")
            cohort.name = cleaned
        if description is not None:
            cohort.description = description.strip()
        if tolerated_version_bands is not None or tolerated_tool_version_ids is not None:
            bands, ids = await self._resolve_tolerance(
                session,
                cohort.tool_alias,
                bands=tolerated_version_bands,
                ids=tolerated_tool_version_ids or [],
            )
            await self._validate_tolerated_versions(session, cohort.tool_alias, ids)
            cohort.tolerated_tool_version_ids = list(ids)
            cohort.tolerated_version_bands = bands
        # An explicit ``None`` **clears** the filter; a key the caller did not
        # pass at all never reaches here. Only real values are validated --
        # there is nothing to check about "no longer filtered".
        supplied = {k: v for k, v in filters.items() if k in _FILTER_FIELDS}
        # ``None`` clears, and so does an emptied list: unticking the last
        # commissioned study has to leave the column NULL rather than ``[]``,
        # because every reader tests the column for truthiness.
        cleared = {k: None for k, v in supplied.items() if v is None or v == []}
        supplied = {k: v for k, v in supplied.items() if k not in cleared}
        clean_filters = await self._validate_filters(
            session, actor_user_id, {k: v for k, v in supplied.items() if v is not None}
        )
        clean_filters.update(cleared)
        for key, value in clean_filters.items():
            setattr(cohort, key, value)

        await self._apply_membership_edit(
            session,
            actor_user_id,
            cohort,
            auto_grow=auto_grow,
            member_job_ids=member_job_ids,
            excluded_job_ids=excluded_job_ids,
        )

        await session.flush()
        await self._audit(
            session,
            cohort,
            "study.cohort.query_edited",
            actor_user_id,
            payload={"fields": sorted({*clean_filters, *_meta_keys(name, description)})},
        )
        return cohort

    async def _apply_membership_edit(
        self,
        session: AsyncSession,
        actor_user_id: int,
        cohort: JobCohort,
        *,
        auto_grow: bool | None,
        member_job_ids: list[int] | None,
        excluded_job_ids: list[int] | None,
    ) -> None:
        """Apply the ``auto_grow`` / membership half of an update in place."""
        effective_auto_grow = cohort.auto_grow if auto_grow is None else auto_grow
        if effective_auto_grow:
            cohort.auto_grow = True
            cohort.member_job_ids = None
            if excluded_job_ids is not None:
                cohort.excluded_job_ids = list(excluded_job_ids) or None
        else:
            # Fixed cohort: keep the existing frozen set unless a new one is
            # supplied; require one to exist after the edit.
            if member_job_ids is not None:
                cohort.member_job_ids = await self._validate_member_jobs(
                    session,
                    actor_user_id,
                    tool_alias=cohort.tool_alias,
                    tolerated_tool_version_ids=list(cohort.tolerated_tool_version_ids),
                    job_ids=member_job_ids,
                )
            elif not cohort.member_job_ids:
                raise CohortConfigError(
                    "A fixed (non-growing) cohort needs at least one run in it. "
                    "Widen the filters until the cohort matches something, or "
                    "archive it to stop it gathering runs altogether."
                )
            cohort.auto_grow = False
            # Kept, not cleared. A fixed cohort can still auto-include studies,
            # and an exclusion is the only way to keep one of *those* runs out —
            # they are not in ``member_job_ids``, so there is nothing to remove.
            if excluded_job_ids is not None:
                cohort.excluded_job_ids = list(excluded_job_ids) or None

    async def archive_cohort(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        cohort_id: int,
    ) -> JobCohort:
        """Archive a cohort — hide from default lists, never delete (D20).

        Requires ``cohorts.curate``.
        """
        await self._require_curate(session, actor_user_id)
        cohort = await self._get(session, cohort_id)
        if cohort.state == "archived":
            return cohort
        cohort.state = "archived"
        cohort.archived_at = _utcnow()
        cohort.archived_by_user_id = actor_user_id
        await session.flush()
        await self._audit(session, cohort, "study.cohort.archived", actor_user_id, payload={})
        return cohort

    async def get(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,  # noqa: ARG002 -- open read (D6); kept for API symmetry
        cohort_id: int,
    ) -> JobCohort:
        """Fetch a cohort (open read — any authenticated caller, D6)."""
        return await self._get(session, cohort_id)

    async def list_all(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,  # noqa: ARG002 -- open read (D6); kept for API symmetry
        include_archived: bool = False,
    ) -> list[JobCohort]:
        """List every cohort workspace-wide (open read, D6); archived hidden by default.

        Cohorts are workspace-level objects (D5), so this is the single
        listing — there is no per-project scoping. A cohort's *results*
        are still ACL-intersected at resolve time (D7).
        """
        stmt = select(JobCohort)
        if not include_archived:
            stmt = stmt.where(JobCohort.state == "active")
        stmt = stmt.order_by(JobCohort.created_at.desc(), JobCohort.id.desc())
        return list((await session.execute(stmt)).scalars().all())

    # --- live membership (§3.1) -----------------------------------------

    async def resolve_members(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        cohort_id: int,
        stamp: bool = True,
    ) -> ResolvedMembers:
        """Compute the cohort's membership for ``actor_user_id`` (D4).

        For an ``auto_grow`` cohort: runs the §3.1 selection workspace-wide
        (or over ``project_ids``), applies the Python ``input_ranges`` +
        ``cross_field_predicate`` filters, and drops ``excluded_job_ids``.
        For a fixed cohort: the membership is the frozen ``member_job_ids``
        set. Either way the result is intersected with the caller's
        per-project ACL (D7). Touches ``last_resolved_at`` unless ``stamp`` is
        False.

        Pass ``stamp=False`` from anything that only *reads* membership (the
        cohort list, the results matrix, a fit's ingestion). Stamping turns a read
        into a write: the list stamped every cohort, so two lists at once — the
        surrogate form loads it twice when opened from a cohort — collided on the
        same rows and one failed ("Couldn't list sources."), and on SQLite each
        stamp also queued behind any other write in progress.

        Open read (D6): any authenticated caller may resolve a cohort;
        the per-project ACL intersect below is what keeps results from
        projects the caller can't see hidden.
        """
        cohort = await self._get(session, cohort_id)
        if cohort.auto_grow:
            effective_ids = await self._effective_version_ids(session, cohort)
            rows = await self._candidate_rows(
                session, _CandidateFilter.from_cohort(cohort, effective_ids)
            )
            excluded = set(cohort.excluded_job_ids or [])
            members = [
                row
                for row in rows
                if row.id not in excluded
                and self._inputs_pass(
                    cohort.input_ranges, cohort.cross_field_predicate, dict(row.inputs or {})
                )
            ]
        else:
            # A fixed cohort honours ``excluded_job_ids`` as well. It used not
            # to, which was invisible until a fixed cohort also auto-included a
            # study: de-selecting one of those runs removed it from
            # ``member_job_ids``, so the summary count fell, and then
            # ``_commissioned_rows`` put it straight back because nothing
            # recorded that the curator had thrown it out. The summary and the
            # plots then disagreed, and every consumer of the membership —
            # coverage, the results matrix, a surrogate fitted from the cohort —
            # took the larger set.
            excluded = set(cohort.excluded_job_ids or [])
            members = [
                row
                for row in await self._static_member_rows(session, cohort.member_job_ids or [])
                if row.id not in excluded
            ]

        members = members + await self._commissioned_rows(session, cohort, members)

        visible_pids = await self._visible_project_ids(
            session, actor_user_id, {row.pid for row in members}
        )
        job_ids = [row.id for row in members if row.pid in visible_pids]
        acl_hidden = sum(1 for row in members if row.pid not in visible_pids)

        if not stamp:
            return ResolvedMembers(
                job_ids=job_ids,
                acl_hidden=acl_hidden,
                last_resolved_at=cohort.last_resolved_at or _utcnow(),
            )
        cohort.last_resolved_at = _utcnow()
        await session.flush()
        return ResolvedMembers(
            job_ids=job_ids, acl_hidden=acl_hidden, last_resolved_at=cohort.last_resolved_at
        )

    async def preview_candidates(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        tool_alias: str,
        tolerated_tool_version_ids: list[int],
        tolerated_version_bands: list[str] | None = None,
        **filters: Any,
    ) -> PreviewResult:
        """Preview the runs a not-yet-saved cohort query would gather (``cohorts.curate``).

        The builder calls this as the curator tweaks the tool / versions /
        filters: it runs the same workspace-wide §3.1 selection as live
        membership but against raw params (no cohort row), returning the
        matching completed runs with enough metadata (project, source,
        version, completion, inputs) to populate the preview table — plus
        a count of matches the curator can't see (ACL-hidden).
        ``tolerated_version_bands`` (when given) is expanded to current
        version ids, exactly as live membership does.
        """
        await self._require_curate(session, actor_user_id)
        _, tolerated_tool_version_ids = await self._resolve_tolerance(
            session, tool_alias, bands=tolerated_version_bands, ids=tolerated_tool_version_ids
        )
        await self._validate_tolerated_versions(session, tool_alias, tolerated_tool_version_ids)
        present = {k: v for k, v in filters.items() if k in _FILTER_FIELDS and v is not None}
        clean = await self._validate_filters(session, actor_user_id, present)
        filt = _CandidateFilter(
            tool_alias=tool_alias.strip(),
            tolerated_tool_version_ids=list(tolerated_tool_version_ids),
            target_project_ids=clean.get("project_ids"),
            scenario_ids=clean.get("scenario_ids"),
            owner_user_ids=clean.get("owner_user_ids"),
            completed_after=clean.get("completed_after"),
            completed_before=clean.get("completed_before"),
            library_file_version_ids=clean.get("library_file_version_ids"),
            material_version_ids=clean.get("material_version_ids"),
            source_kinds=clean.get("source_kinds"),
            parametric_study_ids=clean.get("parametric_study_ids"),
            optimisation_study_ids=clean.get("optimisation_study_ids"),
        )
        rows = await self._candidate_rows(session, filt)
        input_ranges = clean.get("input_ranges")
        predicate = clean.get("cross_field_predicate")
        rows = [r for r in rows if self._inputs_pass(input_ranges, predicate, dict(r.inputs or {}))]
        visible_pids = await self._visible_project_ids(
            session, actor_user_id, {r.pid for r in rows}
        )
        visible = [
            CandidateRow(
                job_id=r.id,
                project_id=r.pid,
                source=r.source,
                source_name=r.source_name,
                created_by_user_id=r.created_by_user_id,
                tool_version_id=r.tool_version_id,
                completed_at=r.completed_at,
                inputs=dict(r.inputs or {}),
            )
            for r in rows
            if r.pid in visible_pids
        ]
        acl_hidden = sum(1 for r in rows if r.pid not in visible_pids)
        return PreviewResult(rows=visible, acl_hidden=acl_hidden)

    async def tool_build_meta(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        tool_alias: str,
    ) -> ToolBuildMeta:
        """Version bands + numeric inputs for the builder (``cohorts.curate``)."""
        await self._require_curate(session, actor_user_id)
        bands = await self._version_bands(session, tool_alias)
        if not bands:
            raise CohortConfigError(
                f"'{tool_alias}' has no installed versions to build a cohort on."
            )
        numerics = await self._numeric_inputs(session, tool_alias)
        launchers, parametric, optimisation = await self._tool_facets(
            session, actor_user_id=actor_user_id, tool_alias=tool_alias, bands=bands
        )
        return ToolBuildMeta(
            version_bands=bands,
            numeric_inputs=numerics,
            launchers=launchers,
            parametric_studies=parametric,
            optimisation_studies=optimisation,
        )

    async def _tool_facets(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        tool_alias: str,
        bands: list[VersionBand],
    ) -> tuple[list[Facet], list[Facet], list[Facet]]:
        """Who launched runs of this tool, and which studies produced them.

        Derived from **the same candidate universe the preview draws from** —
        every completed run of the tool at any installed version, intersected
        with the caller's project ACL. A facet list built any other way could
        offer a filter that matches nothing (a user who ran a different tool, a
        study in a project the caller cannot see), which is worse than no
        filter: the reader blames the filter rather than the data.
        """
        from matofx.users.orm import User  # noqa: PLC0415

        version_ids = [vid for band in bands for vid in band.version_ids]
        if not version_ids:
            return [], [], []
        rows = await self._candidate_rows(
            session,
            _CandidateFilter(tool_alias=tool_alias, tolerated_tool_version_ids=version_ids),
        )
        visible = await self._visible_project_ids(session, actor_user_id, {r.pid for r in rows})
        rows = [r for r in rows if r.pid in visible]
        if not rows:
            return [], [], []

        user_ids = {int(r.created_by_user_id) for r in rows if r.created_by_user_id is not None}
        rows_named = (
            await session.execute(
                select(User.id, func.coalesce(User.full_name, User.username)).where(
                    User.id.in_(user_ids)
                )
            )
        ).all()
        names: dict[int, str] = {int(uid): str(label) for uid, label in rows_named}
        launchers = sorted(
            (Facet(id=uid, name=str(names.get(uid) or uid)) for uid in user_ids),
            key=lambda f: f.name.casefold(),
        )
        parametric = self._study_facets(rows, "parametric_study_id")
        optimisation = self._study_facets(rows, "optimisation_study_id")
        return launchers, parametric, optimisation

    @staticmethod
    def _study_facets(rows: list[Any], id_attr: str) -> list[Facet]:
        """Distinct ``(study id, name)`` pairs among ``rows``, by name."""
        seen: dict[int, str] = {}
        for row in rows:
            sid = getattr(row, id_attr, None)
            if sid is None:
                continue
            seen.setdefault(int(sid), str(row.source_name or f"#{sid}"))
        return sorted(
            (Facet(id=sid, name=name) for sid, name in seen.items()),
            key=lambda f: f.name.casefold(),
        )

    async def _version_bands(self, session: AsyncSession, tool_alias: str) -> list[VersionBand]:
        """Group a tool's versions by ``major.minor``, newest band first (§3.2)."""
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        rows = (
            await session.execute(
                select(ToolVersion.id, ToolVersion.version).where(
                    ToolVersion.alias == tool_alias.strip()
                )
            )
        ).all()
        groups: dict[str, dict[str, Any]] = {}
        for vid, version in rows:
            band = _major_minor(str(version))
            key = _version_sort_key(str(version))
            g = groups.get(band)
            if g is None:
                groups[band] = {"ids": [int(vid)], "latest": str(version), "latest_key": key}
            else:
                g["ids"].append(int(vid))
                if key >= g["latest_key"]:
                    g["latest"], g["latest_key"] = str(version), key
        ordered = sorted(groups.items(), key=lambda kv: _version_sort_key(kv[0]), reverse=True)
        return [
            VersionBand(major_minor=band, latest_version=g["latest"], version_ids=g["ids"])
            for band, g in ordered
        ]

    async def _numeric_inputs(self, session: AsyncSession, tool_alias: str) -> list[NumericInput]:
        """The int/float inputs of the tool's latest descriptor (label-first)."""
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        latest_id = await self._latest_tool_version_id(session, tool_alias)
        tool_version = await session.get(ToolVersion, latest_id)
        snapshot = (tool_version.descriptor_snapshot or {}) if tool_version is not None else {}
        inputs = snapshot.get("inputs", []) if isinstance(snapshot, dict) else []
        numerics: list[NumericInput] = []
        if isinstance(inputs, list):
            for entry in inputs:
                if not isinstance(entry, dict) or entry.get("type") not in {"integer", "float"}:
                    continue
                name = str(entry.get("name") or "")
                if not name:
                    continue
                numerics.append(NumericInput(name=name, label=str(entry.get("label") or name)))
        return numerics

    async def tool_output_columns(
        self, session: AsyncSession, *, tool_alias: str
    ) -> list[OutputColumn]:
        """Ordered response-column metadata for a cohort's tool (open read).

        The label + unit for each scalar output, in descriptor order, so
        the results matrix can render human headers. Returns ``[]`` when
        the tool has no installed version / no declared outputs (the matrix
        then falls back to the bare keys present in the rows).
        """
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        try:
            latest_id = await self._latest_tool_version_id(session, tool_alias)
        except CohortConfigError:
            return []
        tool_version = await session.get(ToolVersion, latest_id)
        snapshot = (tool_version.descriptor_snapshot or {}) if tool_version is not None else {}
        outputs = snapshot.get("outputs", []) if isinstance(snapshot, dict) else []
        cols: list[OutputColumn] = []
        if isinstance(outputs, list):
            for entry in outputs:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "")
                # File / table outputs are never scalar response axes
                # (mirrors ``results_service._extract_scalars``); skip them
                # so they don't seed phantom columns.
                if not name or entry.get("type") in {"file", "table"}:
                    continue
                cols.append(
                    OutputColumn(
                        name=name,
                        label=str(entry.get("label") or name),
                        unit=str(entry.get("unit") or ""),
                    )
                )
        return cols

    async def tool_input_columns(
        self, session: AsyncSession, *, tool_alias: str
    ) -> list[OutputColumn]:
        """Ordered input-parameter columns for a cohort's tool (open read).

        The label + unit for each non-file input, in descriptor order, so
        the results matrix can show the inputs alongside the responses.
        Returns ``[]`` when the tool has no installed version / inputs.
        """
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        try:
            latest_id = await self._latest_tool_version_id(session, tool_alias)
        except CohortConfigError:
            return []
        tool_version = await session.get(ToolVersion, latest_id)
        snapshot = (tool_version.descriptor_snapshot or {}) if tool_version is not None else {}
        inputs = snapshot.get("inputs", []) if isinstance(snapshot, dict) else []
        cols: list[OutputColumn] = []
        if isinstance(inputs, list):
            for entry in inputs:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "")
                # File inputs aren't tabular parameter values; skip them.
                if not name or entry.get("type") == "file":
                    continue
                cols.append(
                    OutputColumn(
                        name=name,
                        label=str(entry.get("label") or name),
                        unit=str(entry.get("unit") or ""),
                    )
                )
        return cols

    async def _resolve_tolerance(
        self,
        session: AsyncSession,
        tool_alias: str,
        *,
        bands: list[str] | None,
        ids: list[int],
    ) -> tuple[list[str] | None, list[int]]:
        """Return ``(bands_to_store, version_ids)`` for create / update (§3.2).

        Band path: normalise to ``major.minor``, expand to the current
        matching version ids (the snapshot). Legacy id path (no bands):
        default an empty id list to the tool's latest version, store no
        bands.
        """
        if bands is not None:
            cleaned = sorted({_major_minor(b) for b in bands if str(b).strip()})
            if not cleaned:
                raise CohortConfigError("Pick at least one tool version.")
            expanded = await self._expand_bands(session, tool_alias, cleaned)
            if not expanded:
                raise CohortConfigError("None of the chosen versions are installed for this tool.")
            return cleaned, expanded
        if not ids:
            ids = [await self._latest_tool_version_id(session, tool_alias)]
        return None, list(ids)

    async def _expand_bands(
        self, session: AsyncSession, tool_alias: str, bands: list[str]
    ) -> list[int]:
        """Current ``tool_version.id``s whose ``major.minor`` is in ``bands`` (§3.2)."""
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        band_set = set(bands)
        rows = (
            await session.execute(
                select(ToolVersion.id, ToolVersion.version).where(
                    ToolVersion.alias == tool_alias.strip()
                )
            )
        ).all()
        return [int(vid) for vid, version in rows if _major_minor(str(version)) in band_set]

    async def _effective_version_ids(self, session: AsyncSession, cohort: JobCohort) -> list[int]:
        """The version ids a live cohort tolerates *right now* (§3.2).

        Band cohorts re-expand on every read so future patches within a
        tolerated band join automatically; band-less cohorts use the
        stored id snapshot.
        """
        if cohort.tolerated_version_bands:
            return await self._expand_bands(
                session, cohort.tool_alias, cohort.tolerated_version_bands
            )
        return list(cohort.tolerated_tool_version_ids)

    async def coverage_preview(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        cohort_id: int,
        overrides: Mapping[str, Any] | None = None,
        settings: Any = None,
    ) -> Any:
        """Answer "is this cohort fit to learn from?" (ADR-0020 §12 — open read).

        The **response-agnostic** design pass: purely geometric, no GP, no
        response. That is what makes it answerable at preview at all — asking
        it after a response was chosen would mean fitting a model to decide
        whether to fit a model.

        Reads through :func:`collect_training_data` so the points are the
        ones that would actually *train* — post the ADR-0017 validity gate,
        post ``FactorFilter`` — never raw membership, which would overstate
        coverage by exactly the runs the platform judged unfit to learn from.

        Read-only, and open to anyone who can see the cohort: committing runs
        from the suggestions is a separate right, gated where that happens.
        """
        from datetime import UTC, datetime  # noqa: PLC0415

        from matofx.studies.coverage import (  # noqa: PLC0415
            analyse_design,
            build_report,
        )

        # No response is chosen yet, so the spec names none. Only
        # ``TrainingData.response`` depends on it; the factor columns — which
        # is all the design pass measures — are the same either way. A
        # per-response pass at fit time may therefore train on *fewer* points
        # than this, where that response is missing on some runs; that is the
        # honest difference between "could this teach anything" and "what did
        # this particular output learn".
        points, envelope = await self._coverage_inputs(
            session, actor_user_id=actor_user_id, cohort_id=cohort_id, overrides=overrides
        )
        modes = await self._sampling_modes(
            session, actor_user_id=actor_user_id, cohort_id=cohort_id
        )
        key = self._coverage_key(cohort_id, points, envelope, settings)
        cached = self._coverage_cache.get(key)
        if cached is not None:
            self._coverage_cache.move_to_end(key)
            return cached

        self._coverage_computations += 1

        def _compute() -> Any:
            design = analyse_design(points, envelope, settings=settings, sampling_modes=modes)
            return build_report(
                design,
                envelope=envelope,
                source_kind="cohort",
                computed_at=datetime.now(UTC),
                points=points,
            )

        # **Off the event loop.** This is NumPy-heavy and unbounded in the
        # archive's size — 1.85 s on a real 708-run cohort — and it used to run
        # inline in an `async def`, stalling every other request on the server
        # for the duration.
        #
        # The `await` opens a window where two concurrent misses both compute.
        # That is deliberate and harmless: the analysis is deterministic, so
        # both produce an identical report and the second simply overwrites the
        # first. A lock would serialise the slow path to make the fast path no
        # faster.
        report = await asyncio.to_thread(_compute)
        self._coverage_cache[key] = report
        while len(self._coverage_cache) > _COVERAGE_CACHE_MAX:
            self._coverage_cache.popitem(last=False)
        return report

    def _coverage_key(
        self, cohort_id: int, points: Any, envelope: Any, settings: Any
    ) -> tuple[Any, ...]:
        """The §9.1 cache key: cohort, membership, envelope — and the floors.

        Membership is hashed from the **resolved rows**, not from the cohort's
        stored definition: an ``auto_grow`` cohort's membership is a query
        result, so keying on the definition would keep serving yesterday's
        verdict after new runs matched it — precisely the failure this feature
        exists to report.

        The declared envelope and the verdict floors are part of the key too.
        Both change the answer, and a cache that ignored either would hand
        back a verdict reached under a different question.
        """
        from matofx.config import CoverageSettings  # noqa: PLC0415

        resolved = settings if settings is not None else CoverageSettings()
        members = tuple(
            sorted(_json.dumps(dict(row), sort_keys=True, default=str) for row in points.rows)
        )
        return (
            cohort_id,
            hash(members),
            hash(tuple(_json.dumps(f.to_json_obj(), sort_keys=True) for f in envelope.factors)),
            hash(_json.dumps(resolved.model_dump(mode="json"), sort_keys=True)),
        )

    async def coverage_suggest(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        cohort_id: int,
        n: int,
        overrides: Mapping[str, Any] | None = None,
        settings: Any = None,
    ) -> Any:
        """Plan runs that would most improve this cohort's coverage (§11.5).

        **Read-only and open** to anyone who can see the cohort: planning a
        batch spends nothing. Committing it does, and that is a separate right
        enforced in :meth:`create_study_from_coverage`.

        Model-free by construction — cohort preview has no fitted model and no
        chosen response (§2.1) — so the batch is attributed to the design
        rather than to a response it cannot name.
        """
        from matofx.studies.coverage import suggest_design_points  # noqa: PLC0415

        points, envelope = await self._coverage_inputs(
            session, actor_user_id=actor_user_id, cohort_id=cohort_id, overrides=overrides
        )
        # Off the event loop for the same reason as the report: greedy
        # k-centre over the candidate set is 0.84 s on the real archive.
        return await asyncio.to_thread(
            suggest_design_points,
            points,
            envelope,
            n=n,
            seed=_COVERAGE_SUGGEST_SEED,
            settings=settings,
        )

    async def coverage_suggestion_plan(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        cohort_id: int,
        sizes: list[int] | None = None,
        overrides: Mapping[str, Any] | None = None,
        measure_overrides: Mapping[str, Any] | None = None,
        settings: Any = None,
    ) -> Any:
        """What each candidate batch size would buy this cohort (§11.5).

        The panel's real question. :meth:`coverage_suggest` answers "give me
        N runs", which presumes the user already knows N -- and N is precisely
        what they came to find out. This prices a spread of batch sizes
        against the same measurement the verdict uses, so the choice can be
        made from the curve rather than guessed.

        ``overrides`` shapes the space the batch may **move** in;
        ``measure_overrides``, when given, shapes the space the figures
        **describe**. They differ whenever an input is left out of the batch
        but should go on reporting its own gaps — leaving it out is expressed
        by pinning it, and a pinned axis is measured by nothing.

        Read-only and open, like the report and the batch: pricing spends
        nothing. Committing is :meth:`create_study_from_coverage`.
        """
        from matofx.studies.coverage import suggestion_plan  # noqa: PLC0415

        points, envelope = await self._coverage_inputs(
            session, actor_user_id=actor_user_id, cohort_id=cohort_id, overrides=overrides
        )
        measure_envelope = None
        if measure_overrides is not None:
            _, measure_envelope = await self._coverage_inputs(
                session,
                actor_user_id=actor_user_id,
                cohort_id=cohort_id,
                overrides=measure_overrides,
            )
        # Off the event loop: one greedy walk plus a covering radius per
        # offered size -- several seconds on a real archive.
        return await asyncio.to_thread(
            suggestion_plan,
            points,
            envelope,
            seed=_COVERAGE_SUGGEST_SEED,
            # `None` lets the engine size the ladder to the design space
            # (see `default_plan_sizes`); an explicit list is the user's own.
            sizes=tuple(sizes) if sizes else None,
            settings=settings,
            measure_envelope=measure_envelope,
        )

    async def create_study_from_coverage(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        cohort_id: int,
        project_id: int,
        name: str,
        n: int,
        overrides: Mapping[str, Any] | None = None,
        settings: Any = None,
    ) -> ParametricStudy:
        """Turn the suggestions into a governed parametric study (§11.5).

        The batch lands as an **auditable draft inside the platform**, not a
        CSV emailed around: a ``table``-mode study (ADR-0001) whose rows are
        exactly the suggested points, in the project the caller chooses, left
        in ``draft`` so somebody reviews it before any solver runs.

        **Gated on the study-create permission**, unlike the analysis that
        produced it. This spawns real jobs and spends real compute, so it is
        deliberately a different right from reading the verdict — and the
        caller still needs EDIT on the target project, which
        ``StudyService.create`` enforces in its own turn.
        """
        from matofx.studies.service import (  # noqa: PLC0415
            DEFAULT_RUN_CAP,
            make_study_service,
        )

        if not await user_has_permission(
            session, actor_user_id, Permission.PARAMETRIC_STUDIES_CREATE
        ):
            # The canonical 403 message. Deliberately the same one every other
            # permission gate uses: the distinction that matters here is that
            # the *analysis* stayed available, which the user can see because
            # they are looking at it.
            raise AccessDeniedError()

        cohort = await self.get(session, actor_user_id=actor_user_id, cohort_id=cohort_id)
        batch = await self.coverage_suggest(
            session,
            actor_user_id=actor_user_id,
            cohort_id=cohort_id,
            n=n,
            overrides=overrides,
            settings=settings,
        )
        if not batch.points:
            reason = batch.warnings[0].message if batch.warnings else ""
            raise CoverageError(
                "There are no runs to suggest for this cohort, so there is nothing to "
                f"create. {reason}".strip()
            )

        factor_spec, runs = _coverage_study_spec(batch.points)
        studies = make_study_service(self.audit)
        study = await studies.create(
            session,
            actor_user_id=actor_user_id,
            project_id=project_id,
            name=name,
            tool_alias=cohort.tool_alias,
            mode="table",
            factor_spec=factor_spec,
            sampling_params={"runs": runs},
            # The cap exists to stop a *generator* running away. Here the point
            # count is exact, already priced, and was shown to the user run by
            # run before they pressed the button -- so it must not be the thing
            # that rejects the batch they just approved.
            run_cap=max(DEFAULT_RUN_CAP, len(runs)),
            description=(
                f"Runs proposed to fill gaps in the coverage of job cohort "
                f"{cohort_id} ({cohort.name})."
            ),
        )

        # **The link is the point of the button.** Without it the study is
        # commissioned to fill this cohort's gaps and its results may never
        # reach it — certainly not for a fixed cohort, and not for an
        # auto-grow one whose own filters exclude them.
        cohort.auto_include_study_ids = [*(cohort.auto_include_study_ids or []), study.id]
        await session.flush()
        await self._audit(
            session,
            cohort,
            "study.cohort.query_edited",
            actor_user_id,
            payload={"fields": ["auto_include_study_ids"], "study_id": study.id},
        )

        # **And run it.** A draft that somebody must then find and submit is
        # not what the button offers, and the batch has already had every
        # review a draft would get: the user priced it, saw each proposed run
        # with its input values, and pressed a button naming the cohort it
        # feeds. Leaving it parked said "Not submitted yet" on a page they had
        # been sent to expressly to watch it run.
        #
        # `submit` is the same call the studies page makes, so the tool's own
        # `approvals_policy` still decides what happens: `none` / `post` fan
        # the points into child jobs and queue them; `pre` / `both` raise the
        # study-level gate and the study waits at `pending_approval`. The
        # governance path is unchanged -- this only stops the *user* being an
        # extra manual step in front of it.
        return await studies.submit(session, actor_user_id=actor_user_id, study_id=study.id)

    async def _coverage_inputs(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        cohort_id: int,
        overrides: Mapping[str, Any] | None,
    ) -> tuple[Any, Any]:
        """``(points, envelope)`` for one cohort — the shared preview prologue."""
        from matofx.studies.coverage import build_envelope, resolve_points  # noqa: PLC0415
        from matofx.studies.surrogate.ingest import (  # noqa: PLC0415
            ResponseSpec,
            collect_training_data,
        )

        cohort = await self.get(session, actor_user_id=actor_user_id, cohort_id=cohort_id)
        await self.populate_study_results(session, actor_user_id=actor_user_id, cohort_id=cohort_id)
        data = await collect_training_data(
            session,
            source_kind="cohort",
            source_id=cohort_id,
            response_spec=ResponseSpec(kind="scalar", name=""),
            factor_filter=self._coverage_filter(cohort),
        )
        descriptor = await self._latest_descriptor(session, cohort.tool_alias)
        points = resolve_points(data, descriptor=descriptor)
        return points, build_envelope(points, descriptor=descriptor, overrides=overrides)

    async def _sampling_modes(
        self, session: AsyncSession, *, actor_user_id: int, cohort_id: int
    ) -> tuple[str, ...]:
        """How this cohort's runs were sampled, or ``()`` if not established.

        **The one thing that still answers "is this design any good" on a
        small cohort.** Below about twenty runs per input every geometric
        reading stops discriminating, so the panel cannot measure that a
        twenty-run archive is well arranged — but a cohort whose runs all came
        from a Latin-hypercube study is space-filling by construction, and
        that is a fact rather than an inference.

        **Every** contributing run must belong to a study, or the answer is
        "unknown". A cohort of thirty study runs and one ad-hoc job is not a
        designed set, and reporting the study's mode for it would describe
        something that is not what is being measured. Ad-hoc runs carry no
        ``parametric_study_id``, so the ``None`` branch of the count is what
        detects them.
        """
        members = await self.resolve_members(
            session, actor_user_id=actor_user_id, cohort_id=cohort_id, stamp=False
        )
        job_ids = members.job_ids
        if not job_ids:
            return ()
        rows = (
            await session.execute(
                select(ParametricStudy.mode, func.count(Job.id))
                .select_from(Job)
                .join(ParametricStudy, Job.parametric_study_id == ParametricStudy.id, isouter=True)
                .where(Job.id.in_(job_ids))
                .group_by(ParametricStudy.mode)
            )
        ).all()
        if any(mode is None for mode, _ in rows):
            return ()
        return tuple(sorted({str(mode) for mode, _ in rows if mode}))

    def _coverage_filter(self, cohort: JobCohort) -> Any:
        """The cohort's own input filter, as the ingest layer expects it.

        The same exclusions the fit will apply, so the preview measures the
        set a fit would actually see rather than a more flattering superset.
        """
        from matofx.studies.surrogate.ingest import FactorFilter  # noqa: PLC0415

        if not cohort.input_ranges and not cohort.cross_field_predicate:
            return None
        return FactorFilter(
            input_ranges=dict(cohort.input_ranges or {}) or None,
            cross_field_predicate=cohort.cross_field_predicate or None,
        )

    async def _latest_descriptor(self, session: AsyncSession, tool_alias: str) -> Any:
        """The tool's latest descriptor, or ``None`` if it won't parse.

        Coverage needs it for one thing the ``FactorSpec`` cannot supply:
        whether a numeric input is an **integer**, and so whether its axis is
        a finite lattice (ADR-0020 §3.3). Without it an integer axis degrades
        to continuous — a real loss, never a wrong answer — so a descriptor
        that fails to parse must not break the preview.
        """
        from matofx.tools.descriptor import ToolDescriptor  # noqa: PLC0415
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        latest_id = await self._latest_tool_version_id(session, tool_alias)
        tool_version = await session.get(ToolVersion, latest_id)
        snapshot = (tool_version.descriptor_snapshot or {}) if tool_version is not None else {}
        if not isinstance(snapshot, dict) or not snapshot:
            return None
        try:
            return ToolDescriptor.model_validate(snapshot)
        except Exception:  # a stale snapshot degrades the answer, never breaks it
            return None

    async def populate_study_results(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        cohort_id: int,
    ) -> int:
        """Lazily materialise ``study_result`` rows for the cohort (§3.3).

        Resolves live membership, then writes one
        ``(source_kind='cohort', source_id=cohort.id, job_id=…)`` row per
        member that doesn't already have one — incremental on every call
        (the cheap diff). Each row's scalars + retained files come from an
        existing ``study_result`` row for that job when one exists
        (parametric children already have one), else from the job's
        ``outputs.json`` on the retained floor (ADR-0003). Returns the
        number of new rows written.

        **Why lazy, not eager?** A job doesn't know which cohorts it
        belongs to (cohorts are queries, not memberships), so nothing
        writes here on job completion — only a cohort *read* with intent
        to consume triggers it (§3.3, non-negotiable #1).
        """
        cohort = await self._get(session, cohort_id)
        resolved = await self.resolve_members(
            session, actor_user_id=actor_user_id, cohort_id=cohort_id, stamp=False
        )
        # **Reconcile, don't just top up.** These rows are a *projection* of
        # membership, and membership changes: a run de-selected, a study swapped
        # for a better one, a filter narrowed. Writing only the missing ones left
        # every job that had *ever* been a member in the table, so the cohort page
        # showed 80 members while coverage and `collect_training_data` read 100 —
        # and a surrogate fitted from the cohort silently trained on runs the
        # curator had removed. Deleting loses nothing: the run's own result lives
        # on its own row (its study's) and on disk; this is the copy.
        await session.execute(
            delete(StudyResult)
            .where(StudyResult.source_kind == "cohort")
            .where(StudyResult.source_id == cohort_id)
            .where(StudyResult.job_id.notin_(resolved.job_ids or [-1]))
        )
        if not resolved.job_ids:
            return 0
        # Existing cohort rows, keyed by job, so we can both skip the cheap
        # diff *and* re-key any legacy row whose scalars still carry the
        # mixed (``<member>.<name>``) keying an optimisation-workflow source
        # wrote — normalisation is idempotent, so a clean row is untouched.
        existing_rows: dict[int, StudyResult] = {
            row.job_id: row
            for row in (
                await session.execute(
                    select(StudyResult).where(
                        StudyResult.source_kind == "cohort",
                        StudyResult.source_id == cohort_id,
                        StudyResult.job_id.in_(resolved.job_ids),
                    )
                )
            )
            .scalars()
            .all()
        }
        written = 0
        for job_id in resolved.job_ids:
            existing = existing_rows.get(job_id)
            if existing is not None:
                normalised = _normalise_cohort_scalars(
                    dict(existing.scalars or {}), cohort.tool_alias
                )
                if normalised != (existing.scalars or {}):
                    existing.scalars = normalised
                # **Heal an empty file list from disk.** Rows written before the
                # alias/PK lookup fix carry ``retained_files == []`` however many
                # ``.vtu`` files sit in the working directory, and the only symptom
                # is a field surrogate three steps later reporting "0 usable runs
                # with a retained snapshot". Re-keying scalars and moving on left
                # those rows broken forever, because nothing else ever rewrites
                # them. A job that genuinely produced no files re-reads a small
                # JSON document and stays empty, which is the acceptable half of
                # not being able to tell the two apart from the row alone.
                #
                # ``_scalars_from_disk``, **not** ``_scalars_for_job``: the
                # latter prefers any existing ``study_result`` for the job, and
                # for an ad-hoc member the only one there is the very row being
                # healed — so it would hand back the same empty list and the
                # heal would be a no-op.
                if not (existing.retained_files or []):
                    _, retained = await self._scalars_from_disk(session, job_id)
                    if retained:
                        existing.retained_files = retained
                continue
            scalars, retained = await self._scalars_for_job(session, job_id)
            session.add(
                StudyResult(
                    source_kind="cohort",
                    source_id=cohort_id,
                    job_id=job_id,
                    scalars=_normalise_cohort_scalars(scalars, cohort.tool_alias),
                    retained_files=retained,
                )
            )
            written += 1
        await session.flush()
        await self._audit(
            session,
            cohort,
            "study.cohort.results_populated",
            actor_user_id,
            payload={"members": len(resolved.job_ids), "written": written},
        )
        return written

    async def tolerated_version_warnings(
        self,
        session: AsyncSession,
        *,
        tool_alias: str,
        anchor_version_id: int,
        candidate_version_id: int,
    ) -> list[str]:
        """Warn if adding ``candidate_version_id`` would mix divergent descriptors.

        Compares the input set / type / unit of the two versions (§3.2).
        Empty list = the descriptors match on the dimensions that affect
        a surrogate fit. The curator decides; the system only warns
        (non-negotiable #6 — never inferred equivalence).
        """
        anchor = await self._descriptor_inputs(session, tool_alias, anchor_version_id)
        candidate = await self._descriptor_inputs(session, tool_alias, candidate_version_id)
        return _diverging_input_warnings(anchor, candidate)

    # --- internals -------------------------------------------------------

    async def _candidate_rows(self, session: AsyncSession, filt: _CandidateFilter) -> list[Any]:
        """The §3.1 SQL selection (everything expressible in SQL).

        Joins each completed job to its owning project across all three
        job homes — scenario (ad-hoc *and* workflow-run children),
        parametric study, *or* optimisation study — the set-wise mirror
        of :func:`matofx.hierarchy.service._job_project_id`'s 1-of-3 CHECK
        resolution, done here with ``COALESCE``. Returns rows carrying
        ``(id, pid, inputs, tool_version_id, completed_at, source)`` for
        the Python filter + ACL pass and the builder preview.

        ``filt.target_project_ids = None`` searches the whole workspace
        (D5); otherwise it restricts to the named projects. Either way a
        job whose home doesn't resolve (``pid IS NULL`` — e.g. an archived
        parent) is excluded. Optimisation-study children carry
        ``scenario_id = NULL`` / ``parametric_study_id = NULL``; without
        the optimisation-study join their ``pid`` would resolve to NULL and
        they would be silently dropped.
        """
        from matofx.library.orm import JobLibraryFileUse  # noqa: PLC0415
        from matofx.materials.orm import JobMaterialUse  # noqa: PLC0415
        from matofx.studies.optimisation.orm import OptimisationStudy  # noqa: PLC0415

        sc = aliased(Scenario)
        ps = aliased(ParametricStudy)
        os_ = aliased(OptimisationStudy)
        pid = func.coalesce(sc.project_id, ps.project_id, os_.project_id)
        source = case(
            (Job.workflow_run_id.isnot(None), "workflow"),
            (Job.parametric_study_id.isnot(None), "parametric_study"),
            (Job.optimisation_study_id.isnot(None), "optimisation_study"),
            else_="scenario",
        ).label("source")
        # Which study, not just what kind. Both parents are already joined for
        # the project COALESCE, so naming them costs nothing extra.
        source_name = func.coalesce(ps.name, os_.name).label("source_name")
        stmt = (
            select(
                Job.id,
                pid.label("pid"),
                Job.inputs,
                Job.tool_version_id,
                Job.completed_at,
                source,
                source_name,
                Job.created_by_user_id,
                Job.parametric_study_id,
                Job.optimisation_study_id,
            )
            .outerjoin(sc, Job.scenario_id == sc.id)
            .outerjoin(ps, Job.parametric_study_id == ps.id)
            .outerjoin(os_, Job.optimisation_study_id == os_.id)
            .where(Job.tool_alias == filt.tool_alias)
            .where(Job.tool_version_id.in_(filt.tolerated_tool_version_ids))
            .where(Job.state == "complete")
        )
        if filt.target_project_ids is not None:
            stmt = stmt.where(pid.in_(filt.target_project_ids))
        else:
            stmt = stmt.where(pid.isnot(None))
        if filt.scenario_ids:
            stmt = stmt.where(Job.scenario_id.in_(filt.scenario_ids))
        if filt.owner_user_ids:
            stmt = stmt.where(Job.created_by_user_id.in_(filt.owner_user_ids))
        if filt.source_kinds:
            stmt = stmt.where(source.in_(list(filt.source_kinds)))
        # Narrowing to named studies is an OR *within* the study kinds, not an
        # AND across them: picking two parametric studies and one optimisation
        # means "runs from any of these three", which is how the builder reads.
        study_clauses = []
        if filt.parametric_study_ids:
            study_clauses.append(Job.parametric_study_id.in_(filt.parametric_study_ids))
        if filt.optimisation_study_ids:
            study_clauses.append(Job.optimisation_study_id.in_(filt.optimisation_study_ids))
        if study_clauses:
            stmt = stmt.where(or_(*study_clauses))
        if filt.completed_after is not None:
            stmt = stmt.where(Job.completed_at >= filt.completed_after)
        if filt.completed_before is not None:
            stmt = stmt.where(Job.completed_at <= filt.completed_before)
        if filt.library_file_version_ids:
            stmt = stmt.where(
                exists().where(
                    JobLibraryFileUse.job_id == Job.id,
                    JobLibraryFileUse.library_file_version_id.in_(filt.library_file_version_ids),
                )
            )
        if filt.material_version_ids:
            stmt = stmt.where(
                exists().where(
                    JobMaterialUse.job_id == Job.id,
                    JobMaterialUse.material_version_id.in_(filt.material_version_ids),
                )
            )
        return list((await session.execute(stmt)).all())

    async def _static_member_rows(self, session: AsyncSession, job_ids: list[int]) -> list[Any]:
        """Resolve ``(id, pid)`` for a fixed cohort's frozen ``member_job_ids``.

        Same 1-of-3 home → project resolution as :meth:`_candidate_rows`,
        but for an explicit job-id set (no filters). Preserves the curated
        order; a job whose home no longer resolves drops out silently.
        """
        if not job_ids:
            return []
        from matofx.studies.optimisation.orm import OptimisationStudy  # noqa: PLC0415

        sc = aliased(Scenario)
        ps = aliased(ParametricStudy)
        os_ = aliased(OptimisationStudy)
        pid = func.coalesce(sc.project_id, ps.project_id, os_.project_id)
        stmt = (
            select(Job.id, pid.label("pid"))
            .outerjoin(sc, Job.scenario_id == sc.id)
            .outerjoin(ps, Job.parametric_study_id == ps.id)
            .outerjoin(os_, Job.optimisation_study_id == os_.id)
            .where(Job.id.in_(job_ids))
            .where(pid.isnot(None))
        )
        by_id = {row.id: row for row in (await session.execute(stmt)).all()}
        return [by_id[jid] for jid in job_ids if jid in by_id]

    async def _commissioned_rows(
        self, session: AsyncSession, cohort: JobCohort, members: list[Any]
    ) -> list[Any]:
        """Completed runs of the studies this cohort commissioned (§11.5).

        **Resolution-time, not event-time.** No completion hook and no queue:
        a run joins the moment it is complete enough to be resolved, which is
        the rule ``auto_grow`` already works by. A hook would have had to fire
        reliably from the runner, survive a crash mid-batch and be idempotent;
        a union at read time is none of those and cannot drift out of step
        with what the jobs actually did.

        Applied to **both** modes. A fixed cohort would never take them
        otherwise, and an auto-grow one can reject them through its own
        filters -- the case where somebody runs the suggestions and watches
        the verdict not move, with nothing on screen to explain why.

        Its own query rather than a reuse of :meth:`_candidate_rows`, whose
        ``tool_version_id IN (...)`` clause matches nothing on an empty
        tolerance list: a commissioned run is included because *this cohort
        asked for it*, not because it happens to satisfy the version bands,
        and passing an empty list through that filter would silently return
        none of them.

        ``excluded_job_ids`` is honoured, and ``tool_alias`` is checked
        defensively -- the study is created for this cohort's tool, and a run
        of a different one has no business in the membership whatever a stale
        link says.
        """
        study_ids = list(cohort.auto_include_study_ids or [])
        if not study_ids:
            return []

        from matofx.studies.optimisation.orm import OptimisationStudy  # noqa: PLC0415

        sc = aliased(Scenario)
        ps = aliased(ParametricStudy)
        os_ = aliased(OptimisationStudy)
        pid = func.coalesce(sc.project_id, ps.project_id, os_.project_id)
        stmt = (
            select(Job.id, pid.label("pid"))
            .outerjoin(sc, Job.scenario_id == sc.id)
            .outerjoin(ps, Job.parametric_study_id == ps.id)
            .outerjoin(os_, Job.optimisation_study_id == os_.id)
            .where(Job.parametric_study_id.in_(study_ids))
            .where(Job.tool_alias == cohort.tool_alias)
            .where(Job.state == "complete")
            .where(pid.isnot(None))
        )
        excluded = set(cohort.excluded_job_ids or [])
        seen = {row.id for row in members}
        return [
            row
            for row in (await session.execute(stmt)).all()
            if row.id not in seen and row.id not in excluded
        ]

    async def _validate_member_jobs(
        self,
        session: AsyncSession,
        actor_user_id: int,
        *,
        tool_alias: str,
        tolerated_tool_version_ids: list[int],
        job_ids: list[int],
    ) -> list[int]:
        """Validate + de-dup a fixed cohort's picked runs; return them in order.

        Each must be a completed run of ``tool_alias`` at a tolerated
        version that the curator can see (ACL). Reuses the candidate
        universe so the rules match the live query exactly.
        """
        if not job_ids:
            raise CohortConfigError("Pick at least one run for a fixed cohort.")
        rows = await self._candidate_rows(
            session,
            _CandidateFilter(
                tool_alias=tool_alias,
                tolerated_tool_version_ids=tolerated_tool_version_ids,
                target_project_ids=None,
            ),
        )
        pid_by_job = {row.id: row.pid for row in rows}
        visible = await self._visible_project_ids(session, actor_user_id, set(pid_by_job.values()))
        ordered: list[int] = []
        seen: set[int] = set()
        for jid in job_ids:
            if jid in seen:
                continue
            seen.add(jid)
            if jid not in pid_by_job:
                raise CohortConfigError(
                    f"Run {jid} isn't a completed run of this tool at a tolerated version."
                )
            if pid_by_job[jid] not in visible:
                raise CohortConfigError(f"Run {jid} is in a project you can't access.")
            ordered.append(jid)
        return ordered

    def _inputs_pass(
        self,
        input_ranges: dict[str, Any] | None,
        cross_field_predicate: str | None,
        inputs: dict[str, Any],
    ) -> bool:
        """Apply ``input_ranges`` + ``cross_field_predicate`` to one job's inputs."""
        ranges = input_ranges or {}
        for factor, bound in ranges.items():
            if not isinstance(bound, dict):
                continue
            value = inputs.get(factor)
            if not isinstance(value, int | float):
                return False
            lo = bound.get("min")
            hi = bound.get("max")
            if lo is not None and value < lo:
                return False
            if hi is not None and value > hi:
                return False
        if cross_field_predicate:
            from matofx.tools.forms import safe_expr  # noqa: PLC0415

            try:
                return bool(safe_expr.evaluate_condition(cross_field_predicate, inputs))
            except safe_expr.UnsafeExpressionError:
                # A predicate that referenced a missing input for this
                # job is "not a member", not an error.
                return False
        return True

    async def _scalars_for_job(
        self, session: AsyncSession, job_id: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """``(scalars, retained_files)`` for a member job (§3.3).

        Prefers an existing ``study_result`` row for the job (parametric
        children already have one — deterministic, no disk hit); falls
        back to parsing the job's ``outputs.json`` on the retained floor.
        """
        existing = (
            await session.execute(select(StudyResult).where(StudyResult.job_id == job_id).limit(1))
        ).scalar_one_or_none()
        if existing is not None:
            return dict(existing.scalars or {}), list(existing.retained_files or [])
        return await self._scalars_from_disk(session, job_id)

    async def _scalars_from_disk(
        self, session: AsyncSession, job_id: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Parse a job's retained ``outputs.json`` → scalars + file refs.

        Returns empty results on any miss (missing working dir, reaped
        file, unparseable document) — a member with no readable outputs
        appears in the matrix with a null response (D3), never an error.
        """
        import asyncio  # noqa: PLC0415
        import json  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        from matofx.studies.results_service import (  # noqa: PLC0415
            _extract_retained_files,
            _extract_scalars,
        )
        from matofx.tools.exec.contracts import OutputsDocument  # noqa: PLC0415
        from matofx.tools.exec_orm import StorageLocation  # noqa: PLC0415
        from matofx.tools.orm import Tool  # noqa: PLC0415

        job = await session.get(Job, job_id)
        if job is None or job.storage_location_id is None or job.working_dir_relative is None:
            return {}, []
        location = await session.get(StorageLocation, job.storage_location_id)
        if location is None:
            return {}, []
        path = Path(location.mount_path) / job.working_dir_relative / "outputs" / "outputs.json"

        def _load() -> Any:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

        payload = await asyncio.to_thread(_load)
        if payload is None:
            return {}, []
        try:
            doc = OutputsDocument.model_validate(payload)
        except ValueError:
            return {}, []
        # **By alias, not by primary key.** ``Tool.id`` is the PK and ``alias``
        # is a separate unique column, so ``session.get(Tool, job.tool_alias)``
        # asked for the row whose *id* is the string "finned_plate" and got
        # ``None`` — silently, because the ``is not None`` guard below reads as
        # "this job's tool was uninstalled" rather than "the lookup was wrong".
        # Scalars need no tool and kept working, so the only visible symptom was
        # a field surrogate reporting "0 usable runs with a retained snapshot"
        # on a cohort whose .vtu files were all sitting on disk.
        tool = (
            await session.execute(select(Tool).where(Tool.alias == job.tool_alias))
        ).scalar_one_or_none()
        retained = _extract_retained_files(doc, tool) if tool is not None else []
        return _extract_scalars(doc), retained

    async def _visible_project_ids(
        self, session: AsyncSession, actor_user_id: int, project_ids: set[int]
    ) -> set[int]:
        """The subset of ``project_ids`` the caller can see (Viewer+)."""
        if not project_ids:
            return set()
        projects = (
            (await session.execute(select(Project).where(Project.id.in_(project_ids))))
            .scalars()
            .all()
        )
        visible: set[int] = set()
        for project in projects:
            level = await _resolve_access(session, project, actor_user_id)
            if level != ProjectAccessLevel.NONE:
                visible.add(project.id)
        return visible

    async def _latest_tool_version_id(self, session: AsyncSession, tool_alias: str) -> int:
        """The newest ``tool_version.id`` for ``tool_alias`` (creation default §3.2)."""
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        vid = (
            await session.execute(
                select(ToolVersion.id)
                .where(ToolVersion.alias == tool_alias.strip())
                .order_by(ToolVersion.created_at.desc(), ToolVersion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if vid is None:
            raise CohortConfigError(
                f"'{tool_alias}' has no installed versions to anchor a cohort on."
            )
        return int(vid)

    async def _validate_tolerated_versions(
        self, session: AsyncSession, tool_alias: str, version_ids: list[int]
    ) -> None:
        """Non-empty (§3.2) and every version belongs to ``tool_alias``."""
        if not version_ids:
            raise CohortConfigError(
                "A cohort must accept at least one tool version. Pick the "
                "version its runs were produced with."
            )
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        found = (
            await session.execute(
                select(ToolVersion.id, ToolVersion.alias).where(ToolVersion.id.in_(version_ids))
            )
        ).all()
        found_by_id: dict[int, str] = {int(vid): str(alias) for vid, alias in found}
        for vid in version_ids:
            alias = found_by_id.get(vid)
            if alias is None:
                raise CohortConfigError(f"Tool version {vid} doesn't exist.")
            if alias != tool_alias.strip():
                raise CohortConfigError(
                    f"Tool version {vid} belongs to '{alias}', not '{tool_alias}'. "
                    "A cohort is keyed on a single tool."
                )

    async def _validate_filters(
        self, session: AsyncSession, actor_user_id: int, filters: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate the optional filter set; return the columns to persist.

        Cross-project ``project_ids`` requires Viewer+ on each referenced
        project (surrogate §14); ``input_ranges`` bounds must be sane;
        ``cross_field_predicate`` must compile under ``safe_expr``.
        """
        clean: dict[str, Any] = {}
        for key, value in filters.items():
            if key not in _FILTER_FIELDS:
                raise CohortConfigError(f"Unknown cohort filter '{key}'.")
            clean[key] = value

        _validate_source_kinds(clean.get("source_kinds"))
        _validate_study_ids(clean.get("auto_include_study_ids"), "auto_include_study_ids")

        project_ids = clean.get("project_ids")
        if project_ids:
            for pid in project_ids:
                await _require_project_access(
                    session,
                    project_id=pid,
                    actor_user_id=actor_user_id,
                    required=ProjectAccessLevel.VIEW,
                )

        ranges = clean.get("input_ranges")
        if ranges is not None:
            if not isinstance(ranges, dict):
                raise CohortConfigError("input_ranges must be a {factor: {min, max}} map.")
            for factor, bound in ranges.items():
                if not isinstance(bound, dict):
                    raise CohortConfigError(f"input_ranges['{factor}'] must be a {{min, max}} map.")
                lo, hi = bound.get("min"), bound.get("max")
                if lo is not None and hi is not None and hi < lo:
                    raise CohortConfigError(
                        f"input_ranges['{factor}'] has max < min ({hi} < {lo})."
                    )

        predicate = clean.get("cross_field_predicate")
        if predicate:
            from matofx.tools.forms import safe_expr  # noqa: PLC0415

            try:
                safe_expr.parse(predicate)
            except safe_expr.UnsafeExpressionError as exc:
                raise CohortConfigError(f"The cross-field filter won't compile: {exc}") from exc
        return clean

    async def _descriptor_inputs(
        self, session: AsyncSession, tool_alias: str, version_id: int
    ) -> dict[str, tuple[str, str]]:
        """``{input_name: (type, unit)}`` from a tool version's descriptor."""
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        tool_version = await session.get(ToolVersion, version_id)
        if tool_version is None or tool_version.alias != tool_alias.strip():
            raise NotFoundError("tool version", version_id)
        snapshot = tool_version.descriptor_snapshot or {}
        inputs = snapshot.get("inputs", []) if isinstance(snapshot, dict) else []
        out: dict[str, tuple[str, str]] = {}
        if isinstance(inputs, list):
            for entry in inputs:
                if isinstance(entry, dict) and entry.get("name"):
                    out[str(entry["name"])] = (
                        str(entry.get("type") or ""),
                        str(entry.get("unit") or ""),
                    )
        return out

    async def _get(self, session: AsyncSession, cohort_id: int) -> JobCohort:
        """Fetch a cohort or raise ``NotFoundError`` (open read, D6)."""
        cohort = await session.get(JobCohort, cohort_id)
        if cohort is None:
            raise NotFoundError("cohort", cohort_id)
        return cohort

    async def _require_create(self, session: AsyncSession, actor_user_id: int) -> None:
        """Gate cohort creation on ``cohorts.create`` (or the curate capability).

        Creation is a base-tier action: a ``cohorts.create`` holder may
        create cohorts, as may a ``cohorts.curate`` curator (who can always
        create). Editing and archiving stay curate-only via
        :meth:`_require_curate`.
        """
        if not await user_has_any_permission(
            session, actor_user_id, Permission.COHORTS_CREATE, Permission.COHORTS_CURATE
        ):
            raise AccessDeniedError

    async def _require_curate(self, session: AsyncSession, actor_user_id: int) -> None:
        """Gate a governance write (edit / archive) on the ``cohorts.curate`` capability (D6)."""
        if not await user_has_permission(session, actor_user_id, Permission.COHORTS_CURATE):
            raise AccessDeniedError

    async def _audit(
        self,
        session: AsyncSession,
        cohort: JobCohort,
        action: str,
        actor_user_id: int,
        *,
        payload: dict[str, Any],
    ) -> None:
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=action,
                resource_type="job_cohort",
                resource_id=str(cohort.id),
                outcome="success",
                payload=payload,
            ),
        )


def _version_components(version: str) -> tuple[int, ...]:
    """Best-effort numeric components of a version string (``"1.2.3" → (1,2,3)``)."""
    parts: list[int] = []
    for token in version.strip().split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _major_minor(version: str) -> str:
    """``major.minor`` band for a version (patches ignored, §3.2).

    Pads a single-component version to ``X.0``. A version with no numeric
    component falls back to its raw string so it still groups sensibly.
    """
    comps = _version_components(version)
    if not any(comps):
        return version.strip()
    major, minor, *_ = (*comps, 0, 0)
    return f"{major}.{minor}"


def _version_sort_key(version: str) -> tuple[int, ...]:
    """Sortable key for version / band strings (newest = largest)."""
    return _version_components(version)


def _normalise_cohort_scalars(scalars: dict[str, Any], tool_alias: str) -> dict[str, Any]:
    """Re-key a member's scalars to the cohort tool's bare output names.

    A cohort is keyed on **one** tool, but member runs come from sources
    that key ``study_result.scalars`` differently. Parametric / standalone
    runs use the bare output name (``heat_dissipation``); an optimisation
    *workflow* run aggregates every member keyed ``<member_tool>.<name>``
    (``heat_sink.heat_dissipation`` — runtime ``_record_run_result``). Left
    as-is, a cohort mixing both shows the same output twice, and a
    surrogate fitted on it sees two columns for one variable.

    This collapses them: the cohort tool's own outputs (bare, or prefixed
    with the cohort ``tool_alias``) keep the bare name; another workflow
    member's outputs (a *different* ``<tool>.`` prefix) are dropped — they
    belong to a different tool, not this single-tool cohort's response
    surface. On a collision the first non-null value wins. Idempotent: an
    already-bare dict is returned unchanged (key order aside).
    """
    prefix = f"{tool_alias}."
    out: dict[str, Any] = {}
    for key, value in scalars.items():
        if key.startswith(prefix):
            bare = key[len(prefix) :]
        elif "." in key:
            continue  # another workflow member's output — not this tool's
        else:
            bare = key
        if not bare:
            continue
        if bare not in out or (out[bare] is None and value is not None):
            out[bare] = value
    return out


def _meta_keys(name: str | None, description: str | None) -> set[str]:
    """The metadata field names touched by an update (for the audit diff)."""
    keys: set[str] = set()
    if name is not None:
        keys.add("name")
    if description is not None:
        keys.add("description")
    return keys


def _diverging_input_warnings(
    anchor: dict[str, tuple[str, str]], candidate: dict[str, tuple[str, str]]
) -> list[str]:
    """Compare two descriptors' inputs; return plain-English divergences (§3.2)."""
    warnings: list[str] = []
    missing = sorted(set(anchor) - set(candidate))
    added = sorted(set(candidate) - set(anchor))
    if missing:
        warnings.append(
            "The added version is missing input(s) the cohort already uses: "
            + ", ".join(missing)
            + "."
        )
    if added:
        warnings.append(
            "The added version introduces new input(s) not in the cohort: " + ", ".join(added) + "."
        )
    for name in sorted(set(anchor) & set(candidate)):
        a_type, a_unit = anchor[name]
        c_type, c_unit = candidate[name]
        if a_type != c_type:
            warnings.append(
                f"Input '{name}' changed type ({a_type or '?'} → {c_type or '?'}); "
                "mixing these will corrupt a surrogate fit."
            )
        if a_unit != c_unit:
            warnings.append(
                f"Input '{name}' changed unit ({a_unit or '?'} → {c_unit or '?'}); "
                "mixing these will corrupt a surrogate fit."
            )
    return warnings


def make_cohort_service(audit: AuditWriter) -> CohortService:
    """Wire up a :class:`CohortService` over the shared audit writer."""
    return CohortService(audit)
