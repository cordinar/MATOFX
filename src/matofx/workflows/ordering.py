"""Compute the run order for a custom workflow's chosen tools.

A custom workflow is a user-picked set of 2–10 tools the user wants to
launch together against a scenario. The user picks tools by name; they
do **not** author dependencies — those are inferred from the tools'
descriptors:

- **Forward edges** come from each tool's ``descriptor.downstream_tools``
  list ("when I run, these tools naturally come next").
- **Backward edges** come from each input field that declares
  ``downstream_input=True`` plus ``upstream_tool="<alias>"`` ("this
  field is fed by some other tool's output").

Both directions are unioned, restricted to the chosen tool set, and
fed through the BFS-of-chains + topological-sort tail ported from the
legacy ``general_func.order_tools``. The chain pass keeps linear
chains contiguous (so the user sees "Material Properties → Heat
Transfer → Stress" as adjacent tabs); the topo tail picks up merge
points (tools with multiple upstreams) and any disconnected tools.

Pure function — no DB access, no descriptor loading, no I/O. Pass in
the chosen aliases + a descriptor lookup; receive an ordered list
back. Cycles raise :class:`CycleError` at save time so the workflow
form can surface a per-field message.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from typing import Any


class CycleError(ValueError):
    """The chosen tool set contains a dependency cycle."""

    def __init__(self, cycle: list[str]) -> None:
        """Hold the member aliases forming the cycle, in the order found."""
        self.cycle = cycle
        joined = " → ".join([*cycle, cycle[0]]) if cycle else "(unknown)"
        super().__init__(
            f"These tools form a loop: {joined}. Remove one of them or pick a different upstream."
        )


def compute_order(
    tool_aliases: Iterable[str],
    descriptors: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Return ``tool_aliases`` reordered so upstreams precede downstreams.

    :param tool_aliases: The user's chosen tools, in selection order.
        Duplicates are not validated here — caller is expected to have
        already rejected them via the service-layer dedupe check.
    :param descriptors: A mapping from tool alias → that tool's
        descriptor JSON (the dict stored on ``tool.descriptor``).
        Missing aliases produce an empty descriptor (the tool is
        treated as having no declared dependencies).
    :returns: Ordered list of aliases. Length and contents match the
        input — only the order changes.
    :raises CycleError: when the inferred dependency graph contains a
        cycle. Caller surfaces this as a per-field save-time error.
    """
    aliases = list(dict.fromkeys(tool_aliases))  # dedupe preserving order
    if len(aliases) <= 1:
        return aliases

    edges = _infer_edges(aliases, descriptors)
    forward, indegree = _build_graph(aliases, edges)
    _detect_cycle(aliases, forward, indegree)
    return _order(aliases, forward, indegree)


# ----- internals ---------------------------------------------------------


def _infer_edges(
    aliases: list[str],
    descriptors: Mapping[str, Mapping[str, Any]],
) -> set[tuple[str, str]]:
    """Union forward + backward descriptor signals into an edge set.

    Edges point from upstream → downstream (the producer of an output
    points at the consumer). Both signals are restricted to the chosen
    set: an edge referencing a tool the user didn't pick is dropped on
    the floor.

    Thin wrapper over :func:`infer_edges_with_fields` that drops the
    per-edge field-name detail — used by :func:`compute_order` where the
    edges are all that's needed.
    """
    edges, _fields = infer_edges_with_fields(aliases, descriptors)
    return edges


def infer_edges_with_fields(  # noqa: PLR0912 - one branch per case — the branches are the specification
    aliases: Iterable[str],
    descriptors: Mapping[str, Mapping[str, Any]],
) -> tuple[set[tuple[str, str]], dict[tuple[str, str], list[str]]]:
    """Same as :func:`_infer_edges`, also returning per-edge display labels.

    Returns ``(edges, labels_by_edge)`` where ``labels_by_edge`` maps
    each edge to the sorted list of downstream input-field **labels**
    (the descriptor's ``label`` field — the human-facing display text,
    e.g. ``"Initial temperature"``) that declared the wiring. Falls
    back to the field's wire ``name`` when no ``label`` is set so the
    UI always has something to show.

    ``downstream_tools``-only edges (no input field named the upstream)
    get an empty list — the workflow-graph view renders those as
    plain arrows; per-field edges show the labels one-per-line above
    the arrow.

    Public (no leading underscore) so :class:`WorkflowGraphService`
    can consume it without reaching into module privates.
    """
    chosen_list = list(aliases)
    chosen = set(chosen_list)
    edges: set[tuple[str, str]] = set()
    labels_by_edge: dict[tuple[str, str], list[str]] = {}

    for upstream_alias in chosen_list:
        descriptor = descriptors.get(upstream_alias) or {}

        # Forward: this tool declares its successors. No field label
        # available — the wiring lives on the upstream's descriptor,
        # not on any specific downstream input.
        forward_list = descriptor.get("downstream_tools")
        if isinstance(forward_list, list):
            for downstream in forward_list:
                if (
                    isinstance(downstream, str)
                    and downstream in chosen
                    and downstream != upstream_alias
                ):
                    edges.add((upstream_alias, downstream))

    for downstream_alias in chosen_list:
        descriptor = descriptors.get(downstream_alias) or {}
        inputs = descriptor.get("inputs")
        if not isinstance(inputs, list):
            continue
        for input_field in inputs:
            if not isinstance(input_field, dict):
                continue
            if not bool(input_field.get("downstream_input")):
                continue
            upstream = input_field.get("upstream_tool")
            if not (
                isinstance(upstream, str) and upstream in chosen and upstream != downstream_alias
            ):
                continue
            # Prefer the human-facing ``label`` (what the UI shows on
            # the form); fall back to the wire ``name`` if no label
            # was set. An input with neither is ignored — there's
            # nothing meaningful to render.
            field_label = input_field.get("label")
            if not isinstance(field_label, str) or not field_label.strip():
                field_name = input_field.get("name")
                field_label = field_name if isinstance(field_name, str) else None
            if not isinstance(field_label, str) or not field_label.strip():
                continue
            edge = (upstream, downstream_alias)
            edges.add(edge)
            labels_by_edge.setdefault(edge, []).append(field_label)

    # Sort the per-edge label lists so output is deterministic for
    # tests + stable for the cytoscape edge tooltips.
    for edge_labels in labels_by_edge.values():
        edge_labels.sort()

    return edges, labels_by_edge


def _build_graph(
    aliases: list[str],
    edges: set[tuple[str, str]],
) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Construct adjacency + indegree maps over the chosen aliases.

    Adjacency lists are alias-sorted so traversal order is fully
    deterministic regardless of how the edge set was assembled.
    """
    forward: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = dict.fromkeys(aliases, 0)
    for src, dst in edges:
        forward[src].append(dst)
        indegree[dst] += 1
    for src in forward:  # noqa: PLC0206 - the key is used to build the message below
        forward[src].sort()
    return forward, indegree


def _detect_cycle(
    aliases: list[str],
    forward: Mapping[str, list[str]],
    indegree: Mapping[str, int],
) -> None:
    """Raise :class:`CycleError` when the graph is not a DAG.

    Uses a Kahn's-style drain pass on a copy of ``indegree``: if any
    node is still un-emitted at the end, those nodes participate in at
    least one cycle. We then walk one such cycle for a friendly error
    message naming the loop.
    """
    remaining = dict(indegree)
    queue: deque[str] = deque(sorted(a for a in aliases if remaining[a] == 0))
    emitted: set[str] = set()
    while queue:
        node = queue.popleft()
        emitted.add(node)
        for child in forward.get(node, []):
            remaining[child] -= 1
            if remaining[child] == 0:
                queue.append(child)
    leftover = [a for a in aliases if a not in emitted]
    if not leftover:
        return
    raise CycleError(_extract_cycle(leftover, forward))


def _extract_cycle(
    leftover: list[str],
    forward: Mapping[str, list[str]],
) -> list[str]:
    """Walk forward from a leftover node until we revisit a node.

    The returned list is the cycle in traversal order (last node points
    back to the first). Used purely for the error message — order is
    deterministic because both ``leftover`` and adjacency lists are
    alias-sorted.
    """
    if not leftover:
        return []
    start = sorted(leftover)[0]
    seen: dict[str, int] = {}
    node = start
    path: list[str] = []
    while node not in seen:
        seen[node] = len(path)
        path.append(node)
        # Pick the first leftover child to keep walking. If the chosen
        # node has no leftover successor, bail — the cycle is somewhere
        # else and the error will still be informative enough.
        next_nodes = [c for c in forward.get(node, []) if c in set(leftover)]
        if not next_nodes:
            return path
        node = next_nodes[0]
    # node is the cycle closer; trim everything before its first visit.
    return path[seen[node] :]


def _order(  # noqa: PLR0912 - one branch per case — the branches are the specification
    aliases: list[str],
    forward: Mapping[str, list[str]],
    indegree: Mapping[str, int],
) -> list[str]:
    """BFS-of-chains + topological-sort tail.

    Ported from legacy ``general_func.order_tools``. Two passes:

    1. **Chain pass** — start from each indegree-0 root (alias-sorted),
       BFS forward into children. Merge nodes (indegree > 1) are
       deliberately *skipped* during this pass so they're not pulled
       under one parent's chain prematurely.
    2. **Topo tail** — standard Kahn's drain across the whole graph,
       picking up every node the chain pass missed (merge nodes,
       disconnected subgraphs that had no indegree-0 root reachable
       from earlier roots).

    Net effect: linear chains stay contiguous in the output; merge
    points and stragglers land in a deterministic tail.
    """
    chosen_set = set(aliases)
    chains: list[list[str]] = []
    visited: set[str] = set()
    roots = sorted(a for a in aliases if indegree[a] == 0)

    for root in roots:
        if root in visited:
            continue
        chain: list[str] = []
        local: set[str] = set()
        queue: deque[str] = deque([root])
        while queue:
            node = queue.popleft()
            if node in local or node not in chosen_set:
                continue
            local.add(node)
            chain.append(node)
            for child in forward.get(node, []):
                if child in visited or child in local:
                    continue
                if indegree[child] > 1:
                    # Merge node — leave it for the topo tail so we
                    # don't pull it under the wrong parent.
                    continue
                queue.append(child)
        chains.append(chain)
        visited.update(chain)

    # Topo tail — Kahn's drain across the full graph.
    remaining = dict(indegree)
    tail_queue: deque[str] = deque(sorted(a for a in aliases if remaining[a] == 0))
    topo: list[str] = []
    while tail_queue:
        node = tail_queue.popleft()
        topo.append(node)
        for child in sorted(forward.get(node, [])):
            remaining[child] -= 1
            if remaining[child] == 0:
                tail_queue.append(child)

    final: list[str] = []
    seen: set[str] = set()
    for chain in chains:
        for node in chain:
            if node not in seen:
                final.append(node)
                seen.add(node)
    for node in topo:
        if node not in seen:
            final.append(node)
            seen.add(node)
    return final
