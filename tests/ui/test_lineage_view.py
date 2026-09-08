"""Helper tests for :mod:`matofx.ui.lineage_view` (ADR-0010, slice 4a).

The component itself is exercised end-to-end via the API client tests
(``tests/ui/test_lineage_client.py``); these are tight unit tests on
the pure-Python helpers that don't depend on the live app fixture.
Keeps the fast-test set fast — no migration cycle.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from nicegui import Client, ui
from nicegui.elements.icon import Icon
from nicegui.elements.slider import Slider
from nicegui.elements.tooltip import Tooltip
from nicegui.page import page

from matofx.lineage import NodeKind
from matofx.ui._cytoscape import _cytoscape_bootstrap_js
from matofx.ui.api_client import ApiError
from matofx.ui.lineage_view import (
    _KIND_LABELS,
    _KIND_ORDER,
    _KIND_STYLE,
    _apply_hidden_kinds,
    _find_centre,
    _friendly_lineage_error,
    _graph_to_cytoscape_elements,
    _kinds_in_graph,
    _render_filter_row,
)
from matofx.ui.static_assets import assets_root


def test_kind_labels_cover_every_node_kind() -> None:
    """Every :class:`NodeKind` member has a human-readable label.

    If a future ADR adds a kind to the enum without touching
    :data:`_KIND_LABELS`, the table-view section header falls back
    to a title-cased slug. This test pins the explicit-label coverage
    so a new kind triggers a clear "add me to _KIND_LABELS" failure.
    """
    missing = {k.value for k in NodeKind} - set(_KIND_LABELS)
    assert missing == set(), f"missing _KIND_LABELS entries: {sorted(missing)}"


def test_kind_order_covers_every_node_kind() -> None:
    """Every :class:`NodeKind` appears in :data:`_KIND_ORDER`.

    Without this, a future ADR's kind would land under the "Other"
    catch-all section. The unknown-kinds fallback in ``_render_tables``
    keeps the surface alive, but the explicit ordering is the
    deliberate engineering-grouping the design wireframe (§6.1) calls
    for.
    """
    missing = {k.value for k in NodeKind} - set(_KIND_ORDER)
    assert missing == set(), f"missing _KIND_ORDER entries: {sorted(missing)}"


def test_find_centre_returns_matching_node() -> None:
    """``_find_centre`` resolves ``"kind:id"`` to the node dict."""
    graph = {
        "centre": "job:42",
        "nodes": [
            {"kind": "user", "id": 7, "label": "Alice"},
            {"kind": "job", "id": 42, "label": "demo #42"},
        ],
        "edges": [],
    }
    centre = _find_centre(graph)
    assert centre is not None
    assert centre["label"] == "demo #42"
    assert centre["kind"] == "job"


def test_find_centre_returns_none_for_missing_match() -> None:
    """Centre id without a matching node returns ``None``.

    Happens when the graph build hides the centre via ACL mid-walk —
    shouldn't ever occur for a centre node (existence-hidden raises
    before the graph builds), but the helper is defensive.
    """
    graph = {
        "centre": "job:99",
        "nodes": [{"kind": "user", "id": 7, "label": "Alice"}],
        "edges": [],
    }
    assert _find_centre(graph) is None


def test_find_centre_handles_malformed_centre_string() -> None:
    """A centre string without ``:`` returns ``None`` (no crash)."""
    assert _find_centre({"centre": "", "nodes": [], "edges": []}) is None
    assert _find_centre({"centre": "not-a-ref", "nodes": [], "edges": []}) is None
    assert _find_centre({"centre": "job:not-int", "nodes": [], "edges": []}) is None


# ---- slice 4b: Cytoscape graph view -----------------------------------------


def _sample_graph() -> dict:
    """A multi-edge graph for the slice-4b helper tests."""
    return {
        "centre": "job:42",
        "nodes": [
            {
                "kind": "job",
                "id": 42,
                "label": "demo #42",
                "state": "complete",
                "archived": False,
                "deep_link": "/jobs/42",
                "payload": {},
                "placeholder": False,
            },
            {
                "kind": "user",
                "id": 7,
                "label": "Alice",
                "state": "active",
                "archived": False,
                "deep_link": "/admin/users/7",
                "payload": {},
                "placeholder": False,
            },
            {
                "kind": "tool_version",
                "id": 117,
                "label": "demo v1",
                "state": "released",
                "archived": False,
                "deep_link": "/admin/tools/demo",
                "payload": {},
                "placeholder": True,
            },
        ],
        "edges": [
            {
                "source": ["job", 42],
                "target": ["user", 7],
                "relation": "submitted_by",
                "label": "submitted by",
                "field_name": None,
                "truncated_count": 0,
            },
            {
                "source": ["job", 42],
                "target": ["tool_version", 117],
                "relation": "ran_tool_version",
                "label": "ran tool version",
                "field_name": None,
                "truncated_count": 0,
            },
        ],
        "hidden_by_acl": 0,
        "hidden_by_depth": 0,
        "soft_capped": False,
    }


def test_graph_to_cytoscape_elements_round_trips_nodes_and_edges() -> None:
    """Every node + edge in the graph appears in the Cytoscape elements array."""
    result = _graph_to_cytoscape_elements(_sample_graph())
    elements = result["elements"]
    node_ids = {e["data"]["id"] for e in elements if "source" not in e["data"]}
    edge_ids = {e["data"]["id"] for e in elements if "source" in e["data"]}
    assert node_ids == {"job:42", "user:7", "tool_version:117"}
    assert len(edge_ids) == 2
    assert result["centre"] == "job:42"


def test_graph_to_cytoscape_elements_drops_edges_with_missing_endpoint() -> None:
    """An edge to a node that isn't in the graph is dropped, not emitted.

    Cytoscape hard-throws ("Can not create edge … with nonexistent
    target"), killing the whole graph, if an edge names an absent
    endpoint — which happens when a node is hidden by ACL / depth (or a
    chip) but its edge survives. The element builder must defend the
    graph view by skipping such dangling edges so the connected part
    still renders.
    """
    graph = _sample_graph()
    # Add an edge to a node that does not exist in ``nodes``.
    graph["edges"].append(
        {
            "source": ["job", 42],
            "target": ["project", 3],  # no project:3 node in the graph
            "relation": "in_project",
            "label": "in project",
            "field_name": None,
            "truncated_count": 0,
        }
    )
    result = _graph_to_cytoscape_elements(graph)
    edges = [e for e in result["elements"] if "source" in e["data"]]
    targets = {e["data"]["target"] for e in edges}
    assert "project:3" not in targets
    # The two well-formed edges survive; the dangling one is gone.
    assert len(edges) == 2
    # Edge ids stay dense (no gap from the skipped edge).
    assert {e["data"]["id"] for e in edges} == {"e0", "e1"}


def test_graph_to_cytoscape_elements_tags_archived_and_placeholder_classes() -> None:
    """Node ``classes`` strings carry the per-state CSS hooks the JS uses."""
    elements = _graph_to_cytoscape_elements(_sample_graph())["elements"]
    nodes = {e["data"]["id"]: e for e in elements if "source" not in e["data"]}
    # The tool_version node is a slice-1 placeholder in the test fixture.
    assert "placeholder" in nodes["tool_version:117"]["classes"]
    # No archived nodes in the sample.
    assert "archived" not in nodes["job:42"]["classes"]


def test_graph_to_cytoscape_elements_emits_kind_class_for_styling() -> None:
    """Every node gets a ``kind-<kind>`` class so the per-kind selectors match."""
    elements = _graph_to_cytoscape_elements(_sample_graph())["elements"]
    nodes = {e["data"]["id"]: e for e in elements if "source" not in e["data"]}
    assert "kind-job" in nodes["job:42"]["classes"]
    assert "kind-user" in nodes["user:7"]["classes"]
    assert "kind-tool_version" in nodes["tool_version:117"]["classes"]


def test_graph_to_cytoscape_elements_stylesheet_covers_every_kind() -> None:
    """The stylesheet emits a per-kind selector for every NodeKind member."""
    stylesheet = _graph_to_cytoscape_elements(_sample_graph())["style"]
    kind_selectors = {
        s["selector"] for s in stylesheet if str(s["selector"]).startswith("node.kind-")
    }
    expected = {f"node.kind-{k.value}" for k in NodeKind}
    missing = expected - kind_selectors
    assert missing == set(), f"missing per-kind selectors: {sorted(missing)}"


def test_kind_style_covers_every_node_kind() -> None:
    """Every :class:`NodeKind` has an entry in :data:`_KIND_STYLE`.

    A future ADR's kind without a style entry falls through to the
    default node selector — readable but visually indistinct. This
    test pins explicit coverage so the new kind requires a deliberate
    shape/colour choice.
    """
    missing = {k.value for k in NodeKind} - set(_KIND_STYLE)
    assert missing == set(), f"missing _KIND_STYLE entries: {sorted(missing)}"


def test_apply_hidden_kinds_drops_target_node_and_marks_edge_truncated() -> None:
    """Hiding ``user`` drops the user node + bumps the submitted_by truncation."""
    filtered = _apply_hidden_kinds(_sample_graph(), frozenset({"user"}))
    node_kinds = {n["kind"] for n in filtered["nodes"]}
    assert "user" not in node_kinds
    # Two edges total still; the user-target one is truncated, the
    # tool-target one is unchanged.
    edge_by_target = {tuple(e["target"]): e for e in filtered["edges"]}
    user_edge = edge_by_target[("user", 7)]
    assert user_edge["truncated_count"] == 1
    tool_edge = edge_by_target[("tool_version", 117)]
    assert tool_edge["truncated_count"] == 0


def test_apply_hidden_kinds_drops_edges_whose_both_endpoints_hide() -> None:
    """Edges between two hidden nodes drop entirely and count toward ``hidden_by_chip``."""
    filtered = _apply_hidden_kinds(_sample_graph(), frozenset({"user", "tool_version"}))
    # Both edges had a hidden endpoint, but their source (the job)
    # remains visible, so both get truncated and stay rendered.
    assert len(filtered["edges"]) == 2
    assert all(e["truncated_count"] >= 1 for e in filtered["edges"])
    # No edge had both endpoints hidden in this fixture.
    assert filtered.get("hidden_by_chip", 0) == 0


def test_apply_hidden_kinds_passthrough_when_filter_empty() -> None:
    """Empty hide-set returns the original graph unchanged."""
    graph = _sample_graph()
    assert _apply_hidden_kinds(graph, frozenset()) is graph


def test_kinds_in_graph_returns_design_order() -> None:
    """``_kinds_in_graph`` reflects the §6.2 ordering for present kinds."""
    kinds = _kinds_in_graph(_sample_graph())
    # Sample has job, user, tool_version — design order places job
    # first, tool_version before user.
    assert kinds == ("job", "tool_version", "user")


def test_cytoscape_bootstrap_js_contains_mount_id_and_data() -> None:
    """The bootstrap source interpolates the mount id + JSON payload.

    Regression cover for the NiceGUI 3.x ``<script>``-rejection fix —
    the bootstrap is pushed via :func:`ui.run_javascript` rather than
    an inline ``<script>`` tag, and must still carry both the mount
    div id and the full Cytoscape elements payload.
    """
    graph = _sample_graph()
    elements = _graph_to_cytoscape_elements(graph)
    js = _cytoscape_bootstrap_js(
        mount_id="matofx-lineage-cy-test",
        elements=elements["elements"],
        stylesheet=elements["style"],
        centre_id=str(elements.get("centre") or ""),
        rank_dir="TB",
    )
    # The mount id flows through to the getElementById call.
    assert "'matofx-lineage-cy-test'" in js
    # The JSON payload is interpolated verbatim — every node id is
    # present in the source.
    assert "job:42" in js
    assert "user:7" in js
    # And the layout call is the dagre layered DAG.
    assert "name: 'dagre'" in js
    # And — critically — no opening ``<script>`` tag (the bug NiceGUI
    # rejects). The bootstrap is pure JS, not HTML.
    assert "<script" not in js


# ---- depth-slider commit wiring --------------------------------------------


@pytest.fixture
def nicegui_client() -> Iterator[Client]:
    """Active NiceGUI client so ``ui.*`` widgets can be created."""
    client = Client(page("/"))
    with client:
        yield client


def test_depth_sliders_have_help_tooltips(nicegui_client: Client) -> None:
    """Each depth slider label carries a hover ``?`` help icon.

    Regression cover for the in-UI explanation: the filter row renders a
    ``help_outline`` icon per slider with a non-empty tooltip so a user
    unsure what Upstream / Downstream mean gets an in-place explanation.
    """
    container = ui.column()
    _render_filter_row(
        container,
        kind="job",
        obj_id=1,
        upstream=6,
        downstream=3,
        view_mode="graph",
        hidden_kinds=frozenset(),
        graph_kinds=("job",),
        on_navigate=None,
        soft_capped=False,
    )

    help_icons = [
        e
        for e in nicegui_client.elements.values()
        if isinstance(e, Icon) and e._props.get("name") == "help_outline"
    ]
    # One per depth slider (Upstream + Downstream).
    assert len(help_icons) == 2

    tooltip_texts = [
        e.text for e in nicegui_client.elements.values() if isinstance(e, Tooltip)
    ]
    # Both depth sliders contribute an explanatory tooltip ("…to trace…").
    trace_tips = [t for t in tooltip_texts if "to trace" in t]
    assert len(trace_tips) == 2
    assert any("back" in t for t in trace_tips)
    assert any("forward" in t for t in trace_tips)


def test_friendly_error_404_is_not_found_copy() -> None:
    """A 404 maps to plain-English 'nothing here yet' copy — no HTTP jargon."""
    headline, body, icon, _colour = _friendly_lineage_error(
        ApiError(404, "Not Found"), kind="job"
    )
    assert headline == "Nothing to show here yet"
    assert "job" in body.lower()
    assert "http" not in (headline + body).lower()
    assert icon == "search_off"


def test_friendly_error_403_is_permission_copy() -> None:
    """401/403 map to a permission message, not a raw error."""
    for status in (401, 403):
        headline, body, icon, _colour = _friendly_lineage_error(
            ApiError(status, "Forbidden"), kind="library_file"
        )
        assert headline == "You don't have access to this"
        assert "permission" in body.lower()
        assert icon == "lock"


def test_friendly_error_500_is_try_again_copy() -> None:
    """A 5xx maps to a reassuring try-again message in the error colour."""
    headline, body, icon, colour = _friendly_lineage_error(
        ApiError(500, "boom"), kind="job"
    )
    assert "went wrong" in headline.lower()
    assert "try" in body.lower()
    assert icon == "error_outline"
    assert colour == "var(--matofx-error-text)"


def test_depth_sliders_refetch_on_commit(nicegui_client: Client) -> None:
    """Both depth sliders register a Quasar ``change`` (commit) listener.

    Regression cover: the upstream/downstream sliders previously only
    wired ``on_value_change`` (→ local state) with no re-fetch, so a
    depth change did nothing until the user pressed **Reload**. The fix
    re-fetches on pointer-release via the ``change`` event — a fetch per
    drag-tick would storm the API, so commit (not every tick) is the
    trigger.
    """
    container = ui.column()
    _render_filter_row(
        container,
        kind="job",
        obj_id=1,
        upstream=6,
        downstream=3,
        view_mode="graph",
        hidden_kinds=frozenset(),
        graph_kinds=("job",),
        on_navigate=None,
        soft_capped=False,
    )

    sliders = [e for e in nicegui_client.elements.values() if isinstance(e, Slider)]
    # Exactly the two depth sliders (upstream + downstream).
    assert len(sliders) == 2
    for slider in sliders:
        listener_types = {listener.type for listener in slider._event_listeners.values()}
        assert "change" in listener_types, "depth slider missing commit (change) re-fetch listener"


def test_cytoscape_bootstrap_js_self_loads_vendored_dependencies() -> None:
    """The bootstrap dynamically loads cytoscape + dagre + cytoscape-dagre.

    Regression cover for the ``add_head_html``-timing bug — the
    bootstrap must NOT rely on the scripts being pre-injected into the
    page head (that path doesn't work when the lineage view renders
    post-await). Instead, the bootstrap dynamically loads them via
    ``document.createElement('script')`` itself.

    Also pins that the three URLs are **same-origin** (P-03): these
    used to point at ``cdn.jsdelivr.net``, which never resolves on the
    air-gapped installs this product targets.
    """
    graph = _sample_graph()
    elements = _graph_to_cytoscape_elements(graph)
    js = _cytoscape_bootstrap_js(
        mount_id="matofx-lineage-cy-test",
        elements=elements["elements"],
        stylesheet=elements["style"],
        centre_id=str(elements.get("centre") or ""),
        rank_dir="TB",
    )
    # The vendored, same-origin URLs are interpolated into the loader.
    assert "/matofx-assets/vendor/cytoscape.min.js" in js
    assert "/matofx-assets/vendor/dagre.min.js" in js
    assert "/matofx-assets/vendor/cytoscape-dagre.min.js" in js
    assert "https://" not in js
    # The dynamic-loader pattern is present.
    assert "document.createElement('script')" in js
    # And the cytoscape.use(cytoscapeDagre) registration runs.
    assert "cytoscape.use(window.cytoscapeDagre)" in js


def test_vendored_cytoscape_assets_are_actually_shipped() -> None:
    """The three files the bootstrap asks for exist in the package.

    A same-origin URL is only an improvement if something serves it.
    Vendoring is a checked-in-files change with no build step, so the
    failure mode is a missing file rather than a broken import — which
    nothing else in the suite would notice until a browser asked for it.
    """
    vendor = assets_root() / "vendor"
    for name in ("cytoscape.min.js", "dagre.min.js", "cytoscape-dagre.min.js"):
        path = vendor / name
        assert path.is_file(), f"vendored asset missing: {path}"
        assert path.stat().st_size > 1024, f"vendored asset looks truncated: {path}"


def test_cytoscape_bootstrap_js_has_friendly_error_fallback() -> None:
    """Render/load failure shows friendly copy + a Table hint, not a raw JS error.

    Regression cover: the catch used to dump the raw Cytoscape exception
    ("Can not create edge … nonexistent target") straight into the page.
    It now explains *why* the diagram couldn't render and tells the user
    to switch to the Table view, while the technical detail goes to the
    browser console for support.
    """
    graph = _sample_graph()
    elements = _graph_to_cytoscape_elements(graph)
    js = _cytoscape_bootstrap_js(
        mount_id="m",
        elements=elements["elements"],
        stylesheet=elements["style"],
        centre_id=str(elements.get("centre") or ""),
        rank_dir="TB",
    )
    # Human headline + a clear instruction to switch views.
    assert "be drawn" in js
    assert "<strong>Table</strong>" in js
    # Both explanations present: script-didn't-load vs too-large/odd-shape.
    assert "lay out" in js
    # The load-failure branch must blame *this server*, not the user's
    # internet: since P-03 the library is served by MATOFX itself, so
    # "your network blocked the download" would send an administrator
    # to inspect a firewall rule that has nothing to do with it.
    assert "MATOFX server" in js
    assert "didn\\u2019t serve" in js
    # Technical detail goes to the console for support, not the user.
    assert "console.error" in js
    # The old raw-error dump is gone.
    assert "load Cytoscape.js" not in js
