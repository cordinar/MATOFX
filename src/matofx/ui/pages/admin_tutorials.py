"""Admin: Tutorial management (``/ui/admin/tutorials``).

Upload, activate / deactivate, and **update** tutorial files (PDFs +
videos). Three scopes:

* Per-tool — at most one active document + one active video per tool
  at any time. Activating one cascades inactive on the previously-
  active sibling so users always see a clean single-document /
  single-video pair on the home page's tool Quick Access card.
* Per-workflow — same cap and cascade as tools, scoped to a workflow.
  Surfaced on the Workflow Quick Access card's *Tutorials* button.
* General — uncapped: an install can ship "Getting started",
  "Advanced tips", and a safety brief all at once.

Tutorials are **never deleted**. To publish a newer version, use
**Update**: it uploads a replacement, makes it the live version, and
keeps the old one as an archived *previous version* (shown collapsed
under the current one, preview-only). Styling follows the home-redesign
shell — themed cards, colours from the theme CSS variables.

Permissions: requires :attr:`Permission.TUTORIALS_MANAGE`. Seeded
on the ``administrator`` and ``super_user`` roles by migration 0020.
"""

from __future__ import annotations

from typing import Any

from nicegui import events, ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token

_MEDIA_TYPE_LABEL: dict[str, str] = {
    "document": "Document (PDF)",
    "video": "Video",
}
_MEDIA_TYPE_ICON: dict[str, str] = {
    "document": "picture_as_pdf",
    "video": "play_circle",
}


@ui.page("/admin/tutorials")
async def admin_tutorials_page() -> None:
    """Render the tutorial-management surface."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())

    with ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;"):
        with ui.column().classes("gap-1"):
            ui.label("Tutorial management").style(
                "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
            )
            ui.label(
                "Upload the PDFs and videos that appear on users' Home page. "
                "Each tool or workflow shows one active document and one active "
                "video at a time; general tutorials have no cap. Publish a newer "
                "version any time with Update — the previous version is kept as "
                "history."
            ).style("color: var(--matofx-muted-text); font-size: 0.9rem; max-width: 70ch;")

        # The shell's fixed top-right cluster (theme + help) sits over the
        # header's right edge, so the primary action goes on its own row
        # below the header rather than beside the title.
        with ui.row().classes("w-full justify-end"):
            ui.button(
                "Upload tutorial",
                icon="upload",
                on_click=lambda: _open_upload_dialog(client, _reload),
            ).props("color=primary unelevated no-caps")

        list_container = ui.column().classes("w-full gap-4")

        async def _reload() -> None:
            list_container.clear()
            try:
                tutorials = await client.list_admin_tutorials()
                tools = await client.list_admin_tools()
                workflows = await client.list_admin_workflows()
            except ApiError as exc:
                with list_container:
                    ui.label(f"We couldn't load tutorials: {exc.detail}").style(
                        "color: var(--matofx-error-text);"
                    )
                return
            with list_container:
                _render_grouped_list(client, tutorials, tools, workflows, _reload)

        await _reload()


# ---------- listing ------------------------------------------------------


def _render_grouped_list(
    client: ApiClient,
    tutorials: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    workflows: list[dict[str, Any]],
    reload_fn: Any,
) -> None:
    """Group *current* tutorials by scope; tuck archived versions under each.

    Three buckets — general first, then per-tool, then per-workflow.
    Archived previous versions (``is_archived``) are not listed in their
    own right: they're attached to their lineage's current card and shown
    collapsed beneath it.
    """
    if not tutorials:
        ui.label("No tutorials uploaded yet. Click 'Upload tutorial' above to add one.").style(
            "color: var(--matofx-muted-text);"
        )
        return

    current = [t for t in tutorials if not t.get("is_archived")]
    archived_by_lineage: dict[int, list[dict[str, Any]]] = {}
    for t in tutorials:
        if t.get("is_archived"):
            lineage = int(t.get("lineage_id") or t.get("id") or 0)
            archived_by_lineage.setdefault(lineage, []).append(t)

    general = [
        t for t in current if t.get("tool_alias") is None and t.get("workflow_alias") is None
    ]
    per_tool: dict[str, list[dict[str, Any]]] = {}
    per_workflow: dict[str, list[dict[str, Any]]] = {}
    for t in current:
        tool_alias = t.get("tool_alias")
        workflow_alias = t.get("workflow_alias")
        if tool_alias is not None:
            per_tool.setdefault(str(tool_alias), []).append(t)
        elif workflow_alias is not None:
            per_workflow.setdefault(str(workflow_alias), []).append(t)

    tool_name_lookup = {str(t.get("alias")): str(t.get("name") or t.get("alias")) for t in tools}
    workflow_name_lookup = {
        str(w.get("alias")): str(w.get("name") or w.get("alias")) for w in workflows
    }

    if general:
        _render_section(
            "General tutorials",
            "Shown to every user on the Home page, regardless of which "
            "tool or workflow they're using.",
            general,
            archived_by_lineage,
            client,
            reload_fn,
        )

    for alias in sorted(per_tool, key=lambda a: (tool_name_lookup.get(a) or a).lower()):
        pretty = tool_name_lookup.get(alias, alias)
        _render_section(
            f"Tool: {pretty}",
            f"Surfaced on the Home page's tool quick-access card for "
            f"{pretty}. At most one document + one video may be active "
            "at a time.",
            per_tool[alias],
            archived_by_lineage,
            client,
            reload_fn,
        )

    for alias in sorted(per_workflow, key=lambda a: (workflow_name_lookup.get(a) or a).lower()):
        pretty = workflow_name_lookup.get(alias, alias)
        _render_section(
            f"Workflow: {pretty}",
            f"Surfaced on the Home page's workflow quick-access card "
            f"for {pretty}. At most one document + one video may be "
            "active at a time.",
            per_workflow[alias],
            archived_by_lineage,
            client,
            reload_fn,
        )


def _render_section(
    heading: str,
    subtitle: str,
    rows: list[dict[str, Any]],
    archived_by_lineage: dict[int, list[dict[str, Any]]],
    client: ApiClient,
    reload_fn: Any,
) -> None:
    """One scope-grouped section header + list of current-version cards."""
    with ui.column().classes("w-full gap-2"):
        ui.label(heading).style(
            "font-size: 1.05rem; font-weight: 650; color: var(--matofx-body-text);"
        )
        ui.label(subtitle).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        documents = sorted(
            [r for r in rows if r.get("media_type") == "document"],
            key=lambda r: str(r.get("created_at") or ""),
            reverse=True,
        )
        videos = sorted(
            [r for r in rows if r.get("media_type") == "video"],
            key=lambda r: str(r.get("created_at") or ""),
            reverse=True,
        )
        for row in documents + videos:
            lineage = int(row.get("lineage_id") or row.get("id") or 0)
            previous = sorted(
                archived_by_lineage.get(lineage, []),
                key=lambda r: str(r.get("created_at") or ""),
                reverse=True,
            )
            _render_tutorial_card(row, previous, client, reload_fn)


def _render_tutorial_card(
    row: dict[str, Any],
    previous_versions: list[dict[str, Any]],
    client: ApiClient,
    reload_fn: Any,
) -> None:
    """One current tutorial — metadata, actions, and its archived history."""
    media_type = str(row.get("media_type") or "")
    is_active = bool(row.get("is_active"))
    title = str(row.get("title") or row.get("original_filename") or f"Tutorial #{row.get('id')}")
    version = str(row.get("version") or "")
    description = str(row.get("description") or "")
    size_label = _format_size(int(row.get("size_bytes") or 0))
    uploaded_at = format_user_datetime(row.get("uploaded_at"))
    uploader = str(row.get("uploaded_by_label") or "—")
    icon = _MEDIA_TYPE_ICON.get(media_type, "description")
    media_label = _MEDIA_TYPE_LABEL.get(media_type, media_type.title())

    with ui.card().classes("w-full").style("gap: 0.75rem;"):
        with ui.row().classes("w-full items-start gap-3 no-wrap"):
            ui.icon(icon).style("font-size: 2rem; color: var(--matofx-accent);")
            with ui.column().classes("gap-1").style("flex: 1 1 0; min-width: 0;"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(title).style(
                        "font-weight: 650; font-size: 1.05rem; color: var(--matofx-body-text);"
                    )
                    _status_badge(is_active)
                    ui.chip(f"v{version}" if version else "v1", icon="label").props("outline dense")
                    ui.chip(media_label, icon=icon).props("outline dense")
                if description:
                    ui.label(description).style(
                        "color: var(--matofx-muted-text); font-size: 0.85rem;"
                    )
                ui.label(
                    f"{row.get('original_filename') or 'tutorial'} · {size_label} · "
                    f"Uploaded {uploaded_at} by {uploader}"
                ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
            with ui.row().classes("gap-1 items-center").style("flex: 0 0 auto;"):
                ui.button(
                    "Preview",
                    icon="open_in_new",
                    on_click=lambda _e=None, _id=int(row["id"]): _open_tutorial_in_new_tab(
                        client, _id
                    ),
                ).props("flat dense no-caps")
                if is_active:
                    ui.button(
                        "Deactivate",
                        icon="visibility_off",
                        on_click=lambda _e=None, _id=int(row["id"]): _do_deactivate(
                            client, _id, reload_fn
                        ),
                    ).props("flat dense no-caps")
                else:
                    ui.button(
                        "Activate",
                        icon="visibility",
                        on_click=lambda _e=None, _id=int(row["id"]): _do_activate(
                            client, _id, reload_fn
                        ),
                    ).props("flat dense no-caps color=primary")
                ui.button(
                    "Update",
                    icon="upgrade",
                    on_click=lambda _e=None, _r=row: _open_update_dialog(client, _r, reload_fn),
                ).props("flat dense no-caps color=primary").tooltip(
                    "Upload a new version; the current one is kept as history"
                )

        if previous_versions:
            _render_previous_versions(previous_versions, client)


def _render_previous_versions(
    previous_versions: list[dict[str, Any]],
    client: ApiClient,
) -> None:
    """Collapsible list of archived previous versions (preview-only)."""
    label = (
        f"Previous version ({len(previous_versions)})"
        if len(previous_versions) == 1
        else f"Previous versions ({len(previous_versions)})"
    )
    expansion = (
        ui.expansion(label, icon="history")
        .classes("w-full")
        .style("border: 1px solid var(--matofx-card-border); border-radius: 10px;")
    )
    with expansion, ui.column().classes("w-full gap-1").style("padding: 0.2rem 0.2rem 0.4rem;"):
        for prev in previous_versions:
            version = str(prev.get("version") or "")
            uploaded_at = format_user_datetime(prev.get("uploaded_at"))
            filename = str(prev.get("original_filename") or "tutorial")
            size_label = _format_size(int(prev.get("size_bytes") or 0))
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.chip(f"v{version}" if version else "v1", icon="label").props("outline dense")
                ui.label(
                    f"{prev.get('title') or filename} — {filename} · {size_label} · "
                    f"Archived {uploaded_at}"
                ).style(
                    "color: var(--matofx-muted-text); font-size: 0.8rem; flex: 1 1 0; "
                    "min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
                )
                ui.button(
                    "Preview",
                    icon="open_in_new",
                    on_click=lambda _e=None, _id=int(prev["id"]): _open_tutorial_in_new_tab(
                        client, _id
                    ),
                ).props("flat dense no-caps")


def _status_badge(is_active: bool) -> None:
    text = "Active" if is_active else "Inactive"
    color = "positive" if is_active else "grey"
    ui.chip(text).props(f"color={color} text-color=white dense")


# ---------- actions ------------------------------------------------------


async def _do_activate(client: ApiClient, tutorial_id: int, reload_fn: Any) -> None:
    try:
        await client.activate_tutorial(tutorial_id)
    except ApiError as exc:
        ui.notify(f"Couldn't activate: {exc.detail}", type="negative")
        return
    ui.notify("Tutorial is now visible on the Home page.", type="positive")
    await reload_fn()


async def _do_deactivate(client: ApiClient, tutorial_id: int, reload_fn: Any) -> None:
    try:
        await client.deactivate_tutorial(tutorial_id)
    except ApiError as exc:
        ui.notify(f"Couldn't deactivate: {exc.detail}", type="negative")
        return
    ui.notify("Tutorial hidden from the Home page.", type="info")
    await reload_fn()


# ---------- update dialog ------------------------------------------------


def _suggested_next_version(current: str) -> str:
    """Suggest the next version label: bump a trailing integer, else reuse.

    ``"3"`` → ``"4"``; ``"v2"`` → ``"v3"``; ``"Rev B"`` → ``"Rev B"`` (no
    trailing number to bump, so the admin types their own).
    """
    import re  # noqa: PLC0415

    match = re.search(r"(\d+)\s*$", current)
    if match:
        bumped = str(int(match.group(1)) + 1)
        return current[: match.start(1)] + bumped + current[match.end(1) :]
    return current


def _open_update_dialog(client: ApiClient, row: dict[str, Any], reload_fn: Any) -> None:
    """Publish-a-new-version form. Scope + type are fixed by the source row.

    Pre-fills the title/description from the current version and suggests
    the next version label; the admin attaches the replacement file. On
    submit the old version is archived and the new one takes its place.
    """
    state: dict[str, Any] = {"file_bytes": None, "file_name": None, "mime_type": None}
    media_type = str(row.get("media_type") or "document")
    media_label = _MEDIA_TYPE_LABEL.get(media_type, media_type.title())
    title_text = str(row.get("title") or "")
    scope_text = "General"
    if row.get("tool_alias"):
        scope_text = f"Tool: {row.get('tool_name') or row.get('tool_alias')}"
    elif row.get("workflow_alias"):
        scope_text = f"Workflow: {row.get('workflow_name') or row.get('workflow_alias')}"

    with ui.dialog() as dialog, ui.card().style("min-width: 520px;"):
        ui.label("Publish a new version").style("font-size: 1.2rem; font-weight: 650;")
        ui.label(
            f'Replacing "{title_text}". The current version is kept as an '
            "archived previous version — it isn't deleted."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        with ui.row().classes("items-center gap-2"):
            ui.chip(scope_text, icon="folder").props("outline dense")
            ui.chip(media_label, icon=_MEDIA_TYPE_ICON.get(media_type, "description")).props(
                "outline dense"
            )

        title_input = ui.input("Title", value=title_text).classes("w-full").props("dense")
        version_input = (
            ui.input(
                "New version label",
                value=_suggested_next_version(str(row.get("version") or "1")),
                placeholder="Free-form, shown next to the title",
            )
            .classes("w-full")
            .props("dense")
        )
        description_input = (
            ui.textarea("Description (optional)", value=str(row.get("description") or ""))
            .classes("w-full")
            .props("dense autogrow")
        )

        upload_status = ui.label("").style("color: var(--matofx-muted-text); font-size: 0.85rem;")

        async def _on_upload(event: events.UploadEventArguments) -> None:
            data = await event.file.read()
            state["file_bytes"] = data
            state["file_name"] = event.file.name
            state["mime_type"] = event.file.content_type or "application/octet-stream"
            kb = len(data) / 1024
            size = f"{kb:.1f} KiB" if kb < 1024 else f"{kb / 1024:.2f} MiB"
            upload_status.text = f"Ready to upload: {event.file.name} ({size})"

        upload = ui.upload(
            on_upload=_on_upload,
            label="Choose the replacement file",
            max_files=1,
            auto_upload=True,
        ).classes("w-full")
        upload.props("accept=.pdf,video/*" if media_type == "document" else "accept=video/*")

        message = ui.label("").style("color: var(--matofx-error-text);")

        async def _submit() -> None:
            message.text = ""
            if not state["file_bytes"]:
                message.text = "Please attach the new file before submitting."
                return
            try:
                await client.update_tutorial(
                    int(row["id"]),
                    filename=str(state["file_name"]),
                    file_bytes=bytes(state["file_bytes"]),
                    mime_type=str(state["mime_type"]),
                    version=str(version_input.value or ""),
                    title=str(title_input.value or ""),
                    description=str(description_input.value or ""),
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("New version published. The old one is kept as history.", type="positive")
            await reload_fn()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button("Publish new version", on_click=_submit).props("color=primary no-caps")
    dialog.open()


# ---------- upload dialog ------------------------------------------------


def _open_upload_dialog(client: ApiClient, reload_fn: Any) -> None:
    """Multipart upload form for a brand-new tutorial: scope + type + metadata + file.

    The scope picker is two-step on purpose: a radio (General / Tool /
    Workflow) chooses *which kind* of scope, and a dependent select
    fills with the matching aliases. Keeps the dropdown short and the
    intent obvious — the alternative single dropdown grouped both
    pools together and made it easy to attach a tutorial to the wrong
    kind of thing.
    """
    state: dict[str, Any] = {"file_bytes": None, "file_name": None, "mime_type": None}

    with ui.dialog() as dialog, ui.card().style("min-width: 520px;"):
        ui.label("Upload a new tutorial").style("font-size: 1.2rem; font-weight: 650;")

        scope_kind = ui.radio(
            options={
                "general": "General",
                "tool": "Tool",
                "workflow": "Workflow",
            },
            value="general",
        ).props("inline")
        ui.label(
            "'General' tutorials show in the left rail; tool/workflow "
            "ones surface on the matching Quick Access card."
        ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")

        # Two sibling selects — only one visible at a time, driven by scope_kind.
        # Options are populated lazily after the dialog opens (see _refresh_scope_options).
        tool_select = (
            ui.select(options={}, label="Tool", with_input=True).classes("w-full").props("dense")
        )
        workflow_select = (
            ui.select(options={}, label="Workflow", with_input=True)
            .classes("w-full")
            .props("dense")
        )
        tool_select.visible = False
        workflow_select.visible = False

        def _refresh_scope_visibility() -> None:
            kind = str(scope_kind.value or "general")
            tool_select.visible = kind == "tool"
            workflow_select.visible = kind == "workflow"

        scope_kind.on_value_change(lambda _e: _refresh_scope_visibility())

        media_select = (
            ui.select(
                options={"document": "Document (PDF)", "video": "Video (mp4/webm/mov/…)"},
                label="Tutorial type",
                value="document",
            )
            .classes("w-full")
            .props("dense")
        )
        title_input = (
            ui.input("Title *", placeholder="e.g. Getting started with MATOFX SPDM")
            .classes("w-full")
            .props("dense")
        )
        version_input = (
            ui.input("Version label", value="1", placeholder="Free-form, shown next to the title")
            .classes("w-full")
            .props("dense")
        )
        description_input = (
            ui.textarea("Description (optional)").classes("w-full").props("dense autogrow")
        )
        activate_switch = ui.switch("Make this the active version once uploaded", value=True)

        upload_status = ui.label("").style("color: var(--matofx-muted-text); font-size: 0.85rem;")

        async def _on_upload(event: events.UploadEventArguments) -> None:
            # NiceGUI 3.x: event.file is a FileUpload; read() is async.
            data = await event.file.read()
            state["file_bytes"] = data
            state["file_name"] = event.file.name
            state["mime_type"] = event.file.content_type or "application/octet-stream"
            kb = len(data) / 1024
            size = f"{kb:.1f} KiB" if kb < 1024 else f"{kb / 1024:.2f} MiB"
            upload_status.text = f"Ready to upload: {event.file.name} ({size})"

        upload = ui.upload(
            on_upload=_on_upload,
            label="Choose a file",
            max_files=1,
            auto_upload=True,
        ).classes("w-full")
        upload.props("accept=.pdf,video/*")

        message = ui.label("").style("color: var(--matofx-error-text);")

        async def _refresh_scope_options() -> None:
            try:
                tools = await client.list_admin_tools()
                workflows = await client.list_admin_workflows()
            except ApiError as exc:
                message.text = (
                    f"Couldn't load the tools / workflows list: {exc.detail}. "
                    "You can still upload a general tutorial."
                )
                return

            tool_options: dict[str, str] = {}
            for tool in tools:
                if tool.get("archived_at"):
                    continue
                alias = str(tool.get("alias") or "")
                name = str(tool.get("name") or alias)
                tool_version = str(tool.get("version") or "")
                label = f"{name} v{tool_version}" if tool_version else name
                tool_options[alias] = label
            tool_select.options = tool_options
            tool_select.update()

            workflow_options: dict[str, str] = {}
            for workflow in workflows:
                if workflow.get("archived_at"):
                    continue
                alias = str(workflow.get("alias") or "")
                name = str(workflow.get("name") or alias)
                wf_version = str(workflow.get("version") or "")
                label = f"{name} v{wf_version}" if wf_version else name
                workflow_options[alias] = label
            workflow_select.options = workflow_options
            workflow_select.update()

        async def _submit() -> None:
            message.text = ""
            if not state["file_bytes"]:
                message.text = "Please attach a file before submitting."
                return
            if not (title_input.value or "").strip():
                message.text = "Please give the tutorial a title."
                return
            kind = str(scope_kind.value or "general")
            tool_alias: str | None = None
            workflow_alias: str | None = None
            if kind == "tool":
                tool_alias = str(tool_select.value or "") or None
                if tool_alias is None:
                    message.text = "Pick which tool this tutorial belongs to."
                    return
            elif kind == "workflow":
                workflow_alias = str(workflow_select.value or "") or None
                if workflow_alias is None:
                    message.text = "Pick which workflow this tutorial belongs to."
                    return
            try:
                await client.upload_tutorial(
                    media_type=str(media_select.value or "document"),
                    title=str(title_input.value),
                    version=str(version_input.value or "1"),
                    description=str(description_input.value or ""),
                    tool_alias=tool_alias,
                    workflow_alias=workflow_alias,
                    filename=str(state["file_name"]),
                    file_bytes=bytes(state["file_bytes"]),
                    mime_type=str(state["mime_type"]),
                    activate=bool(activate_switch.value),
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Tutorial uploaded.", type="positive")
            await reload_fn()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button("Upload", on_click=_submit).props("color=primary no-caps")

        ui.timer(0.05, _refresh_scope_options, once=True)
    dialog.open()


# ---------- preview ------------------------------------------------------


async def _open_tutorial_in_new_tab(client: ApiClient, tutorial_id: int) -> None:
    """Fetch the file bytes via the authenticated API client, open as a blob URL.

    The download endpoint requires a bearer token; an
    ``<a target="_blank">`` would lose the header, so we go via the
    in-process client and hand the bytes to the browser as a blob.
    See ``feedback_no_kaleido_browser_print_pdf.md`` for the broader
    pattern.
    """
    import base64  # noqa: PLC0415
    import json  # noqa: PLC0415

    try:
        # Reuse the streaming endpoint by adapting the existing API call.
        async with await client._client() as http:  # noqa: SLF001
            response = await http.get(
                f"/api/tutorials/{tutorial_id}/file",
                headers=client._headers(),  # noqa: SLF001
            )
            response.raise_for_status()
            data = response.content
            mime = response.headers.get("content-type", "application/octet-stream")
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Couldn't open the tutorial: {exc}", type="negative")
        return

    b64 = base64.b64encode(data).decode("ascii")
    js = (
        "(function() {"
        f"  const b64 = {json.dumps(b64)};"
        f"  const mime = {json.dumps(mime)};"
        "  const bin = atob(b64);"
        "  const bytes = new Uint8Array(bin.length);"
        "  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);"
        "  const blob = new Blob([bytes], {type: mime});"
        "  const url = URL.createObjectURL(blob);"
        "  window.open(url, '_blank');"
        "})();"
    )
    ui.run_javascript(js)


# ---------- helpers ------------------------------------------------------


def _format_size(size_bytes: int) -> str:
    """Pretty-print a byte count as KiB / MiB / GiB."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KiB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MiB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GiB"
