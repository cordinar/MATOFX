"""Data lineage / provenance graph (ADR-0010).

A thin, read-only query + assembly layer over the existing per-domain
provenance rows. Every edge of a job's lineage — submitter, scenario,
tool version, workflow run, parametric / optimisation / cohort study,
library files consumed, materials consumed, surrogate model used,
approval gates — is already in the database under ADRs 0001-0009. This
module renders that data as a directed acyclic graph the UI and the
``/api/lineage/*`` endpoints can hand back.

Design: ``designs/data-lineage.md``. **No new tables**; one permission
slug (``lineage.view``); one Alembic migration (``0052``, slice 3) to
seed the slug onto existing installs; one General Setting
(``lineage_max_nodes``, slice 7) to govern the soft cap.

Built in slices (design §11):

- **Slice 1 (this module):** :class:`LineageService` skeleton + the
  closed :class:`NodeKind` / :class:`EdgeKind` vocabularies + the
  :class:`LineageGraph` dataclass + a full
  :meth:`LineageService._resolve_job` resolver covering every
  job-centred upstream edge in the schema. Other resolvers are
  placeholders that return a minimal labelled node so a depth-1
  graph from a job centre is still meaningful.
- **Slice 2:** fill in the placeholder resolvers via reverse-edge
  helpers (``LibraryService.list_consumers``,
  ``MaterialsService.list_consumers``,
  ``WorkflowRunService.list_child_jobs``, etc.) so downstream walking
  picks up children.
- **Slice 3:** API + permission seed migration (``0052``).
- **Slice 4:** ``LineageView`` UI component + admin standalone page
  at ``/admin/lineage/{kind}/{id}``.
- **Slice 5:** inline-tab mounts on host pages.
- **Slice 6:** ADR-0004 report integration (server-side SVG).
- **Slice 7:** ``lineage_max_nodes`` General Setting + per-resolver
  bulk-fetch perf pass + docs.
"""

from __future__ import annotations

from matofx.lineage.errors import (
    LineageAccessDeniedError,
    LineageDepthOutOfRangeError,
    LineageError,
    LineageInvariantError,
    LineageNotFoundError,
)
from matofx.lineage.serialise import graph_to_graphml, graph_to_json
from matofx.lineage.service import LineageService, make_lineage_service
from matofx.lineage.svg_render import graph_to_svg
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

__all__ = [
    "EDGE_LABELS",
    "EdgeKind",
    "LineageAccessDeniedError",
    "LineageCentre",
    "LineageDepthOutOfRangeError",
    "LineageEdge",
    "LineageError",
    "LineageGraph",
    "LineageInvariantError",
    "LineageNode",
    "LineageNotFoundError",
    "LineageService",
    "NodeKind",
    "NodeRef",
    "graph_to_graphml",
    "graph_to_json",
    "graph_to_svg",
    "make_lineage_service",
]
