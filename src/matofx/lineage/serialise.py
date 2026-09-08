"""Serialisers for :class:`matofx.lineage.LineageGraph` (ADR-0010 §5).

Two output formats, one input shape:

- :func:`graph_to_json` — the canonical JSON dict the live API endpoint
  returns. Matches :class:`matofx.api.schemas.LineageGraphResponse`.
- :func:`graph_to_graphml` — a self-contained GraphML XML document
  consumable by Cytoscape Desktop / Gephi / d3-force tooling. Inlines
  the ``<key>`` declarations so external parsers need no external
  schema reference.

Both are pure functions over :class:`matofx.lineage.LineageGraph`;
they never touch the DB or the audit writer.
"""

from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

from matofx.lineage.types import LineageEdge, LineageGraph, LineageNode


def graph_to_json(graph: LineageGraph) -> dict[str, object]:
    """Render a :class:`LineageGraph` as the wire JSON dict.

    The shape matches :class:`matofx.api.schemas.LineageGraphResponse`
    and is documented as the canonical API response in design §5.2.
    """
    return {
        "centre": _ref_to_str(graph.centre),
        "nodes": [_node_to_dict(n) for n in graph.nodes],
        "edges": [_edge_to_dict(e) for e in graph.edges],
        "hidden_by_acl": graph.hidden_by_acl,
        "hidden_by_depth": graph.hidden_by_depth,
        "soft_capped": graph.soft_capped,
    }


def graph_to_graphml(graph: LineageGraph) -> str:
    """Render a :class:`LineageGraph` as a GraphML XML string.

    The document is self-contained — every attribute the renderer
    might want (label, kind, state, archived, deep_link, relation,
    field_name, truncated_count) is declared with an inline ``<key>``
    so Cytoscape Desktop / Gephi can import the file without an
    external schema reference. Encoding is UTF-8 BOM-less.
    """
    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://graphml.graphdrawing.org/xmlns '
        'http://graphml.graphdrawing.org/xmlns/1.0/graphml.xsd">',
    ]

    # Node-attribute keys.
    out.extend(
        [
            '<key id="kind" for="node" attr.name="kind" attr.type="string"/>',
            '<key id="label" for="node" attr.name="label" attr.type="string"/>',
            '<key id="state" for="node" attr.name="state" attr.type="string"/>',
            '<key id="archived" for="node" attr.name="archived" attr.type="boolean"/>',
            '<key id="deep_link" for="node" attr.name="deep_link" attr.type="string"/>',
            '<key id="placeholder" for="node" attr.name="placeholder" attr.type="boolean"/>',
        ]
    )
    # Edge-attribute keys.
    out.extend(
        [
            '<key id="relation" for="edge" attr.name="relation" attr.type="string"/>',
            '<key id="edge_label" for="edge" attr.name="label" attr.type="string"/>',
            '<key id="field_name" for="edge" attr.name="field_name" attr.type="string"/>',
            '<key id="truncated_count" for="edge" attr.name="truncated_count" attr.type="int"/>',
        ]
    )

    out.append('<graph edgedefault="directed">')

    for node in graph.nodes:
        nid = _ref_to_str((node.kind, node.obj_id))
        out.append(f"<node id={quoteattr(nid)}>")
        out.append(f'<data key="kind">{escape(node.kind.value)}</data>')
        out.append(f'<data key="label">{escape(node.label)}</data>')
        if node.state is not None:
            out.append(f'<data key="state">{escape(node.state)}</data>')
        out.append(f'<data key="archived">{"true" if node.archived else "false"}</data>')
        if node.deep_link is not None:
            out.append(f'<data key="deep_link">{escape(node.deep_link)}</data>')
        if node.placeholder:
            out.append('<data key="placeholder">true</data>')
        out.append("</node>")

    for idx, edge in enumerate(graph.edges):
        sid = _ref_to_str(edge.source)
        tid = _ref_to_str(edge.target)
        out.append(f'<edge id="e{idx}" source={quoteattr(sid)} target={quoteattr(tid)}>')
        out.append(f'<data key="relation">{escape(edge.relation.value)}</data>')
        out.append(f'<data key="edge_label">{escape(edge.label)}</data>')
        if edge.field_name is not None:
            out.append(f'<data key="field_name">{escape(edge.field_name)}</data>')
        if edge.truncated_count:
            out.append(f'<data key="truncated_count">{edge.truncated_count}</data>')
        out.append("</edge>")

    out.append("</graph>")
    out.append("</graphml>")
    return "\n".join(out)


def _ref_to_str(ref: tuple[object, int]) -> str:
    """Render ``(kind, id)`` as the stable ``kind:id`` string id.

    Used as both the GraphML node id and the JSON ``centre`` /
    ``edge.source`` / ``edge.target`` array elements. The format is
    parser-friendly — a single ``:`` split round-trips cleanly.
    """
    kind, obj_id = ref
    kind_str = kind.value if hasattr(kind, "value") else str(kind)
    return f"{kind_str}:{obj_id}"


def _node_to_dict(node: LineageNode) -> dict[str, object]:
    """Map a :class:`LineageNode` to its JSON dict shape."""
    return {
        "kind": node.kind.value,
        "id": node.obj_id,
        "label": node.label,
        "state": node.state,
        "archived": node.archived,
        "deep_link": node.deep_link,
        "payload": node.payload,
        "placeholder": node.placeholder,
    }


def _edge_to_dict(edge: LineageEdge) -> dict[str, object]:
    """Map a :class:`LineageEdge` to its JSON dict shape."""
    return {
        "source": [edge.source[0].value, edge.source[1]],
        "target": [edge.target[0].value, edge.target[1]],
        "relation": edge.relation.value,
        "label": edge.label,
        "field_name": edge.field_name,
        "truncated_count": edge.truncated_count,
    }
