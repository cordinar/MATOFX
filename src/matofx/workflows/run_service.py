"""WorkflowRunService — start / get / cancel runs + derive state.

A run is one execution of a workflow inside a scenario. The service:

- :meth:`start_run` — creates the run row and N child Job rows (one
  per workflow member), all in ``created`` state with empty inputs.
- :meth:`get_run` — fetches a run with its child jobs, member metadata,
  and computed run-level state.
- :meth:`cancel_run` — sets ``cancel_requested_at`` on the run and on
  every still-running child; the runner handles the actual signal.
- :meth:`compute_state` — derived rollup ("draft" / "running" /
  "complete" / "failed" / "cancelled") based on the child jobs.

Per-tool job submission stays on :class:`matofx.hierarchy.service.JobService`
— each tab's *Submit* button calls the standard ``/api/jobs/{id}/queue``
endpoint. The runtime just decides *when* a tab is submittable
(upstream complete) and what values to pre-fill (from upstream
outputs).
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from matofx.audit.schema import RecordInput
from matofx.hierarchy.errors import (
    AccessDeniedError,
    NotFoundError,
)
from matofx.hierarchy.orm import Job, ProjectAccessLevel, Scenario
from matofx.hierarchy.service import _require_project_access, make_job_service
from matofx.tools.orm import Tool, ToolVersion
from matofx.workflows.errors import (
    WorkflowArchivedError,
    WorkflowNotFoundError,
    WorkflowStateError,
)
from matofx.workflows.orm import Workflow, WorkflowMember, WorkflowRun
from matofx.workflows.wiring import ready_member_inputs, wired_inputs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


# ---------- derived state ------------------------------------------------


WorkflowRunState = str
"""``'draft'`` | ``'running'`` | ``'complete'`` | ``'failed'`` | ``'cancelled'``.

Not stored on the row — computed from the child :class:`Job` rows at
read time. See :func:`compute_state`."""


def compute_state(run: WorkflowRun, child_jobs: list[Job]) -> WorkflowRunState:
    """Roll the child jobs' states up into one workflow-run-level state.

    Rules (in order):

    1. ``cancel_requested_at`` set → ``"cancelled"`` (sticky once requested).
    2. Any child in ``"failed"`` → ``"failed"`` (sticky — the user has
       to manually clear the failure by re-submitting the failed tool).
    3. All children in ``"complete"`` → ``"complete"``.
    4. Any child in ``"queued"`` or ``"running"`` → ``"running"``.
    5. All children still in ``"created"`` → ``"draft"`` (the user
       hasn't submitted anything yet).
    """
    if run.cancel_requested_at is not None:
        return "cancelled"
    if not child_jobs:
        return "draft"
    states = {j.state for j in child_jobs}
    if "failed" in states:
        return "failed"
    if states == {"complete"}:
        return "complete"
    if "queued" in states or "running" in states:
        return "running"
    return "draft"


# ---------- completed-member outputs -------------------------------------


@dataclass(frozen=True)
class _MemberOutputs:
    """A completed member's outputs, split for the two consumers (ADR-0015).

    - :attr:`values` — ``{output_name: value}`` fed to the pure wiring
      planner (:func:`resolve_wired_values`). Scalars carry their literal
      value; file outputs carry the **primary** artefact's absolute path.
    - :attr:`file_sources` — ``{output_name: [absolute source paths]}`` for
      file outputs only: the file(s) the staging copy must move into the
      downstream working dir (one for a single file, the whole series for a
      ``field_timesteps`` output).
    """

    values: dict[str, Any] = field(default_factory=dict)
    file_sources: dict[str, list[str]] = field(default_factory=dict)


# ---------- the service --------------------------------------------------


@dataclass(frozen=True)
class WorkflowRunService:
    """Lifecycle for one execution of a workflow inside a scenario."""

    audit: AuditWriter
    environment: str | None = None
    """Staging environment passed to the :class:`JobService` when the
    headless orchestrator (:meth:`advance`) queues a child. ``None`` for
    the read/manual paths, which never queue."""

    # --- mutations -------------------------------------------------------

    async def start_run(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        scenario_id: int,
        workflow_alias: str,
        name: str | None = None,
    ) -> WorkflowRun:
        """Create a workflow run + N draft child jobs in one transaction.

        EDIT permission on the scenario's project required (same gate
        as a direct job submission). Refuses on:

        - Unknown scenario / workflow alias (404).
        - Archived workflow.
        - Duplicate run: a workflow run for the same workflow already
          exists on the scenario (the DB's unique constraint catches
          the race; the pre-flight lookup gives a friendly error
          first).
        """
        scenario = await session.get(Scenario, scenario_id)
        if scenario is None:
            raise NotFoundError("scenario", scenario_id)
        await _require_project_access(
            session,
            project_id=scenario.project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.EDIT,
            resource_type="scenario",
            resource_id=scenario_id,
        )

        wf_stmt = select(Workflow).where(Workflow.alias == workflow_alias)
        wf = (await session.execute(wf_stmt)).scalar_one_or_none()
        if wf is None:
            raise WorkflowNotFoundError(f"No workflow called {workflow_alias!r}.")
        if wf.archived_at is not None:
            raise WorkflowArchivedError(wf.alias)
        # Visibility gate: a workflow the caller can't see — including one
        # whose member cascade exposes a tool they can't see directly —
        # is existence-hidden, matching the rest of the workflow surface.
        # (For a custom workflow the owner keeps access even if a member
        # tool later became invisible; that member's child job is then
        # refused at the per-job submit/stage gate.)
        from matofx.workflows.service import can_user_see_workflow  # noqa: PLC0415

        if not await can_user_see_workflow(session, actor_user_id=actor_user_id, wf=wf):
            raise WorkflowNotFoundError(f"No workflow called {workflow_alias!r}.")

        # Reload members in display order — these become the child jobs.
        members_stmt = (
            select(WorkflowMember)
            .where(WorkflowMember.workflow_id == wf.id)
            .order_by(WorkflowMember.position)
        )
        members = list((await session.execute(members_stmt)).scalars())
        if not members:
            raise WorkflowStateError(
                f"Workflow {wf.alias!r} has no member tools. Add at "
                "least one tool to the workflow before launching it."
            )

        # Resolve the currently-released version of every member tool.
        # Floating versions per the locked design: the run always uses
        # whichever version is in ``released`` (or whichever's latest
        # for new / test if the caller has visibility).
        tool_aliases = [m.tool_alias for m in members]
        version_id_by_alias = await _resolve_current_tool_versions(session, tool_aliases)
        for alias in tool_aliases:
            if alias not in version_id_by_alias:
                raise WorkflowStateError(
                    f"Couldn't resolve a tool_version for {alias!r}. "
                    "The tool may have been archived or never installed."
                )

        run_name = (name or f"{wf.name} run").strip()
        run = WorkflowRun(
            workflow_id=wf.id,
            workflow_version_snapshot=wf.version,
            scenario_id=scenario_id,
            name=run_name,
            submitted_by_user_id=actor_user_id,
        )
        session.add(run)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            constraint_text = str(exc.orig).lower() if exc.orig is not None else str(exc).lower()
            # SQLite reports the offending columns
            # (``workflow_run.scenario_id, workflow_run.workflow_id``);
            # Postgres reports the constraint name. Match either form
            # so the friendly error path covers both engines.
            is_dup = "uq_workflow_run_scenario_workflow" in constraint_text or (
                "workflow_run.scenario_id" in constraint_text
                and "workflow_run.workflow_id" in constraint_text
            )
            if is_dup:
                raise WorkflowStateError(
                    f"This scenario already has a run of workflow "
                    f"{wf.alias!r}. Open the existing run rather than "
                    "starting a new one."
                ) from exc
            raise

        # Create one draft child Job per member, in the member's
        # display order. These start in ``created`` state with empty
        # inputs — the user submits each one via the standard queue
        # endpoint as they walk the tabs.
        for member in members:
            session.add(
                Job(
                    scenario_id=scenario_id,
                    name=f"{member.tool_alias} (run #{run.id})",
                    tool_alias=member.tool_alias,
                    tool_version_id=version_id_by_alias[member.tool_alias],
                    state="created",
                    inputs={},
                    created_by_user_id=actor_user_id,
                    workflow_run_id=run.id,
                    workflow_position=member.position,
                )
            )
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.run.start",
                resource_type="workflow_run",
                resource_id=str(run.id),
                outcome="success",
                payload={
                    "workflow_alias": wf.alias,
                    "workflow_version": wf.version,
                    "scenario_id": scenario_id,
                    "member_count": len(members),
                },
            ),
        )
        return run

    async def cancel_run(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        run_id: int,
        bypass_access: bool = False,
    ) -> WorkflowRun:
        """Mark the run cancelled and signal still-pending children.

        Idempotent: a cancel on an already-cancelled run is a no-op.
        Child-job mechanics:

        - ``created`` children are left alone (they're drafts; no
          runner involvement). The cancelled run banner locks the
          tab so the user can't submit them.
        - ``queued`` / ``running`` children get ``cancel_requested_at``
          stamped — the existing runner cancel flow picks them up on
          its next loop.
        - ``complete`` / ``failed`` children are unaffected (already
          terminal).

        ``bypass_access=True`` skips the project-access check — used by
        the Live Jobs bulk-cancel surface, which gates on
        :attr:`Permission.LIVE_JOBS_CANCEL` before calling, and whose
        operator may hold no ACL on the run's scenario.
        """
        if bypass_access:
            run = await session.get(WorkflowRun, run_id)
            if run is None:
                raise NotFoundError("workflow_run", run_id)
        else:
            run = await self._get_with_access(session, run_id, actor_user_id)
        if run.cancel_requested_at is not None:
            return run
        now = _utcnow()
        run.cancel_requested_at = now
        run.cancel_requested_by_user_id = actor_user_id

        children = await self._list_children(session, run.id)
        for child in children:
            if child.state in ("queued", "running") and child.cancel_requested_at is None:
                child.cancel_requested_at = now
                child.cancel_requested_by_user_id = actor_user_id
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.run.cancel",
                resource_type="workflow_run",
                resource_id=str(run.id),
                outcome="success",
                payload={
                    "cancelled_pending_children": sum(
                        1 for c in children if c.state in ("queued", "running")
                    ),
                },
            ),
        )
        return run

    # --- headless orchestration (ADR-0009 §3.4 prerequisite) -------------

    async def start_headless_run(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        scenario_id: int,
        workflow_alias: str,
        open_inputs: dict[str, dict[str, Any]] | None = None,
        optimisation_study_id: int | None = None,
        name: str | None = None,
    ) -> WorkflowRun:
        """Start a self-driving (``auto_advance``) workflow run + queue its sources.

        Unlike :meth:`start_run` (which leaves every child ``created`` for
        the user to submit tab-by-tab), this creates the run with
        ``auto_advance=True`` and the open-input vector, then immediately
        advances it (:meth:`advance`) — queueing the source members (those
        with no wired upstream in the chain). The runner's post-completion hook
        calls :meth:`advance` again as each member finishes, so the run
        drives itself to completion with no human in the loop.

        ``open_inputs`` is ``{tool_alias: {input_name: value}}``;
        ``optimisation_study_id`` tags the run for the ``workflow``
        optimisation target (§3.4) when set.
        """
        scenario = await session.get(Scenario, scenario_id)
        if scenario is None:
            raise NotFoundError("scenario", scenario_id)
        await _require_project_access(
            session,
            project_id=scenario.project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.EDIT,
            resource_type="scenario",
            resource_id=scenario_id,
        )
        wf = (
            await session.execute(select(Workflow).where(Workflow.alias == workflow_alias))
        ).scalar_one_or_none()
        if wf is None:
            raise WorkflowNotFoundError(f"No workflow called {workflow_alias!r}.")
        if wf.archived_at is not None:
            raise WorkflowArchivedError(wf.alias)
        members = list(
            (
                await session.execute(
                    select(WorkflowMember)
                    .where(WorkflowMember.workflow_id == wf.id)
                    .order_by(WorkflowMember.position)
                )
            ).scalars()
        )
        if not members:
            raise WorkflowStateError(
                f"Workflow {wf.alias!r} has no member tools. Add at least one "
                "tool to the workflow before launching it."
            )
        tool_aliases = [m.tool_alias for m in members]
        version_id_by_alias = await _resolve_current_tool_versions(session, tool_aliases)
        for alias in tool_aliases:
            if alias not in version_id_by_alias:
                raise WorkflowStateError(
                    f"Couldn't resolve a tool_version for {alias!r}. The tool may "
                    "have been archived or never installed."
                )

        run = WorkflowRun(
            workflow_id=wf.id,
            workflow_version_snapshot=wf.version,
            scenario_id=scenario_id,
            name=(name or f"{wf.name} run").strip(),
            submitted_by_user_id=actor_user_id,
            auto_advance=True,
            open_inputs_json=dict(open_inputs or {}),
            optimisation_study_id=optimisation_study_id,
        )
        session.add(run)
        await session.flush()
        for member in members:
            session.add(
                Job(
                    scenario_id=scenario_id,
                    name=f"{member.tool_alias} (run #{run.id})",
                    tool_alias=member.tool_alias,
                    tool_version_id=version_id_by_alias[member.tool_alias],
                    state="created",
                    inputs={},
                    created_by_user_id=actor_user_id,
                    workflow_run_id=run.id,
                    workflow_position=member.position,
                )
            )
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.run.start_headless",
                resource_type="workflow_run",
                resource_id=str(run.id),
                outcome="success",
                payload={
                    "workflow_alias": wf.alias,
                    "scenario_id": scenario_id,
                    "member_count": len(members),
                    "optimisation_study_id": optimisation_study_id,
                },
            ),
        )
        await self.advance(session, run_id=run.id, actor_user_id=actor_user_id)
        return run

    async def advance(
        self,
        session: AsyncSession,
        *,
        run_id: int,
        actor_user_id: int,
    ) -> WorkflowRun:
        """Queue every child that has just become runnable (idempotent).

        Called once at headless start (to launch the source members) and
        again by the runner after each child finalises. For each child
        still ``created`` whose wired upstreams are all ``complete``, it
        resolves the child's inputs (open values from
        ``open_inputs_json`` + wired values pulled from the upstreams'
        outputs) and queues it via :meth:`JobService.queue_for_runner`.
        A no-op for non-``auto_advance`` (manual) or cancelled runs.
        """
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            raise NotFoundError("workflow_run", run_id)
        if not run.auto_advance or run.cancel_requested_at is not None:
            return run

        children = await self._list_children(session, run_id)
        by_alias = {c.tool_alias: c for c in children}
        descriptors = await self._load_descriptors(session, set(by_alias))
        job_service = make_job_service(self.audit, environment=self.environment)

        member_aliases = set(by_alias)
        completed: dict[str, _MemberOutputs] = {}
        for alias, child in by_alias.items():
            if child.state == "complete":
                completed[alias] = await self._read_member_outputs(
                    session, job_service, child, descriptors.get(alias) or {}
                )
        completed_outputs = {alias: mo.values for alias, mo in completed.items()}

        plan = ready_member_inputs(
            member_states={alias: c.state for alias, c in by_alias.items()},
            descriptors=descriptors,
            open_inputs=run.open_inputs_json or {},
            completed_outputs=completed_outputs,
        )
        for alias, inputs in plan.items():
            wired_file_inputs = _wired_file_sources(
                descriptor=descriptors.get(alias) or {},
                member_aliases=member_aliases,
                completed=completed,
            )
            await job_service.queue_for_runner(
                session,
                job_id=by_alias[alias].id,
                actor_user_id=actor_user_id,
                inputs=inputs,
                access_check=False,
                audit_via="workflow",
                wired_file_inputs=wired_file_inputs or None,
            )
        if plan:
            await session.flush()
            await self.audit.record(
                session,
                RecordInput(
                    actor="system:runner",
                    actor_type="system",
                    action="workflows.run.advance",
                    resource_type="workflow_run",
                    resource_id=str(run_id),
                    outcome="success",
                    payload={"queued_members": sorted(plan)},
                ),
            )
        return run

    async def _load_descriptors(
        self, session: AsyncSession, aliases: set[str]
    ) -> dict[str, dict[str, Any]]:
        if not aliases:
            return {}
        rows = (await session.execute(select(Tool).where(Tool.alias.in_(aliases)))).scalars()
        return {t.alias: dict(t.descriptor or {}) for t in rows}

    async def _read_member_outputs(
        self,
        session: AsyncSession,
        job_service: Any,
        child: Job,
        descriptor: Mapping[str, Any],
    ) -> _MemberOutputs:
        """A completed child's outputs — scalars *and* resolved file paths.

        Scalar (``number`` / ``string``) outputs come from the parsed
        ``outputs.json`` document, keyed by output name. **File** outputs are
        resolved to the on-disk absolute path under the member's retained
        working dir (``<mount>/<working_dir_relative>/outputs/<filename>`` —
        the same layout the report + jobs-output routes serve from), so a
        wired file input receives the *same shape a previous-job pick
        produces* (ADR-0015 §5.1). For a time-resolved field
        (``field_timesteps`` glob) every matching snapshot is recorded in
        :attr:`_MemberOutputs.file_sources` so the staging copy can take the
        whole series; the representative ``filename`` is the wired value.

        A file output whose artefact is genuinely absent on disk is omitted
        (no value, no source) — ``resolve_wired_values`` then skips it and
        the downstream job's own validation surfaces a plain "required input
        missing" (§5.4). Empty document / missing working dir degrade to
        scalars-only.
        """
        values: dict[str, Any] = {}
        try:
            doc = await job_service.get_output_document(session, job_id=child.id)
        except NotFoundError:
            doc = None
        if doc is not None:
            for entry in doc.outputs:
                name = getattr(entry, "name", None)
                if isinstance(name, str):
                    values[name] = getattr(entry, "value", None)

        file_sources: dict[str, list[str]] = {}
        file_meta = _descriptor_file_outputs(descriptor)
        working_dir = await self._member_working_dir(session, child)
        if file_meta and working_dir is not None:
            import asyncio  # noqa: PLC0415

            resolved = await asyncio.to_thread(_resolve_file_outputs, working_dir, file_meta)
            for name, (primary, series) in resolved.items():
                # The file path is authoritative — it overrides the file
                # entry's conventional ``None`` value in outputs.json.
                values[name] = primary
                file_sources[name] = series

        return _MemberOutputs(values=values, file_sources=file_sources)

    async def _member_working_dir(self, session: AsyncSession, child: Job) -> Path | None:
        """Absolute working dir of a finished child, or ``None`` if unresolved.

        Needs the child's pinned ``storage_location_id`` + ``working_dir_relative``
        (set at queue time). Returns ``None`` for a child that never staged a
        working dir (e.g. a faked queue in tests) so file resolution simply
        falls back to scalars-only.
        """
        if child.storage_location_id is None or not child.working_dir_relative:
            return None
        from matofx.tools.exec_orm import StorageLocation  # noqa: PLC0415

        location = await session.get(StorageLocation, child.storage_location_id)
        if location is None or not location.mount_path:
            return None
        return Path(location.mount_path) / child.working_dir_relative

    async def resolve_wired_files_for_member(
        self, session: AsyncSession, job: Job
    ) -> tuple[dict[str, str], dict[str, list[str]]]:
        """Resolve a member's **file**-wired inputs from its completed siblings.

        For a workflow-member ``job`` about to be queued, returns
        ``({field: primary_path}, {field: [source_paths]})`` for each of its
        file inputs wired (``downstream_input``) to an in-chain upstream's file
        output that has completed. Used by
        :meth:`JobService.queue_for_runner` so the **manual** tab-by-tab run
        path stages the wired file the same way the headless :meth:`advance`
        does — the per-tab UI only seeds scalar wired values (a file output's
        ``value`` is ``None``), so without this the file input arrives empty.
        Empty when the job isn't a workflow member or nothing's wired/ready.
        """
        if job.workflow_run_id is None:
            return {}, {}
        children = await self._list_children(session, job.workflow_run_id)
        by_alias = {c.tool_alias: c for c in children}
        descriptors = await self._load_descriptors(session, set(by_alias))
        member_aliases = set(by_alias)
        job_service = make_job_service(self.audit, environment=self.environment)
        completed: dict[str, _MemberOutputs] = {}
        for alias, child in by_alias.items():
            if child.state == "complete":
                completed[alias] = await self._read_member_outputs(
                    session, job_service, child, descriptors.get(alias) or {}
                )
        descriptor = descriptors.get(job.tool_alias) or {}
        file_input_names = {
            f["name"]
            for f in descriptor.get("inputs") or []
            if isinstance(f, Mapping)
            and f.get("type") == "file"
            and isinstance(f.get("name"), str)
            and f.get("name")
        }
        primaries: dict[str, str] = {}
        sources: dict[str, list[str]] = {}
        for input_name, (upstream_alias, output_name) in wired_inputs(
            descriptor, member_aliases
        ).items():
            if input_name not in file_input_names:
                continue
            member = completed.get(upstream_alias)
            if member is None:
                continue
            src = member.file_sources.get(output_name)
            primary = member.values.get(output_name)
            if src and isinstance(primary, str) and primary:
                sources[input_name] = list(src)
                primaries[input_name] = primary
        return primaries, sources

    # --- reads -----------------------------------------------------------

    async def get_run(
        self,
        session: AsyncSession,
        *,
        run_id: int,
        actor_user_id: int,
    ) -> WorkflowRun:
        """Fetch one run; VIEW required on the scenario's project."""
        return await self._get_with_access(session, run_id, actor_user_id)

    async def list_children(
        self,
        session: AsyncSession,
        *,
        run_id: int,
        actor_user_id: int,
    ) -> list[Job]:
        """Return the run's child jobs in tab order (``workflow_position`` ASC)."""
        await self._get_with_access(session, run_id, actor_user_id)
        return await self._list_children(session, run_id)

    async def list_for_scenario(
        self,
        session: AsyncSession,
        *,
        scenario_id: int,
        actor_user_id: int,
    ) -> list[WorkflowRun]:
        """Every workflow run on a scenario, newest first."""
        scenario = await session.get(Scenario, scenario_id)
        if scenario is None:
            raise NotFoundError("scenario", scenario_id)
        await _require_project_access(
            session,
            project_id=scenario.project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.VIEW,
            resource_type="scenario",
            resource_id=scenario_id,
        )
        stmt = (
            select(WorkflowRun)
            .where(WorkflowRun.scenario_id == scenario_id)
            .order_by(WorkflowRun.created_at.desc())
        )
        return list((await session.execute(stmt)).scalars())

    async def list_visible(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        limit: int = 50,
    ) -> list[WorkflowRun]:
        """Cross-project listing for the My Jobs workspace.

        Returns runs whose scenarios sit in projects the caller can
        see. Joined to ``scenario`` so the project-access check can
        bulk-filter; deep filtering (per-row access ladder) happens
        in the project-service layer the caller already uses for
        regular jobs.
        """
        # Simple approach for v1: pull all runs, then filter via the
        # per-scenario access check. Phase 2 can swap for a joined
        # query if performance demands it.
        stmt = select(WorkflowRun).order_by(WorkflowRun.created_at.desc()).limit(limit * 4)
        candidates = list((await session.execute(stmt)).scalars())
        visible: list[WorkflowRun] = []
        seen_scenarios: dict[int, bool] = {}
        for run in candidates:
            sid = run.scenario_id
            if sid not in seen_scenarios:
                scenario = await session.get(Scenario, sid)
                if scenario is None:
                    seen_scenarios[sid] = False
                    continue
                try:
                    await _require_project_access(
                        session,
                        project_id=scenario.project_id,
                        actor_user_id=actor_user_id,
                        required=ProjectAccessLevel.VIEW,
                        resource_type="workflow_run",
                        resource_id=run.id,
                    )
                except AccessDeniedError:
                    seen_scenarios[sid] = False
                    continue
                seen_scenarios[sid] = True
            if seen_scenarios[sid]:
                visible.append(run)
                if len(visible) >= limit:
                    break
        return visible

    # --- internals -------------------------------------------------------

    async def _get_with_access(
        self,
        session: AsyncSession,
        run_id: int,
        actor_user_id: int,
    ) -> WorkflowRun:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            raise NotFoundError("workflow_run", run_id)
        scenario = await session.get(Scenario, run.scenario_id)
        if scenario is None:
            raise NotFoundError("workflow_run", run_id)
        await _require_project_access(
            session,
            project_id=scenario.project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.VIEW,
            resource_type="workflow_run",
            resource_id=run_id,
        )
        return run

    async def _list_children(
        self,
        session: AsyncSession,
        run_id: int,
    ) -> list[Job]:
        stmt = select(Job).where(Job.workflow_run_id == run_id).order_by(Job.workflow_position)
        return list((await session.execute(stmt)).scalars())


def _descriptor_file_outputs(
    descriptor: Mapping[str, Any],
) -> list[tuple[str, str, str | None]]:
    """``(output_name, filename, field_timesteps)`` for each file output.

    Walks the descriptor's ``outputs`` for ``type == "file"`` entries that
    declare a ``filename`` (the relative path under the job's ``outputs/``
    dir). ``field_timesteps`` is the optional transient-series glob (relative
    to the working dir), carried through so the resolver can expand the
    whole series. Malformed entries are skipped.
    """
    meta: list[tuple[str, str, str | None]] = []
    for out in descriptor.get("outputs") or []:
        if not isinstance(out, Mapping) or out.get("type") != "file":
            continue
        name = out.get("name")
        filename = out.get("filename")
        if not (isinstance(name, str) and name and isinstance(filename, str) and filename):
            continue
        timesteps = out.get("field_timesteps")
        series = timesteps if isinstance(timesteps, str) and timesteps else None
        meta.append((name, filename, series))
    return meta


def _outputs_relative_leaf(filename: str) -> str:
    """Strip a leading ``./`` / ``outputs/`` from a descriptor file-output name.

    A descriptor ``filename`` may be declared relative to the working dir with
    the ``outputs/`` prefix or as a bare leaf; both resolve under
    ``<wd>/outputs/``. Mirrors the jobs-output download route's normalisation
    so a wired file hand-off doesn't build a doubled ``outputs/outputs/`` path.
    """
    leaf = filename.replace("\\", "/").strip("/")
    while leaf.startswith("./"):
        leaf = leaf[2:]
    if leaf.startswith("outputs/"):
        leaf = leaf[len("outputs/") :]
    return leaf


def _resolve_file_outputs(
    working_dir: Path,
    file_meta: list[tuple[str, str, str | None]],
) -> dict[str, tuple[str, list[str]]]:
    """Resolve each descriptor file output to ``(primary_path, [series])``.

    Runs off the event loop (called via ``asyncio.to_thread``) because it
    stats / globs the shared mount. The primary path is
    ``<working_dir>/outputs/<filename>``; a genuinely-absent file is omitted
    (ADR-0015 §5.4). For a ``field_timesteps`` output the glob is expanded
    against the working dir and the primary is guaranteed a member of the
    returned series.
    """
    resolved: dict[str, tuple[str, list[str]]] = {}
    for name, filename, timesteps in file_meta:
        # The descriptor ``filename`` may already carry the ``outputs/``
        # prefix (relative to the working dir, e.g. Thermal Soak's
        # "outputs/temperature_final.vtu"); strip it so we don't build a
        # doubled ``outputs/outputs/`` path that never exists. The series
        # glob below is matched against the working dir as-is (it carries
        # its own ``outputs/`` prefix), so both resolve under <wd>/outputs/.
        primary = working_dir / "outputs" / _outputs_relative_leaf(filename)
        if not primary.is_file():
            continue
        primary_str = str(primary)
        if timesteps:
            series = sorted(str(p) for p in working_dir.glob(timesteps) if p.is_file())
            if primary_str not in series:
                series.append(primary_str)
        else:
            series = [primary_str]
        resolved[name] = (primary_str, series)
    return resolved


def _wired_file_sources(
    *,
    descriptor: Mapping[str, Any],
    member_aliases: set[str],
    completed: Mapping[str, _MemberOutputs],
) -> dict[str, list[str]]:
    """File-input → upstream source paths for a ready member (ADR-0015 §5.3).

    For each of the member's **file** inputs that is wired to an in-chain
    upstream's file output, return the upstream's resolved source paths so
    :func:`stage_wired_file_inputs` can copy them into the downstream
    working dir. Scalar-wired inputs and file inputs whose upstream produced
    no file are absent — nothing to copy.
    """
    file_input_names = {
        f["name"]
        for f in descriptor.get("inputs") or []
        if isinstance(f, Mapping)
        and f.get("type") == "file"
        and isinstance(f.get("name"), str)
        and f.get("name")
    }
    if not file_input_names:
        return {}
    sources: dict[str, list[str]] = {}
    for input_name, (upstream_alias, output_name) in wired_inputs(
        descriptor, member_aliases
    ).items():
        if input_name not in file_input_names:
            continue
        member = completed.get(upstream_alias)
        if member is None:
            continue
        upstream_sources = member.file_sources.get(output_name)
        if upstream_sources:
            sources[input_name] = list(upstream_sources)
    return sources


async def _resolve_current_tool_versions(
    session: AsyncSession, aliases: list[str]
) -> dict[str, int]:
    """Look up the most-recent ``tool_version`` id for each alias.

    "Most-recent" matches the convention used by direct job submission
    (see :meth:`JobService.create`): the latest snapshot wins.
    """
    if not aliases:
        return {}
    result: dict[str, int] = {}
    for alias in set(aliases):
        stmt = (
            select(ToolVersion.id)
            .where(ToolVersion.alias == alias)
            .order_by(ToolVersion.created_at.desc())
            .limit(1)
        )
        version_id = (await session.execute(stmt)).scalar_one_or_none()
        if version_id is not None:
            result[alias] = int(version_id)
    return result


def make_workflow_run_service(
    audit: AuditWriter, *, environment: str | None = None
) -> WorkflowRunService:
    """Wire up a :class:`WorkflowRunService` from the ambient audit writer.

    ``environment`` is only needed by the headless orchestrator
    (:meth:`WorkflowRunService.advance`), which stages child working
    directories; the read/manual paths leave it ``None``.
    """
    return WorkflowRunService(audit=audit, environment=environment)
