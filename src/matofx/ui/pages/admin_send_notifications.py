"""Admin: Send Notifications (``/ui/admin/send-notifications``).

Two tabs, styled to the home-redesign shell (ADR-0016): lowercase
left-aligned tabs over a transparent panel, themed icon-chip cards.

* **compose** — pick the audience (every registered user, only those
  currently enabled, or specific teams / access-rights groups /
  curators), write a subject and body, optionally attach files, then
  send. The body is wrapped in the persisted header/footer template
  (text + inline images) and delivered as HTML with a plain-text
  fallback. Recipients are always resolved server-side.
* **header & footer template** — the once-and-forget surround applied
  to every broadcast: a header image + header text, and a footer image
  + footer text, each with its own live preview.

The page is fully wired to ``/api/admin/notifications/*`` (gated by
``Permission.NOTIFICATIONS_SEND``). Sending is synchronous: the admin
gets an immediate sent / failed summary. Template text saves on a
*Save* button per block; images upload / clear immediately (each has
its own endpoint and instant visual feedback in the live preview).
"""

from __future__ import annotations

import base64
from typing import Any

from nicegui import events, ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import (
    render_shell,
    require_logged_in,
    section_card,
)
from matofx.ui.state import current_token

#: Selector 1 — the base recipient scope.
_SCOPE_OPTIONS: dict[str, str] = {
    "active": "Users with access currently enabled",
    "all": "All registered users (includes deactivated accounts)",
}

#: Selector 2 — the optional group restriction ("" = no restriction).
_GROUP_OPTIONS: dict[str, str] = {
    "": "Everyone in the selected recipients",
    "teams": "Specific teams",
    "roles": "Specific access-rights groups",
    "curators": "Specific curators and approvers",
}

#: Group kinds that need a multiselect of specific entities.
_GROUP_KINDS = ("teams", "roles", "curators")

_GROUP_PROMPT: dict[str, str] = {
    "teams": "Choose one or more teams",
    "roles": "Choose one or more access-rights groups",
    "curators": "Choose one or more curator or approver groups",
}

#: Plural noun for the "nothing to choose from" empty state.
_GROUP_NOUN: dict[str, str] = {
    "teams": "teams",
    "roles": "access-rights groups",
    "curators": "curator or approver groups",
}

_IMAGE_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
}


@ui.page("/admin/send-notifications")
async def admin_send_notifications_page() -> None:
    """Render the two-tab Send Notifications page."""
    if not require_logged_in():
        return
    render_shell(help_topic="sending-messages")

    client = ApiClient(get_app(), token=current_token())

    with ui.column().classes("w-full max-w-4xl mx-auto gap-4").style(
        "padding: 0.85rem 1.5rem 1.5rem;"
    ):
        ui.label("Send Notifications").style(
            "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
        )
        ui.label(
            "Broadcast an email to your MATOFX users. The header and footer "
            "template wraps every send, so the compose form stays focused on "
            "audience, subject, body, and attachments."
        ).style("color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;")

        # The template snapshot is needed before the template tab builds.
        # A failure here (e.g. 403 for a non-permitted admin) must not
        # blank the whole page — render a clear notice instead.
        try:
            template = await client.get_notification_template()
        except ApiError as exc:
            _render_error_card(exc.detail)
            return
        except Exception as exc:  # never blank the body
            _render_error_card(str(exc))
            return

        # Audience options are best-effort: a failure degrades the group
        # multiselects to "couldn't load" but still lets the admin send
        # to all / currently-enabled users.
        try:
            audience_options = await client.get_broadcast_audience_options()
        except Exception:  # best-effort dropdown data
            audience_options = {}

        with (
            ui.tabs()
            .classes("w-full")
            .props("align=left active-color=primary indicator-color=primary")
            .style("border-bottom: 1px solid var(--matofx-card-border);") as tabs
        ):
            tab_compose = ui.tab("Compose", icon="mail").props("no-caps")
            tab_template = ui.tab("Header & footer template", icon="article").props("no-caps")

        with (
            ui.tab_panels(tabs, value=tab_compose)
            .classes("w-full")
            .style("background-color: transparent;")
        ):
            with ui.tab_panel(tab_compose):
                _render_compose_section(client, audience_options)
            with ui.tab_panel(tab_template):
                await _render_template_section(client, template)


def _render_error_card(detail: str) -> None:
    with section_card("error", "Can't load Send Notifications"):
        ui.label(detail).style("color: var(--matofx-muted-text);")


# ---------- Compose ------------------------------------------------------


def _render_compose_section(client: ApiClient, audience_options: dict[str, Any]) -> None:
    """Audience + subject + body + attachments + Send."""
    # Per-kind {value: label} maps for the group multiselects.
    group_maps: dict[str, dict[str, str]] = {
        kind: {str(o["id"]): str(o["label"]) for o in (audience_options.get(kind) or [])}
        for kind in _GROUP_KINDS
    }
    state: dict[str, Any] = {
        "scope": "active",
        "group_kind": "",
        "attachments": [],
        "selection": {kind: [] for kind in _GROUP_KINDS},
    }

    with ui.column().classes("w-full gap-4"):
        with section_card("groups", "Audience"):
            # Selector 1 — base recipient scope.
            ui.label("Recipients").style(
                "font-weight: 600; font-size: 0.85rem; color: var(--matofx-label-text);"
            )
            ui.label(
                "'Currently enabled' skips anyone whose access has been turned off."
            ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
            ui.radio(
                options=_SCOPE_OPTIONS,
                value=state["scope"],
                on_change=lambda e: state.update(scope=str(e.value)),
            ).props("color=primary")

            ui.separator().style("margin: 0.4rem 0;")

            # Selector 2 — optional group restriction.
            ui.label("Target a specific group (optional)").style(
                "font-weight: 600; font-size: 0.85rem; color: var(--matofx-label-text);"
            )
            ui.label(
                "Narrows the recipients above to members of the teams, "
                "access-rights groups, or curator/approver groups you pick."
            ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
            ui.radio(
                options=_GROUP_OPTIONS,
                value=state["group_kind"],
                on_change=lambda e: _on_group_change(str(e.value)),
            ).props("color=primary")

            picker_area = ui.column().classes("w-full gap-1")

            def _render_picker() -> None:
                picker_area.clear()
                kind = state["group_kind"]
                if kind not in _GROUP_KINDS:
                    return
                with picker_area:
                    options = group_maps.get(kind) or {}
                    if not options:
                        ui.label(
                            f"Nothing to choose from yet — no {_GROUP_NOUN[kind]} "
                            "exist on this install."
                        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
                        return

                    def _on_select(e: Any, _k: str = kind) -> None:
                        state["selection"][_k] = [str(v) for v in (e.value or [])]

                    (
                        ui.select(
                            options=options,
                            value=state["selection"][kind],
                            multiple=True,
                            label=_GROUP_PROMPT[kind],
                            on_change=_on_select,
                        )
                        .classes("w-full")
                        .props("use-chips clearable dense")
                    )

            def _on_group_change(value: str) -> None:
                state["group_kind"] = value
                _render_picker()

            _render_picker()

        with section_card("edit", "Message"):
            subject_input = (
                ui.input("Subject", placeholder="e.g. Scheduled maintenance on Friday")
                .classes("w-full")
                .props("dense")
            )
            body_input = (
                ui.textarea(
                    "Message body",
                    placeholder=(
                        "Write the message your users will see between the "
                        "configured header and footer."
                    ),
                )
                .classes("w-full")
                .props("dense autogrow")
            )

        with section_card("attach_file", "Attachments"):
            ui.label("Optional. Each file is sent alongside every email in the broadcast.").style(
                "color: var(--matofx-muted-text); font-size: 0.85rem;"
            )

            attachments_list = ui.column().classes("w-full gap-1")

            def _refresh_attachments() -> None:
                attachments_list.clear()
                with attachments_list:
                    if not state["attachments"]:
                        ui.label("No attachments added yet.").style(
                            "color: var(--matofx-muted-text); font-size: 0.85rem;"
                        )
                        return
                    for idx, item in enumerate(state["attachments"]):

                        def _remove(_e: Any = None, _i: int = idx) -> None:
                            state["attachments"].pop(_i)
                            _refresh_attachments()

                        with ui.row().classes("items-center gap-2 w-full"):
                            ui.icon("attach_file").style("color: var(--matofx-accent);")
                            ui.label(item["name"]).style("flex: 1 1 0;")
                            ui.label(_format_size(item["size"])).style(
                                "color: var(--matofx-muted-text); font-size: 0.85rem;"
                            )
                            ui.button(icon="close", on_click=_remove).props("flat dense round")

            async def _on_multi_upload(event: events.MultiUploadEventArguments) -> None:
                for upload_file in event.files:
                    data = await upload_file.read()
                    state["attachments"].append(
                        {
                            "name": upload_file.name,
                            "size": len(data),
                            "bytes": data,
                            "type": upload_file.content_type or "application/octet-stream",
                        }
                    )
                # Clear the widget so the just-added files don't linger in
                # its own list — they now live in the list above it, and the
                # widget is ready for the next upload.
                attach_upload.reset()
                _refresh_attachments()

            attach_upload = ui.upload(
                on_multi_upload=_on_multi_upload,
                label="Add attachments",
                auto_upload=True,
                multiple=True,
            ).classes("w-full")

            _refresh_attachments()

        result_area = ui.column().classes("w-full gap-1")

        async def _send() -> None:
            subject = str(subject_input.value or "").strip()
            body = str(body_input.value or "").strip()
            if not subject:
                ui.notify("Please enter a subject before sending.", type="warning")
                return
            if not body:
                ui.notify("Please write a message body before sending.", type="warning")
                return
            scope = state["scope"]
            group_kind = state["group_kind"]
            selection = state["selection"][group_kind] if group_kind in _GROUP_KINDS else []
            if group_kind in _GROUP_KINDS and not selection:
                ui.notify(f"{_GROUP_PROMPT[group_kind]} before sending.", type="warning")
                return
            await _confirm_and_send(
                client,
                scope=scope,
                group_kind=group_kind,
                selection=selection,
                audience_label=_audience_label(scope, group_kind, selection, group_maps),
                subject=subject,
                body=body,
                attachments=state["attachments"],
                result_area=result_area,
            )

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Send broadcast", icon="send", on_click=_send).props("color=primary")


def _audience_label(
    scope: str,
    group_kind: str,
    selection: list[str],
    group_maps: dict[str, dict[str, str]],
) -> str:
    """Human description of the chosen audience for the confirm dialog."""
    base = _SCOPE_OPTIONS.get(scope, scope).lower()
    if group_kind not in _GROUP_KINDS:
        return base
    labels = [group_maps.get(group_kind, {}).get(v, v) for v in selection]
    return f"{base}, restricted to {_GROUP_NOUN[group_kind]}: " + ", ".join(labels)


async def _confirm_and_send(
    client: ApiClient,
    *,
    scope: str,
    group_kind: str,
    selection: list[str],
    audience_label: str,
    subject: str,
    body: str,
    attachments: list[dict[str, Any]],
    result_area: ui.column,
) -> None:
    """Confirm (broadcasts hit real inboxes), then send + show the summary."""
    with ui.dialog() as dialog, ui.card().style("min-width: 460px;"):
        ui.label("Send this broadcast?").style("font-weight: 600;")
        ui.label(f"This emails every recipient in: {audience_label}.").style(
            "color: var(--matofx-muted-text);"
        )
        ui.label(f"Subject: {subject}  ·  Attachments: {len(attachments)}").style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )

        async def _confirm() -> None:
            dialog.close()
            await _do_send(
                client,
                scope=scope,
                group_kind=group_kind,
                selection=selection,
                subject=subject,
                body=body,
                attachments=attachments,
                result_area=result_area,
            )

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Send", on_click=_confirm).props("color=primary")
    dialog.open()


async def _do_send(
    client: ApiClient,
    *,
    scope: str,
    group_kind: str,
    selection: list[str],
    subject: str,
    body: str,
    attachments: list[dict[str, Any]],
    result_area: ui.column,
) -> None:
    result_area.clear()
    with result_area:
        spinner = ui.row().classes("items-center gap-2")
        with spinner:
            ui.spinner(size="sm")
            ui.label("Sending…").style("color: var(--matofx-muted-text);")

    payload = [(a["name"], a["bytes"], a["type"]) for a in attachments]
    try:
        summary = await client.send_broadcast(
            audience=scope,
            subject=subject,
            body=body,
            attachments=payload,
            group_kind=group_kind,
            selection=selection,
        )
    except ApiError as exc:
        result_area.clear()
        ui.notify(f"Couldn't send: {exc.detail}", type="negative")
        return
    except Exception as exc:  # surface, don't crash the page
        result_area.clear()
        ui.notify(f"Couldn't send: {exc}", type="negative")
        return

    result_area.clear()
    with result_area:
        if summary.get("disabled"):
            ui.label(
                "Email is turned off for this install "
                "(email.enabled = false in matofx.toml). Nothing was sent."
            ).style("color: var(--matofx-negative, #c00); font-weight: 600;")
            ui.notify("Email subsystem disabled — nothing sent.", type="warning")
            return
        sent = int(summary.get("sent", 0))
        attempted = int(summary.get("attempted", 0))
        failed = list(summary.get("failed", []))
        msg = f"Sent {sent} of {attempted}."
        if attempted == 0:
            ui.label(
                msg + " The chosen audience has no email recipients."
            ).style("font-weight: 600; color: var(--matofx-muted-text);")
            ui.notify("No recipients matched the chosen audience.", type="warning")
        elif failed:
            ui.label(msg + f" {len(failed)} failed:").style(
                "font-weight: 600; color: var(--matofx-negative, #c00);"
            )
            for f in failed:
                ui.label(f"· {f['email']} — {f['reason']}").style(
                    "color: var(--matofx-muted-text); font-size: 0.85rem;"
                )
            ui.notify(f"Sent {sent}, {len(failed)} failed.", type="warning")
        else:
            ui.label(msg).style("font-weight: 600; color: var(--matofx-positive, #178000);")
            ui.notify(f"Broadcast sent to {sent} recipient(s).", type="positive")


# ---------- Header & Footer template ------------------------------------


async def _render_template_section(client: ApiClient, template: dict[str, Any]) -> None:
    """Header / footer text + one image each, applied to every broadcast."""
    # Both text areas register here so each block's Save persists both.
    texts: dict[str, Any] = {}

    async def _save() -> None:
        try:
            await client.set_notification_template(
                header_text=str(texts["header"].value or ""),
                footer_text=str(texts["footer"].value or ""),
            )
        except ApiError as exc:
            ui.notify(f"Couldn't save: {exc.detail}", type="negative")
            return
        except Exception as exc:
            ui.notify(f"Couldn't save: {exc}", type="negative")
            return
        ui.notify("Saved.", type="positive")

    with ui.column().classes("w-full gap-4"):
        ui.label(
            "These blocks wrap every notification you send. Set them once and forget."
        ).style("color: var(--matofx-muted-text); font-size: 0.9rem;")

        await _render_template_block(
            client,
            slot="header",
            title="Header",
            initial_text=str(template.get("header_text") or ""),
            muted_preview=False,
            texts=texts,
            on_save=_save,
        )
        await _render_template_block(
            client,
            slot="footer",
            title="Footer",
            initial_text=str(template.get("footer_text") or ""),
            muted_preview=True,
            texts=texts,
            on_save=_save,
        )


async def _render_template_block(
    client: ApiClient,
    *,
    slot: str,
    title: str,
    initial_text: str,
    muted_preview: bool,
    texts: dict[str, Any],
    on_save: Any,
) -> None:
    """One header/footer block: image card + text card + live preview + Save.

    The live preview is the single source of truth for "what recipients
    will see" for this block — it reflects the current image *and* the
    current text, updates as you type, and clears the moment the image is
    removed. The upload widget's own thumbnail is reset after each upload
    so it never lingers as a confusing second copy.
    """
    block: dict[str, Any] = {"img": None}

    with ui.column().classes("w-full gap-3"):
        # --- image card -------------------------------------------------
        with section_card("image", f"{title} Image"):
            ui.label(
                "One image, embedded inline in every broadcast. PNG / JPEG / "
                "GIF. Uploading replaces the current one."
            ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

            thumb_row = ui.row().classes("items-center gap-3")

            upload = (
                ui.upload(
                    label=f"Choose {title.lower()} image",
                    max_files=1,
                    auto_upload=True,
                )
                .classes("w-full")
                .props("accept=image/*")
            )

        # --- text card --------------------------------------------------
        with section_card("text_fields", f"{title} Text"):
            ui.label(
                f"Optional text shown {'below' if slot == 'footer' else 'above'} every "
                "broadcast body."
            ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
            text_area = (
                ui.textarea(
                    value=initial_text,
                    placeholder=f"Insert {title.lower()} text…",
                )
                .classes("w-full")
                .props("dense autogrow")
            )
        texts[slot] = text_area

        # --- live preview -----------------------------------------------
        with section_card("visibility", f"{title} preview"):
            preview = ui.column().classes("w-full gap-2")

        # --- save -------------------------------------------------------
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Save", icon="save", on_click=on_save).props("color=primary")

    def _render_preview() -> None:
        preview.clear()
        has_img = block["img"] is not None
        txt = str(text_area.value or "").strip()
        with preview:
            if not has_img and not txt:
                ui.label(
                    f"Nothing to preview yet — add a {title.lower()} image or text above."
                ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
                return
            if has_img:
                _render_image(
                    block["img"],
                    "max-width: 100%; max-height: 240px; height: auto; display: block; "
                    "border: 1px solid var(--matofx-card-border);",
                )
            if txt:
                style = "white-space: pre-wrap;"
                if muted_preview:
                    style += " color: var(--matofx-muted-text); font-size: 0.9rem;"
                ui.label(txt).style(style)

    def _render_thumb() -> None:
        thumb_row.clear()
        with thumb_row:
            if block["img"] is None:
                ui.label("No image set.").style(
                    "color: var(--matofx-muted-text); font-size: 0.85rem;"
                )
                return
            _render_image(
                block["img"],
                "width: 72px; height: 72px; object-fit: contain; "
                "border: 1px solid var(--matofx-card-border);",
            )
            ui.button("Remove image", icon="delete", on_click=_remove).props(
                "flat dense color=negative"
            )

    async def _remove(_e: Any = None) -> None:
        try:
            await client.clear_notification_template_image(slot)
        except ApiError as exc:
            ui.notify(f"Couldn't remove: {exc.detail}", type="negative")
            return
        except Exception as exc:
            ui.notify(f"Couldn't remove: {exc}", type="negative")
            return
        block["img"] = None
        ui.notify(f"{title} image removed.", type="info")
        _render_thumb()
        _render_preview()

    async def _on_upload(event: events.UploadEventArguments) -> None:
        data = await event.file.read()
        name = event.file.name
        mime = event.file.content_type or _guess_mime(name)
        try:
            await client.upload_notification_template_image(
                slot, filename=name, data=data, mime_type=mime
            )
        except ApiError as exc:
            ui.notify(f"Upload rejected: {exc.detail}", type="negative")
            return
        except Exception as exc:
            ui.notify(f"Upload failed: {exc}", type="negative")
            return
        block["img"] = data
        # Clear the upload widget's own file list so it doesn't linger as
        # a second, separately-removable copy of the image.
        upload.reset()
        ui.notify(f"{title} image updated.", type="positive")
        _render_thumb()
        _render_preview()

    upload.on_upload(_on_upload)
    text_area.on_value_change(lambda _e=None: _render_preview())

    try:
        block["img"] = await client.get_notification_template_image(slot)
    except Exception:  # preview is best-effort
        block["img"] = None
    _render_thumb()
    _render_preview()


# ---------- helpers ------------------------------------------------------


def _render_image(data: bytes, style: str) -> None:
    """Render image bytes as a plain ``<img>`` data URI with the given style.

    A real ``<img>`` (not ``ui.image`` / Quasar ``q-img``) so flexible
    sizing like ``max-height`` behaves predictably — ``q-img`` uses a
    ratio-box that collapses to near-zero height with ``width: auto``,
    which is why the larger preview looked empty. The MIME type is sniffed
    from the magic bytes (``data:image/*`` is non-standard).
    """
    mime = _sniff_image_mime(data)
    b64 = base64.b64encode(data).decode("ascii")
    ui.html(
        f'<img src="data:{mime};base64,{b64}" alt="" style="{style}" />',
        sanitize=False,
    )


def _sniff_image_mime(data: bytes) -> str:
    """Best-effort image MIME from the leading magic bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"


def _guess_mime(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _IMAGE_MIME_BY_EXT.get(ext, "application/octet-stream")


def _format_size(size_bytes: int) -> str:
    """Pretty-print a byte count as KiB / MiB / GiB."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KiB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MiB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GiB"
