"""Shared lineage / provenance view (ADR-0010, design §6.1).

The single rendering surface for lineage in the UI. Mounted:

- Inline as a **"Provenance" tab** on each host page (job / workflow
  run / studies / library / material / surrogate detail). Slice 5
  wires those.
- Standalone in the admin space at ``/admin/lineage/{kind}/{id}``
  (see :mod:`matofx.ui.pages.admin_lineage`).

Two render modes, controlled by the in-page View toggle:

- **Graph** — Cytoscape.js + dagre layered DAG (slice 4b). Per-kind
  shapes + colours match the design wireframe (§6.1). Click a node to
  navigate to its ``deep_link``. Slice 4b loads Cytoscape from CDN via
  ``ui.add_head_html`` (mirroring the model-viewer pattern in
  :mod:`matofx.ui.cad_preview`); the air-gap-bundle vendoring step
  per design §6.1 is a separate follow-up.
- **Table** — per-kind sectioned tables (slice 4a). Always available;
  the design's accessibility default (§6.2). Auto-selected when the
  graph is ``soft_capped`` (design §4.5).

The component is mountable standalone — :func:`render_lineage_view`
takes a NiceGUI container and the centre's ``(kind, id)``, and does
its own API call. Host pages pass their own centre, the admin page
passes the URL-path values.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

from nicegui import background_tasks, ui

from matofx.ui._cytoscape import render_cytoscape_canvas
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import icon_chip
from matofx.ui.state import current_token

# Human-readable label per node kind for the table-section headers and
# the filter chips. Stays in sync with :class:`matofx.lineage.NodeKind`.
_KIND_LABELS: dict[str, str] = {
    "job": "Jobs",
    "workflow_run": "Workflow runs",
    "parametric_study": "Parametric studies",
    "optimisation_study": "Optimisation studies",
    "job_cohort": "Job cohorts",
    "tool_version": "Tool versions",
    "library_file_version": "Library file versions",
    "material_version": "Material versions",
    "surrogate_model_version": "Surrogate model versions",
    "scenario": "Scenarios",
    "project": "Projects",
    "user": "Users",
    "approval_request": "Approval requests",
    "vv_record": "V&V records",
}

# Order the sections render in. Centre kind is rendered first, then
# this order, skipping kinds the centre filled already. Matches the
# rough "owner / actor / artefact" grouping the design's wireframe
# uses (§6.1).
_KIND_ORDER: tuple[str, ...] = (
    "job",
    "workflow_run",
    "parametric_study",
    "optimisation_study",
    "job_cohort",
    "tool_version",
    "library_file_version",
    "material_version",
    "surrogate_model_version",
    "scenario",
    "project",
    "user",
    "approval_request",
    "vv_record",
)

# Material icon per node kind for the table-section icon chips. Keeps
# the lineage tables in the home / Compare card idiom. Unmapped kinds
# fall back to a neutral glyph.
_KIND_ICON: dict[str, str] = {
    "job": "play_circle",
    "workflow_run": "account_tree",
    "parametric_study": "science",
    "optimisation_study": "trending_up",
    "job_cohort": "grid_view",
    "tool_version": "construction",
    "library_file_version": "description",
    "material_version": "category",
    "surrogate_model_version": "psychology",
    "scenario": "folder",
    "project": "folder_special",
    "user": "person",
    "approval_request": "how_to_reg",
    "vv_record": "fact_check",
}

# Status → Quasar badge colour for the per-kind tables. Functional
# status colours (failed = red, complete = green, …) are a deliberate
# exception to the brand palette; anything unmapped is neutral grey.
_STATE_BADGE_COLOUR: dict[str, str] = {
    "complete": "positive",
    "completed": "positive",
    "succeeded": "positive",
    "published": "positive",
    "approved": "positive",
    "active": "positive",
    "failed": "negative",
    "rejected": "negative",
    "cancelled": "negative",
    "error": "negative",
    "running": "info",
    "queued": "warning",
    "pending": "warning",
    "created": "grey-6",
    "draft": "grey-6",
    "new": "grey-6",
    "inactive": "grey-6",
    "archived": "grey-6",
    "superseded": "grey-6",
    "retired": "grey-6",
}


def _state_badge_colour(state: str) -> str:
    """Quasar badge colour for a node ``state`` (neutral grey default)."""
    return _STATE_BADGE_COLOUR.get(state.strip().lower(), "grey-6")


def render_provenance_section(
    *,
    kind: str,
    obj_id: int,
    label: str = "Provenance",
    initially_open: bool = False,
) -> None:
    """Mount a lazy-loading "Provenance" section on a host page.

    Used by the inline-tab integration (slice 5) on host pages that
    don't have a tab strip — the job page, workflow-run page, library
    detail, material detail. The section is a collapsible
    :class:`ui.expansion` that fires :func:`render_lineage_view`
    only on first open, so the API call is deferred until the user
    actually wants the graph.

    Pages that *do* have a tab strip (studies — Progress / Results)
    call :func:`render_lineage_view` directly into a tab panel
    instead of using this expansion wrapper.

    The standalone admin page link is rendered alongside the toggle
    so power users can pop the graph open in a dedicated tab without
    losing the host-page context.
    """
    container: ui.column

    async def _on_open_change(event: Any) -> None:
        # Only act on the first open — subsequent toggles don't re-fetch.
        opened = bool(event.value)
        if opened and not getattr(container, "_lineage_loaded", False):
            container._lineage_loaded = True  # type: ignore[attr-defined]
            await render_lineage_view(container, kind=kind, obj_id=obj_id)

    expansion = ui.expansion(label, value=initially_open).classes("w-full")
    expansion.props('header-class="text-weight-medium"')
    expansion.on_value_change(_on_open_change)
    with expansion, ui.column().classes("w-full gap-3").style("padding-top: 0.5rem;") as col:
        container = col
        with ui.row().classes("items-center gap-2 w-full").style("padding-bottom: 0.25rem;"):
            ui.label("Provenance graph — read-only.").style(
                "color: var(--matofx-muted-text); font-size: 0.85rem;"
            )
            ui.space()
            ui.button(
                "Open in admin space",
                on_click=lambda: ui.navigate.to(f"/admin/lineage/{kind}/{obj_id}", new_tab=True),
            ).props("flat dense").style("font-size: 0.75rem;")

    if initially_open:
        # When the host page asks for an initially-open expansion (e.g.
        # the workflow-run page opening Provenance because the user
        # came in via a ``?tab=provenance`` query), fire the fetch
        # immediately rather than waiting for an interaction.
        container._lineage_loaded = True  # type: ignore[attr-defined]

        async def _autoload() -> None:
            await render_lineage_view(container, kind=kind, obj_id=obj_id)

        ui.timer(0.01, _autoload, once=True)


async def render_lineage_view(
    container: ui.element,
    *,
    kind: str,
    obj_id: int,
    upstream: int = 6,
    downstream: int = 3,
    view_mode: str = "graph",
    hidden_kinds: frozenset[str] = frozenset(),
    on_navigate: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Render the lineage view for ``(kind, obj_id)`` into ``container``.

    The container is cleared and repopulated with:

    - A header showing the centre's label + state.
    - The filter / depth row (upstream/downstream sliders, view-mode
      toggle, per-kind show/hide chips, reload button).
    - The graph **or** table view depending on ``view_mode``.
    - An edge-summary block beneath.
    - An export-buttons row (Image + JSON).

    ``view_mode`` defaults to ``"graph"``; auto-falls-through to
    ``"table"`` when the response's ``soft_capped`` is true (design
    §4.5). ``hidden_kinds`` is the per-kind hide-set from the chips —
    nodes of those kinds drop from the graph + the table sections;
    edges that crossed them collapse to dashed stubs.

    ``on_navigate`` lets a host page intercept node clicks for in-page
    transitions instead of full browser navigation. Defaults to
    :func:`ui.navigate.to` for the standalone-page mount.
    """
    container.clear()
    with container:
        ui.label("Loading lineage…").style("color: var(--matofx-muted-text);")

    client = ApiClient(get_app(), token=current_token())
    try:
        graph = await client.lineage_graph(
            kind=kind, obj_id=obj_id, upstream=upstream, downstream=downstream
        )
    except ApiError as exc:
        _render_error(container, exc, kind=kind, obj_id=obj_id)
        return

    # Soft-cap fallback (design §4.5) — when the build hit the node
    # cap the layered-DAG layout doesn't render gracefully and the
    # tabular view is the explicit accessibility default.
    effective_mode = "table" if graph.get("soft_capped") else view_mode

    _render_graph(
        container,
        graph=graph,
        kind=kind,
        obj_id=obj_id,
        upstream=upstream,
        downstream=downstream,
        view_mode=effective_mode,
        hidden_kinds=hidden_kinds,
        on_navigate=on_navigate,
    )


# ---- rendering helpers --------------------------------------------------


def _friendly_kind(kind: str) -> str:
    """Human label for a node kind (falls back to a title-cased slug)."""
    return _KIND_LABELS.get(kind, kind.replace("_", " ").title())


def _friendly_lineage_error(exc: ApiError, *, kind: str) -> tuple[str, str, str, str]:
    """Map an :class:`ApiError` to ``(headline, body, icon, colour)``.

    Plain-English, no HTTP jargon — the status code only steers *which*
    friendly message to show. Pure function so the copy can be unit
    tested without rendering. ``colour`` is a CSS var: muted for the
    "expected" cases (nothing recorded / no access), error-red for an
    actual failure.
    """
    noun = _friendly_kind(kind).lower()
    status = exc.status_code
    muted = "var(--matofx-muted-text)"
    error = "var(--matofx-error-text)"
    if status == HTTPStatus.NOT_FOUND:
        return (
            "Nothing to show here yet",
            f"We couldn't find any provenance for this {noun}. It may be new, "
            "may have been removed, or might not be visible to your account.",
            "search_off",
            muted,
        )
    if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        return (
            "You don't have access to this",
            f"Your account doesn't have permission to view this {noun}'s "
            "provenance. Ask an administrator if you think you should.",
            "lock",
            muted,
        )
    if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return (
            "Something went wrong building the graph",
            "We hit a problem putting the provenance together. Please try "
            "again in a moment — if it keeps happening, let your administrator "
            "know.",
            "error_outline",
            error,
        )
    return (
        "We couldn't load the provenance",
        exc.detail or "The request couldn't be completed. Please try again.",
        "error_outline",
        error,
    )


def _render_error(container: ui.element, exc: ApiError, *, kind: str, obj_id: int) -> None:
    """Friendly inline error rendering — keep the surface alive on 4xx."""
    container.clear()
    headline, body, icon, colour = _friendly_lineage_error(exc, kind=kind)
    with (
        container,
        ui.column()
        .classes("w-full items-center gap-2")
        .style("padding: 1.75rem 1rem; text-align: center;"),
    ):
        ui.icon(icon).style(f"font-size: 2.5rem; color: {colour};")
        ui.label(headline).style("font-size: 1.1rem; font-weight: 600;")
        ui.label(body).style(f"color: {colour}; max-width: 32rem;")
        # A muted reference line for support — the technical status sits in
        # its tooltip rather than in the user's face.
        ui.label(f"{_friendly_kind(kind)} #{obj_id}").classes("cursor-help").style(
            "color: var(--matofx-muted-text); font-size: 0.8rem;"
        ).tooltip(f"Reference for support — HTTP {exc.status_code}: {exc.detail}")


def _render_graph(
    container: ui.element,
    *,
    graph: dict[str, Any],
    kind: str,
    obj_id: int,
    upstream: int,
    downstream: int,
    view_mode: str,
    hidden_kinds: frozenset[str],
    on_navigate: Callable[[str], Awaitable[None]] | None,
) -> None:
    """Render the loaded graph dict — graph or table + filter row + footer."""
    container.clear()
    centre_node = _find_centre(graph)
    filtered = _apply_hidden_kinds(graph, hidden_kinds)
    centre_present_after_filter = _find_centre(filtered) is not None
    # Never let the chip filters drop the centre — the user can hide
    # the centre's surrounding kinds, but the inspected node always
    # stays in view.
    if not centre_present_after_filter and centre_node is not None:
        filtered = _add_node_back(filtered, centre_node)

    with container, ui.column().classes("w-full gap-4"):
        _render_header(centre_node, kind=kind, obj_id=obj_id)
        _render_filter_row(
            container,
            kind=kind,
            obj_id=obj_id,
            upstream=upstream,
            downstream=downstream,
            view_mode=view_mode,
            hidden_kinds=hidden_kinds,
            graph_kinds=_kinds_in_graph(graph),
            on_navigate=on_navigate,
            soft_capped=bool(graph.get("soft_capped")),
        )
        if view_mode == "graph":
            _render_graph_view(filtered, on_navigate=on_navigate)
        else:
            _render_tables(filtered, on_navigate=on_navigate)
        _render_edge_summary(filtered, on_navigate=on_navigate)
        _render_footer(filtered, kind=kind, obj_id=obj_id)


def _kinds_in_graph(graph: dict[str, Any]) -> tuple[str, ...]:
    """Distinct node kinds present in ``graph``, in the §6.2 display order."""
    present = {str(n["kind"]) for n in graph.get("nodes", [])}
    out = [k for k in _KIND_ORDER if k in present]
    out.extend(sorted(present - set(_KIND_ORDER)))
    return tuple(out)


def _apply_hidden_kinds(graph: dict[str, Any], hidden_kinds: frozenset[str]) -> dict[str, Any]:
    """Return a new graph dict with ``hidden_kinds`` removed.

    Edges that cross a hidden node collapse to placeholder
    "(N kinds hidden)" stubs by bumping the edge's
    ``truncated_count`` and dropping the now-orphaned endpoint. Done
    client-side so the user can toggle without re-fetching.
    """
    if not hidden_kinds:
        return graph
    nodes = [n for n in graph.get("nodes", []) if str(n["kind"]) not in hidden_kinds]
    remaining_ids = {(str(n["kind"]), int(n["id"])) for n in nodes}
    edges_out: list[dict[str, Any]] = []
    hidden_count = 0
    for edge in graph.get("edges", []):
        src_kind, src_id = str(edge["source"][0]), int(edge["source"][1])
        tgt_kind, tgt_id = str(edge["target"][0]), int(edge["target"][1])
        src_visible = (src_kind, src_id) in remaining_ids
        tgt_visible = (tgt_kind, tgt_id) in remaining_ids
        if src_visible and tgt_visible:
            edges_out.append(edge)
            continue
        if not src_visible and not tgt_visible:
            hidden_count += 1
            continue
        # One side hidden: emit the edge with bumped truncation so the
        # graph viewer can render it as a dashed "via N hidden" stub.
        bumped = dict(edge)
        bumped["truncated_count"] = int(edge.get("truncated_count", 0) or 0) + 1
        edges_out.append(bumped)
    out = dict(graph)
    out["nodes"] = nodes
    out["edges"] = edges_out
    # Track how many edges were elided so the footer can mention them.
    out["hidden_by_chip"] = hidden_count
    return out


def _add_node_back(graph: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Re-add the centre when chip filtering would have dropped it."""
    out = dict(graph)
    out["nodes"] = [*graph.get("nodes", []), node]
    return out


def _find_centre(graph: dict[str, Any]) -> dict[str, Any] | None:
    centre_id = graph.get("centre")
    if not centre_id or ":" not in centre_id:
        return None
    kind, obj_id_str = centre_id.split(":", 1)
    try:
        obj_id = int(obj_id_str)
    except ValueError:
        return None
    for node in graph.get("nodes", []):
        if node.get("kind") == kind and node.get("id") == obj_id:
            return dict(node)
    return None


def _render_header(centre: dict[str, Any] | None, *, kind: str, obj_id: int) -> None:
    """Top-of-view label + state badge for the centred node."""
    label = str(centre.get("label")) if centre and centre.get("label") else f"{kind} #{obj_id}"
    with ui.row().classes("items-baseline gap-3"):
        ui.label("Provenance —").style("color: var(--matofx-muted-text); font-size: 0.95rem;")
        ui.label(label).style("font-size: 1.35rem; font-weight: 600;")
        state = (centre or {}).get("state")
        if state:
            ui.badge(state).props("color=primary outline").style("font-size: 0.7rem;")
        archived = bool((centre or {}).get("archived"))
        if archived:
            ui.badge("archived").props("color=grey").style("font-size: 0.7rem;")


def _label_with_help(text: str, tooltip: str) -> None:
    """A control label paired with a hover ``?`` icon explaining it.

    Keeps the label and its help affordance on one line so the filter
    row reads cleanly. The icon shows the tooltip on hover (and on tap,
    via Quasar's QTooltip) so a user who's unsure what a slider does
    gets an in-place explanation rather than having to find the docs.
    """
    with ui.row().classes("items-center gap-1").style("flex-wrap: nowrap;"):
        ui.label(text).style("font-size: 0.85rem;")
        ui.icon("help_outline").classes("cursor-help").style(
            "font-size: 1rem; color: var(--matofx-muted-text);"
        ).tooltip(tooltip)


def _render_filter_row(
    container: ui.element,
    *,
    kind: str,
    obj_id: int,
    upstream: int,
    downstream: int,
    view_mode: str,
    hidden_kinds: frozenset[str],
    graph_kinds: tuple[str, ...],
    on_navigate: Callable[[str], Awaitable[None]] | None,
    soft_capped: bool,
) -> None:
    """Upstream / downstream sliders + view toggle + show/hide chips.

    The depth sliders re-fetch on **commit** — Quasar's ``change``
    event fires once on pointer-release, so dragging updates the live
    value (``on_value_change`` → ``state``) without a request per tick,
    and letting go triggers a single fresh fetch at the chosen depth.
    The view toggle + chips are client-side — flipping either re-mounts
    the component with the same graph data, no extra API round-trip.
    Every control is reactive, so there's no manual reload button — the
    view always reflects the current depth/filter/view selection.
    """
    state: dict[str, Any] = {
        "upstream": upstream,
        "downstream": downstream,
        "view_mode": "table" if soft_capped else view_mode,
        "hidden_kinds": hidden_kinds,
    }

    async def re_render() -> None:
        # Re-mount the whole component with the current state. We
        # keep this inside ``container`` so the outer admin-page
        # layout doesn't get clobbered. The first action of
        # ``render_lineage_view`` is ``container.clear()`` — that
        # deletes the toggle / chip widget the callback was invoked
        # from, which would normally surface as a noisy "parent slot
        # has been deleted" log line. We detach via
        # :func:`background_tasks.create` so the calling callback can
        # complete cleanly before the clear runs.
        await render_lineage_view(
            container,
            kind=kind,
            obj_id=obj_id,
            upstream=int(state["upstream"]),
            downstream=int(state["downstream"]),
            view_mode=str(state["view_mode"]),
            hidden_kinds=frozenset(state["hidden_kinds"]),
            on_navigate=on_navigate,
        )

    def re_render_detached() -> None:
        """Fire :func:`re_render` outside the current slot context."""
        background_tasks.create(re_render())

    with (
        ui.row()
        .classes("items-center gap-4 w-full flex-wrap")
        .style("padding: 0.5rem 0; border-bottom: 1px solid var(--matofx-border);")
    ):
        _label_with_help(
            "Upstream:",
            "How far back to trace — the inputs and earlier results that fed "
            "into this item. Higher numbers reveal more of its history. "
            "Release the slider to redraw.",
        )
        upstream_slider = (
            ui.slider(min=0, max=20, value=upstream, step=1)
            .props("label-always thumb-size=20px")
            .style("width: 200px;")
        )
        # ``on_value_change`` fires per drag-tick → keep ``state`` live;
        # Quasar's ``change`` fires once on pointer-release → re-fetch at
        # the committed depth (a fetch per tick would storm the API).
        upstream_slider.on_value_change(lambda e: state.update(upstream=int(e.value)))
        upstream_slider.on("change", lambda _e: re_render_detached())

        _label_with_help(
            "Downstream:",
            "How far forward to trace — the results, reports and other items "
            "built from this one. Higher numbers reveal more of what came "
            "after it. Release the slider to redraw.",
        )
        downstream_slider = (
            ui.slider(min=0, max=20, value=downstream, step=1)
            .props("label-always thumb-size=20px")
            .style("width: 200px;")
        )
        downstream_slider.on_value_change(lambda e: state.update(downstream=int(e.value)))
        downstream_slider.on("change", lambda _e: re_render_detached())

        ui.label("View:").style("font-size: 0.85rem;")
        view_toggle = ui.toggle(
            {"graph": "Graph", "table": "Table"},
            value=str(state["view_mode"]),
        ).props("dense unelevated")
        if soft_capped:
            # The graph layout can't render past the soft cap (design
            # §4.5); pin the toggle to Table.
            view_toggle.disable()

        def _on_view_change(event: Any) -> None:
            state["view_mode"] = str(event.value)
            re_render_detached()

        view_toggle.on_value_change(_on_view_change)

    # Per-kind show/hide chips. Only kinds present in the current
    # response surface — no point offering "hide library files" on a
    # graph that has none.
    if graph_kinds:
        with ui.row().classes("items-center gap-1 w-full flex-wrap").style("padding: 0.25rem 0;"):
            ui.label("Show:").style("font-size: 0.75rem; color: var(--matofx-muted-text);")
            for k in graph_kinds:
                _render_kind_chip(k, state=state, re_render=re_render_detached)


def _render_kind_chip(
    k: str,
    *,
    state: dict[str, Any],
    re_render: Callable[[], None],
) -> None:
    """One toggle chip for a node kind in the filter row.

    ``re_render`` is the detached re-render entry point — it
    schedules the redraw via :mod:`nicegui.background_tasks` so
    the chip-click callback can finish before the container clears.
    """
    label = _KIND_LABELS.get(k, k.replace("_", " ").title())
    active = k not in state["hidden_kinds"]

    def _toggle() -> None:
        hidden = set(state["hidden_kinds"])
        if k in hidden:
            hidden.discard(k)
        else:
            hidden.add(k)
        state["hidden_kinds"] = frozenset(hidden)
        re_render()

    chip = ui.chip(label, on_click=_toggle).props("dense outline" if not active else "dense")
    chip.style("font-size: 0.7rem;")


def _render_graph_view(
    graph: dict[str, Any],
    *,
    on_navigate: Callable[[str], Awaitable[None]] | None,  # noqa: ARG001
) -> None:
    """Mount the Cytoscape.js graph rendering for ``graph``.

    Adds the CDN scripts (idempotent — NiceGUI's head-html dedup
    keeps repeated mounts cheap), emits a uniquely-id'd mount div,
    then pushes the cytoscape-bootstrap JS to the client via
    :func:`ui.run_javascript`. Click handlers route to the node's
    ``deep_link`` via ``window.location.href`` (full-page nav,
    matches the table-row click behaviour).

    NiceGUI 3.x refuses to render ``<script>`` tags inside ``ui.html``
    (raises ``ValueError("HTML elements must not contain <script>
    tags. Use ui.add_body_html() instead.")`` from
    ``content_element._handle_content_change``). The bootstrap
    therefore travels via ``ui.run_javascript`` — a websocket push
    to the client — rather than an inline script tag. The JS polls
    for the mount div + cytoscape globals so a small ordering delay
    between the ``ui.html`` flush and the run_javascript call is
    handled gracefully.

    ``on_navigate`` is honoured for table-row clicks (slice 4a) but
    NOT for graph-node clicks — bridging the JS event into a
    server-side coroutine would need a websocket round trip; full
    navigation matches the design's intent for the standalone admin
    page anyway.
    """
    payload = _graph_to_cytoscape_elements(graph)
    render_cytoscape_canvas(
        elements=payload["elements"],
        stylesheet=payload["style"],
        centre_id=str(payload.get("centre") or ""),
        height_px=720,
        rank_dir="TB",
    )


# Per-kind node styling for the Cytoscape graph. Shape + colour map
# follows the design wireframe (§6.1) — round-rectangle for jobs
# (primary), hexagon for runs/studies (accent), rectangles for tool
# versions and other versions/artefacts, shield (pentagon) for
# approvals, ellipse for users, dashed-border rounded rectangle for
# hierarchy containers (scenario / project), rectangle for V&V.
# (Tool versions were a slanted parallelogram/rhomboid, but the slant
# let long "<name> v<n>" labels spill past the box even when sized to
# fit — a plain rectangle keeps the text contained.)
_KIND_STYLE: dict[str, dict[str, str]] = {
    "job": {"shape": "round-rectangle", "background": "#1976d2", "color": "#fff"},
    "workflow_run": {"shape": "hexagon", "background": "#7b1fa2", "color": "#fff"},
    "parametric_study": {"shape": "hexagon", "background": "#7b1fa2", "color": "#fff"},
    "optimisation_study": {"shape": "hexagon", "background": "#7b1fa2", "color": "#fff"},
    "job_cohort": {"shape": "hexagon", "background": "#7b1fa2", "color": "#fff"},
    "tool_version": {"shape": "rectangle", "background": "#455a64", "color": "#fff"},
    "library_file_version": {"shape": "rectangle", "background": "#00838f", "color": "#fff"},
    "material_version": {"shape": "rectangle", "background": "#2e7d32", "color": "#fff"},
    "surrogate_model_version": {"shape": "rectangle", "background": "#6a1b9a", "color": "#fff"},
    "approval_request": {"shape": "pentagon", "background": "#f9a825", "color": "#000"},
    "user": {"shape": "ellipse", "background": "#90a4ae", "color": "#000"},
    "scenario": {"shape": "round-rectangle", "background": "#cfd8dc", "color": "#000"},
    "project": {"shape": "round-rectangle", "background": "#b0bec5", "color": "#000"},
    "vv_record": {"shape": "rectangle", "background": "#388e3c", "color": "#fff"},
}


def _graph_to_cytoscape_elements(graph: dict[str, Any]) -> dict[str, Any]:
    """Convert a LineageGraph JSON dict to the Cytoscape ``elements`` shape.

    Returns ``{"elements": [...], "style": [...], "centre": "kind:id"}``.
    Pure function so it can be unit-tested without a live browser.
    """
    elements: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for node in graph.get("nodes", []):
        kind = str(node["kind"])
        obj_id = int(node["id"])
        node_id = f"{kind}:{obj_id}"
        node_ids.add(node_id)
        elements.append(
            {
                "data": {
                    "id": node_id,
                    "label": str(node.get("label") or f"{kind} #{obj_id}"),
                    "kind": kind,
                    "deep_link": node.get("deep_link") or "",
                    "archived": bool(node.get("archived")),
                    "placeholder": bool(node.get("placeholder")),
                },
                "classes": " ".join(
                    [
                        f"kind-{kind}",
                        "archived" if node.get("archived") else "",
                        "placeholder" if node.get("placeholder") else "",
                    ]
                ).strip(),
            }
        )
    # Cytoscape hard-throws ("Can not create edge … with nonexistent
    # source/target"), taking the whole graph down, if an edge names an
    # endpoint that isn't in the elements. That happens whenever a node
    # is absent from the response (hidden by ACL or depth) or dropped by
    # a show/hide chip while its edge survives. Skip such dangling edges:
    # the footer already accounts for hidden nodes and the table view
    # still lists everything, so the graph renders the connected part
    # instead of failing outright. ``e``-ids stay dense via ``edge_seq``.
    edge_seq = 0
    for edge in graph.get("edges", []):
        src = f"{edge['source'][0]}:{edge['source'][1]}"
        tgt = f"{edge['target'][0]}:{edge['target'][1]}"
        if src not in node_ids or tgt not in node_ids:
            continue
        truncated = int(edge.get("truncated_count", 0) or 0)
        elements.append(
            {
                "data": {
                    "id": f"e{edge_seq}",
                    "source": src,
                    "target": tgt,
                    "relation": str(edge.get("relation", "")),
                    "label": str(edge.get("label", "")),
                    "field_name": edge.get("field_name") or "",
                    "truncated_count": truncated,
                },
                "classes": "truncated" if truncated else "",
            }
        )
        edge_seq += 1
    return {
        "elements": elements,
        "style": _cytoscape_stylesheet(),
        "centre": str(graph.get("centre", "")),
    }


def _cytoscape_stylesheet() -> list[dict[str, Any]]:
    """The Cytoscape stylesheet: per-kind shapes + dimmed archived nodes.

    Returned as a list of ``{selector, style}`` dicts in Cytoscape's
    canonical form so the JS side passes it straight through.
    """
    stylesheet: list[dict[str, Any]] = [
        {
            "selector": "node",
            "style": {
                "label": "data(label)",
                "text-valign": "center",
                "text-halign": "center",
                "font-size": 14,
                # Size the box to its label on both axes so the full
                # text always fits ([[feedback_use_labels_not_names]] is
                # only useful if the label is actually readable); wrap
                # long names onto multiple lines rather than clipping
                # them with an ellipsis.
                "width": "label",
                "height": "label",
                "padding": "12px",
                "text-wrap": "wrap",
                "text-max-width": 240,
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
            },
        },
        {
            "selector": "edge.truncated",
            "style": {
                "line-style": "dashed",
                "line-color": "#bbb",
                "target-arrow-color": "#bbb",
            },
        },
        {
            "selector": "node.archived",
            "style": {"opacity": 0.5},
        },
        {
            "selector": "node.placeholder",
            "style": {"border-style": "dashed"},
        },
    ]
    for kind, style in _KIND_STYLE.items():
        stylesheet.append(
            {
                "selector": f"node.kind-{kind}",
                "style": {
                    "shape": style["shape"],
                    "background-color": style["background"],
                    "color": style["color"],
                },
            }
        )
    return stylesheet


def _render_tables(
    graph: dict[str, Any],
    *,
    on_navigate: Callable[[str], Awaitable[None]] | None,
) -> None:
    """One table per node kind, sectioned in the §6.2 order."""
    nodes_by_kind: dict[str, list[dict[str, Any]]] = {}
    for node in graph.get("nodes", []):
        nodes_by_kind.setdefault(str(node["kind"]), []).append(node)

    if not nodes_by_kind:
        ui.label("(no nodes)").style("color: var(--matofx-muted-text);")
        return

    for kind in _KIND_ORDER:
        section_nodes = nodes_by_kind.get(kind)
        if not section_nodes:
            continue
        _render_section(kind, section_nodes, on_navigate=on_navigate)

    # Any unknown / reserved kinds the design hasn't enumerated land
    # in an "Other" section so a future ADR's kind doesn't silently
    # drop until the table-section ordering is updated.
    unknown_kinds = set(nodes_by_kind) - set(_KIND_ORDER)
    for kind in sorted(unknown_kinds):
        _render_section(kind, nodes_by_kind[kind], on_navigate=on_navigate)


def _render_section(
    kind: str,
    nodes: list[dict[str, Any]],
    *,
    on_navigate: Callable[[str], Awaitable[None]] | None,
) -> None:
    """One sectioned table for a single node kind.

    Labels lead — never the raw id ([[feedback_use_labels_not_names]]);
    state renders as a coloured badge and archived as a pill. A row is
    a clickable link **only** when its node has a viewable ``deep_link``
    and isn't a restricted placeholder — so a click can't dead-end on a
    404. Non-viewable rows render as plain, inert text.
    """
    label = _KIND_LABELS.get(kind, kind.replace("_", " ").title())
    with ui.column().classes("w-full gap-1"):
        with ui.row().classes("items-center gap-2"):
            icon_chip(_KIND_ICON.get(kind, "category"))
            ui.label(label).style(
                "font-size: 1rem; font-weight: 650; color: var(--matofx-body-text);"
            )
            ui.label(f"({len(nodes)})").style(
                "font-size: 0.85rem; color: var(--matofx-muted-text);"
            )
        rows = [
            {
                "id": n["id"],
                "label": n["label"],
                "state": n.get("state") or "",
                "state_colour": _state_badge_colour(str(n.get("state") or "")),
                "archived": bool(n.get("archived")),
                "viewable": bool(n.get("deep_link")) and not n.get("placeholder"),
                "deep_link": n.get("deep_link") or "",
            }
            for n in sorted(nodes, key=lambda n: str(n["label"]).lower())
        ]
        # Fixed column widths + ``table-layout: fixed`` so every per-kind
        # section table sizes its columns identically — that way the State
        # column starts at the same x in all of them and the badges line up
        # vertically down the page (rather than each table auto-sizing Name
        # to its own content and pushing State around).
        columns: list[dict[str, Any]] = [
            {
                "name": "label",
                "label": "Name",
                "field": "label",
                "sortable": True,
                "align": "left",
                "headerStyle": "width: 66%",
                "style": "width: 66%",
            },
            {
                "name": "state",
                "label": "State",
                "field": "state",
                "sortable": True,
                "align": "left",
                "headerStyle": "width: 28%",
                "style": "width: 28%",
            },
            {
                "name": "open",
                "label": "",
                "field": "open",
                "align": "right",
                "headerStyle": "width: 6%",
                "style": "width: 6%",
            },
        ]
        table = (
            ui.table(columns=columns, rows=rows, row_key="id")
            .classes("w-full")
            .props('dense flat table-style="table-layout: fixed"')
        )
        # Name cell: link-styled when viewable, plain otherwise; archived
        # nodes get a small pill so the lifecycle state is obvious.
        table.add_slot(
            "body-cell-label",
            "<q-td :props=\"props\" :style=\"props.row.viewable ? 'cursor: pointer;' : ''\">"
            '<div class="row items-center" style="min-height: 1.5rem;">'
            '<span :style="props.row.viewable'
            " ? 'color: var(--matofx-accent); font-weight: 500;'"
            " : 'color: var(--matofx-body-text);'\">{{ props.row.label }}</span>"
            '<q-badge v-if="props.row.archived" color="grey-6" outline'
            ' class="q-ml-sm" label="Archived" />'
            "</div>"
            "</q-td>",
        )
        table.add_slot(
            "body-cell-state",
            '<q-td :props="props">'
            '<div class="row items-center" style="min-height: 1.5rem;">'
            '<q-badge v-if="props.row.state" :color="props.row.state_colour"'
            ' :label="props.row.state" />'
            '<span v-else style="color: var(--matofx-muted-text);">—</span>'
            "</div>"
            "</q-td>",
        )
        # Trailing affordance: an open-in-new icon marks the rows that
        # actually lead somewhere viewable.
        table.add_slot(
            "body-cell-open",
            '<q-td :props="props">'
            '<div class="row items-center justify-end" style="min-height: 1.5rem;">'
            '<q-icon v-if="props.row.viewable" name="open_in_new" size="18px"'
            ' style="color: var(--matofx-accent);" />'
            "</div>"
            "</q-td>",
        )

        async def _row_click(event: Any) -> None:
            row = event.args[1] if isinstance(event.args, list) else event.args
            if not isinstance(row, dict) or not row.get("viewable"):
                return
            link = row.get("deep_link")
            if not link:
                return
            if on_navigate is not None:
                await on_navigate(link)
            else:
                ui.navigate.to(link)

        table.on("rowClick", _row_click)


def _node_label(node: dict[str, Any] | None, ref: tuple[str, int]) -> str:
    """Human label for a graph node, falling back to '<Kind> #<id>'."""
    if node and node.get("label"):
        return str(node["label"])
    kind_label = _KIND_LABELS.get(ref[0], ref[0].replace("_", " ").title())
    # Trim a trailing plural 's' so the fallback reads "Job #4", not
    # "Jobs #4" — purely cosmetic for the rare unresolved-node case.
    singular = kind_label[:-1] if kind_label.endswith("s") else kind_label
    return f"{singular} #{ref[1]}"


def _render_relation_entity(
    text: str,
    link: str,
    *,
    on_navigate: Callable[[str], Awaitable[None]] | None,
) -> None:
    """One '• entity' line — a link when viewable, plain text otherwise."""
    with ui.row().classes("items-center gap-1").style("padding-left: 0.75rem;"):
        ui.label("•").style("color: var(--matofx-muted-text);")
        if link:

            async def _go(_e: Any = None, _l: str = link) -> None:
                if on_navigate is not None:
                    await on_navigate(_l)
                else:
                    ui.navigate.to(_l)

            entity = ui.label(text).style(
                "color: var(--matofx-accent); cursor: pointer; font-size: 0.9rem;"
            )
            entity.on("click", _go)
            ui.icon("open_in_new").style("font-size: 0.9rem; color: var(--matofx-accent);")
        else:
            ui.label(text).style("font-size: 0.9rem; color: var(--matofx-body-text);")


def _render_edge_summary(
    graph: dict[str, Any],
    *,
    on_navigate: Callable[[str], Awaitable[None]] | None,
) -> None:
    """Readable, grouped list of the relationships in the graph.

    Replaces the old ``relation: count`` badges with named lines —
    e.g. *Published by → Robert Cordina* — grouping edges by their
    friendly relation label and naming the connected entity (the end
    that isn't the centre). Each named entity is a link only when it
    has a viewable ``deep_link``; restricted / out-of-depth edges roll
    up into a single muted note ([[feedback_use_labels_not_names]]).
    """
    edges = graph.get("edges", [])
    if not edges:
        return
    node_map = {(str(n["kind"]), int(n["id"])): n for n in graph.get("nodes", [])}
    centre = str(graph.get("centre") or "")

    # relation label → list of (display text, deep_link)
    grouped: dict[str, list[tuple[str, str]]] = {}
    truncated = 0
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        truncated += int(edge.get("truncated_count", 0) or 0)
        src = (str(edge["source"][0]), int(edge["source"][1]))
        tgt = (str(edge["target"][0]), int(edge["target"][1]))
        raw = str(edge.get("label") or str(edge.get("relation", "")).replace("_", " ")).strip()
        rel_label = (raw[:1].upper() + raw[1:]) if raw else "Related"
        src_id, tgt_id = f"{src[0]}:{src[1]}", f"{tgt[0]}:{tgt[1]}"
        # Name the end that isn't the centre; when neither is the centre
        # show "source → target" so deeper relationships still read.
        if src_id == centre:
            other = node_map.get(tgt)
            text, link = _node_label(other, tgt), str((other or {}).get("deep_link") or "")
        elif tgt_id == centre:
            other = node_map.get(src)
            text, link = _node_label(other, src), str((other or {}).get("deep_link") or "")
        else:
            target = node_map.get(tgt)
            text = f"{_node_label(node_map.get(src), src)} → {_node_label(target, tgt)}"
            link = str((target or {}).get("deep_link") or "")
        dedup = (rel_label, text, link)
        if dedup in seen:
            continue
        seen.add(dedup)
        grouped.setdefault(rel_label, []).append((text, link))

    with ui.column().classes("w-full gap-2").style("padding-top: 0.5rem;"):
        ui.label("Relations").style("font-size: 1rem; font-weight: 650;")
        for rel_label in sorted(grouped):
            with ui.column().classes("gap-0"):
                ui.label(rel_label).style(
                    "font-size: 0.85rem; font-weight: 600; color: var(--matofx-muted-text);"
                )
                for text, link in grouped[rel_label]:
                    _render_relation_entity(text, link, on_navigate=on_navigate)
        if truncated:
            ui.label(f"+{truncated} restricted / out-of-depth").style(
                "color: var(--matofx-muted-text); font-size: 0.85rem;"
            )


def _render_footer(graph: dict[str, Any], *, kind: str, obj_id: int) -> None:
    """Summary counters + export buttons (PDF report deferred to slice 6)."""
    node_count = len(graph.get("nodes", []))
    edge_count = len(graph.get("edges", []))
    hidden_acl = int(graph.get("hidden_by_acl", 0) or 0)
    hidden_depth = int(graph.get("hidden_by_depth", 0) or 0)
    hidden_chip = int(graph.get("hidden_by_chip", 0) or 0)
    soft_capped = bool(graph.get("soft_capped", False))
    with (
        ui.row()
        .classes("items-center gap-4 w-full")
        .style("padding-top: 0.5rem; border-top: 1px solid var(--matofx-border);")
    ):
        summary = (
            f"{node_count} node(s), {edge_count} edge(s); "
            f"{hidden_acl} hidden by access controls, {hidden_depth} by depth"
        )
        if hidden_chip:
            summary += f", {hidden_chip} by chip filter"
        summary += "."
        ui.label(summary).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        if soft_capped:
            ui.badge("graph too large — table view only").props("color=warning").style(
                "font-size: 0.7rem;"
            )
        ui.space()
        # Download via the authenticated client — opening the
        # ``/api/lineage/{...}/export`` URL in a new browser tab loses
        # the bearer token (API auth is header-only, no session
        # cookie), so we fetch the bytes via ApiClient and push to
        # the browser through ``ui.download.content``.
        ui.button(
            "Download JSON",
            on_click=lambda: _download_export(kind=kind, obj_id=obj_id, fmt="json"),
        ).props("flat dense")
        ui.button(
            "Download as Image",
            on_click=lambda: _download_export(kind=kind, obj_id=obj_id, fmt="svg"),
        ).props("flat dense")


async def _download_export(*, kind: str, obj_id: int, fmt: str) -> None:
    """Authenticated fetch → push-to-browser download for the lineage export.

    Separate function (not a closure) so the test suite can target
    it directly. Friendly notification on any
    :class:`ApiError` — the user sees "Couldn't download lineage:
    ..." rather than a silent failure.
    """
    client = ApiClient(get_app(), token=current_token())
    try:
        content, filename = await client.lineage_export(kind=kind, obj_id=obj_id, export_format=fmt)
    except ApiError as exc:
        ui.notify(f"Couldn't download lineage: {exc.detail}", type="negative")
        return
    ui.download.content(content, filename=filename)
