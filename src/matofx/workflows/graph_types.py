"""Frozen value types for the workflow-structure graph (ADR-0013).

This is the structural cousin of :mod:`matofx.lineage.types`. Where
lineage walks DB-recorded provenance ("what fed this row"), the
workflow graph projects a workflow's *descriptor-inferred* shape ("what
the tools would feed each other if this workflow ran"). The two share
a Cytoscape canvas (:mod:`matofx.ui._cytoscape`) but their models stay
separate so neither one's evolution can drag the other along.

The graph is built per-workflow, optionally overlaid with a specific
run's per-node runtime state. The standalone-tool case — a member tool
with no descriptor-declared upstream / downstream within the chosen
set — renders as a floating node; dagre's natural layout drops
unconnected nodes onto their own rank.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class WorkflowNodeRunState(StrEnum):
    """Per-node runtime state when the graph is built with a ``run_id``.

    Mirrors the workflow-run page's tab vocabulary
    (:mod:`matofx.ui.pages.workflow_run._tab_status_for`) so a user
    looking at the graph reads the same status words as on the tabs:

    - ``ready`` — child job is ``created`` and every upstream member
      tool has produced its outputs; the user can fill the form and
      submit.
    - ``waiting`` — child job is ``created`` but at least one upstream
      member tool isn't ``complete`` yet.
    - ``queued`` — child job has been submitted to the runner.
    - ``running`` — child job is executing.
    - ``complete`` — child job finished successfully.
    - ``failed`` — child job failed; the run is stuck on this node
      until the user re-submits.

    The state vocabulary is intentionally a closed enum: the cytoscape
    stylesheet branches on these values for per-state node colour, and
    a stray fresh value would render as the default style without any
    feedback to the developer.
    """

    READY = "ready"
    WAITING = "waiting"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WorkflowGraphNode:
    """One node in the workflow-structure graph — one member tool.

    Identifier is ``tool_alias`` (the workflow membership stores
    aliases, not tool ids — see :class:`matofx.workflows.orm.WorkflowMember`).
    Aliases are unique inside a workflow (``uq_workflow_member_workflow_id_tool_alias``)
    so they're a safe DOM-id key for the cytoscape canvas.

    :attr:`run_state` is populated only when the graph was built with a
    ``run_id``; otherwise it's ``None`` and the UI renders structure-only.
    :attr:`child_job_id` is the workflow-run child Job that this node
    represents (also run-only); it's exposed so a graph-node click can
    route to ``/jobs/{id}`` like the run page's tabs do.
    """

    tool_alias: str
    label: str
    """Human-readable name for the node — the tool's pretty name, or the
    alias when the tool record can't be loaded (the missing-tool case
    that admin pages already render as ``"missing"`` rather than 500)."""

    position: int
    """1-based workflow position. The label includes it as ``"1. Heat
    Transfer"`` in the UI; tests assert position is dense and unique
    per build."""

    tool_state: str | None = None
    """Tool's lifecycle state (``"new"`` / ``"test"`` / ``"released"`` /
    ``"archived"``) at build time. ``None`` when the tool record can't
    be resolved (the FK is RESTRICT so this is rare). Used by the UI to
    badge stale members on the admin edit page."""

    archived: bool = False
    """Convenience boolean for rendering dimmed nodes — derived from the
    tool's ``archived_at`` rather than ``tool_state`` because archived
    rows hold their state column for the UI's archive-history pane."""

    deep_link: str | None = None
    """Absolute path the UI uses for graph-node clicks. In structure
    mode this is ``/admin/tools/{alias}``; in run mode it overrides to
    ``/jobs/{child_job_id}`` so a click drops the user into the child
    job's full page (mirroring the run-page tab behaviour)."""

    run_state: WorkflowNodeRunState | None = None
    """Per-node runtime state when ``run_id`` is set; ``None`` otherwise.
    The UI's stylesheet keys per-state node fill on this value."""

    child_job_id: int | None = None
    """The workflow-run child Job this node represents (run mode only)."""

    payload: dict[str, Any] = field(default_factory=dict)
    """Render-only extras (e.g. ``tool_name``, ``tool_version``). Never
    raw simulation output — payloads cross the API boundary and need to
    stay bounded."""


@dataclass(frozen=True, slots=True)
class WorkflowGraphEdge:
    """One directed edge — descriptor-inferred upstream → downstream.

    Edge identity is ``(source, target)`` since the inferred graph is
    simple (no parallel edges). Where multiple downstream input fields
    declared the same wiring (``downstream_input=True,
    upstream_tool=<upstream>``), they all land on the same edge with
    their human-facing labels collected in :attr:`field_labels`.

    Edges sourced purely from a forward ``downstream_tools`` list
    (no per-field declaration) have an empty :attr:`field_labels`
    tuple. The UI renders those as plain arrows; per-field edges show
    the labels one per line above the arrow.
    """

    source: str
    """Upstream tool alias."""

    target: str
    """Downstream tool alias."""

    field_labels: tuple[str, ...] = ()
    """Human-facing labels of the downstream input fields that
    declared this wiring (the descriptor's ``label`` text the user
    sees on the submit form, e.g. ``"Initial temperature"``), sorted
    alphabetically. Falls back to the wire ``name`` if the descriptor
    field has no label set. Empty tuple = forward-only edge with no
    per-field claim."""


@dataclass(frozen=True, slots=True)
class WorkflowGraph:
    """The assembled workflow-structure graph.

    Carries enough context for the UI to render the canvas header
    (workflow name + version + state) without a second API round-trip.
    :attr:`run_id` / :attr:`run_state` are populated only when the
    build was scoped to a specific run.
    """

    workflow_id: int
    workflow_alias: str
    workflow_name: str
    workflow_version: str
    workflow_state: str
    """``"new"`` / ``"test"`` / ``"released"`` for admin workflows;
    always ``"released"`` for custom workflows (their CHECK constraint
    pins it)."""

    nodes: tuple[WorkflowGraphNode, ...]
    edges: tuple[WorkflowGraphEdge, ...]

    run_id: int | None = None
    """The :class:`WorkflowRun` id this graph was built against, or
    ``None`` for structure-only mode."""

    run_state: str | None = None
    """The rollup state computed by
    :func:`matofx.workflows.run_service.compute_state` — ``"draft"`` /
    ``"running"`` / ``"complete"`` / ``"failed"`` / ``"cancelled"``.
    ``None`` when :attr:`run_id` is ``None``."""
