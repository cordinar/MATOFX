"""Admin: Migrate Tools/Workflows (``/ui/admin/migrate``).

Single page surfacing the cross-install migration feature
(:doc:`MIGRATE_GUIDE.md`). Three stacked sections top-to-bottom:

1. **This install's fingerprint** + **trusted source installs** —
   one expansion panel grouping the trust-list management.
2. **Export a bundle** — pick tools + workflows, optional notes, click
   Generate. Browser downloads the gzipped tarball.
3. **Import a bundle** — drop a ``.tar.gz`` file, dry-run preview,
   confirm to commit. The same live status log is rendered for both
   the dry-run preview and the final commit.

Permissions: :attr:`Permission.MIGRATE_TOOLS_WORKFLOWS`. Super-users
hold it by default; super-users can grant it to administrators via
the role-edit UI.

The page is deliberately linear — no tabs, no nested wizard. The user
explicitly asked for "as simple and informative as possible" with
"plain English status updates", so every label / button / status line
is written in operator-friendly English. The error-message catalogue
is the single source of truth (see :mod:`matofx.migrate.errors`); the
UI never paraphrases those strings.
"""

from __future__ import annotations

import base64
from typing import Any, Final

from nicegui import events, ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token

# Status-level → display style. The colour tokens map onto the existing
# matofx theme palette so dark / light flips stay consistent.
_LEVEL_STYLE: Final[dict[str, str]] = {
    "info": "color: var(--matofx-secondary-text); font-family: monospace;",
    "imported": "color: var(--matofx-success-text); font-family: monospace; font-weight: 600;",
    "skipped": "color: var(--matofx-warning-text); font-family: monospace;",
    "warning": "color: var(--matofx-warning-text); font-family: monospace;",
    "error": "color: var(--matofx-error-text); font-family: monospace; font-weight: 600;",
    "summary": "color: var(--matofx-primary-text); font-family: monospace; font-weight: 600;",
}
_LEVEL_PREFIX: Final[dict[str, str]] = {
    "info": "  ",
    "imported": "OK   ",
    "skipped": "SKIP ",
    "warning": "WARN ",
    "error": "ERR  ",
    "summary": "---  ",
}


@ui.page("/admin/migrate")
async def admin_migrate_page() -> None:
    """Render the migrate-tools-workflows management surface."""
    if not require_logged_in():
        return
    render_shell()
    client = ApiClient(get_app(), token=current_token())

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        # Header.
        with ui.column().classes("gap-1"):
            ui.label("Migrate Tools/Workflows").style("font-size: 1.5rem; font-weight: 600;")
            ui.label(
                "Copy admin-released tools and workflows between MATOFX "
                "installs. Typically used to promote a freshly-tested "
                "tool or workflow from Staging to Production. Bundles "
                "are signed; the destination only accepts bundles from "
                "trusted source installs."
            ).style("color: var(--matofx-secondary-text); font-size: 0.9rem;")

        await _render_trusted_peers_section(client)
        await _render_export_section(client)
        await _render_import_section(client)


# ---------------------------------------------------------------------------
# Trusted-peers section
# ---------------------------------------------------------------------------


async def _render_trusted_peers_section(client: ApiClient) -> None:
    """Show this install's fingerprint + the destination's trust list.

    Wrapped in an expansion that defaults to collapsed — the section
    is mostly one-time setup, so the export / import surfaces should
    be visible first.
    """
    with ui.expansion("Trusted peers (one-time setup)", icon="verified_user").classes("w-full"):
        with ui.column().classes("w-full gap-4 p-2"):
            await _render_this_install_card(client)
            await _render_peer_list_card(client)


async def _render_this_install_card(client: ApiClient) -> None:
    """The fingerprint of this install, for pasting into peer installs."""
    with ui.card().classes("w-full"):
        ui.label("This install's fingerprint").style("font-weight: 600; font-size: 1.0rem;")
        ui.label(
            "Copy this and paste it into a destination install's "
            "trusted-peers list. The destination will then accept "
            "bundles signed here."
        ).style("color: var(--matofx-secondary-text); font-size: 0.85rem;")

        display_label = ui.label().style(
            "font-family: monospace; font-size: 0.95rem; word-break: break-all;"
        )

        async def _load() -> None:
            try:
                fp = await client.get_install_fingerprint()
            except ApiError as exc:
                display_label.set_text(f"Could not load fingerprint: {exc.detail}")
                return
            display_label.set_text(str(fp["fingerprint_display"]))
            copy_button.canonical = str(fp["fingerprint_display"])  # type: ignore[attr-defined]

        async def _copy() -> None:
            value = getattr(copy_button, "canonical", "")
            if not value:
                return
            # NiceGUI's clipboard helper uses an injected JS shim; pass
            # the value via run_javascript to avoid python-level escaping.
            await ui.run_javascript(f"navigator.clipboard.writeText({value!r});")
            ui.notify("Fingerprint copied to clipboard.", type="positive")

        copy_button = ui.button("Copy fingerprint", icon="content_copy", on_click=_copy).props(
            "flat dense"
        )

        await _load()


async def _render_peer_list_card(client: ApiClient) -> None:
    """List + manage the trusted source installs."""
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-1"):
                ui.label("Trusted source installs").style("font-weight: 600; font-size: 1.0rem;")
                ui.label(
                    "When a source install's fingerprint is on this list, "
                    "bundles signed by that install will be accepted "
                    "here. Bundles from any other install are refused."
                ).style("color: var(--matofx-secondary-text); font-size: 0.85rem;")
            ui.button(
                "Add trusted peer",
                icon="add",
                on_click=lambda: _open_add_peer_dialog(client, _reload),
            ).props("color=primary")

        list_container = ui.column().classes("w-full gap-2 pt-2")

        async def _reload() -> None:
            list_container.clear()
            try:
                peers = await client.list_trusted_peers()
            except ApiError as exc:
                with list_container:
                    ui.label(f"Could not load trust list: {exc.detail}").style(
                        "color: var(--matofx-error-text);"
                    )
                return
            with list_container:
                if not peers:
                    ui.label(
                        "No trusted peers yet. Click 'Add trusted peer' to trust a source install."
                    ).style("color: var(--matofx-secondary-text);")
                    return
                for peer in peers:
                    _render_peer_row(client, peer, _reload)

        await _reload()


def _render_peer_row(client: ApiClient, peer: dict[str, Any], reload_fn: Any) -> None:
    """One row in the trusted-peers list."""
    with (
        ui.card().classes("w-full").props("flat"),
        ui.row().classes("w-full items-start gap-3 no-wrap"),
    ):
        ui.icon("verified").style("font-size: 1.4rem; color: var(--matofx-success-text);")
        with ui.column().classes("gap-1").style("flex: 1 1 0; min-width: 0;"):
            ui.label(str(peer.get("label") or "(unlabelled)")).style(
                "font-weight: 600; font-size: 1.0rem;"
            )
            ui.label(str(peer.get("fingerprint_display") or "")).style(
                "font-family: monospace; font-size: 0.85rem; "
                "color: var(--matofx-secondary-text); word-break: break-all;"
            )
            added = format_user_datetime(peer.get("added_at"))
            last_used_raw = peer.get("last_used_at")
            last_used = format_user_datetime(last_used_raw) if last_used_raw else "never used"
            ui.label(f"Added {added} · last used: {last_used}").style(
                "color: var(--matofx-secondary-text); font-size: 0.8rem;"
            )
            notes = str(peer.get("notes") or "")
            if notes:
                ui.label(f"Notes: {notes}").style(
                    "color: var(--matofx-secondary-text); font-size: 0.85rem;"
                )
        # Bound to locals rather than lambda defaults: the defaults existed
        # only to capture this iteration's peer, and calling ``peer.get`` in
        # a default list evaluates it at definition time (B008).
        peer_fp = str(peer["fingerprint"])
        peer_label = str(peer.get("label") or "")
        ui.button(
            "Remove",
            icon="delete",
            on_click=lambda _e=None, fp=peer_fp, label=peer_label: _open_remove_peer_dialog(
                client, fp, label, reload_fn
            ),
        ).props("flat dense color=negative")


def _open_add_peer_dialog(client: ApiClient, reload_fn: Any) -> None:
    """Modal: paste a fingerprint + label + optional notes."""
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-md gap-3 p-4"):
        ui.label("Add trusted peer").style("font-weight: 600; font-size: 1.1rem;")
        ui.label(
            "Paste the source install's fingerprint exactly as the "
            "source displays it. Spaces, colons, dashes, and any "
            "'SHA256:' prefix are all OK."
        ).style("color: var(--matofx-secondary-text); font-size: 0.85rem;")
        fp_input = (
            ui.input(
                "Fingerprint",
                placeholder="e.g. 9F:2E:A1:7B:04:...",
            )
            .classes("w-full")
            .props("autocomplete=off")
        )
        label_input = ui.input(
            "Label",
            placeholder="e.g. Staging",
        ).classes("w-full")
        notes_input = ui.textarea(
            "Notes (optional)",
            placeholder="Free-text note shown alongside this entry.",
        ).classes("w-full")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            async def _do_add() -> None:
                fp_value = (fp_input.value or "").strip()
                label_value = (label_input.value or "").strip()
                notes_value = (notes_input.value or "").strip()
                if not fp_value or not label_value:
                    ui.notify(
                        "Fingerprint and label are both required.",
                        type="warning",
                    )
                    return
                try:
                    await client.add_trusted_peer(
                        fingerprint=fp_value, label=label_value, notes=notes_value
                    )
                except ApiError as exc:
                    ui.notify(f"Couldn't add peer: {exc.detail}", type="negative")
                    return
                ui.notify(f"Added trusted peer '{label_value}'.", type="positive")
                dialog.close()
                await reload_fn()

            ui.button("Add", on_click=_do_add).props("color=primary")
    dialog.open()


def _open_remove_peer_dialog(
    client: ApiClient, fingerprint: str, label: str, reload_fn: Any
) -> None:
    """Modal: confirm removal with a mandatory reason."""
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-md gap-3 p-4"):
        ui.label(f"Remove trusted peer '{label}'").style("font-weight: 600; font-size: 1.1rem;")
        ui.label(
            "Bundles from this source install will no longer be accepted. "
            "The removal is audit-logged with the reason you provide here."
        ).style("color: var(--matofx-secondary-text); font-size: 0.85rem;")
        reason_input = ui.textarea(
            "Reason (required)",
            placeholder="e.g. Decommissioning the Staging install.",
        ).classes("w-full")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            async def _do_remove() -> None:
                reason = (reason_input.value or "").strip()
                if not reason:
                    ui.notify("A reason is required.", type="warning")
                    return
                try:
                    await client.remove_trusted_peer(fingerprint=fingerprint, reason=reason)
                except ApiError as exc:
                    ui.notify(f"Couldn't remove peer: {exc.detail}", type="negative")
                    return
                ui.notify("Trusted peer removed.", type="positive")
                dialog.close()
                await reload_fn()

            ui.button("Remove", on_click=_do_remove).props("color=negative")
    dialog.open()


# ---------------------------------------------------------------------------
# Export section
# ---------------------------------------------------------------------------


async def _render_export_section(client: ApiClient) -> None:
    """Pick tools + workflows, click Generate, browser downloads the bundle."""
    with ui.card().classes("w-full"):
        ui.label("Export a bundle").style("font-weight: 600; font-size: 1.1rem;")
        ui.label(
            "Bundle admin-released tools and workflows from this install "
            "into a signed .tar.gz file. Transfer it to the destination "
            "install (scp, USB stick, file share — your choice) and use "
            "the Import section there to apply it."
        ).style("color: var(--matofx-secondary-text); font-size: 0.85rem;")

        try:
            tools = await client.list_admin_tools()
            workflows = await client.list_admin_workflows()
        except ApiError as exc:
            ui.label(f"Could not load tools/workflows: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return

        # Both sides filter to admin-released only — custom workflows
        # are deliberately not exportable in v1 of this feature.
        admin_tools = [t for t in tools if not t.get("archived_at")]
        admin_workflows = [
            w for w in workflows if w.get("kind") == "admin" and not w.get("archived_at")
        ]

        ui.label("Tools to bundle").style("font-weight: 600; padding-top: 0.5rem;")
        if not admin_tools:
            ui.label("(No installed tools to export.)").style(
                "color: var(--matofx-secondary-text); font-size: 0.85rem;"
            )
        tool_checkboxes: list[tuple[ui.checkbox, str, str]] = []
        with ui.column().classes("gap-1"):
            for tool in admin_tools:
                alias = str(tool.get("alias") or "")
                version = str(tool.get("version") or "")
                name = str(tool.get("name") or alias)
                state = str(tool.get("state") or "")
                state_chip = f"[{state}]" if state else ""
                cb = ui.checkbox(f"{name} {version} {state_chip}")
                tool_checkboxes.append((cb, alias, version))

        ui.label("Workflows to bundle").style("font-weight: 600; padding-top: 0.5rem;")
        if not admin_workflows:
            ui.label("(No admin-released workflows to export.)").style(
                "color: var(--matofx-secondary-text); font-size: 0.85rem;"
            )
        workflow_checkboxes: list[tuple[ui.checkbox, str]] = []
        with ui.column().classes("gap-1"):
            for wf in admin_workflows:
                alias = str(wf.get("alias") or "")
                name = str(wf.get("name") or alias)
                state = str(wf.get("state") or "")
                state_chip = f"[{state}]" if state else ""
                cb = ui.checkbox(f"{name} {state_chip}")
                workflow_checkboxes.append((cb, alias))

        notes_input = (
            ui.textarea(
                "Notes (optional)",
                placeholder="Free-text note recorded in the bundle's manifest "
                "and shown in the destination's import preview.",
            )
            .classes("w-full")
            .style("padding-top: 0.5rem;")
        )

        async def _do_export() -> None:
            selected_tools = [
                {"alias": alias, "version": version}
                for cb, alias, version in tool_checkboxes
                if cb.value
            ]
            selected_workflows = [alias for cb, alias in workflow_checkboxes if cb.value]
            if not selected_tools and not selected_workflows:
                ui.notify(
                    "Pick at least one tool or workflow before exporting.",
                    type="warning",
                )
                return
            try:
                bundle_bytes, suggested = await client.export_migration_bundle(
                    tool_specs=selected_tools,
                    workflow_aliases=selected_workflows,
                    notes=(notes_input.value or "").strip(),
                )
            except ApiError as exc:
                ui.notify(f"Export failed: {exc.detail}", type="negative")
                return
            filename = suggested or "bundle.tar.gz"
            _trigger_browser_download(bundle_bytes, filename)
            ui.notify(
                f"Bundle ready: {filename} ({len(bundle_bytes):,} bytes). "
                "Saved to your browser's default download location.",
                type="positive",
                timeout=8000,
            )

        with ui.row().classes("w-full justify-end pt-2"):
            ui.button("Generate bundle", icon="download", on_click=_do_export).props(
                "color=primary"
            )


def _trigger_browser_download(content: bytes, filename: str) -> None:
    """Stream ``content`` to the browser as ``filename`` via a data URL.

    NiceGUI's ``ui.download`` doesn't currently accept arbitrary bytes
    from server-side memory (it expects a filesystem path); the
    base64-data-URL + click() trick is the portable workaround. Same
    pattern used by the Compare-Jobs PDF export and the tutorial
    download buttons.
    """
    b64 = base64.b64encode(content).decode("ascii")
    js = (
        f"(function() {{"
        f"  const a = document.createElement('a');"
        f"  a.href = 'data:application/gzip;base64,{b64}';"
        f"  a.download = {filename!r};"
        f"  document.body.appendChild(a);"
        f"  a.click();"
        f"  document.body.removeChild(a);"
        f"}})();"
    )
    ui.run_javascript(js)


# ---------------------------------------------------------------------------
# Import section
# ---------------------------------------------------------------------------


_DRY_RUN_TEXT = (
    "Preview a bundle: verify the signature, check it against the "
    "trusted-peer list, and predict per-item outcomes — without writing "
    "anything. Always run a preview before clicking 'Confirm import'."
)
_COMMIT_TEXT = (
    "Apply the previewed bundle. Each item is imported in its own "
    "transaction; failures don't stop the rest. The same plain-English "
    "log runs above the action buttons during the commit."
)


async def _render_import_section(client: ApiClient) -> None:
    """Drop a .tar.gz file, preview, then commit."""
    with ui.card().classes("w-full"):
        ui.label("Import a bundle").style("font-weight: 600; font-size: 1.1rem;")
        ui.label(
            "Upload a .tar.gz bundle exported from another MATOFX install. "
            "The bundle must be signed by an install whose fingerprint is "
            "in your trusted-peers list above."
        ).style("color: var(--matofx-secondary-text); font-size: 0.85rem;")

        # Mutable state shared by the upload handler and the action buttons.
        state: dict[str, Any] = {"filename": "", "bytes": b""}

        def _show_selected() -> None:
            selected_name.set_text(f"{state['filename']} ({len(state['bytes']):,} bytes)")
            selected_row.visible = True
            file_label.visible = False

        def _clear_selected() -> None:
            state["filename"] = ""
            state["bytes"] = b""
            upload.reset()  # clear the widget's own file list / progress bar
            selected_row.visible = False
            file_label.visible = True
            log_container.clear()
            log_container.visible = False

        # File-drop area. ``auto_upload=True`` so the user doesn't have to
        # click an extra "Upload" button after dropping the file.
        async def _on_upload(e: events.UploadEventArguments) -> None:
            # NiceGUI 3.x: ``e.file`` is a FileUpload and ``read()`` is async.
            # The earlier ``e.content.read()``/``e.name`` form raised
            # AttributeError here and was swallowed, so the bundle never
            # registered and the dry-run reported "choose a file".
            state["bytes"] = await e.file.read()
            state["filename"] = e.file.name
            _show_selected()
            ui.notify(
                f"Bundle accepted: {state['filename']} ({len(state['bytes']):,} bytes). "
                "Use 'Preview (dry-run)' to verify, or 'Remove' to pick another.",
                type="positive",
            )

        upload = (
            ui.upload(
                on_upload=_on_upload,
                label="Drop a .tar.gz bundle here, or click to browse",
                auto_upload=True,
                max_files=1,
            )
            .classes("w-full")
            .props("accept=.tar.gz,.gz,application/gzip")
        )

        # Persistent "accepted file" row with a Remove option — shown once a
        # bundle is accepted so the operator can confirm it landed and swap it.
        selected_row = ui.row().classes("w-full items-center gap-2 pt-1")
        selected_row.visible = False
        with selected_row:
            ui.icon("description").style("color: var(--matofx-accent);")
            selected_name = ui.label("").style("font-size: 0.9rem; font-weight: 600;")
            ui.space()
            ui.button("Remove", icon="close", on_click=_clear_selected).props("flat dense")

        file_label = ui.label("No file selected yet.").style(
            "color: var(--matofx-secondary-text); font-size: 0.85rem;"
        )

        # Status log + action buttons.
        log_container = (
            ui.column()
            .classes("w-full p-3 my-3 gap-1")
            .style(
                "border: 1px solid var(--matofx-border); "
                "border-radius: 6px; "
                "max-height: 24rem; overflow-y: auto;"
            )
        )
        log_container.visible = False

        async def _run_import(*, dry_run: bool) -> None:
            if not state["bytes"]:
                ui.notify(
                    "Choose a bundle file first (drop it on the area above).",
                    type="warning",
                )
                return

            # Reset the log panel for this run.
            log_container.clear()
            log_container.visible = True

            try:
                envelope = await client.import_migration_bundle(
                    filename=state["filename"],
                    file_bytes=state["bytes"],
                    dry_run=dry_run,
                )
            except ApiError as exc:
                with log_container:
                    ui.label(_LEVEL_PREFIX["error"] + (exc.detail or "")).style(
                        _LEVEL_STYLE["error"]
                    )
                ui.notify(f"Import failed: {exc.detail}", type="negative")
                return

            _render_status_log(log_container, envelope)
            # The result summary is also worth highlighting outside the
            # log — the operator may want to know imported/skipped at a
            # glance without scrolling.
            result = envelope.get("result", {}) or {}
            mode = "Preview" if dry_run else "Import"
            ui.notify(
                f"{mode}: {result.get('imported_count', 0)} imported, "
                f"{result.get('skipped_count', 0)} skipped, "
                f"{result.get('noop_count', 0)} already present.",
                type="positive" if result.get("imported_count", 0) else "info",
                timeout=10000,
            )

        with ui.row().classes("w-full gap-2 pt-2"):
            ui.button(
                "Preview (dry-run)",
                icon="visibility",
                on_click=lambda: _run_import(dry_run=True),
            ).props("flat")
            ui.button(
                "Confirm import",
                icon="check_circle",
                on_click=lambda: _run_import(dry_run=False),
            ).props("color=primary")
        with ui.column().classes("w-full gap-1"):
            ui.label(_DRY_RUN_TEXT).style("color: var(--matofx-secondary-text); font-size: 0.8rem;")
            ui.label(_COMMIT_TEXT).style("color: var(--matofx-secondary-text); font-size: 0.8rem;")

        # Render the log container BELOW the action buttons so the
        # user sees the latest events as they appear (no scrolling
        # past form controls).
        ui.label("Status").style("font-weight: 600; padding-top: 0.5rem;")
        # The actual log_container was created above; place its DOM
        # element here in the layout flow.
        # (NiceGUI binds Python order = DOM order; we already created it
        # earlier so it sits above this label. The visual ordering matches
        # operator expectations: action buttons up top, plain-English
        # log below for clarity.)


def _render_status_log(container: ui.column, envelope: dict[str, Any]) -> None:
    """Append every progress event + the result summary to ``container``."""
    events_list = envelope.get("events", []) or []
    result = envelope.get("result", {}) or {}

    with container:
        if not events_list:
            ui.label("(no events emitted)").style(
                "color: var(--matofx-secondary-text); font-family: monospace;"
            )
        for event in events_list:
            level = str(event.get("level") or "info")
            message = str(event.get("message") or "")
            prefix = _LEVEL_PREFIX.get(level, "  ")
            style = _LEVEL_STYLE.get(level, _LEVEL_STYLE["info"])
            ui.label(prefix + message).style(style)

        # Operator-facing summary footer with explicit next-steps when
        # something needs follow-up.
        imported = int(result.get("imported_count", 0))
        skipped = int(result.get("skipped_count", 0))
        noop = int(result.get("noop_count", 0))
        ui.separator()
        ui.label(
            f"Summary: {imported} imported, {skipped} skipped, {noop} already present (no-op)."
        ).style(_LEVEL_STYLE["summary"])
        if result.get("dry_run"):
            ui.label(
                "  This was a preview — nothing has been written. Click "
                "'Confirm import' above to apply."
            ).style(_LEVEL_STYLE["info"])
        else:
            steps: list[str] = []
            if imported > 0:
                steps.append(
                    "Visit Admin -> Tools and Admin -> Workflows to "
                    "promote imported items from 'new' to 'test' or "
                    "'released' when satisfied."
                )
            if skipped > 0:
                steps.append(
                    "Resolve the skipped items above (typically by "
                    "seeding the missing reference or bumping a version "
                    "on the source) and re-import the same bundle to "
                    "pick them up."
                )
            if steps:
                ui.label("Next steps:").style(_LEVEL_STYLE["summary"])
                for step in steps:
                    ui.label("  - " + step).style(_LEVEL_STYLE["info"])
