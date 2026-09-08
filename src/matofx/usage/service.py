"""Read-only aggregation service behind the Usage Statistics surface.

The Usage Statistics page (``Admin → Usage Statistics``, gated by
``usage.view``) summarises how the install is being used. This service
is its data layer: it reads the ``job`` / ``tool`` / ``user`` / ``team``
/ ``role`` tables and returns aggregate statistics — never individual
rows, never anything mutating.

Each tab is one service method over the shared :meth:`UsageService._fetch_jobs`
helper: ``overview`` (KPIs + stacked time series + tool leaderboard),
``compute`` (core/GPU-hours by tool/team, runtime distribution, queue
wait, throughput), and ``reliability`` (success rate + trend, top failing
tools, exit codes, and the approval funnel). Adoption, seats, and
infrastructure follow in later slices.

Compute consumption (core-hours / GPU-hours) multiplies a job's wall-
clock runtime by the *tool's* configured ``resource_request`` (cores /
gpu_count). That is an approximation — the resource request is tool
metadata captured on the tool row, not a per-job measurement — but it
is the best signal available without a scheduler-accounting feed, and
it is the same number the operator tuned on the Edit Tool form.
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.approvals.orm import ApprovalRequest
from matofx.hierarchy.orm import Job
from matofx.tools.orm import Tool
from matofx.users.orm import AuthSession, User, UserLogonEvent
from matofx.users.role_orm import Role
from matofx.users.team_orm import Team, UserTeam

if TYPE_CHECKING:
    from matofx.config.models import Settings

# Tool execution targets (frozen vocabulary, mirrors the ``tool``
# table's ``ck_tool_execution_target`` CHECK). Used to populate the
# filter-bar dropdown without a DB round-trip.
EXECUTION_TARGETS: tuple[str, ...] = ("local", "aos", "hpc")

# Terminal run states that count toward the success-rate denominator.
_SUCCESS_STATE = "complete"
_FAILURE_STATE = "failed"

# How many tools the Overview leaderboard shows.
_TOP_TOOLS_LIMIT = 8

# How many users the People-tab "by user" leaderboard shows.
_PEOPLE_LIMIT = 15


@dataclass(frozen=True)
class JobFilters:
    """The shared filter selection driving every Usage Statistics query.

    ``since`` / ``until`` bound ``job.created_at`` (inclusive). The
    remaining fields are multi-select narrowings stored as tuples (so
    the dataclass stays frozen / hashable); an **empty tuple means
    "all"**. A non-empty tuple matches any of its members (SQL ``IN``).
    """

    since: _dt.datetime
    until: _dt.datetime
    tool_aliases: tuple[str, ...] = ()
    team_ids: tuple[int, ...] = ()
    roles: tuple[str, ...] = ()
    execution_targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Kpi:
    """One headline number plus its period-over-period comparison.

    ``value`` is the current-window figure (``None`` when undefined,
    e.g. a success rate with no terminal jobs). ``previous`` is the same
    figure for the immediately-preceding window of equal length;
    ``delta_pct`` is the percentage change, ``None`` when it can't be
    computed (no prior value, or a prior value of zero).
    """

    value: float | None
    previous: float | None = None
    delta_pct: float | None = None


@dataclass(frozen=True)
class StatePoint:
    """One (day, job-state) bucket in the stacked time series."""

    date: _dt.date
    state: str
    count: int


@dataclass(frozen=True)
class ToolCount:
    """A tool's job count, for the leaderboard."""

    tool_alias: str
    tool_name: str
    count: int


@dataclass(frozen=True)
class OverviewStats:
    """Everything the Overview tab renders for one filter selection."""

    jobs_total: Kpi
    active_users: Kpi
    success_rate: Kpi
    core_hours: Kpi
    gpu_hours: Kpi
    registered_users: int
    by_state: dict[str, int]
    series: list[StatePoint]
    top_tools: list[ToolCount]


@dataclass(frozen=True)
class ToolOption:
    """A selectable tool in the filter bar."""

    alias: str
    name: str


@dataclass(frozen=True)
class TeamOption:
    """A selectable team in the filter bar."""

    id: int
    name: str


@dataclass(frozen=True)
class FilterOptions:
    """The option lists the filter bar's dropdowns are built from."""

    tools: list[ToolOption]
    teams: list[TeamOption]
    roles: list[str]
    execution_targets: list[str]


@dataclass(frozen=True)
class ToolConsumption:
    """Compute consumed by one tool over the window."""

    tool_alias: str
    tool_name: str
    core_hours: float
    gpu_hours: float
    jobs: int


@dataclass(frozen=True)
class TeamConsumption:
    """Compute consumed by one team over the window.

    A job is attributed to every team its owner belongs to (legacy
    "explode teams" behaviour), so the per-team figures can sum to more
    than the install total when users sit in multiple teams. ``team_id``
    is ``None`` for the synthetic "(No team)" bucket.
    """

    team_id: int | None
    team_name: str
    core_hours: float
    gpu_hours: float
    jobs: int


@dataclass(frozen=True)
class RuntimeDist:
    """Wall-clock runtime distribution (hours) for one tool."""

    tool_alias: str
    tool_name: str
    p50_hours: float
    p95_hours: float
    max_hours: float
    runs: int


@dataclass(frozen=True)
class DayValue:
    """A single (day, value) point — used for the queue-wait trend."""

    date: _dt.date
    value: float


@dataclass(frozen=True)
class ThroughputPoint:
    """Jobs submitted vs completed on one day."""

    date: _dt.date
    submitted: int
    completed: int


@dataclass(frozen=True)
class ComputeStats:
    """Everything the Compute & Throughput tab renders."""

    by_tool: list[ToolConsumption]
    by_team: list[TeamConsumption]
    runtime: list[RuntimeDist]
    queue_wait: list[DayValue]
    throughput: list[ThroughputPoint]


@dataclass(frozen=True)
class FailingTool:
    """A tool's failure record over the window."""

    tool_alias: str
    tool_name: str
    failures: int
    failure_rate: float
    """Percent: failures / (complete + failed) for this tool."""
    last_failed_at: _dt.datetime | None


@dataclass(frozen=True)
class ExitCodeCount:
    """How many jobs ended with a given process exit code."""

    exit_code: int
    count: int


@dataclass(frozen=True)
class ApprovalKindFunnel:
    """Approval-request outcomes for one gate kind (``pre`` / ``post``)."""

    kind: str
    requested: int
    approved: int
    denied: int
    pending: int


@dataclass(frozen=True)
class ReliabilityStats:
    """Everything the Reliability tab renders."""

    success_rate: Kpi
    success_trend: list[DayValue]
    top_failing: list[FailingTool]
    exit_codes: list[ExitCodeCount]
    approval_funnel: list[ApprovalKindFunnel]


@dataclass(frozen=True)
class NamedCount:
    """A display label and its job count (People-tab breakdowns)."""

    name: str
    count: int


@dataclass(frozen=True)
class PeopleStats:
    """Everything the People & Adoption tab renders.

    ``peak_concurrency`` is the maximum number of overlapping auth
    sessions within the window (a proxy for concurrent users — a user
    with two browsers counts twice). The job breakdowns count each job
    once per user / role, and once per *each* of the owner's teams.
    """

    active_users: Kpi
    new_users: Kpi
    peak_concurrency: int
    registered_users: int
    active_trend: list[DayValue]
    by_user: list[NamedCount]
    by_team: list[NamedCount]
    by_role: list[NamedCount]


@dataclass(frozen=True)
class LogonEventPoint:
    """Logon-access grants vs revocations on one day."""

    date: _dt.date
    granted: int
    revoked: int


@dataclass(frozen=True)
class SeatsStats:
    """Everything the Seats & Licence tab renders.

    ``cap`` is the licensed entitlement, filled from the signed licence
    manifest (ADR-0019 slice 7) — ``None`` on an install with no usable
    licence, where the UI shows "—".

    ``licence_type`` matters for *reading* the cap, because the two
    types measure different things: under **CSL** the cap governs
    ``peak_concurrency``, under **FAL** it governs ``enabled_seats``.
    A tab that showed one number against the other would be quietly
    wrong, so it is surfaced and the UI says which line the cap applies
    to.
    """

    registered_users: int
    enabled_seats: int
    peak_concurrency: int
    cap: int | None
    logon_events: list[LogonEventPoint]
    concurrency_trend: list[DayValue]
    licence_type: str | None = None
    licence_state: str | None = None


def _licence_entitlement(settings: Settings | None) -> tuple[int | None, str | None, str | None]:
    """``(cap, licence_type, state)`` from the installed licence.

    All ``None`` when there is no usable licence — the service stays
    usable (and testable) without one, and the tab shows "—" rather than
    inventing an entitlement. Never raises: a usage tab must not be the
    thing that surfaces a licence problem.
    """
    if settings is None:
        return (None, None, None)
    try:
        from matofx.licence import resolve_licence  # noqa: PLC0415

        resolution = resolve_licence(settings)
    except Exception:  # pragma: no cover - defensive; usage must not break
        return (None, None, None)
    if resolution.status is None:
        return (None, None, None)
    manifest = resolution.status.manifest
    return (manifest.n_slots, manifest.licence_type, resolution.status.state)
@dataclass(frozen=True)
class TargetUsage:
    """Jobs + compute on one execution target (local / aos / hpc)."""

    target: str
    jobs: int
    core_hours: float
    gpu_hours: float


@dataclass(frozen=True)
class InfraStats:
    """Everything the Infrastructure tab renders."""

    by_target: list[TargetUsage]
    cpu_jobs: int
    gpu_jobs: int
    cpu_core_hours: float
    gpu_core_hours: float


@dataclass(frozen=True)
class _JobRow:
    """One job, denormalised with the tool fields the aggregates need."""

    state: str
    created_at: _dt.datetime
    started_at: _dt.datetime | None
    completed_at: _dt.datetime | None
    tool_alias: str
    tool_name: str
    cores: float
    gpu_count: float
    user_id: int
    exit_code: int | None = None
    execution_target: str = "local"


@dataclass(frozen=True)
class _RawKpis:
    """The window-summary scalars computed from a list of job rows."""

    jobs_total: int
    active_users: int
    success_rate: float | None
    core_hours: float
    gpu_hours: float


@dataclass(frozen=True)
class UsageService:
    """Aggregates usage analytics from the read model. Stateless."""

    async def overview(self, session: AsyncSession, *, filters: JobFilters) -> OverviewStats:
        """Compute the Overview tab's KPIs, time series, and leaderboard.

        Fetches the filtered jobs for the current window and the
        immediately-preceding window of equal length (for the deltas),
        then derives every figure in Python so the per-tool / per-state
        groupings and the resource-weighted core-hours share one pass.
        """
        filters = await self._normalize_team_filter(session, filters)
        rows = await self._fetch_jobs(session, filters)
        prior_rows = await self._fetch_jobs(session, _shift_window(filters))

        current = _aggregate(rows)
        previous = _aggregate(prior_rows)
        registered = await self._registered_users(session)

        return OverviewStats(
            jobs_total=_kpi(float(current.jobs_total), float(previous.jobs_total)),
            active_users=_kpi(float(current.active_users), float(previous.active_users)),
            success_rate=_kpi(current.success_rate, previous.success_rate),
            core_hours=_kpi(current.core_hours, previous.core_hours),
            gpu_hours=_kpi(current.gpu_hours, previous.gpu_hours),
            registered_users=registered,
            by_state=_by_state(rows),
            series=_series(rows),
            top_tools=_top_tools(rows),
        )

    async def compute(self, session: AsyncSession, *, filters: JobFilters) -> ComputeStats:
        """Compute the Compute & Throughput tab's figures.

        Builds on the same :meth:`_fetch_jobs` rows as the Overview tab:
        consumption (core / GPU-hours + job counts) by tool and by team,
        the per-tool runtime distribution (p50 / p95 / max), the daily
        queue-wait trend, and submitted-vs-completed throughput.
        """
        filters = await self._normalize_team_filter(session, filters)
        rows = await self._fetch_jobs(session, filters)
        user_teams = await self._user_teams(session)
        return ComputeStats(
            by_tool=_consumption_by_tool(rows),
            by_team=_consumption_by_team(rows, user_teams),
            runtime=_runtime_distribution(rows),
            queue_wait=_queue_wait_series(rows),
            throughput=_throughput_series(rows),
        )

    async def reliability(self, session: AsyncSession, *, filters: JobFilters) -> ReliabilityStats:
        """Compute the Reliability tab's figures.

        Success rate (overall + daily trend), the top failing tools, the
        process exit-code breakdown — all from the shared
        :meth:`_fetch_jobs` rows — plus the approval funnel, which is a
        separate query over ``approval_request`` (the same filter
        dimensions applied to ``requested_at`` / ``tool_alias`` /
        requestor).
        """
        filters = await self._normalize_team_filter(session, filters)
        rows = await self._fetch_jobs(session, filters)
        prior_rows = await self._fetch_jobs(session, _shift_window(filters))
        funnel = await self._approval_funnel(session, filters)
        return ReliabilityStats(
            success_rate=_kpi(_success_rate(rows), _success_rate(prior_rows)),
            success_trend=_success_trend(rows),
            top_failing=_top_failing(rows),
            exit_codes=_exit_code_counts(rows),
            approval_funnel=funnel,
        )

    async def people(self, session: AsyncSession, *, filters: JobFilters) -> PeopleStats:
        """Compute the People & Adoption tab's figures.

        Active users + per-day trend and the by-user / by-team / by-role
        job breakdowns come from the shared :meth:`_fetch_jobs` rows;
        new-user counts and peak concurrency are separate queries
        (``user`` create dates and overlapping ``auth_session`` rows).
        """
        filters = await self._normalize_team_filter(session, filters)
        rows = await self._fetch_jobs(session, filters)
        prior_rows = await self._fetch_jobs(session, _shift_window(filters))
        directory = await self._user_directory(session)
        user_teams = await self._user_teams(session)
        registered = await self._registered_users(session)
        new_users = await self._new_users(session, filters)
        prior_new = await self._new_users(session, _shift_window(filters))
        intervals = await self._session_intervals(session, filters)
        return PeopleStats(
            active_users=_kpi(
                float(len({r.user_id for r in rows})),
                float(len({r.user_id for r in prior_rows})),
            ),
            new_users=_kpi(float(new_users), float(prior_new)),
            peak_concurrency=_peak_concurrency(intervals),
            registered_users=registered,
            active_trend=_active_users_trend(rows),
            by_user=_jobs_by_user(rows, directory),
            by_team=_jobs_by_team_named(rows, user_teams),
            by_role=_jobs_by_role(rows, directory),
        )

    async def seats(
        self,
        session: AsyncSession,
        *,
        filters: JobFilters,
        settings: Settings | None = None,
    ) -> SeatsStats:
        """Compute the Seats & Licence tab's figures.

        Enabled seats = currently-active accounts. Peak concurrency +
        the daily trend come from the auth sessions overlapping the
        window. ``cap`` comes from the installed licence when there is a
        usable one (ADR-0019 slice 7). The logon-events series reads the
        append-only ``user_logon_event`` history within the window.
        """
        filters = await self._normalize_team_filter(session, filters)
        intervals = await self._session_intervals(session, filters)
        cap, licence_type, licence_state = _licence_entitlement(settings)
        return SeatsStats(
            registered_users=await self._registered_users(session),
            enabled_seats=await self._enabled_seats(session),
            peak_concurrency=_peak_concurrency(intervals),
            cap=cap,
            licence_type=licence_type,
            licence_state=licence_state,
            logon_events=await self._logon_events(session, filters),
            concurrency_trend=_peak_concurrency_by_day(intervals, filters.since, filters.until),
        )

    async def infra(self, session: AsyncSession, *, filters: JobFilters) -> InfraStats:
        """Compute the Infrastructure tab's figures (target + CPU/GPU mix)."""
        filters = await self._normalize_team_filter(session, filters)
        rows = await self._fetch_jobs(session, filters)
        return _infra_stats(rows)

    async def _approval_funnel(
        self, session: AsyncSession, filters: JobFilters
    ) -> list[ApprovalKindFunnel]:
        """Approval-request counts by (kind, status) for the filter window.

        Mirrors the job filters: ``requested_at`` window, tool alias,
        execution target (via the tool), and requestor team / role
        (user-id subqueries). Returns one entry per gate kind that has
        any requests, ``pre`` before ``post``.
        """
        stmt = select(
            ApprovalRequest.kind,
            ApprovalRequest.status,
            func.count(ApprovalRequest.id),
        ).where(
            ApprovalRequest.requested_at >= filters.since,
            ApprovalRequest.requested_at <= filters.until,
        )
        if filters.tool_aliases:
            stmt = stmt.where(ApprovalRequest.tool_alias.in_(filters.tool_aliases))
        if filters.execution_targets:
            target_aliases = select(Tool.alias).where(
                Tool.execution_target.in_(filters.execution_targets)
            )
            stmt = stmt.where(ApprovalRequest.tool_alias.in_(target_aliases))
        if filters.team_ids:
            team_user_ids = select(UserTeam.user_id).where(UserTeam.team_id.in_(filters.team_ids))
            stmt = stmt.where(ApprovalRequest.requested_by_user_id.in_(team_user_ids))
        if filters.roles:
            role_user_ids = (
                select(User.id)
                .join(Role, Role.id == User.role_id)
                .where(Role.name.in_(filters.roles))
            )
            stmt = stmt.where(ApprovalRequest.requested_by_user_id.in_(role_user_ids))
        stmt = stmt.group_by(ApprovalRequest.kind, ApprovalRequest.status)

        # kind -> status -> count
        tallies: dict[str, Counter[str]] = defaultdict(Counter)
        for kind, status, count in (await session.execute(stmt)).all():
            tallies[kind][status] = count
        return [
            ApprovalKindFunnel(
                kind=kind,
                requested=sum(tallies[kind].values()),
                approved=tallies[kind].get("approved", 0),
                denied=tallies[kind].get("denied", 0),
                pending=tallies[kind].get("pending", 0),
            )
            for kind in ("pre", "post")
            if tallies.get(kind)
        ]

    async def _normalize_team_filter(
        self, session: AsyncSession, filters: JobFilters
    ) -> JobFilters:
        """Resolve the ``World`` wildcard in the team selection.

        ``World`` is a universal-visibility marker, not a real membership
        group — admins never assign users to it (the user forms exclude
        it from their team pickers), so almost nobody is a *member*. A
        naive team filter on ``World`` would therefore match no users and
        blank out every chart. Treat it the way the rest of the app does:
        ``World`` means "everyone". So if its team id is among the
        selection, drop the team narrowing entirely (empty ⇒ all teams).
        Any non-``World`` selection is left untouched.
        """
        if not filters.team_ids:
            return filters
        world_id = await self._world_team_id(session)
        if world_id is not None and world_id in filters.team_ids:
            return replace(filters, team_ids=())
        return filters

    async def _world_team_id(self, session: AsyncSession) -> int | None:
        """The id of the ``World`` wildcard team, or ``None`` if absent."""
        # Lazy import: matofx.tools.service pulls in a large dependency
        # graph; importing it at module load would risk a cycle (the same
        # reason workflows.service imports WORLD_TEAM_NAME inside its
        # functions).
        from matofx.tools.service import WORLD_TEAM_NAME  # noqa: PLC0415

        stmt = select(Team.id).where(Team.name == WORLD_TEAM_NAME)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _user_teams(self, session: AsyncSession) -> dict[int, list[tuple[int, str]]]:
        """Map each user id to the ``(team_id, team_name)`` pairs they're in."""
        result = await session.execute(
            select(UserTeam.user_id, Team.id, Team.name).join(Team, Team.id == UserTeam.team_id)
        )
        mapping: dict[int, list[tuple[int, str]]] = defaultdict(list)
        for user_id, team_id, name in result.all():
            mapping[user_id].append((team_id, name))
        return mapping

    async def _user_directory(self, session: AsyncSession) -> dict[int, tuple[str, str]]:
        """Map each user id to ``(full_name, role_name)``."""
        result = await session.execute(
            select(User.id, User.full_name, Role.name).join(Role, Role.id == User.role_id)
        )
        return {uid: (full_name, role) for uid, full_name, role in result.all()}

    async def _new_users(self, session: AsyncSession, filters: JobFilters) -> int:
        """Count user accounts created within the filter window."""
        stmt = select(func.count(User.id)).where(
            User.created_at >= filters.since, User.created_at <= filters.until
        )
        return int((await session.execute(stmt)).scalar_one())

    async def _enabled_seats(self, session: AsyncSession) -> int:
        """Count currently-active (logon-enabled) accounts."""
        stmt = select(func.count(User.id)).where(User.is_active.is_(True))
        return int((await session.execute(stmt)).scalar_one())

    async def _session_intervals(
        self, session: AsyncSession, filters: JobFilters
    ) -> list[tuple[_dt.datetime, _dt.datetime]]:
        """Auth-session ``(start, end)`` intervals clamped to the window.

        ``end`` is the revoke time, else last activity. Team / role
        filters narrow by the session's user (tool / target don't apply
        to sessions). Intervals that don't intersect the window drop out.
        """
        stmt = select(
            AuthSession.created_at,
            AuthSession.last_activity_at,
            AuthSession.revoked_at,
        ).where(AuthSession.created_at <= filters.until)
        if filters.team_ids:
            team_user_ids = select(UserTeam.user_id).where(UserTeam.team_id.in_(filters.team_ids))
            stmt = stmt.where(AuthSession.user_id.in_(team_user_ids))
        if filters.roles:
            role_user_ids = (
                select(User.id)
                .join(Role, Role.id == User.role_id)
                .where(Role.name.in_(filters.roles))
            )
            stmt = stmt.where(AuthSession.user_id.in_(role_user_ids))

        intervals: list[tuple[_dt.datetime, _dt.datetime]] = []
        for created_at, last_activity_at, revoked_at in (await session.execute(stmt)).all():
            # SQLite returns timezone-aware columns as naive; coerce to
            # UTC so the Python-side clamp below can compare them against
            # the aware window bounds (Postgres already returns aware).
            created = _ensure_aware(created_at)
            ended = _ensure_aware(revoked_at or last_activity_at or created_at)
            start = max(created, filters.since)
            end = min(ended, filters.until)
            if start <= end:
                intervals.append((start, end))
        return intervals

    async def _logon_events(
        self, session: AsyncSession, filters: JobFilters
    ) -> list[LogonEventPoint]:
        """Daily granted / revoked logon-access counts within the window."""
        stmt = select(UserLogonEvent.event, UserLogonEvent.at).where(
            UserLogonEvent.at >= filters.since, UserLogonEvent.at <= filters.until
        )
        granted: Counter[_dt.date] = Counter()
        revoked: Counter[_dt.date] = Counter()
        for event, at in (await session.execute(stmt)).all():
            (granted if event == "granted" else revoked)[at.date()] += 1
        days = sorted(set(granted) | set(revoked))
        return [
            LogonEventPoint(date=day, granted=granted[day], revoked=revoked[day]) for day in days
        ]

    async def filter_options(self, session: AsyncSession) -> FilterOptions:
        """Return the option lists for the filter bar's dropdowns.

        Tools and teams are listed by name; roles are every role name in
        the install (so a usage-only "manager" role need not also hold
        ``tools.install`` / ``teams.manage`` / ``roles.manage`` to see
        the dropdowns). Execution targets are the frozen vocabulary.
        """
        tools = [
            ToolOption(alias=alias, name=name)
            for alias, name in (
                await session.execute(select(Tool.alias, Tool.name).order_by(Tool.name))
            ).all()
        ]
        teams = [
            TeamOption(id=team_id, name=name)
            for team_id, name in (
                await session.execute(select(Team.id, Team.name).order_by(Team.name))
            ).all()
        ]
        roles = list((await session.execute(select(Role.name).order_by(Role.name))).scalars().all())
        return FilterOptions(
            tools=tools,
            teams=teams,
            roles=roles,
            execution_targets=list(EXECUTION_TARGETS),
        )

    async def _fetch_jobs(self, session: AsyncSession, filters: JobFilters) -> list[_JobRow]:
        """Fetch the jobs matching ``filters``, denormalised with tool data.

        The shared query every tab builds on. Optional filters add a
        WHERE (and, for team / role, a JOIN) only when set, so the
        common "all" case stays a single tool join.
        """
        stmt = (
            select(
                Job.state,
                Job.created_at,
                Job.started_at,
                Job.completed_at,
                Job.tool_alias,
                Tool.name,
                Tool.resource_request,
                Job.created_by_user_id,
                Job.exit_code,
                Tool.execution_target,
            )
            .join(Tool, Tool.alias == Job.tool_alias)
            .where(Job.created_at >= filters.since, Job.created_at <= filters.until)
        )
        if filters.tool_aliases:
            stmt = stmt.where(Job.tool_alias.in_(filters.tool_aliases))
        if filters.execution_targets:
            stmt = stmt.where(Tool.execution_target.in_(filters.execution_targets))
        if filters.team_ids:
            # Subquery (not a join) so a user in several of the selected
            # teams doesn't duplicate their jobs in the result.
            team_user_ids = select(UserTeam.user_id).where(UserTeam.team_id.in_(filters.team_ids))
            stmt = stmt.where(Job.created_by_user_id.in_(team_user_ids))
        if filters.roles:
            role_user_ids = (
                select(User.id)
                .join(Role, Role.id == User.role_id)
                .where(Role.name.in_(filters.roles))
            )
            stmt = stmt.where(Job.created_by_user_id.in_(role_user_ids))

        result = await session.execute(stmt)
        rows: list[_JobRow] = []
        for row in result.all():
            (
                state,
                created_at,
                started_at,
                completed_at,
                alias,
                name,
                rr,
                user_id,
                exit_code,
                target,
            ) = row
            cores, gpu = _resources(rr)
            rows.append(
                _JobRow(
                    state=state,
                    created_at=created_at,
                    started_at=started_at,
                    completed_at=completed_at,
                    tool_alias=alias,
                    tool_name=name,
                    cores=cores,
                    gpu_count=gpu,
                    user_id=user_id,
                    exit_code=exit_code,
                    execution_target=target,
                )
            )
        return rows

    async def _registered_users(self, session: AsyncSession) -> int:
        """Total number of user accounts (the adoption denominator)."""
        return int((await session.execute(select(func.count(User.id)))).scalar_one())


def make_usage_service() -> UsageService:
    """Build a :class:`UsageService` (no dependencies; read-only)."""
    return UsageService()


# ---- pure helpers ----------------------------------------------------------


def _shift_window(filters: JobFilters) -> JobFilters:
    """Return ``filters`` shifted back one window length (for deltas)."""
    span = filters.until - filters.since
    return replace(filters, since=filters.since - span, until=filters.since)


def _ensure_aware(value: _dt.datetime) -> _dt.datetime:
    """Return ``value`` as UTC-aware (SQLite hands back naive datetimes)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=_dt.UTC)


def _resources(resource_request: dict[str, object] | None) -> tuple[float, float]:
    """Extract ``(cores, gpu_count)`` from a tool ``resource_request``.

    Missing / unparseable values fall back to one core and zero GPUs —
    the same defaults the runner assumes for a tool that declares no
    explicit resources.
    """
    if not isinstance(resource_request, dict):
        return (1.0, 0.0)
    cores = _as_float(resource_request.get("cores"), default=1.0) or 1.0
    gpu = _as_float(resource_request.get("gpu_count"), default=0.0) or 0.0
    return (cores, gpu)


def _as_float(value: object, *, default: float) -> float:
    """Coerce a JSON scalar to ``float``, returning ``default`` on failure."""
    if isinstance(value, bool):  # bool is an int subclass; never a resource count
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _runtime_hours(row: _JobRow) -> float | None:
    """Wall-clock hours a job ran, or ``None`` if it never completed."""
    if row.started_at is None or row.completed_at is None:
        return None
    seconds = (row.completed_at - row.started_at).total_seconds()
    return seconds / 3600.0 if seconds > 0 else 0.0


def _success_rate(rows: list[_JobRow]) -> float | None:
    """Percent of terminal jobs that completed, or ``None`` if none ran to a result."""
    complete = sum(1 for r in rows if r.state == _SUCCESS_STATE)
    failed = sum(1 for r in rows if r.state == _FAILURE_STATE)
    terminal = complete + failed
    return (complete / terminal * 100.0) if terminal else None


def _aggregate(rows: list[_JobRow]) -> _RawKpis:
    """Reduce a window's job rows to the scalar KPIs."""
    success_rate = _success_rate(rows)

    core_hours = 0.0
    gpu_hours = 0.0
    for row in rows:
        hours = _runtime_hours(row)
        if hours is None:
            continue
        core_hours += hours * row.cores
        gpu_hours += hours * row.gpu_count

    return _RawKpis(
        jobs_total=len(rows),
        active_users=len({r.user_id for r in rows}),
        success_rate=success_rate,
        core_hours=core_hours,
        gpu_hours=gpu_hours,
    )


def _kpi(current: float | None, previous: float | None) -> Kpi:
    """Wrap a current/previous pair, computing the percentage delta."""
    delta_pct: float | None = None
    if current is not None and previous is not None and previous != 0:
        delta_pct = (current - previous) / previous * 100.0
    return Kpi(value=current, previous=previous, delta_pct=delta_pct)


def _by_state(rows: list[_JobRow]) -> dict[str, int]:
    """Job counts keyed by run state, for the window."""
    counts = Counter(r.state for r in rows)
    return dict(sorted(counts.items()))


def _series(rows: list[_JobRow]) -> list[StatePoint]:
    """Per-day, per-state job counts, ordered by date then state."""
    buckets: dict[tuple[_dt.date, str], int] = defaultdict(int)
    for row in rows:
        buckets[(row.created_at.date(), row.state)] += 1
    return [
        StatePoint(date=day, state=state, count=count)
        for (day, state), count in sorted(buckets.items())
    ]


def _top_tools(rows: list[_JobRow]) -> list[ToolCount]:
    """The most-used tools in the window, highest job count first."""
    counts: Counter[str] = Counter(r.tool_alias for r in rows)
    names = {r.tool_alias: r.tool_name for r in rows}
    return [
        ToolCount(tool_alias=alias, tool_name=names.get(alias, alias), count=count)
        for alias, count in counts.most_common(_TOP_TOOLS_LIMIT)
    ]


@dataclass
class _Consumption:
    """Mutable accumulator for the consumption reductions."""

    name: str
    core_hours: float = 0.0
    gpu_hours: float = 0.0
    jobs: int = 0

    def add(self, row: _JobRow) -> None:
        """Fold one job into the running totals."""
        self.jobs += 1
        hours = _runtime_hours(row)
        if hours is not None:
            self.core_hours += hours * row.cores
            self.gpu_hours += hours * row.gpu_count


def _consumption_by_tool(rows: list[_JobRow]) -> list[ToolConsumption]:
    """Core/GPU-hours + job counts per tool, highest core-hours first."""
    agg: dict[str, _Consumption] = {}
    for row in rows:
        agg.setdefault(row.tool_alias, _Consumption(name=row.tool_name)).add(row)
    out = [
        ToolConsumption(
            tool_alias=alias,
            tool_name=acc.name,
            core_hours=acc.core_hours,
            gpu_hours=acc.gpu_hours,
            jobs=acc.jobs,
        )
        for alias, acc in agg.items()
    ]
    out.sort(key=lambda c: (c.core_hours, c.jobs), reverse=True)
    return out


def _consumption_by_team(
    rows: list[_JobRow], user_teams: dict[int, list[tuple[int, str]]]
) -> list[TeamConsumption]:
    """Core/GPU-hours + job counts per team (owner's teams), highest first.

    A job whose owner belongs to several teams is counted under each of
    them; an owner in no team falls into the ``None`` "(No team)" bucket.
    """
    no_team: list[tuple[int | None, str]] = [(None, "(No team)")]
    agg: dict[int | None, _Consumption] = {}
    for row in rows:
        teams: list[tuple[int | None, str]] = list(user_teams.get(row.user_id) or no_team)
        for team_id, team_name in teams:
            agg.setdefault(team_id, _Consumption(name=team_name)).add(row)
    out = [
        TeamConsumption(
            team_id=team_id,
            team_name=acc.name,
            core_hours=acc.core_hours,
            gpu_hours=acc.gpu_hours,
            jobs=acc.jobs,
        )
        for team_id, acc in agg.items()
    ]
    out.sort(key=lambda c: (c.core_hours, c.jobs), reverse=True)
    return out


def _runtime_distribution(rows: list[_JobRow]) -> list[RuntimeDist]:
    """Per-tool runtime percentiles (hours), highest p95 first.

    Only jobs that actually ran (have both timestamps) contribute.
    """
    by_tool: dict[str, list[float]] = defaultdict(list)
    names: dict[str, str] = {}
    for row in rows:
        hours = _runtime_hours(row)
        if hours is None:
            continue
        by_tool[row.tool_alias].append(hours)
        names[row.tool_alias] = row.tool_name
    out = [
        RuntimeDist(
            tool_alias=alias,
            tool_name=names[alias],
            p50_hours=_percentile(values, 0.50),
            p95_hours=_percentile(values, 0.95),
            max_hours=max(values),
            runs=len(values),
        )
        for alias, values in by_tool.items()
    ]
    out.sort(key=lambda d: d.p95_hours, reverse=True)
    return out


def _queue_wait_series(rows: list[_JobRow]) -> list[DayValue]:
    """Average queue wait (hours) per day a job started.

    Wait is ``started_at - created_at``; only started jobs contribute,
    bucketed by the day they started.
    """
    buckets: dict[_dt.date, list[float]] = defaultdict(list)
    for row in rows:
        if row.started_at is None:
            continue
        wait = (row.started_at - row.created_at).total_seconds() / 3600.0
        buckets[row.started_at.date()].append(max(wait, 0.0))
    return [
        DayValue(date=day, value=sum(values) / len(values))
        for day, values in sorted(buckets.items())
    ]


def _throughput_series(rows: list[_JobRow]) -> list[ThroughputPoint]:
    """Jobs submitted (by created day) vs completed (by completed day)."""
    submitted: Counter[_dt.date] = Counter(r.created_at.date() for r in rows)
    completed: Counter[_dt.date] = Counter(
        r.completed_at.date() for r in rows if r.completed_at is not None
    )
    days = sorted(set(submitted) | set(completed))
    return [
        ThroughputPoint(date=day, submitted=submitted[day], completed=completed[day])
        for day in days
    ]


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolated ``q``-quantile (0..1) of ``values``.

    Returns ``0.0`` for an empty list. Matches numpy's default
    ("linear") method so the figures line up with any offline analysis.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


def _success_trend(rows: list[_JobRow]) -> list[DayValue]:
    """Daily success rate (percent), for days that had any terminal jobs."""
    complete: Counter[_dt.date] = Counter()
    terminal: Counter[_dt.date] = Counter()
    for row in rows:
        if row.state == _SUCCESS_STATE:
            complete[row.created_at.date()] += 1
            terminal[row.created_at.date()] += 1
        elif row.state == _FAILURE_STATE:
            terminal[row.created_at.date()] += 1
    return [
        DayValue(date=day, value=complete[day] / terminal[day] * 100.0) for day in sorted(terminal)
    ]


def _top_failing(rows: list[_JobRow]) -> list[FailingTool]:
    """Tools with at least one failure, most failures first."""
    failures: Counter[str] = Counter()
    terminal: Counter[str] = Counter()
    names: dict[str, str] = {}
    last_failed: dict[str, _dt.datetime] = {}
    for row in rows:
        names[row.tool_alias] = row.tool_name
        if row.state == _SUCCESS_STATE:
            terminal[row.tool_alias] += 1
        elif row.state == _FAILURE_STATE:
            terminal[row.tool_alias] += 1
            failures[row.tool_alias] += 1
            when = row.completed_at or row.created_at
            if row.tool_alias not in last_failed or when > last_failed[row.tool_alias]:
                last_failed[row.tool_alias] = when
    out = [
        FailingTool(
            tool_alias=alias,
            tool_name=names[alias],
            failures=count,
            failure_rate=(count / terminal[alias] * 100.0) if terminal[alias] else 0.0,
            last_failed_at=last_failed.get(alias),
        )
        for alias, count in failures.items()
    ]
    out.sort(key=lambda f: (f.failures, f.failure_rate), reverse=True)
    return out


def _exit_code_counts(rows: list[_JobRow]) -> list[ExitCodeCount]:
    """Distribution of process exit codes, ascending by code.

    Only jobs that recorded an exit code contribute (a job that never
    ran has ``None``).
    """
    counts: Counter[int] = Counter(r.exit_code for r in rows if r.exit_code is not None)
    return [ExitCodeCount(exit_code=code, count=count) for code, count in sorted(counts.items())]


def _humanize_role(role: str) -> str:
    """Render a canonical role name for display (``super_user`` → ``Super User``)."""
    return role.replace("_", " ").title() if role else "Unknown"


def _active_users_trend(rows: list[_JobRow]) -> list[DayValue]:
    """Distinct active users (job creators) per day."""
    by_day: dict[_dt.date, set[int]] = defaultdict(set)
    for row in rows:
        by_day[row.created_at.date()].add(row.user_id)
    return [DayValue(date=day, value=float(len(users))) for day, users in sorted(by_day.items())]


def _jobs_by_user(rows: list[_JobRow], directory: dict[int, tuple[str, str]]) -> list[NamedCount]:
    """Top users by job count, labelled with their full name."""
    counts: Counter[int] = Counter(r.user_id for r in rows)
    return [
        NamedCount(name=directory.get(uid, (f"User {uid}", ""))[0], count=count)
        for uid, count in counts.most_common(_PEOPLE_LIMIT)
    ]


def _jobs_by_team_named(
    rows: list[_JobRow], user_teams: dict[int, list[tuple[int, str]]]
) -> list[NamedCount]:
    """Jobs per team (owner's teams; a job counts for each), highest first."""
    no_team: list[tuple[int | None, str]] = [(None, "(No team)")]
    counts: Counter[str] = Counter()
    for row in rows:
        teams: list[tuple[int | None, str]] = list(user_teams.get(row.user_id) or no_team)
        for _team_id, team_name in teams:
            counts[team_name] += 1
    return [NamedCount(name=name, count=count) for name, count in counts.most_common()]


def _jobs_by_role(rows: list[_JobRow], directory: dict[int, tuple[str, str]]) -> list[NamedCount]:
    """Jobs per (pretty-printed) role of the job's owner, highest first."""
    counts: Counter[str] = Counter()
    for row in rows:
        role = directory.get(row.user_id, ("", ""))[1]
        counts[role] += 1
    return [
        NamedCount(name=_humanize_role(role), count=count) for role, count in counts.most_common()
    ]


def _peak_concurrency(intervals: list[tuple[_dt.datetime, _dt.datetime]]) -> int:
    """Maximum number of overlapping intervals (sweep line)."""
    events: list[tuple[_dt.datetime, int]] = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    # Intervals are closed [start, end]: at equal timestamps process
    # starts (+1) before ends (-1) so a zero-length session (a brand-new
    # session whose created_at == last_activity_at) still counts as one.
    events.sort(key=lambda e: (e[0], -e[1]))
    running = 0
    peak = 0
    for _moment, delta in events:
        running += delta
        peak = max(peak, running)
    return peak


def _peak_concurrency_by_day(
    intervals: list[tuple[_dt.datetime, _dt.datetime]],
    since: _dt.datetime,
    until: _dt.datetime,
) -> list[DayValue]:
    """Peak concurrency for each day in the window that had any sessions."""
    out: list[DayValue] = []
    day = since.date()
    last = until.date()
    while day <= last:
        day_start = _dt.datetime(day.year, day.month, day.day, tzinfo=_dt.UTC)
        day_end = day_start + _dt.timedelta(days=1) - _dt.timedelta(seconds=1)
        clamped = [
            (max(start, day_start), min(end, day_end))
            for start, end in intervals
            if max(start, day_start) <= min(end, day_end)
        ]
        peak = _peak_concurrency(clamped)
        if peak > 0:
            out.append(DayValue(date=day, value=float(peak)))
        day += _dt.timedelta(days=1)
    return out


def _infra_stats(rows: list[_JobRow]) -> InfraStats:
    """Job counts + core/GPU-hours by execution target and CPU/GPU split."""
    by_target: dict[str, _Consumption] = {}
    cpu_jobs = 0
    gpu_jobs = 0
    cpu_core_hours = 0.0
    gpu_core_hours = 0.0
    for row in rows:
        by_target.setdefault(row.execution_target, _Consumption(name=row.execution_target)).add(row)
        hours = _runtime_hours(row)
        core = hours * row.cores if hours is not None else 0.0
        if row.gpu_count > 0:
            gpu_jobs += 1
            gpu_core_hours += core
        else:
            cpu_jobs += 1
            cpu_core_hours += core
    targets = [
        TargetUsage(
            target=target,
            jobs=acc.jobs,
            core_hours=acc.core_hours,
            gpu_hours=acc.gpu_hours,
        )
        for target, acc in by_target.items()
    ]
    targets.sort(key=lambda t: t.core_hours, reverse=True)
    return InfraStats(
        by_target=targets,
        cpu_jobs=cpu_jobs,
        gpu_jobs=gpu_jobs,
        cpu_core_hours=cpu_core_hours,
        gpu_core_hours=gpu_core_hours,
    )
