"""Admin: Tools (``/ui/admin/tools``).

Lists every installed tool — including archived ones — with a state
badge, the bundle path, and per-tool actions:

- **Promote** — move through New → Test → Released.
- **Edit** — open the metadata editor (``/admin/tools/{alias}/edit``).
- **Archive** — soft-shelve the tool with a confirm dialog explaining
  the consequences.
- **Re-activate** — bring an archived tool back online.

A "New tool" button at the top opens ``/admin/tools/new``.

Archived tools stay in this list (with an ARCHIVED badge) so admins
can re-activate them later. They're hidden from the home page and
the submission flow via :meth:`ToolService.list_visible`'s filter.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token

# Forward state-transition graph: each state's allowed next state.
_NEXT_STATE: dict[str, str | None] = {
    "new": "test",
    "test": "released",
    "released": None,
}

# Backward state-transition graph: each state's rollback target. Rollback
# is always allowed by the server (see ToolService.promote_state) — used
# here purely to decide which button to render on the card.
_PREV_STATE: dict[str, str | None] = {
    "new": None,
    "test": "new",
    "released": "test",
}

_STATE_LABEL: dict[str, str] = {
    "new": "New",
    "test": "Test",
    "released": "Released",
}

# Operator-facing labels for the descriptor's approvals_policy values.
_APPROVALS_LABEL: dict[str, str] = {
    "none": "No approvals required",
    "pre": "Pre-run approval",
    "post": "Post-run approval",
    "both": "Pre + post-run approval",
}

_WORLD_TEAM_NAME = "World"


@ui.page("/admin/tools")
async def admin_tools_page() -> None:
    """Render the tools admin table."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())
    try:
        tools = await client.list_admin_tools()
        # Users power the approver-name resolution on each card —
        # one bulk fetch up front avoids N+1 calls during render.
        users = await client.list_users()
    except ApiError as exc:
        with ui.column().classes("w-full max-w-5xl mx-auto p-6"):
            ui.label(f"We couldn't load the tools list: {exc.detail}").style("color: #c0392b;")
        return

    # In-progress New Tool drafts for this user. Best-effort: a failure
    # here just hides the section rather than blocking the whole page.
    try:
        drafts = await client.list_tool_drafts()
    except ApiError:
        drafts = []

    # Default sort: A→Z by tool name (case-insensitive).
    tools = sorted(tools, key=lambda t: str(t.get("name") or "").lower())

    # Lookup tables built once, reused per card.
    tool_name_by_alias = {str(t["alias"]): str(t.get("name") or t["alias"]) for t in tools}
    user_label_by_username: dict[str, str] = {}
    for u in users:
        username = str(u.get("username") or "")
        full_name = str(u.get("full_name") or "").strip()
        user_label_by_username[username] = f"{full_name} ({username})" if full_name else username

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Tools").style("font-size: 1.5rem; font-weight: 600;")
            ui.button(
                "New tool",
                icon="add",
                on_click=lambda: ui.navigate.to("/admin/tools/new"),
            ).props("color=primary")
        ui.label(
            "Promote moves through New → Test → Released. "
            "Archive hides the tool from new submissions but keeps existing job "
            "rows resolving — archived tools stay in this list and can be "
            "re-activated at any time."
        ).style("color: #555; font-size: 0.9rem;")

        if drafts:
            _render_drafts_section(drafts)

        if not tools:
            ui.label("No tools installed yet. Click 'New tool' to upload one.").style(
                "color: #555;"
            )
            return

        # Always-on search input + reactive count + reactive card list.
        search = (
            ui.input("Search", placeholder="By name, alias, or description")
            .classes("w-full")
            .props("dense clearable")
        )
        count_label = ui.label("").style("color: #888; font-size: 0.85rem;")
        list_container = ui.column().classes("w-full gap-3")

        def _rerender(needle: str) -> None:
            needle = needle.strip().lower()
            if needle:
                visible = [
                    t
                    for t in tools
                    if needle in str(t.get("name") or "").lower()
                    or needle in str(t.get("alias") or "").lower()
                    or needle in str(t.get("short_description") or "").lower()
                    or needle in str(t.get("long_description") or "").lower()
                ]
            else:
                visible = list(tools)
            suffix = "" if len(tools) == 1 else "s"
            count_label.text = f"Showing {len(visible):,} of {len(tools):,} tool{suffix}"
            list_container.clear()
            with list_container:
                if not visible:
                    ui.label("No tools match the search. Try clearing the search box.").style(
                        "color: #555;"
                    )
                    return
                for tool in visible:
                    _render_tool_card(tool, tool_name_by_alias, user_label_by_username)

        def _on_search_change(e: Any = None) -> None:
            # Read the new value from the event arg when available — the
            # input's .value attribute can lag a tick behind on the
            # initial Vue commit, especially when the clearable button
            # fires update:model-value with null.
            if e is not None and getattr(e, "args", None) is not None:
                raw = "" if e.args is None else str(e.args)
            else:
                raw = "" if search.value is None else str(search.value)
            _rerender(raw)

        # Bind to multiple events so the clearable X-button reliably
        # triggers a re-render: Quasar fires `clear` on click and
        # `update:model-value` (with null) for the value commit, but
        # the order/timing isn't always consistent.
        search.on("update:model-value", _on_search_change)
        search.on("clear", lambda _e=None: _rerender(""))
        _rerender("")


def _render_drafts_section(drafts: list[dict[str, Any]]) -> None:
    """Collapsible list of the user's saved (in-progress) New Tool drafts.

    Each row resumes the New Tool form pre-populated (``?draft=<id>``) or
    discards the draft. Uploaded bundle files are never part of a draft,
    so the heading says so to set expectations on resume.
    """
    with ui.expansion(
        f"Drafts in progress ({len(drafts)})", icon="drafts"
    ).classes("w-full").props("default-opened"):
        ui.label(
            "Saved New Tool setups you haven't installed yet. Resume to pick up "
            "where you left off — you'll need to re-attach the descriptor / static "
            "ZIP / icon, which aren't part of a draft."
        ).style("color: #555; font-size: 0.85rem;")
        for draft in drafts:
            draft_id = int(draft["id"])
            title = str(draft.get("title") or "").strip() or "(untitled draft)"
            alias = str(draft.get("alias") or "").strip()
            when = str(draft.get("updated_at") or "")
            when_short = when[:19].replace("T", " ") if "T" in when and len(when) >= 19 else when
            with ui.card().classes("w-full"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.column().classes("gap-0"):
                        ui.label(title).style("font-weight: 600;")
                        sub = f"alias: {alias}" if alias else "alias: (not set yet)"
                        if when_short:
                            sub += f" · last saved {when_short}"
                        ui.label(sub).style("color: #888; font-size: 0.8rem;")
                    with ui.row().classes("gap-2"):
                        ui.button(
                            "Resume",
                            icon="edit",
                            on_click=lambda _e=None, _id=draft_id: ui.navigate.to(
                                f"/admin/tools/new?draft={_id}"
                            ),
                        ).props("color=primary")
                        ui.button(
                            "Delete",
                            icon="delete",
                            on_click=lambda _e=None, _id=draft_id, _t=title: _confirm_delete_draft(
                                _id, _t
                            ),
                        ).props("flat color=negative")


def _confirm_delete_draft(draft_id: int, title: str) -> None:
    """Confirm-and-discard dialog for a saved draft."""
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label(f"Delete draft: {title}?").style("font-size: 1.1rem; font-weight: 600;")
        ui.label("This discards the saved setup. It can't be undone.").style(
            "color: #555; font-size: 0.9rem;"
        )
        message = ui.label("").style("color: #c0392b;")

        async def _go() -> None:
            client = ApiClient(get_app(), token=current_token())
            try:
                await client.delete_tool_draft(draft_id)
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Draft deleted.", type="positive")
            ui.navigate.to("/admin/tools")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Delete", on_click=_go).props("color=negative")
    dialog.open()


def _render_tool_card(
    tool: dict[str, Any],
    tool_name_by_alias: dict[str, str],
    user_label_by_username: dict[str, str],
) -> None:
    state = str(tool.get("state") or "")
    archived_at = tool.get("archived_at")
    state_badge = {
        "new": "color=grey",
        "test": "color=info",
        "released": "color=positive",
    }.get(state, "color=grey")
    alias = str(tool["alias"])

    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-start justify-between"):
            with ui.column().classes("gap-1 flex-1"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(str(tool["name"])).style("font-weight: 600;")
                    ui.label(f"v{tool.get('version', '')}").style(
                        "color: #888; font-size: 0.85rem;"
                    )
                    ui.badge(state.upper() or "?").props(state_badge)
                    if archived_at:
                        ui.badge("ARCHIVED").props("color=negative")
                ui.label(f"alias: {alias}").style("color: #555; font-size: 0.85rem;")
                if tool.get("short_description"):
                    ui.label(str(tool["short_description"])).style(
                        "color: #555; font-size: 0.9rem;"
                    )
                ui.label(f"Bundle: {tool.get('bundle_path', '')}").style(
                    "color: #888; font-size: 0.8rem;"
                )
                ui.label(_visibility_line(tool)).style("color: #b0b3b8; font-size: 0.85rem;")
                ui.label(_approvers_line(tool, user_label_by_username)).style(
                    "color: #b0b3b8; font-size: 0.85rem;"
                )
                ui.label(_downstream_line(tool, tool_name_by_alias)).style(
                    "color: #b0b3b8; font-size: 0.85rem;"
                )

            with ui.column().classes("gap-2 items-end"):
                next_state = _NEXT_STATE.get(state)
                prev_state = _PREV_STATE.get(state)
                if not archived_at and next_state is not None:
                    ui.button(
                        f"Promote to {_STATE_LABEL.get(next_state, next_state.title())}",
                        icon="arrow_upward",
                        on_click=lambda _e=None, _a=alias, _ns=next_state: _promote(_a, _ns),
                    ).props("color=primary")
                if not archived_at and prev_state is not None:
                    ui.button(
                        f"Roll back to {_STATE_LABEL.get(prev_state, prev_state.title())}",
                        icon="arrow_downward",
                        on_click=lambda _e=None, _a=alias, _ps=prev_state: _promote(_a, _ps),
                    ).props("flat")
                if not archived_at:
                    ui.button(
                        "Edit",
                        icon="edit",
                        on_click=lambda _e=None, _a=alias: ui.navigate.to(
                            f"/admin/tools/{_a}/edit"
                        ),
                    ).props("flat")
                    ui.button(
                        "Archive",
                        icon="archive",
                        on_click=lambda _e=None, _a=alias: _open_archive_dialog(_a),
                    ).props("flat color=warning")
                else:
                    ui.button(
                        "Re-activate",
                        icon="unarchive",
                        on_click=lambda _e=None, _a=alias: _confirm_reactivate(_a),
                    ).props("color=positive")
                ui.button(
                    "View change log",
                    icon="history",
                    on_click=lambda _e=None, _a=alias: _open_change_log_dialog(
                        _a, tool_name_by_alias, user_label_by_username
                    ),
                ).props("flat")


async def _promote(alias: str, new_state: str) -> None:
    client = ApiClient(get_app(), token=current_token())
    try:
        await client.promote_tool(alias, new_state=new_state)
    except ApiError as exc:
        ui.notify(f"Promote failed: {exc.detail}", type="negative")
        return
    ui.notify(f"Promoted {alias} to {new_state}.", type="positive")
    ui.navigate.to("/admin/tools")


def _open_archive_dialog(alias: str) -> None:
    """Confirm-and-archive dialog with explicit consequences copy."""
    with ui.dialog() as dialog, ui.card().style("min-width: 480px;"):
        ui.label(f"Archive tool: {alias}?").style("font-size: 1.2rem; font-weight: 600;")
        ui.label(
            "Archiving hides this tool from everyone's home page and stops "
            "new jobs from being submitted against it. Existing jobs that ran "
            "on this tool stay queryable. The tool will still show in the "
            "admin Tools list with an ARCHIVED badge, and you can re-activate "
            "it any time from there."
        ).style("color: #555; font-size: 0.9rem;")
        reason = ui.input("Reason (optional)").classes("w-full").props("dense")
        message = ui.label("").style("color: #c0392b;")

        async def _archive() -> None:
            client = ApiClient(get_app(), token=current_token())
            try:
                await client.archive_tool(alias, reason=str(reason.value or ""))
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify(f"Archived {alias}.", type="positive")
            ui.navigate.to("/admin/tools")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Archive", on_click=_archive).props("color=warning")
    dialog.open()


def _confirm_reactivate(alias: str) -> None:
    """Lightweight confirmation before un-archiving."""
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label(f"Re-activate {alias}?").style("font-size: 1.2rem; font-weight: 600;")
        ui.label(
            "The tool becomes visible to users again and will accept new jobs from the home page."
        ).style("color: #555; font-size: 0.9rem;")
        message = ui.label("").style("color: #c0392b;")

        async def _go() -> None:
            client = ApiClient(get_app(), token=current_token())
            try:
                await client.unarchive_tool(alias)
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify(f"Re-activated {alias}.", type="positive")
            ui.navigate.to("/admin/tools")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Re-activate", on_click=_go).props("color=positive")
    dialog.open()


def _open_change_log_dialog(
    alias: str,
    tool_name_by_alias: dict[str, str],
    user_label_by_username: dict[str, str],
) -> None:
    """Modal: scrollable list of immutable snapshots, newest first.

    Each entry shows the headline summary (``"Metadata edit — patch
    bump 0.1.0 → 0.1.1 (changed: name, teams)"``) plus a collapsible
    expansion that renders the structured ``changes`` map as
    ``field: <old> → <new>`` bullets. Field keys render as human labels
    and alias / username / enum values resolve to the same friendly
    labels used on the cards (never raw internal names). Snapshots are
    append-only — the modal is read-only by design.
    """
    with ui.dialog() as dialog, ui.card().style("min-width: 640px; max-height: 80vh;"):
        ui.label(f"Change log: {alias}").style("font-size: 1.2rem; font-weight: 600;")
        ui.label(
            "Every install / edit / replace / first-release snap creates "
            "an immutable snapshot. Entries can't be edited or deleted from "
            "the UI — every snapshot insertion is mirrored in the audit "
            "log for tamper-evidence."
        ).style("color: #888; font-size: 0.85rem;")

        body = ui.column().classes("w-full gap-2").style("overflow-y: auto;")

        async def _populate() -> None:
            client = ApiClient(get_app(), token=current_token())
            try:
                versions = await client.list_tool_versions(alias)
            except ApiError as exc:
                with body:
                    ui.label(f"Couldn't load history: {exc.detail}").style("color: #c0392b;")
                return
            with body:
                if not versions:
                    ui.label("No snapshots yet.").style("color: #555;")
                    return
                for entry in versions:
                    _render_change_log_entry(
                        entry, tool_name_by_alias, user_label_by_username
                    )

        ui.timer(0.0, _populate, once=True)

        with ui.row().classes("w-full justify-end mt-2"):
            ui.button("Close", on_click=dialog.close).props("flat")
    dialog.open()


def _render_change_log_entry(
    entry: dict[str, Any],
    tool_name_by_alias: dict[str, str],
    user_label_by_username: dict[str, str],
) -> None:
    """One row in the change-log modal.

    Headline + expandable detail. Each field-level change renders as a
    two-line block: the field's human label + arrow, then the old/new
    values on indented rows. Aliases / usernames / enum codes resolve
    to friendly labels (never raw internal names); multi-line /
    structured values pretty-print as monospace JSON so a teams list or
    descriptor diff stays readable instead of collapsing onto one
    ``[a, b, c, d, e]`` line.
    """
    version = str(entry.get("version") or "?")
    when = str(entry.get("created_at") or "")
    when_short = when[:19].replace("T", " ") if "T" in when and len(when) >= 19 else when
    by = str(entry.get("created_by_label") or "")
    summary = str(entry.get("change_summary") or "")
    changes = entry.get("changes") or {}

    headline = f"v{version} · {when_short} · {by}"
    with ui.expansion(headline, icon="history").classes("w-full"):
        if summary:
            ui.label(summary).style("font-style: italic; color: #b0b3b8; font-size: 0.9rem;")
        if not isinstance(changes, dict) or not changes:
            ui.label("(no field-level diff)").style("color: #888; font-size: 0.85rem;")
            return
        for field, diff in changes.items():
            if not isinstance(diff, dict):
                continue
            old_value = _resolve_change_value(
                str(field), diff.get("old"), tool_name_by_alias, user_label_by_username
            )
            new_value = _resolve_change_value(
                str(field), diff.get("new"), tool_name_by_alias, user_label_by_username
            )
            with ui.column().classes("w-full gap-0 q-mt-sm"):
                ui.label(_change_log_field_label(str(field))).style(
                    "font-weight: 600; font-size: 0.9rem; color: var(--matofx-label-text);"
                )
                _render_pretty_value("old", old_value)
                _render_pretty_value("new", new_value)


# Descriptor field keys → human labels for the change-log modal, so the
# diff never shows raw internal names (use-labels-not-names). Keys not
# listed here fall back to a humanised form of the key.
_CHANGE_LOG_FIELD_LABELS: dict[str, str] = {
    "name": "Name",
    "short_description": "Short description",
    "long_description": "Long description",
    "teams": "Teams",
    "downstream_tools": "Downstream tools",
    "cpu_cores": "CPU cores",
    "ram_mb": "RAM (MB)",
    "gpu_memory_mb": "GPU memory (MB)",
    "licences_required": "Licences",
    "approvals_policy": "Approval policy",
    "approver_usernames": "Approvers",
    "email_notifications": "Email notifications",
    "duration_class": "Duration class",
    "state": "State",
    "version": "Version",
    "main_script": "Main script",
    "icon_filename": "Icon",
    "inputs": "Inputs",
    "outputs": "Outputs",
    "input_groups": "Input groups",
    "output_groups": "Output groups",
    "retain_files": "Files to retain",
    "delete_io_files": "Files to delete",
    "presets": "Presets",
    "archived": "Archived",
    "archive_reason": "Archive reason",
    "run_cap": "Parametric study run cap",
    "python_env_id": "Execution environment",
}


def _change_log_field_label(field: str) -> str:
    """Human label for a change-log field key (falls back to humanised key)."""
    if field in _CHANGE_LOG_FIELD_LABELS:
        return _CHANGE_LOG_FIELD_LABELS[field]
    return field.replace("_", " ").capitalize()


def _resolve_change_value(
    field: str,
    value: Any,
    tool_name_by_alias: dict[str, str],
    user_label_by_username: dict[str, str],
) -> Any:
    """Map raw stored values to the friendly labels shown on the cards.

    Resolves downstream-tool aliases → names, approver usernames →
    ``Full Name (username)``, and the approval-policy / state enum
    codes → their operator-facing labels. Anything else passes through
    unchanged for :func:`_render_pretty_value` to format.
    """
    if value is None:
        return None
    if field == "downstream_tools" and isinstance(value, list):
        return [tool_name_by_alias.get(str(a), str(a)) for a in value]
    if field == "approver_usernames" and isinstance(value, list):
        return [user_label_by_username.get(str(u), str(u)) for u in value]
    if field == "approvals_policy" and isinstance(value, str):
        return _APPROVALS_LABEL.get(value, value)
    if field == "state" and isinstance(value, str):
        return _STATE_LABEL.get(value, value)
    return value


def _render_pretty_value(label: str, value: Any) -> None:
    """Emit one side of an old → new diff as a labelled, indented block.

    Scalars (None / str / int / bool / number) render inline with the
    label. Structured values (list / dict) render as multi-line
    pretty-printed JSON in a monospace block so a 12-team list or a
    nested descriptor patch stays scannable.
    """
    import json  # noqa: PLC0415

    if value is None:
        rendered = "∅"
        inline = True
    elif isinstance(value, str):
        rendered = value
        inline = "\n" not in value and len(value) <= 80
    elif isinstance(value, list | dict):
        rendered = json.dumps(value, indent=2, ensure_ascii=False, default=str)
        inline = False
    else:
        rendered = str(value)
        inline = True

    prefix_style = (
        "color: var(--matofx-muted-text); font-size: 0.8rem; "
        "min-width: 2.5rem; font-weight: 600; font-family: monospace;"
    )
    if inline:
        with ui.row().classes("w-full items-baseline gap-2 q-pl-md"):
            ui.label(f"{label}:").style(prefix_style)
            ui.label(rendered).style("font-family: monospace; font-size: 0.85rem;")
        return
    with ui.column().classes("w-full gap-1 q-pl-md"):
        ui.label(f"{label}:").style(prefix_style)
        # ``ui.code`` would force language-tagged syntax highlighting; a
        # plain <pre> keeps it clean and preserves wrapping behaviour
        # via ``white-space: pre-wrap``.
        ui.html(
            f"<pre style='margin:0; padding:0.4rem 0.6rem; "
            f"background:rgba(255,255,255,0.04); "
            f"border:1px solid var(--matofx-card-border); border-radius:4px; "
            f"font-family:monospace; font-size:0.8rem; white-space:pre-wrap; "
            f"word-break:break-word;'>{_html_escape(rendered)}</pre>",
            sanitize=False,
        )


def _html_escape(value: str) -> str:
    """Tiny HTML escaper for the <pre> block above — keeps the dependency surface zero."""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


# ---- summary lines for the tool cards -----------------------------------


def _visibility_line(tool: dict[str, Any]) -> str:
    """One-line summary of who can see the tool.

    Tools tagged with ``World`` (or with no teams at all, treated the
    same way) are visible to every authenticated user; otherwise the
    operator sees the explicit team list. Admin / super-user
    visibility is universal regardless and isn't called out here.
    """
    teams = list(tool.get("teams") or [])
    if not teams or _WORLD_TEAM_NAME in teams:
        return "Visible by: Everyone (World)"
    return "Visible by: " + ", ".join(teams)


def _approvers_line(tool: dict[str, Any], user_label_by_username: dict[str, str]) -> str:
    """Approval-policy + approver-list summary, or 'No approvals required'.

    Approver usernames resolve to ``Full Name (username)`` via the
    page-level lookup; an unknown username (e.g. a deactivated user
    that hasn't been swept off the descriptor yet) falls through as
    the bare username so the operator still sees something useful.
    """
    policy = str(tool.get("approvals_policy") or "none")
    if policy == "none":
        return "Approvers: No approvals required"
    label = _APPROVALS_LABEL.get(policy, policy.title())
    approvers = list(tool.get("approver_usernames") or [])
    if not approvers:
        return f"Approvers: {label} — none configured"
    rendered = ", ".join(user_label_by_username.get(a, a) for a in approvers)
    return f"Approvers: {label} — {rendered}"


def _downstream_line(tool: dict[str, Any], tool_name_by_alias: dict[str, str]) -> str:
    """Comma-separated list of downstream tool names, or 'None'.

    Aliases that no longer resolve to a known tool fall through as
    the bare alias so the operator can still spot dangling references.
    """
    aliases = list(tool.get("downstream_tools") or [])
    if not aliases:
        return "Downstream tools: None"
    rendered = ", ".join(tool_name_by_alias.get(a, a) for a in aliases)
    return f"Downstream tools: {rendered}"
