"""Project view (``/ui/projects/{project_id}``) — ADR-0002.

Shows the project's description + metadata, the access list grouped
by category (Creator / Owners / Members / Viewers, each as
``Full name (username)``), and the scenario list. Creator/Owner can
Edit (name, description, metadata), Share (per-user ACL via a people
picker), and Archive. The Creator is pinned and never removable.

The body is a :func:`ui.refreshable` so an Edit or Share change shows
immediately — no manual browser refresh.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui import metadata_form
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token

_ROLES = ["owner", "member", "viewer"]
_ROLE_LABEL = {"owner": "Owners", "member": "Members", "viewer": "Viewers"}


def _person(directory: dict[int, dict[str, str]], user_id: int) -> str:
    """``Full name (username)`` for a user id, or a stable fallback."""
    entry = directory.get(user_id)
    if entry is None:
        return f"user #{user_id}"
    return f"{entry['full_name']} ({entry['username']})"


@ui.page("/projects/{project_id}")
async def project_page(project_id: int) -> None:
    """Render one project: governance header + scenarios list."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())

    @ui.refreshable
    async def content() -> None:
        try:
            me = await client.me()
            project = await client.get_project(project_id)
            scenarios = await client.list_scenarios(project_id)
            acl = await client.get_project_acl(project_id)
            directory_list = await client.list_user_directory()
            fields = await client.list_metadata_fields()
            meta_rows = await client.get_project_metadata(project_id)
        except ApiError as exc:
            with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
                ui.label(f"We couldn't load that project: {exc.detail}").style("color: #c0392b;")
            return

        my_id = int(me["user_id"])
        creator_id = int(project["creator_user_id"])
        directory = {int(d["user_id"]): d for d in directory_list}
        my_entry = next((e for e in acl["entries"] if e["user_id"] == my_id), None)
        is_owner = (my_id == creator_id) or (my_entry is not None and my_entry["role"] == "owner")

        value_label = {v["id"]: v["value"] for f in fields for v in (f.get("values") or [])}
        field_by_id = {f["id"]: f for f in fields}
        grouped: dict[int, list[str]] = {}
        for row in meta_rows:
            if row.get("value_id") is not None:
                txt = value_label.get(row["value_id"], "?")
            elif row.get("value_num") is not None:
                txt = str(row["value_num"])
            else:
                txt = str(row.get("value_date") or "")
            grouped.setdefault(row["field_id"], []).append(txt)
        meta_display: list[tuple[str, str]] = [
            (f["label"], ", ".join(grouped[f["id"]])) for f in fields if f["id"] in grouped
        ]
        for fid, vals in grouped.items():
            if fid not in field_by_id:
                meta_display.append((f"field #{fid}", ", ".join(vals)))

        with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
            ui.button(
                "← Back to projects",
                on_click=lambda: ui.navigate.to("/dashboard"),
            ).props("flat")

            with ui.row().classes("items-start justify-between w-full"):
                with ui.column().classes("gap-1"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(str(project["name"])).style("font-size: 1.5rem; font-weight: 600;")
                        if project.get("state") != "active":
                            ui.badge(str(project["state"]), color="grey")
                    ui.label(f"Created by {_person(directory, creator_id)}").style(
                        "color: #777; font-size: 0.85rem;"
                    )
                    if project.get("description"):
                        ui.label(str(project["description"])).style("color: #555;")
                if is_owner:
                    with ui.row().classes("gap-2"):
                        ui.button(
                            "Edit",
                            on_click=lambda: _edit_dialog(client, project, content.refresh),
                        ).props("flat")
                        ui.button(
                            "Share",
                            on_click=lambda: _share_dialog(
                                client,
                                project_id,
                                creator_id,
                                directory_list,
                                content.refresh,
                            ),
                        ).props("flat")
                        if project.get("state") == "active":
                            ui.button(
                                "Archive",
                                on_click=lambda: _archive_dialog(client, project_id),
                            ).props("flat color=negative")

            with ui.card().classes("w-full"), ui.row().classes("w-full gap-8 flex-wrap"):
                with ui.column().classes("gap-1 grow"):
                    ui.label("Metadata").style("font-weight: 600;")
                    if not meta_display:
                        ui.label("No metadata set.").style("color: #999; font-size: 0.85rem;")
                    for label, val in meta_display:
                        ui.label(f"{label}: {val}").style("font-size: 0.9rem;")
                with ui.column().classes("gap-1 grow"):
                    ui.label("Access").style("font-weight: 600;")
                    ui.label(f"Creator — {_person(directory, creator_id)}").style(
                        "font-size: 0.9rem;"
                    )
                    by_role: dict[str, list[str]] = {
                        "owner": [],
                        "member": [],
                        "viewer": [],
                    }
                    for e in acl["entries"]:
                        by_role.setdefault(e["role"], []).append(_person(directory, e["user_id"]))
                    for role in _ROLES:
                        people = sorted(by_role.get(role, []))
                        if people:
                            ui.label(f"{_ROLE_LABEL[role]} — " + "; ".join(people)).style(
                                "font-size: 0.9rem;"
                            )

            with ui.row().classes("items-center justify-between w-full"):
                ui.label("Scenarios").style("font-size: 1.2rem; font-weight: 600;")
                ui.button(
                    "New scenario",
                    on_click=lambda: _open_new_scenario_dialog(client, project_id),
                ).props("color=primary")

            if not scenarios:
                ui.label("No scenarios in this project yet. Click 'New scenario' to start.").style(
                    "color: #555;"
                )
                return
            for scenario in scenarios:
                _render_scenario_row(scenario)

    await content()


def _render_scenario_row(scenario: dict[str, Any]) -> None:
    sid = scenario["id"]
    with (
        ui.card()
        .classes("w-full cursor-pointer hover:bg-gray-50")
        .on("click", lambda _e=None, _sid=sid: ui.navigate.to(f"/scenarios/{_sid}"))
    ):
        ui.label(str(scenario["name"])).style("font-size: 1.1rem; font-weight: 600;")
        description = str(scenario.get("description") or "")
        if description:
            ui.label(description).style("color: #555; font-size: 0.9rem;")


def _open_new_scenario_dialog(client: ApiClient, project_id: int) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label("New scenario").style("font-size: 1.2rem; font-weight: 600;")
        name = ui.input("Scenario name *").props("autofocus").classes("w-full")
        description = ui.input("Description (optional)").classes("w-full")
        message = ui.label("").style("color: #c0392b;")

        async def _create() -> None:
            if not name.value or not name.value.strip():
                message.text = "Please enter a name for the scenario."
                return
            try:
                scenario = await client.create_scenario(
                    project_id, name=name.value, description=description.value or ""
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Scenario created.", type="positive")
            ui.navigate.to(f"/scenarios/{scenario['id']}")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create", on_click=_create).props("color=primary")
    dialog.open()


def _edit_dialog(client: ApiClient, project: dict[str, Any], refresh: Any) -> None:
    pid = int(project["id"])
    with ui.dialog() as dialog, ui.card().style("min-width: 520px;"):
        ui.label("Edit project").style("font-size: 1.2rem; font-weight: 600;")
        name = ui.input("Name *", value=str(project["name"])).classes("w-full")
        description = ui.input("Description", value=str(project.get("description") or "")).classes(
            "w-full"
        )
        ui.separator()
        ui.label("Metadata").style("font-weight: 600;")
        meta_box = ui.column().classes("w-full gap-2")
        state: dict[str, Any] = {"fields": [], "widgets": {}}
        message = ui.label("").style("color: #c0392b;")

        async def _load_meta() -> None:
            meta_box.clear()
            fields = await metadata_form.load_fields(client)
            try:
                current = metadata_form.current_by_field(await client.get_project_metadata(pid))
            except ApiError as exc:
                with meta_box:
                    ui.label(exc.detail).style("color: #c0392b;")
                return
            state["fields"] = fields
            with meta_box:
                state["widgets"] = metadata_form.render_fields(fields, current)

        async def _save() -> None:
            try:
                selections = metadata_form.collect(state["fields"], state["widgets"])
            except metadata_form.MetadataInputError as exc:
                message.text = str(exc)
                return
            try:
                await client.update_project(
                    pid,
                    name=str(name.value).strip(),
                    description=str(description.value or ""),
                    expected_version=int(project.get("version", 0)) or None,
                )
                await client.set_project_metadata(pid, selections=selections)
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Project updated.", type="positive")
            refresh()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=_save).props("color=primary")
    dialog.open()
    ui.timer(0.05, _load_meta, once=True)


def _share_dialog(
    client: ApiClient,
    project_id: int,
    creator_id: int,
    directory_list: list[dict[str, Any]],
    refresh: Any,
) -> None:
    directory = {int(d["user_id"]): d for d in directory_list}
    # Staged, in-memory ACL (user_id -> role), excluding the Creator.
    # Every edit touches this map only; nothing hits the server until
    # Save, where the whole map is sent as one batch replace. ``saved``
    # is the snapshot loaded from the server, used for dirty-detection.
    working: dict[int, str] = {}
    state: dict[str, Any] = {"saved": {}, "add_err": None, "loaded": False}

    with ui.dialog() as dialog, ui.card().style("min-width: 560px; max-width: 640px;"):
        ui.label("Share project").style("font-size: 1.2rem; font-weight: 600;")
        ui.label(
            f"Creator — {_person(directory, creator_id)} — always has full "
            "control and can't be removed."
        ).style("color: #777; font-size: 0.85rem;")
        # Edits stage here and rebuild the body locally — no server round
        # trips and no dialog teardown until Save commits the batch.
        body_box = ui.column().classes("w-full gap-2")
        save_btn: dict[str, Any] = {}

        def _refresh_save_state() -> None:
            btn = save_btn.get("btn")
            if btn is not None:
                btn.set_enabled(working != state["saved"])

        def _set_role(user_id: int, role: str) -> None:
            working[user_id] = role
            _refresh_save_state()  # no re-render: the select shows its own value

        def _remove(user_id: int) -> None:
            working.pop(user_id, None)
            _render()

        def _add(picker: Any, role_in: Any) -> None:
            err = state["add_err"]
            if picker.value in (None, ""):
                if err is not None:
                    err.text = "Pick a person."
                return
            working[int(picker.value)] = str(role_in.value)
            _render()

        def _member_row(uid: int, role: str) -> None:
            with ui.row().classes("items-center justify-between w-full"):
                ui.label(_person(directory, uid)).classes("grow")
                with ui.row().classes("items-center gap-2"):
                    sel = ui.select(_ROLES, value=role).props("dense")
                    sel.on(
                        "update:model-value",
                        lambda _e=None, u=uid, s=sel: _set_role(u, s.value),
                    )
                    ui.button(
                        icon="delete",
                        on_click=lambda _e=None, u=uid: _remove(u),
                    ).props("flat dense color=negative")

        def _add_section(options: dict[int, str]) -> None:
            with ui.column().classes("w-full gap-1"):
                with ui.row().classes("items-center gap-2 w-full"):
                    picker = (
                        ui.select(options, label="Person", with_input=True)
                        .props("dense")
                        .classes("grow")
                    )
                    role_in = ui.select(_ROLES, value="viewer").props("dense")
                    ui.button(
                        "Add",
                        on_click=lambda _e=None, p=picker, r=role_in: _add(p, r),
                    ).props("color=primary")
                state["add_err"] = ui.label("").style("color: #c0392b; font-size: 0.8rem;")

        def _render() -> None:
            body_box.clear()
            with body_box:
                ui.label("People with access").style("font-weight: 600;")
                with ui.column().classes("w-full gap-1").style(
                    "max-height: 40vh; overflow-y: auto;"
                ):
                    with ui.row().classes("items-center justify-between w-full"):
                        ui.label(_person(directory, creator_id)).classes("grow")
                        ui.label("Owner · Creator").style("color: #777; font-size: 0.85rem;")
                    if not working:
                        ui.label("No one else has access yet.").style(
                            "color: #999; font-size: 0.85rem;"
                        )
                    for uid, role in working.items():
                        _member_row(uid, role)

                ui.separator()
                ui.label("Add a person").style("font-weight: 600;")
                taken = {creator_id} | set(working)
                options = {
                    int(d["user_id"]): f"{d['full_name']} ({d['username']})"
                    for d in directory_list
                    if int(d["user_id"]) not in taken
                }
                if not options:
                    ui.label("Everyone in the directory already has access.").style(
                        "color: #999; font-size: 0.85rem;"
                    )
                else:
                    _add_section(options)
            _refresh_save_state()

        async def _load() -> None:
            try:
                current = await client.get_project_acl(project_id)
            except ApiError as exc:
                body_box.clear()
                with body_box:
                    ui.label(exc.detail).style("color: #c0392b;")
                return
            working.clear()
            for e in current["entries"]:
                working[int(e["user_id"])] = str(e["role"])
            state["saved"] = dict(working)
            state["loaded"] = True
            _render()

        async def _save() -> None:
            if not state["loaded"]:
                return  # never overwrite the ACL with an unloaded (empty) map
            grants = [{"user_id": uid, "role": role} for uid, role in working.items()]
            try:
                await client.replace_project_acl(project_id, grants=grants)
            except ApiError as exc:
                ui.notify(exc.detail, type="negative")
                return
            ui.notify("Sharing updated.", type="positive")
            dialog.close()
            refresh()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            btn = ui.button("Save", on_click=_save).props("color=primary")
            btn.set_enabled(False)
            save_btn["btn"] = btn
    dialog.open()
    ui.timer(0.05, _load, once=True)


def _archive_dialog(client: ApiClient, project_id: int) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label("Archive project").style("font-size: 1.2rem; font-weight: 600;")
        ui.label(
            "Archiving hides the project from the default list. Nothing is "
            "deleted — it stays in the system and can still be found with "
            "'Show archived'."
        ).style("color: #555;")
        message = ui.label("").style("color: #c0392b;")

        async def _do(state: str) -> None:
            try:
                await client.archive_project(project_id, state=state)
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify(f"Project {state}.", type="positive")
            ui.navigate.to("/dashboard")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Cancel project", on_click=lambda: _do("cancelled")).props(
                "flat color=negative"
            )
            ui.button("Archive", on_click=lambda: _do("archived")).props("color=negative")
    dialog.open()
