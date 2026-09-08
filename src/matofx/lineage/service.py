"""LineageService — assemble the provenance graph centred on one object.

Design: ``designs/data-lineage.md`` §4. The service is **read-only** —
no method here mutates a row in any other domain. Each per-kind
resolver composes the existing per-domain service or ACL helper for
that kind (``JobService``, ``ScenarioService``, ``LibraryService``,
``MaterialsService``, …); ACL is enforced at resolution time via the
domain's existing visibility check, so a node the caller can't see in
its own surface is never leaked into a lineage graph.

Slices landed:

- **Slice 1** — :class:`LineageService` skeleton + the closed
  :class:`NodeKind` / :class:`EdgeKind` vocabularies + the
  :class:`LineageGraph` dataclass + a full
  :meth:`LineageService._resolve_job` resolver covering every
  job-centred upstream edge in the schema. Defensive
  :func:`_assert_dag` cycle guard.
- **Slice 2 (this slice)** — real resolvers for every other v1.0
  node kind (``USER`` / ``TOOL_VERSION`` / ``SCENARIO`` / ``PROJECT`` /
  ``WORKFLOW_RUN`` / ``PARAMETRIC_STUDY`` / ``LIBRARY_FILE_VERSION`` /
  ``MATERIAL_VERSION`` / ``APPROVAL_REQUEST``). Each emits its
  upstream edges from existing FKs and (where the owning service
  already exposes the helper) emits downstream edges to its
  child rows. The slice-1 ``_PLACEHOLDER_SPECS`` lookup retires.

Reserved-for-later resolvers (``OPTIMISATION_STUDY`` / ``JOB_COHORT`` /
``SURROGATE_MODEL_VERSION`` / ``VV_RECORD``) collapse to ``None`` when
invoked — they have no ORM table yet — and are silently dropped when
an edge points at them (impossible until their owning ADR ships an
edge kind that does so).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from matofx.audit.schema import RecordInput
from matofx.hierarchy.errors import AccessDeniedError, NotFoundError
from matofx.hierarchy.orm import Job, ProjectAccessLevel
from matofx.hierarchy.service import _job_project_id, _require_project_access
from matofx.lineage.errors import (
    LineageAccessDeniedError,
    LineageDepthOutOfRangeError,
    LineageInvariantError,
    LineageNotFoundError,
)
from matofx.lineage.types import (
    EDGE_LABELS,
    EdgeKind,
    LineageCentre,
    LineageEdge,
    LineageGraph,
    LineageNode,
    NodeKind,
    NodeRef,
)
from matofx.users.permissions import Permission, user_has_permission

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


# Hard cap on per-call depth — the service refuses larger values up
# front. Beyond this, the UI lets users expand individual edges; the
# global depth knob isn't where deeper drilling should live.
_MAX_DEPTH: int = 20

# Default soft-cap on the assembled node count. Operators override via
# the ``lineage_max_nodes`` General Setting (lands in slice 7). When the
# build reaches this many nodes mid-walk, the remaining frontier is
# truncated and the UI degrades to table-only rendering (design §4.5).
_DEFAULT_SOFT_CAP: int = 500


# A resolver returns ``(node, upstream_edges, downstream_edges)`` —
# upstream = follow source→target arrows (older / dependencies);
# downstream = arrows that point AT this node from newer objects. The
# BFS walks each direction independently from the centre.
_ResolveResult = tuple[LineageNode, list[LineageEdge], list[LineageEdge]] | None


@dataclass
class _BuildContext:
    """Mutable per-build state — kept off the (frozen) service.

    The service itself is ``frozen=True`` (mirrors
    :class:`LibraryService`); per-call state needs its own object so
    concurrent builds don't share counters.
    """

    actor_user_id: int
    soft_cap: int
    cache: dict[NodeRef, LineageNode] = field(default_factory=dict)
    edges: list[LineageEdge] = field(default_factory=list)
    hidden_by_acl: int = 0
    hidden_by_depth: int = 0
    soft_capped: bool = False


def _assert_dag(edges: tuple[LineageEdge, ...]) -> None:
    """Raise :class:`LineageInvariantError` if ``edges`` contain a cycle.

    Defensive guard for the DAG-by-construction promise (design §3.3).
    Production shouldn't see this fire — every edge kind has a
    schema-level direction that flows older → newer, so a cycle
    indicates either a resolver bug or a forged edge tuple in a test.

    Standard DFS with three-colour marking (white / grey / black). A
    grey node revisited mid-traversal is a back-edge, i.e. a cycle.
    Iterative to avoid Python's recursion-limit landmine on large
    graphs.
    """
    if not edges:
        return

    # Build adjacency (source → list[target]) + the node set.
    adj: dict[NodeRef, list[NodeRef]] = {}
    nodes: set[NodeRef] = set()
    for e in edges:
        adj.setdefault(e.source, []).append(e.target)
        nodes.add(e.source)
        nodes.add(e.target)

    white, grey, black = 0, 1, 2
    colour: dict[NodeRef, int] = dict.fromkeys(nodes, white)

    for root in nodes:
        if colour[root] != white:
            continue
        # Iterative DFS: stack entries are ``(node, neighbour_index)``.
        stack: list[tuple[NodeRef, int]] = [(root, 0)]
        colour[root] = grey
        while stack:
            node, idx = stack[-1]
            neighbours = adj.get(node, ())
            if idx >= len(neighbours):
                colour[node] = black
                stack.pop()
                continue
            stack[-1] = (node, idx + 1)
            nxt = neighbours[idx]
            if colour[nxt] == grey:
                raise LineageInvariantError(f"cycle through node {nxt!r}")
            if colour[nxt] == white:
                colour[nxt] = grey
                stack.append((nxt, 0))


@dataclass(frozen=True)
class LineageService:
    """Read-only graph assembly over existing per-domain rows (ADR-0010)."""

    audit: AuditWriter

    # ---- public API ---------------------------------------------------

    async def build_graph(
        self,
        session: AsyncSession,
        *,
        kind: NodeKind,
        obj_id: int,
        actor_user_id: int,
        upstream_depth: int = 6,
        downstream_depth: int = 3,
        soft_cap: int | None = None,
        surface: str = "service",
    ) -> LineageGraph:
        """Build the lineage graph centred on ``(kind, obj_id)``.

        Raises :class:`LineageDepthOutOfRangeError` if either depth is
        outside ``0..20``; :class:`LineageAccessDeniedError` if the
        caller lacks ``lineage.view``; :class:`LineageNotFoundError`
        if the centre doesn't exist or the caller can't see it
        (existence-hidden).

        Mid-walk hidden nodes drop silently from the result; the
        incident edge carries a ``truncated_count`` bump and the
        :attr:`LineageGraph.hidden_by_acl` counter increments.
        """
        if not 0 <= upstream_depth <= _MAX_DEPTH:
            raise LineageDepthOutOfRangeError(upstream_depth)
        if not 0 <= downstream_depth <= _MAX_DEPTH:
            raise LineageDepthOutOfRangeError(downstream_depth)

        if not await user_has_permission(session, actor_user_id, Permission.LINEAGE_VIEW):
            raise LineageAccessDeniedError

        effective_cap = soft_cap if soft_cap is not None else await _resolve_soft_cap(session)
        ctx = _BuildContext(
            actor_user_id=actor_user_id,
            soft_cap=effective_cap,
        )

        # Resolve the centre — must succeed (existence-hidden otherwise).
        centre_result = await self._resolve(session, ctx, kind, obj_id)
        if centre_result is None:
            raise LineageNotFoundError(kind.value, obj_id)
        centre_node, centre_up, centre_down = centre_result
        ctx.cache[(kind, obj_id)] = centre_node

        # BFS each direction independently — upstream follows the
        # source→target arrow; downstream follows incoming edges (i.e.
        # the resolver's downstream_edges list).
        await self._walk(session, ctx, list(centre_up), depth_limit=upstream_depth, direction="up")
        await self._walk(
            session, ctx, list(centre_down), depth_limit=downstream_depth, direction="down"
        )

        edges_tuple = tuple(ctx.edges)
        nodes_tuple = tuple(ctx.cache.values())

        _assert_dag(edges_tuple)

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="lineage.viewed",
                resource_type=kind.value,
                resource_id=str(obj_id),
                outcome="success",
                payload={
                    "centre_kind": kind.value,
                    "centre_id": obj_id,
                    "upstream_depth": upstream_depth,
                    "downstream_depth": downstream_depth,
                    "node_count": len(nodes_tuple),
                    "edge_count": len(edges_tuple),
                    "hidden_by_acl": ctx.hidden_by_acl,
                    "hidden_by_depth": ctx.hidden_by_depth,
                    "soft_capped": ctx.soft_capped,
                    # ``surface`` tells compliance which surface drove
                    # the view: "tab" / "admin_page" / "json" /
                    # "graphml" / "report" — passed through by the
                    # API / report integration; default ``"service"``
                    # for direct service calls (mostly tests).
                    "surface": surface,
                },
            ),
        )

        return LineageGraph(
            centre=(kind, obj_id),
            nodes=nodes_tuple,
            edges=edges_tuple,
            hidden_by_acl=ctx.hidden_by_acl,
            hidden_by_depth=ctx.hidden_by_depth,
            soft_capped=ctx.soft_capped,
        )

    # ---- centre picker (slice 5b) -------------------------------------

    async def list_centres(
        self,
        session: AsyncSession,
        *,
        kind: NodeKind,
        actor_user_id: int,
        limit: int = 100,
    ) -> list[LineageCentre]:
        """List visible centre options for the admin lineage picker.

        Each :class:`LineageCentre` is a candidate the operator can
        choose from the dropdown — id + human-readable label + an
        optional state badge. Per-kind dispatch delegates to the
        existing domain services, so the visibility rules track
        whatever the owning service enforces:

        - ``job`` / ``workflow_run`` / ``parametric_study`` /
          ``scenario`` / ``project`` — project ACL
          (ADR-0002).
        - ``library_file_version`` — scope visibility (ADR-0006).
        - ``material_version`` — read-open (ADR-0007).
        - ``tool_version`` — ``tools.see_*_state`` gating.
        - ``user`` — admin-only (``projects.see_all``).
        - ``approval_request`` — the actor's own pending inbox.

        Reserved-for-later kinds (``optimisation_study`` /
        ``job_cohort`` / ``surrogate_model_version`` / ``vv_record``)
        return an empty list — their owning service hasn't shipped
        yet, so there's nothing to enumerate.
        """
        if not await user_has_permission(session, actor_user_id, Permission.LINEAGE_VIEW):
            raise LineageAccessDeniedError
        handler = _CENTRE_LIST_HANDLERS.get(kind)
        if handler is None:
            return []
        return await handler(session, actor_user_id, limit)

    # ---- BFS driver ---------------------------------------------------

    async def _walk(
        self,
        session: AsyncSession,
        ctx: _BuildContext,
        seed_edges: list[LineageEdge],
        *,
        depth_limit: int,
        direction: str,
    ) -> None:
        """Walk ``seed_edges`` outward up to ``depth_limit`` layers.

        For each edge, resolve the **far end** (target for upstream,
        source for downstream). A None resolve = hidden by ACL → emit
        the edge with bumped ``truncated_count``; otherwise emit the
        edge and queue the far node's own edges into the next layer.
        Stops cleanly when the soft cap fires or the depth limit is
        reached.
        """
        frontier = seed_edges
        depth = 0
        while frontier and depth < depth_limit:
            next_layer: list[LineageEdge] = []
            for edge in frontier:
                if len(ctx.cache) >= ctx.soft_cap:
                    ctx.soft_capped = True
                    ctx.hidden_by_depth += 1
                    ctx.edges.append(edge)
                    continue
                far = edge.target if direction == "up" else edge.source
                if far in ctx.cache:
                    ctx.edges.append(edge)
                    continue
                result = await self._resolve(session, ctx, far[0], far[1])
                if result is None:
                    ctx.hidden_by_acl += 1
                    ctx.edges.append(
                        LineageEdge(
                            source=edge.source,
                            target=edge.target,
                            relation=edge.relation,
                            label=edge.label,
                            field_name=edge.field_name,
                            truncated_count=edge.truncated_count + 1,
                        )
                    )
                    continue
                far_node, far_up, far_down = result
                ctx.cache[far] = far_node
                ctx.edges.append(edge)
                additional = far_up if direction == "up" else far_down
                next_layer.extend(additional)
            frontier = next_layer
            depth += 1
        # Anything left in the frontier at depth_limit is hidden by depth.
        for edge in frontier:
            far = edge.target if direction == "up" else edge.source
            if far not in ctx.cache:
                ctx.hidden_by_depth += 1
            ctx.edges.append(edge)

    # ---- resolver dispatch -------------------------------------------

    async def _resolve(
        self,
        session: AsyncSession,
        ctx: _BuildContext,
        kind: NodeKind,
        obj_id: int,
    ) -> _ResolveResult:
        """Dispatch to the per-kind resolver.

        Reserved-for-later kinds (``OPTIMISATION_STUDY``,
        ``JOB_COHORT``, ``SURROGATE_MODEL_VERSION``, ``VV_RECORD``)
        have no ORM yet and collapse to ``None`` (treated as
        not-found / hidden by the BFS).
        """
        resolver = _RESOLVERS.get(kind)
        if resolver is None:
            return None
        return await resolver(self, session, ctx, obj_id)

    # ---- the job resolver (full implementation) ----------------------

    async def _resolve_job(
        self,
        session: AsyncSession,
        ctx: _BuildContext,
        job_id: int,
    ) -> _ResolveResult:
        """Resolve a ``Job`` node with all upstream edges (design §3.4).

        ACL: VIEW on the owning project, via
        :func:`matofx.hierarchy.service._require_project_access`. The
        hierarchy service does the existence-hide collapse already; a
        ``NotFoundError`` or ``AccessDeniedError`` from there
        translates to ``None`` (caller drops the node and reports it
        as hidden-by-ACL).

        Upstream edges materialised (slice 1):

        - :attr:`EdgeKind.SUBMITTED_BY` → ``user`` (``Job.created_by_user_id``)
        - :attr:`EdgeKind.BELONGS_TO_SCENARIO` → ``scenario`` (``Job.scenario_id``,
          when the job is scenario-homed) **or**
          :attr:`EdgeKind.SEEDED_BY_STUDY` → ``parametric_study``
          (``Job.parametric_study_id``, when the job is study-homed).
          The XOR is enforced at the DB level by
          ``ck_job_exactly_one_home``.
        - :attr:`EdgeKind.BELONGS_TO_WORKFLOW_RUN` → ``workflow_run``
          (``Job.workflow_run_id``, when set).
        - :attr:`EdgeKind.RAN_TOOL_VERSION` → ``tool_version``
          (``Job.tool_version_id``).
        - :attr:`EdgeKind.CONSUMED_LIBRARY_FILE_VERSION` →
          ``library_file_version`` (one per row in
          ``job_library_file_use``).
        - :attr:`EdgeKind.CONSUMED_MATERIAL_VERSION` →
          ``material_version`` (one per row in ``job_material_use``).
        - :attr:`EdgeKind.GATED_BY` → ``approval_request`` (one per row
          in ``approval_request`` where ``job_id = job.id``).

        Downstream edges are empty for slice 1 — the surfaces that
        point at a Job (workflow children consuming its outputs,
        surrogates trained on its results) need their owning resolvers
        first, which land in slice 2 + ADR-0008.
        """
        from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415
        from matofx.library.orm import JobLibraryFileUse  # noqa: PLC0415
        from matofx.materials.orm import JobMaterialUse  # noqa: PLC0415
        from matofx.tools.orm import Tool  # noqa: PLC0415

        job = await session.get(Job, job_id)
        if job is None:
            return None
        try:
            await _require_project_access(
                session,
                project_id=await _job_project_id(session, job),
                actor_user_id=ctx.actor_user_id,
                required=ProjectAccessLevel.VIEW,
                resource_type="job",
                resource_id=job_id,
            )
        except (NotFoundError, AccessDeniedError):
            return None

        # Show the tool's human-readable descriptor name, never its
        # internal alias ([[feedback_use_labels_not_names]]); fall back to
        # the alias only if the tool row has gone missing.
        tool_name = (
            await session.execute(select(Tool.name).where(Tool.alias == job.tool_alias).limit(1))
        ).scalar_one_or_none()

        node = LineageNode(
            kind=NodeKind.JOB,
            obj_id=job_id,
            label=f"{tool_name or job.tool_alias} #{job_id}",
            state=job.state,
            archived=False,  # jobs don't archive; the scenario does
            deep_link=f"/jobs/{job_id}",
            payload={
                "name": job.name,
                "tool_alias": job.tool_alias,
                "tool_version_id": job.tool_version_id,
                "created_at": job.created_at.isoformat() if job.created_at else None,
            },
        )
        ref = (NodeKind.JOB, job_id)
        upstream: list[LineageEdge] = []

        # Submitter — every job has one (NOT NULL FK).
        upstream.append(
            LineageEdge(
                source=ref,
                target=(NodeKind.USER, job.created_by_user_id),
                relation=EdgeKind.SUBMITTED_BY,
                label=EDGE_LABELS[EdgeKind.SUBMITTED_BY],
            )
        )

        # Scenario XOR parametric-study home (ck_job_exactly_one_home).
        if job.scenario_id is not None:
            upstream.append(
                LineageEdge(
                    source=ref,
                    target=(NodeKind.SCENARIO, job.scenario_id),
                    relation=EdgeKind.BELONGS_TO_SCENARIO,
                    label=EDGE_LABELS[EdgeKind.BELONGS_TO_SCENARIO],
                )
            )
        elif job.parametric_study_id is not None:
            upstream.append(
                LineageEdge(
                    source=ref,
                    target=(NodeKind.PARAMETRIC_STUDY, job.parametric_study_id),
                    relation=EdgeKind.SEEDED_BY_STUDY,
                    label=EDGE_LABELS[EdgeKind.SEEDED_BY_STUDY],
                )
            )
        elif job.optimisation_study_id is not None:
            upstream.append(
                LineageEdge(
                    source=ref,
                    target=(NodeKind.OPTIMISATION_STUDY, job.optimisation_study_id),
                    relation=EdgeKind.SEEDED_BY_STUDY,
                    label=EDGE_LABELS[EdgeKind.SEEDED_BY_STUDY],
                )
            )

        # Workflow membership (optional).
        if job.workflow_run_id is not None:
            upstream.append(
                LineageEdge(
                    source=ref,
                    target=(NodeKind.WORKFLOW_RUN, job.workflow_run_id),
                    relation=EdgeKind.BELONGS_TO_WORKFLOW_RUN,
                    label=EDGE_LABELS[EdgeKind.BELONGS_TO_WORKFLOW_RUN],
                )
            )

        # Tool version — every job has one (NOT NULL FK).
        upstream.append(
            LineageEdge(
                source=ref,
                target=(NodeKind.TOOL_VERSION, job.tool_version_id),
                relation=EdgeKind.RAN_TOOL_VERSION,
                label=EDGE_LABELS[EdgeKind.RAN_TOOL_VERSION],
            )
        )

        # Library files consumed (zero or more rows, one per field).
        lib_rows = await session.execute(
            select(JobLibraryFileUse.field_name, JobLibraryFileUse.library_file_version_id)
            .where(JobLibraryFileUse.job_id == job_id)
            .order_by(JobLibraryFileUse.field_name)
        )
        for field_name, version_id in lib_rows.all():
            upstream.append(
                LineageEdge(
                    source=ref,
                    target=(NodeKind.LIBRARY_FILE_VERSION, version_id),
                    relation=EdgeKind.CONSUMED_LIBRARY_FILE_VERSION,
                    label=EDGE_LABELS[EdgeKind.CONSUMED_LIBRARY_FILE_VERSION],
                    field_name=field_name,
                )
            )

        # Materials consumed (zero or more rows, one per field).
        mat_rows = await session.execute(
            select(JobMaterialUse.field_name, JobMaterialUse.material_version_id)
            .where(JobMaterialUse.job_id == job_id)
            .order_by(JobMaterialUse.field_name)
        )
        for field_name, version_id in mat_rows.all():
            upstream.append(
                LineageEdge(
                    source=ref,
                    target=(NodeKind.MATERIAL_VERSION, version_id),
                    relation=EdgeKind.CONSUMED_MATERIAL_VERSION,
                    label=EDGE_LABELS[EdgeKind.CONSUMED_MATERIAL_VERSION],
                    field_name=field_name,
                )
            )

        # Surrogate model version the job ran (ADR-0008 §7.4). Set only on
        # synthesised surrogate-tool jobs; the tag is captured at submit
        # from the tool's pinned descriptor. Naturally dormant when the
        # surrogate feature is off (no such jobs exist).
        if job.surrogate_model_version_id is not None:
            upstream.append(
                LineageEdge(
                    source=ref,
                    target=(NodeKind.SURROGATE_MODEL_VERSION, job.surrogate_model_version_id),
                    relation=EdgeKind.RAN_SURROGATE_VERSION,
                    label=EDGE_LABELS[EdgeKind.RAN_SURROGATE_VERSION],
                )
            )

        # Approval gates that touched this job (pre or post). Sorted
        # by id for stable ordering across builds. We carry the
        # ``pre`` / ``post`` discriminator on the edge's ``field_name``
        # slot so a slice-1 placeholder graph still distinguishes pre
        # from post; slice 2's ``_resolve_approval_request`` will move
        # this onto the node's payload + label proper.
        approval_rows = await session.execute(
            select(ApprovalRequest.id, ApprovalRequest.kind)
            .where(ApprovalRequest.job_id == job_id)
            .order_by(ApprovalRequest.id)
        )
        for approval_id, kind_str in approval_rows.all():
            upstream.append(
                LineageEdge(
                    source=ref,
                    target=(NodeKind.APPROVAL_REQUEST, approval_id),
                    relation=EdgeKind.GATED_BY,
                    label=EDGE_LABELS[EdgeKind.GATED_BY],
                    field_name=kind_str,
                )
            )

        return node, upstream, []

    # ---- user --------------------------------------------------------

    async def _resolve_user(
        self,
        session: AsyncSession,
        ctx: _BuildContext,  # noqa: ARG002 — user nodes are broadly visible; no ACL state needed
        user_id: int,
    ) -> _ResolveResult:
        """Resolve a ``User`` node.

        Any authenticated holder of ``lineage.view`` can see another
        user's display name + role on this surface (design §4.3). The
        full activity-graph view ("every job / approval / curated
        artefact this user touched") is the admin-only standalone
        page; the lineage node itself is broadly visible.

        No edges in slice 2 — the user's activity graph requires a
        reverse-edge helper (``UserService.list_recent_activity_for``)
        that lands when the admin user-activity surface is wired up.
        """
        from matofx.users.orm import User  # noqa: PLC0415

        user = await session.get(User, user_id)
        if user is None:
            return None
        label = user.full_name.strip() or user.username
        return (
            LineageNode(
                kind=NodeKind.USER,
                obj_id=user_id,
                label=label,
                state="active" if user.is_active else "inactive",
                archived=not user.is_active,
                deep_link=f"/admin/users/{user_id}/edit",
                payload={"username": user.username},
            ),
            [],
            [],
        )

    # ---- tool version ------------------------------------------------

    async def _resolve_tool_version(
        self,
        session: AsyncSession,
        ctx: _BuildContext,
        version_id: int,
    ) -> _ResolveResult:
        """Resolve a ``ToolVersion`` node with creator edge.

        ACL mirrors :meth:`ToolService.get` — a tool whose state is
        gated (``new`` / ``test``) is hidden from users without the
        matching ``tools.see_*_state`` permission. Archived tools are
        visible here (provenance is historical) but rendered greyed.
        """
        from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415
        from matofx.tools.states import user_can_see_state  # noqa: PLC0415

        version = await session.get(ToolVersion, version_id)
        if version is None:
            return None
        tool = (
            await session.execute(select(Tool).where(Tool.alias == version.alias).limit(1))
        ).scalar_one_or_none()
        if tool is None:
            return None
        if not await user_can_see_state(session, ctx.actor_user_id, tool.state):
            return None

        archived = tool.archived_at is not None
        upstream: list[LineageEdge] = [
            LineageEdge(
                source=(NodeKind.TOOL_VERSION, version_id),
                target=(NodeKind.USER, version.created_by_user_id),
                relation=EdgeKind.PUBLISHED_BY,
                label=EDGE_LABELS[EdgeKind.PUBLISHED_BY],
            )
        ]
        return (
            LineageNode(
                kind=NodeKind.TOOL_VERSION,
                obj_id=version_id,
                # Descriptor name, not the alias ([[feedback_use_labels_not_names]]).
                label=f"{tool.name} v{version.version}",
                state=tool.state,
                archived=archived,
                deep_link=f"/admin/tools/{version.alias}/edit",
                payload={
                    "alias": version.alias,
                    "version": version.version,
                    "tool_state": tool.state,
                },
            ),
            upstream,
            [],
        )

    # ---- scenario ----------------------------------------------------

    async def _resolve_scenario(
        self,
        session: AsyncSession,
        ctx: _BuildContext,
        scenario_id: int,
    ) -> _ResolveResult:
        """Resolve a ``Scenario`` node with project + creator edges.

        ACL: VIEW on the parent project via the existing hierarchy
        helper.
        """
        from matofx.hierarchy.orm import Scenario  # noqa: PLC0415

        scenario = await session.get(Scenario, scenario_id)
        if scenario is None:
            return None
        try:
            await _require_project_access(
                session,
                project_id=scenario.project_id,
                actor_user_id=ctx.actor_user_id,
                required=ProjectAccessLevel.VIEW,
                resource_type="scenario",
                resource_id=scenario_id,
            )
        except (NotFoundError, AccessDeniedError):
            return None

        # Scenario has no creator FK (ADR-0002 deliberately doesn't
        # record one — the project Creator is the durable authority);
        # only the project edge is emitted upstream.
        ref = (NodeKind.SCENARIO, scenario_id)
        upstream: list[LineageEdge] = [
            LineageEdge(
                source=ref,
                target=(NodeKind.PROJECT, scenario.project_id),
                relation=EdgeKind.SCENARIO_IN_PROJECT,
                label=EDGE_LABELS[EdgeKind.SCENARIO_IN_PROJECT],
            ),
        ]
        return (
            LineageNode(
                kind=NodeKind.SCENARIO,
                obj_id=scenario_id,
                label=scenario.name,
                state=scenario.state,
                archived=scenario.state != "active",
                deep_link=f"/scenarios/{scenario_id}",
                payload={"name": scenario.name, "project_id": scenario.project_id},
            ),
            upstream,
            [],
        )

    # ---- project -----------------------------------------------------

    async def _resolve_project(
        self,
        session: AsyncSession,
        ctx: _BuildContext,
        project_id: int,
    ) -> _ResolveResult:
        """Resolve a ``Project`` node.

        ACL: VIEW on the project. The Creator user is emitted as a
        :attr:`EdgeKind.SUBMITTED_BY` edge (re-using the same relation
        name across hierarchy levels keeps the UI's relation glossary
        small — "submitted by" reads naturally on a project too).
        """
        from matofx.hierarchy.orm import Project  # noqa: PLC0415

        try:
            project = await _require_project_access(
                session,
                project_id=project_id,
                actor_user_id=ctx.actor_user_id,
                required=ProjectAccessLevel.VIEW,
                resource_type="project",
                resource_id=project_id,
            )
        except (NotFoundError, AccessDeniedError):
            return None
        assert isinstance(project, Project)  # noqa: S101 — narrowing for type-checker

        upstream: list[LineageEdge] = [
            LineageEdge(
                source=(NodeKind.PROJECT, project_id),
                target=(NodeKind.USER, project.creator_user_id),
                relation=EdgeKind.SUBMITTED_BY,
                label=EDGE_LABELS[EdgeKind.SUBMITTED_BY],
            )
        ]
        return (
            LineageNode(
                kind=NodeKind.PROJECT,
                obj_id=project_id,
                label=project.name,
                state=project.state,
                archived=project.state != "active",
                deep_link=f"/projects/{project_id}",
                payload={"name": project.name},
            ),
            upstream,
            [],
        )

    # ---- workflow run ------------------------------------------------

    async def _resolve_workflow_run(
        self,
        session: AsyncSession,
        ctx: _BuildContext,
        run_id: int,
    ) -> _ResolveResult:
        """Resolve a ``WorkflowRun`` node with scenario + creator edges.

        ACL: VIEW on the run's scenario's project (via
        :meth:`WorkflowRunService.get_run` semantics, replicated
        directly to avoid pulling the service into the constructor).

        Downstream edges: emits :attr:`EdgeKind.BELONGS_TO_WORKFLOW_RUN`
        in reverse for each child job — i.e. one edge per child Job
        with ``source=(JOB, child_id)``, ``target=(WORKFLOW_RUN,
        run_id)``. The BFS's downstream pass walks these as incoming
        edges.
        """
        from matofx.hierarchy.orm import Scenario  # noqa: PLC0415
        from matofx.workflows.orm import WorkflowRun  # noqa: PLC0415

        run = await session.get(WorkflowRun, run_id)
        if run is None:
            return None
        scenario = await session.get(Scenario, run.scenario_id)
        if scenario is None:
            return None
        try:
            await _require_project_access(
                session,
                project_id=scenario.project_id,
                actor_user_id=ctx.actor_user_id,
                required=ProjectAccessLevel.VIEW,
                resource_type="workflow_run",
                resource_id=run_id,
            )
        except (NotFoundError, AccessDeniedError):
            return None

        ref = (NodeKind.WORKFLOW_RUN, run_id)
        upstream: list[LineageEdge] = [
            LineageEdge(
                source=ref,
                target=(NodeKind.SCENARIO, run.scenario_id),
                relation=EdgeKind.BELONGS_TO_SCENARIO,
                label=EDGE_LABELS[EdgeKind.BELONGS_TO_SCENARIO],
            ),
            LineageEdge(
                source=ref,
                target=(NodeKind.USER, run.submitted_by_user_id),
                relation=EdgeKind.SUBMITTED_BY,
                label=EDGE_LABELS[EdgeKind.SUBMITTED_BY],
            ),
        ]

        # Downstream: each child Job has its own BELONGS_TO_WORKFLOW_RUN
        # edge pointing at this run. The BFS walks these in the
        # downstream pass.
        child_rows = await session.execute(
            select(Job.id, Job.workflow_position)
            .where(Job.workflow_run_id == run_id)
            .order_by(Job.workflow_position.asc(), Job.id.asc())
        )
        downstream: list[LineageEdge] = [
            LineageEdge(
                source=(NodeKind.JOB, child_id),
                target=ref,
                relation=EdgeKind.BELONGS_TO_WORKFLOW_RUN,
                label=EDGE_LABELS[EdgeKind.BELONGS_TO_WORKFLOW_RUN],
                field_name=str(position) if position is not None else None,
            )
            for child_id, position in child_rows.all()
        ]

        return (
            LineageNode(
                kind=NodeKind.WORKFLOW_RUN,
                obj_id=run_id,
                label=f"Workflow run #{run_id}",
                deep_link=f"/workflow-runs/{run_id}",
                payload={
                    "scenario_id": run.scenario_id,
                    "workflow_id": run.workflow_id,
                    "workflow_version_snapshot": run.workflow_version_snapshot,
                },
            ),
            upstream,
            downstream,
        )

    # ---- parametric study --------------------------------------------

    async def _resolve_parametric_study(
        self,
        session: AsyncSession,
        ctx: _BuildContext,
        study_id: int,
    ) -> _ResolveResult:
        """Resolve a ``ParametricStudy`` node with project / tool / creator edges.

        ACL: VIEW on the study's project. Downstream edges: each
        child Job's :attr:`EdgeKind.SEEDED_BY_STUDY` edge (reverse
        direction — source is the child Job, target is the study).
        """
        from matofx.studies.orm import ParametricStudy  # noqa: PLC0415

        study = await session.get(ParametricStudy, study_id)
        if study is None:
            return None
        try:
            await _require_project_access(
                session,
                project_id=study.project_id,
                actor_user_id=ctx.actor_user_id,
                required=ProjectAccessLevel.VIEW,
                resource_type="parametric_study",
                resource_id=study_id,
            )
        except (NotFoundError, AccessDeniedError):
            return None

        ref = (NodeKind.PARAMETRIC_STUDY, study_id)
        upstream: list[LineageEdge] = [
            LineageEdge(
                source=ref,
                target=(NodeKind.PROJECT, study.project_id),
                relation=EdgeKind.SCENARIO_IN_PROJECT,
                label=EDGE_LABELS[EdgeKind.SCENARIO_IN_PROJECT],
            ),
            LineageEdge(
                source=ref,
                target=(NodeKind.TOOL_VERSION, study.tool_version_id),
                relation=EdgeKind.RAN_TOOL_VERSION,
                label=EDGE_LABELS[EdgeKind.RAN_TOOL_VERSION],
            ),
            LineageEdge(
                source=ref,
                target=(NodeKind.USER, study.created_by_user_id),
                relation=EdgeKind.SUBMITTED_BY,
                label=EDGE_LABELS[EdgeKind.SUBMITTED_BY],
            ),
        ]

        child_rows = await session.execute(
            select(Job.id).where(Job.parametric_study_id == study_id).order_by(Job.id.asc())
        )
        downstream: list[LineageEdge] = [
            LineageEdge(
                source=(NodeKind.JOB, child_id),
                target=ref,
                relation=EdgeKind.SEEDED_BY_STUDY,
                label=EDGE_LABELS[EdgeKind.SEEDED_BY_STUDY],
            )
            for (child_id,) in child_rows.all()
        ]

        return (
            LineageNode(
                kind=NodeKind.PARAMETRIC_STUDY,
                obj_id=study_id,
                label=study.name,
                state=study.state,
                archived=study.state in {"archived", "cancelled"},
                deep_link=f"/studies/{study_id}",
                payload={
                    "name": study.name,
                    "tool_alias": study.tool_alias,
                    "tool_version_id": study.tool_version_id,
                },
            ),
            upstream,
            downstream,
        )

    # ---- optimisation study (ADR-0009) -------------------------------

    async def _resolve_optimisation_study(
        self,
        session: AsyncSession,
        ctx: _BuildContext,
        study_id: int,
    ) -> _ResolveResult:
        """Resolve an ``OptimisationStudy`` node — project / tool / creator + children.

        Mirrors the parametric-study shape (ADR-0010 §20.1): VIEW on the
        study's project; the pinned tool version (tool target only — a
        workflow target has no single tool); each child Job's reverse
        :attr:`EdgeKind.SEEDED_BY_STUDY` edge.
        """
        from matofx.studies.optimisation.orm import OptimisationStudy  # noqa: PLC0415

        study = await session.get(OptimisationStudy, study_id)
        if study is None:
            return None
        try:
            await _require_project_access(
                session,
                project_id=study.project_id,
                actor_user_id=ctx.actor_user_id,
                required=ProjectAccessLevel.VIEW,
                resource_type="optimisation_study",
                resource_id=study_id,
            )
        except (NotFoundError, AccessDeniedError):
            return None

        ref = (NodeKind.OPTIMISATION_STUDY, study_id)
        upstream: list[LineageEdge] = [
            LineageEdge(
                source=ref,
                target=(NodeKind.PROJECT, study.project_id),
                relation=EdgeKind.SCENARIO_IN_PROJECT,
                label=EDGE_LABELS[EdgeKind.SCENARIO_IN_PROJECT],
            ),
            LineageEdge(
                source=ref,
                target=(NodeKind.USER, study.created_by_user_id),
                relation=EdgeKind.SUBMITTED_BY,
                label=EDGE_LABELS[EdgeKind.SUBMITTED_BY],
            ),
        ]
        if study.tool_version_id is not None:
            upstream.append(
                LineageEdge(
                    source=ref,
                    target=(NodeKind.TOOL_VERSION, study.tool_version_id),
                    relation=EdgeKind.RAN_TOOL_VERSION,
                    label=EDGE_LABELS[EdgeKind.RAN_TOOL_VERSION],
                )
            )

        child_rows = await session.execute(
            select(Job.id).where(Job.optimisation_study_id == study_id).order_by(Job.id.asc())
        )
        downstream: list[LineageEdge] = [
            LineageEdge(
                source=(NodeKind.JOB, child_id),
                target=ref,
                relation=EdgeKind.SEEDED_BY_STUDY,
                label=EDGE_LABELS[EdgeKind.SEEDED_BY_STUDY],
            )
            for (child_id,) in child_rows.all()
        ]

        return (
            LineageNode(
                kind=NodeKind.OPTIMISATION_STUDY,
                obj_id=study_id,
                label=study.name,
                state=study.state,
                archived=study.archived_at is not None,
                deep_link=f"/optimisation/{study_id}",
                payload={
                    "name": study.name,
                    "target_kind": study.target_kind,
                    "tool_alias": study.tool_alias,
                    "workflow_alias": study.workflow_alias,
                },
            ),
            upstream,
            downstream,
        )

    # ---- library file version ----------------------------------------

    async def _resolve_library_file_version(
        self,
        session: AsyncSession,
        ctx: _BuildContext,
        version_id: int,
    ) -> _ResolveResult:
        """Resolve a ``LibraryFileVersion`` node with creator edge.

        ACL via :meth:`LibraryService.get_version` — version-scoped
        visibility is the file's visibility per ADR-0006 §3 (personal
        → owner only; project → project-VIEW; global → all
        authenticated, with pending versions hidden from non-curator
        non-owners).

        No downstream edges in slice 2 — walking from a version to the
        jobs that consumed it needs :meth:`LibraryService.list_consumers`
        which lands in slice 5 alongside the UI page that needs it.
        """
        from matofx.library import LibraryError, make_library_service  # noqa: PLC0415

        lib_service = make_library_service(self.audit)
        try:
            version = await lib_service.get_version(
                session, actor_user_id=ctx.actor_user_id, version_id=version_id
            )
        except LibraryError:
            return None

        upstream: list[LineageEdge] = [
            LineageEdge(
                source=(NodeKind.LIBRARY_FILE_VERSION, version_id),
                target=(NodeKind.USER, version.created_by_user_id),
                relation=EdgeKind.PUBLISHED_BY,
                label=EDGE_LABELS[EdgeKind.PUBLISHED_BY],
            )
        ]
        archived = version.publish_state in {"rejected"}
        return (
            LineageNode(
                kind=NodeKind.LIBRARY_FILE_VERSION,
                obj_id=version_id,
                label=f"{version.original_filename} (v{version.ordinal})",
                state=version.publish_state,
                archived=archived,
                deep_link=f"/library/{version.library_file_id}",
                payload={
                    "ordinal": version.ordinal,
                    "library_file_id": version.library_file_id,
                    "content_hash": version.content_hash,
                    "size_bytes": version.size_bytes,
                },
            ),
            upstream,
            [],
        )

    # ---- material version --------------------------------------------

    async def _resolve_material_version(
        self,
        session: AsyncSession,
        ctx: _BuildContext,  # noqa: ARG002 — materials are read-open (ADR-0007); no ACL state needed
        version_id: int,
    ) -> _ResolveResult:
        """Resolve a ``MaterialVersion`` node with creator edge.

        Materials are read-open to any authenticated user (ADR-0007 +
        design §4.3) — no scope-based visibility, no project gate.
        Existence is the only check.
        """
        from matofx.materials import MaterialsError, make_materials_service  # noqa: PLC0415

        mat_service = make_materials_service(self.audit)
        try:
            version = await mat_service.get_version(session, version_id)
        except MaterialsError:
            return None

        upstream: list[LineageEdge] = [
            LineageEdge(
                source=(NodeKind.MATERIAL_VERSION, version_id),
                target=(NodeKind.USER, version.created_by_user_id),
                relation=EdgeKind.PUBLISHED_BY,
                label=EDGE_LABELS[EdgeKind.PUBLISHED_BY],
            )
        ]
        archived = version.publish_state in {"rejected"}
        return (
            LineageNode(
                kind=NodeKind.MATERIAL_VERSION,
                obj_id=version_id,
                label=f"Material v{version.ordinal}",
                state=version.publish_state,
                archived=archived,
                deep_link=f"/materials/{version.material_id}",
                payload={
                    "ordinal": version.ordinal,
                    "material_id": version.material_id,
                },
            ),
            upstream,
            [],
        )

    # ---- surrogate model version -------------------------------------

    async def _resolve_surrogate_model_version(
        self,
        session: AsyncSession,
        ctx: _BuildContext,  # noqa: ARG002 — version reads ride the model's scope; existence-only here
        version_id: int,
    ) -> _ResolveResult:
        """Resolve a ``SurrogateModelVersion`` node (ADR-0008 §10 lineage).

        Emits the node plus its provenance edges: ``published_by`` → the
        user who fitted it, and (for a parametric-sourced fit)
        ``derived_from`` → the source study that composes back to the
        contributing runs. A cohort-sourced fit's ``job_cohort`` node
        lands with that resolver; until then the source edge is omitted
        rather than dangling. Naturally not-found when the surrogate
        feature is off (no such rows exist).
        """
        from matofx.models.orm import SurrogateModelVersion  # noqa: PLC0415

        version = await session.get(SurrogateModelVersion, version_id)
        if version is None:
            return None

        upstream: list[LineageEdge] = [
            LineageEdge(
                source=(NodeKind.SURROGATE_MODEL_VERSION, version_id),
                target=(NodeKind.USER, version.fitted_by_user_id),
                relation=EdgeKind.PUBLISHED_BY,
                label=EDGE_LABELS[EdgeKind.PUBLISHED_BY],
            )
        ]
        if version.source_kind == "parametric":
            upstream.append(
                LineageEdge(
                    source=(NodeKind.SURROGATE_MODEL_VERSION, version_id),
                    target=(NodeKind.PARAMETRIC_STUDY, version.source_id),
                    relation=EdgeKind.DERIVED_FROM,
                    label=EDGE_LABELS[EdgeKind.DERIVED_FROM],
                )
            )
        archived = version.publish_state in {"rejected"}
        return (
            LineageNode(
                kind=NodeKind.SURROGATE_MODEL_VERSION,
                obj_id=version_id,
                label=f"Surrogate v{version.ordinal}",
                state=version.publish_state,
                archived=archived,
                deep_link=f"/models/{version.surrogate_model_id}",
                payload={
                    "ordinal": version.ordinal,
                    "model_id": version.surrogate_model_id,
                    "kind": version.kind,
                    "source_kind": version.source_kind,
                },
            ),
            upstream,
            [],
        )

    # ---- approval request --------------------------------------------

    async def _resolve_approval_request(
        self,
        session: AsyncSession,
        ctx: _BuildContext,
        request_id: int,
    ) -> _ResolveResult:
        """Resolve an ``ApprovalRequest`` node.

        ACL (design §4.3): visible iff the actor was the requestor, the
        designated approver, an admin with ``projects.see_all``, or has
        VIEW on the target job's project (the most common path for
        users reaching approvals via job-centred lineage).
        Library/material/study-target approvals fall back to the
        "admin / requestor / approver" trio since those targets have
        their own visibility models.

        Edges: the requestor as :attr:`EdgeKind.SUBMITTED_BY` and the
        designated approver (if any) as :attr:`EdgeKind.PUBLISHED_BY`
        — the latter doubling as "this person decided / will decide
        this gate" without inventing a new relation kind.
        """
        from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

        request = await session.get(ApprovalRequest, request_id)
        if request is None:
            return None

        # ACL: build the visibility decision.
        visible = await self._approval_request_visible(session, ctx, request)
        if not visible:
            return None

        ref = (NodeKind.APPROVAL_REQUEST, request_id)
        upstream: list[LineageEdge] = [
            LineageEdge(
                source=ref,
                target=(NodeKind.USER, request.requested_by_user_id),
                relation=EdgeKind.SUBMITTED_BY,
                label=EDGE_LABELS[EdgeKind.SUBMITTED_BY],
            )
        ]
        if request.designated_approver_user_id is not None:
            upstream.append(
                LineageEdge(
                    source=ref,
                    target=(NodeKind.USER, request.designated_approver_user_id),
                    relation=EdgeKind.PUBLISHED_BY,
                    label=EDGE_LABELS[EdgeKind.PUBLISHED_BY],
                )
            )

        label = f"Approval ({request.kind}) #{request_id} [{request.status}]"
        return (
            LineageNode(
                kind=NodeKind.APPROVAL_REQUEST,
                obj_id=request_id,
                label=label,
                state=request.status,
                payload={
                    "kind": request.kind,
                    "status": request.status,
                },
            ),
            upstream,
            [],
        )

    async def _resolve_vv_record(
        self,
        session: AsyncSession,
        ctx: _BuildContext,
        record_id: int,
    ) -> _ResolveResult:
        """Resolve a V&V record node (ADR-0011 §10 lineage integration).

        Polymorphic over the two V&V record tables: a ``record_id`` is
        first looked up as a :class:`ToolVVRecord` (then falls back to
        :class:`JobVVRecord` when not present). The resolver emits the
        node plus one upstream edge to the record's target:

        - tool record of kind ``verification`` → :attr:`EdgeKind.VERIFIED_BY`
          → :attr:`NodeKind.TOOL_VERSION`.
        - tool record of kind ``validation`` → :attr:`EdgeKind.VALIDATED_BY`
          → :attr:`NodeKind.TOOL_VERSION`.
        - job record (any kind) → :attr:`EdgeKind.VALIDATED_BY` →
          :attr:`NodeKind.JOB`.

        ACL: V&V records are install-global reads — anyone authenticated
        can see one. The downstream tool / job resolver intersects ACL
        on the *target* during BFS, so a record pointing at a job the
        actor can't see still surfaces the V&V node but truncates the
        target edge cleanly.
        """
        del ctx  # V&V records are install-global; no per-actor ACL here
        from matofx.vv.orm import JobVVRecord, ToolVVRecord  # noqa: PLC0415

        tool_record = await session.get(ToolVVRecord, record_id)
        if tool_record is not None:
            relation = (
                EdgeKind.VERIFIED_BY
                if tool_record.kind == "verification"
                else EdgeKind.VALIDATED_BY
            )
            label = f"V&V {tool_record.kind} #{record_id} [{tool_record.state}]"
            node = LineageNode(
                kind=NodeKind.VV_RECORD,
                obj_id=record_id,
                label=label,
                state=tool_record.state,
                payload={
                    "record_kind": "tool",
                    "vv_kind": tool_record.kind,
                    "target_id": tool_record.tool_version_id,
                },
                deep_link=f"/vv-records/tool/{record_id}",
            )
            upstream = [
                LineageEdge(
                    source=(NodeKind.VV_RECORD, record_id),
                    target=(NodeKind.TOOL_VERSION, tool_record.tool_version_id),
                    relation=relation,
                    label=EDGE_LABELS[relation],
                )
            ]
            return node, upstream, []

        job_record = await session.get(JobVVRecord, record_id)
        if job_record is None:
            return None
        label = f"V&V {job_record.kind} #{record_id} [{job_record.state}]"
        node = LineageNode(
            kind=NodeKind.VV_RECORD,
            obj_id=record_id,
            label=label,
            state=job_record.state,
            payload={
                "record_kind": "job",
                "vv_kind": job_record.kind,
                "target_id": job_record.job_id,
            },
            deep_link=f"/vv-records/job/{record_id}",
        )
        upstream = [
            LineageEdge(
                source=(NodeKind.VV_RECORD, record_id),
                target=(NodeKind.JOB, job_record.job_id),
                relation=EdgeKind.VALIDATED_BY,
                label=EDGE_LABELS[EdgeKind.VALIDATED_BY],
            )
        ]
        return node, upstream, []

    async def _approval_request_visible(
        self,
        session: AsyncSession,
        ctx: _BuildContext,
        request: object,
    ) -> bool:
        """Apply the §4.3 approval visibility predicate.

        Pulled out as a method so the test suite can target it directly
        without going through a full ``build_graph`` call. The three
        always-visible paths (requestor / designated approver /
        ``projects.see_all`` admin) short-circuit first; the job-target
        fall-through inherits the target job's project ACL.
        Library/material/study-target gates without one of the
        always-visible paths fall through to hidden.
        """
        from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

        assert isinstance(request, ApprovalRequest)  # noqa: S101 — narrowing
        actor_id = ctx.actor_user_id
        participant_ids = {
            request.requested_by_user_id,
            request.designated_approver_user_id,
        }
        always_visible = actor_id in participant_ids or await user_has_permission(
            session, actor_id, Permission.PROJECTS_SEE_ALL
        )
        if always_visible:
            return True
        if request.job_id is None:
            return False
        job = await session.get(Job, request.job_id)
        if job is None:
            return False
        try:
            await _require_project_access(
                session,
                project_id=await _job_project_id(session, job),
                actor_user_id=actor_id,
                required=ProjectAccessLevel.VIEW,
                resource_type="job",
                resource_id=job.id,
            )
        except (NotFoundError, AccessDeniedError):
            return False
        return True


# Resolver dispatch table — kept module-level (not on the class) so
# adding a kind is a one-place edit and ``_resolve`` stays a 4-line
# lookup. Each function takes ``(service, session, ctx, obj_id)``.
_RESOLVERS: dict[
    NodeKind,
    Callable[[LineageService, AsyncSession, _BuildContext, int], Awaitable[_ResolveResult]],
] = {
    NodeKind.JOB: LineageService._resolve_job,
    NodeKind.USER: LineageService._resolve_user,
    NodeKind.TOOL_VERSION: LineageService._resolve_tool_version,
    NodeKind.SCENARIO: LineageService._resolve_scenario,
    NodeKind.PROJECT: LineageService._resolve_project,
    NodeKind.WORKFLOW_RUN: LineageService._resolve_workflow_run,
    NodeKind.PARAMETRIC_STUDY: LineageService._resolve_parametric_study,
    NodeKind.OPTIMISATION_STUDY: LineageService._resolve_optimisation_study,
    NodeKind.LIBRARY_FILE_VERSION: LineageService._resolve_library_file_version,
    NodeKind.MATERIAL_VERSION: LineageService._resolve_material_version,
    NodeKind.APPROVAL_REQUEST: LineageService._resolve_approval_request,
    NodeKind.VV_RECORD: LineageService._resolve_vv_record,
    NodeKind.SURROGATE_MODEL_VERSION: LineageService._resolve_surrogate_model_version,
    # Reserved-for-later: JOB_COHORT lands with ADR-0008's cohort lineage.
}


# ---- centre-list handlers (slice 5b) -----------------------------------
#
# One handler per :class:`NodeKind` the admin picker can list. Each
# handler delegates to its owning domain service, so visibility
# tracks whatever the domain already enforces — no parallel ACL
# code on the lineage side.


async def _list_jobs(session: AsyncSession, actor_user_id: int, limit: int) -> list[LineageCentre]:
    from matofx.audit import AuditWriter  # noqa: PLC0415
    from matofx.hierarchy.service import make_job_service  # noqa: PLC0415

    svc = make_job_service(AuditWriter(signing_key=None, key_id=None))
    rows = await svc.list_visible_to_user(session, actor_user_id=actor_user_id, limit=limit)
    return [
        LineageCentre(
            obj_id=int(r.id),
            label=f"#{r.id}: {r.tool_alias} — {r.name}",
            state=r.state,
        )
        for r in rows
    ]


async def _list_workflow_runs(
    session: AsyncSession, actor_user_id: int, limit: int
) -> list[LineageCentre]:
    from matofx.audit import AuditWriter  # noqa: PLC0415
    from matofx.workflows.run_service import make_workflow_run_service  # noqa: PLC0415

    svc = make_workflow_run_service(AuditWriter(signing_key=None, key_id=None))
    rows = await svc.list_visible(session, actor_user_id=actor_user_id, limit=limit)
    return [
        LineageCentre(
            obj_id=int(r.id),
            label=f"#{r.id}: {r.name}",
        )
        for r in rows
    ]


async def _list_parametric_studies(
    session: AsyncSession, actor_user_id: int, limit: int
) -> list[LineageCentre]:
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.studies.orm import ParametricStudy  # noqa: PLC0415

    # Pull every study whose project the actor can see (via the
    # ProjectService's user-visible projects). Cheaper than asking
    # the study service per-project because the picker only needs
    # a flat list.
    visible_project_ids = await _visible_project_ids(session, actor_user_id)
    if not visible_project_ids:
        return []
    stmt = (
        select(ParametricStudy.id, ParametricStudy.name, ParametricStudy.state)
        .where(ParametricStudy.project_id.in_(visible_project_ids))
        .order_by(ParametricStudy.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        LineageCentre(obj_id=int(r[0]), label=f"#{r[0]}: {r[1]}", state=str(r[2] or ""))
        for r in rows
    ]


async def _list_optimisation_studies(
    session: AsyncSession, actor_user_id: int, limit: int
) -> list[LineageCentre]:
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.studies.optimisation.orm import OptimisationStudy  # noqa: PLC0415

    visible_project_ids = await _visible_project_ids(session, actor_user_id)
    if not visible_project_ids:
        return []
    stmt = (
        select(OptimisationStudy.id, OptimisationStudy.name, OptimisationStudy.state)
        .where(OptimisationStudy.project_id.in_(visible_project_ids))
        .order_by(OptimisationStudy.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        LineageCentre(obj_id=int(r[0]), label=f"#{r[0]}: {r[1]}", state=str(r[2] or ""))
        for r in rows
    ]


async def _list_scenarios(
    session: AsyncSession, actor_user_id: int, limit: int
) -> list[LineageCentre]:
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.hierarchy.orm import Scenario  # noqa: PLC0415

    visible_project_ids = await _visible_project_ids(session, actor_user_id)
    if not visible_project_ids:
        return []
    stmt = (
        select(Scenario.id, Scenario.name, Scenario.state)
        .where(Scenario.project_id.in_(visible_project_ids))
        .order_by(Scenario.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        LineageCentre(obj_id=int(r[0]), label=f"#{r[0]}: {r[1]}", state=str(r[2] or ""))
        for r in rows
    ]


async def _list_projects(
    session: AsyncSession, actor_user_id: int, limit: int
) -> list[LineageCentre]:
    from matofx.audit import AuditWriter  # noqa: PLC0415
    from matofx.hierarchy.service import make_project_service  # noqa: PLC0415

    svc = make_project_service(AuditWriter(signing_key=None, key_id=None))
    rows = await svc.list_for_user(session, actor_user_id=actor_user_id, include_archived=True)
    return [
        LineageCentre(obj_id=int(p.id), label=f"#{p.id}: {p.name}", state=str(p.state or ""))
        for p in rows[:limit]
    ]


async def _list_tool_versions(
    session: AsyncSession, actor_user_id: int, limit: int
) -> list[LineageCentre]:
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415
    from matofx.tools.states import user_can_see_state  # noqa: PLC0415

    # Pull every (tool, latest-version) pair, then filter by visibility.
    stmt = (
        select(ToolVersion.id, ToolVersion.alias, ToolVersion.version, Tool.state)
        .join(Tool, Tool.alias == ToolVersion.alias)
        .order_by(ToolVersion.id.desc())
        .limit(limit * 2)  # over-fetch to cover state filtering below
    )
    rows = (await session.execute(stmt)).all()
    out: list[LineageCentre] = []
    for tv_id, alias, version, tool_state in rows:
        if not await user_can_see_state(session, actor_user_id, tool_state):
            continue
        out.append(LineageCentre(obj_id=int(tv_id), label=f"#{tv_id}: {alias} v{version}"))
        if len(out) >= limit:
            break
    return out


async def _list_library_file_versions(
    session: AsyncSession, actor_user_id: int, limit: int
) -> list[LineageCentre]:
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.audit import AuditWriter  # noqa: PLC0415
    from matofx.library.orm import LibraryFileVersion  # noqa: PLC0415
    from matofx.library.service import make_library_service  # noqa: PLC0415

    svc = make_library_service(AuditWriter(signing_key=None, key_id=None))
    files = await svc.list_files(session, actor_user_id=actor_user_id, include_retired=True)
    file_ids = [int(f.id) for f in files][:limit]
    if not file_ids:
        return []
    # Latest version per file. ``ROW_NUMBER`` would be cleaner but
    # the test set is small enough that a per-file scan is fine.
    stmt = (
        select(
            LibraryFileVersion.id, LibraryFileVersion.library_file_id, LibraryFileVersion.ordinal
        )
        .where(LibraryFileVersion.library_file_id.in_(file_ids))
        .order_by(LibraryFileVersion.library_file_id, LibraryFileVersion.ordinal.desc())
    )
    rows = (await session.execute(stmt)).all()
    seen: set[int] = set()
    by_file_name = {int(f.id): f.name for f in files}
    out: list[LineageCentre] = []
    for ver_id, file_id, ordinal in rows:
        if int(file_id) in seen:
            continue
        seen.add(int(file_id))
        out.append(
            LineageCentre(
                obj_id=int(ver_id),
                label=f"#{ver_id}: {by_file_name.get(int(file_id), '?')} (v{ordinal})",
            )
        )
        if len(out) >= limit:
            break
    return out


async def _list_material_versions(
    session: AsyncSession,
    actor_user_id: int,  # noqa: ARG001 — materials are read-open (ADR-0007)
    limit: int,
) -> list[LineageCentre]:
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.materials.orm import Material, MaterialVersion  # noqa: PLC0415

    stmt = (
        select(
            MaterialVersion.id, MaterialVersion.material_id, MaterialVersion.ordinal, Material.name
        )
        .join(Material, Material.id == MaterialVersion.material_id)
        .order_by(MaterialVersion.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [LineageCentre(obj_id=int(r[0]), label=f"#{r[0]}: {r[3]} (v{r[2]})") for r in rows]


async def _list_surrogate_model_versions(
    session: AsyncSession,
    actor_user_id: int,  # noqa: ARG001 — the BFS ACL-checks each expanded target
    limit: int,
) -> list[LineageCentre]:
    """Surrogate model version centres — most-recent first (ADR-0008 §10).

    Lists across scopes for the admin picker; the BFS still intersects
    ACL on every expanded edge target. Empty when the surrogate feature
    has never been used (no rows).
    """
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.models.orm import SurrogateModel, SurrogateModelVersion  # noqa: PLC0415

    stmt = (
        select(
            SurrogateModelVersion.id,
            SurrogateModelVersion.ordinal,
            SurrogateModelVersion.publish_state,
            SurrogateModel.name,
        )
        .join(SurrogateModel, SurrogateModel.id == SurrogateModelVersion.surrogate_model_id)
        .order_by(SurrogateModelVersion.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        LineageCentre(obj_id=int(r[0]), label=f"#{r[0]}: {r[3]} (v{r[1]})", state=str(r[2] or ""))
        for r in rows
    ]


async def _list_users(session: AsyncSession, actor_user_id: int, limit: int) -> list[LineageCentre]:
    # The user list is admin-only — surfaces every account, so the
    # picker is gated to operators with ``projects.see_all`` (the
    # closest existing admin marker; falls under the same authority
    # band as the admin nav presence the page already sits behind).
    if not await user_has_permission(session, actor_user_id, Permission.PROJECTS_SEE_ALL):
        return []
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.users.orm import User  # noqa: PLC0415

    stmt = select(User.id, User.username, User.full_name).order_by(User.id.asc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [
        LineageCentre(
            obj_id=int(r[0]),
            label=f"#{r[0]}: {r[1]}" + (f" ({r[2]})" if r[2] else ""),
        )
        for r in rows
    ]


async def _list_approval_requests(
    session: AsyncSession, actor_user_id: int, limit: int
) -> list[LineageCentre]:
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

    # Show the actor's recent approval activity (requested OR
    # designated-approver). Admins with PROJECTS_SEE_ALL get the
    # cross-install set; everyone else gets their own pending +
    # decided rows.
    if await user_has_permission(session, actor_user_id, Permission.PROJECTS_SEE_ALL):
        stmt = (
            select(ApprovalRequest.id, ApprovalRequest.kind, ApprovalRequest.status)
            .order_by(ApprovalRequest.id.desc())
            .limit(limit)
        )
    else:
        stmt = (
            select(ApprovalRequest.id, ApprovalRequest.kind, ApprovalRequest.status)
            .where(
                (ApprovalRequest.requested_by_user_id == actor_user_id)
                | (ApprovalRequest.designated_approver_user_id == actor_user_id)
            )
            .order_by(ApprovalRequest.id.desc())
            .limit(limit)
        )
    rows = (await session.execute(stmt)).all()
    return [
        LineageCentre(
            obj_id=int(r[0]),
            label=f"#{r[0]}: {r[1]} [{r[2]}]",
            state=str(r[2] or ""),
        )
        for r in rows
    ]


async def _list_vv_records(
    session: AsyncSession, actor_user_id: int, limit: int
) -> list[LineageCentre]:
    """V&V record centres — most-recent first, polymorphic over tool + job records.

    V&V records are install-global; reads open to any authenticated user
    (ADR-0011 §3). We don't filter by actor here — the BFS still ACL-checks
    each target tool / job individually when expanding the graph.
    """
    from sqlalchemy import literal, select, union_all  # noqa: PLC0415

    from matofx.vv.orm import JobVVRecord, ToolVVRecord  # noqa: PLC0415

    del actor_user_id  # V&V reads are install-global per ADR-0011 §3
    tool_stmt = select(
        ToolVVRecord.id.label("rid"),
        ToolVVRecord.kind.label("vv_kind"),
        ToolVVRecord.state.label("state"),
        literal("tool").label("record_kind"),
        ToolVVRecord.recorded_at.label("recorded_at"),
    )
    job_stmt = select(
        JobVVRecord.id.label("rid"),
        JobVVRecord.kind.label("vv_kind"),
        JobVVRecord.state.label("state"),
        literal("job").label("record_kind"),
        JobVVRecord.recorded_at.label("recorded_at"),
    )
    combined = union_all(tool_stmt, job_stmt).subquery()
    stmt = select(combined).order_by(combined.c.recorded_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [
        LineageCentre(
            obj_id=int(r.rid),
            label=f"#{r.rid}: {r.record_kind} / {r.vv_kind} [{r.state}]",
            state=str(r.state or ""),
        )
        for r in rows
    ]


async def _visible_project_ids(session: AsyncSession, actor_user_id: int) -> list[int]:
    """Project ids the actor can VIEW — shared by scenario + study list helpers."""
    from matofx.audit import AuditWriter  # noqa: PLC0415
    from matofx.hierarchy.service import make_project_service  # noqa: PLC0415

    svc = make_project_service(AuditWriter(signing_key=None, key_id=None))
    rows = await svc.list_for_user(session, actor_user_id=actor_user_id, include_archived=True)
    return [int(p.id) for p in rows]


_CENTRE_LIST_HANDLERS: dict[
    NodeKind,
    Callable[[AsyncSession, int, int], Awaitable[list[LineageCentre]]],
] = {
    NodeKind.JOB: _list_jobs,
    NodeKind.WORKFLOW_RUN: _list_workflow_runs,
    NodeKind.PARAMETRIC_STUDY: _list_parametric_studies,
    NodeKind.OPTIMISATION_STUDY: _list_optimisation_studies,
    NodeKind.SCENARIO: _list_scenarios,
    NodeKind.PROJECT: _list_projects,
    NodeKind.TOOL_VERSION: _list_tool_versions,
    NodeKind.LIBRARY_FILE_VERSION: _list_library_file_versions,
    NodeKind.MATERIAL_VERSION: _list_material_versions,
    NodeKind.USER: _list_users,
    NodeKind.APPROVAL_REQUEST: _list_approval_requests,
    NodeKind.VV_RECORD: _list_vv_records,
    NodeKind.SURROGATE_MODEL_VERSION: _list_surrogate_model_versions,
    # Reserved-for-later (no ORM yet): JOB_COHORT returns [].
}


async def _resolve_soft_cap(session: AsyncSession) -> int:
    """Read the live ``lineage_max_nodes`` General Setting.

    Falls through to :data:`_DEFAULT_SOFT_CAP` (500) when the
    setting hasn't been overridden or the read fails. The lookup
    runs once per ``build_graph`` call — for a graph build that
    already touches dozens of rows, the extra small SELECT is
    negligible, and centralising it here means every callsite
    (API, tests, future reports + UI mounts) honours the same
    operator-tunable cap.
    """
    try:
        from matofx.config.general_service import load_general_settings  # noqa: PLC0415

        settings = await load_general_settings(session)
    except Exception:  # never let setting load take a build down
        return _DEFAULT_SOFT_CAP
    raw = settings.get("lineage_max_nodes", _DEFAULT_SOFT_CAP)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_SOFT_CAP


def make_lineage_service(audit: AuditWriter) -> LineageService:
    """Factory mirroring :func:`matofx.library.service.make_library_service`."""
    return LineageService(audit=audit)
