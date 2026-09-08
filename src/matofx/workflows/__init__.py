"""Admin-released workflows — ordered groupings of tools.

A workflow bundles N tools into a runnable sequence. On a scenario,
the user picks a workflow, the page renders one tab per tool (labelled
with the tool's pretty name), and each tab unlocks once every upstream
dependency has produced its outputs. Output→input wiring is
auto-discovered from each tool's descriptor — admins don't wire
fields manually; they just pick the tools in order.

Workflows mirror tools' lifecycle (``new`` / ``test`` / ``released`` +
``archived_at``). Archiving a member tool cascades-archives every
workflow that contains it.

This module ships the data model + admin surface (Phase A). The
runtime — scenario-page tabbed UI + tab gating + auto-fill on
upstream complete — lands in Phase B.
"""

from matofx.workflows.graph_service import (
    WorkflowGraphService,
    make_workflow_graph_service,
)
from matofx.workflows.graph_types import (
    WorkflowGraph,
    WorkflowGraphEdge,
    WorkflowGraphNode,
    WorkflowNodeRunState,
)
from matofx.workflows.orm import (
    CUSTOM_WORKFLOW_STATE,
    Workflow,
    WorkflowKind,
    WorkflowMember,
    WorkflowRun,
    WorkflowShare,
    WorkflowVersion,
    WorkflowVisibility,
)
from matofx.workflows.run_service import WorkflowRunService, make_workflow_run_service
from matofx.workflows.service import WorkflowService, make_workflow_service

__all__ = [
    "CUSTOM_WORKFLOW_STATE",
    "Workflow",
    "WorkflowGraph",
    "WorkflowGraphEdge",
    "WorkflowGraphNode",
    "WorkflowGraphService",
    "WorkflowKind",
    "WorkflowMember",
    "WorkflowNodeRunState",
    "WorkflowRun",
    "WorkflowRunService",
    "WorkflowService",
    "WorkflowShare",
    "WorkflowVersion",
    "WorkflowVisibility",
    "make_workflow_graph_service",
    "make_workflow_run_service",
    "make_workflow_service",
]
