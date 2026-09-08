"""Results warehouse (ADR-0012 §8 / designs/kpi-extraction.md §8.1).

Two surfaces, both gated by ``features.kpi_extraction``:

- ``GET /results`` — tool-picker landing. Lists tools that declare any
  KPI. Selecting one navigates to the per-tool warehouse page.
- ``GET /results/{tool_alias}`` — the warehouse table for one tool.
  Header (tool name + version dropdown), filter card (completed
  window + per-KPI ranges + Reset), AG-Grid table (one row per
  visible completed job, KPI columns pivoted), unbounded tick-box
  selection with the "N selected" pill + Clear, "View in Compare"
  button (auto-disabled past 6 with the cap tooltip).

The Plotly trend card + CSV/PDF export buttons land in slice 7;
the V&V status filter + per-row badge land in slice 9; the per-job
"Headline KPIs" card lands in slice 10. This page is the
table-shape surface only.

Behaviour rules (feedback_unbounded_selection_capped_actions):

- Tick boxes are unbounded; the "N selected" pill always shows
  the live count.
- Selection survives pagination and filter changes — rows that
  leave the filter stay ticked but visually hidden.
- The Compare button enables only when 2 ≤ N ≤ 6; outside that
  range it disables with the per-cap tooltip.

The filter card surfaces a subset of the API's WarehouseFilters in
v1.0:

- Version dropdown (release versions of the tool).
- Completed window (date-range pickers).
- Per-KPI min / max range inputs.
- Reset link.

The project / scenario / requestor multi-select pickers are a
polish-pass follow-up — the API surface accepts them already
(slice 5), the page just doesn't expose the widgets yet.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from nicegui import ui

from matofx.ui._background import spawn
from matofx.ui._features import kpi_extraction_enabled, vv_tracking_enabled
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_job_id, format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.pages._results_plot import (
    PlotDefaults,
    axis_label,
    build_scatter_data,
    build_timeseries_data,
    default_plot_state,
)
from matofx.ui.pages._results_selection import (
    SelectionState,
)
from matofx.ui.state import current_token
from matofx.ui.theme import themed_plotly


def _client() -> ApiClient:
    return ApiClient(get_app(), token=current_token())


def _feature_ok() -> bool:
    """Redirect home when the KPI feature is off (page *absent*)."""
    if not kpi_extraction_enabled():
        ui.navigate.to("/home")
        return False
    return True


# ---- /results landing ------------------------------------------------


@ui.page("/results")
async def results_landing_page() -> None:
    """Tool-picker landing for the warehouse.

    Lists every tool that declares at least one KPI. Clicking a tile
    navigates to ``/results/{alias}`` — the actual warehouse table.
    A tool that's hidden by the per-tool ACL never appears here.
    """
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        ui.label("Results warehouse").style("font-size: 1.5rem; font-weight: 600;")
        ui.label("Cross-job KPI tables, one per tool. Pick a tool to open its warehouse.").classes(
            "text-grey-7"
        )

        try:
            tools = await client.list_tools()
        except ApiError as exc:
            ui.label(f"Couldn't load the tool list: {exc}").classes("text-negative")
            return

        with_kpis: list[dict[str, Any]] = []
        for tool in tools:
            try:
                catalog = await client.list_tool_kpis(tool["alias"])
            except ApiError:
                continue
            if catalog.get("entries"):
                with_kpis.append({**tool, "_kpi_count": len(catalog["entries"])})

        if not with_kpis:
            ui.label(
                "No tools declare KPIs yet. Tool authors mark outputs as KPIs "
                "on the descriptor's Outputs tab."
            ).classes("text-grey-7 italic")
            return

        with ui.column().classes("w-full gap-2"):
            for tool in with_kpis:
                with ui.card().classes("w-full"):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.column().classes("gap-0"):
                            ui.label(tool.get("name", tool["alias"])).style("font-weight: 600;")
                            ui.label(f"{tool['_kpi_count']} KPI(s)").classes("text-grey-7 text-xs")
                        ui.button(
                            "Open warehouse",
                            on_click=lambda alias=tool["alias"]: ui.navigate.to(
                                f"/results/{alias}"
                            ),
                        ).props("flat color=primary")


# ---- /results/{tool_alias} warehouse table ---------------------------


@ui.page("/results/{tool_alias}")
async def results_warehouse_page(tool_alias: str) -> None:
    """The per-tool KPI results warehouse."""
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()

    # --- One-time data loads ----------------------------------------
    try:
        catalog = await client.list_tool_kpis(tool_alias)
    except ApiError as exc:
        ui.label(f"Couldn't load KPIs for {tool_alias!r}: {exc}").classes("text-negative")
        return
    entries: list[dict[str, Any]] = list(catalog.get("entries", []))

    try:
        tool = await client.get_tool(tool_alias)
    except ApiError:
        tool = {"alias": tool_alias, "name": tool_alias}

    # The tool's versions for the "Tool version" filter — get_tool doesn't
    # carry a version list, so fetch it (best-effort; empty on error).
    tool_versions: list[dict[str, Any]] = []
    try:
        tool_versions = await client.list_tool_version_options(tool_alias)
    except ApiError:
        tool_versions = []

    # --- Page state -------------------------------------------------
    state: dict[str, Any] = {
        "selection": SelectionState(),
        "version_id": None,
        "completed_from": None,
        "completed_to": None,
        "vv_status": "any",
        # {kpi_name: (min|None, max|None)} — only KPIs with a bound present.
        "kpi_ranges": {},
        "page": 1,
        "page_size": 50,
        # Cached row payload for the "selection survives filter" rule —
        # ticking a row stores its job_id in the SelectionState; the
        # tick survives even after a filter change hides the row.
    }

    with ui.column().classes("w-full max-w-7xl mx-auto p-6 gap-4"):
        # Header --------------------------------------------------
        with ui.row().classes("w-full items-center justify-between"):
            with ui.row().classes("items-center gap-2"):
                ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/results")).props(
                    "flat round dense"
                ).tooltip("Back to the results warehouse")
                with ui.column().classes("gap-0"):
                    ui.label(f"Results — {tool.get('name', tool_alias)}").style(
                        "font-size: 1.5rem; font-weight: 600;"
                    )
                    ui.label(f"{len(entries)} KPI(s) declared").classes("text-grey-7")
            version_choices: dict[int | None, str] = {None: "All versions"}
            for v in tool_versions:
                vid = v.get("tool_version_id")
                if vid is None:
                    continue
                label = f"v{v.get('version', '?')}"
                if v.get("is_current"):
                    label += " (current)"
                version_choices[int(vid)] = label
            ui.select(
                options=version_choices,
                value=None,
                label="Tool version",
                on_change=lambda e: _on_version_change(e.value, state, reload),
            ).props("dense outlined")

        # Filter card ---------------------------------------------
        if entries:
            with ui.expansion("Filter").classes("w-full") as filter_card:
                _render_filter_card(
                    state,
                    entries,
                    # ``reload`` is defined below; the lambda forward-refs it.
                    reload_cb=lambda: reload(),  # noqa: PLW0108
                    vv_enabled=vv_tracking_enabled(),
                )
            del filter_card  # NiceGUI handle, no further use

        # Empty / no-KPI state ------------------------------------
        if not entries:
            with ui.card().classes("w-full"):
                ui.label(
                    f"{tool.get('name', tool_alias)} declares no KPIs. "
                    "Tool authors mark outputs as KPIs on the descriptor's "
                    "Outputs tab."
                ).classes("text-grey-7 italic")
            return

        # Selection pill ------------------------------------------
        pill_row = ui.row().classes("w-full items-center gap-2")

        # Table ---------------------------------------------------
        table_container = ui.column().classes("w-full")

        # Footer (pagination + Compare button) --------------------
        footer_row = ui.row().classes("w-full items-center justify-between")

        # Export bar (slice 7) ------------------------------------
        export_row = ui.row().classes("w-full items-center gap-2")

        # Plot card (slice 7) -------------------------------------
        plot_container = ui.column().classes("w-full")

        async def reload() -> None:
            """Fetch the current page + redraw the table + footer + exports."""
            await _reload(
                state=state,
                client=client,
                tool_alias=tool_alias,
                entries=entries,
                pill_row=pill_row,
                table_container=table_container,
                footer_row=footer_row,
                export_row=export_row,
                plot_container=plot_container,
            )

        await reload()


# ---- helpers ---------------------------------------------------------


def _on_version_change(new_value: int | None, state: dict[str, Any], reload_cb: Any) -> None:
    state["version_id"] = new_value
    state["page"] = 1
    # Don't clear selection — selection survives filter changes per
    # feedback_unbounded_selection_capped_actions.

    spawn(reload_cb())


def _date_input(label: str, value: str | None) -> Any:
    """A compact text input backed by a calendar popup; value is ``YYYY-MM-DD``."""
    with (
        ui.input(label, value=value)
        .props("dense outlined clearable")
        .style("min-width: 150px;") as date_in
    ):
        with ui.menu().props("no-parent-event") as menu:
            ui.date().bind_value(date_in)
        with date_in.add_slot("append"):
            ui.icon("event").classes("cursor-pointer").on("click", menu.open)
    return date_in


def _kpi_ranges_list(
    state: dict[str, Any],
) -> list[tuple[str, float | None, float | None]]:
    """Flatten ``state['kpi_ranges']`` into the client's ``(name, min, max)`` list."""
    out: list[tuple[str, float | None, float | None]] = []
    for name, bounds in (state.get("kpi_ranges") or {}).items():
        vmin, vmax = bounds
        out.append((str(name), vmin, vmax))
    return out


def _render_filter_card(
    state: dict[str, Any],
    entries: list[dict[str, Any]],
    reload_cb: Any,
    *,
    vv_enabled: bool,
) -> None:
    """The collapsible filter card: completed window + V&V status + per-KPI ranges.

    All filters apply together on **Apply** (page resets to 1); **Reset**
    clears the lot. Selection survives per
    ``feedback_unbounded_selection_capped_actions``.
    """

    def _run_reload() -> None:
        # ui.timer(once) is the NiceGUI idiom for "kick an async reload from a
        # sync handler" — no dangling asyncio task to keep a reference to.
        state["page"] = 1
        ui.timer(0.0, reload_cb, once=True)

    with ui.column().classes("w-full gap-3").style("padding: 0.5rem 0.25rem;"):
        # Completed window.
        with ui.row().classes("items-end gap-3 flex-wrap"):
            ui.label("Completed between").style(
                "color: var(--matofx-muted-text); font-size: 0.85rem; padding-bottom: 0.4rem;"
            )
            from_input = _date_input("From", state.get("completed_from"))
            to_input = _date_input("To", state.get("completed_to"))
            vv_sel = None
            if vv_enabled:
                vv_sel = (
                    ui.select(
                        {
                            "any": "Any",
                            "verified_only": "Verified",
                            "verified_validated": "Verified & validated",
                        },
                        value=state.get("vv_status", "any"),
                        label="V&V status",
                    )
                    .props("dense outlined")
                    .style("min-width: 200px;")
                )

        # Per-KPI value ranges — one row per declared KPI.
        range_inputs: dict[str, tuple[Any, Any]] = {}
        if entries:
            ui.label("KPI value ranges").style("font-weight: 600; margin-top: 0.35rem;")
            existing = state.get("kpi_ranges") or {}
            for e in entries:
                name = str(e.get("kpi_name") or "")
                if not name:
                    continue
                label = str(e.get("kpi_label") or name)
                unit = e.get("kpi_unit")
                cur_min, cur_max = existing.get(name, (None, None))
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.label(f"{label} ({unit})" if unit else label).style(
                        "min-width: 200px; font-size: 0.9rem;"
                    )
                    min_in = (
                        ui.number(label="min", value=cur_min)
                        .props("dense outlined")
                        .style("width: 130px;")
                    )
                    max_in = (
                        ui.number(label="max", value=cur_max)
                        .props("dense outlined")
                        .style("width: 130px;")
                    )
                    range_inputs[name] = (min_in, max_in)

        def _apply() -> None:
            state["completed_from"] = (from_input.value or "").strip() or None
            state["completed_to"] = (to_input.value or "").strip() or None
            if vv_sel is not None:
                state["vv_status"] = vv_sel.value or "any"
            ranges: dict[str, tuple[float | None, float | None]] = {}
            for name, (min_in, max_in) in range_inputs.items():
                vmin = min_in.value if min_in.value not in (None, "") else None
                vmax = max_in.value if max_in.value not in (None, "") else None
                if vmin is not None or vmax is not None:
                    ranges[name] = (vmin, vmax)
            state["kpi_ranges"] = ranges
            _run_reload()

        def _reset() -> None:
            from_input.value = None
            to_input.value = None
            if vv_sel is not None:
                vv_sel.value = "any"
            for min_in, max_in in range_inputs.values():
                min_in.value = None
                max_in.value = None
            state["completed_from"] = None
            state["completed_to"] = None
            state["vv_status"] = "any"
            state["kpi_ranges"] = {}
            _run_reload()

        with ui.row().classes("gap-2").style("margin-top: 0.35rem;"):
            ui.button("Apply", icon="filter_alt", on_click=_apply).props(
                "color=primary no-caps unelevated"
            )
            ui.button("Reset", icon="clear", on_click=_reset).props("flat no-caps")


async def _reload(
    *,
    state: dict[str, Any],
    client: ApiClient,
    tool_alias: str,
    entries: list[dict[str, Any]],
    pill_row: Any,
    table_container: Any,
    footer_row: Any,
    export_row: Any | None = None,
    plot_container: Any | None = None,
) -> None:
    """Fetch + redraw the warehouse table.

    Re-renders the selection pill, the AG-Grid, and the footer
    (pagination + Compare action). Called on initial mount and on
    every filter / pagination / version change.
    """
    try:
        page = await client.list_tool_results(
            tool_alias,
            version_id=state["version_id"],
            completed_from=state["completed_from"],
            completed_to=state["completed_to"],
            kpi_ranges=_kpi_ranges_list(state),
            vv_status=state.get("vv_status", "any"),
            page=state["page"],
            page_size=state["page_size"],
        )
    except ApiError as exc:
        table_container.clear()
        with table_container:
            ui.label(f"Couldn't load results: {exc}").classes("text-negative")
        return

    rows = list(page.get("rows", []))
    total = int(page.get("total", 0))
    hidden = int(page.get("hidden_by_acl", 0))

    # The grid (set below when there are rows); ``None`` on an empty
    # page. The selection-widget closures close over it by name so a
    # Clear click can deselect the live grid.
    grid: Any = None
    compare_slot = None

    async def _on_clear() -> None:
        """Clear the whole selection — state, the grid ticks, the widgets."""
        state["selection"] = SelectionState()
        if grid is not None:
            await grid.run_grid_method("deselectAll")
        _repaint_selection_widgets()

    def _paint_pill() -> None:
        selection: SelectionState = state["selection"]
        pill_row.clear()
        with pill_row:
            ui.chip(f"{selection.count} selected").props("color=primary text-color=white dense")
            if selection.count:
                ui.button("Clear selection", on_click=_on_clear).props("flat dense")

    def _paint_compare() -> None:
        if compare_slot is None:
            return
        compare_slot.clear()
        action = state["selection"].compare_action()
        with compare_slot:
            btn = ui.button(
                action.label,
                on_click=(
                    lambda h=action.href: ui.navigate.to(h) if action.enabled and h else None
                ),
            ).props("color=primary unelevated")
            if not action.enabled:
                btn.props("disable")
                if action.tooltip:
                    with btn:
                        ui.tooltip(action.tooltip)

    def _paint_export() -> None:
        if export_row is None:
            return
        export_row.clear()
        with export_row:
            _render_export_buttons(tool_alias=tool_alias, state=state, client=client)

    def _repaint_selection_widgets() -> None:
        """Repaint everything that depends on the live selection.

        Called on every tick / untick / clear so the pill, the Compare
        button, and the "Download selected" export button all stay in
        sync without re-fetching the page.
        """
        _paint_pill()
        _paint_compare()
        _paint_export()

    # AG-Grid table.
    table_container.clear()
    with table_container:
        if not rows:
            ui.label("No completed jobs match these filters yet.").classes("text-grey-7 italic")
            # Still paint the pill so an off-page selection stays visible
            # and clearable even when this page is empty.
            _paint_pill()
            footer_row.clear()
            if export_row is not None:
                export_row.clear()
            if plot_container is not None:
                plot_container.clear()
            return
        coldefs = _build_coldefs(entries)
        ag_rows = _build_ag_rows(rows, entries)
        grid = (
            ui.aggrid(
                {
                    "columnDefs": coldefs,
                    "rowData": ag_rows,
                    "rowSelection": "multiple",
                    "suppressRowClickSelection": True,
                    # Stable per-row id so we can restore tick state by
                    # job_id after a page/filter change (run_row_method).
                    ":getRowId": "(params) => String(params.data.job_id)",
                    "defaultColDef": {
                        "sortable": True,
                        "filter": True,
                        "resizable": True,
                    },
                }
            )
            .classes("w-full")
            .style("height: 55vh;")
        )

        async def _on_selection_change() -> None:
            if grid is None:
                return
            picked = await grid.get_selected_rows()
            picked_ids = {int(r["job_id"]) for r in picked}
            # Selection survives filter: rows ticked in earlier pages
            # / filter states stay in the set even if they're not on
            # the current page. So union the current grid's selection
            # with the off-page rows (everything not in this page's
            # row data).
            current_page_ids = {int(r["job_id"]) for r in rows}
            off_page = state["selection"].ticked - current_page_ids
            new_set = picked_ids | off_page
            state["selection"] = SelectionState(ticked=frozenset(new_set))
            _repaint_selection_widgets()

        async def _restore_ticks() -> None:
            # Re-tick rows that were selected on a previous page / before
            # a filter change so the selection survives *visually*, not
            # just in the count (feedback_unbounded_selection_capped_actions).
            ticked = state["selection"].ticked
            for r in rows:
                if int(r["job_id"]) in ticked:
                    await grid.run_row_method(str(r["job_id"]), "setSelected", True)

        grid.on("selectionChanged", lambda _e: _async(_on_selection_change()))
        grid.on("firstDataRendered", lambda _e: _async(_restore_ticks()))

    # Footer — pagination + Compare button.
    async def _full_reload() -> None:
        await _reload(
            state=state,
            client=client,
            tool_alias=tool_alias,
            entries=entries,
            pill_row=pill_row,
            table_container=table_container,
            footer_row=footer_row,
            export_row=export_row,
            plot_container=plot_container,
        )

    def _go_page(delta: int) -> None:
        state["page"] = max(1, state["page"] + delta)
        _async(_full_reload())

    footer_row.clear()
    with footer_row:
        # Pagination.
        with ui.row().classes("items-center gap-2"):
            start = (state["page"] - 1) * state["page_size"] + 1
            end = min(state["page"] * state["page_size"], total)
            count_label = f"Showing {start}–{end} of {total}" if total else "Showing 0 of 0"
            if hidden:
                count_label += f" • {hidden} hidden by access controls"
            ui.label(count_label).classes("text-grey-7")
            ui.button("Previous", on_click=lambda: _go_page(-1)).props(
                "flat dense"
            ).bind_enabled_from(state, "page", lambda v: v > 1)
            ui.button("Next", on_click=lambda: _go_page(+1)).props("flat dense").bind_enabled_from(
                state,
                "page",
                lambda v: v * state["page_size"] < total,
            )

        # Compare action — painted into its own slot so selection
        # changes can repaint just the button (not the pagination).
        compare_slot = ui.row().classes("items-center")

    # Paint the selection-dependent widgets (pill / Compare / export)
    # now that all their slots exist.
    _repaint_selection_widgets()

    # Plot card (slice 7) -------------------------------------
    if plot_container is not None and entries:
        plot_container.clear()
        with plot_container:
            await _render_plot_card(
                tool_alias=tool_alias,
                entries=entries,
                rows=rows,
                state=state,
                client=client,
            )


def _build_coldefs(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the AG-Grid columnDefs — identity cols + one per KPI."""
    coldefs: list[dict[str, Any]] = [
        {
            "headerName": "",
            "checkboxSelection": True,
            "headerCheckboxSelection": True,
            "width": 50,
            "pinned": "left",
            "filter": False,
            "sortable": False,
        },
        {
            "headerName": "Job",
            "field": "job_id_display",
            "maxWidth": 130,
            "pinned": "left",
        },
        {"headerName": "Project", "field": "project_display"},
        {"headerName": "Scenario", "field": "scenario_display"},
        {"headerName": "Requestor", "field": "requestor_display"},
        {"headerName": "Completed", "field": "completed_at_display"},
        {"headerName": "Tool version", "field": "tool_version_display"},
    ]
    for e in entries:
        header = e.get("kpi_label") or e.get("kpi_name", "")
        unit = e.get("kpi_unit")
        if unit:
            header = f"{header} ({unit})"
        coldefs.append(
            {
                "headerName": header,
                "field": f"kpi__{e['kpi_name']}",
                "type": "numericColumn",
                "filter": "agNumberColumnFilter",
            }
        )
    return coldefs


def _build_ag_rows(
    rows: list[dict[str, Any]],
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pivot the API's row shape into AG-Grid rowData with KPI columns flat."""
    out: list[dict[str, Any]] = []
    for r in rows:
        kpi_values = r.get("kpi_values", {}) or {}
        row: dict[str, Any] = {
            "job_id": r["job_id"],
            "job_id_display": format_job_id(r["job_id"]),
            # Show labels, not bare ids (feedback_use_labels_not_names);
            # fall back to the id when the name didn't resolve.
            "project_display": _label_or_id(r.get("project_name"), r.get("project_id")),
            "scenario_display": _label_or_id(r.get("scenario_name"), r.get("scenario_id")),
            "requestor_display": _label_or_id(r.get("requestor_name"), r.get("requestor_user_id")),
            "completed_at_display": _format_completed_at(r.get("completed_at")),
            "tool_version_display": _label_or_id(
                r.get("tool_version_label"), r.get("tool_version_id")
            ),
        }
        for e in entries:
            row[f"kpi__{e['kpi_name']}"] = kpi_values.get(e["kpi_name"])
        out.append(row)
    return out


def _label_or_id(label: Any, fallback_id: Any) -> str:
    """Prefer the human-readable label; fall back to the id; else blank.

    The warehouse identity columns show names (project / scenario /
    requestor / tool version) per feedback_use_labels_not_names, but a
    name can be missing (orphaned FK, ACL trimming) — show the id so
    the row is still traceable rather than blank.
    """
    if label:
        return str(label)
    if fallback_id is not None:
        return str(fallback_id)
    return ""


def _format_completed_at(raw: Any) -> str:
    if not raw:
        return ""
    if isinstance(raw, str):
        try:
            dt = _dt.datetime.fromisoformat(raw)
        except ValueError:
            return raw
    elif isinstance(raw, _dt.datetime):
        dt = raw
    else:
        return str(raw)
    try:
        return format_user_datetime(dt)
    except Exception:
        return dt.isoformat()


# ---- Export bar (slice 7) -------------------------------------------


def _render_export_buttons(
    *,
    tool_alias: str,
    state: dict[str, Any],
    client: ApiClient,
) -> None:
    """Three download buttons: CSV (filtered) + CSV (selected) + PDF.

    "Download selected as CSV (N)" is disabled when N == 0 — when
    the user hasn't ticked any row, the filtered CSV button is the
    only path to a partial export. PDF respects the same selection
    semantics: with ticked rows present, only those export; with
    none ticked, the full filtered set.

    Per design §8.1 export-bar — the URLs honour the current filter
    state (version / completed window) read off ``state``.
    """
    selection: SelectionState = state["selection"]
    selected_ids = sorted(selection.ticked) or None
    common_kwargs: dict[str, Any] = {
        "version_id": state.get("version_id"),
        "completed_from": state.get("completed_from"),
        "completed_to": state.get("completed_to"),
        "kpi_ranges": _kpi_ranges_list(state),
        "vv_status": state.get("vv_status", "any"),
    }

    # These endpoints are bearer-only, so a browser ``<a href>`` 401s (the
    # bug users hit). Fetch the bytes server-side (the api_client carries the
    # bearer) and hand them to ``ui.download.content`` — the same pattern the
    # job-output + per-job-report downloads use.
    async def _download_csv(job_ids: list[int] | None) -> None:
        try:
            data = await client.download_tool_results_csv(
                tool_alias, **common_kwargs, job_ids=job_ids
            )
        except ApiError as exc:
            ui.notify(f"Couldn't export CSV: {exc.detail}", type="negative")
            return
        ui.download.content(data, f"{tool_alias}_results.csv")

    async def _download_pdf() -> None:
        try:
            data = await client.download_tool_results_pdf(
                tool_alias, **common_kwargs, job_ids=selected_ids
            )
        except ApiError as exc:
            ui.notify(f"Couldn't export the report: {exc.detail}", type="negative")
            return
        # Print-ready HTML — open it and pick "Save as PDF" in the print dialog.
        ui.download.content(data, f"{tool_alias}_results.html")

    # 1) Filtered CSV — always enabled.
    ui.button("Download CSV (filtered)", on_click=lambda: _download_csv(None)).props(
        "flat color=primary"
    )

    # 2) Selected CSV — disabled at N == 0.
    if selected_ids is not None:
        ui.button(
            f"Download selected as CSV ({selection.count})",
            on_click=lambda: _download_csv(selected_ids),
        ).props("flat color=primary")
    else:
        btn = ui.button("Download selected as CSV (0)").props("flat color=primary disable")
        with btn:
            ui.tooltip("Tick at least one row to export the selection")

    # 3) PDF — same selection-aware behaviour as CSV.
    ui.button("Download PDF", on_click=_download_pdf).props("flat color=primary")


# ---- Plotly card (slice 7) ------------------------------------------


async def _render_plot_card(
    *,
    tool_alias: str,
    entries: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    state: dict[str, Any],
    client: ApiClient,
) -> None:
    """Plot card below the table — X/Y scatter or time-series toggle.

    State key ``_plot`` stashes the user's last mode + axis picks so
    a filter change rebuilds the figure with the same picks (rather
    than snap-reverting to defaults). First-render seeds from
    :func:`default_plot_state`.
    """
    from plotly import graph_objects as go  # noqa: PLC0415

    if not entries:
        return

    plot_state: dict[str, Any] = state.get("_plot") or {}
    if "mode" not in plot_state:
        defaults: PlotDefaults = default_plot_state(entries)
        plot_state = {
            "mode": defaults.mode,
            "x_kpi": defaults.x_kpi,
            "y_kpi": defaults.y_kpi,
        }
        state["_plot"] = plot_state

    kpi_names = [e["kpi_name"] for e in entries]
    # Axis pickers key on kpi_name but display the label + unit
    # (feedback_use_labels_not_names).
    kpi_options: dict[str, str] = {
        e["kpi_name"]: axis_label(entries, e["kpi_name"]) for e in entries
    }

    with ui.expansion("Plot", value=True).classes("w-full"):
        with ui.row().classes("w-full items-center gap-2"):
            mode_select = ui.select(
                options={"scatter": "Scatter", "timeseries": "Time-series"},
                value=plot_state["mode"],
                label="Mode",
            ).props("dense outlined")
            x_select = ui.select(
                options=kpi_options,
                value=plot_state.get("x_kpi") or kpi_names[0],
                label="X axis",
            ).props("dense outlined")
            y_select = ui.select(
                options=kpi_options,
                value=plot_state.get("y_kpi") or kpi_names[0],
                label="Y axis",
            ).props("dense outlined")
            plot_slot = ui.column().classes("w-full")

        async def _redraw() -> None:
            plot_state["mode"] = mode_select.value
            plot_state["x_kpi"] = x_select.value
            plot_state["y_kpi"] = y_select.value
            plot_slot.clear()
            with plot_slot:
                fig = await _build_plot_figure(
                    tool_alias=tool_alias,
                    entries=entries,
                    rows=rows,
                    state=state,
                    client=client,
                    plot_state=plot_state,
                    go=go,
                )
                # Route through ``themed_plotly`` so the chart adopts the
                # active palette (card-matching background + body font) and
                # the shared runtime config, instead of rendering with
                # Plotly's untinted default that clashed with the theme.
                ui.plotly(themed_plotly(fig)).classes("w-full").style("height: 400px;")

        # Wire pickers — every change repaints.
        mode_select.on_value_change(lambda _e: _async(_redraw()))
        x_select.on_value_change(lambda _e: _async(_redraw()))
        y_select.on_value_change(lambda _e: _async(_redraw()))

        # Initial draw.
        await _redraw()


def _async(coro: Any) -> None:
    """Schedule a coroutine on the running loop (UI event handlers can't await)."""
    spawn(coro)


async def _build_plot_figure(
    *,
    tool_alias: str,
    entries: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    state: dict[str, Any],
    client: ApiClient,
    plot_state: dict[str, Any],
    go: Any,
) -> Any:
    """Construct the Plotly figure for the current mode + axis picks."""
    mode = plot_state["mode"]
    if mode == "timeseries":
        kpi_name = plot_state.get("y_kpi") or entries[0]["kpi_name"]
        try:
            body = await client.tool_results_timeseries(
                tool_alias,
                kpi_name=kpi_name,
                version_id=state.get("version_id"),
                completed_from=state.get("completed_from"),
                completed_to=state.get("completed_to"),
            )
        except ApiError:
            body = {"points": []}
        xs, ys, _ids = build_timeseries_data(list(body.get("points", [])))
        fig = go.Figure(data=[go.Scatter(x=xs, y=ys, mode="lines+markers", name=kpi_name)])
        fig.update_layout(
            xaxis_title="Extracted at",
            yaxis_title=axis_label(entries, kpi_name),
            margin={"l": 40, "r": 20, "t": 20, "b": 40},
        )
        return fig

    # Scatter mode.
    x_kpi = plot_state.get("x_kpi") or entries[0]["kpi_name"]
    y_kpi = plot_state.get("y_kpi") or entries[-1]["kpi_name"]
    xs, ys, ids = build_scatter_data(rows, x_kpi=x_kpi, y_kpi=y_kpi)
    fig = go.Figure(
        data=[
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers",
                text=[f"Job {i}" for i in ids],
                name="Jobs",
            )
        ]
    )
    fig.update_layout(
        xaxis_title=axis_label(entries, x_kpi),
        yaxis_title=axis_label(entries, y_kpi),
        margin={"l": 40, "r": 20, "t": 20, "b": 40},
    )
    return fig
