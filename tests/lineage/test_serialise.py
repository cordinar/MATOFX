"""Unit tests for :mod:`matofx.lineage.serialise`.

Pure functions over in-memory :class:`LineageGraph` instances — no DB,
no fixtures, no async. The two serialisers (JSON and GraphML) are the
external contract: anything in :class:`LineageGraph` that flows to a
client goes through one of them, so the assertions here pin down the
wire shape.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from matofx.lineage import (
    EdgeKind,
    LineageEdge,
    LineageGraph,
    LineageNode,
    NodeKind,
    graph_to_graphml,
    graph_to_json,
)

# ---- shared fixtures -----------------------------------------------------


def _make_graph() -> LineageGraph:
    """A minimal but multi-edge graph for round-trip tests."""
    job_node = LineageNode(
        kind=NodeKind.JOB,
        obj_id=4213,
        label="heat_transfer #4213",
        state="complete",
        archived=False,
        deep_link="/jobs/4213",
        payload={"tool_alias": "heat_transfer", "submitted_at": "2026-05-26T10:14:02Z"},
    )
    user_node = LineageNode(
        kind=NodeKind.USER,
        obj_id=17,
        label="Alice User",
        state="active",
        deep_link="/admin/users/17",
        placeholder=False,
    )
    tool_node = LineageNode(
        kind=NodeKind.TOOL_VERSION,
        obj_id=117,
        label="heat_transfer v1.0",
        state="released",
        deep_link="/admin/tools/heat_transfer",
    )
    edges = (
        LineageEdge(
            source=(NodeKind.JOB, 4213),
            target=(NodeKind.USER, 17),
            relation=EdgeKind.SUBMITTED_BY,
            label="submitted by",
        ),
        LineageEdge(
            source=(NodeKind.JOB, 4213),
            target=(NodeKind.TOOL_VERSION, 117),
            relation=EdgeKind.RAN_TOOL_VERSION,
            label="ran tool version",
            field_name=None,
            truncated_count=2,  # exercises the truncation-count attribute
        ),
    )
    return LineageGraph(
        centre=(NodeKind.JOB, 4213),
        nodes=(job_node, user_node, tool_node),
        edges=edges,
        hidden_by_acl=1,
        hidden_by_depth=0,
        soft_capped=False,
    )


# ---- JSON ---------------------------------------------------------------


def test_graph_to_json_centre_shape() -> None:
    """``centre`` is a stable ``kind:id`` string identifier."""
    graph = _make_graph()
    blob = graph_to_json(graph)
    assert blob["centre"] == "job:4213"


def test_graph_to_json_node_shape() -> None:
    """Each node carries kind/id/label/state/archived/deep_link/payload/placeholder."""
    graph = _make_graph()
    blob = graph_to_json(graph)
    nodes = blob["nodes"]
    assert isinstance(nodes, list)
    centre = next(n for n in nodes if n["id"] == 4213)
    assert centre["kind"] == "job"
    assert centre["label"] == "heat_transfer #4213"
    assert centre["state"] == "complete"
    assert centre["archived"] is False
    assert centre["deep_link"] == "/jobs/4213"
    assert centre["payload"]["tool_alias"] == "heat_transfer"
    assert centre["placeholder"] is False


def test_graph_to_json_edge_shape_includes_truncated_count() -> None:
    """Edges serialise as ``[kind, id]`` arrays for source/target."""
    graph = _make_graph()
    blob = graph_to_json(graph)
    edges = blob["edges"]
    tool_edge = next(e for e in edges if e["relation"] == "ran_tool_version")
    assert tool_edge["source"] == ["job", 4213]
    assert tool_edge["target"] == ["tool_version", 117]
    assert tool_edge["truncated_count"] == 2
    assert tool_edge["field_name"] is None


def test_graph_to_json_carries_summary_counters() -> None:
    """``hidden_by_acl`` / ``hidden_by_depth`` / ``soft_capped`` round-trip."""
    graph = _make_graph()
    blob = graph_to_json(graph)
    assert blob["hidden_by_acl"] == 1
    assert blob["hidden_by_depth"] == 0
    assert blob["soft_capped"] is False


def test_graph_to_json_is_json_serialisable() -> None:
    """The dict round-trips through ``json.dumps`` cleanly — no exotic types."""
    graph = _make_graph()
    blob = graph_to_json(graph)
    serialised = json.dumps(blob)
    reparsed = json.loads(serialised)
    assert reparsed["centre"] == "job:4213"
    assert len(reparsed["nodes"]) == 3


# ---- GraphML -------------------------------------------------------------


def test_graph_to_graphml_parses_as_well_formed_xml() -> None:
    """The GraphML output is well-formed XML."""
    graph = _make_graph()
    xml = graph_to_graphml(graph)
    root = ET.fromstring(xml)  # noqa: S314 — input is our own controlled serialiser output
    # GraphML's root tag in its namespace.
    assert root.tag.endswith("graphml")


def test_graph_to_graphml_declares_node_and_edge_keys() -> None:
    """Every wire-meaningful attribute has an inline ``<key>`` declaration."""
    graph = _make_graph()
    xml = graph_to_graphml(graph)
    root = ET.fromstring(xml)  # noqa: S314 — input is our own controlled serialiser output
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    key_ids = {k.attrib["id"] for k in root.findall(f"{ns}key")}
    # Per-node keys.
    for required in ("kind", "label", "state", "archived", "deep_link", "placeholder"):
        assert required in key_ids, f"missing node-attribute key: {required!r}"
    # Per-edge keys.
    for required in ("relation", "edge_label", "field_name", "truncated_count"):
        assert required in key_ids, f"missing edge-attribute key: {required!r}"


def test_graph_to_graphml_emits_one_node_per_lineage_node() -> None:
    """``LineageGraph.nodes`` count matches GraphML ``<node>`` count."""
    graph = _make_graph()
    xml = graph_to_graphml(graph)
    root = ET.fromstring(xml)  # noqa: S314 — input is our own controlled serialiser output
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    g = root.find(f"{ns}graph")
    assert g is not None
    nodes = g.findall(f"{ns}node")
    assert len(nodes) == 3
    ids = {n.attrib["id"] for n in nodes}
    assert ids == {"job:4213", "user:17", "tool_version:117"}


def test_graph_to_graphml_emits_directed_edges_with_truncated_count() -> None:
    """Edges carry source/target + the relation/truncated_count payload."""
    graph = _make_graph()
    xml = graph_to_graphml(graph)
    root = ET.fromstring(xml)  # noqa: S314 — input is our own controlled serialiser output
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    g = root.find(f"{ns}graph")
    assert g is not None
    assert g.attrib["edgedefault"] == "directed"
    edges = g.findall(f"{ns}edge")
    assert len(edges) == 2
    # Find the truncated edge and check its truncated_count data element.
    tool_edge = next(e for e in edges if e.attrib["target"] == "tool_version:117")
    data_keys = {d.attrib["key"]: d.text for d in tool_edge.findall(f"{ns}data")}
    assert data_keys["relation"] == "ran_tool_version"
    assert data_keys.get("truncated_count") == "2"


def test_graph_to_graphml_omits_falsy_optional_fields() -> None:
    """A node with ``placeholder=False`` doesn't emit a placeholder data element.

    Keeps the GraphML output compact for the common case (most nodes
    aren't placeholders, most edges have no field_name and no
    truncation).
    """
    graph = _make_graph()
    xml = graph_to_graphml(graph)
    root = ET.fromstring(xml)  # noqa: S314 — input is our own controlled serialiser output
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    g = root.find(f"{ns}graph")
    assert g is not None
    job_node = next(n for n in g.findall(f"{ns}node") if n.attrib["id"] == "job:4213")
    data_keys = {d.attrib["key"] for d in job_node.findall(f"{ns}data")}
    assert "placeholder" not in data_keys


@pytest.mark.parametrize("malicious", ['<>&"', "<script>", '" onclick="x'])
def test_graph_to_graphml_escapes_xml_special_chars_in_labels(malicious: str) -> None:
    """An attacker-controlled label can't break the XML structure."""
    node = LineageNode(
        kind=NodeKind.JOB,
        obj_id=1,
        label=malicious,
        deep_link=None,
    )
    graph = LineageGraph(centre=(NodeKind.JOB, 1), nodes=(node,), edges=())
    xml = graph_to_graphml(graph)
    # Round-trips through the XML parser without raising.
    root = ET.fromstring(xml)  # noqa: S314 — input is our own controlled serialiser output
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    g = root.find(f"{ns}graph")
    assert g is not None
    parsed_label = next(
        d.text
        for n in g.findall(f"{ns}node")
        for d in n.findall(f"{ns}data")
        if d.attrib["key"] == "label"
    )
    assert parsed_label == malicious
