"""Read + bulk-cancel service behind the Live Jobs tab.

The Live Jobs tab on ``Admin → Usage & Statistics`` shows an install-wide
snapshot of every in-flight item across the three kinds — standalone
jobs, workflow runs, and parametric studies — that are queued, running,
or waiting at an approval gate, regardless of project access. An operator
multi-selects rows and cancels them.

Gating lives at the router: ``live_jobs.view`` for the read, and
``live_jobs.cancel`` for the bulk cancel. Cancellation reuses the
existing per-item cancel flows
(:meth:`~matofx.hierarchy.service.JobService.request_cancel`,
:meth:`~matofx.workflows.run_service.WorkflowRunService.cancel_run`,
:meth:`~matofx.studies.service.StudyService.cancel`) with
``bypass_access=True`` — identical SIGTERM → SIGKILL runner mechanics,
just driven in bulk by an operator who may hold no project ACL.

Cancel eligibility mirrors what those flows can actually act on:

- A standalone job is cancellable only in ``queued`` / ``running`` (the
  runner's cancel loop ignores other states). Jobs paused at an approval
  gate (``pending_approval`` / ``pending_results``) are *shown* for
  visibility but resolved on the Approvals page, not here.
- A workflow run is cancellable whenever it is in-flight (the run-level
  cancel marks it cancelled and cascades to its queued/running children).
- A parametric study is cancellable in ``running`` / ``pending_approval``
  (its cancel transitions the stored state and cascades to children).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select

from matofx.audit.schema import RecordInput
from matofx.hierarchy.orm import Job, Project, Scenario
from matofx.hierarchy.service import JobService, make_job_service
from matofx.studies.optimisation.orm import OptimisationStudy
from matofx.studies.orm import ParametricStudy
from matofx.studies.service import StudyService, make_study_service
from matofx.tools.orm import Tool
from matofx.users.orm import User
from matofx.workflows.orm import Workflow, WorkflowRun
from matofx.workflows.run_service import WorkflowRunService, make_workflow_run_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter

LiveKind = Literal["job", "workflow", "study", "optimisation_study"]

INFLIGHT_JOB_STATES: tuple[str, ...] = (
    "queued",
    "running",
    "pending_approval",
    "pending_results",
)
"""Job states treated as in-flight (shown in the Live Jobs list)."""

_JOB_CANCELLABLE_STATES: frozenset[str] = frozenset({"queued", "running"})
"""The job states the runner's cancel loop can actually act on."""

INFLIGHT_STUDY_STATES: tuple[str, ...] = ("running", "pending_approval")
INFLIGHT_OPTIMISATION_STATES: tuple[str, ...] = ("running", "pending_start_approval")
"""Optimisation-study states treated as in-flight in the Live Jobs list (ADR-0009)."""
"""Parametric-study states treated as in-flight (and cancellable here)."""


def _ensure_aware(value: _dt.datetime) -> _dt.datetime:
    """Coerce a possibly-naive datetime to UTC-aware (SQLite returns naive)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=_dt.UTC)
    return value


def _within_window(
    value: _dt.datetime, since: _dt.datetime | None, until: _dt.datetime | None
) -> bool:
    """Return ``True`` when ``value`` falls within the (optional) inclusive window."""
    moment = _ensure_aware(value)
    if since is not None and moment < _ensure_aware(since):
        return False
    return not (until is not None and moment > _ensure_aware(until))


def _run_display_state(child_states: set[str]) -> str:
    """Summarise a workflow run's in-flight children into one display label."""
    if "running" in child_states:
        return "running"
    if "queued" in child_states:
        return "queued"
    if child_states & {"pending_approval", "pending_results"}:
        return "awaiting approval"
    return "running"


@dataclass(frozen=True)
class LiveItem:
    """One in-flight row in the Live Jobs list."""

    kind: LiveKind
    id: int
    name: str
    state: str
    submitter_user_id: int | None
    submitter_name: str
    submitted_at: _dt.datetime
    project_name: str | None
    scenario_name: str | None
    child_count: int | None
    cancellable: bool


@dataclass(frozen=True)
class LiveSubmitter:
    """A distinct submitter present in the current in-flight set (a filter option)."""

    user_id: int
    name: str


@dataclass(frozen=True)
class LiveJobsView:
    """The Live Jobs read result: the rows plus the submitter filter options."""

    items: list[LiveItem] = field(default_factory=list)
    submitters: list[LiveSubmitter] = field(default_factory=list)


@dataclass(frozen=True)
class CancelOutcome:
    """The result of attempting to cancel one item."""

    kind: LiveKind
    id: int
    cancelled: bool
    reason: str = ""


@dataclass(frozen=True)
class CancelResult:
    """Aggregate result of a bulk cancel — one :class:`CancelOutcome` per item."""

    outcomes: list[CancelOutcome] = field(default_factory=list)

    @property
    def cancelled_count(self) -> int:
        """How many items were successfully marked for cancellation."""
        return sum(1 for o in self.outcomes if o.cancelled)

    @property
    def failures(self) -> list[CancelOutcome]:
        """The outcomes that didn't cancel, each with a human-readable reason."""
        return [o for o in self.outcomes if not o.cancelled]


def _distinct_submitters(items: list[LiveItem]) -> list[LiveSubmitter]:
    """Return the unique submitters present in ``items``, sorted by name."""
    seen: dict[int, str] = {}
    for item in items:
        if item.submitter_user_id is not None and item.submitter_user_id not in seen:
            seen[item.submitter_user_id] = item.submitter_name
    return sorted(
        (LiveSubmitter(user_id=uid, name=name) for uid, name in seen.items()),
        key=lambda s: s.name.lower(),
    )


@dataclass(frozen=True)
class LiveJobsService:
    """Install-wide in-flight listing + bulk cancel for the Live Jobs tab."""

    audit: AuditWriter

    # --- reads -----------------------------------------------------------

    async def list_inflight(
        self,
        session: AsyncSession,
        *,
        kinds: tuple[str, ...] = (),
        submitter_user_ids: tuple[int, ...] = (),
        since: _dt.datetime | None = None,
        until: _dt.datetime | None = None,
    ) -> LiveJobsView:
        """Return every in-flight item across the install for the window.

        ``kinds`` (empty ⇒ all) and ``submitter_user_ids`` (empty ⇒ all)
        filter the returned rows; ``since`` / ``until`` bound the
        submitted timestamp. The ``submitters`` filter options are
        computed from the window-filtered set *before* the kind /
        submitter filters apply, so the dropdown stays stable as
        selections change.
        """
        windowed = await self._build_items(session, since=since, until=until)
        submitters = _distinct_submitters(windowed)
        items = [
            item
            for item in windowed
            if (not kinds or item.kind in kinds)
            and (not submitter_user_ids or item.submitter_user_id in submitter_user_ids)
        ]
        items.sort(key=lambda i: _ensure_aware(i.submitted_at), reverse=True)
        return LiveJobsView(items=items, submitters=submitters)

    async def _build_items(
        self,
        session: AsyncSession,
        *,
        since: _dt.datetime | None,
        until: _dt.datetime | None,
    ) -> list[LiveItem]:
        """Assemble the window-only (unfiltered-by-kind/submitter) item list."""
        jobs = list(
            (await session.execute(select(Job).where(Job.state.in_(INFLIGHT_JOB_STATES)))).scalars()
        )
        studies = list(
            (
                await session.execute(
                    select(ParametricStudy).where(ParametricStudy.state.in_(INFLIGHT_STUDY_STATES))
                )
            ).scalars()
        )
        opt_studies = list(
            (
                await session.execute(
                    select(OptimisationStudy).where(
                        OptimisationStudy.state.in_(INFLIGHT_OPTIMISATION_STATES),
                        OptimisationStudy.archived_at.is_(None),
                    )
                )
            ).scalars()
        )

        standalone = [
            j
            for j in jobs
            if j.workflow_run_id is None
            and j.parametric_study_id is None
            and j.optimisation_study_id is None
        ]

        # Group in-flight children by their parent run / study (a tool-target
        # optimisation child is homed on optimisation_study_id; workflow-target
        # children live under their WorkflowRun, already grouped above).
        children_by_run: dict[int, list[Job]] = {}
        children_by_study: dict[int, list[Job]] = {}
        children_by_opt_study: dict[int, list[Job]] = {}
        for j in jobs:
            if j.workflow_run_id is not None:
                children_by_run.setdefault(j.workflow_run_id, []).append(j)
            elif j.parametric_study_id is not None:
                children_by_study.setdefault(j.parametric_study_id, []).append(j)
            elif j.optimisation_study_id is not None:
                children_by_opt_study.setdefault(j.optimisation_study_id, []).append(j)

        runs: list[WorkflowRun] = []
        if children_by_run:
            run_rows = (
                await session.execute(
                    select(WorkflowRun).where(WorkflowRun.id.in_(children_by_run.keys()))
                )
            ).scalars()
            runs = [r for r in run_rows if r.cancel_requested_at is None]

        # Batch-resolve display labels in one round-trip each.
        tool_names = await self._tool_names(
            session, {j.tool_alias for j in standalone} | {s.tool_alias for s in studies}
        )
        workflow_names = await self._workflow_names(session, {r.workflow_id for r in runs})
        scenario_info = await self._scenario_info(
            session,
            {j.scenario_id for j in standalone if j.scenario_id is not None}
            | {r.scenario_id for r in runs},
        )
        project_ids = (
            {pid for (_name, pid) in scenario_info.values()}
            | {s.project_id for s in studies}
            | {o.project_id for o in opt_studies}
        )
        project_names = await self._project_names(session, project_ids)
        user_names = await self._user_names(
            session,
            {j.created_by_user_id for j in standalone}
            | {r.submitted_by_user_id for r in runs}
            | {s.created_by_user_id for s in studies}
            | {o.created_by_user_id for o in opt_studies},
        )

        items: list[LiveItem] = []
        for j in standalone:
            scen = scenario_info.get(j.scenario_id) if j.scenario_id is not None else None
            items.append(
                LiveItem(
                    kind="job",
                    id=j.id,
                    name=tool_names.get(j.tool_alias, j.tool_alias),
                    state=j.state,
                    submitter_user_id=j.created_by_user_id,
                    submitter_name=user_names.get(j.created_by_user_id, ""),
                    submitted_at=j.created_at,
                    project_name=project_names.get(scen[1]) if scen else None,
                    scenario_name=scen[0] if scen else None,
                    child_count=None,
                    cancellable=j.state in _JOB_CANCELLABLE_STATES,
                )
            )
        for r in runs:
            children = children_by_run.get(r.id, [])
            scen = scenario_info.get(r.scenario_id)
            items.append(
                LiveItem(
                    kind="workflow",
                    id=r.id,
                    name=workflow_names.get(r.workflow_id, r.name),
                    state=_run_display_state({c.state for c in children}),
                    submitter_user_id=r.submitted_by_user_id,
                    submitter_name=user_names.get(r.submitted_by_user_id, ""),
                    submitted_at=r.created_at,
                    project_name=project_names.get(scen[1]) if scen else None,
                    scenario_name=scen[0] if scen else None,
                    child_count=len(children),
                    cancellable=True,
                )
            )
        for s in studies:
            items.append(
                LiveItem(
                    kind="study",
                    id=s.id,
                    name=s.name,
                    state=s.state,
                    submitter_user_id=s.created_by_user_id,
                    submitter_name=user_names.get(s.created_by_user_id, ""),
                    submitted_at=s.created_at,
                    project_name=project_names.get(s.project_id),
                    scenario_name=None,
                    child_count=len(children_by_study.get(s.id, [])),
                    cancellable=s.state in INFLIGHT_STUDY_STATES,
                )
            )
        for o in opt_studies:
            # The campaign is summarised as one item (its in-flight children
            # grouped). Cancellation is the per-study Halt control on the
            # optimisation detail page, not a live-jobs action (bulk-halt
            # from here is a later slice), so it isn't cancellable here.
            items.append(
                LiveItem(
                    kind="optimisation_study",
                    id=o.id,
                    name=o.name,
                    state=o.state,
                    submitter_user_id=o.created_by_user_id,
                    submitter_name=user_names.get(o.created_by_user_id, ""),
                    submitted_at=o.created_at,
                    project_name=project_names.get(o.project_id),
                    scenario_name=None,
                    child_count=len(children_by_opt_study.get(o.id, [])),
                    cancellable=False,
                )
            )

        return [item for item in items if _within_window(item.submitted_at, since, until)]

    async def _tool_names(self, session: AsyncSession, aliases: set[str]) -> dict[str, str]:
        """Map tool alias → display name for the given aliases."""
        if not aliases:
            return {}
        rows = (
            await session.execute(select(Tool.alias, Tool.name).where(Tool.alias.in_(aliases)))
        ).all()
        return {row[0]: row[1] for row in rows}

    async def _workflow_names(self, session: AsyncSession, ids: set[int]) -> dict[int, str]:
        """Map workflow id → display name for the given workflow ids."""
        if not ids:
            return {}
        rows = (
            await session.execute(select(Workflow.id, Workflow.name).where(Workflow.id.in_(ids)))
        ).all()
        return {row[0]: row[1] for row in rows}

    async def _scenario_info(
        self, session: AsyncSession, ids: set[int]
    ) -> dict[int, tuple[str, int]]:
        """Map scenario id → ``(name, project_id)`` for the given scenario ids."""
        if not ids:
            return {}
        rows = (
            await session.execute(
                select(Scenario.id, Scenario.name, Scenario.project_id).where(Scenario.id.in_(ids))
            )
        ).all()
        return {row[0]: (row[1], row[2]) for row in rows}

    async def _project_names(self, session: AsyncSession, ids: set[int]) -> dict[int, str]:
        """Map project id → name for the given project ids."""
        if not ids:
            return {}
        rows = (
            await session.execute(select(Project.id, Project.name).where(Project.id.in_(ids)))
        ).all()
        return {row[0]: row[1] for row in rows}

    async def _user_names(self, session: AsyncSession, ids: set[int]) -> dict[int, str]:
        """Map user id → display name (``full_name`` else ``username``)."""
        if not ids:
            return {}
        rows = (
            await session.execute(
                select(User.id, User.full_name, User.username).where(User.id.in_(ids))
            )
        ).all()
        return {row[0]: (row[1] or row[2]) for row in rows}

    # --- mutations -------------------------------------------------------

    async def cancel_many(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        job_ids: tuple[int, ...] = (),
        run_ids: tuple[int, ...] = (),
        study_ids: tuple[int, ...] = (),
    ) -> CancelResult:
        """Cancel the given items in bulk (access already gated by the router).

        Each item is attempted independently — a failure on one never
        aborts the rest. An item the underlying flow can't act on (a job
        at an approval gate, an already-terminal row) comes back in
        :attr:`CancelResult.failures` with a plain-English reason rather
        than silently no-op'ing. Reuses the per-item cancel flows with
        ``bypass_access=True``; a single summary audit row is written
        alongside each flow's own per-item audit.
        """
        job_service = make_job_service(self.audit)
        run_service = make_workflow_run_service(self.audit)
        study_service = make_study_service(self.audit)

        outcomes: list[CancelOutcome] = []
        for jid in job_ids:
            outcomes.append(await self._cancel_job(session, job_service, actor_user_id, jid))
        for rid in run_ids:
            outcomes.append(await self._cancel_run(session, run_service, actor_user_id, rid))
        for sid in study_ids:
            outcomes.append(await self._cancel_study(session, study_service, actor_user_id, sid))

        result = CancelResult(outcomes=outcomes)
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="live_jobs.bulk_cancel",
                resource_type="live_jobs",
                resource_id="bulk",
                outcome="success",
                payload={
                    "requested": len(outcomes),
                    "cancelled": result.cancelled_count,
                    "failed": len(result.failures),
                },
            ),
        )
        return result

    async def _cancel_job(
        self,
        session: AsyncSession,
        job_service: JobService,
        actor_user_id: int,
        job_id: int,
    ) -> CancelOutcome:
        """Cancel one standalone job (queued / running only)."""
        job = await session.get(Job, job_id)
        if job is None:
            return CancelOutcome("job", job_id, cancelled=False, reason="Job not found.")
        if job.state not in _JOB_CANCELLABLE_STATES:
            return CancelOutcome(
                "job",
                job_id,
                cancelled=False,
                reason=(
                    f"Not cancellable from here (state {job.state!r}). A job waiting on an "
                    "approval gate is resolved on the Approvals page."
                ),
            )
        try:
            await job_service.request_cancel(
                session, job_id=job_id, actor_user_id=actor_user_id, bypass_access=True
            )
        except Exception as exc:  # isolate this item's failure so the batch continues
            return CancelOutcome("job", job_id, cancelled=False, reason=str(exc))
        return CancelOutcome("job", job_id, cancelled=True)

    async def _cancel_run(
        self,
        session: AsyncSession,
        run_service: WorkflowRunService,
        actor_user_id: int,
        run_id: int,
    ) -> CancelOutcome:
        """Cancel one workflow run (cascades to its in-flight children)."""
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            return CancelOutcome(
                "workflow", run_id, cancelled=False, reason="Workflow run not found."
            )
        if run.cancel_requested_at is not None:
            return CancelOutcome("workflow", run_id, cancelled=True)
        children = list(
            (await session.execute(select(Job).where(Job.workflow_run_id == run_id))).scalars()
        )
        if not any(c.state in INFLIGHT_JOB_STATES for c in children):
            return CancelOutcome(
                "workflow", run_id, cancelled=False, reason="Workflow run is not in-flight."
            )
        try:
            await run_service.cancel_run(
                session, actor_user_id=actor_user_id, run_id=run_id, bypass_access=True
            )
        except Exception as exc:  # isolate this item's failure so the batch continues
            return CancelOutcome("workflow", run_id, cancelled=False, reason=str(exc))
        return CancelOutcome("workflow", run_id, cancelled=True)

    async def _cancel_study(
        self,
        session: AsyncSession,
        study_service: StudyService,
        actor_user_id: int,
        study_id: int,
    ) -> CancelOutcome:
        """Cancel one parametric study (running / pending-approval only)."""
        study = await session.get(ParametricStudy, study_id)
        if study is None:
            return CancelOutcome("study", study_id, cancelled=False, reason="Study not found.")
        if study.state not in INFLIGHT_STUDY_STATES:
            return CancelOutcome(
                "study",
                study_id,
                cancelled=False,
                reason=f"Not cancellable from here (state {study.state!r}).",
            )
        try:
            await study_service.cancel(
                session, actor_user_id=actor_user_id, study_id=study_id, bypass_access=True
            )
        except Exception as exc:  # isolate this item's failure so the batch continues
            return CancelOutcome("study", study_id, cancelled=False, reason=str(exc))
        return CancelOutcome("study", study_id, cancelled=True)


def make_live_jobs_service(audit: AuditWriter) -> LiveJobsService:
    """Build a :class:`LiveJobsService` from the ambient audit writer."""
    return LiveJobsService(audit=audit)
