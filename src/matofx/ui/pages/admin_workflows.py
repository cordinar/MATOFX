"""Admin: New/Edit workflow (``/ui/admin/workflows``).

List page + inline edit dialog. The form is deliberately tiny:
identity (alias / name / description), teams, and a reorderable list
of member tools. Output→input wiring is **not** edited here — it's
discovered at run time from each tool's descriptor.

A workflow that contains a ``new``-state member tool is admin/su
only regardless of the workflow's own state — surfaced as a chip on
the card so the admin sees it.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.pages.admin_tool_edit import _file_uploader
from matofx.ui.state import current_token

_STATE_LABEL: dict[str, str] = {
    "new": "New",
    "test": "Test",
    "released": "Released",
}
_STATE_COLOR: dict[str, str] = {
    "new": "grey",
    "test": "warning",
    "released": "positive",
}

# Forward state-transition graph: each state's allowed next state.
_NEXT_STATE: dict[str, str | None] = {
    "new": "test",
    "test": "released",
    "released": None,
}

# Backward state-transition graph: each state's rollback target. Rollback
# is always allowed by the server — used here purely to decide which
# button to render on the card.
_PREV_STATE: dict[str, str | None] = {
    "new": None,
    "test": "new",
    "released": "test",
}


@ui.page("/admin/workflows")
async def admin_workflows_page() -> None:
    """Render the workflows management surface."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-1"):
                ui.label("Workflows").style("font-size: 1.5rem; font-weight: 600;")
                ui.label(
                    "A workflow groups two or more tools into a sequence end "
                    "users can run as one. Wiring (which tool's outputs feed "
                    "which tool's inputs) is read straight from each tool's "
                    "descriptor — you only pick the tools and put them in "
                    "order."
                ).style("color: #888; font-size: 0.9rem;")
            ui.button(
                "New workflow",
                icon="schema",
                on_click=lambda: _open_workflow_dialog(client, _reload, existing=None),
            ).props("color=primary")

        list_container = ui.column().classes("w-full gap-3")

        async def _reload() -> None:
            list_container.clear()
            try:
                workflows = await client.list_admin_workflows()
                tools = await client.list_admin_tools()
            except ApiError as exc:
                with list_container:
                    ui.label(f"We couldn't load workflows: {exc.detail}").style(
                        "color: var(--matofx-error-text);"
                    )
                return
            with list_container:
                _render_list(workflows, tools, client, _reload)

        await _reload()


# ---------- listing ------------------------------------------------------


_STATE_FILTERS: tuple[str, ...] = ("All", "New", "Test", "Released", "Archived")
"""Toolbar status filter options. Mirrors the admin_users page shape."""


def _render_list(
    workflows: list[dict[str, Any]],
    tools: list[dict[str, Any]],  # noqa: ARG001 - kept so every row renderer takes the same arguments
    client: ApiClient,
    reload_fn: Any,
) -> None:
    """Search / state / sort toolbar + reactive card list + count."""
    if not workflows:
        ui.label(
            "No workflows defined yet. Click 'New workflow' above to get "
            "started — you'll pick two or more installed tools and put "
            "them in order."
        ).style("color: #888;")
        return

    state: dict[str, Any] = {"search": "", "status": _STATE_FILTERS[0]}

    with ui.row().classes("w-full items-end gap-3"):
        search = (
            ui.input(
                "Search",
                placeholder="By name, alias, or description",
            )
            .classes("flex-1")
            .props("dense clearable")
        )
        status_select = (
            ui.select(
                list(_STATE_FILTERS),
                label="Status",
                value=state["status"],
            )
            .classes("w-44")
            .props("dense")
        )

    count_label = ui.label("").style("color: #888; font-size: 0.85rem;")
    list_container = ui.column().classes("w-full gap-3")

    def _matches(wf: dict[str, Any], needle: str, status_filter: str) -> bool:
        if needle:
            haystack = " ".join(
                [
                    str(wf.get("name") or ""),
                    str(wf.get("alias") or ""),
                    str(wf.get("description") or ""),
                ]
            ).lower()
            if needle not in haystack:
                return False
        if status_filter == "All":
            return True
        if status_filter == "Archived":
            return bool(wf.get("archived_at"))
        # State filter — exclude archived rows from the live-state buckets.
        if wf.get("archived_at"):
            return False
        return str(wf.get("state") or "").lower() == status_filter.lower()

    def _rerender() -> None:
        state["search"] = str(search.value or "").strip().lower()
        state["status"] = str(status_select.value or _STATE_FILTERS[0])
        visible = [w for w in workflows if _matches(w, state["search"], state["status"])]
        suffix = "" if len(workflows) == 1 else "s"
        count_label.text = f"Showing {len(visible):,} of {len(workflows):,} workflow{suffix}"
        list_container.clear()
        with list_container:
            if not visible:
                ui.label(
                    "No workflows match those filters. Try clearing the "
                    "search or switching the status filter."
                ).style("color: #555;")
                return
            # When the status filter is "All", split active vs archived
            # the way the original list did. For specific filters we
            # respect the operator's choice and don't auto-group.
            if state["status"] == "All":
                active = [w for w in visible if not w.get("archived_at")]
                archived = [w for w in visible if w.get("archived_at")]
                for wf in active:
                    _render_workflow_card(wf, tools, client, reload_fn, archived=False)
                if archived:
                    with ui.expansion(
                        f"Archived workflows ({len(archived)})", icon="archive"
                    ).classes("w-full"):
                        for wf in archived:
                            _render_workflow_card(wf, tools, client, reload_fn, archived=True)
            else:
                for wf in visible:
                    _render_workflow_card(
                        wf, tools, client, reload_fn, archived=bool(wf.get("archived_at"))
                    )

    def _on_search_change(_e: Any = None) -> None:
        _rerender()

    # Mirror the admin_tools pattern: bind both events so the clearable
    # X-button reliably triggers a re-render across Quasar versions.
    search.on("update:model-value", _on_search_change)
    search.on("clear", lambda _e=None: _rerender())
    status_select.on("update:model-value", _on_search_change)
    _rerender()


def _render_workflow_card(
    wf: dict[str, Any],
    tools: list[dict[str, Any]],  # noqa: ARG001 - signature kept uniform across the sibling callers
    client: ApiClient,
    reload_fn: Any,
    *,
    archived: bool,
) -> None:
    """One workflow card — identity + state chip + members + actions."""
    alias = str(wf.get("alias") or "")
    name = str(wf.get("name") or alias)
    version = str(wf.get("version") or "")
    state = str(wf.get("state") or "new")
    kind = str(wf.get("kind") or "admin")
    is_custom = kind == "custom"
    members = list(wf.get("members") or [])
    teams = list(wf.get("teams") or [])
    description = str(wf.get("description") or "")
    updated_at = format_user_datetime(wf.get("updated_at"))

    has_new_member = any(str(m.get("tool_state") or "") == "new" for m in members)
    has_archived_member = any(str(m.get("tool_state") or "") == "archived" for m in members)

    next_state = _NEXT_STATE.get(state)
    prev_state = _PREV_STATE.get(state)

    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-start justify-between gap-3 no-wrap"):
            with ui.column().classes("gap-2 flex-1").style("min-width: 0;"):
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.icon("schema").style("font-size: 1.6rem; color: #40A1A4;")
                    with ui.column().classes("gap-0").style("min-width: 0;"):
                        ui.label(name).style("font-size: 1.1rem; font-weight: 600;")
                        ui.label(f"{alias} · v{version}").style("color: #888; font-size: 0.85rem;")
                    _state_chip(state)
                    if archived:
                        ui.chip("Archived", icon="archive").props(
                            "color=grey text-color=white dense"
                        )
                    if has_new_member:
                        ui.chip("Admins only (new-state member)", icon="visibility_off").props(
                            "color=warning text-color=white dense"
                        )
                    if has_archived_member:
                        ui.chip("Member tool archived", icon="warning").props(
                            "color=negative text-color=white dense"
                        )

                if description:
                    ui.label(description).style("color: #aaa; font-size: 0.9rem;")

                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.label("Visible to:").style("color: #888; font-size: 0.85rem;")
                    if wf.get("teams_pending"):
                        ui.chip(
                            "No teams yet (imported) — set teams before promoting",
                            icon="group_off",
                        ).props("color=warning text-color=white dense")
                    elif not teams or "World" in teams:
                        ui.chip("Everyone (World)", icon="public").props("outline dense")
                    else:
                        for team in teams:
                            ui.chip(team, icon="group").props("outline dense")

                # Member sequence
                with ui.row().classes("items-center gap-1 flex-wrap").style("margin-top: 0.25rem;"):
                    ui.label("Tools:").style("color: #888; font-size: 0.85rem;")
                    if not members:
                        ui.label("(none)").style("color: #c0392b; font-size: 0.85rem;")
                    for i, m in enumerate(members):
                        pretty = str(m.get("tool_name") or m.get("tool_alias") or "—")
                        version_label = str(m.get("tool_version") or "")
                        chip_label = f"{pretty} v{version_label}" if version_label else pretty
                        ui.chip(f"{m.get('position')}. {chip_label}", icon="build").props(
                            "outline dense"
                        )
                        if i < len(members) - 1:
                            ui.icon("arrow_forward").style("color: #888;")

                ui.label(f"Last updated {updated_at}").style("color: #888; font-size: 0.8rem;")

            with ui.column().classes("gap-2 items-end"):
                # Custom workflows live in 'released' until archived —
                # they don't ramp through new/test/released, so the
                # promote / rollback controls are hidden for them.
                if not archived and not is_custom and next_state is not None:
                    ui.button(
                        f"Promote to {_STATE_LABEL.get(next_state, next_state.title())}",
                        icon="arrow_upward",
                        on_click=lambda _e=None, _a=alias, _ns=next_state: _do_promote(
                            client, _a, _ns, reload_fn
                        ),
                    ).props("color=primary")
                if not archived and not is_custom and prev_state is not None:
                    ui.button(
                        f"Roll back to {_STATE_LABEL.get(prev_state, prev_state.title())}",
                        icon="arrow_downward",
                        on_click=lambda _e=None, _a=alias, _ps=prev_state: _do_promote(
                            client, _a, _ps, reload_fn
                        ),
                    ).props("flat")
                if not archived:
                    ui.button(
                        "Edit",
                        icon="edit",
                        on_click=lambda _e=None, _a=alias: _open_workflow_dialog(
                            client, reload_fn, existing=_a
                        ),
                    ).props("flat")
                    ui.button(
                        "Archive",
                        icon="archive",
                        on_click=lambda _e=None, _a=alias, _n=name: _confirm_archive(
                            client, _a, _n, reload_fn
                        ),
                    ).props("flat color=warning")
                else:
                    ui.button(
                        "Re-activate",
                        icon="unarchive",
                        on_click=lambda _e=None, _a=alias: _do_unarchive(client, _a, reload_fn),
                    ).props("color=positive")
                ui.button(
                    "View change log",
                    icon="history",
                    on_click=lambda _e=None, _a=alias: _open_version_history(client, _a),
                ).props("flat")


def _state_chip(state: str) -> None:
    label = _STATE_LABEL.get(state, state.title())
    color = _STATE_COLOR.get(state, "grey")
    ui.chip(label).props(f"color={color} text-color=white dense")


# ---------- actions ------------------------------------------------------


async def _do_promote(client: ApiClient, alias: str, new_state: str, reload_fn: Any) -> None:
    try:
        await client.promote_workflow(alias, new_state=new_state)
    except ApiError as exc:
        ui.notify(f"Couldn't change state: {exc.detail}", type="negative")
        return
    ui.notify(f"Workflow now in '{_STATE_LABEL.get(new_state, new_state)}' state.", type="positive")
    await reload_fn()


async def _do_unarchive(client: ApiClient, alias: str, reload_fn: Any) -> None:
    try:
        await client.unarchive_workflow(alias)
    except ApiError as exc:
        ui.notify(f"Couldn't unarchive: {exc.detail}", type="negative")
        return
    ui.notify("Workflow restored.", type="positive")
    await reload_fn()


def _confirm_archive(client: ApiClient, alias: str, name: str, reload_fn: Any) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 440px;"):
        ui.label("Archive workflow?").style("font-size: 1.1rem; font-weight: 600;")
        ui.label(
            f'"{name}" will be hidden from the runs list. Any in-progress '
            "runs still resolve, and you can unarchive it later."
        ).style("color: #aaa;")
        reason_input = (
            ui.textarea(
                "Reason (optional)",
                placeholder="A short note that shows up in the archive history.",
            )
            .classes("w-full")
            .props("dense autogrow")
        )

        async def _confirm() -> None:
            try:
                await client.archive_workflow(alias, reason=str(reason_input.value or ""))
            except ApiError as exc:
                ui.notify(f"Couldn't archive: {exc.detail}", type="negative")
                return
            dialog.close()
            ui.notify("Workflow archived.", type="info")
            await reload_fn()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Archive", on_click=_confirm).props("color=negative")
    dialog.open()


# ---------- create / edit dialog ----------------------------------------


def _open_workflow_dialog(
    client: ApiClient,
    reload_fn: Any,
    *,
    existing: str | None,
) -> None:
    """The shared create + edit dialog.

    ``existing=None`` ⇒ create (alias editable, blank fields).
    ``existing=<alias>`` ⇒ edit (alias read-only, pre-populated).
    """
    state: dict[str, Any] = {
        "tool_aliases": [],  # ordered list of tool aliases
        "available_tools": [],  # list of {alias, name, version, state, archived}
        "all_teams": [],  # ordered list of team names from /admin/teams
        "icon": {
            "bytes": None,
            "filename": None,
            "on_change": None,
            "current": None,  # set from the loaded workflow for existing rows
        },
    }

    with ui.dialog() as dialog, ui.card().style("min-width: 640px; max-width: 90vw;"):
        ui.label("New workflow" if existing is None else f"Edit workflow: {existing}").style(
            "font-size: 1.2rem; font-weight: 600;"
        )

        alias_input = (
            ui.input("Alias", placeholder="lowercase_snake_case").classes("w-full").props("dense")
        )
        if existing is not None:
            alias_input.value = existing
            alias_input.props("readonly")
        name_input = ui.input("Display name *").classes("w-full").props("dense")
        description_input = ui.textarea("Description").classes("w-full").props("dense autogrow")

        teams_select = (
            ui.select(
                options={},
                label="Teams that can see this workflow",
                multiple=True,
                with_input=True,
            )
            .classes("w-full")
            .props("dense use-chips")
        )
        ui.label(
            "Pick 'World' (the default) to make this workflow visible to "
            "everyone. Pick specific teams to restrict it to members of "
            "those teams (plus admins / super users)."
        ).style("color: #888; font-size: 0.85rem;")

        ui.label("Tools, in order").style("font-weight: 600; margin-top: 0.5rem;")
        ui.label(
            "The order shapes the tabs end users see, and which tool's "
            "outputs each downstream tool can pull from."
        ).style("color: #888; font-size: 0.85rem;")

        tools_container = ui.column().classes("w-full gap-1")
        add_row = ui.row().classes("w-full items-center gap-2")
        with add_row:
            tool_picker = (
                ui.select(options={}, label="Add a tool", with_input=True)
                .classes("flex-1")
                .props("dense")
            )

            def _on_add() -> None:
                value = tool_picker.value
                if not value:
                    return
                alias = str(value)
                if alias in state["tool_aliases"]:
                    ui.notify("That tool is already in the workflow.", type="warning")
                    return
                state["tool_aliases"].append(alias)
                tool_picker.value = None
                tool_picker.update()
                _rerender_tools()

            ui.button("Add", icon="add", on_click=_on_add).props("color=primary dense")

        # ---- Structure preview (ADR-0013) -------------------------------
        # On-demand "Preview structure" button that projects the
        # descriptor-inferred wiring for whatever's in ``state[
        # "tool_aliases"]`` at click time. Works on both create and
        # edit — no workflow id required, the preview endpoint is
        # ephemeral. The user clicks again after adding / removing
        # tools to refresh the rendering.
        from matofx.ui.workflow_graph_view import (  # noqa: PLC0415
            render_workflow_graph_preview_button,
        )

        render_workflow_graph_preview_button(
            get_tool_aliases=lambda: list(state["tool_aliases"]),
        )

        # ---- Icon section (only meaningful for existing workflows) ------
        # The icon is only patch-bumped via the dedicated endpoint, so
        # uploads only fire when editing an existing workflow (a new
        # workflow has no alias on disk yet). On create, the admin saves
        # the workflow first, reopens edit, then uploads an icon.
        icon_status_label: ui.label | None = None
        icon_preview_box: ui.column | None = None
        if existing is not None:
            with ui.card().classes("w-full").style("margin-top: 0.5rem;"):
                ui.label("Icon").style("font-weight: 600;")
                with ui.row().classes("w-full items-start gap-3 no-wrap"):
                    icon_preview_box = (
                        ui.column()
                        .classes("items-center justify-center")
                        .style(
                            "width: 120px; height: 120px; flex: 0 0 auto; "
                            "border: 1px dashed var(--matofx-card-border); "
                            "border-radius: 6px; padding: 4px;"
                        )
                    )
                    with icon_preview_box:
                        ui.icon("image_not_supported").style("font-size: 2.6rem; color: #888;")
                    with ui.column().classes("gap-1").style("flex: 1 1 0; min-width: 0;"):
                        icon_status_label = ui.label("Loading current icon…").style(
                            "color: #888; font-size: 0.85rem;"
                        )

                        def _on_icon_pending_change() -> None:
                            if state["icon"]["bytes"] and icon_preview_box is not None:
                                import base64  # noqa: PLC0415

                                b64 = base64.b64encode(bytes(state["icon"]["bytes"])).decode(
                                    "ascii"
                                )
                                icon_preview_box.clear()
                                with icon_preview_box:
                                    ui.image(f"data:image/png;base64,{b64}").style(
                                        "width: 100%; height: 100%; object-fit: contain;"
                                    )
                                if icon_status_label is not None:
                                    icon_status_label.text = (
                                        f"New icon pending — saved when you click "
                                        f"'Save changes' ({state['icon']['filename']})."
                                    )
                                    icon_status_label.style("color: #43a047; font-size: 0.85rem;")

                        state["icon"]["on_change"] = _on_icon_pending_change
                        _file_uploader(
                            label="Icon (PNG)",
                            accept=".png,image/png",
                            helper=(
                                "100x100 recommended. Replaces any existing icon for "
                                "this workflow. Saved together with the rest of the "
                                "form when you click 'Save changes'."
                            ),
                            target=state["icon"],
                        )

        message = ui.label("").style("color: var(--matofx-error-text);")

        def _rerender_tools() -> None:
            tools_container.clear()
            available = {str(t["alias"]): t for t in state["available_tools"]}
            with tools_container:
                if not state["tool_aliases"]:
                    ui.label("No tools yet — add at least one above.").style("color: #888;")
                for idx, alias in enumerate(state["tool_aliases"]):
                    tool = available.get(alias)
                    pretty = f"{tool['name']} v{tool['version']}" if tool else f"{alias} (unknown)"
                    with (
                        ui.row()
                        .classes("w-full items-center gap-1")
                        .style(
                            "border: 1px solid var(--matofx-card-border); "
                            "padding: 0.4rem; border-radius: 4px;"
                        )
                    ):
                        ui.label(f"{idx + 1}.").style("min-width: 1.5rem;")
                        ui.icon("build").style("color: #40A1A4;")
                        ui.label(pretty).style("flex: 1;")
                        if tool and tool.get("state") and tool.get("state") != "released":
                            ui.chip(_STATE_LABEL.get(str(tool["state"]), str(tool["state"]))).props(
                                f"color={_STATE_COLOR.get(str(tool['state']), 'grey')} "
                                "text-color=white dense"
                            )
                        ui.button(
                            icon="arrow_upward",
                            on_click=lambda _e=None, i=idx: _move(state, i, -1, _rerender_tools),
                        ).props("flat dense").bind_enabled_from(
                            state,
                            "tool_aliases",
                            lambda lst, _i=idx: _i > 0,  # type: ignore[misc]  # noqa: ARG005 - bind_enabled_from passes the bound value; only the index matters
                        )
                        ui.button(
                            icon="arrow_downward",
                            on_click=lambda _e=None, i=idx: _move(state, i, 1, _rerender_tools),
                        ).props("flat dense").bind_enabled_from(
                            state,
                            "tool_aliases",
                            lambda lst, _i=idx: _i < len(lst) - 1,  # type: ignore[misc]
                        )
                        ui.button(
                            icon="close",
                            on_click=lambda _e=None, i=idx: _remove(state, i, _rerender_tools),
                        ).props("flat dense color=negative")
                _refresh_picker_options(tool_picker, state)

        async def _load_data() -> None:
            try:
                tools = await client.list_admin_tools()
                team_rows = await client.list_teams()
            except ApiError as exc:
                message.text = f"Couldn't load tools / teams: {exc.detail}"
                return
            # Skip archived tools — they can't be added to a workflow.
            state["available_tools"] = [
                {
                    "alias": str(t["alias"]),
                    "name": str(t.get("name") or t["alias"]),
                    "version": str(t.get("version") or ""),
                    "state": str(t.get("state") or "released"),
                }
                for t in tools
                if not t.get("archived_at")
            ]
            team_names = [str(t["name"]) for t in team_rows]
            if "World" not in team_names:
                team_names.insert(0, "World")
            state["all_teams"] = team_names
            teams_select.options = {n: n for n in team_names}
            teams_select.update()

            if existing is not None:
                try:
                    wf = await client.get_admin_workflow(existing)
                except ApiError as exc:
                    message.text = exc.detail
                    return
                name_input.value = str(wf.get("name") or "")
                description_input.value = str(wf.get("description") or "")
                teams_select.value = list(wf.get("teams") or [])
                if wf.get("teams_pending"):
                    teams_select.props(
                        'error error-message="Imported without teams — choose '
                        'at least one (World for everyone) before promoting."'
                    )
                state["tool_aliases"] = [str(m.get("tool_alias")) for m in wf.get("members") or []]
                current_icon = wf.get("icon_filename")
                state["icon"]["current"] = current_icon
                if icon_status_label is not None:
                    if current_icon:
                        icon_status_label.text = f"Current icon: {current_icon}"
                        icon_status_label.style("color: #888; font-size: 0.85rem;")
                    else:
                        icon_status_label.text = "No icon set yet — upload one below."
                        icon_status_label.style("color: #b58900; font-size: 0.85rem;")
                if current_icon and icon_preview_box is not None:
                    try:
                        result = await client.get_workflow_icon_bytes(existing)
                    except Exception:  # noqa: BLE001
                        result = None
                    if result:
                        import base64  # noqa: PLC0415

                        raw, mime = result
                        b64 = base64.b64encode(raw).decode("ascii")
                        icon_preview_box.clear()
                        with icon_preview_box:
                            ui.image(f"data:{mime};base64,{b64}").style(
                                "width: 100%; height: 100%; object-fit: contain;"
                            )
            else:
                # Default teams = [World] on create, matching tool form.
                teams_select.value = ["World"]
            _rerender_tools()

        async def _submit() -> None:
            message.text = ""
            payload_alias = str(alias_input.value or "").strip()
            if not payload_alias:
                message.text = "Please give the workflow an alias."
                return
            if not (name_input.value or "").strip():
                message.text = "Please give the workflow a display name."
                return
            if not state["tool_aliases"]:
                message.text = "Please add at least one tool."
                return
            teams = [t for t in (teams_select.value or []) if t]
            try:
                if existing is None:
                    await client.create_workflow(
                        alias=payload_alias,
                        name=str(name_input.value),
                        description=str(description_input.value or ""),
                        teams=teams,
                        tool_aliases=list(state["tool_aliases"]),
                    )
                else:
                    await client.update_workflow(
                        existing,
                        name=str(name_input.value),
                        description=str(description_input.value or ""),
                        teams=teams,
                        tool_aliases=list(state["tool_aliases"]),
                    )
                    if state["icon"]["bytes"]:
                        await client.update_workflow_icon(
                            existing,
                            icon_bytes=bytes(state["icon"]["bytes"]),
                            icon_filename=str(state["icon"]["filename"] or f"{existing}.png"),
                        )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify(
                "Workflow created." if existing is None else "Workflow updated.",
                type="positive",
            )
            await reload_fn()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button(
                "Create" if existing is None else "Save changes",
                on_click=_submit,
            ).props("color=primary")

        ui.timer(0.05, _load_data, once=True)
    dialog.open()


def _move(state: dict[str, Any], idx: int, delta: int, rerender: Any) -> None:
    lst = state["tool_aliases"]
    new_idx = idx + delta
    if 0 <= new_idx < len(lst):
        lst[idx], lst[new_idx] = lst[new_idx], lst[idx]
        rerender()


def _remove(state: dict[str, Any], idx: int, rerender: Any) -> None:
    lst = state["tool_aliases"]
    if 0 <= idx < len(lst):
        lst.pop(idx)
        rerender()


def _refresh_picker_options(picker: ui.select, state: dict[str, Any]) -> None:
    """Show only tools not already in the workflow."""
    selected = set(state["tool_aliases"])
    options = {
        t["alias"]: f"{t['name']} v{t['version']}"
        + (f"  ·  {_STATE_LABEL.get(t['state'], t['state'])}" if t["state"] != "released" else "")
        for t in state["available_tools"]
        if t["alias"] not in selected
    }
    picker.options = options
    picker.update()


# ---------- version history --------------------------------------------


async def _open_version_history(client: ApiClient, alias: str) -> None:
    """Open a dialog showing every snapshot of the workflow, newest first."""
    try:
        versions = await client.list_workflow_versions(alias)
    except ApiError as exc:
        ui.notify(f"Couldn't load version history: {exc.detail}", type="negative")
        return

    with ui.dialog() as dialog, ui.card().style("min-width: 640px; max-width: 90vw;"):
        ui.label(f"Version history: {alias}").style("font-size: 1.2rem; font-weight: 600;")
        if not versions:
            ui.label("No snapshots yet.").style("color: #888;")
        for v in versions:
            with ui.expansion(
                f"v{v.get('version')} — {v.get('change_summary') or 'edit'}",
                icon="history",
            ).classes("w-full"):
                ui.label(
                    f"By {v.get('created_by_label') or '—'} on "
                    f"{format_user_datetime(v.get('created_at'))}"
                ).style("color: #888; font-size: 0.85rem;")
                changes = v.get("changes") or {}
                if changes:
                    for field, diff in changes.items():
                        ui.label(f"{field}: {diff.get('old')!r} → {diff.get('new')!r}").style(
                            "font-family: monospace; font-size: 0.85rem;"
                        )
                members = v.get("members_snapshot") or []
                if members:
                    pretty = " → ".join(f"{m.get('tool_alias')}" for m in members)
                    ui.label(f"Tools at this point: {pretty}").style(
                        "color: #aaa; font-size: 0.85rem;"
                    )
        with ui.row().classes("w-full justify-end"):
            ui.button("Close", on_click=dialog.close).props("flat")
    dialog.open()
