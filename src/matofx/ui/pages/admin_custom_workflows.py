"""``/admin/custom-workflows`` — inspector surface for troubleshooting.

Gated behind ``custom_workflows.inspect``. Holders see every custom
workflow in the system — owned, private, shared, archived — and can:

- View the full row in a detail drawer: members + share list + lineage.
- Archive a row **with a required reason** (audit-logged distinctly
  from owner-driven archives so the trail makes plain who did what).
- Make a copy — forks the row into the inspector's own list so they
  can reproduce a bug or test a fix without touching the original.

Read-only otherwise. Per the design note, custom workflows are never
promoted into the inspector's personal picker; the inspector arrives
here on purpose for troubleshooting and leaves through the same
surface.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token


@ui.page("/admin/custom-workflows")
async def admin_custom_workflows_page() -> None:
    """Render the inspector list + filter bar."""
    if not require_logged_in():
        return
    render_shell()

    with ui.column().classes("w-full max-w-6xl mx-auto p-6 gap-4"):
        ui.label("Custom Workflows — Inspector").style("font-size: 1.5rem; font-weight: 600;")
        ui.label(
            "Every user-created workflow in the system, for "
            "troubleshooting. Owners are unaffected by what you "
            "do here unless you Archive — that's the only mutation "
            "available and it writes an audit trail with your reason."
        ).style("color: var(--matofx-muted-text);")

        client = ApiClient(get_app(), token=current_token())
        await _render_inspector(client)


async def _render_inspector(client: ApiClient) -> None:
    """Fetch reference data + render the filter bar and result list."""
    try:
        share_candidates = await client.list_custom_workflow_share_candidates()
        tools = await client.list_tools()
    except ApiError as exc:
        ui.label(f"We couldn't load the inspector: {exc.detail}").style(
            "color: var(--matofx-error-text);"
        )
        return

    filters: dict[str, Any] = {
        "owner_id": None,
        "contains_tool_alias": None,
        "include_archived": True,
        "search": None,
    }

    owner_options: dict[Any, str] = {None: "(any owner)"}
    for cand in share_candidates:
        owner_options[int(cand["user_id"])] = f"{cand['full_name']} ({cand['username']})"
    tool_options: dict[Any, str] = {None: "(any tool)"}
    for t in tools:
        if t.get("archived_at"):
            continue
        tool_options[str(t["alias"])] = t["name"]

    # Filters render FIRST (top of page) per the durable UI preference
    # — see ARCHITECTURE.md, "Filter and search controls live above the
    # results, never below". The list_container is created *after* the
    # filter card so it lands below it in the rendered DOM.

    async def refresh() -> None:
        try:
            rows = await client.admin_list_custom_workflows(
                owner_id=filters["owner_id"],
                contains_tool_alias=filters["contains_tool_alias"],
                include_archived=filters["include_archived"],
                search=filters["search"],
            )
        except ApiError as exc:
            ui.notify(f"Failed to refresh: {exc.detail}", type="negative")
            return
        list_container.clear()
        with list_container:
            _render_result_list(client, rows, on_changed=refresh)

    with ui.card().classes("w-full"):
        ui.label("Filters").style("font-weight: 600;")

        # Free-text search sits above the structured dropdowns and is
        # additive to them — type a workflow name, an owner, or a tool
        # and it narrows whatever the dropdowns already selected.
        search_input = (
            ui.input(
                label="Search by name, owner, or tool",
                placeholder="e.g. a person's name, a workflow title, or a tool",
            )
            .props("clearable debounce=300")
            .classes("w-full")
        )

        async def _on_search_change(_e: Any = None) -> None:
            filters["search"] = (search_input.value or "").strip() or None
            await refresh()

        search_input.on("update:model-value", _on_search_change)

        with ui.row().classes("gap-3 w-full items-center"):
            owner_select = ui.select(
                options=owner_options,
                value=None,
                label="Owner",
            ).classes("min-w-64")

            async def _on_owner_change(_e: Any = None) -> None:
                filters["owner_id"] = owner_select.value or None
                await refresh()

            owner_select.on("update:model-value", _on_owner_change)

            tool_select = ui.select(
                options=tool_options,
                value=None,
                label="Contains tool",
            ).classes("min-w-64")

            async def _on_tool_change(_e: Any = None) -> None:
                filters["contains_tool_alias"] = tool_select.value or None
                await refresh()

            tool_select.on("update:model-value", _on_tool_change)

            archived_switch = ui.switch("Include archived", value=True)

            async def _on_archived_change(_e: Any = None) -> None:
                filters["include_archived"] = bool(archived_switch.value)
                await refresh()

            archived_switch.on("update:model-value", _on_archived_change)

    list_container = ui.column().classes("w-full gap-3")
    await refresh()
    return  # list_container already rendered into the page body


def _render_result_list(
    client: ApiClient,
    rows: list[dict[str, Any]],
    *,
    on_changed: Any,
) -> None:
    if not rows:
        ui.label("No custom workflows match the current filters.").style(
            "color: var(--matofx-muted-text); padding: 1rem 0;"
        )
        return
    for row in rows:
        _render_inspector_card(client, row, on_changed=on_changed)


def _render_inspector_card(
    client: ApiClient,
    row: dict[str, Any],
    *,
    on_changed: Any,
) -> None:
    archived = row.get("archived_at") is not None
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between w-full"):
            with ui.column().classes("gap-1"):
                ui.label(row.get("name") or "(unnamed)").style(
                    "font-size: 1.1rem; font-weight: 600;"
                )
                owner_label = row.get("owner_full_name") or row.get("owner_username") or "?"
                with ui.row().classes("gap-1"):
                    ui.badge(f"Owner: {owner_label}").style(
                        "background-color: #777; color: white; "
                        "padding: 0.15rem 0.5rem; font-size: 0.75rem; "
                        "border-radius: 0.4rem;"
                    )
                    if row.get("visibility") == "shared":
                        ui.badge("Shared").style(
                            "background-color: #777; color: white; "
                            "padding: 0.15rem 0.5rem; font-size: 0.75rem; "
                            "border-radius: 0.4rem;"
                        )
                    if archived:
                        # Amber, not red — red is reserved for the Failed
                        # job status. Archived is an inactive/parked state,
                        # so it reads as a caution rather than alarming.
                        ui.badge("Archived").props("color=amber text-color=black").style(
                            "padding: 0.15rem 0.5rem; font-size: 0.75rem; border-radius: 0.4rem;"
                        )

            with ui.row().classes("gap-2"):
                ui.button(
                    "View details",
                    icon="visibility",
                    on_click=lambda _e=None, _id=int(row["id"]): _open_details_drawer(client, _id),
                ).props("flat color=primary")
                ui.button(
                    "Make a copy",
                    icon="content_copy",
                    on_click=lambda _e=None, _id=int(row["id"]): _do_inspector_copy(
                        client, _id, on_changed
                    ),
                ).props("flat color=primary").tooltip(
                    "Fork this workflow into your own list to reproduce "
                    "a bug or test a fix. Doesn't touch the original."
                )
                if not archived:
                    ui.button(
                        "Archive…",
                        icon="inventory_2",
                        on_click=lambda _e=None, _id=int(row["id"]): _open_inspector_archive_dialog(
                            client, _id, on_changed
                        ),
                    ).props("flat color=warning").tooltip(
                        "Archive on behalf of the owner. Reason is required and audit-logged."
                    )
                else:
                    ui.button(
                        "Release",
                        icon="unarchive",
                        on_click=lambda _e=None, _id=int(row["id"]): _do_inspector_release(
                            client, _id, on_changed
                        ),
                    ).props("flat color=primary").tooltip(
                        "Release this workflow back to active on behalf of the owner. Audit-logged."
                    )

        member_count = int(row.get("member_count") or 0)
        share_count = int(row.get("share_count") or 0)
        member_tools = list(row.get("member_tools") or [])
        with ui.row().classes("gap-2 items-center"):
            ui.icon("widgets").style("color: var(--matofx-muted-text);")
            ui.label(f"{member_count} tool(s)").style(
                "color: var(--matofx-muted-text); font-size: 0.9rem;"
            )
            if share_count:
                ui.icon("share").style("color: var(--matofx-muted-text);")
                ui.label(f"shared with {share_count} user(s)").style(
                    "color: var(--matofx-muted-text); font-size: 0.9rem;"
                )

        # Show *which* tools are chained (names, in run order) right on
        # the card so the inspector can scan the list without opening
        # each detail drawer. Arrows make the run order explicit.
        if member_tools:
            with ui.row().classes("gap-1 items-center flex-wrap"):
                ui.icon("account_tree").style("color: var(--matofx-muted-text);")
                for idx, tool_name in enumerate(member_tools):
                    if idx:
                        ui.icon("arrow_right_alt").style("color: var(--matofx-muted-text);")
                    ui.badge(tool_name).style(
                        "background-color: var(--matofx-surface, #eef2f5); "
                        "color: var(--matofx-text, #07203F); "
                        "padding: 0.15rem 0.5rem; font-size: 0.75rem; "
                        "border-radius: 0.4rem;"
                    )


async def _open_details_drawer(client: ApiClient, workflow_id: int) -> None:
    """Open a read-only dialog showing the full row + members + shares."""
    try:
        full = await client.get_custom_workflow(workflow_id)
    except ApiError as exc:
        ui.notify(f"Couldn't open: {exc.detail}", type="negative")
        return

    with ui.dialog() as dialog, ui.card().style("min-width: 560px;"):
        ui.label(full.get("name") or "(unnamed)").style("font-size: 1.2rem; font-weight: 600;")
        owner = full.get("owner_full_name") or full.get("owner_username")
        ui.label(f"Owner: {owner}").style("color: var(--matofx-muted-text);")
        if full.get("description"):
            ui.label(full["description"]).style("padding: 0.5rem 0;")

        ui.label("Tools (in run order)").style("font-weight: 600; margin-top: 0.75rem;")
        for m in full.get("members") or []:
            with ui.row().classes("items-center gap-2"):
                ui.icon("arrow_right").style("color: var(--matofx-muted-text);")
                ui.label(
                    f"{m.get('position')}. {m.get('tool_name')} "
                    f"({m.get('tool_alias')}) — {m.get('tool_state')}"
                )

        ui.label("Share list").style("font-weight: 600; margin-top: 0.75rem;")
        shares = full.get("shares") or []
        if not shares:
            ui.label("Not shared with anyone.").style("color: var(--matofx-muted-text);")
        for s in shares:
            ui.label(f"{s.get('full_name')} ({s.get('username')}) — since {s.get('shared_at')}")

        copied_from = full.get("copied_from_workflow_id")
        if copied_from:
            ui.label(f"Forked from workflow id={copied_from}").style(
                "color: var(--matofx-muted-text); margin-top: 0.75rem;"
            )

        # Structure preview (ADR-0013) — lazy-loaded so the inspector
        # opens snappily; the graph only fetches when the operator
        # clicks to inspect the wiring.
        from matofx.ui.workflow_graph_view import (  # noqa: PLC0415
            render_workflow_graph_section,
        )

        render_workflow_graph_section(workflow_id=workflow_id)

        if full.get("archived_at"):
            with ui.row().classes("gap-1 items-center").style("margin-top: 0.75rem;"):
                ui.icon("warning").style("color: var(--matofx-error-text);")
                ui.label(f"Archived — {full.get('archive_reason') or '(no reason)'}").style(
                    "color: var(--matofx-error-text);"
                )

        with ui.row().classes("justify-end w-full").style("margin-top: 1rem;"):
            ui.button("Close", on_click=dialog.close).props("flat")
    await dialog


async def _open_inspector_archive_dialog(
    client: ApiClient,
    workflow_id: int,
    on_changed: Any,
) -> None:
    """Force-archive with a required reason."""
    with ui.dialog() as dialog, ui.card().style("min-width: 480px;"):
        ui.label("Archive this workflow on behalf of the owner").style(
            "font-size: 1.05rem; font-weight: 600;"
        )
        ui.label(
            "Reason is required and audit-logged. The owner will see "
            "it next to the archived row in their list."
        ).style("color: var(--matofx-muted-text);")
        reason_input = ui.textarea(
            "Reason (required)",
            placeholder="e.g. tool 'thermal_sim' was retired, contacted owner.",
        ).classes("w-full")
        error_label = ui.label("").style("color: var(--matofx-error-text); min-height: 1.2rem;")
        with ui.row().classes("justify-end gap-2 w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            async def _submit() -> None:
                reason = (reason_input.value or "").strip()
                if not reason:
                    error_label.text = "Please add a short reason."
                    return
                try:
                    await client.archive_custom_workflow(workflow_id, reason=reason)
                except ApiError as exc:
                    error_label.text = exc.detail
                    return
                dialog.submit("ok")

            ui.button("Archive", on_click=_submit).props("color=warning")
    result = await dialog
    if result == "ok":
        ui.notify("Workflow archived.", type="positive")
        await on_changed()


async def _do_inspector_release(
    client: ApiClient,
    workflow_id: int,
    on_changed: Any,
) -> None:
    """Release (un-archive) the row back to active on the owner's behalf."""
    try:
        await client.unarchive_custom_workflow(workflow_id)
    except ApiError as exc:
        ui.notify(f"Couldn't release: {exc.detail}", type="negative")
        return
    ui.notify("Workflow released.", type="positive")
    await on_changed()


async def _do_inspector_copy(
    client: ApiClient,
    workflow_id: int,
    on_changed: Any,
) -> None:
    """Fork the row into the inspector's own list."""
    try:
        result = await client.copy_custom_workflow(workflow_id)
    except ApiError as exc:
        ui.notify(f"Couldn't copy: {exc.detail}", type="negative")
        return
    ui.notify(
        f"Copied to your list as '{result.get('name')}'. Open /my-workflows to edit your copy.",
        type="positive",
    )
    await on_changed()
