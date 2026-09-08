"""Dashboard (``/ui/dashboard``) — the projects table (ADR-0002).

Lists every project the user can see (Creator / Owner / Member /
Viewer; admins see all). Filter row on top per the UI convention:
text search + a "show archived" toggle. The search matches a project's
**name, its metadata values, and its owner**. Each card shows the
project's state, whether the viewer is its Creator, any metadata fields
flagged *show in table*, and who the project is shared with (and their
role). "New project" collects the name/description plus the active
metadata taxonomy.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nicegui import ui

from matofx.ui import metadata_form, share_form
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import format_role_label, render_shell, require_logged_in
from matofx.ui.state import current_token


@ui.page("/dashboard")
async def dashboard_page() -> None:
    """Render the dashboard."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())
    try:
        me = await client.me()
        fields = await client.list_metadata_fields()
        directory_list = await client.list_user_directory()
    except ApiError as exc:
        with ui.column().classes("w-full max-w-5xl mx-auto p-6"):
            ui.label(f"We couldn't load the dashboard: {exc.detail}").style("color: #c0392b;")
        return

    my_id = int(me["user_id"])
    table_fields = [f for f in fields if f.get("show_in_table")]
    value_label = {v["id"]: v["value"] for f in fields for v in (f.get("values") or [])}
    # user_id → display name, for the "shared with" line and owner search.
    directory_name = {
        int(u["user_id"]): str(u.get("full_name") or u.get("username") or f"User {u['user_id']}")
        for u in directory_list
    }

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Your projects").style("font-size: 1.5rem; font-weight: 600;")
            ui.button(
                "New project",
                on_click=lambda: _open_new_project_dialog(client, fields, directory_list, my_id),
            ).props("color=primary")

        # Filter row — above the results (UI convention).
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-4 w-full"):
                search = (
                    ui.input("Search by name, metadata or owner")
                    .props("clearable dense")
                    .classes("grow")
                )
                show_archived = ui.switch("Show archived")

        results = ui.column().classes("w-full gap-2")
        # Enriched projects (with metadata + share list) cached between
        # keystrokes so typing filters in-memory rather than refetching.
        cache: dict[str, list[dict[str, Any]]] = {"projects": []}

        def _render() -> None:
            results.clear()
            term = (search.value or "").strip().lower()
            shown = [e for e in cache["projects"] if not term or term in e["search_blob"]]
            with results:
                if not shown:
                    msg = (
                        "No projects match your search."
                        if term
                        else "No projects yet. Click 'New project' to start."
                    )
                    ui.label(msg).style("color: #555;")
                    return
                for entry in shown:
                    _render_project_card(entry, my_id, table_fields)

        async def _reload() -> None:
            results.clear()
            try:
                projects = await client.list_projects_ex(include_archived=bool(show_archived.value))
            except ApiError as exc:
                cache["projects"] = []
                with results:
                    ui.label(f"Couldn't load projects: {exc.detail}").style("color: #c0392b;")
                return
            cache["projects"] = await _enrich_projects(
                client, projects, value_label, directory_name
            )
            _render()

        search.on("update:model-value", lambda _e=None: _render())
        show_archived.on("update:model-value", lambda _e=None: _reload())
        await _reload()


async def _enrich_projects(
    client: ApiClient,
    projects: list[dict[str, Any]],
    value_label: dict[int, str],
    directory_name: dict[int, str],
) -> list[dict[str, Any]]:
    """Fetch each project's metadata + share list and build a search blob.

    Done once per (re)load and cached, so the search box can filter in
    memory. Per-project metadata and ACL are fetched concurrently; a
    failure on either degrades that project gracefully (empty) rather
    than breaking the list.
    """

    async def _one(project: dict[str, Any]) -> dict[str, Any]:
        pid = project["id"]
        meta, acl = await asyncio.gather(
            client.get_project_metadata(pid),
            client.get_project_acl(pid),
            return_exceptions=True,
        )
        by_field: dict[int, list[str]] = {}
        if not isinstance(meta, BaseException):
            for row in meta:
                if row.get("value_id") is not None:
                    text = value_label.get(row["value_id"], "?")
                elif row.get("value_num") is not None:
                    text = str(row["value_num"])
                else:
                    text = str(row.get("value_date") or "")
                by_field.setdefault(row["field_id"], []).append(text)
        shared: list[tuple[str, str]] = []
        if not isinstance(acl, BaseException):
            for ent in acl.get("entries", []):
                name = directory_name.get(int(ent["user_id"]), f"User {ent['user_id']}")
                shared.append((name, format_role_label(str(ent["role"]))))
        meta_texts = [t for vals in by_field.values() for t in vals]
        search_blob = " ".join(
            [str(project.get("name") or ""), str(project.get("owner_name") or ""), *meta_texts]
        ).lower()
        return {
            "project": project,
            "by_field": by_field,
            "shared": shared,
            "search_blob": search_blob,
        }

    return list(await asyncio.gather(*[_one(p) for p in projects]))


def _render_project_card(
    entry: dict[str, Any],
    my_id: int,
    table_fields: list[dict[str, Any]],
) -> None:
    """Render one project card from its pre-fetched, enriched data."""
    project = entry["project"]
    pid = project["id"]
    state = str(project.get("state") or "active")
    is_creator = int(project.get("creator_user_id", -1)) == my_id
    by_field: dict[int, list[str]] = entry["by_field"]
    shared: list[tuple[str, str]] = entry["shared"]
    with (
        ui.card()
        .classes("w-full cursor-pointer hover:bg-gray-50")
        .on("click", lambda _e=None, _pid=pid: ui.navigate.to(f"/projects/{_pid}"))
    ):
        with ui.column().classes("gap-1 w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.label(str(project["name"])).style("font-size: 1.1rem; font-weight: 600;")
                if state != "active":
                    ui.badge(state, color="grey")
                ui.badge(
                    "Creator" if is_creator else "Shared",
                    color="primary" if is_creator else "secondary",
                )
            description = str(project.get("description") or "")
            if description:
                ui.label(description).style("color: #555; font-size: 0.9rem;")
            # Metadata fields flagged "show in table".
            if table_fields:
                with ui.row().classes("items-center gap-3 flex-wrap"):
                    for f in table_fields:
                        vals = by_field.get(f["id"])
                        if vals:
                            ui.label(f"{f['label']}: {', '.join(vals)}").style(
                                "color: #777; font-size: 0.8rem;"
                            )
            # Who the project is shared with (per-user grants + their role).
            if shared:
                shared_text = ", ".join(f"{name} ({role})" for name, role in shared)
                with ui.row().classes("items-center gap-1 flex-wrap"):
                    ui.icon("group").style("color: #777; font-size: 1rem;")
                    ui.label(f"Shared with: {shared_text}").style(
                        "color: #777; font-size: 0.8rem;"
                    )


def _open_new_project_dialog(
    client: ApiClient,
    fields: list[dict[str, Any]],
    directory_list: list[dict[str, Any]],
    creator_user_id: int,
) -> None:
    """Modal: name + description + metadata + (optional) share."""
    with ui.dialog() as dialog, ui.card().style("min-width: 480px;"):
        ui.label("New project").style("font-size: 1.2rem; font-weight: 600;")
        name = ui.input("Project name *").props("autofocus").classes("w-full")
        description = ui.input("Description (optional)").classes("w-full")
        widgets: dict[int, Any] = {}
        if fields:
            ui.separator()
            ui.label("Metadata").style("font-weight: 600;")
            widgets = metadata_form.render_fields(fields)
        ui.separator()
        ui.label("Share with (optional)").style("font-weight: 600;")
        share_state = share_form.render(directory_list, creator_user_id)
        message = ui.label("").style("color: #c0392b;")

        async def _create() -> None:
            if not name.value or not name.value.strip():
                message.text = "Please enter a name for the project."
                return
            try:
                # Validate the metadata (e.g. date format) before creating
                # anything, so a bad entry can't leave an orphan project.
                selections = metadata_form.collect(fields, widgets)
            except metadata_form.MetadataInputError as exc:
                message.text = str(exc)
                return
            try:
                project = await client.create_project(
                    name=name.value, description=description.value or ""
                )
                if selections or metadata_form.has_required(fields):
                    await client.set_project_metadata(project["id"], selections=selections)
                await share_form.apply(client, int(project["id"]), share_form.collect(share_state))
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Project created.", type="positive")
            ui.navigate.to(f"/projects/{project['id']}")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create", on_click=_create).props("color=primary")
    dialog.open()
