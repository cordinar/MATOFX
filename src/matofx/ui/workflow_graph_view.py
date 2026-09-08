"""Workflow-structure graph view (ADR-0013).

Mounts a Cytoscape.js + dagre canvas onto a container, fed by
``GET /api/workflows/{id}/graph``. Two modes:

- **Structure only** (``run_id=None``) — nodes are member tools,
  edges are descriptor-inferred wiring. Used on the admin workflow
  editor + the custom-workflow editor's live preview.
- **Run overlay** (``run_id=<id>``) — same shape, plus a per-node
  state badge (``ready`` / ``waiting`` / ``queued`` / ``running`` /
  ``complete`` / ``failed``) computed from the workflow run's child
  Job rows. Used on the workflow-run page beside the per-tool tabs.

Shares :func:`matofx.ui._cytoscape.render_cytoscape_canvas` with the
lineage view; the workflow graph's stylesheet (per-state node fill,
``LR`` rank direction by default — workflows read more naturally
left-to-right than the lineage's top-to-bottom DAG) is owned here.

Polling: in run-overlay mode, the component polls the graph endpoint
every :data:`_REFRESH_INTERVAL_S` seconds. Polling stops automatically
once every node reaches a terminal state (``complete`` / ``failed``)
to leave the page static at rest.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from nicegui import background_tasks, ui

from matofx.ui._cytoscape import render_cytoscape_canvas
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.state import current_token

_REFRESH_INTERVAL_S = 3.0
"""How often the run-overlay view polls the graph endpoint. Matches
the workflow-run page's tab strip cadence (2.5 s) closely enough that
the badge updates feel synchronous; slightly slower because the graph
is read-only and not actively driving form gating."""


# Per-state node fill. Picked from the same Material palette the
# workflow-run page tab strip uses (:data:`_TAB_STATE_COLOR`) so the
# badge in the graph and the colour on the tab strip match.
_RUN_STATE_STYLE: dict[str, dict[str, str]] = {
    "ready": {"background": "#1976d2", "color": "#fff"},  # primary blue
    "waiting": {"background": "#90a4ae", "color": "#000"},  # grey
    "queued": {"background": "#0288d1", "color": "#fff"},  # info blue
    "running": {"background": "#f57c00", "color": "#000"},  # warning amber
    "complete": {"background": "#2e7d32", "color": "#fff"},  # positive green
    "failed": {"background": "#c62828", "color": "#fff"},  # negative red
}
"""Structure-only nodes (no ``run_state``) use the default fill
defined below in :func:`_workflow_stylesheet`."""


_TERMINAL_RUN_STATES: frozenset[str] = frozenset({"complete", "failed"})
"""When every node's ``run_state`` is in this set, polling stops."""


def render_workflow_graph_preview_button(
    *,
    get_tool_aliases: Callable[[], list[str]],
    button_label: str = "Preview structure",
    height_px: int = 320,
) -> None:
    """Mount a "Preview structure" button + below-it graph container.

    Used by the workflow create / edit dialogs where the user is
    still picking tools and there's no saved workflow_id to graph
    against. Clicking the button calls ``get_tool_aliases()`` to read
    the picker's current selection, fires
    :meth:`ApiClient.workflow_graph_preview`, and renders the result
    into the container below the button. Re-clicking re-fetches —
    cheap, on-demand, no debounce.

    Renders nothing into the container until the first click, so the
    dialog opens at its natural compact size; the canvas only takes
    layout space once the user has actually asked for a preview.
    """
    container: ui.column

    async def _on_click() -> None:
        aliases = get_tool_aliases()
        if not aliases:
            container.clear()
            with container:
                ui.label(
                    "Pick at least one tool above, then click Preview to see the wiring."
                ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
            return
        client = ApiClient(get_app(), token=current_token())
        container.clear()
        with container:
            ui.label("Loading preview…").style("color: var(--matofx-muted-text);")
        try:
            graph = await client.workflow_graph_preview(tool_aliases=aliases)
        except ApiError as exc:
            container.clear()
            with container:
                ui.label("Couldn't build preview").style(
                    "font-size: 1rem; font-weight: 600;"
                )
                ui.label(exc.detail).style("color: var(--matofx-muted-text);")
                ui.label(f"(HTTP {exc.status_code})").style(
                    "color: var(--matofx-muted-text); font-size: 0.85rem;"
                )
            return
        # Preview mode — no run, no real workflow row. Suppress the
        # header (its placeholder values would read "Preview preview"
        # otherwise) and use the existing render path.
        _render_graph(
            container,
            graph=graph,
            workflow_id=0,
            run_id=None,
            rank_dir="LR",
            height_px=height_px,
            show_header=False,
            on_navigate=None,
            client=client,
        )

    with ui.row().classes("w-full items-center gap-2").style("margin-top: 0.5rem;"):
        ui.button(button_label, icon="account_tree", on_click=_on_click).props(
            "outline dense color=primary"
        )
        ui.label(
            "Renders the descriptor-inferred wiring for your current "
            "tool picks. Click again after adding or removing tools."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
    container = ui.column().classes("w-full")


def render_workflow_graph_section(
    *,
    workflow_id: int,
    run_id: int | None = None,
    label: str = "Structure preview",
    initially_open: bool = False,
    height_px: int = 360,
) -> None:
    """Mount a lazy-loading "Structure preview" expansion on a host page.

    Mirrors :func:`matofx.ui.lineage_view.render_provenance_section`'s
    pattern — a collapsible :class:`ui.expansion` that defers the API
    call to :func:`render_workflow_graph` until the user actually opens
    the section. Used by the admin / custom-workflow / my-workflows
    surfaces where the graph isn't the primary content of the page.

    ``height_px`` defaults shorter than :func:`render_workflow_graph`
    (360 vs 480) because the embedded expansion case usually sits
    alongside a form rather than as the page's main canvas.
    """
    container: ui.column

    async def _on_open_change(event: Any) -> None:
        opened = bool(event.value)
        if opened and not getattr(container, "_workflow_graph_loaded", False):
            container._workflow_graph_loaded = True  # type: ignore[attr-defined]
            await render_workflow_graph(
                container,
                workflow_id=workflow_id,
                run_id=run_id,
                show_header=False,
                height_px=height_px,
            )

    expansion = ui.expansion(label, value=initially_open).classes("w-full")
    expansion.props('header-class="text-weight-medium"')
    expansion.on_value_change(_on_open_change)
    with expansion, ui.column().classes("w-full gap-3").style("padding-top: 0.5rem;") as col:
        container = col
        ui.label(
            "Descriptor-inferred wiring. Edges are read-only — change the "
            "tool list and resave to update."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

    if initially_open:
        container._workflow_graph_loaded = True  # type: ignore[attr-defined]

        async def _autoload() -> None:
            await render_workflow_graph(
                container,
                workflow_id=workflow_id,
                run_id=run_id,
                show_header=False,
                height_px=height_px,
            )

        ui.timer(0.01, _autoload, once=True)


async def render_workflow_graph(
    container: ui.element,
    *,
    workflow_id: int,
    run_id: int | None = None,
    rank_dir: str = "LR",
    height_px: int = 480,
    show_header: bool = True,
    on_navigate: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Render the workflow-structure graph for ``workflow_id``.

    The container is cleared and repopulated with:

    - An optional header (workflow name + version + state + run rollup).
    - The Cytoscape canvas itself.
    - A small footer with the node / edge counts.

    ``rank_dir`` controls dagre layout direction — ``"LR"`` (left→right,
    the workflow default) reads more naturally than the lineage view's
    ``"TB"`` for a process flow.

    ``run_id`` triggers the runtime overlay: each node gets a state
    badge and the component polls the graph endpoint until every node
    reaches ``complete`` / ``failed``.

    ``on_navigate`` lets the host page intercept node clicks for
    in-page transitions; defaults to :func:`ui.navigate.to`. Unused for
    graph-node clicks today (the same JS-bridge limitation
    :mod:`matofx.ui.lineage_view` calls out) but kept on the API for
    parity + future wiring.
    """
    container.clear()
    with container:
        ui.label("Loading workflow graph…").style("color: var(--matofx-muted-text);")

    client = ApiClient(get_app(), token=current_token())
    try:
        graph = await client.workflow_graph(workflow_id=workflow_id, run_id=run_id)
    except ApiError as exc:
        _render_error(container, exc, workflow_id=workflow_id)
        return

    _render_graph(
        container,
        graph=graph,
        workflow_id=workflow_id,
        run_id=run_id,
        rank_dir=rank_dir,
        height_px=height_px,
        show_header=show_header,
        on_navigate=on_navigate,
        client=client,
    )


# ---- rendering helpers --------------------------------------------------


def _render_error(
    container: ui.element,
    exc: ApiError,
    *,
    workflow_id: int,
) -> None:
    """Friendly inline error rendering — keep the surface alive on 4xx."""
    container.clear()
    with container, ui.column().classes("w-full gap-2"):
        ui.label("Couldn't load workflow graph").style(
            "font-size: 1.1rem; font-weight: 600;"
        )
        ui.label(exc.detail).style("color: var(--matofx-muted-text);")
        ui.label(f"(workflow #{workflow_id} — HTTP {exc.status_code})").style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )


def _render_graph(
    container: ui.element,
    *,
    graph: dict[str, Any],
    workflow_id: int,
    run_id: int | None,
    rank_dir: str,
    height_px: int,
    show_header: bool,
    on_navigate: Callable[[str], Awaitable[None]] | None,
    client: ApiClient,
) -> None:
    """Render the loaded graph dict — header + canvas + footer."""
    container.clear()
    with container, ui.column().classes("w-full gap-3"):
        if show_header:
            _render_header(graph)
        elements, stylesheet = _graph_to_cytoscape(graph)
        # Cytoscape's centre is the workflow's first node when no
        # specific centre is meaningful — picks a useful initial
        # viewport rather than dagre's geometric centre.
        centre_id = ""
        if graph.get("nodes"):
            first = graph["nodes"][0]
            centre_id = f"{first['tool_alias']}"
        render_cytoscape_canvas(
            elements=elements,
            stylesheet=stylesheet,
            centre_id=centre_id,
            height_px=height_px,
            rank_dir=rank_dir,
        )
        _render_footer(graph)

    # Schedule a poll if we're in run-overlay mode and at least one
    # node is still non-terminal. Polling is detached so the calling
    # render call can complete cleanly before the next fetch fires.
    if run_id is not None and not _all_nodes_terminal(graph):
        _schedule_refresh(
            container,
            workflow_id=workflow_id,
            run_id=run_id,
            rank_dir=rank_dir,
            height_px=height_px,
            show_header=show_header,
            on_navigate=on_navigate,
        )

    del client  # currently unused inside the render; kept for symmetry with lineage_view


def _render_header(graph: dict[str, Any]) -> None:
    """Top-of-view label + version + state badge + run rollup."""
    name = str(graph.get("workflow_name") or "")
    version = str(graph.get("workflow_version") or "")
    state = str(graph.get("workflow_state") or "")
    run_id = graph.get("run_id")
    run_state = graph.get("run_state")
    with ui.row().classes("items-baseline gap-3"):
        ui.label("Workflow —").style(
            "color: var(--matofx-muted-text); font-size: 0.95rem;"
        )
        ui.label(name).style("font-size: 1.2rem; font-weight: 600;")
        if version:
            ui.label(f"v{version}").style(
                "color: var(--matofx-muted-text); font-size: 0.85rem;"
            )
        if state:
            ui.badge(state).props("color=primary outline").style("font-size: 0.7rem;")
        if run_id is not None and run_state:
            ui.label("·").style("color: var(--matofx-muted-text);")
            ui.badge(f"run #{run_id}: {run_state}").props(
                f"color={_RUN_ROLLUP_COLOR.get(str(run_state), 'grey')}"
            ).style("font-size: 0.7rem;")


_RUN_ROLLUP_COLOR: dict[str, str] = {
    "draft": "grey",
    "running": "warning",
    "complete": "positive",
    "failed": "negative",
    "cancelled": "grey",
}


def _render_footer(graph: dict[str, Any]) -> None:
    """Per-graph node / edge count summary."""
    node_count = len(graph.get("nodes", []) or [])
    edge_count = len(graph.get("edges", []) or [])
    with ui.row().classes("items-center gap-2 w-full").style(
        "border-top: 1px solid var(--matofx-border); padding-top: 0.5rem;"
    ):
        ui.label(f"{node_count} tool(s), {edge_count} wired dependency(ies).").style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )


def _graph_to_cytoscape(
    graph: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert the API response dict into ``(elements, stylesheet)``.

    Pure function so it can be unit-tested without a live browser.
    Node ids are the tool aliases — unique inside a workflow (see
    :class:`matofx.workflows.orm.WorkflowMember`'s
    ``(workflow_id, tool_alias)`` UNIQUE) — so Cytoscape's id-based
    lookups don't collide.
    """
    elements: list[dict[str, Any]] = []
    for node in graph.get("nodes", []) or []:
        alias = str(node["tool_alias"])
        run_state = node.get("run_state")
        classes = [f"position-{int(node.get('position', 0))}"]
        if node.get("archived"):
            classes.append("archived")
        if run_state:
            classes.append(f"run-{run_state}")
        elements.append(
            {
                "data": {
                    "id": alias,
                    "label": str(node.get("label") or alias),
                    "tool_alias": alias,
                    "position": int(node.get("position") or 0),
                    "run_state": run_state or "",
                    "deep_link": node.get("deep_link") or "",
                    "archived": bool(node.get("archived")),
                    "child_job_id": node.get("child_job_id"),
                },
                "classes": " ".join(c for c in classes if c),
            }
        )
    for idx, edge in enumerate(graph.get("edges", []) or []):
        # One label per line — Cytoscape edges render the ``\n``
        # separators verbatim once ``text-wrap: 'wrap'`` is set on the
        # edge selector (see :func:`_workflow_stylesheet`). The
        # ``field_labels`` key is the new name; ``field_names`` falls
        # back so that a pre-rename cached response doesn't blank the
        # tooltip if it sneaks through.
        labels = list(edge.get("field_labels") or edge.get("field_names") or [])
        elements.append(
            {
                "data": {
                    "id": f"e{idx}",
                    "source": str(edge["source"]),
                    "target": str(edge["target"]),
                    "field_labels": labels,
                    "label": "\n".join(labels),
                },
            }
        )
    return elements, _workflow_stylesheet()


def _workflow_stylesheet() -> list[dict[str, Any]]:
    """Cytoscape stylesheet for the workflow graph.

    Default node fill is a muted grey; per-state overrides win when a
    run overlay populates :data:`run_state`. Archived nodes drop to 0.5
    opacity so the editor reads at a glance.
    """
    stylesheet: list[dict[str, Any]] = [
        {
            "selector": "node",
            "style": {
                "label": "data(label)",
                "shape": "round-rectangle",
                "text-valign": "center",
                "text-halign": "center",
                "font-size": 11,
                "width": "label",
                "height": 36,
                "padding": "8px",
                "text-wrap": "ellipsis",
                "text-max-width": 180,
                "background-color": "#cfd8dc",
                "color": "#000",
                "border-width": 1,
                "border-color": "#000",
                "border-opacity": 0.2,
            },
        },
        {
            "selector": "edge",
            "style": {
                "curve-style": "bezier",
                "target-arrow-shape": "triangle",
                "target-arrow-color": "#666",
                "line-color": "#999",
                "width": 1.5,
                "font-size": 9,
                "color": "#666",
                "label": "data(label)",
                "text-rotation": "autorotate",
                # ``wrap`` honours ``\n`` separators in the label so
                # each downstream input field renders on its own line
                # above the arrow.
                "text-wrap": "wrap",
                "text-max-width": 240,
                # Lift the label clear of the arrow head and stack
                # rows up rather than down so multi-line labels grow
                # away from the connector, not into it.
                "text-margin-y": -12,
            },
        },
        {
            "selector": "node.archived",
            "style": {"opacity": 0.5},
        },
    ]
    # Per-state node fills.
    for state, colours in _RUN_STATE_STYLE.items():
        stylesheet.append(
            {
                "selector": f"node.run-{state}",
                "style": {
                    "background-color": colours["background"],
                    "color": colours["color"],
                },
            }
        )
    return stylesheet


def _all_nodes_terminal(graph: dict[str, Any]) -> bool:
    """True when every node's ``run_state`` is in :data:`_TERMINAL_RUN_STATES`.

    Empty graphs return ``True`` — no need to keep polling a workflow
    that has no members yet. None ``run_state`` values mean the graph
    is structure-only; in that mode the caller wouldn't schedule a
    poll anyway.
    """
    nodes = graph.get("nodes", []) or []
    if not nodes:
        return True
    for node in nodes:
        state = node.get("run_state")
        if state is None:
            # Structure-only node — nothing to poll for.
            continue
        if str(state) not in _TERMINAL_RUN_STATES:
            return False
    return True


def _schedule_refresh(
    container: ui.element,
    *,
    workflow_id: int,
    run_id: int,
    rank_dir: str,
    height_px: int,
    show_header: bool,
    on_navigate: Callable[[str], Awaitable[None]] | None,
) -> None:
    """Queue the next overlay-mode fetch on the NiceGUI background loop.

    The timer fires once after :data:`_REFRESH_INTERVAL_S` seconds and
    re-enters :func:`render_workflow_graph`. The re-render decides for
    itself whether another poll is needed (``_all_nodes_terminal``
    short-circuits the chain once every child reaches a terminal
    state).
    """

    async def _refresh() -> None:
        await render_workflow_graph(
            container,
            workflow_id=workflow_id,
            run_id=run_id,
            rank_dir=rank_dir,
            height_px=height_px,
            show_header=show_header,
            on_navigate=on_navigate,
        )

    def _fire() -> None:
        background_tasks.create(_refresh())

    ui.timer(_REFRESH_INTERVAL_S, _fire, once=True)
