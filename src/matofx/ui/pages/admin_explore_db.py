"""Admin → Explore Database page (``/ui/admin/explore-db``).

Three tabs:

1. **Diagnostics** — default tab. Curated operational cards: failed
   jobs in the last 7 days, current runner queue depth, recently
   active users, orphaned working directories. Each card has its own
   refresh icon.
2. **Tables** — fully functional. Pick an allowed table, build a
   nested AND/OR filter (each condition matches a column against a
   value-picker — for low-cardinality columns — or free text), sort by
   any column, page through results. Row click on the ``job`` table
   reveals the inline stdout/stderr log viewer (legacy parity).
3. **User activity** — fully functional. Pick a user; the page shows
   their identity card, owned projects (with scenario counts),
   scenarios they created, jobs they submitted, workflows they own or
   have run (admin-released and custom), the parametric studies,
   optimisations, surrogate models and job cohorts they created (each
   shown only when its feature is enabled), audit-trail entries whose
   actor was the user, and the logon-grant / revoke history.

The page is gated by login + ``database.explore``. Without the
permission, the API returns 403 and the page surfaces a friendly
error rather than a partial UI.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Awaitable, Callable
from typing import Any

from nicegui import ui

from matofx.ui._features import (
    optimisation_framework_enabled,
    parametric_studies_enabled,
    surrogate_family_enabled,
)
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import format_role_label, render_shell, require_logged_in
from matofx.ui.state import current_token

_DEFAULT_PAGE_SIZE = 100

_PAGE_SIZE_OPTIONS = [25, 50, 100, 250]
"""Selectable page sizes for the Tables results view."""

_ROWS_PER_PAGE_PROP = "[" + ",".join(str(n) for n in _PAGE_SIZE_OPTIONS) + "]"
"""``_PAGE_SIZE_OPTIONS`` as a space-free JS array literal for the q-table
``:rows-per-page-options`` binding (NiceGUI props split on spaces)."""

_STICKY_TABLE_CSS = """
<style>
/* Confine the scroll to Quasar's body wrapper so the header row sticks to
   the top of the scroll area and the native pagination footer stays pinned
   below it (rather than scrolling away with the rows). */
.matofx-sticky-table .q-table__middle {
  max-height: 540px;
  overflow: auto;
}
.matofx-sticky-table thead tr th {
  position: sticky;
  top: 0;
  z-index: 2;
  background-color: var(--matofx-card-bg, #ffffff);
}
</style>
"""
"""Pins the results table header row while the body scrolls. Injected once
when the Tables tab mounts (q-table has no built-in sticky-header prop)."""


@ui.page("/admin/explore-db")
async def admin_explore_db_page() -> None:
    """Render the three-tab explorer."""
    if not require_logged_in():
        return
    render_shell()

    with ui.column().classes("w-full p-6 gap-4"):
        ui.label("Explore database").style("font-size: 1.5rem; font-weight: 600;")
        ui.label(
            "Read-only browser over the install's data. Sensitive columns "
            "(passwords, secrets, audit-chain bytes) are redacted; some "
            "internal tables are hidden entirely."
        ).style("color: #888;")

        # Tabs in the Compare-page idiom: left-aligned, sentence-case
        # (``no-caps`` defeats Quasar's ALL-CAPS), teal label + indicator
        # when active, and a hairline under the strip. The panels are forced
        # transparent so the page background shows through rather than
        # Quasar's own surface tint.
        with (
            ui.tabs()
            .classes("w-full")
            .props("align=left active-color=primary indicator-color=primary")
            .style("border-bottom: 1px solid var(--matofx-card-border);") as tabs
        ):
            tab_diag = ui.tab("Diagnostics", icon="dashboard").props("no-caps")
            tab_tables = ui.tab("Tables", icon="storage").props("no-caps")
            tab_user = ui.tab("User activity", icon="person_search").props("no-caps")

        with (
            ui.tab_panels(tabs, value=tab_diag)
            .classes("w-full")
            .style("background-color: transparent;")
        ):
            with ui.tab_panel(tab_diag).style("background-color: transparent;"):
                await _render_diagnostics_tab()
            with ui.tab_panel(tab_tables).style("background-color: transparent;"):
                await _render_tables_tab()
            with ui.tab_panel(tab_user).style("background-color: transparent;"):
                await _render_user_activity_tab()


# ---------- Diagnostics tab -------------------------------------------------


async def _render_diagnostics_tab() -> None:
    """Curated operational cards. Each card refreshes independently."""
    client = ApiClient(get_app(), token=current_token())

    grid = ui.grid(columns=2).classes("w-full gap-4")

    # One slot per card so a refresh redraws only its own contents.
    with grid:
        failed_card = ui.column().classes("w-full")
        queue_card = ui.column().classes("w-full")
        users_card = ui.column().classes("w-full")
        orphans_card = ui.column().classes("w-full")

    async def _refresh_all() -> None:
        try:
            data = await client.get_db_diagnostics()
        except ApiError as exc:
            failed_card.clear()
            queue_card.clear()
            users_card.clear()
            orphans_card.clear()
            with failed_card:
                ui.label(f"Couldn't load diagnostics: {exc.detail}").style("color: #ff6b6b;")
            return
        _render_failed_jobs_card(failed_card, data.get("failed_jobs_7d") or {}, _refresh_all)
        _render_queue_card(queue_card, data.get("queue_depth") or {}, _refresh_all)
        _render_recent_users_card(users_card, data.get("recent_users") or [], _refresh_all)
        _render_orphans_card(orphans_card, data.get("orphaned_working_dirs") or {}, _refresh_all)

    await _refresh_all()


def _diag_card(
    slot: ui.column,
    title: str,
    subtitle: str,
    on_refresh: Any,
) -> ui.column:
    """Render the chrome of a diagnostics card and return its body slot."""
    slot.clear()
    with slot, ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.label(title).style("font-size: 1.05rem; font-weight: 600;")
            ui.element("div").style("flex: 1;")
            ui.button(icon="refresh", on_click=on_refresh).props("flat round dense")
        if subtitle:
            ui.label(subtitle).style("color: #b0b3b8; font-size: 0.85rem;")
        return ui.column().classes("w-full gap-2")


def _render_failed_jobs_card(slot: ui.column, data: dict[str, Any], on_refresh: Any) -> None:
    """Failed-jobs-in-the-last-7-days card."""
    body = _diag_card(
        slot,
        "Failed jobs (last 7 days)",
        "Jobs that ended in the failed state in the past week.",
        on_refresh,
    )
    count = int(data.get("count") or 0)
    examples = list(data.get("examples") or [])
    with body:
        ui.label(f"{count:,}").style("font-size: 1.6rem; font-weight: 600;")
        if not examples:
            ui.label("No failed jobs in the window. Nice and quiet.").style("color: #888;")
            return
        rows = [
            {
                "id": int(e["id"]),
                "id_text": f"{int(e['id']):07d}",
                "name": str(e.get("name") or "—"),
                "tool": str(e.get("tool_alias") or "—"),
                "when": _short_timestamp(e.get("completed_at") or e.get("updated_at")),
            }
            for e in examples
        ]
        columns = [
            {"name": "id", "label": "Job", "field": "id_text", "align": "left"},
            {"name": "name", "label": "Name", "field": "name", "align": "left"},
            {"name": "tool", "label": "Tool", "field": "tool", "align": "left"},
            {"name": "when", "label": "Failed", "field": "when", "align": "left"},
        ]
        table = (
            ui.table(columns=columns, rows=rows, row_key="id")
            .classes("w-full")
            .props("dense flat hide-bottom")
        )
        table.on(
            "row-click",
            lambda e: ui.navigate.to(f"/jobs/{e.args[1]['id']}"),
        )


def _render_queue_card(slot: ui.column, data: dict[str, Any], on_refresh: Any) -> None:
    """Current runner queue depth — queued + running."""
    body = _diag_card(
        slot,
        "Runner queue depth",
        "Live snapshot of jobs the runner is sitting on right now.",
        on_refresh,
    )
    queued = int(data.get("queued") or 0)
    running = int(data.get("running") or 0)
    with body, ui.row().classes("w-full gap-6"):
        with ui.column().classes("gap-0"):
            ui.label("Queued").style("color: #b0b3b8; font-size: 0.85rem;")
            ui.label(f"{queued:,}").style("font-size: 1.6rem; font-weight: 600;")
        with ui.column().classes("gap-0"):
            ui.label("Running").style("color: #b0b3b8; font-size: 0.85rem;")
            ui.label(f"{running:,}").style("font-size: 1.6rem; font-weight: 600;")


def _render_recent_users_card(
    slot: ui.column, users: list[dict[str, Any]], on_refresh: Any
) -> None:
    """Top-N recently-active users by latest audit-event timestamp."""
    body = _diag_card(
        slot,
        "Recently active users",
        "Users with the newest entries in the audit log.",
        on_refresh,
    )
    with body:
        if not users:
            ui.label("No audit activity yet.").style("color: #888;")
            return
        rows = [
            {
                "user_id": int(u["user_id"]),
                "name": str(u.get("full_name") or u.get("username") or "?"),
                "username": str(u.get("username") or "?"),
                "last_active": _short_timestamp(u.get("last_active_at")),
            }
            for u in users
        ]
        columns = [
            {"name": "name", "label": "User", "field": "name", "align": "left"},
            {
                "name": "username",
                "label": "Username",
                "field": "username",
                "align": "left",
            },
            {
                "name": "last_active",
                "label": "Last seen",
                "field": "last_active",
                "align": "left",
            },
        ]
        ui.table(columns=columns, rows=rows, row_key="user_id").classes("w-full").props(
            "dense flat hide-bottom"
        )


def _render_orphans_card(slot: ui.column, data: dict[str, Any], on_refresh: Any) -> None:
    """Orphaned working dirs — best-effort scan of every storage location."""
    body = _diag_card(
        slot,
        "Orphaned working directories",
        "Directories left on the shared mount whose job row has been deleted.",
        on_refresh,
    )
    items = list(data.get("items") or [])
    note = str(data.get("note") or "")
    checked = bool(data.get("checked"))
    with body:
        if not checked:
            ui.label(note or "Couldn't read any storage-location mounts from this host.").style(
                "color: #ffae42;"
            )
            return
        if note:
            ui.label(note).style("color: #ffae42; font-size: 0.85rem;")
        if not items:
            ui.label("No orphaned working directories found.").style("color: #888;")
            return
        rows = [
            {
                "id": idx,
                "location": str(item.get("storage_location_name") or "—"),
                "path": str(item.get("relative_path") or "—"),
            }
            for idx, item in enumerate(items)
        ]
        columns = [
            {"name": "location", "label": "Storage", "field": "location", "align": "left"},
            {"name": "path", "label": "Path", "field": "path", "align": "left"},
        ]
        ui.table(columns=columns, rows=rows, row_key="id").classes("w-full").props(
            "dense flat hide-bottom"
        )


# ---------- Tables tab ------------------------------------------------------


async def _render_tables_tab() -> None:
    """Picker + filter + sort + paginated table view, with inline job logs."""
    client = ApiClient(get_app(), token=current_token())
    # Sticky table headers, injected once here — the tab panel builds a
    # single time on page load, so this never duplicates per results render.
    ui.add_head_html(_STICKY_TABLE_CSS)
    try:
        tables = await client.list_db_tables()
    except ApiError as exc:
        ui.label(f"Couldn't load the table list: {exc.detail}").style("color: #ff6b6b;")
        return

    if not tables:
        ui.label("No tables to explore.").style("color: #888;")
        return

    state: dict[str, Any] = {
        "selected_table": None,
        "selected_table_meta": None,
        "page": 1,
        "page_size": _DEFAULT_PAGE_SIZE,
        "sort_by": None,
        "sort_order": "desc",
        # The draft filter tree being edited in the builder, the pruned
        # tree last applied (sent to the API), and a per-column
        # distinct-values cache so re-rendering the builder doesn't
        # re-hit the API for columns already looked up.
        "filter_tree": _empty_group(),
        "applied_filter": None,
        "distinct_cache": {},
        # Live slots stashed so module-level handlers (table pick, chip
        # removal) can redraw the picker highlight / filter builder without
        # threading the elements through every signature.
        "builder_slot": None,
        "filter_expansion": None,
        "rebuild_picker": None,
    }

    with ui.row().classes("w-full items-start gap-4 no-wrap"):
        with ui.column().classes("gap-2").style("flex: 0 0 260px;"):
            ui.label("Tables").style("font-weight: 600;")
            search = (
                ui.input(placeholder="Filter tables…")
                .props("dense outlined clearable")
                .classes("w-full")
            )
            # Scroll the rail rather than letting a long table list push the
            # rest of the page down; framed so it reads as a contained list.
            picker_container = (
                ui.column()
                .classes("w-full gap-1")
                .style(
                    "max-height: 70vh; overflow-y: auto; "
                    "border: 1px solid var(--matofx-card-border); "
                    "border-radius: 8px; padding: 0.25rem;"
                )
            )
        body = ui.column().classes("gap-3").style("flex: 1 1 0; min-width: 0;")

    def _build_picker() -> None:
        query = (search.value or "").strip().lower()
        picker_container.clear()
        shown = [e for e in tables if query in str(e["name"]).lower()]
        with picker_container:
            if not shown:
                ui.label("No tables match your search.").style(
                    "color: #888; font-size: 0.85rem; padding: 0.5rem 0.4rem;"
                )
            for entry in shown:
                _render_picker_row(entry, state, body, client)

    # Stash so picking a table can redraw the rail (moving the highlight).
    state["rebuild_picker"] = _build_picker
    search.on_value_change(lambda _e: _build_picker())
    _build_picker()

    with body:
        ui.label("Pick a table on the left to start exploring.").style("color: #888;")


def _render_picker_row(
    entry: dict[str, Any],
    state: dict[str, Any],
    body: ui.column,
    client: ApiClient,
) -> None:
    """One clickable row in the left-rail table picker."""
    name = str(entry["name"])
    row_count = int(entry.get("row_count") or 0)
    is_selected = state["selected_table"] == name
    bg = "rgba(64,161,164,0.18)" if is_selected else "transparent"
    with (
        ui.row()
        .classes("items-baseline justify-between cursor-pointer rounded w-full")
        .style(f"padding: 0.35rem 0.6rem; background-color: {bg};")
        .on(
            "click",
            lambda _e=None, _n=name, _meta=entry: _on_table_picked(_n, _meta, state, body, client),
        )
    ):
        ui.label(name).style("font-size: 0.9rem;")
        ui.label(f"{row_count:,}").style("color: #888; font-size: 0.8rem;")


async def _on_table_picked(
    name: str,
    meta: dict[str, Any],
    state: dict[str, Any],
    body: ui.column,
    client: ApiClient,
) -> None:
    """Reset the explorer state for the new table and re-render."""
    state["selected_table"] = name
    state["selected_table_meta"] = meta
    state["page"] = 1
    state["sort_by"] = None
    # Default to descending — newest IDs first, friendlier than 1, 2, 3...
    state["sort_order"] = "desc"
    state["filter_tree"] = _empty_group()
    state["applied_filter"] = None
    state["distinct_cache"] = {}
    # Redraw the rail so the active row's highlight moves to this table.
    rebuild = state.get("rebuild_picker")
    if callable(rebuild):
        rebuild()
    await _render_table_view(state, body, client)


async def _render_table_view(state: dict[str, Any], body: ui.column, client: ApiClient) -> None:
    """Re-render the right-side body for the currently selected table.

    Lays out the header, then a *builder slot* (the nested filter
    builder + sort controls) and a *results slot* (the data table +
    pagination). The two slots re-render independently: editing the
    builder rebuilds only the builder, while Apply / Sort / paging
    re-fetches only the results.
    """
    table_name = state["selected_table"]
    meta = state["selected_table_meta"] or {}
    body.clear()
    with body:
        ui.label(f"{table_name} · {int(meta.get('row_count') or 0):,} rows").style(
            "font-size: 1.1rem; font-weight: 600;"
        )
        if meta.get("redacted_columns"):
            ui.label(
                f"Redacted columns: {', '.join(meta['redacted_columns'])} — values "
                "shown as '•••' so the plaintext never leaves the database."
            ).style("color: #b0b3b8; font-size: 0.85rem;")
        # The filter lives in a collapsible panel so the data table sits near
        # the top of the body. It opens by default on a fresh table and
        # collapses itself once a filter is applied — the active conditions
        # then read as removable chips in the results toolbar. (Sorting is on
        # the result table's own headers, not here.)
        filter_expansion = (
            ui.expansion("Filter", icon="filter_list", value=True).classes("w-full").props("dense")
        )
        with filter_expansion:
            builder_slot = ui.column().classes("w-full gap-2")
        results_slot = ui.column().classes("w-full gap-3")
    state["builder_slot"] = builder_slot
    state["filter_expansion"] = filter_expansion
    await _render_filter_builder(state, builder_slot, results_slot, client)
    await _render_results(state, results_slot, client)


# ---------- filter builder (recursive AND/OR tree) --------------------------


def _empty_group() -> dict[str, Any]:
    """A fresh, empty AND group — the root of a cleared filter."""
    return {"op": "and", "children": []}


def _empty_leaf() -> dict[str, Any]:
    """A fresh, blank leaf condition (no column picked yet)."""
    return {"column": None, "match": "values", "values": [], "include_null": False, "text": ""}


async def _render_filter_builder(
    state: dict[str, Any],
    builder_slot: ui.column,
    results_slot: ui.column,
    client: ApiClient,
) -> None:
    """(Re)render the recursive filter builder into ``builder_slot``.

    Structural edits (add / remove a node, change a column or match
    mode) call ``_rebuild`` to redraw the builder from the in-memory
    tree. Value edits bind straight into the tree dicts, so they need no
    redraw. Only ``Apply`` prunes the tree and reloads the results.
    """
    builder_slot.clear()
    meta = state["selected_table_meta"] or {}
    columns = list(meta.get("columns") or [])

    async def _rebuild() -> None:
        await _render_filter_builder(state, builder_slot, results_slot, client)

    async def _apply() -> None:
        state["applied_filter"] = _prune_tree(state["filter_tree"])
        state["page"] = 1
        # Collapse the panel once applied so the results lead; the active
        # conditions stay visible as chips in the results toolbar.
        expansion = state.get("filter_expansion")
        if expansion is not None and state["applied_filter"] is not None:
            expansion.value = False
        await _render_results(state, results_slot, client)

    async def _clear() -> None:
        state["filter_tree"] = _empty_group()
        state["applied_filter"] = None
        state["page"] = 1
        expansion = state.get("filter_expansion")
        if expansion is not None:
            expansion.value = True
        await _rebuild()
        await _render_results(state, results_slot, client)

    with builder_slot:
        await _render_group_node(
            state["filter_tree"], None, 0, True, state, columns, client, _rebuild
        )
        with ui.row().classes("items-center gap-2"):
            ui.button("Apply filter", on_click=_apply).props("color=primary dense")
            ui.button("Clear", on_click=_clear).props("flat dense")
        ui.label("Sort by clicking a column header in the results below.").style(
            "color: #b0b3b8; font-size: 0.8rem;"
        )


async def _render_group_node(
    node: dict[str, Any],
    parent_children: list[dict[str, Any]] | None,
    index: int,
    is_root: bool,
    state: dict[str, Any],
    columns: list[str],
    client: ApiClient,
    rebuild: Callable[[], Awaitable[None]],
) -> None:
    """Render one group node and its children recursively.

    ``parent_children`` / ``index`` locate this group inside its parent
    so the remove button can splice it out; both are ignored for the
    root group (which has no remove control).
    """
    border = "" if is_root else "border-left: 3px solid #40A1A4;"
    with ui.card().classes("w-full").style(f"padding: 0.6rem; gap: 0.5rem; {border}"):
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label("Match").style("font-size: 0.85rem;")
            ui.toggle({"and": "ALL", "or": "ANY"}, value=node.get("op", "and")).props(
                "dense no-caps"
            ).bind_value(node, "op")
            ui.label("of the following:").style("font-size: 0.85rem;")
            ui.element("div").style("flex: 1;")
            if not is_root and parent_children is not None:
                pc = parent_children
                idx = index

                async def _remove_group() -> None:
                    pc.pop(idx)
                    await rebuild()

                ui.button(icon="close", on_click=_remove_group).props("flat round dense")

        children: list[dict[str, Any]] = node["children"]
        for i, child in enumerate(children):
            if "op" in child:
                await _render_group_node(child, children, i, False, state, columns, client, rebuild)
            else:
                await _render_leaf_node(child, children, i, state, columns, client, rebuild)

        with ui.row().classes("items-center gap-2"):

            async def _add_condition() -> None:
                node["children"].append(_empty_leaf())
                await rebuild()

            async def _add_group() -> None:
                node["children"].append(_empty_group())
                await rebuild()

            ui.button("+ Condition", on_click=_add_condition).props("flat dense")
            ui.button("+ Group", on_click=_add_group).props("flat dense")


async def _render_leaf_node(
    node: dict[str, Any],
    parent_children: list[dict[str, Any]],
    index: int,
    state: dict[str, Any],
    columns: list[str],
    client: ApiClient,
    rebuild: Callable[[], Awaitable[None]],
) -> None:
    """Render one leaf condition: a column picker + its value editor."""
    with ui.row().classes("items-center gap-2 no-wrap w-full"):
        col_select = (
            ui.select(options=columns, value=node.get("column"), label="Column", with_input=True)
            .props("dense")
            .classes("w-44")
        )
        editor_slot = ui.row().classes("items-center gap-2 no-wrap")

        async def _on_col_change() -> None:
            # Switching column invalidates whatever value/mode was set.
            node["column"] = col_select.value
            node["values"] = []
            node["text"] = ""
            node["include_null"] = False
            await _render_leaf_editor(node, editor_slot, state, client)

        col_select.on_value_change(_on_col_change)

        ui.element("div").style("flex: 1;")

        async def _remove_leaf() -> None:
            parent_children.pop(index)
            await rebuild()

        ui.button(icon="close", on_click=_remove_leaf).props("flat round dense")

    if node.get("column"):
        await _render_leaf_editor(node, editor_slot, state, client)


async def _render_leaf_editor(
    node: dict[str, Any],
    editor_slot: ui.row,
    state: dict[str, Any],
    client: ApiClient,
) -> None:
    """Render the value editor for a leaf based on its column's cardinality.

    Columns with fewer than the explorer's distinct-value limit get a
    Values/Text mode toggle (Values = a multiselect of the distinct
    values, OR-combined, plus an "(empty)" choice when the column has
    NULLs). Higher-cardinality columns are forced to free-text search.
    """
    editor_slot.clear()
    column = node.get("column")
    with editor_slot:
        if not column:
            ui.label("— pick a column —").style("color: #888; font-size: 0.85rem;")
            return
        distinct = await _get_distinct(state, client, str(column))
        if distinct.get("too_many"):
            node["match"] = "text"
            _render_text_value(node)
            ui.label(f"{int(distinct.get('limit') or 50)}+ distinct — text search only").style(
                "color: #b0b3b8; font-size: 0.8rem;"
            )
            return
        if node.get("match") not in ("values", "text"):
            node["match"] = "values"
        mode = ui.toggle({"values": "Values", "text": "Text"}, value=node["match"]).props(
            "dense no-caps"
        )
        sub = ui.row().classes("items-center gap-2 no-wrap")

        def _render_sub() -> None:
            sub.clear()
            with sub:
                if node["match"] == "values":
                    _render_values_value(node, distinct)
                else:
                    _render_text_value(node)

        def _on_mode() -> None:
            node["match"] = mode.value or "values"
            _render_sub()

        mode.on_value_change(_on_mode)
        _render_sub()


def _render_values_value(node: dict[str, Any], distinct: dict[str, Any]) -> None:
    """Multiselect of the column's distinct values (+ optional "(empty)")."""
    options = list(distinct.get("values") or [])
    ui.select(
        options=options,
        multiple=True,
        value=list(node.get("values") or []),
        label="is any of",
    ).props("dense use-chips").style("min-width: 14rem;").bind_value(node, "values")
    if distinct.get("has_null"):
        ui.checkbox("(empty)", value=bool(node.get("include_null"))).bind_value(
            node, "include_null"
        )


def _render_text_value(node: dict[str, Any]) -> None:
    """Free-text substring input bound to the leaf's ``text``."""
    ui.input(value=node.get("text") or "", placeholder="contains…").props("dense").classes(
        "w-56"
    ).bind_value(node, "text")


async def _get_distinct(state: dict[str, Any], client: ApiClient, column: str) -> dict[str, Any]:
    """Distinct-value summary for ``column``, cached per table for the session."""
    cache: dict[str, dict[str, Any]] = state["distinct_cache"]
    if column not in cache:
        try:
            cache[column] = await client.get_column_distinct_values(state["selected_table"], column)
        except ApiError:
            # Treat a lookup failure as "too many" so the column still
            # works via free-text search rather than breaking the row.
            cache[column] = {"too_many": True, "values": [], "has_null": False, "limit": 50}
    return cache[column]


def _prune_tree(node: dict[str, Any]) -> dict[str, Any] | None:
    """Drop blank leaves / empty groups, returning a clean tree or ``None``.

    Half-built rows (no column picked, empty value set, blank text) are
    omitted so a draft in progress doesn't make the API reject the whole
    request — Apply just filters by whatever conditions are complete.
    """
    if "op" in node:
        kids: list[dict[str, Any]] = []
        for child in node.get("children", []):
            pruned = _prune_tree(child)
            if pruned is not None:
                kids.append(pruned)
        if not kids:
            return None
        return {"op": node.get("op", "and"), "children": kids}

    column = node.get("column")
    if not column:
        return None
    if node.get("match") == "values":
        values = list(node.get("values") or [])
        include_null = bool(node.get("include_null"))
        if not values and not include_null:
            return None
        return {
            "column": column,
            "match": "values",
            "values": values,
            "include_null": include_null,
        }
    text = (node.get("text") or "").strip()
    if not text:
        return None
    return {"column": column, "match": "text", "text": text}


def _describe_leaf(node: dict[str, Any]) -> str:
    """Compact human-readable label for one leaf condition (used on chips)."""
    column = node.get("column")
    if node.get("match") == "values":
        vals = [str(v) for v in (node.get("values") or [])]
        if node.get("include_null"):
            vals.append("(empty)")
        return f"{column} ∈ {{{', '.join(vals)}}}"
    return f'{column} ~ "{node.get("text", "")}"'


def _leaf_chips(node: dict[str, Any] | None) -> list[tuple[list[int], str]]:
    """Flatten an applied filter tree into ``(path, description)`` chip specs.

    ``path`` is the list of child indices locating the leaf from the root,
    so a chip's ✕ can splice exactly that condition out via
    :func:`_remove_leaf_at_path`. The AND/OR nesting is dropped for the chip
    strip (the builder still shows the full structure) — chips are a quick
    "what's active" read of the individual conditions.
    """
    chips: list[tuple[list[int], str]] = []

    def _walk(n: dict[str, Any], path: list[int]) -> None:
        if "op" in n:
            for i, child in enumerate(n.get("children", [])):
                _walk(child, [*path, i])
        else:
            chips.append((path, _describe_leaf(n)))

    if node is not None:
        _walk(node, [])
    return chips


def _remove_leaf_at_path(node: dict[str, Any], path: list[int]) -> dict[str, Any] | None:
    """Return a copy of ``node`` with the leaf at ``path`` removed.

    Empty groups left behind by the removal are pruned away; if every
    condition is gone the result is ``None`` (no filter).
    """
    if not path:
        return None
    idx, rest = path[0], path[1:]
    children = list(node.get("children", []))
    if idx >= len(children):
        return node
    if not rest:
        children.pop(idx)
    else:
        replaced = _remove_leaf_at_path(children[idx], rest)
        if replaced is None:
            children.pop(idx)
        else:
            children[idx] = replaced
    if not children:
        return None
    return {"op": node.get("op", "and"), "children": children}


def _applied_to_draft(node: dict[str, Any] | None) -> dict[str, Any]:
    """Rebuild an editable draft tree from a pruned applied filter.

    Keeps the builder in step after a chip removal: applied-filter leaves
    carry only the fields they need, so each is re-expanded to the full
    leaf shape :func:`_empty_leaf` produces.
    """
    if node is None:
        return _empty_group()
    if "op" in node:
        return {
            "op": node.get("op", "and"),
            "children": [_applied_to_draft(c) for c in node.get("children", [])],
        }
    leaf = _empty_leaf()
    leaf["column"] = node.get("column")
    leaf["match"] = node.get("match", "values")
    if leaf["match"] == "values":
        leaf["values"] = list(node.get("values") or [])
        leaf["include_null"] = bool(node.get("include_null"))
    else:
        leaf["text"] = node.get("text", "")
    return leaf


def _render_results_toolbar(
    state: dict[str, Any], results_slot: ui.column, client: ApiClient
) -> None:
    """Strip above the results: the active-filter chips.

    Each chip is one applied condition; its ✕ drops just that condition,
    re-applies the rest and reloads — no need to reopen the builder. Sorting
    and page size live on the results table itself (sortable headers + the
    native footer), so they aren't duplicated here.
    """
    applied = state.get("applied_filter")
    chips = _leaf_chips(applied)
    with ui.row().classes("w-full items-center gap-2").style("flex-wrap: wrap;"):
        if not chips:
            ui.label("No filter applied.").style("color: #b0b3b8; font-size: 0.85rem;")
        else:
            ui.label("Active filter:").style("color: #b0b3b8; font-size: 0.85rem;")
            for path, desc in chips:
                _render_filter_chip(state, results_slot, client, path, desc)


def _render_filter_chip(
    state: dict[str, Any],
    results_slot: ui.column,
    client: ApiClient,
    path: list[int],
    desc: str,
) -> None:
    """One removable chip for an applied filter condition at ``path``."""
    chip = ui.chip(desc, icon="filter_alt", removable=True).props(
        "dense color=primary text-color=white"
    )

    async def _remove(_e: Any = None) -> None:
        applied = state.get("applied_filter")
        new_applied = _remove_leaf_at_path(applied, path) if applied else None
        state["applied_filter"] = new_applied
        # Keep the draft builder in step so reopening it shows the same set.
        state["filter_tree"] = _applied_to_draft(new_applied)
        state["page"] = 1
        builder_slot = state.get("builder_slot")
        if builder_slot is not None:
            await _render_filter_builder(state, builder_slot, results_slot, client)
        await _render_results(state, results_slot, client)

    chip.on("remove", _remove)


async def _render_results(
    state: dict[str, Any], results_slot: ui.column, client: ApiClient
) -> None:
    """Fetch the current page with the applied filter and render it."""
    results_slot.clear()
    table_name = state["selected_table"]
    applied = state.get("applied_filter")
    with results_slot:
        _render_results_toolbar(state, results_slot, client)
        try:
            page_data = await client.read_db_table(
                table_name,
                page=state["page"],
                page_size=state["page_size"],
                sort_by=state["sort_by"],
                sort_order=state["sort_order"],
                filters=applied,
            )
        except ApiError as exc:
            ui.label(f"Couldn't load rows: {exc.detail}").style("color: #ff6b6b;")
            return
        _render_data_table(state, table_name, page_data, results_slot, client)


def _render_data_table(
    state: dict[str, Any],
    table_name: str,
    page_data: dict[str, Any],
    container: ui.column,
    client: ApiClient,
) -> None:
    """The results ``ui.table`` for the current page, in server-side mode.

    Columns are click-to-sort, and the native footer drives pagination /
    rows-per-page. Because the table is in Quasar's *server-side* mode
    (``rowsNumber`` set), a header click or page change emits the ``request``
    event instead of reordering only the rows on screen — we refetch the
    matching slice so the sort applies across the whole filtered table.
    """
    columns = [
        {"name": c, "label": c, "field": c, "align": "left", "sortable": True}
        for c in page_data.get("columns") or []
    ]
    rows = list(page_data.get("rows") or [])
    if not rows:
        ui.label("No rows match the current filter.").style("color: #888;")
        return

    pagination = {
        "sortBy": state["sort_by"],
        "descending": state["sort_order"] == "desc",
        "page": int(page_data.get("page") or 1),
        "rowsPerPage": int(page_data.get("page_size") or state["page_size"]),
        # Total row count flips q-table into server-side mode, so sort / page
        # changes ask us to fetch rather than reslicing the loaded page.
        "rowsNumber": int(page_data.get("total_rows") or 0),
    }
    table = (
        ui.table(columns=columns, rows=rows, pagination=pagination)
        .classes("w-full matofx-sticky-table")
        .props(f"dense flat :rows-per-page-options={_ROWS_PER_PAGE_PROP}")
    )

    async def _on_request(event: Any) -> None:
        pg = event.args.get("pagination", {}) if isinstance(event.args, dict) else {}
        state["sort_by"] = pg.get("sortBy") or None
        state["sort_order"] = "desc" if pg.get("descending") else "asc"
        state["page"] = int(pg.get("page") or 1)
        # Quasar sends 0 for the "All" choice; we never load unbounded, so
        # keep the prior size in that case.
        size = int(pg.get("rowsPerPage") or 0)
        state["page_size"] = size if size > 0 else state["page_size"]
        await _render_results(state, container, client)

    table.on("request", _on_request)

    if table_name == "job":
        # Row click → fetch + render the launcher's stdout/stderr below.
        table.on(
            "row-click",
            lambda e: _show_job_log(e.args[1], container, client),
        )


async def _show_job_log(row: dict[str, Any], container: ui.column, client: ApiClient) -> None:
    """Inline log viewer for a clicked ``job`` row — legacy parity."""
    job_id = int(row["id"])
    try:
        logs = await client.get_job_logs(job_id)
    except ApiError as exc:
        ui.notify(f"Couldn't load logs: {exc.detail}", type="negative")
        return
    # Append a log card at the bottom of the results. Each click adds a
    # fresh card without dropping the previously-opened ones — matches
    # the legacy "side container expanding to show more logs" feel.
    with container, ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label(f"Log for Job {job_id:07d}").style("font-size: 1rem; font-weight: 600;")
        with ui.expansion("stdout", icon="article", value=True).classes("w-full"):
            ui.code(logs.get("stdout") or "(no stdout output)").classes("w-full")
        with ui.expansion("stderr", icon="warning").classes("w-full"):
            ui.code(logs.get("stderr") or "(no stderr output)").classes("w-full")
        ui.button(
            "Open job page",
            icon="open_in_new",
            on_click=lambda _e=None, _jid=job_id: ui.navigate.to(f"/jobs/{_jid}"),
        ).props("flat color=primary")


# ---------- User Activity tab ----------------------------------------------


async def _render_user_activity_tab() -> None:
    """User picker + per-user dashboard.

    Sections: identity card, owned projects, scenarios created, jobs
    submitted, workflows owned or run (admin-released and custom),
    parametric studies / optimisations / surrogate models / job cohorts
    created (feature-gated), audit-trail entries, and logon grant/revoke
    history.
    """
    client = ApiClient(get_app(), token=current_token())
    try:
        users = await client.list_users()
    except ApiError as exc:
        ui.label(f"Couldn't load the users list: {exc.detail}").style("color: #ff6b6b;")
        return

    options: dict[int, str] = {
        int(u["id"]): f"{u.get('full_name') or u['username']} ({u['username']})" for u in users
    }
    if not options:
        ui.label("No users to inspect.").style("color: #888;")
        return

    # Picker first so it sits above the activity body it drives —
    # selectors stay at the top of the section they control.
    with ui.row().classes("items-center w-full gap-3"):
        ui.label("Pick a user").style("font-weight: 600;")
        picker = (
            ui.select(options=options, label="User", with_input=True)
            .classes("w-96")
            .props("dense")
            .on_value_change(lambda _e: _on_user_picked())
        )

    body = ui.column().classes("w-full gap-3")

    async def _on_user_picked() -> None:
        user_id = picker.value
        if user_id is None:
            return
        await _render_activity_body(int(user_id), body, client)

    with body:
        ui.label("Pick a user above to see their activity.").style("color: #888;")


async def _render_activity_body(user_id: int, body: ui.column, client: ApiClient) -> None:
    """Fetch + render the per-user sections."""
    body.clear()
    with body:
        try:
            data = await client.get_user_activity(user_id)
        except ApiError as exc:
            ui.label(f"Couldn't load activity: {exc.detail}").style("color: #ff6b6b;")
            return
        # Export the whole activity dashboard — identity details plus every
        # section below — as one sectioned CSV. The data is already in hand,
        # so the build is purely client-side (no extra round trip).
        with ui.row().classes("w-full items-center"):
            ui.element("div").style("flex: 1;")
            subject = data["subject"]
            username = str(subject.get("username") or f"user-{user_id}")
            ui.button(
                "Export CSV",
                icon="download",
                on_click=lambda _e=None, _d=data, _u=username: ui.download.content(
                    _build_user_activity_csv(_d), f"user-activity-{_u}.csv"
                ),
            ).props("outline color=primary no-caps dense")
        _render_subject_card(data["subject"])
        _render_user_projects_section(data["projects"])
        _render_user_scenarios_section(data["scenarios"])
        _render_user_workflows_section(data["workflows"])
        # Feature-gated sections — shown only when the install has the
        # feature on, mirroring the sidebar's gating so a disabled
        # feature doesn't leave a stray empty card on the page.
        if parametric_studies_enabled():
            _render_user_named_records_section(
                "Parametric Studies",
                "This user hasn't created any parametric studies.",
                data["parametric_studies"],
                route_prefix="/studies",
            )
        if optimisation_framework_enabled():
            _render_user_named_records_section(
                "Optimisations",
                "This user hasn't created any optimisations.",
                data["optimisations"],
                route_prefix="/optimisation",
            )
        if surrogate_family_enabled():
            _render_user_named_records_section(
                "Surrogate Models",
                "This user doesn't own any surrogate models.",
                data["surrogate_models"],
                route_prefix="/models",
            )
            _render_user_named_records_section(
                "Job Cohorts",
                "This user hasn't created any job cohorts.",
                data["cohorts"],
                route_prefix="/cohorts",
            )
        _render_user_jobs_section(data["jobs"])
        _render_user_audit_section(data["audit_events"])
        _render_user_logon_events_section(data["logon_events"])


def _build_user_activity_csv(data: dict[str, Any]) -> bytes:
    """Serialise a user-activity payload into one sectioned CSV (UTF-8 bytes).

    The export mirrors what the page renders: an identity block with the
    user's details (name, username, role, approver/active status) followed
    by one labelled block per section — projects, scenarios, workflows, the
    feature-gated record types, jobs, audit trail and logon history. Each
    block is a ``# Section`` marker row, a header row, then its data rows,
    separated by a blank line so the result opens cleanly in a spreadsheet.
    Feature-gated sections are included only when their feature is enabled,
    matching the on-screen gating.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)

    def _section(title: str, header: list[str], rows: list[list[Any]]) -> None:
        writer.writerow([f"# {title}"])
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
        writer.writerow([])

    subject = data["subject"]
    role_label = format_role_label(str(subject.get("role") or "")) or "—"
    _section(
        "User details",
        ["Field", "Value"],
        [
            ["Full name", subject.get("full_name") or ""],
            ["Username", subject.get("username") or ""],
            ["Role", role_label],
            ["Approver", "Yes" if subject.get("is_approver") else "No"],
            ["Status", "Active" if subject.get("is_active") else "Deactivated"],
        ],
    )

    _section(
        "Projects",
        ["Project", "Description", "# Scenarios", "Created"],
        [
            [
                p.get("name") or "",
                p.get("description") or "",
                int(p.get("scenario_count") or 0),
                _short_timestamp(p.get("created_at")),
            ]
            for p in data.get("projects") or []
        ],
    )

    _section(
        "Scenarios",
        ["Scenario", "Project", "Created"],
        [
            [
                s.get("name") or "",
                s.get("project_name") or "",
                _short_timestamp(s.get("created_at")),
            ]
            for s in data.get("scenarios") or []
        ],
    )

    _section(
        "Workflows",
        ["Workflow", "Kind", "Relationship", "# Runs", "Last run", "State"],
        [_workflow_csv_row(w) for w in data.get("workflows") or []],
    )

    # Feature-gated record sections — included only when the install has the
    # feature on, mirroring the on-screen gating.
    if parametric_studies_enabled():
        _section(
            "Parametric Studies",
            ["Name", "State", "Created"],
            _named_record_csv_rows(data.get("parametric_studies") or []),
        )
    if optimisation_framework_enabled():
        _section(
            "Optimisations",
            ["Name", "State", "Created"],
            _named_record_csv_rows(data.get("optimisations") or []),
        )
    if surrogate_family_enabled():
        _section(
            "Surrogate Models",
            ["Name", "State", "Created"],
            _named_record_csv_rows(data.get("surrogate_models") or []),
        )
        _section(
            "Job Cohorts",
            ["Name", "State", "Created"],
            _named_record_csv_rows(data.get("cohorts") or []),
        )

    _section(
        "Jobs",
        ["Job ID", "Name", "Tool", "State", "Created"],
        [
            [
                f"{int(j['id']):07d}",
                j.get("name") or "",
                j.get("tool_alias") or "",
                str(j.get("state") or "").upper(),
                _short_timestamp(j.get("created_at")),
            ]
            for j in data.get("jobs") or []
        ],
    )

    _section(
        "Audit trail",
        ["When", "Action", "Resource", "Outcome", "Reason"],
        [
            [
                _short_timestamp(e.get("timestamp")),
                e.get("action") or "",
                f"{e.get('resource_type', '')} / {e.get('resource_id', '')}",
                e.get("outcome") or "",
                e.get("reason") or "",
            ]
            for e in data.get("audit_events") or []
        ],
    )

    _section(
        "Logon access history",
        ["When", "Event", "Triggered by", "Reason"],
        [
            [
                _short_timestamp(e.get("at")),
                str(e.get("event") or "").upper(),
                (str(e.get("actor_user_id")) if e.get("actor_user_id") is not None else "system"),
                e.get("reason") or "",
            ]
            for e in data.get("logon_events") or []
        ],
    )

    return buf.getvalue().encode("utf-8")


def _workflow_csv_row(w: dict[str, Any]) -> list[Any]:
    """Project one workflow record into its CSV row (matches the on-screen table)."""
    owned = bool(w.get("owned"))
    ran = int(w.get("run_count") or 0) > 0
    if owned and ran:
        relationship = "Owner · Ran"
    elif owned:
        relationship = "Owner"
    else:
        relationship = "Ran"
    name = str(w.get("name") or "")
    if w.get("archived"):
        name = f"{name} (archived)"
    return [
        name,
        str(w.get("kind") or "").capitalize(),
        relationship,
        int(w.get("run_count") or 0),
        _short_timestamp(w.get("last_run_at")) if w.get("last_run_at") else "",
        str(w.get("state") or "").upper(),
    ]


def _named_record_csv_rows(records: list[dict[str, Any]]) -> list[list[Any]]:
    """Name / state / created rows for the generic feature-gated record sections."""
    return [
        [
            r.get("name") or "",
            str(r.get("state") or "").upper(),
            _short_timestamp(r.get("created_at")),
        ]
        for r in records
    ]


def _render_subject_card(subject: dict[str, Any]) -> None:
    """Identity card — the chosen user's headline info."""
    with ui.card().classes("w-full"), ui.row().classes("items-center w-full gap-4"):
        ui.icon("person").style("font-size: 1.6rem; color: #40A1A4;")
        with ui.column().classes("gap-0").style("flex: 1 1 0; min-width: 0;"):
            ui.label(str(subject.get("full_name") or subject.get("username") or "?")).style(
                "font-size: 1.1rem; font-weight: 600;"
            )
            role_label = format_role_label(str(subject.get("role") or "")) or "—"
            ui.label(f"Username: {subject.get('username', '')} · Role: {role_label}").style(
                "color: #b0b3b8; font-size: 0.85rem;"
            )
        if subject.get("is_approver"):
            ui.badge("APPROVER").props("color=info")
        ui.badge("ACTIVE" if subject.get("is_active") else "DEACTIVATED").props(
            "color=positive" if subject.get("is_active") else "color=negative"
        )


def _render_user_projects_section(projects: list[dict[str, Any]]) -> None:
    """Projects owned by the user — table with name + scenario count + created date."""
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label("Projects").style("font-size: 1.05rem; font-weight: 600;")
        if not projects:
            ui.label("This user doesn't own any projects.").style("color: #888;")
            return
        rows = [
            {
                "id": int(p["id"]),
                "name": str(p.get("name") or "—"),
                "description": _truncate(str(p.get("description") or ""), 80),
                "scenarios": int(p.get("scenario_count") or 0),
                "created": _short_timestamp(p.get("created_at")),
            }
            for p in projects
        ]
        columns = [
            {"name": "name", "label": "Project", "field": "name", "align": "left"},
            {
                "name": "description",
                "label": "Description",
                "field": "description",
                "align": "left",
            },
            {
                "name": "scenarios",
                "label": "# Scenarios",
                "field": "scenarios",
                "align": "right",
            },
            {"name": "created", "label": "Created", "field": "created", "align": "left"},
        ]
        table = (
            ui.table(columns=columns, rows=rows, row_key="id")
            .classes("w-full")
            .props("dense flat hide-bottom")
        )
        table.on(
            "row-click",
            lambda e: ui.navigate.to(f"/projects/{e.args[1]['id']}"),
        )


def _render_user_scenarios_section(scenarios: list[dict[str, Any]]) -> None:
    """Scenarios the user created — name + project + created date."""
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label("Scenarios").style("font-size: 1.05rem; font-weight: 600;")
        if not scenarios:
            ui.label("This user hasn't created any scenarios.").style("color: #888;")
            return
        rows = [
            {
                "id": int(s["id"]),
                "name": str(s.get("name") or "—"),
                "project_id": int(s["project_id"]),
                "project_name": str(s.get("project_name") or "—"),
                "created": _short_timestamp(s.get("created_at")),
            }
            for s in scenarios
        ]
        columns = [
            {"name": "name", "label": "Scenario", "field": "name", "align": "left"},
            {
                "name": "project_name",
                "label": "Project",
                "field": "project_name",
                "align": "left",
            },
            {"name": "created", "label": "Created", "field": "created", "align": "left"},
        ]
        table = (
            ui.table(columns=columns, rows=rows, row_key="id")
            .classes("w-full")
            .props("dense flat hide-bottom")
        )
        table.on(
            "row-click",
            lambda e: ui.navigate.to(f"/scenarios/{e.args[1]['id']}"),
        )


def _render_user_workflows_section(workflows: list[dict[str, Any]]) -> None:
    """Workflows the user owns or has run — admin-released and custom.

    One row per workflow showing the display label, its kind, the user's
    relationship to it (owner / ran / both), how many times *this* user
    ran it and when, and its lifecycle state. Clicking a row jumps to
    the management surface for that kind — the admin workflow editor or
    the custom-workflow inspector.
    """
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label("Workflows").style("font-size: 1.05rem; font-weight: 600;")
        if not workflows:
            ui.label("This user doesn't own or run any workflows.").style("color: #888;")
            return
        rows = []
        for w in workflows:
            owned = bool(w.get("owned"))
            ran = int(w.get("run_count") or 0) > 0
            if owned and ran:
                relationship = "Owner · Ran"
            elif owned:
                relationship = "Owner"
            else:
                relationship = "Ran"
            name = str(w.get("name") or "—")
            if w.get("archived"):
                name = f"{name} (archived)"
            rows.append(
                {
                    "id": int(w["id"]),
                    "kind": str(w.get("kind") or "—").capitalize(),
                    "name": name,
                    "relationship": relationship,
                    "runs": int(w.get("run_count") or 0),
                    "last_run": (
                        _short_timestamp(w.get("last_run_at")) if w.get("last_run_at") else "—"
                    ),
                    "state": str(w.get("state") or "—").upper(),
                }
            )
        columns = [
            {"name": "name", "label": "Workflow", "field": "name", "align": "left"},
            {"name": "kind", "label": "Kind", "field": "kind", "align": "left"},
            {
                "name": "relationship",
                "label": "Relationship",
                "field": "relationship",
                "align": "left",
            },
            {"name": "runs", "label": "# Runs", "field": "runs", "align": "right"},
            {"name": "last_run", "label": "Last run", "field": "last_run", "align": "left"},
            {"name": "state", "label": "State", "field": "state", "align": "left"},
        ]
        table = (
            ui.table(columns=columns, rows=rows, row_key="id")
            .classes("w-full")
            .props("dense flat hide-bottom")
        )
        # Admin-released → the workflow editor; custom → the inspector.
        # Neither page deep-links to a single row yet, so this lands the
        # admin on the right management surface for that kind.
        table.on(
            "row-click",
            lambda e: ui.navigate.to(
                "/admin/workflows"
                if str(e.args[1].get("kind", "")).lower() == "admin"
                else "/admin/custom-workflows"
            ),
        )


def _render_user_named_records_section(
    title: str,
    empty_message: str,
    records: list[dict[str, Any]],
    *,
    route_prefix: str,
) -> None:
    """Generic "records the user created" table — name / state / created.

    Reused by the Parametric Studies, Optimisations, Surrogate Models and
    Job Cohorts sections; each record carries ``id`` / ``name`` /
    ``state`` / ``created_at`` and a row-click deep-links to
    ``{route_prefix}/{id}``.
    """
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label(title).style("font-size: 1.05rem; font-weight: 600;")
        if not records:
            ui.label(empty_message).style("color: #888;")
            return
        rows = [
            {
                "id": int(r["id"]),
                "name": str(r.get("name") or "—"),
                "state": str(r.get("state") or "—").upper(),
                "created": _short_timestamp(r.get("created_at")),
            }
            for r in records
        ]
        columns = [
            {"name": "name", "label": "Name", "field": "name", "align": "left"},
            {"name": "state", "label": "State", "field": "state", "align": "left"},
            {"name": "created", "label": "Created", "field": "created", "align": "left"},
        ]
        table = (
            ui.table(columns=columns, rows=rows, row_key="id")
            .classes("w-full")
            .props("dense flat hide-bottom")
        )
        table.on(
            "row-click",
            lambda e, _p=route_prefix: ui.navigate.to(f"{_p}/{e.args[1]['id']}"),
        )


def _render_user_jobs_section(jobs: list[dict[str, Any]]) -> None:
    """Jobs the user launched (created)."""
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label("Jobs").style("font-size: 1.05rem; font-weight: 600;")
        if not jobs:
            ui.label("This user hasn't launched any jobs.").style("color: #888;")
            return
        rows = [
            {
                "id": int(j["id"]),
                "id_text": f"{int(j['id']):07d}",
                "name": str(j.get("name") or "—"),
                "tool": str(j.get("tool_alias") or "—"),
                "state": str(j.get("state") or "—").upper(),
                "created": _short_timestamp(j.get("created_at")),
            }
            for j in jobs
        ]
        columns = [
            {"name": "id", "label": "Job ID", "field": "id_text", "align": "left"},
            {"name": "name", "label": "Name", "field": "name", "align": "left"},
            {"name": "tool", "label": "Tool", "field": "tool", "align": "left"},
            {"name": "state", "label": "State", "field": "state", "align": "left"},
            {"name": "created", "label": "Created", "field": "created", "align": "left"},
        ]
        table = (
            ui.table(columns=columns, rows=rows, row_key="id")
            .classes("w-full")
            .props("dense flat hide-bottom")
        )
        table.on(
            "row-click",
            lambda e: ui.navigate.to(f"/jobs/{e.args[1]['id']}"),
        )


def _render_user_audit_section(events: list[dict[str, Any]]) -> None:
    """Audit events whose actor was this user — the forensics view."""
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label("Audit trail").style("font-size: 1.05rem; font-weight: 600;")
        if not events:
            ui.label("No audit events whose actor matches this user yet.").style("color: #888;")
            return
        rows = [
            {
                "id": int(e["id"]),
                "timestamp": _short_timestamp(e.get("timestamp")),
                "action": str(e.get("action") or "—"),
                "resource": f"{e.get('resource_type', '—')} / {e.get('resource_id', '—')}",
                "outcome": str(e.get("outcome") or "—"),
                "reason": _truncate(str(e.get("reason") or ""), 60),
            }
            for e in events
        ]
        columns = [
            {"name": "timestamp", "label": "When", "field": "timestamp", "align": "left"},
            {"name": "action", "label": "Action", "field": "action", "align": "left"},
            {
                "name": "resource",
                "label": "Resource",
                "field": "resource",
                "align": "left",
            },
            {"name": "outcome", "label": "Outcome", "field": "outcome", "align": "left"},
            {"name": "reason", "label": "Reason", "field": "reason", "align": "left"},
        ]
        ui.table(columns=columns, rows=rows, row_key="id").classes("w-full").props(
            "dense flat hide-bottom"
        )


def _render_user_logon_events_section(events: list[dict[str, Any]]) -> None:
    """Logon-grant / revoke history (deactivate / reactivate cycles)."""
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label("Logon access history").style("font-size: 1.05rem; font-weight: 600;")
        if not events:
            ui.label("No logon-grant or revoke events recorded.").style("color: #888;")
            return
        rows = [
            {
                "id": int(e["id"]),
                "event": str(e.get("event") or "—").upper(),
                "at": _short_timestamp(e.get("at")),
                "actor_user_id": (
                    str(e.get("actor_user_id")) if e.get("actor_user_id") is not None else "system"
                ),
                "reason": _truncate(str(e.get("reason") or ""), 60),
            }
            for e in events
        ]
        columns = [
            {"name": "at", "label": "When", "field": "at", "align": "left"},
            {"name": "event", "label": "Event", "field": "event", "align": "left"},
            {
                "name": "actor_user_id",
                "label": "Triggered by",
                "field": "actor_user_id",
                "align": "left",
            },
            {"name": "reason", "label": "Reason", "field": "reason", "align": "left"},
        ]
        ui.table(columns=columns, rows=rows, row_key="id").classes("w-full").props(
            "dense flat hide-bottom"
        )


# ---------- shared formatting -----------------------------------------------


def _short_timestamp(value: Any) -> str:
    """``YYYY-MM-DD HH:MM`` for ISO 8601 strings; pass-through otherwise."""
    if value is None:
        return ""
    text = str(value)
    if "T" in text and len(text) >= 16:
        return text[:16].replace("T", " ")
    return text


def _truncate(text: str, max_len: int) -> str:
    """Trim long strings so the table column doesn't blow up."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"
