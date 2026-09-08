"""WorkflowGraphService — assemble the structure graph for one workflow.

This is the structural projection of a workflow (ADR-0013), not its
provenance. Where :class:`matofx.lineage.LineageService` walks recorded
FK relations to answer "what fed this row?", this service projects a
workflow's *descriptor-declared* wiring onto a Cytoscape-renderable
graph — answering "if this workflow ran, what would feed what?". The
two services co-exist; the UI mounts both via :func:`render_cytoscape_canvas`.

Read-only. No method here mutates a workflow or job row — the only
side effect is the audit row that ``build`` writes so compliance can
trace who looked at which workflow's graph.

Per-node runtime overlay (when ``run_id`` is supplied) reads child
job states off :class:`matofx.workflows.orm.WorkflowRun` + its child
:class:`matofx.hierarchy.orm.Job` rows. The vocabulary matches the
workflow-run page's tab statuses
(:mod:`matofx.ui.pages.workflow_run._tab_status_for`) so the badge in
the graph and the badge on the tab strip read the same word.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from matofx.audit.schema import RecordInput
from matofx.hierarchy.errors import AccessDeniedError, NotFoundError
from matofx.hierarchy.orm import Job, ProjectAccessLevel, Scenario
from matofx.hierarchy.service import _require_project_access
from matofx.tools.orm import Tool
from matofx.workflows._custom_helpers import load_tool_descriptors, user_can_see_tool
from matofx.workflows.errors import (
    DuplicateMemberError,
    UnknownToolMemberError,
    WorkflowNotFoundError,
)
from matofx.workflows.graph_types import (
    WorkflowGraph,
    WorkflowGraphEdge,
    WorkflowGraphNode,
    WorkflowNodeRunState,
)
from matofx.workflows.ordering import infer_edges_with_fields
from matofx.workflows.orm import (
    Workflow,
    WorkflowMember,
    WorkflowRun,
)
from matofx.workflows.run_service import compute_state

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


@dataclass(frozen=True)
class WorkflowGraphService:
    """Read-only graph assembly over the workflow + descriptor data.

    Construct one per process; cheap dataclass with a single audit
    writer reference. Methods are stateless beyond what the supplied
    session reads. Mirrors :class:`matofx.lineage.LineageService`'s
    shape so the two services compose the same way in dependency-
    injection wiring.
    """

    audit: AuditWriter

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def build(
        self,
        session: AsyncSession,
        *,
        workflow_id: int,
        actor_user_id: int,
        run_id: int | None = None,
    ) -> WorkflowGraph:
        """Build the workflow-structure graph for ``workflow_id``.

        ``run_id`` is optional — when omitted, the graph is structure-
        only (no per-node runtime state). When set, the graph is
        overlaid with each child job's current state (``ready`` /
        ``waiting`` / ``queued`` / ``running`` / ``complete`` /
        ``failed``).

        ACL:

        - Workflow must be visible to the caller via the same gate
          :meth:`WorkflowService.list_visible` /
          :meth:`WorkflowService.list_for_user` use, modulo the
          inspector permission. Failure existence-hides
          (:class:`WorkflowNotFoundError` → 404).
        - When ``run_id`` is set, the run's scenario must pass VIEW
          on the owning project (same gate
          :meth:`WorkflowRunService.get_run` enforces). Failure
          existence-hides (:class:`NotFoundError` → 404).

        Raises:
        - :class:`WorkflowNotFoundError` — workflow doesn't exist, or
          the caller can't see it.
        - :class:`NotFoundError` — ``run_id`` supplied but the run
          doesn't exist / isn't on a visible scenario / belongs to a
          different workflow than the one being graphed (the last
          case prevents a hostile caller from cross-pollinating the
          per-node overlay with a foreign run).
        """
        workflow = await session.get(Workflow, workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"No workflow with id={workflow_id}.")
        if not await _user_can_see_workflow(session, workflow, actor_user_id):
            # Existence-hide: same shape an unknown id would surface as.
            raise WorkflowNotFoundError(f"No workflow with id={workflow_id}.")

        # Load members in display order — these become graph nodes.
        members = list(
            (
                await session.execute(
                    select(WorkflowMember)
                    .where(WorkflowMember.workflow_id == workflow.id)
                    .order_by(WorkflowMember.position)
                )
            ).scalars()
        )
        member_aliases = [m.tool_alias for m in members]

        # Pull each member tool's record (state, archived_at, name) and
        # descriptor in two queries so a workflow with N members doesn't
        # fan out to N+1 selects.
        tools_by_alias = await _load_tools(session, member_aliases)
        descriptors_by_alias = await load_tool_descriptors(session, member_aliases)

        edges_set, labels_by_edge = infer_edges_with_fields(member_aliases, descriptors_by_alias)
        edges = tuple(
            WorkflowGraphEdge(
                source=src,
                target=tgt,
                field_labels=tuple(labels_by_edge.get((src, tgt), [])),
            )
            for src, tgt in sorted(edges_set)
        )

        # Run-overlay lookup: child job state by tool_alias, plus the
        # run-level state rollup. ``None`` skips the overlay entirely.
        run_state_by_alias: dict[str, WorkflowNodeRunState] = {}
        child_job_id_by_alias: dict[str, int] = {}
        run_rollup: str | None = None
        resolved_run_id: int | None = None
        if run_id is not None:
            run, children = await _load_run_and_children(
                session,
                workflow_id=workflow_id,
                run_id=run_id,
                actor_user_id=actor_user_id,
            )
            resolved_run_id = run.id
            run_rollup = compute_state(run, children)
            completed_aliases = {c.tool_alias for c in children if c.state == "complete"}
            for child in children:
                child_job_id_by_alias[child.tool_alias] = child.id
                run_state_by_alias[child.tool_alias] = _resolve_node_run_state(
                    child=child,
                    completed_aliases=completed_aliases,
                    descriptors_by_alias=descriptors_by_alias,
                    workflow_aliases=set(member_aliases),
                )

        nodes = tuple(
            _build_node(
                member=member,
                tool=tools_by_alias.get(member.tool_alias),
                run_state=run_state_by_alias.get(member.tool_alias),
                child_job_id=child_job_id_by_alias.get(member.tool_alias),
            )
            for member in members
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.graph.viewed",
                resource_type="workflow",
                resource_id=str(workflow.id),
                outcome="success",
                payload={
                    "workflow_alias": workflow.alias,
                    "node_count": len(nodes),
                    "edge_count": len(edges),
                    "run_id": resolved_run_id,
                },
            ),
        )

        return WorkflowGraph(
            workflow_id=workflow.id,
            workflow_alias=workflow.alias,
            workflow_name=workflow.name,
            workflow_version=workflow.version,
            workflow_state="archived" if workflow.archived_at is not None else workflow.state,
            nodes=nodes,
            edges=edges,
            run_id=resolved_run_id,
            run_state=run_rollup,
        )

    async def build_preview(
        self,
        session: AsyncSession,
        *,
        tool_aliases: list[str],
        actor_user_id: int,
    ) -> WorkflowGraph:
        """Project a structure graph for an *unsaved* tool selection.

        Same descriptor-inferred wiring as :meth:`build`, but for a
        workflow that doesn't exist yet — the user is still picking
        tools in the create / edit dialog and wants to see the shape
        their choices produce before saving.

        ACL: each alias in ``tool_aliases`` must be a tool the caller
        can see under the standard state + team gate (mirrors
        :func:`_assert_member_tools_visible` from
        :mod:`matofx.workflows.service`). Unknown or hidden aliases
        raise :class:`UnknownToolMemberError` (→ 400) — same shape
        the workflow create / update paths surface, so the UI's
        existing error handling fits.

        Duplicate aliases raise :class:`DuplicateMemberError` (→ 400);
        a workflow cannot list the same tool twice, and a preview
        shouldn't lie to the user about a graph the save would
        reject.

        The returned :class:`WorkflowGraph` carries
        ``workflow_id=0`` as a preview sentinel (the API mapper
        translates this to ``None`` on the wire) and synthetic header
        values so the dataclass contract stays whole. No run
        overlay: a preview has no run.

        Audit: writes one ``workflows.graph.preview`` row per call
        with the alias list + counts. Useful for telemetry on which
        combos people preview before committing.
        """
        if not tool_aliases:
            raise UnknownToolMemberError(
                ""
            )  # pragma: no cover — Pydantic min_length=1 catches first

        # Dedup check — mirrors WorkflowService.install / create_custom.
        seen: set[str] = set()
        for alias in tool_aliases:
            if alias in seen:
                raise DuplicateMemberError(alias)
            seen.add(alias)

        tools_by_alias = await _load_tools(session, tool_aliases)
        # Existence-check every alias *before* the visibility check so
        # "unknown" wins over "hidden" — the caller never gets a
        # different error on aliases they don't own anyway.
        for alias in tool_aliases:
            if alias not in tools_by_alias:
                raise UnknownToolMemberError(alias)
        # Per-tool visibility — same predicate the save paths use.
        for alias in tool_aliases:
            if not await user_can_see_tool(
                session, user_id=actor_user_id, tool=tools_by_alias[alias]
            ):
                # Existence-hide hidden aliases as "unknown" — matches
                # _assert_member_tools_visible's contract.
                raise UnknownToolMemberError(alias)

        descriptors_by_alias = await load_tool_descriptors(session, tool_aliases)
        edges_set, labels_by_edge = infer_edges_with_fields(tool_aliases, descriptors_by_alias)
        edges = tuple(
            WorkflowGraphEdge(
                source=src,
                target=tgt,
                field_labels=tuple(labels_by_edge.get((src, tgt), [])),
            )
            for src, tgt in sorted(edges_set)
        )

        # Build nodes in user-supplied order. Position is the index
        # (1-based) — the same numbering the create dialog will save
        # if the user commits these picks unchanged.
        nodes = tuple(
            _build_preview_node(position=idx, tool=tools_by_alias[alias])
            for idx, alias in enumerate(tool_aliases, start=1)
        )

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="workflows.graph.preview",
                resource_type="workflow",
                resource_id="(preview)",
                outcome="success",
                payload={
                    "tool_aliases": list(tool_aliases),
                    "node_count": len(nodes),
                    "edge_count": len(edges),
                },
            ),
        )

        return WorkflowGraph(
            workflow_id=0,
            workflow_alias="",
            workflow_name="Preview",
            workflow_version="",
            workflow_state="preview",
            nodes=nodes,
            edges=edges,
            run_id=None,
            run_state=None,
        )


def make_workflow_graph_service(audit: AuditWriter) -> WorkflowGraphService:
    """Factory mirroring :func:`make_workflow_service`."""
    return WorkflowGraphService(audit=audit)


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _build_preview_node(*, position: int, tool: Tool) -> WorkflowGraphNode:
    """Project a preview-mode node from a position + Tool row.

    Distinct from :func:`_build_node` because previews have no
    :class:`WorkflowMember` row — position comes from the index in
    the user-supplied alias list, not from the DB. No
    ``child_job_id`` / ``run_state`` (there's no run); deep_link
    always points at the tool admin edit page.
    """
    archived = tool.archived_at is not None
    return WorkflowGraphNode(
        tool_alias=tool.alias,
        label=f"{position}. {tool.name}",
        position=position,
        tool_state="archived" if archived else tool.state,
        archived=archived,
        deep_link=f"/admin/tools/{tool.alias}/edit",
        run_state=None,
        child_job_id=None,
        payload={
            "tool_name": tool.name,
            "tool_version": tool.version,
        },
    )


def _build_node(
    *,
    member: WorkflowMember,
    tool: Tool | None,
    run_state: WorkflowNodeRunState | None,
    child_job_id: int | None,
) -> WorkflowGraphNode:
    """Project a (member, tool, run-state) tuple into a graph node.

    Falls back to the bare ``tool_alias`` when the tool record can't
    be resolved — matches how :mod:`matofx.api.routers.workflows`
    surfaces missing-tool members on the admin page rather than 500.
    """
    if tool is None:
        return WorkflowGraphNode(
            tool_alias=member.tool_alias,
            label=member.tool_alias,
            position=member.position,
            tool_state=None,
            archived=False,
            deep_link=None,
            run_state=run_state,
            child_job_id=child_job_id,
            payload={"missing": True},
        )

    archived = tool.archived_at is not None
    # Click target depends on whether the graph is run-scoped. In run
    # mode, dropping the user into the child Job page mirrors the run-
    # page tab behaviour; in structure mode, /admin/tools/{alias}/edit
    # is where the tool's editable surface lives.
    if child_job_id is not None:
        deep_link = f"/jobs/{child_job_id}"
    else:
        deep_link = f"/admin/tools/{tool.alias}/edit"
    return WorkflowGraphNode(
        tool_alias=tool.alias,
        label=f"{member.position}. {tool.name}",
        position=member.position,
        tool_state="archived" if archived else tool.state,
        archived=archived,
        deep_link=deep_link,
        run_state=run_state,
        child_job_id=child_job_id,
        payload={
            "tool_name": tool.name,
            "tool_version": tool.version,
        },
    )


def _resolve_node_run_state(
    *,
    child: Job,
    completed_aliases: set[str],
    descriptors_by_alias: dict[str, dict[str, Any]],
    workflow_aliases: set[str],
) -> WorkflowNodeRunState:
    """Map a child job's state into the closed graph-node vocabulary.

    Mirrors :func:`matofx.ui.pages.workflow_run._tab_status_for` —
    a child in ``created`` state distinguishes ``ready`` vs
    ``waiting`` by whether every upstream member tool is complete.
    A child whose upstream isn't part of *this* workflow is treated
    as a free-entry field (doesn't block readiness), matching the
    run page's ``_all_upstream_ready`` semantics.
    """
    state = child.state
    if state == "complete":
        return WorkflowNodeRunState.COMPLETE
    if state == "failed":
        return WorkflowNodeRunState.FAILED
    if state == "running":
        return WorkflowNodeRunState.RUNNING
    if state == "queued":
        return WorkflowNodeRunState.QUEUED
    # state == "created" — depends on upstream readiness.
    descriptor = descriptors_by_alias.get(child.tool_alias) or {}
    inputs = descriptor.get("inputs") or []
    if isinstance(inputs, list):
        for input_field in inputs:
            if not isinstance(input_field, dict):
                continue
            if not input_field.get("downstream_input"):
                continue
            upstream = input_field.get("upstream_tool")
            if not isinstance(upstream, str):
                continue
            if upstream not in workflow_aliases:
                # Upstream isn't part of this workflow — falls back to
                # free entry; doesn't gate readiness.
                continue
            if upstream not in completed_aliases:
                return WorkflowNodeRunState.WAITING
    return WorkflowNodeRunState.READY


async def _load_tools(session: AsyncSession, aliases: list[str]) -> dict[str, Tool]:
    """Return ``{alias: Tool}`` for the supplied member aliases."""
    if not aliases:
        return {}
    stmt = select(Tool).where(Tool.alias.in_(list(set(aliases))))
    return {tool.alias: tool for tool in (await session.execute(stmt)).scalars()}


async def _load_run_and_children(
    session: AsyncSession,
    *,
    workflow_id: int,
    run_id: int,
    actor_user_id: int,
) -> tuple[WorkflowRun, list[Job]]:
    """Fetch the run + children with the same ACL ``get_run`` applies.

    Existence-hides on three failure modes:

    1. Run row doesn't exist.
    2. Run row belongs to a workflow other than ``workflow_id``
       (prevents cross-workflow overlay smuggling).
    3. Caller lacks VIEW on the run's scenario's project.

    All three collapse to :class:`NotFoundError` so the surface error
    contract matches whatever ``get_run`` would have produced.
    """
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        raise NotFoundError("workflow_run", run_id)
    if run.workflow_id != workflow_id:
        # The run belongs to a different workflow — refuse rather than
        # silently overlay foreign state. Existence-hide via the same
        # error shape as a missing row.
        raise NotFoundError("workflow_run", run_id)
    scenario = await session.get(Scenario, run.scenario_id)
    if scenario is None:
        raise NotFoundError("workflow_run", run_id)
    try:
        await _require_project_access(
            session,
            project_id=scenario.project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.VIEW,
            resource_type="workflow_run",
            resource_id=run_id,
        )
    except AccessDeniedError as exc:
        raise NotFoundError("workflow_run", run_id) from exc

    children = list(
        (
            await session.execute(
                select(Job).where(Job.workflow_run_id == run_id).order_by(Job.workflow_position)
            )
        ).scalars()
    )
    return run, children


async def _user_can_see_workflow(
    session: AsyncSession,
    workflow: Workflow,
    actor_user_id: int,
) -> bool:
    """Per-workflow visibility predicate covering admin + custom rows.

    Thin delegate to :func:`matofx.workflows.service.can_user_see_workflow`
    — the single kind-aware source of truth. For admin rows that means
    the state + team gates **and the member cascade** (a workflow can't
    expose a ``new``/``test`` or team-restricted member tool the caller
    couldn't see directly); for custom rows, owner / share-recipient /
    inspector scope.
    """
    # Lazy import to avoid the workflows.service ↔ graph_service cycle.
    from matofx.workflows.service import can_user_see_workflow  # noqa: PLC0415

    return await can_user_see_workflow(session, actor_user_id=actor_user_id, wf=workflow)
