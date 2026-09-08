"""``/my-workflows`` — owner-facing surface for custom workflows.

Lists the workflows the caller owns plus workflows shared with them
(badge marks which is which), with per-row actions:

- **Edit** (owner only) — opens a dialog pre-filled with the current
  state; saving calls ``POST /api/workflows/custom/{id}/update``.
- **Archive** (owner) — soft-shelves the row. Admins holding
  ``custom_workflows.inspect`` reach this surface too in a later
  pass; for now the inspector page is a separate route.
- **Make a copy** — any visible row can be forked into the caller's
  own list. Recipients use this to take Alice's workflow and tweak
  it without touching Alice's original.

The **New Custom Workflow** button opens an empty version of the
same form. Both create and edit go through ``CustomWorkflowDraft``
on the service layer, which computes the run order from descriptors
and validates 2–10 unique tools + share-recipient visibility.

Keeps the legacy UI shape per the durable user preference: a card
list on the page body rather than a heavyweight table component.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token, current_username
from matofx.ui.workflow_launch import open_launch_in_existing_project_dialog

_MIN_TOOLS = 2
_MAX_TOOLS = 10


@ui.page("/my-workflows")
async def my_workflows_page() -> None:
    """Render the owner-facing custom-workflow workspace."""
    if not require_logged_in():
        return
    render_shell()

    with ui.column().classes("w-full max-w-6xl mx-auto p-6 gap-4"):
        ui.label("My Custom Workflows").style("font-size: 1.5rem; font-weight: 600;")
        ui.label(
            "Group 2–10 tools you already have access to into a saved "
            "workflow you can re-launch and (optionally) share with "
            "named users."
        ).style("color: var(--matofx-muted-text);")

        client = ApiClient(get_app(), token=current_token())
        await _render_workspace(client)


# ----- workspace --------------------------------------------------------


async def _render_workspace(client: ApiClient) -> None:
    """Fetch tools + workflows + share candidates, then render the list."""
    try:
        tools = await client.list_tools()
        rows = await client.list_my_custom_workflows()
        share_candidates = await client.list_custom_workflow_share_candidates()
    except ApiError as exc:
        ui.label(f"We couldn't load your workflows: {exc.detail}").style(
            "color: var(--matofx-error-text);"
        )
        return

    visible_tools = [t for t in tools if not t.get("archived_at")]
    tool_label_by_alias = {t["alias"]: t["name"] for t in visible_tools}

    show_archived = {"value": False}
    refresh_button: dict[str, Any] = {}

    async def refresh() -> None:
        try:
            refreshed = await client.list_my_custom_workflows(
                include_archived=show_archived["value"]
            )
        except ApiError as exc:
            ui.notify(f"Failed to refresh: {exc.detail}", type="negative")
            return
        list_container.clear()
        with list_container:
            _render_list(
                client,
                refreshed,
                tool_options=visible_tools,
                tool_label_by_alias=tool_label_by_alias,
                share_candidates=share_candidates,
                on_changed=refresh,
            )

    with ui.row().classes("items-center justify-between w-full"):
        ui.button(
            "New Custom Workflow",
            icon="add",
            on_click=lambda: _open_workflow_dialog(
                client,
                workflow_id=None,
                tool_options=visible_tools,
                tool_label_by_alias=tool_label_by_alias,
                share_candidates=share_candidates,
                on_saved=refresh,
            ),
        ).props("color=primary")

        with ui.row().classes("items-center gap-2"):
            archived_toggle = ui.switch("Show archived", value=False)

            async def _on_toggle(_e: Any = None) -> None:
                show_archived["value"] = bool(archived_toggle.value)
                await refresh()

            archived_toggle.on("update:model-value", _on_toggle)

    list_container = ui.column().classes("w-full gap-3")
    with list_container:
        _render_list(
            client,
            rows,
            tool_options=visible_tools,
            tool_label_by_alias=tool_label_by_alias,
            share_candidates=share_candidates,
            on_changed=refresh,
        )

    refresh_button["fn"] = refresh


def _render_list(
    client: ApiClient,
    rows: list[dict[str, Any]],
    *,
    tool_options: list[dict[str, Any]],
    tool_label_by_alias: dict[str, str],
    share_candidates: list[dict[str, Any]],
    on_changed: Any,
) -> None:
    """Render the list of workflow cards or an empty-state message."""
    if not rows:
        ui.label(
            "You don't have any custom workflows yet. Click "
            "'New Custom Workflow' to create your first one."
        ).style("color: var(--matofx-muted-text); padding: 1rem 0;")
        return

    me = current_username() or ""
    for row in rows:
        _render_workflow_card(
            client,
            row,
            me=me,
            tool_options=tool_options,
            tool_label_by_alias=tool_label_by_alias,
            share_candidates=share_candidates,
            on_changed=on_changed,
        )


def _render_workflow_card(
    client: ApiClient,
    row: dict[str, Any],
    *,
    me: str,  # noqa: ARG001 - kept so every row renderer takes the same arguments
    tool_options: list[dict[str, Any]],
    tool_label_by_alias: dict[str, str],
    share_candidates: list[dict[str, Any]],
    on_changed: Any,
) -> None:
    """One row card: identity + members + share list + actions."""
    is_owner = bool(row.get("is_owner"))
    archived = row.get("archived_at") is not None
    wf_alias = str(row["alias"])
    wf_name = str(row.get("name") or "workflow")

    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between w-full"):
            with ui.column().classes("gap-1"):
                ui.label(row.get("name") or "(unnamed)").style(
                    "font-size: 1.1rem; font-weight: 600;"
                )
                _render_badges(row, is_owner=is_owner, archived=archived)

            with ui.row().classes("gap-2"):
                if not archived:
                    ui.button(
                        "Launch",
                        icon="play_arrow",
                        on_click=lambda _e=None, _a=wf_alias, _n=wf_name: (
                            open_launch_in_existing_project_dialog(client, _a, _n)
                        ),
                    ).props("color=primary").tooltip(
                        "Pick a project and scenario (or add a new scenario) "
                        "and start a run of this workflow."
                    )
                if is_owner and not archived:
                    ui.button(
                        "Edit",
                        icon="edit",
                        on_click=lambda _e=None, _id=int(row["id"]): _open_workflow_dialog(
                            client,
                            workflow_id=_id,
                            tool_options=tool_options,
                            tool_label_by_alias=tool_label_by_alias,
                            share_candidates=share_candidates,
                            on_saved=on_changed,
                        ),
                    ).props("flat color=primary")
                ui.button(
                    "Make a copy",
                    icon="content_copy",
                    on_click=lambda _e=None, _id=int(row["id"]): _do_copy(client, _id, on_changed),
                ).props("flat color=primary").tooltip(
                    "Fork this workflow into your own list — owner becomes "
                    "you, share list cleared, members preserved."
                )
                if is_owner and not archived:
                    ui.button(
                        "Archive",
                        icon="inventory_2",
                        on_click=lambda _e=None, _id=int(row["id"]): _confirm_archive(
                            client, _id, on_changed
                        ),
                    ).props("flat color=warning")
                if is_owner and archived:
                    ui.button(
                        "Unarchive",
                        icon="unarchive",
                        on_click=lambda _e=None, _id=int(row["id"]): _do_unarchive(
                            client, _id, on_changed
                        ),
                    ).props("flat color=primary")

        member_count = int(row.get("member_count") or 0)
        share_count = int(row.get("share_count") or 0)
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


def _render_badges(
    row: dict[str, Any],
    *,
    is_owner: bool,
    archived: bool,
) -> None:
    """Small chip strip under the workflow name."""
    badges: list[tuple[str, str]] = []
    if is_owner:
        badges.append(("My workflow", "var(--matofx-accent)"))
    else:
        owner = row.get("owner_full_name") or row.get("owner_username") or "another user"
        badges.append((f"Shared by {owner}", "#777"))
    if row.get("visibility") == "shared":
        badges.append(("Shared", "#777"))
    if archived:
        badges.append(("Archived", "#c0392b"))
    with ui.row().classes("gap-1"):
        for label, colour in badges:
            ui.badge(label).style(
                f"background-color: {colour}; color: white; "
                "padding: 0.15rem 0.5rem; font-size: 0.75rem; "
                "border-radius: 0.4rem;"
            )


# ----- actions -----------------------------------------------------------


async def _do_copy(client: ApiClient, workflow_id: int, on_changed: Any) -> None:
    """Copy the workflow + refresh the list."""
    try:
        result = await client.copy_custom_workflow(workflow_id)
    except ApiError as exc:
        ui.notify(f"Couldn't copy: {exc.detail}", type="negative")
        return
    ui.notify(
        f"Copied to '{result.get('name')}' — open it from the list to edit.",
        type="positive",
    )
    await on_changed()


async def _do_unarchive(client: ApiClient, workflow_id: int, on_changed: Any) -> None:
    """Unarchive the workflow + refresh the list."""
    try:
        await client.unarchive_custom_workflow(workflow_id)
    except ApiError as exc:
        ui.notify(f"Couldn't unarchive: {exc.detail}", type="negative")
        return
    ui.notify("Workflow restored.", type="positive")
    await on_changed()


async def _confirm_archive(client: ApiClient, workflow_id: int, on_changed: Any) -> None:
    """Confirm dialog → archive request → refresh."""
    with ui.dialog() as dialog, ui.card():
        ui.label("Archive this workflow?").style("font-size: 1.05rem; font-weight: 600;")
        ui.label(
            "It will disappear from your active list. You can bring it "
            "back by toggling 'Show archived' (the owner can unarchive "
            "from the same view in a future update)."
        ).style("color: var(--matofx-muted-text);")
        with ui.row().classes("justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Archive", on_click=lambda: dialog.submit("ok")).props("color=warning")
    result = await dialog
    if result != "ok":
        return
    try:
        await client.archive_custom_workflow(workflow_id)
    except ApiError as exc:
        ui.notify(f"Couldn't archive: {exc.detail}", type="negative")
        return
    ui.notify("Workflow archived.", type="positive")
    await on_changed()


# ----- create / edit dialog ---------------------------------------------


async def _open_workflow_dialog(
    client: ApiClient,
    *,
    workflow_id: int | None,
    tool_options: list[dict[str, Any]],
    tool_label_by_alias: dict[str, str],
    share_candidates: list[dict[str, Any]],
    on_saved: Any,
) -> None:
    """Open the unified create / edit dialog.

    ``workflow_id=None`` → create form; otherwise pre-fills from the
    existing row via :meth:`ApiClient.get_custom_workflow`.
    """
    existing: dict[str, Any] | None = None
    if workflow_id is not None:
        try:
            existing = await client.get_custom_workflow(workflow_id)
        except ApiError as exc:
            ui.notify(f"Couldn't open: {exc.detail}", type="negative")
            return

    title = "Edit Custom Workflow" if existing else "New Custom Workflow"
    default_name = (existing or {}).get("name", "")
    default_description = (existing or {}).get("description", "")
    default_tools = (
        [m["tool_alias"] for m in (existing or {}).get("members", [])] if existing else []
    )
    default_share_ids = (
        [s["user_id"] for s in (existing or {}).get("shares", [])] if existing else []
    )

    tool_choice_map = {t["alias"]: t["name"] for t in tool_options}
    share_choice_map = {
        c["user_id"]: f"{c['full_name']} ({c['username']})" for c in share_candidates
    }

    with ui.dialog() as dialog, ui.card().style("min-width: 520px;"):
        ui.label(title).style("font-size: 1.2rem; font-weight: 600;")

        name_input = ui.input("Workflow name", value=default_name).classes("w-full")
        description_input = ui.textarea(
            "Description (optional)", value=default_description
        ).classes("w-full")

        ui.label("Tools (pick 2–10)").style("font-weight: 600; margin-top: 0.5rem;")
        tools_select = (
            ui.select(
                options=tool_choice_map,
                value=list(default_tools),
                multiple=True,
                label="Choose tools",
            )
            .props("use-chips clearable")
            .classes("w-full")
        )

        order_label = ui.label("").style("color: var(--matofx-muted-text); font-size: 0.85rem;")

        def _refresh_order_label() -> None:
            picked = list(tools_select.value or [])
            if not picked:
                order_label.text = (
                    "Run order is computed from each tool's descriptor "
                    "after you pick at least one tool."
                )
                return
            display = " → ".join(tool_label_by_alias.get(a, a) for a in picked)
            order_label.text = "Tools you've picked (final order is computed on save): " + display

        tools_select.on("update:model-value", lambda _e=None: _refresh_order_label())
        _refresh_order_label()

        # Structure preview (ADR-0013) — on-demand button that
        # projects the descriptor-inferred wiring of whatever's in
        # the tools picker right now. Works on both create and edit
        # (no workflow_id required); the user clicks after adding /
        # removing tools to refresh the preview.
        from matofx.ui.workflow_graph_view import (  # noqa: PLC0415
            render_workflow_graph_preview_button,
        )

        render_workflow_graph_preview_button(
            get_tool_aliases=lambda: list(tools_select.value or []),
        )

        ui.label("Share with (optional)").style("font-weight: 600; margin-top: 0.5rem;")
        share_select = (
            ui.select(
                options=share_choice_map,
                value=list(default_share_ids),
                multiple=True,
                label="Pick users to share with",
            )
            .props("use-chips clearable")
            .classes("w-full")
        )

        error_label = ui.label("").style(
            "color: var(--matofx-error-text); font-size: 0.9rem; min-height: 1.2rem;"
        )

        async def _save() -> None:
            name = (name_input.value or "").strip()
            description = (description_input.value or "").strip()
            tool_aliases = list(tools_select.value or [])
            share_ids = [int(uid) for uid in (share_select.value or [])]
            if not name:
                error_label.text = "Please give the workflow a name."
                return
            if len(tool_aliases) < _MIN_TOOLS:
                error_label.text = (
                    f"Pick at least {_MIN_TOOLS} tools — a custom "
                    "workflow needs two or more to be useful."
                )
                return
            if len(tool_aliases) > _MAX_TOOLS:
                error_label.text = f"Pick at most {_MAX_TOOLS} tools — drop a few and try again."
                return
            try:
                if existing is None:
                    await client.create_custom_workflow(
                        name=name,
                        description=description,
                        tool_aliases=tool_aliases,
                        share_with_user_ids=share_ids,
                    )
                else:
                    await client.update_custom_workflow(
                        int(existing["id"]),
                        name=name,
                        description=description,
                        tool_aliases=tool_aliases,
                        share_with_user_ids=share_ids,
                    )
            except ApiError as exc:
                error_label.text = exc.detail
                return
            dialog.submit("ok")

        with ui.row().classes("justify-end gap-2 w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button(
                "Save" if existing else "Create",
                on_click=_save,
            ).props("color=primary")

    result = await dialog
    if result == "ok":
        ui.notify(
            "Workflow saved." if existing else "Workflow created.",
            type="positive",
        )
        await on_saved()
