"""File library UI (ADR-0006 §8 / designs/file-library.md §8).

Two surfaces, both behind the ``features.file_library`` flag (the nav
entry + these pages are absent when it's off, matching the API which
404s every route — a disabled-feature visit redirects home):

- ``GET /library`` — the workspace: filter + browse the files you can
  see, and create a new one (upload its first version, choose scope +
  tool assignment, name an approver when it lands in global).
- ``GET /library/{id}`` — the file detail: version history (with
  download + add-version), tool assignments (add / remove), the
  governed scope-change (with approver picker), entry-metadata edit,
  and retire.

Approve / reject happen in the existing Approvals inbox (the
directed-approver gate), not here. UI gating uses the caller's resolved
``library.curate`` permission so the curator-only hints are shown to the
right people; the service stays the authoritative gate regardless.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nicegui import ui

from matofx.ui._features import file_library_enabled
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token

_LIBRARY_CURATE = "library.curate"
_LIBRARY_CREATE = "library.create"

_SCOPE_LABEL: dict[str, str] = {
    "personal": "Personal",
    "project": "Project",
    "global": "Global",
}
_SCOPE_COLOR: dict[str, str] = {
    "personal": "grey",
    "project": "info",
    "global": "primary",
}
_STATE_COLOR: dict[str, str] = {
    "active": "positive",
    "pending": "warning",
    "retired": "grey",
}
_PUBLISH_LABEL: dict[str, str] = {
    "draft": "Draft",
    "pending": "Pending review",
    "published": "Published",
    "rejected": "Rejected",
}
_PUBLISH_COLOR: dict[str, str] = {
    "draft": "grey",
    "pending": "warning",
    "published": "positive",
    "rejected": "negative",
}


def _client() -> ApiClient:
    return ApiClient(get_app(), token=current_token())


def _feature_ok() -> bool:
    """Redirect home when the feature is off (page *absent*, design §12)."""
    if not file_library_enabled():
        ui.navigate.to("/home")
        return False
    return True


def _scope_chip(scope: str) -> None:
    ui.chip(_SCOPE_LABEL.get(scope, scope.title())).props(
        f"color={_SCOPE_COLOR.get(scope, 'grey')} text-color=white dense"
    )


def _state_chip(state: str) -> None:
    if state == "active":
        return  # the common case reads as no chip — only flag the exceptions
    ui.chip(state.title()).props(f"color={_STATE_COLOR.get(state, 'grey')} text-color=white dense")


def _publish_chip(publish_state: str) -> None:
    ui.chip(_PUBLISH_LABEL.get(publish_state, publish_state.title())).props(
        f"color={_PUBLISH_COLOR.get(publish_state, 'grey')} text-color=white dense"
    )


def _parse_tags(text: str) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


# --- tool / field label resolution -----------------------------------
#
# A file's assignments are stored as raw ``tool_alias[, field_name]``.
# The UI always shows *labels*: the tool's display name and the input
# field's descriptor label. The tool name comes from ``list_tools``;
# the field label needs the tool's descriptor (``get_tool``), so we
# fetch those once per distinct alias and cache them.

_ANY_FIELD_LABEL = "any compatible file field"


def _tool_file_inputs(descriptor: dict[str, Any] | None) -> list[dict[str, str]]:
    """The tool's **file**-type inputs as ``[{name, label}]`` (empty if none)."""
    if not descriptor:
        return []
    out: list[dict[str, str]] = []
    for i in descriptor.get("inputs") or []:
        if str(i.get("type") or "") == "file":
            name = str(i.get("name") or "")
            out.append({"name": name, "label": str(i.get("label") or name)})
    return out


def _field_label(descriptor: dict[str, Any] | None, field_name: str | None) -> str:
    """Resolve a stored ``field_name`` to its descriptor label.

    A NULL field means "any compatible file field". An unknown name
    (e.g. the tool changed since assignment) falls back to the raw name.
    """
    if not field_name:
        return _ANY_FIELD_LABEL
    for i in (descriptor or {}).get("inputs") or []:
        if str(i.get("name") or "") == field_name:
            return str(i.get("label") or field_name)
    return field_name


async def _fetch_descriptors(client: ApiClient, aliases: set[str]) -> dict[str, dict[str, Any]]:
    """Fetch each tool's descriptor once, concurrently; skip failures."""
    needed = sorted(a for a in aliases if a)
    if not needed:
        return {}
    results = await asyncio.gather(*[client.get_tool(a) for a in needed], return_exceptions=True)
    return {a: r for a, r in zip(needed, results, strict=True) if not isinstance(r, BaseException)}


# ===================== workspace (list + create) =====================


_SCOPE_GROUP_ORDER = ("personal", "project", "global")
_GROUP_BY_OPTS = {"none": "No grouping", "scope": "Group by scope", "tool": "Group by tool"}


@ui.page("/library")
async def library_page() -> None:
    """The File library workspace — search/filter your files + create one."""
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    try:
        me = await client.me()
    except ApiError:
        me = {}
    perms = me.get("permissions") or []
    is_curator = _LIBRARY_CURATE in perms
    # Adding/versioning files needs library.create (or curate — curators can
    # always contribute); a viewer just browses the catalogue.
    can_create = is_curator or _LIBRARY_CREATE in perms
    try:
        tools = await client.list_tools()
    except ApiError:
        tools = []
    tool_name = {str(t["alias"]): str(t.get("name") or t["alias"]) for t in tools}

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-0"):
                ui.label("File Library").style("font-size: 1.5rem; font-weight: 600;")
                ui.label(
                    "Reusable, governed tool-input files — geometry, meshes, "
                    "boundary conditions — with immutable, versioned history."
                ).style("color: #888;")
            if can_create:
                ui.button(
                    "New library file",
                    icon="add",
                    on_click=lambda: _open_create_dialog(client, is_curator),
                ).props("color=primary")

        filters: dict[str, Any] = {"scope": None, "tool": None, "include_retired": False}
        # Enriched files cached between keystrokes so the search box and
        # the group-by selector filter/regroup in memory (cumulative with
        # the server-side scope/tool/retired filters) without refetching.
        cache: dict[str, list[dict[str, Any]]] = {"files": []}

        # Filter row — search + dropdowns, all cumulative (above results).
        with ui.card().classes("w-full"), ui.row().classes("w-full items-center gap-3 flex-wrap"):
            search = (
                ui.input("Search by name, description, tag or tool")
                .props("clearable dense")
                .classes("grow")
            )
            scope_sel = (
                ui.select(
                    {
                        None: "All scopes",
                        "personal": "Personal",
                        "project": "Project",
                        "global": "Global",
                    },
                    value=None,
                    label="Scope",
                )
                .props("dense")
                .classes("w-36")
            )
            tool_opts: dict[Any, str] = {None: "All tools"}
            tool_opts.update(tool_name)
            tool_sel = ui.select(tool_opts, value=None, label="Tool").props("dense").classes("w-48")
            group_sel = (
                ui.select(_GROUP_BY_OPTS, value="none", label="Group by")
                .props("dense")
                .classes("w-40")
            )
            retired_cb = ui.checkbox("Include retired")

        results = ui.column().classes("w-full gap-2")

        def _render() -> None:
            results.clear()
            term = (search.value or "").strip().lower()
            shown = [e for e in cache["files"] if not term or term in e["search_blob"]]
            with results:
                if not shown:
                    ui.label(
                        "No files match your search and filters."
                        if (term or filters["scope"] or filters["tool"])
                        else "No library files yet — create one with “New library file”."
                    ).style("color: #888;")
                    return
                _render_grouped(shown, str(group_sel.value or "none"))

        async def _reload() -> None:
            results.clear()
            try:
                files = await client.list_library_files(
                    scope=filters["scope"],
                    tool=filters["tool"],
                    include_retired=bool(filters["include_retired"]),
                )
            except ApiError as exc:
                cache["files"] = []
                with results:
                    ui.label(f"Couldn't load library files: {exc.detail}").style(
                        "color: var(--matofx-error-text);"
                    )
                return
            cache["files"] = await _enrich_files(client, files, tool_name)
            _render()

        def _apply_filters(_e: Any = None) -> None:
            filters["scope"] = scope_sel.value
            filters["tool"] = tool_sel.value
            filters["include_retired"] = bool(retired_cb.value)
            ui.timer(0.0, _reload, once=True)

        scope_sel.on("update:model-value", _apply_filters)
        tool_sel.on("update:model-value", _apply_filters)
        retired_cb.on("update:model-value", _apply_filters)
        search.on("update:model-value", lambda _e=None: _render())
        group_sel.on("update:model-value", lambda _e=None: _render())
        await _reload()


async def _enrich_files(
    client: ApiClient, files: list[dict[str, Any]], tool_name: dict[str, str]
) -> list[dict[str, Any]]:
    """Resolve each file's tool/field labels and build its search blob.

    The list response carries the raw ``tools`` assignments; we fetch the
    descriptors of the distinct assigned tools once to turn field names
    into labels, then precompute a lowercased blob (name + description +
    tags + tool/field labels) the search box matches against in memory.
    """
    aliases = {
        str(a.get("tool_alias") or "")
        for f in files
        for a in (f.get("tools") or [])
        if a.get("tool_alias")
    }
    descriptors = await _fetch_descriptors(client, aliases)
    enriched: list[dict[str, Any]] = []
    for f in files:
        assignments: list[dict[str, str]] = []
        for a in f.get("tools") or []:
            alias = str(a.get("tool_alias") or "")
            assignments.append(
                {
                    "alias": alias,
                    "tool_label": tool_name.get(alias, alias),
                    "field_label": _field_label(descriptors.get(alias), a.get("field_name")),
                }
            )
        parts = [
            str(f.get("name") or ""),
            str(f.get("description") or ""),
            str(f.get("file_kind") or ""),
            *[str(t) for t in (f.get("tags") or [])],
        ]
        for a in assignments:
            parts += [a["tool_label"], a["field_label"]]
        enriched.append(
            {"file": f, "assignments": assignments, "search_blob": " ".join(parts).lower()}
        )
    return enriched


def _render_grouped(entries: list[dict[str, Any]], group_by: str) -> None:
    """Render cards, optionally bucketed by scope or by assigned tool."""
    if group_by == "scope":
        groups: dict[str, list[dict[str, Any]]] = {}
        for e in entries:
            groups.setdefault(str(e["file"].get("scope") or "personal"), []).append(e)
        order = [s for s in _SCOPE_GROUP_ORDER if s in groups] + [
            s for s in sorted(groups) if s not in _SCOPE_GROUP_ORDER
        ]
        for scope in order:
            _render_group_header(_SCOPE_LABEL.get(scope, scope.title()), len(groups[scope]))
            for e in groups[scope]:
                _render_file_card(e)
    elif group_by == "tool":
        # A file with several tools appears under each; unassigned files
        # fall under a trailing "Unassigned" group.
        groups = {}
        for e in entries:
            if e["assignments"]:
                for a in e["assignments"]:
                    groups.setdefault(a["tool_label"], []).append(e)
            else:
                groups.setdefault("Unassigned", []).append(e)
        for label in sorted(groups, key=lambda x: (x == "Unassigned", x.lower())):
            _render_group_header(label, len(groups[label]))
            for e in groups[label]:
                _render_file_card(e)
    else:
        for e in entries:
            _render_file_card(e)


def _render_group_header(label: str, count: int) -> None:
    ui.label(f"{label}  ({count})").style(
        "font-weight: 600; color: var(--matofx-muted-text); font-size: 0.85rem; margin-top: 0.4rem;"
    )


def _render_file_card(entry: dict[str, Any]) -> None:
    """One file as a Projects-style card: identity, metadata, assignments."""
    f = entry["file"]
    fid = int(f.get("id") or 0)
    name = str(f.get("name") or f"File #{fid}")
    kind = str(f.get("file_kind") or "")
    desc = str(f.get("description") or "").strip()
    version_count = int(f.get("version_count") or 0)
    with (
        ui.card()
        .classes("w-full cursor-pointer hover:bg-gray-50")
        .on("click", lambda _e=None, _id=fid: ui.navigate.to(f"/library/{_id}"))
    ):
        with ui.column().classes("gap-1 w-full"):
            with ui.row().classes("w-full items-center gap-2"):
                ui.label(name).style("font-size: 1.05rem; font-weight: 600;").classes("flex-grow")
                if version_count:
                    ui.badge(f"v{version_count}").props("color=grey text-color=white").style(
                        "font-size: 0.7rem;"
                    )
                _scope_chip(str(f.get("scope") or "personal"))
                _state_chip(str(f.get("state") or "active"))
            if kind or f.get("tags"):
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    if kind:
                        ui.label(kind).style("color: #888; font-size: 0.8rem;")
                    for tag in list(f.get("tags") or [])[:6]:
                        ui.chip(str(tag)).props("dense outline")
            if desc:
                ui.label(desc[:160] + ("…" if len(desc) > 160 else "")).style(
                    "color: #555; font-size: 0.85rem;"
                )
            # Tool assignments — always labels, never aliases/field names.
            for a in entry["assignments"]:
                with ui.row().classes("items-center gap-1 flex-wrap"):
                    ui.icon("build").style("color: #777; font-size: 0.95rem;")
                    ui.label(f"{a['tool_label']} · {a['field_label']}").style(
                        "color: #777; font-size: 0.8rem;"
                    )


# ---------------------------- create ---------------------------------


def _open_create_dialog(client: ApiClient, is_curator: bool) -> None:
    upload_state: dict[str, Any] = {"filename": "", "bytes": b""}

    with ui.dialog() as dialog, ui.card().style("min-width: 560px; max-width: 92vw;"):
        ui.label("New library file").style("font-size: 1.1rem; font-weight: 600;")
        name_in = ui.input("Name *").classes("w-full")
        desc_in = ui.textarea("Description").classes("w-full")
        prov_in = ui.input("Provenance (where it came from) *").classes("w-full")
        kind_in = ui.input("Kind (e.g. geometry, mesh)").classes("w-full")
        tags_in = ui.input("Tags (comma-separated)").classes("w-full")

        scope_sel = ui.select(
            {"personal": "Personal", "project": "Project", "global": "Global"},
            value="personal",
            label="Scope",
        ).classes("w-full")
        scope_extra = ui.column().classes("w-full gap-2")

        tool_sel = ui.select({}, label="Assign to tool *").props("dense").classes("w-full")
        field_sel = (
            ui.select({None: _ANY_FIELD_LABEL}, value=None, label="Field")
            .props("dense")
            .classes("w-full")
        )
        desc_cache: dict[str, dict[str, Any]] = {}

        async def _on_tool_change(_e: Any = None) -> None:
            # Populate Field with the selected tool's *file* inputs, each
            # shown as "Label (name)"; the stored value is the input name
            # (or None = any compatible file field).
            opts: dict[Any, str] = {None: _ANY_FIELD_LABEL}
            alias = str(tool_sel.value or "")
            if alias:
                if alias not in desc_cache:
                    try:
                        desc_cache[alias] = await client.get_tool(alias)
                    except ApiError:
                        desc_cache[alias] = {}
                for inp in _tool_file_inputs(desc_cache[alias]):
                    opts[inp["name"]] = f"{inp['label']} ({inp['name']})"
            field_sel.options = opts
            field_sel.value = None
            field_sel.update()

        tool_sel.on("update:model-value", lambda _e=None: ui.timer(0.0, _on_tool_change, once=True))

        file_label = ui.label("No file selected.").style("color: #888; font-size: 0.85rem;")

        async def _on_upload(event: Any) -> None:
            # NiceGUI's upload event carries a ``file`` attribute (a
            # SpooledTemporaryFile-alike); ``read()`` returns the bytes.
            # The older ``e.name`` / ``e.content`` shape AttributeErrors
            # silently inside NiceGUI's loop (the upload looks dead).
            try:
                data = await event.file.read()
                name = event.file.name or "upload"
            except AttributeError:
                ui.notify("Couldn't read that upload (unexpected event shape).", type="negative")
                return
            upload_state["filename"] = name
            upload_state["bytes"] = data
            file_label.set_text(f"Selected: {name} ({len(data):,} bytes)")

        ui.upload(
            on_upload=_on_upload, label="Upload the first version", auto_upload=True, max_files=1
        ).classes("w-full")
        message = ui.label("").style("color: var(--matofx-error-text);")

        ctx: dict[str, Any] = {"project_sel": None, "approver_sel": None}

        async def _rebuild_scope_extra() -> None:
            scope_extra.clear()
            ctx["project_sel"] = None
            ctx["approver_sel"] = None
            scope = str(scope_sel.value or "personal")
            with scope_extra:
                if scope == "project":
                    try:
                        projects = await client.list_projects()
                    except ApiError as exc:
                        ui.label(f"Couldn't load projects: {exc.detail}").style("color: #888;")
                        return
                    opts = {int(p["id"]): str(p.get("name") or p["id"]) for p in projects}
                    ctx["project_sel"] = ui.select(opts, label="Project").classes("w-full")
                elif scope == "global":
                    await _add_approver_picker(client, scope_extra, ctx, is_curator)

        scope_sel.on(
            "update:model-value", lambda _e=None: ui.timer(0.0, _rebuild_scope_extra, once=True)
        )

        async def _load_tools() -> None:
            try:
                tools = await client.list_tools()
            except ApiError:
                tools = []
            tool_sel.options = {str(t["alias"]): str(t.get("name") or t["alias"]) for t in tools}
            tool_sel.update()

        async def _submit() -> None:
            message.text = ""
            name = (name_in.value or "").strip()
            if not name:
                message.text = "Give the file a name."
                return
            if not (prov_in.value or "").strip():
                message.text = "Add a provenance note (where the file came from)."
                return
            if not tool_sel.value:
                message.text = "Assign the file to at least one tool."
                return
            if not upload_state["bytes"]:
                message.text = "Upload the file's first version."
                return
            scope = str(scope_sel.value or "personal")
            meta: dict[str, Any] = {
                "name": name,
                "description": (desc_in.value or "").strip(),
                "provenance": (prov_in.value or "").strip(),
                "scope": scope,
                "file_kind": (kind_in.value or "").strip() or None,
                "tags": _parse_tags(tags_in.value or ""),
                "tool_assignments": [
                    {
                        "tool_alias": str(tool_sel.value),
                        "field_name": (str(field_sel.value) if field_sel.value else None),
                    }
                ],
            }
            if scope == "project":
                psel = ctx.get("project_sel")
                if psel is None or not psel.value:
                    message.text = "Pick a project for a project-scoped file."
                    return
                meta["project_id"] = int(psel.value)
            if scope == "global":
                asel = ctx.get("approver_sel")
                if asel is not None and asel.value:
                    meta["approver_user_id"] = int(asel.value)
            try:
                created = await client.create_library_file(
                    metadata=meta,
                    filename=str(upload_state["filename"] or "upload"),
                    content=bytes(upload_state["bytes"]),
                )
            except ApiError as exc:
                message.text = exc.detail or "Couldn't create the file."
                return
            dialog.close()
            ui.notify("Library file created.", type="positive")
            ui.navigate.to(f"/library/{int(created['file']['id'])}")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create", icon="save", on_click=_submit).props("color=primary")

    dialog.open()
    ui.timer(0.0, _load_tools, once=True)


async def _add_approver_picker(
    client: ApiClient, box: ui.column, ctx: dict[str, Any], is_curator: bool
) -> None:
    """Reviewer picker for a global landing — required when the gate is on."""
    try:
        approvers = await client.library_approvers()
    except ApiError:
        approvers = []
    with box:
        if approvers:
            opts = {
                int(a["user_id"]): f"{a.get('full_name') or a['username']} ({a['username']})"
                for a in approvers
            }
            ctx["approver_sel"] = ui.select(
                opts, label="Reviewer to approve the global publish"
            ).classes("w-full")
            ui.label(
                "Publishing to the global library needs one curator (not you) to review it. "
                "If your install lets curators publish directly, you can leave this blank."
                if is_curator
                else "Publishing to the global library needs one curator (not you) to review it."
            ).style("color: #888; font-size: 0.8rem;")
        else:
            ui.label(
                "No other library curators are available to review a global publish — "
                "ask an administrator to grant the library-curator capability."
            ).style("color: #888; font-size: 0.8rem;")


# ============================ detail =================================


@ui.page("/library/{file_id}")
async def library_detail_page(file_id: int) -> None:
    """One library file — versions, tools, scope, metadata, retire."""
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    try:
        me = await client.me()
    except ApiError:
        me = {}
    is_curator = _LIBRARY_CURATE in (me.get("permissions") or [])

    with ui.column().classes("w-full max-w-4xl mx-auto p-6 gap-3"):
        body = ui.column().classes("w-full gap-3")
    await _render_detail(client, int(file_id), body, is_curator)


async def _render_detail(
    client: ApiClient, file_id: int, body: ui.column, is_curator: bool
) -> None:
    body.clear()
    try:
        detail = await client.get_library_file(file_id)
    except ApiError as exc:
        with body:
            ui.label(f"Couldn't load this file: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
        return

    async def _reload() -> None:
        await _render_detail(client, file_id, body, is_curator)

    f = detail.get("file") or {}
    versions = detail.get("versions") or []
    tools = detail.get("tools") or []
    # Resolve assigned tool/field labels (shown instead of aliases/names).
    aliases = {str(t.get("tool_alias") or "") for t in tools if t.get("tool_alias")}
    try:
        all_tools = await client.list_tools()
    except ApiError:
        all_tools = []
    tool_ctx = {
        "name": {str(t["alias"]): str(t.get("name") or t["alias"]) for t in all_tools},
        "desc": await _fetch_descriptors(client, aliases),
    }
    with body:
        _render_detail_header(client, f, _reload)
        _render_metadata_card(client, f, _reload)
        _render_versions_card(client, file_id, f, versions, is_curator, _reload)
        _render_tools_card(client, file_id, f, tools, tool_ctx, _reload)
        _render_scope_card(client, f, is_curator, _reload)
        # Provenance for the latest version (ADR-0010 slice 5). The
        # design's §7 entry-points table calls for a version selector
        # inside the tab when a file has multiple versions — slice 5
        # ships with the latest-version-only path; a per-version
        # dropdown lands when the V&V row (ADR-0011) starts asking
        # which version a record refers to.
        if versions:
            latest_version_id = int(versions[0].get("id") or 0)
            if latest_version_id > 0:
                from matofx.ui.lineage_view import (  # noqa: PLC0415
                    render_provenance_section,
                )

                render_provenance_section(
                    kind="library_file_version",
                    obj_id=latest_version_id,
                    label=f"Provenance (version {versions[0].get('ordinal') or '?'})",
                )


def _render_detail_header(client: ApiClient, f: dict[str, Any], reload: Any) -> None:
    fid = int(f.get("id") or 0)
    name = str(f.get("name") or f"File #{fid}")
    state = str(f.get("state") or "active")
    with ui.row().classes("w-full items-center justify-between gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/library")).props(
                "flat round dense"
            ).tooltip("Back to the File library")
            ui.label(name).style("font-size: 1.4rem; font-weight: 600;")
            _scope_chip(str(f.get("scope") or "personal"))
            _state_chip(state)
        if state != "retired":
            ui.button(
                "Retire",
                icon="archive",
                on_click=lambda: _confirm_retire(client, fid, reload),
            ).props("flat dense color=negative").tooltip(
                "Hide from pickers and lock edits. Nothing is deleted — reversible."
            )
        else:
            ui.button(
                "Restore",
                icon="unarchive",
                on_click=lambda: _confirm_restore(client, fid, reload),
            ).props("flat dense color=primary").tooltip(
                "Reactivate this file so it can be picked and edited again."
            )


def _render_metadata_card(client: ApiClient, f: dict[str, Any], reload: Any) -> None:
    fid = int(f.get("id") or 0)
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Details").style("font-weight: 600;")
            ui.button(
                "Edit",
                icon="edit",
                on_click=lambda: _open_metadata_dialog(client, f, reload),
            ).props("flat dense")
        desc = str(f.get("description") or "").strip()
        ui.label(desc or "No description.").style("color: #888;" if not desc else "")
        with ui.row().classes("items-center gap-4 flex-wrap"):
            kind = str(f.get("file_kind") or "")
            if kind:
                ui.label(f"Kind: {kind}").style("color: #888; font-size: 0.85rem;")
            handling = str(f.get("handling_label") or "")
            if handling:
                ui.label(f"Handling: {handling}").style("color: #888; font-size: 0.85rem;")
            for tag in list(f.get("tags") or []):
                ui.chip(str(tag)).props("dense outline")
        ui.label(
            f"Created {format_user_datetime(str(f.get('created_at') or ''))} · "
            f"owner #{f.get('owner_user_id')}"
        ).style("color: #888; font-size: 0.8rem;")
    del fid


def _render_versions_card(
    client: ApiClient,
    file_id: int,
    f: dict[str, Any],
    versions: list[dict[str, Any]],
    is_curator: bool,
    reload: Any,
) -> None:
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Versions").style("font-weight: 600;")
            if str(f.get("state")) != "retired":
                ui.button(
                    "Add version",
                    icon="upload_file",
                    on_click=lambda: _open_add_version_dialog(
                        client, file_id, f, is_curator, reload
                    ),
                ).props("flat dense")
        if not versions:
            ui.label("No versions yet.").style("color: #888;")
            return
        for v in versions:
            _render_version_row(client, file_id, v)


def _render_version_row(client: ApiClient, file_id: int, v: dict[str, Any]) -> None:
    vid = int(v.get("id") or 0)
    with ui.row().classes("w-full items-center gap-3 p-1"):
        ui.label(f"v{v.get('ordinal')}").style("font-weight: 600; flex: 0 0 2.5rem;")
        with ui.column().classes("gap-0 flex-grow"):
            ui.label(str(v.get("original_filename") or "")).style("font-size: 0.9rem;")
            prov = str(v.get("provenance") or "")
            if prov:
                ui.label(prov).style("color: #888; font-size: 0.8rem;")
        ui.label(_fmt_size(int(v.get("size_bytes") or 0))).style("color: #888; font-size: 0.8rem;")
        _publish_chip(str(v.get("publish_state") or "draft"))

        async def _download(_e: Any = None, _vid: int = vid) -> None:
            try:
                content, filename = await client.download_library_version(file_id, _vid)
            except ApiError as exc:
                ui.notify(f"Couldn't download: {exc.detail}", type="negative")
                return
            ui.download.content(content, filename)

        ui.button(icon="download", on_click=_download).props("flat dense").tooltip("Download")


def _render_tools_card(
    client: ApiClient,
    file_id: int,
    f: dict[str, Any],
    tools: list[dict[str, Any]],
    tool_ctx: dict[str, Any],
    reload: Any,
) -> None:
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Tool assignments").style("font-weight: 600;")
            if str(f.get("state")) != "retired":
                ui.button(
                    "Assign tool",
                    icon="add_link",
                    on_click=lambda: _open_assign_dialog(client, file_id, reload),
                ).props("flat dense")
        ui.label("Which tools (and optionally which file field) can pick this file.").style(
            "color: #888; font-size: 0.8rem;"
        )
        for t in tools:
            tid = int(t.get("id") or 0)
            alias = str(t.get("tool_alias") or "")
            tool_label = tool_ctx["name"].get(alias, alias)
            field_label = _field_label(tool_ctx["desc"].get(alias), t.get("field_name"))
            with ui.row().classes("w-full items-center gap-2 p-1"):
                ui.label(tool_label).classes("flex-grow")
                ui.label(field_label).style("color: #888; font-size: 0.85rem;")
                if str(f.get("state")) != "retired":
                    ui.button(
                        icon="link_off",
                        on_click=lambda _e=None, _tid=tid: _do_unassign(
                            client, file_id, _tid, reload
                        ),
                    ).props("flat dense color=negative").tooltip("Remove")


def _render_scope_card(client: ApiClient, f: dict[str, Any], is_curator: bool, reload: Any) -> None:
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.row().classes("items-center gap-2"):
                ui.label("Scope").style("font-weight: 600;")
                _scope_chip(str(f.get("scope") or "personal"))
            if str(f.get("state")) != "retired":
                ui.button(
                    "Change scope",
                    icon="swap_horiz",
                    on_click=lambda: _open_scope_dialog(client, f, is_curator, reload),
                ).props("flat dense")
        ui.label(
            "Personal = only you. Project = members of the project. "
            "Global = everyone (curator-reviewed)."
        ).style("color: #888; font-size: 0.8rem;")


# ----------------------------- dialogs -------------------------------


def _open_metadata_dialog(client: ApiClient, f: dict[str, Any], reload: Any) -> None:
    fid = int(f.get("id") or 0)
    with ui.dialog() as dialog, ui.card().style("min-width: 480px;"):
        ui.label("Edit details").style("font-size: 1.1rem; font-weight: 600;")
        name_in = ui.input("Name *", value=str(f.get("name") or "")).classes("w-full")
        desc_in = ui.textarea("Description", value=str(f.get("description") or "")).classes(
            "w-full"
        )
        kind_in = ui.input("Kind", value=str(f.get("file_kind") or "")).classes("w-full")
        tags_in = ui.input(
            "Tags (comma-separated)", value=", ".join(list(f.get("tags") or []))
        ).classes("w-full")
        handling_in = ui.input("Handling label", value=str(f.get("handling_label") or "")).classes(
            "w-full"
        )
        message = ui.label("").style("color: var(--matofx-error-text);")

        async def _save() -> None:
            body: dict[str, Any] = {
                "name": (name_in.value or "").strip(),
                "description": desc_in.value or "",
                "file_kind": (kind_in.value or "").strip(),
                "tags": _parse_tags(tags_in.value or ""),
                "handling_label": (handling_in.value or "").strip(),
                "expected_version": int(f.get("version") or 0),
            }
            try:
                await client.update_library_metadata(fid, body=body)
            except ApiError as exc:
                message.text = exc.detail or "Couldn't save."
                return
            dialog.close()
            ui.notify("Saved.", type="positive")
            await reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", icon="save", on_click=_save).props("color=primary")
    dialog.open()


def _open_add_version_dialog(
    client: ApiClient, file_id: int, f: dict[str, Any], is_curator: bool, reload: Any
) -> None:
    upload_state: dict[str, Any] = {"filename": "", "bytes": b""}
    is_global = str(f.get("scope")) == "global"
    with ui.dialog() as dialog, ui.card().style("min-width: 520px;"):
        ui.label("Add a version").style("font-size: 1.1rem; font-weight: 600;")
        prov_in = ui.input("Provenance (where it came from) *").classes("w-full")
        ref_in = ui.input("Reference (optional)").classes("w-full")
        notes_in = ui.textarea("What changed (optional)").classes("w-full")
        approver_box = ui.column().classes("w-full gap-2")
        ctx: dict[str, Any] = {"approver_sel": None}
        file_label = ui.label("No file selected.").style("color: #888; font-size: 0.85rem;")

        async def _on_upload(event: Any) -> None:
            # NiceGUI's upload event carries a ``file`` attribute (a
            # SpooledTemporaryFile-alike); ``read()`` returns the bytes.
            # The older ``e.name`` / ``e.content`` shape AttributeErrors
            # silently inside NiceGUI's loop (the upload looks dead).
            try:
                data = await event.file.read()
                name = event.file.name or "upload"
            except AttributeError:
                ui.notify("Couldn't read that upload (unexpected event shape).", type="negative")
                return
            upload_state["filename"] = name
            upload_state["bytes"] = data
            file_label.set_text(f"Selected: {name} ({len(data):,} bytes)")

        ui.upload(
            on_upload=_on_upload, label="Upload the new version", auto_upload=True, max_files=1
        ).classes("w-full")
        message = ui.label("").style("color: var(--matofx-error-text);")

        async def _maybe_approvers() -> None:
            if is_global:
                await _add_approver_picker(client, approver_box, ctx, is_curator)

        async def _save() -> None:
            message.text = ""
            if not (prov_in.value or "").strip():
                message.text = "Add a provenance note."
                return
            if not upload_state["bytes"]:
                message.text = "Upload the new version's file."
                return
            meta: dict[str, Any] = {
                "provenance": (prov_in.value or "").strip(),
                "reference": (ref_in.value or "").strip() or None,
                "version_notes": (notes_in.value or "").strip() or None,
            }
            asel = ctx.get("approver_sel")
            if is_global and asel is not None and asel.value:
                meta["approver_user_id"] = int(asel.value)
            try:
                await client.add_library_version(
                    file_id,
                    metadata=meta,
                    filename=str(upload_state["filename"] or "upload"),
                    content=bytes(upload_state["bytes"]),
                )
            except ApiError as exc:
                message.text = exc.detail or "Couldn't add the version."
                return
            dialog.close()
            ui.notify("Version added.", type="positive")
            await reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Add version", icon="upload_file", on_click=_save).props("color=primary")
    dialog.open()
    ui.timer(0.0, _maybe_approvers, once=True)


def _open_assign_dialog(client: ApiClient, file_id: int, reload: Any) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 460px;"):
        ui.label("Assign to a tool").style("font-size: 1.1rem; font-weight: 600;")
        tool_sel = ui.select({}, label="Tool *").classes("w-full")
        field_sel = ui.select({None: _ANY_FIELD_LABEL}, value=None, label="Field").classes("w-full")
        message = ui.label("").style("color: var(--matofx-error-text);")
        desc_cache: dict[str, dict[str, Any]] = {}

        async def _load_tools() -> None:
            try:
                tools = await client.list_tools()
            except ApiError:
                tools = []
            tool_sel.options = {str(t["alias"]): str(t.get("name") or t["alias"]) for t in tools}
            tool_sel.update()

        async def _on_tool_change(_e: Any = None) -> None:
            opts: dict[Any, str] = {None: _ANY_FIELD_LABEL}
            alias = str(tool_sel.value or "")
            if alias:
                if alias not in desc_cache:
                    try:
                        desc_cache[alias] = await client.get_tool(alias)
                    except ApiError:
                        desc_cache[alias] = {}
                for inp in _tool_file_inputs(desc_cache[alias]):
                    opts[inp["name"]] = f"{inp['label']} ({inp['name']})"
            field_sel.options = opts
            field_sel.value = None
            field_sel.update()

        tool_sel.on("update:model-value", lambda _e=None: ui.timer(0.0, _on_tool_change, once=True))

        async def _save() -> None:
            message.text = ""
            if not tool_sel.value:
                message.text = "Pick a tool."
                return
            try:
                await client.assign_library_tool(
                    file_id,
                    tool_alias=str(tool_sel.value),
                    field_name=(str(field_sel.value) if field_sel.value else None),
                )
            except ApiError as exc:
                message.text = exc.detail or "Couldn't assign."
                return
            dialog.close()
            ui.notify("Tool assigned.", type="positive")
            await reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Assign", icon="add_link", on_click=_save).props("color=primary")
    dialog.open()
    ui.timer(0.0, _load_tools, once=True)


def _open_scope_dialog(client: ApiClient, f: dict[str, Any], is_curator: bool, reload: Any) -> None:
    fid = int(f.get("id") or 0)
    with ui.dialog() as dialog, ui.card().style("min-width: 480px;"):
        ui.label("Change scope").style("font-size: 1.1rem; font-weight: 600;")
        scope_sel = ui.select(
            {"personal": "Personal", "project": "Project", "global": "Global"},
            value=str(f.get("scope") or "personal"),
            label="New scope",
        ).classes("w-full")
        extra = ui.column().classes("w-full gap-2")
        ctx: dict[str, Any] = {"project_sel": None, "approver_sel": None}
        message = ui.label("").style("color: var(--matofx-error-text);")

        async def _rebuild() -> None:
            extra.clear()
            ctx["project_sel"] = None
            ctx["approver_sel"] = None
            scope = str(scope_sel.value or "personal")
            with extra:
                if scope == "project":
                    try:
                        projects = await client.list_projects()
                    except ApiError as exc:
                        ui.label(f"Couldn't load projects: {exc.detail}").style("color: #888;")
                        return
                    opts = {int(p["id"]): str(p.get("name") or p["id"]) for p in projects}
                    ctx["project_sel"] = ui.select(opts, label="Project").classes("w-full")
            if scope == "global":
                await _add_approver_picker(client, extra, ctx, is_curator)

        scope_sel.on("update:model-value", lambda _e=None: ui.timer(0.0, _rebuild, once=True))

        async def _save() -> None:
            message.text = ""
            scope = str(scope_sel.value or "personal")
            project_id: int | None = None
            if scope == "project":
                psel = ctx.get("project_sel")
                if psel is None or not psel.value:
                    message.text = "Pick a project."
                    return
                project_id = int(psel.value)
            approver_id: int | None = None
            asel = ctx.get("approver_sel")
            if scope == "global" and asel is not None and asel.value:
                approver_id = int(asel.value)
            try:
                await client.change_library_scope(
                    fid,
                    new_scope=scope,
                    project_id=project_id,
                    expected_version=int(f.get("version") or 0),
                    approver_user_id=approver_id,
                )
            except ApiError as exc:
                message.text = exc.detail or "Couldn't change the scope."
                return
            dialog.close()
            ui.notify("Scope updated.", type="positive")
            await reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Change scope", icon="swap_horiz", on_click=_save).props("color=primary")
    dialog.open()
    ui.timer(0.0, _rebuild, once=True)


# ----------------------------- actions -------------------------------


def _confirm_retire(client: ApiClient, file_id: int, reload: Any) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label("Retire this file?").style("font-size: 1.1rem; font-weight: 600;")
        ui.label(
            "It will stop appearing in pickers and can't be edited until restored. "
            "Nothing is deleted — retire is reversible."
        ).style("color: #888;")

        async def _confirm() -> None:
            try:
                await client.retire_library_file(file_id)
            except ApiError as exc:
                ui.notify(f"Couldn't retire: {exc.detail}", type="negative")
                return
            dialog.close()
            ui.notify("File retired.", type="info")
            await reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Retire", icon="archive", on_click=_confirm).props("color=negative")
    dialog.open()


def _confirm_restore(client: ApiClient, file_id: int, reload: Any) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label("Restore this file?").style("font-size: 1.1rem; font-weight: 600;")
        ui.label(
            "It will become active again — appearing in pickers and open to edits."
        ).style("color: #888;")

        async def _confirm() -> None:
            try:
                await client.restore_library_file(file_id)
            except ApiError as exc:
                ui.notify(f"Couldn't restore: {exc.detail}", type="negative")
                return
            dialog.close()
            ui.notify("File restored.", type="positive")
            await reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Restore", icon="unarchive", on_click=_confirm).props("color=primary")
    dialog.open()


async def _do_unassign(client: ApiClient, file_id: int, map_id: int, reload: Any) -> None:
    try:
        await client.unassign_library_tool(file_id, map_id)
    except ApiError as exc:
        ui.notify(f"Couldn't remove: {exc.detail}", type="negative")
        return
    ui.notify("Assignment removed.", type="info")
    await reload()
