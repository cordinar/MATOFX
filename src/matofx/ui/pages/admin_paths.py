"""Admin: Execution environments (``/ui/admin/paths``).

Five tabs covering the registries an admin needs to manage during
install + day-to-day:

* **Storage locations** — the shared FS mounts jobs run under.
  Create / list / retire (no hard delete; old jobs keep resolving).
* **Python envs** — the interpreters tools may pin.
  Create / list / deactivate.
* **Always-on servers** — persistent SSH/WinRM hosts the runner
  dispatches jobs to. Create / edit / activate / deactivate.
* **HPC clusters** — Slurm / MS-HPC head nodes the runner hands jobs
  off to. Create / edit / activate / deactivate.
* **Runtime settings** — the admin-tunable runner knobs (concurrency,
  heartbeat intervals, cancel grace). Each row is editable inline
  and validated server-side via the catalog.

The route slug stays ``/admin/paths`` for backward compatibility.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.runner.settings import RUNTIME_SETTINGS_CATALOG
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import icon_chip, render_shell, require_logged_in
from matofx.ui.state import current_token


@ui.page("/admin/paths")
async def admin_paths_page() -> None:
    """Render the three-tab paths admin page."""
    if not require_logged_in():
        return
    render_shell()

    with ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;"):
        ui.label("Execution environments").style(
            "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
        )
        ui.label(
            "Register where jobs run — the shared mount paths, the Python "
            "interpreters tools may pin, the always-on servers and HPC "
            "clusters the runner dispatches to, and the runner's own knobs."
        ).style("color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;")

        # Tabs in the Compare-page idiom: left-aligned, transparent, teal
        # active label + indicator, sentence-case labels (``no-caps``).
        with (
            ui.tabs()
            .classes("w-full")
            .props("align=left active-color=primary indicator-color=primary")
            .style("border-bottom: 1px solid var(--matofx-card-border);") as tabs
        ):
            tab_storage = ui.tab("Storage locations", icon="folder_open").props("no-caps")
            tab_envs = ui.tab("Python envs", icon="terminal").props("no-caps")
            tab_aos = ui.tab("Always-on servers", icon="dns").props("no-caps")
            tab_hpc = ui.tab("HPC clusters", icon="lan").props("no-caps")
            tab_runtime = ui.tab("Runtime settings", icon="tune").props("no-caps")

        with (
            ui.tab_panels(tabs, value=tab_storage)
            .classes("w-full")
            .style("background-color: transparent;")
        ):
            with ui.tab_panel(tab_storage).style("background-color: transparent;"):
                await _render_storage_section()
            with ui.tab_panel(tab_envs).style("background-color: transparent;"):
                await _render_python_envs_section()
            with ui.tab_panel(tab_aos).style("background-color: transparent;"):
                await _render_aos_hosts_section()
            with ui.tab_panel(tab_hpc).style("background-color: transparent;"):
                await _render_hpc_clusters_section()
            with ui.tab_panel(tab_runtime).style("background-color: transparent;"):
                await _render_runtime_settings_section()


# ---- storage locations -------------------------------------------------


async def _render_storage_section() -> None:
    client = ApiClient(get_app(), token=current_token())
    try:
        rows = await client.list_storage_locations()
    except ApiError as exc:
        ui.label(
            f"We couldn't load storage locations: {exc.detail}"
        ).style("color: var(--matofx-error-text);")
        return

    with ui.row().classes("w-full items-center justify-between"):
        with ui.row().classes("items-center").style("gap: 0.6rem;"):
            icon_chip("folder_open")
            ui.label("Shared mount paths").style(
                "font-weight: 650; font-size: 1.05rem; color: var(--matofx-body-text);"
            )
        ui.button("New storage location", on_click=_open_new_storage_dialog).props("color=primary")

    if not rows:
        ui.label("No storage locations yet. Click 'New storage location' to add one.").style(
            "color: var(--matofx-muted-text);"
        )
        return

    for row in rows:
        _render_storage_card(row)


def _render_storage_card(row: dict[str, Any]) -> None:
    row_id = int(row["id"])
    is_active = bool(row["is_active"])
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-start justify-between"):
            with ui.column().classes("gap-1"):
                ui.label(str(row["name"])).style("font-weight: 600; font-size: 1.05rem;")
                ui.label(str(row["mount_path"])).style(
                    "font-family: monospace; color: var(--matofx-muted-text); font-size: 0.9rem;"
                )
                if not is_active:
                    ui.label(f"Retired {row.get('retired_at') or ''}").style(
                        "color: var(--matofx-muted-text); font-size: 0.8rem;"
                    )
            if is_active:

                async def _retire(_e: Any = None, _r: int = row_id) -> None:
                    c = ApiClient(get_app(), token=current_token())
                    try:
                        await c.retire_storage_location(_r)
                    except ApiError as exc:
                        ui.notify(f"Retire failed: {exc.detail}", type="negative")
                        return
                    ui.notify("Storage location retired.", type="positive")
                    ui.navigate.to("/admin/paths")

                ui.button("Retire", on_click=_retire).props("flat color=negative")


def _open_new_storage_dialog() -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 480px;"):
        ui.label("New storage location").style("font-size: 1.2rem; font-weight: 600;")
        name = ui.input("Friendly name (e.g. 'fsx-prod') *").props("autofocus").classes("w-full")
        mount_path = ui.input("Mount path (e.g. '/mnt/fsx') *").classes("w-full")
        message = ui.label("").style("color: var(--matofx-error-text);")

        async def _create() -> None:
            if not name.value or not str(name.value).strip():
                message.text = "Name is required."
                return
            if not mount_path.value or not str(mount_path.value).strip():
                message.text = "Mount path is required."
                return
            client = ApiClient(get_app(), token=current_token())
            try:
                await client.create_storage_location(
                    name=str(name.value),
                    mount_path=str(mount_path.value),
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Storage location added.", type="positive")
            ui.navigate.to("/admin/paths")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create", on_click=_create).props("color=primary")
    dialog.open()


# ---- python envs --------------------------------------------------------


async def _render_python_envs_section() -> None:
    client = ApiClient(get_app(), token=current_token())
    try:
        rows = await client.list_python_envs()
        storage_rows = await client.list_storage_locations()
    except ApiError as exc:
        ui.label(
            f"We couldn't load Python envs: {exc.detail}"
        ).style("color: var(--matofx-error-text);")
        return

    storage_choices = {f"{r['name']} ({r['mount_path']})": int(r["id"]) for r in storage_rows}

    with ui.row().classes("w-full items-center justify-between"):
        with ui.row().classes("items-center").style("gap: 0.6rem;"):
            icon_chip("terminal")
            ui.label("Python interpreters").style(
                "font-weight: 650; font-size: 1.05rem; color: var(--matofx-body-text);"
            )
        ui.button(
            "New Python env",
            on_click=lambda: _open_new_python_env_dialog(storage_choices),
        ).props("color=primary")

    if not rows:
        ui.label("No Python envs yet. Click 'New Python env' to add one.").style(
            "color: var(--matofx-muted-text);"
        )
        return

    storage_label_by_id = {v: k for k, v in storage_choices.items()}

    for row in rows:
        _render_python_env_card(row, storage_label_by_id)


def _render_python_env_card(row: dict[str, Any], storage_label_by_id: dict[int, str]) -> None:
    row_id = int(row["id"])
    is_active = bool(row["is_active"])
    storage_id = row.get("storage_location_id")
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-start justify-between"):
            with ui.column().classes("gap-1"):
                ui.label(str(row["name"])).style("font-weight: 600; font-size: 1.05rem;")
                ui.label(str(row["interpreter_path"])).style(
                    "font-family: monospace; color: var(--matofx-muted-text); font-size: 0.9rem;"
                )
                if storage_id is not None:
                    storage_label = storage_label_by_id.get(int(storage_id), f"id={storage_id}")
                    ui.label(f"On storage: {storage_label}").style(
                        "color: var(--matofx-muted-text); font-size: 0.8rem;"
                    )
                if not is_active:
                    ui.label("Inactive").style(
                        "color: var(--matofx-muted-text); font-size: 0.8rem;"
                    )
            if is_active:

                async def _deactivate(_e: Any = None, _r: int = row_id) -> None:
                    c = ApiClient(get_app(), token=current_token())
                    try:
                        await c.deactivate_python_env(_r)
                    except ApiError as exc:
                        ui.notify(f"Deactivate failed: {exc.detail}", type="negative")
                        return
                    ui.notify("Python env deactivated.", type="positive")
                    ui.navigate.to("/admin/paths")

                ui.button("Deactivate", on_click=_deactivate).props("flat color=negative")


def _open_new_python_env_dialog(storage_choices: dict[str, int]) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 520px;"):
        ui.label("New Python env").style("font-size: 1.2rem; font-weight: 600;")
        name = ui.input("Friendly name (e.g. 'matofx-py312') *").props("autofocus").classes(
            "w-full"
        )
        interpreter_path = ui.input(
            "Interpreter path (e.g. '/opt/envs/py312/bin/python') *"
        ).classes("w-full")
        storage_pick = ui.select(
            options=["(none)", *storage_choices.keys()],
            value="(none)",
            label="Storage location (optional)",
        ).classes("w-full")
        message = ui.label("").style("color: var(--matofx-error-text);")

        async def _create() -> None:
            if not name.value or not str(name.value).strip():
                message.text = "Name is required."
                return
            if not interpreter_path.value or not str(interpreter_path.value).strip():
                message.text = "Interpreter path is required."
                return
            storage_id: int | None = None
            if storage_pick.value and storage_pick.value != "(none)":
                storage_id = storage_choices.get(str(storage_pick.value))
            client = ApiClient(get_app(), token=current_token())
            try:
                await client.create_python_env(
                    name=str(name.value),
                    interpreter_path=str(interpreter_path.value),
                    storage_location_id=storage_id,
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Python env added.", type="positive")
            ui.navigate.to("/admin/paths")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create", on_click=_create).props("color=primary")
    dialog.open()


# ---- shared host/cluster helpers ---------------------------------------

_PATHS_PAGE = "/admin/paths"


def _render_secret_status(row: dict[str, Any]) -> None:
    ref = str(row.get("secret_ref", ""))
    if row.get("secret_present"):
        ui.label(f"Credential '{ref}': set ✓").style("color: #3fb950; font-size: 0.8rem;")
    else:
        ui.label(
            f"Credential '{ref}': NOT set — run  matofx secrets set {ref}"
        ).style("color: var(--matofx-error-text); font-size: 0.8rem;")


def _render_active_toggle(row_id: int, is_active: bool, *, kind: str) -> None:
    async def _toggle(_e: Any = None) -> None:
        c = ApiClient(get_app(), token=current_token())
        try:
            if kind == "aos":
                await c.set_aos_host_active(row_id, active=not is_active)
            else:
                await c.set_hpc_cluster_active(row_id, active=not is_active)
        except ApiError as exc:
            ui.notify(f"Update failed: {exc.detail}", type="negative")
            return
        ui.notify("Updated.", type="positive")
        ui.navigate.to(_PATHS_PAGE)

    label = "Deactivate" if is_active else "Activate"
    colour = "negative" if is_active else "positive"
    ui.button(label, on_click=_toggle).props(f"flat dense color={colour}")


# ---- always-on servers --------------------------------------------------


async def _render_aos_hosts_section() -> None:
    client = ApiClient(get_app(), token=current_token())
    try:
        rows = await client.list_aos_hosts()
    except ApiError as exc:
        ui.label(
            f"We couldn't load always-on servers: {exc.detail}"
        ).style("color: var(--matofx-error-text);")
        return

    with ui.row().classes("w-full items-center justify-between"):
        with ui.row().classes("items-center").style("gap: 0.6rem;"):
            icon_chip("dns")
            ui.label("Always-on servers (AOS)").style(
                "font-weight: 650; font-size: 1.05rem; color: var(--matofx-body-text);"
            )
        ui.button(
            "New always-on server", on_click=lambda: _open_aos_dialog(None)
        ).props("color=primary")
    ui.label(
        "Persistent SSH (Linux) / WinRM (Windows) hosts the runner dispatches jobs to. "
        "The runner caps how many jobs each host runs at once. Set the host's credential "
        "separately on the server with  matofx secrets set <reference>."
    ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

    if not rows:
        ui.label("No always-on servers yet.").style("color: var(--matofx-muted-text);")
        return
    for row in rows:
        _render_aos_card(row)


def _render_aos_card(row: dict[str, Any]) -> None:
    row_id = int(row["id"])
    is_active = bool(row["is_active"])
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-start justify-between"):
            with ui.column().classes("gap-1"):
                ui.label(str(row["name"])).style("font-weight: 600; font-size: 1.05rem;")
                ui.label(
                    f"{row['username']}@{row['hostname']}:{row['port']} "
                    f"· {row['os']}/{row['transport']}"
                ).style(
                    "font-family: monospace; color: var(--matofx-muted-text); font-size: 0.9rem;"
                )
                cap = f"max {row['max_concurrency']} job(s) at once"
                if row.get("cpu_cores"):
                    cap += f" · {row['cpu_cores']}-core budget"
                if row.get("memory_mb"):
                    cap += f" · {row['memory_mb']} MB budget"
                ui.label(cap).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
                _render_secret_status(row)
                if not is_active:
                    ui.label("Inactive").style(
                        "color: var(--matofx-muted-text); font-size: 0.8rem;"
                    )
            with ui.row().classes("items-center gap-2"):
                ui.button(
                    "Edit", on_click=lambda _e=None, r=row: _open_aos_dialog(r)
                ).props("flat dense")
                _render_active_toggle(row_id, is_active, kind="aos")


def _open_aos_dialog(host: dict[str, Any] | None) -> None:
    editing = host is not None
    h = host or {}
    with ui.dialog() as dialog, ui.card().style("min-width: 540px;"):
        ui.label("Edit always-on server" if editing else "New always-on server").style(
            "font-size: 1.2rem; font-weight: 600;"
        )
        name = ui.input("Friendly name", value=str(h.get("name", ""))).classes("w-full")
        if editing:
            name.props("readonly")
        hostname = ui.input("Hostname / IP *", value=str(h.get("hostname", ""))).classes("w-full")
        os_pick = ui.select(
            options=["linux", "windows"],
            value=str(h.get("os", "linux")),
            label="OS  (transport: linux → ssh, windows → winrm)",
        ).classes("w-full")
        port = ui.number(
            "Port  (blank → 22 ssh / 5986 winrm)", value=h.get("port"), min=1, max=65535
        ).classes("w-full")
        username = ui.input("Username *", value=str(h.get("username", ""))).classes("w-full")
        secret_ref = ui.input(
            "Credential reference *  (set the value with  matofx secrets)",
            value=str(h.get("secret_ref", "")),
        ).classes("w-full")
        max_conc = ui.number(
            "Max concurrent jobs", value=h.get("max_concurrency", 1), min=1, max=4096
        ).classes("w-full")
        cpu = ui.number(
            "CPU-core budget  (optional)", value=h.get("cpu_cores"), min=1, max=4096
        ).classes("w-full")
        mem = ui.number(
            "Memory budget MB  (optional)", value=h.get("memory_mb"), min=1
        ).classes("w-full")
        message = ui.label("").style("color: var(--matofx-error-text);")

        async def _save() -> None:
            for field, lbl in (
                (hostname, "Hostname"),
                (username, "Username"),
                (secret_ref, "Credential reference"),
            ):
                if not field.value or not str(field.value).strip():
                    message.text = f"{lbl} is required."
                    return
            os_value = str(os_pick.value)
            body: dict[str, Any] = {
                "hostname": str(hostname.value).strip(),
                "os": os_value,
                "username": str(username.value).strip(),
                "secret_ref": str(secret_ref.value).strip(),
                "max_concurrency": int(max_conc.value or 1),
                "cpu_cores": int(cpu.value) if cpu.value else None,
                "memory_mb": int(mem.value) if mem.value else None,
            }
            client = ApiClient(get_app(), token=current_token())
            try:
                if editing:
                    body["port"] = (
                        int(port.value)
                        if port.value
                        else (22 if os_value == "linux" else 5986)
                    )
                    await client.update_aos_host(int(h["id"]), body=body)
                else:
                    if not str(name.value).strip():
                        message.text = "Name is required."
                        return
                    body["name"] = str(name.value).strip()
                    if port.value:
                        body["port"] = int(port.value)
                    await client.create_aos_host(body=body)
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Always-on server saved.", type="positive")
            ui.navigate.to(_PATHS_PAGE)

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=_save).props("color=primary")
    dialog.open()


# ---- HPC clusters -------------------------------------------------------


async def _render_hpc_clusters_section() -> None:
    client = ApiClient(get_app(), token=current_token())
    try:
        rows = await client.list_hpc_clusters()
    except ApiError as exc:
        ui.label(
            f"We couldn't load HPC clusters: {exc.detail}"
        ).style("color: var(--matofx-error-text);")
        return

    with ui.row().classes("w-full items-center justify-between"):
        with ui.row().classes("items-center").style("gap: 0.6rem;"):
            icon_chip("lan")
            ui.label("HPC clusters").style(
                "font-weight: 650; font-size: 1.05rem; color: var(--matofx-body-text);"
            )
        ui.button(
            "New HPC cluster", on_click=lambda: _open_hpc_dialog(None)
        ).props("color=primary")
    ui.label(
        "Slurm (SSH) / MS-HPC (WinRM) head nodes. The cluster's own scheduler owns the "
        "queue — the runner just hands jobs off. Set the cluster's credential on the "
        "server with  matofx secrets set <reference>."
    ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

    if not rows:
        ui.label("No HPC clusters yet.").style("color: var(--matofx-muted-text);")
        return
    for row in rows:
        _render_hpc_card(row)


def _render_hpc_card(row: dict[str, Any]) -> None:
    row_id = int(row["id"])
    is_active = bool(row["is_active"])
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-start justify-between"):
            with ui.column().classes("gap-1"):
                ui.label(str(row["name"])).style("font-weight: 600; font-size: 1.05rem;")
                ui.label(
                    f"{row['username']}@{row['head_node']}:{row['port']} "
                    f"· {row['scheduler']}/{row['transport']}"
                ).style(
                    "font-family: monospace; color: var(--matofx-muted-text); font-size: 0.9rem;"
                )
                if row.get("default_queue"):
                    ui.label(f"Default queue: {row['default_queue']}").style(
                        "color: var(--matofx-muted-text); font-size: 0.8rem;"
                    )
                charged_to = " · ".join(
                    f"{label}: {row[key]}"
                    for key, label in (("account", "Account"), ("wckey", "Work key"))
                    if row.get(key)
                )
                if charged_to:
                    ui.label(f"Charged to — {charged_to}").style(
                        "color: var(--matofx-muted-text); font-size: 0.8rem;"
                    )
                _render_secret_status(row)
                if not is_active:
                    ui.label("Inactive").style(
                        "color: var(--matofx-muted-text); font-size: 0.8rem;"
                    )
            with ui.row().classes("items-center gap-2"):
                ui.button(
                    "Edit", on_click=lambda _e=None, r=row: _open_hpc_dialog(r)
                ).props("flat dense")
                _render_active_toggle(row_id, is_active, kind="hpc")


def _open_hpc_dialog(cluster: dict[str, Any] | None) -> None:
    editing = cluster is not None
    c = cluster or {}
    with ui.dialog() as dialog, ui.card().style("min-width: 540px;"):
        ui.label("Edit HPC cluster" if editing else "New HPC cluster").style(
            "font-size: 1.2rem; font-weight: 600;"
        )
        name = ui.input("Friendly name", value=str(c.get("name", ""))).classes("w-full")
        if editing:
            name.props("readonly")
        head_node = ui.input(
            "Head node hostname / IP *", value=str(c.get("head_node", ""))
        ).classes("w-full")
        scheduler_pick = ui.select(
            options=["slurm", "mshpc"],
            value=str(c.get("scheduler", "slurm")),
            label="Scheduler  (transport: slurm → ssh, mshpc → winrm)",
        ).classes("w-full")
        port = ui.number(
            "Port  (blank → 22 ssh / 5986 winrm)", value=c.get("port"), min=1, max=65535
        ).classes("w-full")
        username = ui.input("Username *", value=str(c.get("username", ""))).classes("w-full")
        secret_ref = ui.input(
            "Credential reference *  (set the value with  matofx secrets)",
            value=str(c.get("secret_ref", "")),
        ).classes("w-full")
        default_queue = ui.input(
            "Default queue / partition  (optional)", value=str(c.get("default_queue") or "")
        ).classes("w-full")

        # Slurm-only: the project a run is charged to. Sites use one of
        # these or the other, so both are offered and both are optional.
        # MS-HPC has no equivalent directive, so they're hidden for it.
        account = ui.input(
            "Slurm account  (optional — sent as --account)",
            value=str(c.get("account") or ""),
        ).classes("w-full")
        wckey = ui.input(
            "Slurm work key  (optional — sent as --wckey)",
            value=str(c.get("wckey") or ""),
        ).classes("w-full")
        accounting_hint = ui.label(
            "Clusters that charge time to a project reject or mis-attribute jobs that "
            "don't name it. Ask the cluster's administrator which of the two they use."
        ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")

        def _on_scheduler_change(value: str) -> None:
            is_slurm = value == "slurm"
            for element in (account, wckey, accounting_hint):
                element.set_visibility(is_slurm)

        scheduler_pick.on_value_change(lambda e: _on_scheduler_change(str(e.value)))
        _on_scheduler_change(str(c.get("scheduler", "slurm")))

        message = ui.label("").style("color: var(--matofx-error-text);")

        async def _save() -> None:
            for field, lbl in (
                (head_node, "Head node"),
                (username, "Username"),
                (secret_ref, "Credential reference"),
            ):
                if not field.value or not str(field.value).strip():
                    message.text = f"{lbl} is required."
                    return
            sched_value = str(scheduler_pick.value)

            def _optional(field: Any) -> str | None:
                return str(field.value).strip() or None if field.value else None

            # Accounting is Slurm-only; switching a cluster to MS-HPC
            # clears the two fields rather than saving values its
            # scheduler would silently ignore.
            is_slurm = sched_value == "slurm"
            body: dict[str, Any] = {
                "head_node": str(head_node.value).strip(),
                "scheduler": sched_value,
                "username": str(username.value).strip(),
                "secret_ref": str(secret_ref.value).strip(),
                "default_queue": _optional(default_queue),
                "account": _optional(account) if is_slurm else None,
                "wckey": _optional(wckey) if is_slurm else None,
            }
            client = ApiClient(get_app(), token=current_token())
            try:
                if editing:
                    body["port"] = (
                        int(port.value)
                        if port.value
                        else (22 if sched_value == "slurm" else 5986)
                    )
                    await client.update_hpc_cluster(int(c["id"]), body=body)
                else:
                    if not str(name.value).strip():
                        message.text = "Name is required."
                        return
                    body["name"] = str(name.value).strip()
                    if port.value:
                        body["port"] = int(port.value)
                    await client.create_hpc_cluster(body=body)
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("HPC cluster saved.", type="positive")
            ui.navigate.to(_PATHS_PAGE)

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=_save).props("color=primary")
    dialog.open()


# ---- runtime settings ---------------------------------------------------


async def _render_runtime_settings_section() -> None:
    client = ApiClient(get_app(), token=current_token())
    try:
        snapshot = await client.get_runtime_settings()
    except ApiError as exc:
        ui.label(
            f"We couldn't load runtime settings: {exc.detail}"
        ).style("color: var(--matofx-error-text);")
        return

    values: dict[str, Any] = dict(snapshot.get("settings", {}))

    with ui.row().classes("items-center").style("gap: 0.6rem;"):
        icon_chip("tune")
        ui.label("Runner knobs").style(
            "font-weight: 650; font-size: 1.05rem; color: var(--matofx-body-text);"
        )
    ui.label(
        "Edits hot-reload into the runner within ~30 seconds. Values "
        "outside the allowed range are rejected with a plain-English message."
    ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

    for key, spec in RUNTIME_SETTINGS_CATALOG.items():
        _render_runtime_setting_row(key, spec, values.get(key, spec.default))


def _render_runtime_setting_row(key: str, spec: Any, current_value: Any) -> None:
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-start justify-between"):
            with ui.column().classes("gap-1 flex-1"):
                ui.label(spec.label).style("font-weight: 600;")
                ui.label(spec.description).style(
                    "color: var(--matofx-muted-text); font-size: 0.85rem;"
                )
                ui.label(
                    f"Range: {spec.min_value} to {spec.max_value} {spec.unit} "
                    f"(default {spec.default})"
                ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")

            with ui.row().classes("items-center gap-2"):
                input_field = (
                    ui.number(
                        label=spec.unit,
                        value=float(current_value),
                        min=spec.min_value,
                        max=spec.max_value,
                        step=1 if spec.kind == "int" else 0.1,
                    )
                    .props("dense")
                    .style("width: 130px;")
                )

                async def _save(
                    _e: Any = None,
                    _k: str = key,
                    _f: Any = input_field,
                    _kind: str = spec.kind,
                ) -> None:
                    if _f.value is None:
                        ui.notify("Value is required.", type="negative")
                        return
                    raw = _f.value
                    coerced: Any = int(raw) if _kind == "int" else float(raw)
                    c = ApiClient(get_app(), token=current_token())
                    try:
                        await c.set_runtime_setting(_k, value=coerced)
                    except ApiError as exc:
                        ui.notify(f"Save failed: {exc.detail}", type="negative")
                        return
                    ui.notify(f"{_k} saved.", type="positive")

                async def _reset(_e: Any = None, _k: str = key) -> None:
                    c = ApiClient(get_app(), token=current_token())
                    try:
                        await c.reset_runtime_setting(_k)
                    except ApiError as exc:
                        ui.notify(f"Reset failed: {exc.detail}", type="negative")
                        return
                    ui.notify(f"{_k} reset to default.", type="positive")
                    ui.navigate.to("/admin/paths")

                ui.button("Save", on_click=_save).props("color=primary dense")
                ui.button("Reset", on_click=_reset).props("flat dense")
