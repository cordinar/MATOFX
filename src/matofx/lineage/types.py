"""Frozen value types for the lineage graph (ADR-0010, design §3-§4).

The closed vocabularies (``NodeKind`` / ``EdgeKind``) plus the three
dataclasses the rest of the API surface returns. Everything here is
frozen + slot-tight — these flow through the JSON / GraphML
serialisers and across the API boundary, and must be safe to share
across coroutines without further synchronisation.

Adding a node or edge kind means editing one of these enums **and**
wiring a resolver in :mod:`matofx.lineage.service`. Kinds reserved
for later ADRs (``OPTIMISATION_STUDY``, ``JOB_COHORT``,
``SURROGATE_MODEL_VERSION``, ``VV_RECORD``) ship here so the API
contract is stable across ADR landings; their resolvers stay as
placeholders until the owning ADR wires them up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class NodeKind(StrEnum):
    """Closed vocabulary of node kinds in the lineage graph (design §3.1).

    Each kind has exactly one ORM source-of-truth (see
    :data:`EDGE_LABELS` / ``service._resolve_*``) and exactly one
    resolver in :class:`matofx.lineage.service.LineageService`.
    """

    JOB = "job"
    WORKFLOW_RUN = "workflow_run"
    PARAMETRIC_STUDY = "parametric_study"
    OPTIMISATION_STUDY = "optimisation_study"  # reserved for ADR-0009
    JOB_COHORT = "job_cohort"  # reserved for ADR-0008
    TOOL_VERSION = "tool_version"
    LIBRARY_FILE_VERSION = "library_file_version"
    MATERIAL_VERSION = "material_version"
    SURROGATE_MODEL_VERSION = "surrogate_model_version"  # reserved for ADR-0008
    SCENARIO = "scenario"
    PROJECT = "project"
    USER = "user"
    APPROVAL_REQUEST = "approval_request"
    VV_RECORD = "vv_record"  # reserved for ADR-0011


class EdgeKind(StrEnum):
    """Closed vocabulary of edge kinds (design §3.2).

    Each edge carries a stable ``source → target`` direction; "upstream"
    means follow the arrow (toward the older / dependency node).
    """

    SUBMITTED_BY = "submitted_by"
    BELONGS_TO_SCENARIO = "belongs_to_scenario"
    SCENARIO_IN_PROJECT = "scenario_in_project"
    BELONGS_TO_WORKFLOW_RUN = "belongs_to_workflow_run"
    SEEDED_BY_STUDY = "seeded_by_study"
    RAN_TOOL_VERSION = "ran_tool_version"
    CONSUMED_LIBRARY_FILE_VERSION = "consumed_library_file_version"
    CONSUMED_MATERIAL_VERSION = "consumed_material_version"
    RAN_SURROGATE_VERSION = "ran_surrogate_version"  # reserved for ADR-0008
    GATED_BY = "gated_by"
    PUBLISHED_BY = "published_by"
    DERIVED_FROM = "derived_from"  # reserved for ADR-0008
    VERIFIED_BY = "verified_by"  # ADR-0011 — V&V verification claim edge
    VALIDATED_BY = "validated_by"  # ADR-0011 — V&V validation claim edge


# Human-readable label per edge kind. The UI renders these on hover; the
# GraphML serialiser writes them as the ``label`` attribute so external
# tools (Cytoscape Desktop / Gephi) show something engineering-meaningful.
EDGE_LABELS: dict[EdgeKind, str] = {
    EdgeKind.SUBMITTED_BY: "submitted by",
    EdgeKind.BELONGS_TO_SCENARIO: "in scenario",
    EdgeKind.SCENARIO_IN_PROJECT: "in project",
    EdgeKind.BELONGS_TO_WORKFLOW_RUN: "part of workflow run",
    EdgeKind.SEEDED_BY_STUDY: "seeded by study",
    EdgeKind.RAN_TOOL_VERSION: "ran tool version",
    EdgeKind.CONSUMED_LIBRARY_FILE_VERSION: "consumed library file",
    EdgeKind.CONSUMED_MATERIAL_VERSION: "consumed material",
    EdgeKind.RAN_SURROGATE_VERSION: "ran surrogate version",
    EdgeKind.GATED_BY: "gated by",
    EdgeKind.PUBLISHED_BY: "published by",
    EdgeKind.DERIVED_FROM: "derived from",
    EdgeKind.VERIFIED_BY: "verified by",
    EdgeKind.VALIDATED_BY: "validated by",
}


NodeRef = tuple[NodeKind, int]
"""A node's stable composite key in the graph: ``(kind, obj_id)``."""


@dataclass(frozen=True, slots=True)
class LineageNode:
    """One rendered node in a lineage graph (design §3.1 + §4 dataclass).

    Carries just enough for the UI to draw a labelled, kind-styled,
    state-aware deep-linkable shape — never the underlying ORM row.
    Free-text columns and large blobs are deliberately omitted from
    :attr:`payload` to keep API responses bounded.
    """

    kind: NodeKind
    obj_id: int
    label: str
    state: str | None = None
    """Per-kind lifecycle state when meaningful (``"complete"``,
    ``"archived"``, ``"retired"``, …); ``None`` for kinds without one."""
    archived: bool = False
    """Convenience boolean for rendering greyed-out nodes; computed
    from :attr:`state` by the resolver."""
    deep_link: str | None = None
    """Absolute path on this install pointing at the node's primary
    surface (e.g. ``/jobs/4213``). ``None`` when the kind has no
    user-facing surface (e.g. ``approval_request``)."""
    payload: dict[str, Any] = field(default_factory=dict)
    """Render-only extras (tool alias, submitted_at, …). Per-kind
    structure documented at the resolver. Never raw simulation output."""
    placeholder: bool = False
    """``True`` when the resolver for this kind hasn't been wired yet
    (e.g. slice 1 only resolves :attr:`NodeKind.JOB` in full; every
    other kind returns a placeholder with the minimal ``label`` /
    ``deep_link``). The UI renders placeholders with a dashed border
    and a "(detail pending)" suffix; later slices flip these to
    ``False`` as resolvers land."""


@dataclass(frozen=True, slots=True)
class LineageEdge:
    """One directed edge in a lineage graph (design §3.2).

    Direction is ``source → target`` where ``target`` is the upstream
    (older / dependency) node. ``truncated_count`` is ``>0`` when the
    edge stands in for one or more hidden nodes — either dropped by
    ACL or beyond the depth limit.
    """

    source: NodeRef
    target: NodeRef
    relation: EdgeKind
    label: str
    field_name: str | None = None
    """Populated for ``consumed_library_file_version`` /
    ``consumed_material_version`` — the descriptor field on the
    consuming job that received this version. ``None`` for edges that
    have no per-field interpretation."""
    truncated_count: int = 0


@dataclass(frozen=True, slots=True)
class LineageCentre:
    """One picker option for the admin lineage page (slice 5b fix).

    The standalone admin centre-picker shows a kind dropdown + a
    populated id selector. Each option is one of these — the
    ``obj_id`` flows into the canonical URL, the ``label`` is what
    the operator sees, and the optional ``state`` lets the picker
    render an archived / pending badge inline.
    """

    obj_id: int
    label: str
    state: str | None = None


@dataclass(frozen=True, slots=True)
class LineageGraph:
    """The assembled subgraph centred on one node (design §4)."""

    centre: NodeRef
    nodes: tuple[LineageNode, ...]
    edges: tuple[LineageEdge, ...]
    hidden_by_acl: int = 0
    """Count of nodes dropped from the graph because the actor lacks
    visibility on them. Each contributes ``+1`` to one incident edge's
    :attr:`LineageEdge.truncated_count`."""
    hidden_by_depth: int = 0
    """Count of nodes the walk would have reached if the upstream /
    downstream depth limit hadn't fired first."""
    soft_capped: bool = False
    """``True`` when the build reached ``soft_cap`` nodes mid-walk and
    truncated the remaining frontier. The UI degrades to table-only
    rendering when this is set (design §4.5)."""
