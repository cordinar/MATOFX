"""The runner's long-lived async coordinator.

:class:`RunnerSupervisor` owns four cooperating tasks:

- ``_pickup_loop`` — DB-polls for jobs in state ``"queued"``,
  starts them via the registered :class:`ExecutionTarget` while
  respecting :attr:`RuntimeSettings.local_concurrency`, and
  transitions the canonical state to ``"running"``.
- ``_heartbeat_loop`` — for every job in state ``"running"``, polls
  the heartbeat directory at the per-target interval and drives
  the canonical state to ``"complete"`` or ``"failed"`` when the
  launcher writes a terminal heartbeat. Honours the descriptor's
  cleanup rules on success.
- ``_cancel_loop`` — for every job that has a non-null
  ``cancel_requested_at``, asks the relevant target to cancel
  with the configured grace period.
- ``_settings_loop`` — re-reads the ``runtime_setting`` table every
  30 s and atomically swaps the current snapshot.

The four loops share one event loop and are cancelled together on
graceful shutdown. The supervisor never exits on its own — only a
caller-issued ``stop()`` ends it.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError

from matofx.audit.schema import RecordInput

# The runner flushes ORM objects (Job, ApprovalRequest, study results,
# ...) whose mappers carry foreign keys into tables defined across many
# modules. SQLAlchemy resolves *every* FK in the shared MetaData when it
# sorts tables for a flush, so a table missing from the metadata — its
# module never imported in this process — raises NoReferencedTableError
# and breaks every flush (the deployed runner crashed exactly this way
# on approval_request.tool_vv_record_id → tool_vv_record). The API gets
# the full model set transitively via its routers; the standalone
# runner must register it explicitly. Note this module is imported
# during matofx.config initialisation, so any ORM package __init__ that
# eagerly imports config back will circular-import here — break such
# cycles in the offending package (cf. favourites/service.py), not by
# deferring this import.
from matofx.db import all_orm as _all_orm  # noqa: F401
from matofx.db.session import session_scope
from matofx.hierarchy.orm import Job
from matofx.kpi import extract_kpis_for_job
from matofx.notify import NoopNotifier, Notifier
from matofx.runner.hostcaps import resolve_local_budget
from matofx.runner.registry import TargetRegistry, UnsupportedTargetError
from matofx.runner.settings import (
    DEFAULT_RUNTIME_SETTINGS,
    RuntimeSettings,
    load_runtime_settings,
)
from matofx.studies.results_service import record_study_result
from matofx.tools.exec.base import (
    ExecutionTarget,
    ExecutionTargetKind,
    ResourceRequest,
    SubmitResult,
    TargetContext,
)
from matofx.tools.exec.cleanup import apply_cleanup
from matofx.tools.exec.contracts import OutputsDocument
from matofx.tools.exec.staging import (
    HEARTBEAT_DIRNAME,
    INPUTS_DIRNAME,
    LOGS_DIRNAME,
    OUTPUTS_DIRNAME,
    StagedWorkingDir,
)
from matofx.tools.exec_orm import AosHost, StorageLocation
from matofx.tools.orm import Tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.approvals.orm import ApprovalRequest
    from matofx.audit import AuditWriter
    from matofx.db.session import SessionFactory

_log = logging.getLogger(__name__)


_PICKUP_INTERVAL_S: float = 1.0
"""How often the pickup loop scans for queued jobs. Hard-coded
because it's a controller-side knob, not an admin one."""

_SETTINGS_RELOAD_INTERVAL_S: float = 30.0
"""How often the settings loop re-reads ``runtime_setting``."""

_CANCEL_LOOP_INTERVAL_S: float = 1.0
"""How often the cancel loop scans for cancel requests."""

_OPTIMISATION_INTERVAL_S: float = 5.0
"""How often the optimisation loop scans for ``running`` optimisation
studies to drive (ADR-0009 §4). Picks up newly-configured studies and
spawns a per-study outer-loop task; cheap when none are running."""

_LICENCE_RECHECK_INTERVAL_S: float = 3600.0
"""How often the licence is re-verified (brief §C16 default: 60 min).

Deliberately slow: this is the "has someone edited the licence file, or
has it expired since boot?" check, not a per-request gate. Each tick
writes one audit record, which is what a compliance report counts."""


_VALID_TARGETS: frozenset[str] = frozenset({"local", "aos", "hpc"})

_MAX_CONSECUTIVE_MISSING_STAGING_POLLS: int = 10
"""How many consecutive heartbeat polls a job is allowed to spend with
its staged working directory unreachable before the runner declares it
lost and fails it. With local/AOS at 2 s polls that's 20 s of grace;
with HPC running at 10 s polls it's 100 s — long enough that
intermittent NFS hiccups don't kill jobs. **B2** fix."""


@dataclass
class _PollState:
    """In-memory per-job heartbeat-poll bookkeeping.

    Lives only on the supervisor instance. Cleared on every tick for
    jobs that have left the running state, and naturally lost on a
    runner restart (the launcher heartbeat is the source of truth, so
    a fresh poll on restart will re-converge the canonical state).
    """

    last_polled_at: float = 0.0
    """Monotonic timestamp of the last successful poll for this job. **P1** fix."""

    consecutive_missing_staging: int = 0
    """How many consecutive ticks observed the staged working directory
    as unreachable. Drives the bounded-retry fail in **B2**."""


@dataclass
class _LaneState:
    """Mutable per-tick budget for the local admission lanes.

    Seeded from what's already running plus the resolved host budget, then
    decremented as the pickup loop admits local jobs this tick. AOS/HPC
    jobs never touch it (they're handed off to a remote queue).
    """

    local_slots: int
    """Remaining ``local_concurrency`` slots (CPU + GPU local jobs)."""

    gpu_slots: int
    """Remaining ``gpu_concurrency`` slots (local GPU jobs only)."""

    cpu_used: int
    """Cores already committed to running + this-tick-admitted local jobs."""

    mem_used: int
    """Memory (MB) committed, same scope as :attr:`cpu_used`."""

    cpu_budget: int
    """Total cores the runner may hand out (resolved from settings/host)."""

    mem_budget: int
    """Total memory (MB) the runner may hand out."""


@dataclass
class _AosHostLane:
    """Mutable per-tick admission budget for one always-on server.

    Unlike HPC (where the cluster scheduler owns the queue), an AOS box is
    a dumb persistent host, so the runner gates it per-host: an always-on
    job-count cap, plus optional CPU / memory budgets when the admin has
    declared the host's specs (a remote host can't be auto-detected).
    """

    host_name: str
    """The host's display name, for the over-capacity failure message."""

    slots: int
    """Remaining ``max_concurrency`` slots on this host."""

    cpu_used: int
    """Cores committed to running + this-tick-admitted jobs on this host."""

    mem_used: int
    """Memory (MB) committed, same scope as :attr:`cpu_used`."""

    cpu_budget: int | None
    """CPU-core budget; ``None`` ⇒ no CPU budget (count-cap only)."""

    mem_budget: int | None
    """Memory budget (MB); ``None`` ⇒ no memory budget (count-cap only)."""


def _kind_for(tool: Tool) -> ExecutionTargetKind:
    """Narrow ``tool.execution_target`` (a ``str``) to the runtime literal type.

    The CHECK constraint on the column guarantees the value is one
    of ``"local"`` / ``"aos"`` / ``"hpc"``; the helper exists to
    reflect that to mypy without scattering ``cast`` calls.
    """
    value = tool.execution_target
    if value not in _VALID_TARGETS:
        raise UnsupportedTargetError(
            f"Tool {tool.alias!r} has unrecognised execution_target {value!r}."
        )
    return value  # type: ignore[return-value]


@dataclass
class RunnerSupervisor:
    """Coordinates the runner's pickup, heartbeat, cancel, and settings loops.

    Construct one per process; call :meth:`start` to spin the
    loops up and :meth:`stop` for a graceful shutdown.
    """

    factory: SessionFactory
    audit: AuditWriter
    notifier: Notifier = field(default_factory=NoopNotifier)
    registry: TargetRegistry = field(default_factory=TargetRegistry)
    settings: RuntimeSettings = DEFAULT_RUNTIME_SETTINGS
    pickup_interval_s: float = _PICKUP_INTERVAL_S
    settings_reload_interval_s: float = _SETTINGS_RELOAD_INTERVAL_S
    cancel_loop_interval_s: float = _CANCEL_LOOP_INTERVAL_S
    optimisation_loop_interval_s: float = _OPTIMISATION_INTERVAL_S
    app_settings: Any = None
    """The app :class:`~matofx.config.Settings`, for the licence re-check.

    ``None`` (the default, and what most tests construct) skips the
    licence loop entirely — the supervisor's other loops must stay
    usable without a configured licence."""
    licence_recheck_interval_s: float = _LICENCE_RECHECK_INTERVAL_S
    environment: str | None = None
    """Staging environment for optimisation-study children (ADR-0009 §4);
    ``None`` uses the default path segment."""

    _stopping: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list, init=False)
    _poll_state: dict[int, _PollState] = field(default_factory=dict, init=False)
    _optimisation_runtime: Any = field(default=None, init=False)
    _licence_state: Any = field(default=None, init=False)
    """Last licence resolution, so the loop logs transitions rather than
    repeating the same line every hour."""

    async def start(self) -> None:
        """Spin up the background loops (pickup / heartbeat / cancel / settings / inactivity / retention / optimisation)."""  # noqa: E501
        from matofx.runner.retention_sweeper import run_retention_sweeper  # noqa: PLC0415
        from matofx.users.inactive_sweeper import run_inactive_sweeper  # noqa: PLC0415

        if self._tasks:
            raise RuntimeError("RunnerSupervisor already started")
        async with session_scope(self.factory) as session:
            self.settings = await load_runtime_settings(session)
        self._tasks = [
            asyncio.create_task(self._settings_loop(), name="runner.settings"),
            asyncio.create_task(self._pickup_loop(), name="runner.pickup"),
            asyncio.create_task(self._heartbeat_loop(), name="runner.heartbeat"),
            asyncio.create_task(self._cancel_loop(), name="runner.cancel"),
            asyncio.create_task(
                run_inactive_sweeper(
                    self.factory,
                    notifier=self.notifier,
                    audit=self.audit,
                    stopping=self._stopping,
                ),
                name="runner.inactivity",
            ),
            asyncio.create_task(
                run_retention_sweeper(
                    self.factory,
                    audit=self.audit,
                    stopping=self._stopping,
                ),
                name="runner.retention",
            ),
            asyncio.create_task(self._optimisation_loop(), name="runner.optimisation"),
        ]
        if self.app_settings is not None:
            self._tasks.append(
                asyncio.create_task(self._licence_loop(), name="runner.licence")
            )

    async def stop(self) -> None:
        """Signal all loops to stop, await their exit, drain handles."""
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                continue
            except Exception:
                _log.exception("Runner task raised on shutdown; continuing drain")
        self._tasks = []

    # ----- loop bodies ---------------------------------------------------

    async def _settings_loop(self) -> None:
        await self._sleepable(self._reload_settings, self.settings_reload_interval_s)

    async def _pickup_loop(self) -> None:
        await self._sleepable(self._pickup_once, self.pickup_interval_s)

    async def _heartbeat_loop(self) -> None:
        await self._sleepable(
            self._heartbeat_once,
            lambda: self.settings.heartbeat_local_aos_interval_s,
        )

    async def _cancel_loop(self) -> None:
        await self._sleepable(self._cancel_once, self.cancel_loop_interval_s)

    async def _optimisation_loop(self) -> None:
        await self._sleepable(self._optimisation_once, self.optimisation_loop_interval_s)

    async def _licence_loop(self) -> None:
        await self._sleepable(self._licence_once, self.licence_recheck_interval_s)

    async def _licence_once(self) -> None:
        """Re-verify the licence and record the outcome (ADR-0019 slice 3).

        The runner is the right home for this: it is the one process
        guaranteed to be long-lived, and it already owns the audit
        writer. Never raises — a licence that changed underneath a
        running install must not kill jobs mid-flight; it is recorded,
        logged, and escalates through the banner and the next compliance
        report instead.
        """
        from matofx.licence import recheck_licence, verify_install_binding  # noqa: PLC0415
        from matofx.licence.errors import LicenceInvalidError  # noqa: PLC0415

        async with session_scope(self.factory) as session:
            before = self._licence_state
            self._licence_state = await recheck_licence(
                session,
                settings=self.app_settings,
                previous=self._licence_state,
            )
            await self._alert_on_licence_transition(session, before, self._licence_state)
            # Catch a licence swapped in *after* boot that belongs to a
            # different install. The API checks this at startup and
            # refuses to serve; the runner cannot refuse without killing
            # jobs mid-flight, so it logs — the API will refuse on its
            # next restart, and the mismatch is already in the chain via
            # `recheck_licence`.
            try:
                await verify_install_binding(session, self._licence_state)
            except LicenceInvalidError as exc:
                _log.error("Licence does not belong to this install: %s", exc)
            await self._verify_chain_once(session)

    async def _alert_on_licence_transition(
        self,
        session: AsyncSession,
        before: object | None,
        after: object,
    ) -> None:
        """Email administrators when the licence *state* changes.

        Only on a transition, never per tick: an hourly "still in grace"
        email for a month would train everyone to filter the one that
        matters. This is the third licence alert (with break-glass and
        exhausted slots) and the one with the most lead time — a grace
        period is a warning that a renewal is overdue, and it is worth
        far more before read-only than after.
        """
        before_state = getattr(getattr(before, "status", None), "state", None)
        after_state = getattr(getattr(after, "status", None), "state", None)
        if before is None or before_state == after_state or after_state is None:
            return
        if after_state not in ("grace", "expired"):
            return

        banner = getattr(after, "banner", None)
        detail = (
            str(banner)
            if banner
            else "Contact your supplier for a renewal licence."
        )
        if after_state == "grace":
            summary = "your licence has expired and is in its grace period"
            detail = (
                f"{detail}\n\nMATOFX keeps working normally during the grace "
                "period. When it ends the site becomes read-only: existing work "
                "stays readable, but no new jobs can be submitted."
            )
        else:
            summary = "your licence has expired and the site is now read-only"
            detail = (
                f"{detail}\n\nExisting work can still be viewed and downloaded. "
                "Installing a valid licence restores the site immediately, with "
                "no restart."
            )
        try:
            await self.notifier.notify_licence_alert(
                session, kind=f"licence_{after_state}", summary=summary, detail=detail
            )
        except Exception:  # pragma: no cover - never break the tick
            _log.exception("Licence alert failed")

    async def _verify_chain_once(self, session: AsyncSession) -> None:
        """Full audit-chain walk, recording the outcome (ADR-0019 §C14).

        Rides the hourly licence tick rather than getting its own loop:
        both are "is this install still trustworthy?", both are far too
        expensive for a request path, and one loop is one thing to reason
        about. A failure flips the site read-only until a Super User
        acknowledges it — hence the deliberately slow cadence.
        """
        from matofx.audit import verify  # noqa: PLC0415
        from matofx.audit.keys import collect_public_keys  # noqa: PLC0415
        from matofx.compliance.integrity import record_verify_result  # noqa: PLC0415

        audit_settings = self.app_settings.audit
        public_keys = (
            collect_public_keys(audit_settings.signing_key_path)
            if audit_settings.signing_key_path is not None
            else {}
        )
        result = await verify(session, public_keys=public_keys or None)
        await record_verify_result(session, result=result, audit=self.audit)

    async def _optimisation_once(self) -> None:
        """Pick up ``running`` optimisation studies and drive their loops (§4).

        The runtime is built lazily on first use so importing the
        supervisor doesn't pull Optuna; it tracks its own active set, so
        re-scanning every interval only spawns loops for studies not
        already in flight.
        """
        if self._optimisation_runtime is None:
            from matofx.studies.optimisation.runtime import (  # noqa: PLC0415
                OptimisationRuntime,
                ToolJobRunner,
            )

            runner = ToolJobRunner(self.factory, self.audit, environment=self.environment)
            self._optimisation_runtime = OptimisationRuntime(
                self.factory, self.audit, runner=runner, environment=self.environment
            )
        await self._optimisation_runtime.scan()

    async def _sleepable(
        self,
        body: Callable[[], Awaitable[None]],
        interval: float | Callable[[], float],
    ) -> None:
        while not self._stopping.is_set():
            try:
                await body()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("Runner loop body raised; continuing")
            try:
                wait = interval() if callable(interval) else interval
                await asyncio.wait_for(self._stopping.wait(), timeout=max(wait, 0.05))
            except TimeoutError:
                continue

    # ----- per-tick implementations -------------------------------------

    async def _reload_settings(self) -> None:
        async with session_scope(self.factory) as session:
            self.settings = await load_runtime_settings(session)

    async def _pickup_once(self) -> None:
        """Find queued jobs whose lane has capacity; submit each within it.

        Three admission lanes (see ``designs/execution-scheduling.md``):

        - **local CPU** — counted against ``local_concurrency`` *and* the
          host CPU / memory budget (a free slot isn't enough; the host must
          have the cores + RAM the descriptor declares).
        - **local GPU** (``requires_gpu`` + local target) — additionally
          counted against ``gpu_concurrency`` (default 1) so two GPU jobs
          can't exhaust device memory. A GPU job also consumes a local slot.
        - **AOS** — exempt from the *local* lanes, but gated **per host**:
          an always-on server has no scheduler of its own, so the runner
          caps how many jobs (and optionally how much CPU / RAM) it sends
          each host (``_AosHostLane``).
        - **HPC** — fully exempt: the cluster scheduler owns its own queue,
          so the runner just hands off.
        """
        async with session_scope(self.factory) as session:
            running = list(
                (await session.execute(select(Job).where(Job.state == "running"))).scalars()
            )

            # Ordering, in tiers (design §6 "lower-priority study lane"):
            #   1. non-study jobs before study children — study children
            #      never inherit skip-queue/priority and always queue
            #      behind ordinary + workflow jobs of equal priority
            #      (``parametric_study_id IS NULL`` sorts first under
            #      ``.desc()``: True before False);
            #   2. then the skip-queue flag (admin "Skip Queue");
            #   3. then FIFO by ``created_at``.
            queued_stmt = (
                select(Job)
                .where(Job.state == "queued")
                .order_by(
                    Job.parametric_study_id.is_(None).desc(),
                    Job.priority.desc(),
                    Job.created_at.asc(),
                )
            )
            queued = list((await session.execute(queued_stmt)).scalars())
            if not queued:
                return

            # Classify by ``execution_target`` + ``requires_gpu`` — cheap
            # column reads, no descriptor-JSON parse. Batch-load the tool
            # rows once so the per-job classification is a dict lookup.
            tools = await self._load_tools_by_alias(
                session, {j.tool_alias for j in (*running, *queued)}
            )
            lane = self._build_lane_state(running, tools)

            # Per-AOS-host lanes — only pay the host query when an AOS job is
            # actually in play (the common local-only install skips it).
            needs_aos = any(
                (t := tools.get(j.tool_alias)) is not None and t.execution_target == "aos"
                for j in (*running, *queued)
            )
            aos_lanes = await self._build_aos_lanes(session, running, tools) if needs_aos else {}

            # Per-study concurrency cap (design §6): never let one
            # parametric study have more than the cap running at once, so a
            # large sweep can't seize every worker. Seed the tally from
            # what's already running; _admit_queued keeps it current.
            study_running: dict[int, int] = {}
            for r in running:
                if r.parametric_study_id is not None:
                    study_running[r.parametric_study_id] = (
                        study_running.get(r.parametric_study_id, 0) + 1
                    )

            await self._admit_queued(
                session,
                queued=queued,
                tools=tools,
                lane=lane,
                aos_lanes=aos_lanes,
                study_running=study_running,
            )

    def _build_lane_state(self, running: list[Job], tools: dict[str, Tool]) -> _LaneState:
        """Seed the per-tick lane budget from what's already running.

        AOS/HPC running jobs count toward neither local lane (they're not
        local processes); the resolved CPU/memory budget comes from the
        admin settings, falling back to the detected host capacity.
        """
        budget = resolve_local_budget(self.settings)
        local_running = gpu_running = cpu_used = mem_used = 0
        for r in running:
            tool = tools.get(r.tool_alias)
            if tool is None or tool.execution_target != "local":
                continue
            rr = _resource_request_for(tool)
            local_running += 1
            cpu_used += rr.cpu_cores
            mem_used += rr.memory_mb
            if tool.requires_gpu:
                gpu_running += 1
        return _LaneState(
            local_slots=self.settings.local_concurrency - local_running,
            gpu_slots=self.settings.gpu_concurrency - gpu_running,
            cpu_used=cpu_used,
            mem_used=mem_used,
            cpu_budget=budget.cpu_cores,
            mem_budget=budget.memory_mb,
        )

    async def _admit_queued(
        self,
        session: Any,
        *,
        queued: list[Job],
        tools: dict[str, Tool],
        lane: _LaneState,
        aos_lanes: dict[int, _AosHostLane],
        study_running: dict[int, int],
    ) -> None:
        """Walk the ordered queue, admitting each job its lane has room for."""
        study_cap = self.settings.study_concurrency_cap
        for job in queued:
            sid = job.parametric_study_id
            if sid is not None and study_running.get(sid, 0) >= study_cap:
                # This study is at its cap; skip its children this tick.
                continue

            tool = tools.get(job.tool_alias)
            if tool is None:
                # Tool vanished from under a queued job. _pickup_job records
                # the clean failure; consumes no slot.
                await self._pickup_job(session, job)
                continue

            target_kind = tool.execution_target
            if target_kind == "local":
                picked = await self._admit_local(session, job, tool, lane)
            elif target_kind == "aos":
                picked = await self._admit_aos(session, job, tool, aos_lanes)
            else:
                # HPC: exempt — the cluster scheduler owns the queue.
                picked = await self._pickup_job(session, job)

            if picked and sid is not None:
                study_running[sid] = study_running.get(sid, 0) + 1

    async def _build_aos_lanes(
        self, session: Any, running: list[Job], tools: dict[str, Tool]
    ) -> dict[int, _AosHostLane]:
        """Seed each registered AOS host's per-tick lane from its capacity.

        Loads every ``aos_host`` row (there are few) so a candidate's host is
        known even when nothing is running on it yet, then subtracts the
        running usage. Inactive hosts get a lane too; the dispatcher refuses
        them at submit, so they fail cleanly rather than being silently
        starved here.
        """
        hosts = (await session.execute(select(AosHost))).scalars()
        lanes: dict[int, _AosHostLane] = {
            host.id: _AosHostLane(
                host_name=host.name,
                slots=host.max_concurrency,
                cpu_used=0,
                mem_used=0,
                cpu_budget=host.cpu_cores,
                mem_budget=host.memory_mb,
            )
            for host in hosts
        }
        for r in running:
            tool = tools.get(r.tool_alias)
            if tool is None or tool.execution_target != "aos" or tool.aos_host_id is None:
                continue
            lane = lanes.get(tool.aos_host_id)
            if lane is None:
                continue
            rr = _resource_request_for(tool)
            lane.slots -= 1
            lane.cpu_used += rr.cpu_cores
            lane.mem_used += rr.memory_mb
        return lanes

    async def _admit_aos(
        self, session: Any, job: Job, tool: Tool, aos_lanes: dict[int, _AosHostLane]
    ) -> bool:
        """Admit one AOS job if its host's concurrency + optional budget allow.

        A job too large for a *set* host budget is failed (it could never
        fit); one that simply doesn't fit right now is left queued. A job
        whose host isn't registered is handed to ``_pickup_job``, which fails
        it cleanly via the dispatcher.
        """
        host_id = tool.aos_host_id
        lane = aos_lanes.get(host_id) if host_id is not None else None
        if lane is None:
            return await self._pickup_job(session, job)

        rr = _resource_request_for(tool)
        over_cpu = lane.cpu_budget is not None and rr.cpu_cores > lane.cpu_budget
        over_mem = lane.mem_budget is not None and rr.memory_mb > lane.mem_budget
        if over_cpu or over_mem:
            await self._fail_job(
                session,
                job,
                reason=(
                    f"Job needs {rr.cpu_cores} core(s) / {rr.memory_mb} MB, which exceeds the "
                    f"capacity configured for always-on server {lane.host_name!r}. Lower the "
                    "tool's resource request or raise the host's capacity."
                ),
            )
            return False

        if (
            lane.slots <= 0
            or (lane.cpu_budget is not None and lane.cpu_used + rr.cpu_cores > lane.cpu_budget)
            or (lane.mem_budget is not None and lane.mem_used + rr.memory_mb > lane.mem_budget)
        ):
            return False

        if not await self._pickup_job(session, job):
            return False

        lane.slots -= 1
        lane.cpu_used += rr.cpu_cores
        lane.mem_used += rr.memory_mb
        return True

    async def _admit_local(self, session: Any, job: Job, tool: Tool, lane: _LaneState) -> bool:
        """Admit one local job if the count caps + resource budget allow it.

        Returns ``True`` and decrements ``lane`` when the job is started.
        A request too large for the whole budget is failed (it could never
        fit); a request that simply doesn't fit *right now* is left queued.
        """
        rr = _resource_request_for(tool)
        is_gpu = tool.requires_gpu

        # Over-large guard: a request that can't fit even on an idle host
        # would sit queued forever. Fail it with a clear reason.
        if rr.cpu_cores > lane.cpu_budget or rr.memory_mb > lane.mem_budget:
            await self._fail_job(
                session,
                job,
                reason=(
                    f"Job needs {rr.cpu_cores} core(s) and {rr.memory_mb} MB, but the local "
                    f"runner budget is {lane.cpu_budget} core(s) / {lane.mem_budget} MB. Lower "
                    "the tool's resource request or raise the runner's local CPU / memory budget."
                ),
            )
            return False

        # Lane capacity. A full lane is a skip, not a stop — a job in a
        # different lane queued behind it still gets a look this tick.
        if (
            lane.local_slots <= 0
            or (is_gpu and lane.gpu_slots <= 0)
            or lane.cpu_used + rr.cpu_cores > lane.cpu_budget
            or lane.mem_used + rr.memory_mb > lane.mem_budget
        ):
            return False

        if not await self._pickup_job(session, job):
            return False

        lane.local_slots -= 1
        lane.cpu_used += rr.cpu_cores
        lane.mem_used += rr.memory_mb
        if is_gpu:
            lane.gpu_slots -= 1
        return True

    async def _pickup_job(self, session: Any, job: Job) -> bool:
        """Submit a single queued job; return True if state advanced to running."""
        tool = await self._load_tool_for_job(session, job)
        if tool is None:
            await self._fail_job(
                session, job, reason=f"Tool {job.tool_alias!r} is no longer installed."
            )
            return False
        try:
            target = self.registry.resolve(_kind_for(tool))
        except UnsupportedTargetError as e:
            await self._fail_job(session, job, reason=str(e))
            return False

        staged = await self._resolve_staged_working_dir(session, job)
        if staged is None:
            await self._fail_job(
                session,
                job,
                reason=(
                    "Job has no staged working directory. "
                    "Re-submit the job; staging may have failed."
                ),
            )
            return False

        try:
            submit_result = await target.submit(
                staged=staged,
                resource_request=_resource_request_for(tool),
                context=_context_for(tool, job),
            )
        except Exception as e:
            await self._fail_job(session, job, reason=f"Failed to start the job: {e}")
            return False

        await self._mark_running(session, job, submit_result)
        return True

    async def _heartbeat_once(self) -> None:
        """Poll every running job whose per-target interval has elapsed.

        **P1**: HPC jobs are gated by ``heartbeat_hpc_running_interval_s``
        (or ``..._queued_interval_s`` while the launcher hasn't yet
        written ``running.json``); local/AOS jobs by
        ``heartbeat_local_aos_interval_s``. The loop itself wakes on the
        local/AOS interval so HPC ticks are decided on a per-job basis,
        not by slowing the whole loop down.
        """
        async with session_scope(self.factory) as session:
            stmt = select(Job).where(Job.state == "running")
            running = list((await session.execute(stmt)).scalars())

            active_ids = {job.id for job in running}
            for stale_id in [jid for jid in self._poll_state if jid not in active_ids]:
                del self._poll_state[stale_id]

            now_mono = asyncio.get_running_loop().time()
            for job in running:
                # Keep the id as a plain int. A rolled-back savepoint expires
                # every attribute on ``job``, so reading ``job.id`` in the
                # handlers below would fire a lazy refresh — sync IO on the
                # greenlet-less path, which raises MissingGreenlet *out of*
                # the except clause and takes the whole cycle down with it.
                job_id = job.id
                tool = await self._load_tool_for_job(session, job)
                if tool is None:
                    # Tool row vanished from under a running job. Don't
                    # advance the canonical state here — pickup's
                    # missing-tool guard handles the queued case; for a
                    # running job we can't know whether the launcher is
                    # still running on the remote. Leave the row alone.
                    continue
                interval = self._heartbeat_interval_for(tool, job)
                state = self._poll_state.setdefault(job_id, _PollState())
                if state.last_polled_at and (now_mono - state.last_polled_at) < interval:
                    continue
                state.last_polled_at = now_mono
                # Isolate each job in its own savepoint so one poisoned poll
                # can't abort the cycle for its siblings (session_scope rolls
                # back + re-raises on any exception). A job row can also be
                # deleted out from under us mid-poll — e.g. its parent study is
                # removed, cascading the delete — which surfaces as a
                # StaleDataError on the fail-flush; treat that as benign.
                try:
                    async with session.begin_nested():
                        await self._poll_heartbeat_for(session, job, tool=tool, poll_state=state)
                except StaleDataError:
                    _log.info(
                        "Job %s vanished mid-heartbeat (deleted concurrently); dropping it",
                        job_id,
                    )
                    self._poll_state.pop(job_id, None)
                except Exception:
                    _log.exception(
                        "Heartbeat poll for job %s raised; skipping it this cycle", job_id
                    )
                    # The savepoint rolled back, but the failed flush left the
                    # instance dirty/expired in the *outer* session. Evict it so
                    # the next statement in this cycle doesn't autoflush the
                    # same doomed UPDATE and abort a sibling's poll.
                    with contextlib.suppress(Exception):
                        session.expunge(job)

    def _heartbeat_interval_for(self, tool: Tool, job: Job) -> float:
        """Pick the per-target heartbeat interval for ``job``.

        Local + AOS jobs use the short interval. HPC jobs use the long
        running interval once the launcher has written its first
        heartbeat, and the longer queued interval before that — most
        clusters dislike sub-second polling on queued ids.
        """
        try:
            kind = _kind_for(tool)
        except UnsupportedTargetError:
            return self.settings.heartbeat_local_aos_interval_s
        if kind != "hpc":
            return self.settings.heartbeat_local_aos_interval_s
        if job.heartbeat_state == "running":
            return self.settings.heartbeat_hpc_running_interval_s
        return self.settings.heartbeat_hpc_queued_interval_s

    async def _poll_heartbeat_for(
        self,
        session: Any,
        job: Job,
        *,
        tool: Tool,
        poll_state: _PollState,
    ) -> None:
        try:
            target = self.registry.resolve(_kind_for(tool))
        except UnsupportedTargetError:
            return

        staged = await self._resolve_staged_working_dir(session, job)
        if staged is None:
            # **B2**: bounded retry. The shared mount may be briefly
            # unreachable (NFS reconnect, FSx failover); after the
            # configured number of consecutive misses we declare the
            # working directory lost and fail the job rather than
            # leaving it in ``running`` forever.
            poll_state.consecutive_missing_staging += 1
            if poll_state.consecutive_missing_staging >= _MAX_CONSECUTIVE_MISSING_STAGING_POLLS:
                await self._fail_job(
                    session,
                    job,
                    reason=(
                        "Lost contact with the storage location for this job. "
                        "Check that the shared mount is reachable from the runner host, "
                        "then re-submit the job."
                    ),
                )
                self._poll_state.pop(job.id, None)
            return
        poll_state.consecutive_missing_staging = 0

        observation = await target.poll(staged=staged, context=_context_for(tool, job))
        if observation.heartbeat_state in (None, "pending", "running"):
            # Walltime guard: stop a runaway local job before it ties up a
            # worker forever (e.g. a solver that won't converge).
            if await self._enforce_walltime(session, job, tool=tool, target=target, staged=staged):
                return
            if observation.heartbeat_state is not None:
                job.heartbeat_state = observation.heartbeat_state
                job.heartbeat_last_seen_at = _utcnow()
            return

        run_succeeded = observation.heartbeat_state == "complete"
        run_outcome = "complete" if run_succeeded else "failed"
        job.heartbeat_state = observation.heartbeat_state
        job.heartbeat_last_seen_at = _utcnow()
        job.exit_code = observation.exit_code
        job.completed_at = _utcnow()

        # Post-approval gate (§16.15). A *successful* run of a
        # post/both-policy tool doesn't go straight to ``complete`` —
        # the outputs are withheld in ``pending_results`` until an
        # approver releases them. Cleanup still runs (the run finished;
        # ``_post_complete`` preserves outputs exactly as for a normal
        # complete), only the canonical state differs. A *failed* run
        # never enters post — there's nothing to release.
        post_request: ApprovalRequest | None = None
        if run_succeeded:
            await self._post_complete(staged, tool, session=session, job=job)
            post_request = await self._open_post_approval_if_required(session, job=job)
        else:
            # Study children get a row even on failure / cancel — empty
            # ``scalars`` so the Results matrix shows them with a null
            # response (D3 / partial-data banner; never silently dropped).
            # ``record_study_result`` is a no-op for non-study children.
            await record_study_result(
                self.audit,
                session,
                job=job,
                tool=tool,
                outputs_doc=None,
                succeeded=False,
            )
        held_for_results = post_request is not None

        job.state = "pending_results" if held_for_results else run_outcome
        await self._refresh_parent_study(session, job)
        # Headless workflow runs self-cascade: a just-completed member may
        # unblock downstream members. A held (pending_results) job is not
        # yet "complete", so advance() correctly defers the cascade until
        # release. No-op for standalone + manual-UI workflow jobs.
        await self._advance_workflow_run(session, job)

        await self.audit.record(
            session,
            RecordInput(
                actor="system:runner",
                actor_type="system",
                action=f"runner.job.state.{run_outcome}",
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                payload={
                    "exit_code": observation.exit_code,
                    "failure_reason": observation.failure_reason,
                },
            ),
        )
        if held_for_results:
            await self.audit.record(
                session,
                RecordInput(
                    actor="system:runner",
                    actor_type="system",
                    action="runner.job.pending_results",
                    resource_type="job",
                    resource_id=str(job.id),
                    outcome="success",
                    payload={"tool_alias": job.tool_alias},
                ),
            )
        # Notifications are job-lifecycle events. A failed run still
        # notifies the requestor; a normal (ungated) complete notifies
        # as before. A *held* successful run does NOT tell the
        # requestor "complete" (results aren't released yet) — instead
        # the eligible approvers are emailed that results await release.
        if held_for_results and post_request is not None:
            try:
                await self.notifier.notify_approval_request_opened(session, request=post_request)
            except Exception:
                _log.exception("approval notify raised on job %s; swallowed", job.id)
        else:
            await self._notify(session, job=job, event=run_outcome)

    async def _open_post_approval_if_required(
        self, session: Any, *, job: Job
    ) -> ApprovalRequest | None:
        """Open a ``post`` approval request when the captured policy needs one.

        Reads ``approvals_policy`` from the job's *pinned*
        ``tool_version`` snapshot (never the live tool row — a policy
        edit after submit can't change a job already in flight, same
        discipline the pre gate uses). Returns the created
        :class:`ApprovalRequest` when the job should be held in
        ``pending_results``, else ``None``.

        The snapshot is read as a plain mapping rather than parsed
        through :class:`ToolDescriptor`: run finalisation must be
        fail-safe — a snapshot that doesn't fully validate (a legacy
        or minimal descriptor) must not raise here and strand a
        finished run. Absent / non-post policy → no gate, the job
        completes normally.

        A **parametric-study or optimisation child never gets a per-child
        post gate**: the parent owns a single results-release gate, opened
        by its rollup once every child is terminal (studies
        ``refresh_state``; optimisation ``_finish``). Holding each child in
        ``pending_results`` behind its own gate would keep the parent from
        ever seeing all children terminal — so its one gate would never
        open and the approver would face N per-job decisions instead of
        one (the bug this guard fixes).
        """
        from matofx.approvals import make_approval_service  # noqa: PLC0415
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        if job.parametric_study_id is not None or job.optimisation_study_id is not None:
            return None
        # A *workflow*-target optimisation candidate runs the chain as a
        # headless WorkflowRun, so the per-member job carries no
        # ``optimisation_study_id`` (it lives on the run). Holding such a
        # member in ``pending_results`` would stall the run — and the chain,
        # and the whole campaign — behind a per-run manual approval the
        # autonomous optimiser never resolves (a workflow target has no
        # campaign-level results gate either). Recognise it via the run.
        if job.workflow_run_id is not None:
            from matofx.workflows.orm import WorkflowRun  # noqa: PLC0415

            run = await session.get(WorkflowRun, job.workflow_run_id)
            if run is not None and run.optimisation_study_id is not None:
                return None

        tool_version = await session.get(ToolVersion, job.tool_version_id)
        if tool_version is None:
            # Snapshot vanished (shouldn't happen — append-only). Fail
            # safe: don't withhold, let it complete normally.
            return None
        snapshot = tool_version.descriptor_snapshot or {}
        policy = snapshot.get("approvals_policy", "none")
        if policy not in ("post", "both"):
            return None
        raw_approvers = snapshot.get("approver_usernames") or []
        approvers = [str(u) for u in raw_approvers if isinstance(u, str)]

        approval_service = make_approval_service(self.audit)
        return await approval_service.create_request(
            session,
            job_id=job.id,
            kind="post",
            requested_by_user_id=job.created_by_user_id,
            eligible_approvers=approvers,
            tool_alias=job.tool_alias,
            tool_version_id=job.tool_version_id,
            justification="",
        )

    async def _cancel_once(self) -> None:
        async with session_scope(self.factory) as session:
            stmt = select(Job).where(
                Job.cancel_requested_at.is_not(None),
                Job.state.in_(["queued", "running"]),
            )
            requested = list((await session.execute(stmt)).scalars())
            for job in requested:
                await self._cancel_job(session, job)

    async def _cancel_job(self, session: Any, job: Job) -> None:
        tool = await self._load_tool_for_job(session, job)
        if tool is None:
            await self._fail_job(session, job, reason="Job cancelled.")
            return
        try:
            target = self.registry.resolve(_kind_for(tool))
        except UnsupportedTargetError:
            await self._fail_job(session, job, reason="Job cancelled.")
            return

        staged = await self._resolve_staged_working_dir(session, job)
        if staged is None:
            await self._fail_job(session, job, reason="Job cancelled.")
            return

        submit_result = SubmitResult(
            pid=None,
            scheduler_job_id=job.scheduler_job_id,
        )
        # ``pid`` isn't persisted on the job row in Phase 1 — local
        # cancellation walks the heartbeat-recorded PID via the launcher.
        # Read it from the heartbeat file if present.
        pid = await self._read_pid_from_heartbeat(staged)
        if pid is not None:
            submit_result = SubmitResult(pid=pid, scheduler_job_id=job.scheduler_job_id)

        try:
            await target.cancel(
                submit_result=submit_result,
                staged=staged,
                grace_seconds=self.settings.cancel_grace_s,
                context=_context_for(tool, job),
            )
        except Exception as e:
            _log.exception("Cancel raised on job %s: %s", job.id, e)

        # Drive the canonical state to failed if it isn't already
        # terminal; the heartbeat watcher will pick up the launcher's
        # final write a moment later, but we don't want to leave the
        # job hanging in ``running`` if the cancel succeeded silently.
        if job.state in ("queued", "running"):
            now = _utcnow()
            previous_state = job.state
            job.state = "failed"
            # Cancel-before-pickup case: started_at is still NULL but the
            # CHECK constraint demands it whenever completed_at is set.
            if job.started_at is None:
                job.started_at = now
            job.completed_at = now
            await self.audit.record(
                session,
                RecordInput(
                    actor=f"user:{job.cancel_requested_by_user_id or 0}",
                    actor_type="user",
                    action="runner.job.cancelled",
                    resource_type="job",
                    resource_id=str(job.id),
                    outcome="success",
                    payload={
                        "requested_at": (
                            job.cancel_requested_at.isoformat()
                            if job.cancel_requested_at is not None
                            else None
                        ),
                        # The row can only say ``failed`` (no ``cancelled`` member
                        # in JOB_STATES), so the distinction between "the tool
                        # broke" and "someone stopped it" lives here.
                        "reason": "Stopped by a cancel request.",
                        "previous": previous_state,
                    },
                ),
            )
            await self._refresh_parent_study(session, job)

    # ----- helpers ------------------------------------------------------

    async def _refresh_parent_study(self, session: Any, job: Job) -> None:
        """Roll a parametric study's state up when a child finalises.

        No-op for ordinary / workflow jobs. System-attributed (the
        runner, not a user) so the ``studies.study.complete`` audit
        event isn't tied to whoever happened to trigger the tick.
        Idempotent: :meth:`StudyService.refresh_state` only persists +
        audits on an actual state change.
        """
        if job.parametric_study_id is None:
            return
        from matofx.studies import make_study_service  # noqa: PLC0415

        service = make_study_service(self.audit)
        await service.refresh_state(session, actor_user_id=None, study_id=job.parametric_study_id)

    async def _advance_workflow_run(self, session: Any, job: Job) -> None:
        """Queue the now-ready downstream members of a headless workflow run.

        No-op for standalone jobs and for manual (UI-driven) workflow
        runs — only ``auto_advance`` runs self-cascade. When this child
        belongs to such a run, :meth:`WorkflowRunService.advance` picks up
        every sibling whose wired upstreams have just completed, resolves
        its inputs from the upstream outputs, and queues it. Idempotent;
        attributed to the run's submitter (the staging path the queued
        children inherit). A failure here must not strand the just-finished
        job, so it is logged and swallowed.
        """
        if job.workflow_run_id is None:
            return
        from matofx.workflows.orm import WorkflowRun  # noqa: PLC0415
        from matofx.workflows.run_service import make_workflow_run_service  # noqa: PLC0415

        run = await session.get(WorkflowRun, job.workflow_run_id)
        if run is None or not run.auto_advance:
            return
        service = make_workflow_run_service(self.audit, environment=self.environment)
        try:
            await service.advance(session, run_id=run.id, actor_user_id=run.submitted_by_user_id)
        except Exception:
            _log.exception(
                "workflow auto-advance raised on run %s (job %s); swallowed",
                run.id,
                job.id,
            )

    async def _load_tool_for_job(self, session: Any, job: Job) -> Tool | None:
        stmt = select(Tool).where(Tool.alias == job.tool_alias)
        result = (await session.execute(stmt)).scalar_one_or_none()
        return result if isinstance(result, Tool) else None

    async def _load_tools_by_alias(self, session: Any, aliases: set[str]) -> dict[str, Tool]:
        """Batch-load tool rows for ``aliases`` → ``{alias: Tool}``.

        Keeps the pickup loop's lane classification to a single query
        instead of one per running + queued job. Aliases with no live tool
        row are simply absent from the dict (the caller routes those to the
        clean per-job failure path).
        """
        if not aliases:
            return {}
        rows = (await session.execute(select(Tool).where(Tool.alias.in_(aliases)))).scalars()
        return {t.alias: t for t in rows}

    async def _resolve_staged_working_dir(self, session: Any, job: Job) -> StagedWorkingDir | None:
        if job.storage_location_id is None or job.working_dir_relative is None:
            return None
        location = await session.get(StorageLocation, job.storage_location_id)
        if location is None:
            return None
        from pathlib import Path  # noqa: PLC0415 — local import keeps the seam tight

        wd: Path = Path(location.mount_path) / job.working_dir_relative
        return StagedWorkingDir(
            working_dir=wd,
            inputs_dir=wd / INPUTS_DIRNAME,
            outputs_dir=wd / OUTPUTS_DIRNAME,
            logs_dir=wd / LOGS_DIRNAME,
            heartbeat_dir=wd / HEARTBEAT_DIRNAME,
            inputs_json_path=wd / INPUTS_DIRNAME / "inputs.json",
            launcher_path=wd / "launcher.py",
        )

    async def _read_pid_from_heartbeat(self, staged: StagedWorkingDir) -> int | None:
        import json  # noqa: PLC0415 — local import to keep dependencies obvious

        for filename in ("running.json", "complete.json", "failed.json"):
            path = staged.heartbeat_dir / filename
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pid = payload.get("pid")
            if isinstance(pid, int):
                return pid
        return None

    async def _mark_running(self, session: Any, job: Job, submit_result: SubmitResult) -> None:
        previous = job.state
        job.state = "running"
        job.started_at = _utcnow()
        if submit_result.scheduler_job_id is not None:
            job.scheduler_job_id = submit_result.scheduler_job_id
        await self.audit.record(
            session,
            RecordInput(
                actor="system:runner",
                actor_type="system",
                action="runner.job.state.running",
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                payload={"previous": previous, "scheduler_job_id": submit_result.scheduler_job_id},
            ),
        )
        await self._notify(session, job=job, event="running")

    async def _enforce_walltime(
        self,
        session: Any,
        job: Job,
        *,
        tool: Tool,
        target: ExecutionTarget,
        staged: StagedWorkingDir,
    ) -> bool:
        """Stop + fail a local job that has exceeded its walltime limit.

        Effective limit = the tool descriptor's ``walltime_minutes`` when set,
        else the install-wide ``local_job_walltime_minutes`` setting (0 = no
        limit). Only ``local`` jobs are governed here — AOS / HPC honour their
        own scheduler walltime. Returns ``True`` when the job was killed so the
        caller stops processing this poll.
        """
        if tool.execution_target != "local" or job.started_at is None:
            return False
        rr = tool.resource_request if isinstance(tool.resource_request, dict) else {}
        tool_limit = rr.get("walltime_minutes")
        limit = (
            int(tool_limit)
            if isinstance(tool_limit, int) and not isinstance(tool_limit, bool) and tool_limit > 0
            else self.settings.local_job_walltime_minutes
        )
        if limit <= 0:
            return False
        # ``started_at`` round-trips from SQLite as a naive datetime; treat it
        # as UTC so the subtraction with the aware ``_utcnow()`` doesn't raise.
        started = job.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=_dt.UTC)
        elapsed_min = (_utcnow() - started).total_seconds() / 60.0
        if elapsed_min <= limit:
            return False
        # Over the limit — terminate the launcher's subprocess, then fail.
        pid = await self._read_pid_from_heartbeat(staged)
        try:
            await target.cancel(
                submit_result=SubmitResult(pid=pid, scheduler_job_id=job.scheduler_job_id),
                staged=staged,
                grace_seconds=self.settings.cancel_grace_s,
                context=_context_for(tool, job),
            )
        except Exception:
            _log.exception("Walltime kill: cancel raised on job %s", job.id)
        await self._fail_job(
            session,
            job,
            reason=(
                f"Stopped after exceeding the {limit}-minute walltime limit "
                f"(ran {elapsed_min:.0f} min). Raise the limit or narrow the inputs."
            ),
        )
        self._poll_state.pop(job.id, None)
        return True

    async def _fail_job(self, session: Any, job: Job, *, reason: str) -> None:
        # The job row can be deleted out from under a running/queued job (e.g.
        # its parent study is removed, cascading the delete). Confirm it still
        # exists BEFORE mutating + flushing, so a vanished row is a clean no-op
        # rather than a StaleDataError that aborts the runner's work cycle. The
        # heartbeat loop wraps each poll in a savepoint as a backstop for the
        # residual race between this check and the fail-flush.
        if await session.scalar(select(Job.id).where(Job.id == job.id)) is None:
            _log.info(
                "Job %s no longer exists (deleted concurrently); skipping fail", job.id
            )
            return
        previous = job.state
        now = _utcnow()
        job.state = "failed"
        # The job CHECK constraint requires started_at <= completed_at
        # AND started_at IS NOT NULL whenever completed_at is set. A job
        # failed before pickup never started, so we record start and end
        # at the same instant — the "ran for zero seconds" case.
        if job.started_at is None:
            job.started_at = now
        job.completed_at = now
        await self.audit.record(
            session,
            RecordInput(
                actor="system:runner",
                actor_type="system",
                action="runner.job.state.failed",
                resource_type="job",
                resource_id=str(job.id),
                outcome="success",
                payload={"previous": previous, "reason": reason},
            ),
        )
        await self._notify(session, job=job, event="failed")
        await self._refresh_parent_study(session, job)

    async def _notify(self, session: Any, *, job: Job, event: str) -> None:
        """Fire-and-forget dispatch through the configured notifier.

        SMTP failures are swallowed by the notifier itself; the wider
        try-except here is belt-and-braces so a notifier-level bug
        can't take a state transition with it.
        """
        try:
            await self.notifier.notify_job_state_changed(
                session,
                job=job,
                event=event,  # type: ignore[arg-type]  # event is one of running/complete/failed
            )
        except Exception:
            _log.exception("Notifier raised on job %s event %s; swallowed", job.id, event)

    async def _post_complete(
        self,
        staged: StagedWorkingDir,
        tool: Tool,
        *,
        session: Any,
        job: Job,
    ) -> None:
        """On successful run: parse outputs.json, record study result, apply cleanup rules.

        The results write happens **before** cleanup so the persisted
        scalars + retained-file refs survive ``clean_directory``
        regardless of the descriptor's ``retain_files``
        (ADR-0001 §10.0 / ADR-0003). ``record_study_result`` is a
        no-op for non-study children (the early return inside).
        """
        outputs_doc = await asyncio.to_thread(_read_outputs_document, staged)
        await record_study_result(
            self.audit,
            session,
            job=job,
            tool=tool,
            outputs_doc=outputs_doc,
            succeeded=True,
        )
        # KPI extraction (ADR-0012, slice 3). Runs after the per-study
        # results write so a study child's matrix lands first; runs
        # before cleanup so ``outputs.json`` is still on disk to read.
        # Best-effort: ``extract_kpis_for_job`` never raises (the
        # SAVEPOINT inside guards the outer transaction).
        await extract_kpis_for_job(self.audit, session, job=job, outputs_doc=outputs_doc)
        await asyncio.to_thread(_apply_cleanup_for_tool, staged.working_dir, tool)


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


def _apply_cleanup_for_tool(working_dir: Path, tool: Tool) -> None:
    descriptor = tool.descriptor or {}
    retain = descriptor.get("retain_files", []) if isinstance(descriptor, dict) else []
    delete = descriptor.get("delete_io_files", []) if isinstance(descriptor, dict) else []
    outputs = descriptor.get("outputs", []) if isinstance(descriptor, dict) else []
    if not isinstance(retain, list):
        retain = []
    if not isinstance(delete, list):
        delete = []
    if not isinstance(outputs, list):
        outputs = []
    # Every descriptor-declared file output joins the retention floor so a
    # tool's promised results survive cleanup even when the author lists
    # nothing extra in the keep-list (``retain_files``). This subsumes the
    # earlier ADR-0003 ``csv_plot`` guarantee (Results explorer reads them)
    # and generalises it to all ``type: file`` outputs.
    file_output_paths = [
        out["filename"]
        for out in outputs
        if isinstance(out, dict)
        and out.get("type") == "file"
        and isinstance(out.get("filename"), str)
        and out.get("filename")
    ]
    # A time-resolved ``field`` output (``field_timesteps`` glob) protects
    # the whole series (ADR-0008 §4.4), expanded against the on-disk working
    # dir at cleanup time — beyond the single representative ``filename``.
    field_paths = _field_retained_paths(working_dir, outputs)
    apply_cleanup(
        working_dir=working_dir,
        retain_files=[r for r in retain if isinstance(r, str)],
        delete_io_files=[d for d in delete if isinstance(d, str)],
        extra_protected=[*file_output_paths, *field_paths],
    )


def _field_retained_paths(working_dir: Path, outputs: list[Any]) -> list[str]:
    """Relative paths of every descriptor ``field`` snapshot to retain.

    The representative ``filename`` plus, when the output is time-resolved,
    every file matching its ``field_timesteps`` glob (expanded against the
    finished working dir). Returns working-dir-relative POSIX-ish strings so
    they merge straight into the cleanup floor.
    """
    paths: list[str] = []
    for out in outputs:
        if not isinstance(out, dict) or out.get("file_type") != "field":
            continue
        filename = out.get("filename")
        if isinstance(filename, str) and filename:
            paths.append(filename)
        timesteps = out.get("field_timesteps")
        if isinstance(timesteps, str) and timesteps:
            for match in working_dir.glob(timesteps):
                try:
                    paths.append(match.relative_to(working_dir).as_posix())
                except ValueError:  # pragma: no cover - glob escaped working dir
                    continue
    return paths


def _context_for(tool: Tool, job: Job) -> TargetContext:
    """Build the per-job dispatch context from the Tool and Job rows.

    Local tools have no host info; AOS / HPC tools carry the
    relevant id so the target instance can route to the right
    server / cluster on its session pool. ``scheduler_job_id`` is
    threaded through from the job row so HPC poll can talk to the
    scheduler about the in-flight job (it's ``None`` until submit
    has assigned it).
    """
    return TargetContext(
        aos_host_id=tool.aos_host_id,
        hpc_cluster_id=tool.hpc_cluster_id,
        scheduler_job_id=job.scheduler_job_id,
    )


def _declared_resources(tool: Tool) -> dict[str, Any]:
    """The ``resources`` block from the tool's own descriptor.

    Tolerant on purpose: a descriptor that predates a field, or a row
    whose JSON is not the shape we expect, yields ``{}`` and the caller
    falls back to defaults rather than failing a submission.
    """
    descriptor = tool.descriptor
    if not isinstance(descriptor, dict):
        return {}
    declared = descriptor.get("resources")
    return declared if isinstance(declared, dict) else {}


def _resource_request_for(tool: Tool) -> ResourceRequest:
    """Resolve what to ask the scheduler for: descriptor first, override on top.

    Two sources, and the precedence matters. The **descriptor** is what
    the tool's author declared and is the normal answer.
    ``Tool.resource_request`` is an **operator override** — nullable, and
    populated only by the cross-install ``migrate`` import or by hand —
    so where it names a field it wins, and where it does not the
    descriptor shows through.

    Until 2026-08-13 only the override was read, and nothing on the
    normal install path ever populates it. So every HPC job went out as
    ``--cpus-per-task=1 --mem=512M`` with no ``--time``, whatever the
    descriptor said (D-11). A tool declaring 16 cores got one; a tool
    declaring 64 GB got 512 MB and was OOM-killed, which reads as the
    tool's fault. Found on ENV-A because ``sacct`` reports what was
    actually requested rather than what we believed we sent.

    The two shapes use different key names and both are kept, because
    they mean different things to different audiences:

    ==================  ======================  ============
    Descriptor          Override                Emitted as
    ==================  ======================  ============
    ``cpu_cores``       ``cores``               ``--cpus-per-task``
    ``ram_mb``          ``memory_mb``           ``--mem``
    ``walltime_minutes````walltime_minutes``    ``--time``
    ``gpu_memory_mb``   ``gpu_count``           ``--gres=gpu:N``
    --                  ``queue_override``      ``--partition``
    ==================  ======================  ============

    GPU is the one lossy mapping: the descriptor states how much GPU
    *memory* a tool needs, while admission and ``--gres`` are by whole
    devices. Only the sign is used — any positive requirement asks for
    one GPU — which matches how the scheduler already routes GPU work.
    An override naming ``gpu_count`` says exactly what it means and wins.
    """
    declared = _declared_resources(tool)
    override = tool.resource_request if isinstance(tool.resource_request, dict) else {}

    def _int(source: dict[str, Any], key: str) -> int | None:
        value = source.get(key)
        # bool is an int in Python; a stray `true` must not become 1 core.
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    cpu_cores = _int(override, "cores") or _int(declared, "cpu_cores") or 1
    memory_mb = _int(override, "memory_mb") or _int(declared, "ram_mb") or 512

    walltime = _int(override, "walltime_minutes")
    if walltime is None:
        walltime = _int(declared, "walltime_minutes")

    gpu_count = _int(override, "gpu_count")
    if gpu_count is None:
        gpu_memory_mb = _int(declared, "gpu_memory_mb") or 0
        gpu_count = 1 if gpu_memory_mb > 0 else 0

    return ResourceRequest(
        cpu_cores=cpu_cores,
        memory_mb=memory_mb,
        walltime_minutes=walltime,
        gpu_count=gpu_count,
        queue_override=(
            str(override["queue_override"])
            if isinstance(override.get("queue_override"), str)
            else None
        ),
    )


def _read_outputs_document(staged: StagedWorkingDir) -> OutputsDocument | None:
    import json  # noqa: PLC0415 — keeps the IO at the seam

    path = staged.outputs_dir / "outputs.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return OutputsDocument.model_validate(payload)
    except ValueError:
        return None


# Re-export for the external callers that need them but don't want
# to reach into matofx.tools.exec.
__all__ = ["RunnerSupervisor"]


# Internal helpers used by tests; not part of the public surface.
_internal_resource_request_for = _resource_request_for
_internal_read_outputs_document = _read_outputs_document
_internal_context_for = _context_for


# silence unused-import on ExecutionTargetKind import (it's part of
# the supervisor's public method signatures via the registry)
_unused: ExecutionTargetKind | None = None
