"""Server-side SVG renderer for :class:`LineageGraph` (ADR-0010 slice 6).

The per-job HTML report (ADR-0004) inlines a static SVG of the
provenance graph when the report builder is asked for it. The SVG is
self-contained — no JS runtime, no CDN, no external font — so it
survives ADR-0004's "fully inlined, no-network, no-script" invariant.

Layout: a simplified layered DAG. Longest-path ranking puts each node
on a row; nodes within a row spread evenly across the available width;
edges draw as straight arrows between row centres. No crossing
minimisation (would lift the LOC count substantially and is overkill
for the typical 5-30 node per-job graph) — for very dense graphs the
report falls back to a tabular section instead (see
:func:`graph_to_svg`'s ``max_nodes`` parameter).

Pure Python (~250 LOC), no external deps. Air-gap-safe by design.
The alternative path the design weighed (§13 open items) — shelling
out to node + Cytoscape — would have added a system dependency and
trampled the report's no-script guarantee. Worth the extra LOC.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from xml.sax.saxutils import escape

from matofx.lineage.types import LineageEdge, LineageGraph, LineageNode, NodeKind

# Per-kind shape + fill colour. Mirrors the Cytoscape live-view stylesheet
# in :data:`matofx.ui.lineage_view._KIND_STYLE` so the report and the
# live page render the same wireframe shape per kind.
_KIND_FILL: dict[str, str] = {
    "job": "#1976d2",
    "workflow_run": "#7b1fa2",
    "parametric_study": "#7b1fa2",
    "optimisation_study": "#7b1fa2",
    "job_cohort": "#7b1fa2",
    "tool_version": "#455a64",
    "library_file_version": "#00838f",
    "material_version": "#2e7d32",
    "surrogate_model_version": "#6a1b9a",
    "approval_request": "#f9a825",
    "user": "#90a4ae",
    "scenario": "#cfd8dc",
    "project": "#b0bec5",
    "vv_record": "#388e3c",
}
_KIND_TEXT: dict[str, str] = {
    "job": "#ffffff",
    "workflow_run": "#ffffff",
    "parametric_study": "#ffffff",
    "optimisation_study": "#ffffff",
    "job_cohort": "#ffffff",
    "tool_version": "#ffffff",
    "library_file_version": "#ffffff",
    "material_version": "#ffffff",
    "surrogate_model_version": "#ffffff",
    "approval_request": "#000000",
    "user": "#000000",
    "scenario": "#000000",
    "project": "#000000",
    "vv_record": "#ffffff",
}

# Layout constants. All in user-space units of the rendered SVG; the
# outer ``<svg>`` carries a ``viewBox`` so the host page can scale to
# fit the report's column width.
_NODE_WIDTH = 240
_NODE_HEIGHT = 44
_ROW_SPACING = 96  # vertical centre-to-centre
_COL_SPACING = 44  # horizontal gap between siblings on the same row
_MARGIN = 30
_FONT_SIZE = 13  # user-space px; matches the live view's bumped label size


@dataclass(frozen=True, slots=True)
class _PositionedNode:
    node: LineageNode
    x: int  # top-left
    y: int


def graph_to_svg(
    graph: LineageGraph,
    *,
    max_nodes: int = 500,
    width_hint: int = 1000,
) -> str | None:
    """Render ``graph`` to a self-contained SVG string.

    Returns ``None`` when the graph has more than ``max_nodes`` nodes
    — the report falls back to a tabular section in that case
    (design §8). Empty / centre-only graphs render as a "(no
    relationships found)" placeholder rather than an empty SVG, so
    the report's Provenance section is always meaningful when the
    toggle is on.

    ``width_hint`` is the target SVG width in user-space units; the
    layout spreads nodes within this width. The output ``<svg>``
    carries a ``viewBox`` so the host can scale to whatever column
    width the report's CSS gives it.
    """
    if len(graph.nodes) > max_nodes:
        return None

    if not graph.nodes:
        return _placeholder_svg("(no nodes resolved)")

    rows = _rank_nodes(graph)
    positioned = _layout_nodes(rows, width_hint)
    if not positioned:
        return _placeholder_svg("(graph has no resolvable layout)")

    return _emit_svg(positioned, graph.edges, width_hint=width_hint)


def _rank_nodes(graph: LineageGraph) -> list[list[LineageNode]]:
    """Assign each node a row index via longest-path-from-source ranking.

    Source nodes (no incoming edges within the graph) sit on row 0;
    every other node sits at ``1 + max(rank(predecessors))``. Stable
    against arbitrary insertion order — the rank only depends on the
    edge structure.

    Returns a list-of-lists, indexed by row. Within each row the
    nodes preserve their original
    :attr:`LineageGraph.nodes` order.
    """
    node_by_ref: dict[tuple[NodeKind, int], LineageNode] = {
        (n.kind, n.obj_id): n for n in graph.nodes
    }
    incoming: dict[tuple[NodeKind, int], list[tuple[NodeKind, int]]] = defaultdict(list)
    for edge in graph.edges:
        if edge.source in node_by_ref and edge.target in node_by_ref:
            incoming[edge.source].append(edge.target)

    rank: dict[tuple[NodeKind, int], int] = {}

    def _rank_of(ref: tuple[NodeKind, int], stack: frozenset[tuple[NodeKind, int]]) -> int:
        if ref in rank:
            return rank[ref]
        if ref in stack:
            # Cycle (shouldn't happen — :func:`_assert_dag` guards
            # against it — but the renderer stays robust by treating
            # the back-edge node as a fresh root).
            return 0
        preds = incoming.get(ref, ())
        if not preds:
            rank[ref] = 0
            return 0
        deeper = stack | {ref}
        rank[ref] = 1 + max(_rank_of(p, deeper) for p in preds)
        return rank[ref]

    for n in graph.nodes:
        _rank_of((n.kind, n.obj_id), frozenset())

    rows: dict[int, list[LineageNode]] = defaultdict(list)
    for n in graph.nodes:
        rows[rank[(n.kind, n.obj_id)]].append(n)
    return [rows[i] for i in sorted(rows)]


def _layout_nodes(rows: list[list[LineageNode]], width_hint: int) -> list[_PositionedNode]:
    """Place each row of nodes horizontally evenly across ``width_hint``."""
    placed: list[_PositionedNode] = []
    for row_idx, row_nodes in enumerate(rows):
        y = _MARGIN + row_idx * _ROW_SPACING
        n = len(row_nodes)
        if n == 0:
            continue
        total_width = n * _NODE_WIDTH + (n - 1) * _COL_SPACING
        # If the row would overflow, compress horizontal spacing.
        if total_width > width_hint - 2 * _MARGIN:
            usable = max(width_hint - 2 * _MARGIN, n * _NODE_WIDTH)
            effective_col_spacing = max(0, (usable - n * _NODE_WIDTH) // max(1, (n - 1)))
            total_width = n * _NODE_WIDTH + (n - 1) * effective_col_spacing
        else:
            effective_col_spacing = _COL_SPACING
        start_x = (width_hint - total_width) // 2
        for col_idx, node in enumerate(row_nodes):
            x = start_x + col_idx * (_NODE_WIDTH + effective_col_spacing)
            placed.append(_PositionedNode(node=node, x=x, y=y))
    return placed


def _emit_svg(
    positioned: list[_PositionedNode],
    edges: tuple[LineageEdge, ...],
    *,
    width_hint: int,
) -> str:
    """Build the final SVG XML string from positioned nodes + edges."""
    by_ref = {(p.node.kind, p.node.obj_id): p for p in positioned}
    height = max((p.y for p in positioned), default=0) + _NODE_HEIGHT + _MARGIN

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width_hint} {height}" '
        f'width="100%" '
        f"style=\"font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; "
        f'font-size: {_FONT_SIZE}px; background: #ffffff;">',
        # Arrow-head marker, shared by every edge.
        "<defs>"
        '<marker id="matofx-lineage-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#666"/>'
        "</marker>"
        "</defs>",
    ]

    # Edges first so node rectangles paint over them.
    for edge in edges:
        src = by_ref.get(edge.source)
        tgt = by_ref.get(edge.target)
        if src is None or tgt is None:
            continue
        out.append(_edge_path(src, tgt, edge))

    # Then nodes on top.
    for p in positioned:
        out.append(_node_rect(p))

    out.append("</svg>")
    return "".join(out)


def _edge_path(src: _PositionedNode, tgt: _PositionedNode, edge: LineageEdge) -> str:
    """Render one edge as a straight line with an arrow head.

    The line connects the centre of the source's bottom edge to the
    centre of the target's top edge (for downward edges) or the
    reverse (upward). Truncated edges (``truncated_count > 0``)
    render dashed.
    """
    sx = src.x + _NODE_WIDTH // 2
    sy = src.y + _NODE_HEIGHT
    tx = tgt.x + _NODE_WIDTH // 2
    ty = tgt.y
    # If the source is below the target (upstream walk), flip the
    # anchor points so the arrow exits the source's top.
    if src.y > tgt.y:
        sy = src.y
        ty = tgt.y + _NODE_HEIGHT
    dash = ' stroke-dasharray="6 4"' if edge.truncated_count else ""
    stroke = "#bbb" if edge.truncated_count else "#999"
    return (
        f'<line x1="{sx}" y1="{sy}" x2="{tx}" y2="{ty}" '
        f'stroke="{stroke}" stroke-width="1.5"{dash} '
        f'marker-end="url(#matofx-lineage-arrow)"/>'
    )


def _node_rect(p: _PositionedNode) -> str:
    """Render one node as a labeled rectangle.

    Archived nodes render with reduced opacity. Placeholder nodes
    (slice 1 strugglers) get a dashed border. Per-kind fill colour
    matches the live Cytoscape view's stylesheet so reports and the
    UI look like the same product.
    """
    kind_str = p.node.kind.value if hasattr(p.node.kind, "value") else str(p.node.kind)
    fill = _KIND_FILL.get(kind_str, "#999")
    text = _KIND_TEXT.get(kind_str, "#000")
    opacity = "0.5" if p.node.archived else "1"
    stroke_dash = ' stroke-dasharray="4 3"' if p.node.placeholder else ""
    # Truncate the label to fit in the wider node — roughly 34 chars
    # at 13px font fits the ~224 user-space units inside the padding.
    label = _ellipsis(p.node.label, 34)
    return (
        f'<g opacity="{opacity}">'
        f'<rect x="{p.x}" y="{p.y}" width="{_NODE_WIDTH}" height="{_NODE_HEIGHT}" '
        f'rx="4" ry="4" fill="{fill}" stroke="#000" stroke-opacity="0.2"{stroke_dash}/>'
        f'<text x="{p.x + _NODE_WIDTH // 2}" y="{p.y + _NODE_HEIGHT // 2 + 5}" '
        f'text-anchor="middle" fill="{text}">'
        f"{escape(label)}"
        f"</text>"
        f"</g>"
    )


def _ellipsis(text: str, max_chars: int) -> str:
    """Truncate ``text`` to ``max_chars`` with a trailing ellipsis."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _placeholder_svg(message: str) -> str:
    """Render a tiny SVG that just shows the placeholder message.

    Used for empty graphs and "too large to render" fallbacks — the
    report's Provenance section stays a coherent rectangle in either
    case rather than collapsing to whitespace.
    """
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'viewBox="0 0 600 80" width="100%" '
        'style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; '
        'font-size: 12px; background: #f5f5f5;">'
        '<rect x="0" y="0" width="600" height="80" fill="#f5f5f5" '
        'stroke="#ccc" stroke-width="1"/>'
        '<text x="300" y="44" text-anchor="middle" fill="#666">'
        f"{escape(message)}"
        "</text>"
        "</svg>"
    )
