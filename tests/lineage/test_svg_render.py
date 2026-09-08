"""Unit tests for :mod:`matofx.lineage.svg_render` (ADR-0010 slice 6).

Pure functions over in-memory :class:`LineageGraph` instances — no
DB, no fixtures, no async. The renderer is the bridge between the
graph data model and the inlined SVG in the per-job HTML report;
these assertions pin the output shape so a regression surfaces
without a browser snapshot test.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from matofx.lineage import (
    EdgeKind,
    LineageEdge,
    LineageGraph,
    LineageNode,
    NodeKind,
    graph_to_svg,
)


def _sample_graph() -> LineageGraph:
    """A small DAG: job centre + submitter + scenario + tool version."""
    return LineageGraph(
        centre=(NodeKind.JOB, 42),
        nodes=(
            LineageNode(
                kind=NodeKind.JOB,
                obj_id=42,
                label="heat_transfer #42",
                state="complete",
            ),
            LineageNode(kind=NodeKind.USER, obj_id=7, label="Alice User"),
            LineageNode(kind=NodeKind.SCENARIO, obj_id=3, label="Scenario A", archived=False),
            LineageNode(kind=NodeKind.TOOL_VERSION, obj_id=117, label="heat_transfer v1.0"),
        ),
        edges=(
            LineageEdge(
                source=(NodeKind.JOB, 42),
                target=(NodeKind.USER, 7),
                relation=EdgeKind.SUBMITTED_BY,
                label="submitted by",
            ),
            LineageEdge(
                source=(NodeKind.JOB, 42),
                target=(NodeKind.SCENARIO, 3),
                relation=EdgeKind.BELONGS_TO_SCENARIO,
                label="in scenario",
            ),
            LineageEdge(
                source=(NodeKind.JOB, 42),
                target=(NodeKind.TOOL_VERSION, 117),
                relation=EdgeKind.RAN_TOOL_VERSION,
                label="ran tool version",
                truncated_count=1,
            ),
        ),
    )


def test_graph_to_svg_parses_as_xml() -> None:
    """The output is a single well-formed ``<svg>`` document."""
    svg = graph_to_svg(_sample_graph())
    assert svg is not None
    root = ET.fromstring(svg)  # noqa: S314 — input is our own controlled output
    assert root.tag.endswith("svg")


def test_graph_to_svg_emits_one_rect_per_node() -> None:
    """Every node lands as a ``<rect>`` in the SVG."""
    svg = graph_to_svg(_sample_graph())
    assert svg is not None
    # Four nodes in the sample → four rects (centre + 3 neighbours).
    # The placeholder fallback path emits one extra rect (the bg) —
    # check we're well above that floor.
    rect_count = svg.count("<rect")
    assert rect_count >= 4, f"expected >=4 rects, got {rect_count}"


def test_graph_to_svg_emits_one_line_per_edge() -> None:
    """Every edge lands as a ``<line>`` with the arrow-end marker."""
    svg = graph_to_svg(_sample_graph())
    assert svg is not None
    # Three edges → three lines.
    assert svg.count("<line") == 3
    # All edges carry the shared arrow marker reference.
    assert svg.count('marker-end="url(#matofx-lineage-arrow)"') == 3


def test_graph_to_svg_inlines_node_labels() -> None:
    """Node labels survive into the rendered SVG (with HTML-escaping)."""
    svg = graph_to_svg(_sample_graph())
    assert svg is not None
    assert "heat_transfer #42" in svg
    assert "Alice User" in svg
    assert "Scenario A" in svg


def test_graph_to_svg_dashes_truncated_edges() -> None:
    """Edges with ``truncated_count > 0`` render dashed (design §3.3)."""
    svg = graph_to_svg(_sample_graph())
    assert svg is not None
    # The tool-version edge is truncated in the sample.
    assert "stroke-dasharray=" in svg


def test_graph_to_svg_returns_none_when_exceeding_max_nodes() -> None:
    """Soft-cap fallback — the report renders a table section instead.

    When the graph is bigger than ``max_nodes`` the renderer returns
    ``None`` so the report builder can switch to a tabular section
    (design §8 ``include_provenance`` toggle behaviour).
    """
    nodes = tuple(LineageNode(kind=NodeKind.JOB, obj_id=i, label=f"#{i}") for i in range(20))
    graph = LineageGraph(centre=(NodeKind.JOB, 0), nodes=nodes, edges=())
    assert graph_to_svg(graph, max_nodes=10) is None


def test_graph_to_svg_returns_placeholder_for_empty_graph() -> None:
    """An empty graph renders a "(no nodes resolved)" placeholder SVG."""
    graph = LineageGraph(centre=(NodeKind.JOB, 1), nodes=(), edges=())
    svg = graph_to_svg(graph)
    assert svg is not None
    assert "no nodes resolved" in svg


def test_graph_to_svg_escapes_malicious_labels() -> None:
    """An attacker-controlled label can't break the SVG structure."""
    nasty = LineageNode(kind=NodeKind.JOB, obj_id=1, label='<script>alert("xss")</script>')
    graph = LineageGraph(centre=(NodeKind.JOB, 1), nodes=(nasty,), edges=())
    svg = graph_to_svg(graph)
    assert svg is not None
    # The raw script tag must not survive into the SVG (it's escaped
    # to ``&lt;script&gt;...``). XML parses cleanly.
    assert "<script" not in svg
    ET.fromstring(svg)  # noqa: S314 — input is our own controlled output


def test_graph_to_svg_truncates_long_labels() -> None:
    """Labels longer than ~26 chars get ellipsised to keep node-width bounded."""
    long_node = LineageNode(
        kind=NodeKind.JOB,
        obj_id=1,
        label="A really very extremely long label that definitely overflows the node box",
    )
    graph = LineageGraph(centre=(NodeKind.JOB, 1), nodes=(long_node,), edges=())
    svg = graph_to_svg(graph)
    assert svg is not None
    # The ellipsis character lands; the raw "overflows" word doesn't.
    assert "…" in svg
    assert "overflows the node box" not in svg
