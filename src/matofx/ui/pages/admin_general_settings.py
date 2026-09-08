"""Admin → General Settings page (``/admin/general-settings``).

Renders the General Settings catalog as four collapsible sections.
Each row's input control is picked from the spec's ``kind``: number,
text, switch, masked secret, select, or image upload. Per-row Save
and Reset buttons hit the matching API endpoints; per-row "lock"
state is computed from the caller's role versus the spec's
``requires_role``.

The page deliberately mirrors the layout pattern of the runner
``runtime-settings`` tab on ``/admin/paths``: one card per setting,
inline edit, no separate "save all" button. That model maps cleanly
onto the per-row audit and role-gate semantics enforced by the API.
"""

from __future__ import annotations

from typing import Any, cast

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token
from matofx.ui.static_assets import logo_url
from matofx.users.roles import UserRole, has_role_at_least

# ---- display-label maps -------------------------------------------------
# Every user-facing surface shows human labels, never raw role/enum
# names. Role names map here; enum choices that aren't roles map in
# ``_ENUM_CHOICE_LABELS``; both fall back to a title-cased rendering.

_ROLE_LABELS: dict[str, str] = {
    "super_user": "Super User",
    "administrator": "Administrator",
    "beta_tester": "Beta Tester",
    "regular_user": "Regular User",
}

_ENUM_CHOICE_LABELS: dict[str, dict[str, str]] = {
    "registration_mode": {
        "open": "Open",
        "invite_only": "Invite only",
        "closed": "Closed",
    },
}

# Per-row override for the ``zero_means_never`` toggle caption. Defaults
# to "Never delete" (correct for the retention rows); rows where 0 means
# something other than "don't delete" override it here.
_ZERO_TOGGLE_LABELS: dict[str, str] = {
    "auto_disable_inactive_days": "Never disable",
    "auto_disable_grace_warning_days": "No warning email",
}


def _role_label(role: str | None) -> str:
    """Human label for a role name; falls back to a title-cased form."""
    if not role:
        return ""
    return _ROLE_LABELS.get(str(role), str(role).replace("_", " ").title())


def _choice_label(key: str, value: str) -> str:
    """Human label for an enum choice value on a given setting."""
    per_key = _ENUM_CHOICE_LABELS.get(key)
    if per_key and value in per_key:
        return per_key[value]
    if value in _ROLE_LABELS:
        return _ROLE_LABELS[value]
    return str(value).replace("_", " ").title()


def _default_image_url(key: str) -> str | None:
    """URL of the shipped default image for a branding row, or ``None``.

    The header icon falls back to the packaged brand logo so the page
    can preview the real default. The favicon has no dedicated shipped
    asset (the browser supplies its own default tab icon), so it
    returns ``None`` and the row shows a text note instead.
    """
    if key == "header_icon_path":
        return logo_url()
    return None


# ---- section layout -----------------------------------------------------

SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Session & Security",
        "Idle/hard session timeouts, password policy, failed-login "
        "lockout, registration mode, auto-disable inactive users. "
        "Super User only; Super Users may delegate the inactivity "
        "threshold to Administrators per the per-row delegation toggle.",
        (
            "session_idle_timeout_minutes",
            "session_hard_timeout_hours",
            "password_min_length",
            "password_require_uppercase",
            "password_require_lowercase",
            "password_require_digit",
            "password_require_symbol",
            "password_allowed_symbols",
            "failed_login_lockout_threshold",
            "failed_login_lockout_minutes",
            "auto_disable_inactive_days",
            "auto_disable_sweep_interval_hours",
            "auto_disable_grace_warning_days",
            "registration_mode",
            "new_account_default_role",
        ),
    ),
    (
        "Notifications",
        "SMTP server config + the master switch for automated job "
        "notifications. Admin → Send Notifications broadcasts use the "
        "same SMTP relay but are sent regardless of the master switch.",
        (
            "job_email_notifications_enabled",
            "smtp_host",
            "smtp_port",
            "smtp_username",
            "smtp_password",
            "use_starttls",
            "smtp_from_address",
            "public_url",
        ),
    ),
    (
        "Licence alerts",
        "Emails that tell administrators about a licence problem while it "
        "is happening — a Super User admitted over the limit, a sign-in "
        "refused because every place was in use, or a licence entering its "
        "grace period. Without these, the first sign is a compliance "
        "report, which may be months away. Turning them off does not stop "
        "the events being recorded.",
        ("licence_alert_emails_enabled",),
    ),
    (
        "Approval notifications",
        "Emails that tell an approver or curator an item is waiting for "
        "their decision (and tell the requestor the outcome). The master "
        "switch turns them all off at once; the per-type switches let you "
        "silence one kind of approval. Separate from the job-state switch "
        "above.",
        (
            "approval_email_notifications_enabled",
            "approval_email_jobs",
            "approval_email_library",
            "approval_email_materials",
            "approval_email_models",
            "approval_email_vv",
            "approval_email_studies",
            "approval_email_optimisation",
        ),
    ),
    (
        "Job housekeeping",
        "How long unsubmitted drafts, job logs, and completed / failed "
        "working directories are kept before an automatic cleanup deletes "
        "them. 0 = keep forever (the default for everything except draft "
        "jobs).",
        (
            "retain_draft_jobs_days",
            "retain_job_logs_days",
            "retain_completed_workdirs_days",
            "retain_failed_workdirs_days",
        ),
    ),
    (
        "Branding & UX",
        "App title, icons, footer contact, default timezone, "
        "maintenance-mode banner, Quick Access favourites toggle.",
        (
            "app_display_title",
            "header_icon_path",
            "favicon_path",
            "support_email_footer",
            "default_timezone",
            "maintenance_banner_text",
            "maintenance_window_iso",
            "favourites_enabled",
        ),
    ),
    (
        "Data governance",
        "Governance controls for the shared file library, materials "
        "database, surrogate models, provenance-graph rendering, and "
        "AI result summaries. Super User only.",
        (
            "library_global_publish_approval",
            "materials_change_approval",
            "models_global_publish_approval",
            "lineage_max_nodes",
            "llm_summaries_enabled",
        ),
    ),
)


# ---- page entry point ---------------------------------------------------


@ui.page("/admin/general-settings")
async def admin_general_settings_page() -> None:
    """Render the four-section General Settings page."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())
    try:
        me = await client.me()
        snapshot = await client.get_general_settings()
    except ApiError as exc:
        ui.label(f"We couldn't load general settings: {exc.detail}").style(
            "color: #c0392b; padding: 1rem;"
        )
        return

    role = str(me.get("role", ""))
    catalog: list[dict[str, Any]] = list(snapshot.get("catalog", []))
    values: dict[str, Any] = dict(snapshot.get("values", {}))
    delegations: dict[str, str] = dict(snapshot.get("delegations", {}))
    by_key = {entry["key"]: entry for entry in catalog}

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        ui.label("General Settings").style("font-size: 1.5rem; font-weight: 600;")
        ui.label(
            "Site-wide settings the Super User and Administrator can tune at "
            "runtime. Each row saves independently; sensitive rows "
            "(session timeouts, password rules) are locked to Super Users. "
            "Super Users may delegate selected Super User only rows to "
            "Administrators per the row's delegation toggle."
        ).style("color: #555;")

        for section_name, section_blurb, keys in SECTIONS:
            _render_section(section_name, section_blurb, keys, by_key, values, delegations, role)


# ---- section rendering --------------------------------------------------


def _render_section(
    name: str,
    blurb: str,
    keys: tuple[str, ...],
    by_key: dict[str, dict[str, Any]],
    values: dict[str, Any],
    delegations: dict[str, str],
    role: str,
) -> None:
    # Cross-row coordination within a section (e.g. "Require symbol"
    # enabling/disabling "Allowed symbols"). Keys are rendered in
    # ``keys`` order, so an earlier row can register state another
    # later row reads.
    controls: dict[str, Any] = {}
    with ui.card().classes("w-full"):
        ui.label(name).style("font-size: 1.15rem; font-weight: 600;")
        ui.label(blurb).style("color: #555; font-size: 0.9rem;")
        ui.separator()
        for key in keys:
            spec = by_key.get(key)
            if spec is None:
                # Catalog drift — the row was removed but the section
                # layout still references it. Show a tiny placeholder
                # rather than 500ing the whole page.
                ui.label(f"(missing catalog entry: {key})").style("color: #999; font-size: 0.8rem;")
                continue
            _render_row(spec, values.get(key), delegations.get(key), role, controls)


def _is_locked_for_set(spec: dict[str, Any], delegated_to: str | None, role: str) -> bool:
    """Compute the effective row lock honouring per-row delegation.

    A Super User only row whose Super User has delegated to
    administrators unlocks for administrators; everyone below the
    delegated role stays locked. Mirrors the service-side
    ``_require_role_for_set`` gate so the UI lock matches the
    server's accept/reject decision.
    """
    requires = cast("UserRole", str(spec.get("requires_role", "administrator")))
    if has_role_at_least(role, requires):
        return False
    delegatable_to = spec.get("delegatable_to")
    if (  # noqa: SIM103 - the branches below are not a single negation
        delegatable_to is not None
        and delegated_to is not None
        and delegated_to == delegatable_to
        and has_role_at_least(role, cast("UserRole", str(delegatable_to)))
    ):  # noqa: SIM103 - the branches below are not a single negation
        return False
    return True


def _render_row(
    spec: dict[str, Any],
    current_value: Any,
    delegated_to: str | None,
    role: str,
    controls: dict[str, Any],
) -> None:
    locked = _is_locked_for_set(spec, delegated_to, role)
    kind = spec["kind"]
    if kind in ("int", "float"):
        _render_numeric_row(spec, current_value, locked, delegated_to, role)
    elif kind == "bool":
        _render_bool_row(spec, current_value, locked, delegated_to, role, controls)
    elif kind == "str":
        _render_str_row(spec, current_value, locked, delegated_to, role, controls)
    elif kind == "secret":
        _render_secret_row(spec, locked, delegated_to, role)
    elif kind == "enum":
        _render_enum_row(spec, current_value, locked, delegated_to, role)
    elif kind == "image":
        _render_image_row(spec, current_value, locked, delegated_to, role)


# ---- row renderers per kind ---------------------------------------------


def _row_frame(
    spec: dict[str, Any],
    locked: bool,
    delegated_to: str | None = None,
    role: str = "",
) -> tuple[Any, Any]:
    """Render the common row frame (label + description + lock icon + delegation toggle).

    Returns ``(left_column, right_column)`` for the caller to add the
    kind-specific input + buttons to. When the spec marks the row
    ``delegatable_to`` *and* the viewer is a Super User, the left
    column gains a "Allow administrators to change" switch that
    grants / revokes the delegation in one click.
    """
    card = ui.card().classes("w-full")
    with card:
        row = ui.row().classes("w-full items-start justify-between gap-4")
        with row:
            left = ui.column().classes("gap-1 flex-1")
            with left:
                with ui.row().classes("items-center gap-2"):
                    ui.label(spec["label"]).style("font-weight: 600;")
                    if locked:
                        delegatable_to = spec.get("delegatable_to")
                        requires_label = _role_label(spec.get("requires_role"))
                        if delegatable_to:
                            tooltip = (
                                f"This row requires the '{requires_label}' role "
                                f"by default. A Super User can delegate "
                                f"this setting to '{_role_label(delegatable_to)}' "
                                f"users via the per-row toggle."
                            )
                        else:
                            tooltip = f"This row requires the '{requires_label}' role."
                        ui.icon("lock").tooltip(tooltip).style("color: #888; font-size: 1.1rem;")
                    if spec.get("pending_restart"):
                        ui.label("Restart required").style(
                            "background: #fff3cd; color: #856404; "
                            "padding: 0 0.5rem; border-radius: 4px; "
                            "font-size: 0.75rem; font-weight: 500;"
                        )
                ui.label(spec["description"]).style("color: #555; font-size: 0.85rem;")
                _render_delegation_toggle(spec, delegated_to, role)
            right = ui.row().classes("items-center gap-2")
    return left, right


def _render_delegation_toggle(
    spec: dict[str, Any],
    delegated_to: str | None,
    role: str,
) -> None:
    """Per-row "Allow administrators to change" switch — Super User only.

    Renders nothing when the spec isn't delegatable or when the
    viewer isn't a Super User. The switch's state mirrors the
    persisted ``delegated_to_role``; flipping it on grants
    delegation to the role the spec advertises, flipping it off
    revokes.
    """
    delegatable_to = spec.get("delegatable_to")
    if not delegatable_to:
        return
    # Only Super Users see + flip the toggle; lower-tier callers
    # never even know the delegation exists in the UI, by design —
    # this is Super User authority.
    if not has_role_at_least(role, "super_user"):
        if delegated_to:
            # Show a passive badge so an administrator can see why
            # the row is unlocked for them despite the lock icon.
            ui.label(f"Delegated to: {_role_label(delegated_to)}").style(
                "background: #e3f2fd; color: #0d47a1; "
                "padding: 0 0.5rem; border-radius: 4px; "
                "font-size: 0.75rem; font-weight: 500; "
                "align-self: flex-start; margin-top: 0.25rem;"
            )
        return

    key = spec["key"]
    is_delegated = delegated_to is not None and delegated_to == delegatable_to
    with ui.row().classes("items-center gap-2").style("margin-top: 0.25rem;"):
        switch = ui.switch(
            f"Allow {_role_label(delegatable_to)}s to change this",
            value=is_delegated,
        ).props("dense")

        async def _on_toggle(_e: Any = None) -> None:
            new_state = bool(switch.value)
            target: str | None = str(delegatable_to) if new_state else None
            client = ApiClient(get_app(), token=current_token())
            try:
                await client.delegate_general_setting(key, delegated_to_role=target)
            except ApiError as exc:
                # Revert the switch on failure so the UI doesn't drift
                # from the server.
                switch.value = is_delegated
                ui.notify(f"Couldn't change delegation: {exc.detail}", type="negative")
                return
            verb = "granted" if new_state else "revoked"
            ui.notify(f"Delegation {verb} for {spec['label']}.", type="positive")
            ui.navigate.to("/admin/general-settings")

        switch.on("update:model-value", _on_toggle)


def _render_numeric_row(
    spec: dict[str, Any],
    current_value: Any,
    locked: bool,
    delegated_to: str | None,
    role: str,
) -> None:
    key = spec["key"]
    zero_means_never = bool(spec.get("zero_means_never"))
    unit = spec.get("unit") or ""
    _left, right = _row_frame(spec, locked, delegated_to, role)
    with right:
        if zero_means_never:
            never_toggle = ui.switch(
                _ZERO_TOGGLE_LABELS.get(key, "Never delete"),
                value=(_as_int(current_value) == 0),
            ).props("dense")
        else:
            never_toggle = None
        number = (
            ui.number(
                label=unit,
                value=_as_float(current_value),
                min=spec.get("min_value"),
                max=spec.get("max_value"),
                step=1 if spec["kind"] == "int" else 0.1,
            )
            .props("dense")
            .style("width: 130px;")
        )
        if never_toggle is not None and never_toggle.value:
            number.disable()

        def _on_never_toggle(_e: Any = None) -> None:
            if never_toggle is None:
                return
            if never_toggle.value:
                number.value = 0
                number.disable()
            else:
                # Restore to spec default (rather than 0) when toggling
                # off so admins don't accidentally save 0 thinking they
                # just turned "never" off.
                default = spec.get("default")
                number.value = float(default) if isinstance(default, int | float) else 1
                number.enable()

        if never_toggle is not None:
            never_toggle.on("update:model-value", _on_never_toggle)

        async def _save(_e: Any = None) -> None:
            raw = number.value
            if raw is None:
                ui.notify("Value is required.", type="negative")
                return
            value: int | float = int(raw) if spec["kind"] == "int" else float(raw)
            # The inactivity threshold gets a preview-confirm dance so the
            # operator sees how many users the new value would disable
            # before committing. Every other numeric row saves directly.
            if key == "auto_disable_inactive_days":
                await _save_inactivity_threshold(int(value), current_value, spec["label"])
                return
            await _do_save(key, value, spec["label"])

        async def _reset(_e: Any = None) -> None:
            await _do_reset(key, spec["label"])

        save_btn = ui.button("Save", on_click=_save).props("color=primary dense")
        reset_btn = ui.button("Reset", on_click=_reset).props("flat dense")
        if locked:
            save_btn.disable()
            reset_btn.disable()
            number.disable()
            if never_toggle is not None:
                never_toggle.disable()


def _render_bool_row(
    spec: dict[str, Any],
    current_value: Any,
    locked: bool,
    delegated_to: str | None,
    role: str,
    controls: dict[str, Any],
) -> None:
    # Bool rows save the moment the toggle flips — no Save/Reset
    # buttons. The green "saved" toast is the confirmation. This keeps
    # the on/off state and the persisted state visibly in lock-step.
    key = spec["key"]
    _left, right = _row_frame(spec, locked, delegated_to, role)
    with right:
        switch = ui.switch(
            "Enabled" if bool(current_value) else "Disabled",
            value=bool(current_value),
        ).props("dense")
        # Earlier-rendered "Require symbol" stashes its value so the
        # later "Allowed symbols" row can set its initial enabled state.
        if key == "password_require_symbol":
            controls["password_require_symbol_value"] = bool(current_value)

        async def _on_change(_e: Any = None) -> None:
            on = bool(switch.value)
            switch.text = "Enabled" if on else "Disabled"
            await _do_save(key, on, spec["label"])
            # "Require symbol" off → grey out the Allowed-symbols row.
            if key == "password_require_symbol":
                setter = controls.get("password_allowed_symbols")
                if setter is not None:
                    setter(on)

        if locked:
            switch.disable()
        else:
            switch.on_value_change(_on_change)


def _render_str_row(
    spec: dict[str, Any],
    current_value: Any,
    locked: bool,
    delegated_to: str | None,
    role: str,
    controls: dict[str, Any],
) -> None:
    key = spec["key"]
    _left, right = _row_frame(spec, locked, delegated_to, role)
    with right:
        # Slightly wider for free-form text. Allowed-symbols is monospace
        # so the operator can see exactly which characters they're saving.
        is_symbols = key == "password_allowed_symbols"
        text_input = (
            ui.input(
                value=str(current_value) if current_value is not None else "",
            )
            .props("dense")
            .style("width: 320px;" + (" font-family: monospace;" if is_symbols else ""))
        )

        async def _save(_e: Any = None) -> None:
            await _do_save(key, str(text_input.value or ""), spec["label"])

        async def _reset(_e: Any = None) -> None:
            await _do_reset(key, spec["label"])

        save_btn = ui.button("Save", on_click=_save).props("color=primary dense")
        reset_btn = ui.button("Reset", on_click=_reset).props("flat dense")
        if locked:
            save_btn.disable()
            reset_btn.disable()
            text_input.disable()
        elif is_symbols:
            # Allowed symbols is meaningless when "Require symbol" is off:
            # grey out the field + buttons, and follow the toggle live.
            def _set_enabled(on: bool) -> None:
                for element in (text_input, save_btn, reset_btn):
                    element.set_enabled(on)

            controls["password_allowed_symbols"] = _set_enabled
            _set_enabled(bool(controls.get("password_require_symbol_value", True)))


def _render_secret_row(
    spec: dict[str, Any],
    locked: bool,
    delegated_to: str | None,
    role: str,
) -> None:
    """Secret rows show a masked '••••••••' display + 'Replace' button.

    The current value is never shown — the API returns a redacted
    sentinel, and the UI mirrors that. Replace opens a small dialog
    where the operator types the new secret in full.
    """
    key = spec["key"]
    _left, right = _row_frame(spec, locked, delegated_to, role)
    with right:
        ui.label("••••••••").style(
            "font-family: monospace; color: #666; "
            "padding: 0.25rem 0.5rem; background: #f5f5f5; border-radius: 4px;"
        )

        def _open_replace_dialog(_e: Any = None) -> None:
            with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
                ui.label(f"Replace: {spec['label']}").style("font-size: 1.05rem; font-weight: 600;")
                ui.label(spec["description"]).style("color: #555; font-size: 0.85rem;")
                new_value = ui.input("New value").props("type=password dense").classes("w-full")
                ui.label("Leave blank and save to clear the value.").style(
                    "color: #888; font-size: 0.8rem;"
                )

                async def _save_secret() -> None:
                    await _do_save(key, str(new_value.value or ""), spec["label"])
                    dialog.close()

                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button("Save", on_click=_save_secret).props("color=primary")
            dialog.open()

        async def _reset(_e: Any = None) -> None:
            await _do_reset(key, spec["label"])

        replace_btn = ui.button("Replace", on_click=_open_replace_dialog).props(
            "color=primary dense"
        )
        reset_btn = ui.button("Reset", on_click=_reset).props("flat dense")
        if locked:
            replace_btn.disable()
            reset_btn.disable()


def _render_enum_row(
    spec: dict[str, Any],
    current_value: Any,
    locked: bool,
    delegated_to: str | None,
    role: str,
) -> None:
    key = spec["key"]
    choices: list[str] = list(spec.get("choices") or [])
    # Map each stored choice value to a human label so the dropdown
    # shows "Regular User" / "Invite only", never "regular_user". The
    # dict form of ``ui.select`` keeps the raw value as ``select.value``.
    options = {choice: _choice_label(key, choice) for choice in choices}
    _left, right = _row_frame(spec, locked, delegated_to, role)
    with right:
        select = (
            ui.select(
                options=options,
                value=str(current_value) if current_value in choices else choices[0],
            )
            .props("dense")
            .style("min-width: 200px;")
        )

        async def _save(_e: Any = None) -> None:
            await _do_save(key, str(select.value), spec["label"])

        async def _reset(_e: Any = None) -> None:
            await _do_reset(key, spec["label"])

        save_btn = ui.button("Save", on_click=_save).props("color=primary dense")
        reset_btn = ui.button("Reset", on_click=_reset).props("flat dense")
        if locked:
            save_btn.disable()
            reset_btn.disable()
            select.disable()


def _render_image_row(
    spec: dict[str, Any],
    current_value: Any,
    locked: bool,
    delegated_to: str | None,
    role: str,
) -> None:
    key = spec["key"]
    accepted: list[str] = list(spec.get("accepted_mime_types") or [])
    _left, right = _row_frame(spec, locked, delegated_to, role)
    with right:
        # Render the actual image — the uploaded override when one is
        # set, otherwise the shipped default — so the operator sees the
        # current branding, not just a filename. Favicon has no shipped
        # default asset, so it falls back to a text note.
        with ui.column().classes("items-start gap-1"):
            # Raw <img> with a host-absolute src via ui.html: ui.image()
            # rewrites the URL relative to NiceGUI's /ui mount, so the
            # asset (served from the outer app at /matofx-assets or
            # /api/branding) 404s and shows a blank box. sanitize=False
            # so the tag survives the HTML sanitiser. Mirrors login.py.
            img_style = (
                "max-height: 64px; max-width: 240px; height: auto; "
                "object-fit: contain; display: block; "
                "background: #f5f5f5; border-radius: 4px; padding: 0.25rem;"
            )
            if current_value:
                # Cache-bust on the basename so a re-upload refreshes.
                ui.html(
                    f'<img src="/api/branding/{key}?v={current_value}" '
                    f'alt="{spec["label"]}" style="{img_style}">',
                    sanitize=False,
                )
                ui.label(str(current_value)).style(
                    "font-family: monospace; color: #555; font-size: 0.8rem;"
                )
            else:
                default_url = _default_image_url(key)
                if default_url:
                    ui.html(
                        f'<img src="{default_url}" alt="{spec["label"]}" style="{img_style}">',
                        sanitize=False,
                    )
                    ui.label("Bundled default").style("color: #888; font-size: 0.8rem;")
                else:
                    ui.label("(using bundled default)").style("color: #888; font-size: 0.85rem;")

        async def _handle_upload(event: Any) -> None:
            # NiceGUI's ``ui.upload`` event carries a ``file`` attribute
            # (a SpooledTemporaryFile-alike); ``read()`` returns the
            # bytes. The earlier ``e.content``/``e.type``/``e.name`` form
            # silently AttributeError'd inside the NiceGUI loop, which
            # is why "saving" appeared to do nothing — the API was
            # never called.
            try:
                data = await event.file.read()
                filename = event.file.name or key
                mime = event.file.content_type or "application/octet-stream"
            except AttributeError:
                ui.notify(
                    "Could not read the uploaded file (unexpected event shape).",
                    type="negative",
                )
                return
            client = ApiClient(get_app(), token=current_token())
            try:
                await client.upload_general_setting_image(
                    key, filename=filename, data=data, mime_type=mime
                )
            except ApiError as exc:
                ui.notify(f"Upload failed: {exc.detail}", type="negative")
                return
            ui.notify(f"{spec['label']} uploaded.", type="positive")
            ui.navigate.to("/admin/general-settings")

        upload = ui.upload(
            label="Replace",
            on_upload=_handle_upload,
            auto_upload=True,
        ).props(f'accept="{",".join(accepted)}" dense flat' if accepted else "dense flat")

        async def _reset(_e: Any = None) -> None:
            await _do_reset(key, spec["label"])

        reset_btn = ui.button("Reset to default", on_click=_reset).props("flat dense")
        if locked:
            upload.disable()
            reset_btn.disable()


# ---- API helpers --------------------------------------------------------


async def _do_save(key: str, value: Any, label: str) -> None:
    client = ApiClient(get_app(), token=current_token())
    try:
        await client.set_general_setting(key, value=value)
    except ApiError as exc:
        ui.notify(f"Save failed: {exc.detail}", type="negative")
        return
    ui.notify(f"{label} saved.", type="positive")


async def _save_inactivity_threshold(new_value: int, previous_value: Any, label: str) -> None:
    """Threshold-reduction confirm dance for ``auto_disable_inactive_days``.

    When the new value is 0 (feature off) or higher than the current
    setting (never disables new users on save), the save proceeds
    without a preview. Otherwise we GET the preview from the API,
    show the count + "Show preview" expander, and only call the
    actual save endpoint after the operator confirms.
    """
    key = "auto_disable_inactive_days"
    previous_int = _as_int(previous_value)

    # Feature off, or no-op, or threshold raised — proceed silently.
    if new_value <= 0:
        await _do_save(key, new_value, label)
        ui.navigate.to("/admin/general-settings")
        return
    if previous_int > 0 and new_value >= previous_int:
        await _do_save(key, new_value, label)
        ui.navigate.to("/admin/general-settings")
        return

    # Threshold being introduced or reduced — fetch the preview.
    client = ApiClient(get_app(), token=current_token())
    try:
        preview = await client.preview_inactive_user_sweep(threshold_days=new_value, limit=100)
    except ApiError as exc:
        ui.notify(f"Couldn't preview the change: {exc.detail}", type="negative")
        return

    count = int(preview.get("count") or 0)
    users = list(preview.get("users") or [])

    if count == 0:
        await _do_save(key, new_value, label)
        ui.navigate.to("/admin/general-settings")
        return

    plural = "user" if count == 1 else "users"
    with ui.dialog() as dialog, ui.card().style("min-width: 560px; max-width: 800px;"):
        ui.label("Confirm inactivity threshold change").style(
            "font-size: 1.1rem; font-weight: 600;"
        )
        ui.label(
            f"Saving this change will cause the next inactivity sweep to "
            f"disable {count:,} {plural}. Super Users are excluded from the "
            f"count. You can still re-activate any account afterwards from "
            f"the Admin → Users page."
        ).style("color: #555;")

        with ui.expansion("Show preview list").classes("w-full"):
            ui.label("Showing up to 100 of the most-inactive matching accounts.").style(
                "color: #888; font-size: 0.85rem;"
            )
            with ui.column().classes("w-full gap-1").style("max-height: 360px; overflow-y: auto;"):
                for user in users:
                    days = int(user.get("days_inactive") or 0)
                    role_name = _role_label(user.get("role"))
                    line = (
                        f"{user.get('full_name') or user.get('username')} "
                        f"({user.get('username')}) — {role_name} — "
                        f"idle {days} day{'s' if days != 1 else ''}"
                    )
                    ui.label(line).style("font-size: 0.85rem;")
                if count > len(users):
                    ui.label(f"... and {count - len(users):,} more not shown.").style(
                        "color: #888; font-size: 0.8rem; font-style: italic;"
                    )

        async def _confirm() -> None:
            await _do_save(key, new_value, label)
            dialog.close()
            ui.navigate.to("/admin/general-settings")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button(
                f"Yes — save and disable {count:,} {plural}",
                on_click=_confirm,
            ).props("color=warning unelevated")
    dialog.open()


async def _do_reset(key: str, label: str) -> None:
    client = ApiClient(get_app(), token=current_token())
    try:
        await client.reset_general_setting(key)
    except ApiError as exc:
        ui.notify(f"Reset failed: {exc.detail}", type="negative")
        return
    ui.notify(f"{label} reset to default.", type="positive")
    ui.navigate.to("/admin/general-settings")


def _as_int(value: Any) -> int:
    """Coerce a JSON value to int, defaulting to 0 on miss."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return int(value)
    return 0


def _as_float(value: Any) -> float:
    """Coerce a JSON value to float, defaulting to 0.0 on miss."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    return 0.0
