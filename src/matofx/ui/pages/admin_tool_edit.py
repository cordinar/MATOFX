"""Admin: Edit tool (``/ui/admin/tools/{alias}/edit``).

Single-page metadata edit form — same field shape as the New Tool
page minus the alias (immutable) and the file uploads (those move
to a separate "Replace bundle" modal so the metadata-edit and
replace-bundle paths stay distinct).

Saving the form calls :func:`update_tool_descriptor` which only
rewrites the DB row's descriptor JSON. Replace bundle calls
:func:`replace_tool_bundle` which also restages the on-disk bundle.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from nicegui import ui

from matofx.tools.descriptor import MAX_GUIDE_IMAGES, tool_descriptions
from matofx.ui._features import materials_database_enabled, vv_tracking_enabled
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import (
    render_shell,
    render_teams_pending_banner,
    require_logged_in,
)
from matofx.ui.pages._descriptor_wizard import (
    build_descriptor_payload,
    build_upstream_wiring,
    reconcile_downstream_registrations,
    render_descriptor_wizard,
    state_from_descriptor,
    wired_upstream_aliases,
)
from matofx.ui.state import current_token

_APPROVALS_OPTIONS: dict[str, str] = {
    "none": "None — jobs run without approval",
    "pre": "Pre-run approval required",
    "post": "Post-run approval required",
    "both": "Pre + post-run approval required",
}

_STATE_OPTIONS: dict[str, str] = {
    "new": "New (author-only)",
    "test": "Testing (beta-tester visible)",
    "released": "Released (everyone)",
}

_WORLD_TEAM_NAME = "World"
"""Always offered in the team picker even if the seed migration hasn't run."""


@ui.page("/admin/tools/{alias}/edit")
async def admin_tool_edit_page(alias: str) -> None:
    """Render the Edit Tool form for ``alias``."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())
    try:
        tool = await client.get_admin_tool(alias)
        teams = await client.list_teams()
        users = await client.list_users()
        existing_tools = await client.list_admin_tools()
    except ApiError as exc:
        with ui.column().classes("w-full max-w-5xl mx-auto p-6"):
            ui.label(f"We couldn't load that tool: {exc.detail}").style("color: #c0392b;")
        return

    # Python envs populate the "Execution environment" picker. Listing
    # needs ``python_envs.manage`` (admins have it via migration 0012);
    # if the caller somehow lacks it we degrade to "default only" rather
    # than blanking the whole edit form.
    try:
        python_envs = await client.list_python_envs()
    except ApiError:
        python_envs = []

    # Pre-fetch the live materials catalogue (when the feature is on) so
    # a `material` input renders a class select + a class-scoped
    # multiselect of required properties. An empty dict on error means
    # "feature on but couldn't load" — the wizard falls back gracefully.
    # ``{class_name: [{"slug": str, "prop_name": str}, ...]}`` — see
    # admin_tool_new.py for the wizard contract; the multiselect shows
    # display labels but the descriptor stores immutable slugs.
    material_classes_with_props: dict[str, list[dict[str, str]]] | None = None
    if materials_database_enabled():
        try:
            classes = await client.list_material_classes()
            material_classes_with_props = {
                str(c["name"]): [
                    {"slug": str(p["slug"]), "prop_name": str(p["prop_name"])}
                    for p in (c.get("properties") or [])
                ]
                for c in classes
            }
        except ApiError:
            material_classes_with_props = {}

    team_names = {str(t["name"]) for t in teams}
    team_names.add(_WORLD_TEAM_NAME)
    team_options = sorted(team_names)
    approver_options = {
        str(u["username"]): f"{u.get('full_name') or u['username']} ({u['username']})"
        for u in users
        if u.get("is_approver") and u.get("is_active")
    }
    # Downstream picker: alias → name dict so labels read as tool names.
    downstream_options = {
        str(t["alias"]): str(t["name"])
        for t in sorted(existing_tools, key=lambda t: str(t.get("name") or ""))
        if not t.get("archived_at") and str(t["alias"]) != alias
    }

    with ui.column().classes("w-full mx-auto p-6 gap-4").style("max-width: 1700px;"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.row().classes("items-center gap-2"):
                ui.button(
                    icon="arrow_back",
                    on_click=lambda: ui.navigate.to("/admin/tools"),
                ).props("flat round dense")
                # Header reads as the human-friendly name + current version;
                # alias appears in the subtitle below for the operator's
                # reference but isn't the headline.
                ui.label(f"{tool.get('name') or alias} (v{tool.get('version', '')})").style(
                    "font-size: 1.5rem; font-weight: 600;"
                )
            ui.button(
                "Replace bundle",
                icon="folder_zip",
                on_click=lambda _e=None, _a=alias: _open_replace_bundle_dialog(_a, tool),
            ).props("flat color=warning")

        ui.label(f"alias: {alias} · Bundle on disk: {tool.get('bundle_path', '')}").style(
            "color: #888; font-size: 0.85rem;"
        )
        if tool.get("teams_pending"):
            render_teams_pending_banner()

        # Three tabs: metadata edit (existing), file editor (Session
        # C C2), inputs/outputs wizard (Session C C3). The header
        # above stays put on tab switches so the operator never
        # loses context. The wizard tab is the only post-install
        # path to edit inputs[] / outputs[] / groups / layout — the
        # file editor explicitly excludes descriptor.json.
        vv_tab_enabled = vv_tracking_enabled()
        with (
            ui.tabs()
            .classes("w-full")
            .props("align=left active-color=primary indicator-color=primary")
            .style(
                "background-color: transparent; border-bottom: 1px solid var(--matofx-card-border);"
            ) as tabs
        ):
            metadata_tab = ui.tab("Metadata", icon="tune").props("no-caps")
            files_tab = ui.tab("Files", icon="folder_open").props("no-caps")
            io_tab = ui.tab("Inputs & Outputs", icon="dynamic_form").props("no-caps")
            vv_tab = ui.tab("V&V", icon="verified").props("no-caps") if vv_tab_enabled else None
        # The panels stream to the browser one after another as each is
        # built, so an operator who clicks ahead (Inputs & Outputs is the
        # slowest — it prefetches upstream tool details) would otherwise
        # see an empty panel for seconds. Open every panel with a loading
        # placeholder first, then fill them in order.
        with (
            ui.tab_panels(tabs, value=metadata_tab)
            .classes("w-full")
            .style("background-color: transparent;")
        ):
            with ui.tab_panel(metadata_tab):
                metadata_box = _loading_panel_box()
            with ui.tab_panel(files_tab):
                files_box = _loading_panel_box()
            with ui.tab_panel(io_tab):
                io_box = _loading_panel_box()
            vv_box = None
            if vv_tab is not None:
                with ui.tab_panel(vv_tab):
                    vv_box = _loading_panel_box()

        async with _filling(metadata_box):
            await _render_form(
                tool, team_options, approver_options, downstream_options, python_envs
            )
        async with _filling(files_box):
            await _render_files_tab(client, alias=alias, tool=tool)
        async with _filling(io_box):
            await _render_wizard_tab(
                client,
                alias=alias,
                tool=tool,
                existing_tools=existing_tools,
                material_classes_with_props=material_classes_with_props,
            )
        if vv_box is not None:
            async with _filling(vv_box):
                await _render_vv_tab(client, tool=tool)


def _loading_panel_box() -> ui.column:
    """A tab panel's content box, showing a spinner until it's filled."""
    box = ui.column().classes("w-full gap-0")
    with (
        box,
        ui.row()
        .classes("w-full items-center justify-center gap-3 matofx-panel-loading")
        .style("padding: 3rem 0; color: var(--matofx-muted-text);"),
    ):
        ui.spinner(size="lg", color="primary")
        ui.label("Loading…")
    return box


@contextlib.asynccontextmanager
async def _filling(box: ui.column) -> AsyncIterator[None]:
    """Render into ``box``, dropping its loading placeholder when done.

    The placeholder goes even if rendering raises, so a failed panel never
    leaves a spinner turning forever.
    """
    try:
        with box:
            yield
    finally:
        for child in list(box):
            if "matofx-panel-loading" in child.classes:
                child.delete()


_PYTHON_ENV_DEFAULT_VALUE = 0
"""Sentinel select value for "no pin — use the main MATOFX environment".

Real ``python_env`` ids are ``>= 1`` so ``0`` can't collide; it maps
back to ``None`` (clear the pin) on save."""


def _build_python_env_options(
    python_envs: list[dict[str, Any]],
    current_id: int | None,
) -> tuple[dict[int, str], int]:
    """Build the ``{value: label}`` map + initial value for the env picker.

    Only *active* envs are offered as new choices (deactivation means
    "new tools can't pick this env"). If the tool is currently pinned
    to an env that's since been deactivated or removed, that id is
    still listed — flagged inactive/unknown — so the operator can see
    what's set and switch away from it. The returned initial value is
    the current pin (or the default sentinel when unset).
    """
    options: dict[int, str] = {
        _PYTHON_ENV_DEFAULT_VALUE: "Default — main MATOFX environment",
    }
    seen_current = current_id is None
    for env in python_envs:
        env_id = int(env["id"])
        if not env.get("is_active") and env_id != current_id:
            continue  # inactive envs aren't offered as fresh choices
        label = f"{env.get('name')} ({env.get('interpreter_path')})"
        if not env.get("is_active"):
            label += " — inactive"
        options[env_id] = label
        if env_id == current_id:
            seen_current = True
    # Pinned env no longer in the registry at all (hard-removed): keep
    # the operator informed rather than silently snapping to default.
    if not seen_current and current_id is not None:
        options[current_id] = f"#{current_id} — no longer available"
    return options, (current_id if current_id is not None else _PYTHON_ENV_DEFAULT_VALUE)


async def _render_form(
    tool: dict[str, Any],
    team_options: list[str],
    approver_options: dict[str, str],
    downstream_options: dict[str, str],
    python_envs: list[dict[str, Any]],
) -> None:
    """Pre-populated metadata form. Submit calls update-descriptor."""
    descriptor = dict(tool.get("descriptor") or {})
    resources = dict(descriptor.get("resources") or {})
    licences = list(descriptor.get("licences_required") or [])
    licences_csv = ", ".join(str(item.get("name", "")) for item in licences if item.get("name"))
    # Run cap is a row-level column (not part of the descriptor); track
    # its loaded value so save can detect a change and the version-bump
    # hint can reflect "run-cap-only" edits.
    original_run_cap = tool.get("run_cap")
    # Python env is also a row-level column. Build the picker options +
    # track the loaded value the same way so save only POSTs on a change.
    original_python_env_id = tool.get("python_env_id")
    python_env_options, python_env_initial = _build_python_env_options(
        python_envs, original_python_env_id
    )

    with ui.row().classes("w-full no-wrap gap-4 items-start"):
        with ui.column().classes("gap-3").style("flex: 1 1 0; min-width: 0;"):
            with ui.card().classes("w-full"):
                ui.label("Identity").style("font-weight: 600;")
                ui.label(f"Alias: {tool['alias']} (read-only)").style(
                    "color: #888; font-size: 0.85rem;"
                )
                name = (
                    ui.input("Name *", value=str(tool.get("name") or ""))
                    .classes("w-full")
                    .props("dense")
                )
                _seed_short, _seed_long = tool_descriptions(descriptor)
                short_description_in = (
                    ui.textarea("Short description *", value=_seed_short)
                    .classes("w-full")
                    .props("dense outlined maxlength=200 counter")
                )
                ui.label(
                    "Mandatory, max 200 characters — the only description shown on "
                    "the Quick Access card."
                ).style("color: #888; font-size: 0.8rem;")
                long_description_in = (
                    ui.textarea("Long description", value=_seed_long or "")
                    .classes("w-full")
                    .props("dense outlined maxlength=2048 counter")
                )
                ui.label(
                    "Optional — fuller text shown above the input form and in the "
                    "card's info dialog."
                ).style("color: #888; font-size: 0.8rem;")
                # Version is server-controlled — show the current value
                # plus a live "Saving will bump to X.Y.Z" hint computed
                # from what the operator has changed. The operator can't
                # type a version directly.
                current_version = str(tool.get("version") or "0.1.0")
                ui.label(f"Current version: {current_version}").style(
                    "font-weight: 500; margin-top: 0.5rem;"
                )
                version_bump_hint = ui.label("").style(
                    "color: #b58900; font-size: 0.85rem; min-height: 1.1em;"
                )
                state_select = (
                    ui.select(
                        _STATE_OPTIONS,
                        label="State (lifecycle)",
                        value=str(tool.get("state") or "new"),
                    )
                    .classes("w-full")
                    .props("dense")
                )
                ui.label(
                    "New → Test → Released. Promote to make the tool visible "
                    "to wider audiences. Rollbacks are allowed when something "
                    "needs to be pulled back."
                ).style("color: #888; font-size: 0.8rem;")

            with ui.card().classes("w-full"):
                ui.label("Teams & relations").style("font-weight: 600;")
                teams_picker = (
                    ui.select(
                        team_options,
                        multiple=True,
                        label="Teams",
                        with_input=True,
                    )
                    .classes("w-full")
                    .props("dense use-chips")
                )
                teams_picker.value = list(descriptor.get("teams") or [])
                _wire_world_mutual_exclusion(teams_picker)
                ui.label(
                    "Pick 'World' to make this tool visible to everyone. Pick "
                    "specific teams to restrict tool access to Beta Testers + "
                    "Regular Users members of those teams. Admins and Super "
                    "Users always see every tool."
                ).style("color: #888; font-size: 0.8rem;")
                downstream_picker = (
                    ui.select(
                        downstream_options,
                        multiple=True,
                        label="Downstream tools",
                        with_input=True,
                    )
                    .classes("w-full")
                    .props("dense use-chips")
                )
                downstream_picker.value = list(descriptor.get("downstream_tools") or [])

            with ui.card().classes("w-full"):
                ui.label("Parametric studies - run cap").style("font-weight: 600;")
                ui.label(
                    "Maximum number of runs a single parametric study of this "
                    "tool may generate. Leave empty to use the install-wide "
                    "default (currently 500). Saved together with the rest of "
                    "the form by 'Save changes' below — no version bump."
                ).style("color: #888; font-size: 0.85rem;")
                run_cap_in = (
                    ui.number(
                        "Run cap (leave empty for default)",
                        value=original_run_cap,
                        min=1,
                    )
                    .classes("w-full")
                    .props("dense")
                )

            with ui.card().classes("w-full"):
                ui.label("Execution environment").style("font-weight: 600;")
                ui.label(
                    "The registered Python environment this tool's jobs run "
                    "under. Leave as the default to use the main MATOFX "
                    "environment (the server's own interpreter). Manage the "
                    "list on the Execution environments page. Saved together "
                    "with the rest of the form — no version bump."
                ).style("color: #888; font-size: 0.85rem;")
                python_env_select = (
                    ui.select(
                        python_env_options,
                        label="Python environment",
                        value=python_env_initial,
                    )
                    .classes("w-full")
                    .props("dense")
                )

        with ui.column().classes("gap-3").style("flex: 1 1 0; min-width: 0;"):
            with ui.card().classes("w-full"):
                ui.label("Resources").style("font-weight: 600;")
                with ui.row().classes("w-full gap-2"):
                    cpu_cores = (
                        ui.number(
                            "CPU cores *",
                            value=int(resources.get("cpu_cores") or 1),
                            min=1,
                            max=128,
                            step=1,
                        )
                        .classes("flex-1")
                        .props("dense")
                    )
                    ram_mb = (
                        ui.number(
                            "RAM (MB) *",
                            value=int(resources.get("ram_mb") or 512),
                            min=32,
                            max=1024 * 1024,
                            step=64,
                        )
                        .classes("flex-1")
                        .props("dense")
                    )
                gpu_memory_mb = (
                    ui.number(
                        "GPU memory (MB, leave 0 for none)",
                        value=int(resources.get("gpu_memory_mb") or 0),
                        min=0,
                        max=512 * 1024,
                        step=64,
                    )
                    .classes("w-full")
                    .props("dense")
                )

            with ui.card().classes("w-full"):
                ui.label("Licences").style("font-weight: 600;")
                licences_in = (
                    ui.input("Licence names (comma-separated)", value=licences_csv)
                    .classes("w-full")
                    .props("dense")
                )

            with ui.card().classes("w-full"):
                ui.label("Approvals").style("font-weight: 600;")
                approvals_policy = (
                    ui.select(
                        _APPROVALS_OPTIONS,
                        label="Approval policy",
                        value=str(descriptor.get("approvals_policy") or "none"),
                    )
                    .classes("w-full")
                    .props("dense")
                )
                approvers_picker = (
                    ui.select(
                        approver_options,
                        multiple=True,
                        label="Approvers (eligible users)",
                        with_input=True,
                    )
                    .classes("w-full")
                    .props("dense use-chips")
                )
                approvers_picker.value = list(descriptor.get("approver_usernames") or [])
                if str(approvals_policy.value) == "none":
                    approvers_picker.value = []
                    approvers_picker.disable()

                def _on_policy_change(_e: Any = None) -> None:
                    """Activate / clear the approvers picker based on the policy."""
                    if str(approvals_policy.value) == "none":
                        approvers_picker.value = []
                        approvers_picker.disable()
                    else:
                        approvers_picker.enable()

                approvals_policy.on("update:model-value", _on_policy_change)

            with ui.card().classes("w-full"):
                ui.label("Notifications").style("font-weight: 600;")
                # Unticked unless this tool's descriptor says otherwise —
                # including when the key is absent, which is what the
                # runtime gate reads it as too.
                email_notifications = ui.checkbox(
                    "Send completion / failure emails for this tool",
                    value=bool(descriptor.get("email_notifications", False)),
                )
                ui.label(
                    "Off unless ticked. When on, the job's submitter is emailed as "
                    "it starts, completes or fails — subject to the site-wide job-email "
                    "setting and each user's own preference."
                ).style("color: #888; font-size: 0.85rem;")

            with ui.card().classes("w-full"):
                ui.label("Icon").style("font-weight: 600;")
                icon_pending = _render_icon_section(tool)

            with ui.card().classes("w-full"):
                ui.label("Parameter guide images").style("font-weight: 600;")
                guides_pending = _render_guides_section(tool)

        message = ui.label("").style("color: #c0392b;")

    save_button: ui.button | None = None
    gate_hint: ui.label | None = None
    current_state = str(tool.get("state") or "new")

    def _save_blockers() -> list[str]:
        """Every reason Save is currently blocked (mirrors the server's rules).

        Enumerates *all* outstanding reasons at once (like the Submit-job
        gate), so the operator never satisfies one only to discover
        another. The approvals rule mirrors ``ToolDescriptor``'s
        validator: a non-``none`` policy needs at least one approver.
        """
        reasons: list[str] = []
        if not (name.value or "").strip():
            reasons.append("Name is required.")
        if not (short_description_in.value or "").strip():
            reasons.append("Short description is required.")
        elif len((short_description_in.value or "").strip()) > 200:
            reasons.append("Short description must be 200 characters or fewer.")
        policy = str(approvals_policy.value or "none")
        if policy != "none" and not list(approvers_picker.value or []):
            if not approver_options:
                reasons.append(
                    "No users are marked as approvers yet — mark one under "
                    "People & access, then pick them here (or set the approval "
                    "policy to None)."
                )
            else:
                reasons.append(
                    "Pick at least one approver for the chosen approval policy "
                    "(or set the policy to None)."
                )
        return reasons

    def _has_metadata_changes() -> bool:
        """Quick check: would saving produce a patch bump?

        Mirrors the descriptor diff the backend computes — drives the
        reactive "Saving will bump to X.Y.Z" hint without needing a
        full round-trip to find out what'll change.
        """
        new_name = str(name.value or "").strip()
        new_short = str(short_description_in.value or "").strip()
        new_long = str(long_description_in.value or "").strip() or None
        old_short, old_long = tool_descriptions(descriptor)
        new_teams = sorted(teams_picker.value or [])
        old_teams = sorted(descriptor.get("teams") or [])
        new_downstream = sorted(downstream_picker.value or [])
        old_downstream = sorted(descriptor.get("downstream_tools") or [])
        new_approvers = sorted(approvers_picker.value or [])
        old_approvers = sorted(descriptor.get("approver_usernames") or [])
        return (
            new_name != str(tool.get("name") or "")
            or new_short != old_short
            or new_long != old_long
            or new_teams != old_teams
            or new_downstream != old_downstream
            or int(cpu_cores.value or 1) != int(resources.get("cpu_cores") or 1)
            or int(ram_mb.value or 512) != int(resources.get("ram_mb") or 512)
            or int(gpu_memory_mb.value or 0) != int(resources.get("gpu_memory_mb") or 0)
            or str(approvals_policy.value or "none")
            != str(descriptor.get("approvals_policy") or "none")
            or new_approvers != old_approvers
            or bool(email_notifications.value) != bool(descriptor.get("email_notifications", False))
            or [s.strip() for s in (licences_in.value or "").split(",") if s.strip()]
            != [
                str(item.get("name", ""))
                for item in (descriptor.get("licences_required") or [])
                if item.get("name")
            ]
        )

    def _run_cap_payload() -> int | None:
        """Normalise the run-cap field to ``int | None`` (empty → None)."""
        value = run_cap_in.value
        return int(value) if value is not None and str(value) != "" else None

    def _python_env_payload() -> int | None:
        """Map the env-select value to ``int | None`` (default sentinel → None)."""
        value = python_env_select.value
        if value is None or int(value) == _PYTHON_ENV_DEFAULT_VALUE:
            return None
        return int(value)

    def _refresh_validation(_e: Any = None) -> None:
        # Disable Save while anything's outstanding AND spell out every
        # reason in a muted line under the buttons (the same pattern as
        # the Submit-job gate), so the operator fixes it here rather than
        # meeting a raw server validation error on Save. ``message`` stays
        # for API errors raised in ``_save``. Also keeps the version-bump
        # hint in sync with the form's current diff.
        from matofx.tools.versioning import (  # noqa: PLC0415
            bump_major,
            bump_patch,
            first_release_snap,
        )

        blockers = _save_blockers()
        if save_button is not None:
            if blockers:
                save_button.disable()
            else:
                save_button.enable()
        if gate_hint is not None:
            gate_hint.text = ("Before saving — " + "  •  ".join(blockers)) if blockers else ""
            gate_hint.visible = bool(blockers)

        new_state = str(state_select.value or current_state)
        metadata_changed = _has_metadata_changes()
        state_changed = new_state != current_state
        run_cap_changed = _run_cap_payload() != original_run_cap
        python_env_changed = _python_env_payload() != original_python_env_id

        # Walk the actual save order so the hint matches the server:
        # update_descriptor first (patch on metadata change), then
        # promote (first-release snap or re-release patch when moving
        # TO released).
        projected = current_version
        reasons: list[str] = []
        if metadata_changed:
            projected = bump_patch(projected)
            reasons.append("patch for metadata edit")
        if state_changed and new_state == "released":
            first_snap = first_release_snap(projected)
            if first_snap is not None:
                projected = first_snap
                reasons.append("snap to 1.0.0 for first promotion to Released")
            else:
                projected = bump_major(projected)
                reasons.append("major for re-promotion to Released")

        if projected != current_version:
            version_bump_hint.text = (
                f"Saving will bump version to {projected} ({' + '.join(reasons)})."
            )
        elif state_changed:
            version_bump_hint.text = (
                f"Saving will change state from {current_state} to {new_state} — no version change."
            )
        elif run_cap_changed and python_env_changed:
            version_bump_hint.text = (
                "Saving will update the run cap + execution environment — no version change."
            )
        elif run_cap_changed:
            version_bump_hint.text = "Saving will update the run cap — no version change."
        elif python_env_changed:
            version_bump_hint.text = (
                "Saving will update the execution environment — no version change."
            )
        else:
            version_bump_hint.text = "No version change — saving has no effect."

    for field in (name, short_description_in, long_description_in, licences_in):
        field.on("update:model-value", _refresh_validation)
    for picker in (
        teams_picker,
        downstream_picker,
        approvers_picker,
        approvals_policy,
        state_select,
        python_env_select,
    ):
        picker.on("update:model-value", _refresh_validation)
    for num in (cpu_cores, ram_mb, gpu_memory_mb, run_cap_in):
        num.on("update:model-value", _refresh_validation)
    email_notifications.on("update:model-value", _refresh_validation)

    async def _save() -> None:
        if _save_blockers():
            return  # belt and braces; button is disabled while invalid
        licence_names = [s.strip() for s in (licences_in.value or "").split(",") if s.strip()]
        body: dict[str, Any] = {
            "name": str(name.value).strip(),
            # Server picks the new version — we send the current value;
            # backend overrides on the actual write. Required by the
            # Pydantic schema's min_length, so keep it populated.
            "version": current_version,
            "short_description": str(short_description_in.value).strip(),
            "long_description": (str(long_description_in.value).strip() or None),
            "teams": list(teams_picker.value or []),
            "downstream_tools": list(downstream_picker.value or []),
            "cpu_cores": int(cpu_cores.value or 1),
            "ram_mb": int(ram_mb.value or 512),
            "gpu_memory_mb": int(gpu_memory_mb.value or 0),
            "licences_required": [{"name": n, "count": 1} for n in licence_names],
            # duration_class isn't exposed in the form — Phase 1 has
            # no scheduler that consumes it. Default keeps the
            # descriptor valid; Phase 2 may bring the field back.
            "duration_class": "medium",
            "approvals_policy": str(approvals_policy.value or "none"),
            "approver_usernames": list(approvers_picker.value or []),
            "email_notifications": bool(email_notifications.value),
        }

        client = ApiClient(get_app(), token=current_token())
        new_state = str(state_select.value or current_state)
        try:
            await client.update_tool_descriptor(str(tool["alias"]), body=body)
            if icon_pending.get("bytes"):
                await client.update_tool_icon(
                    str(tool["alias"]),
                    icon_bytes=bytes(icon_pending["bytes"]),
                    icon_filename=str(icon_pending["filename"] or f"{tool['alias']}.png"),
                )
            if guides_pending.get("dirty"):
                manifest, new_images = _guides_payload(guides_pending["items"])
                await client.update_tool_guides(
                    str(tool["alias"]), manifest=manifest, new_images=new_images
                )
            if new_state != current_state:
                await client.promote_tool(str(tool["alias"]), new_state=new_state)
            # Run cap is a row-level setting (no version bump); ship it
            # in the same Save click when it changed.
            run_cap_payload = _run_cap_payload()
            if run_cap_payload != original_run_cap:
                await client.set_tool_run_cap(str(tool["alias"]), run_cap_payload)
            # Python env is likewise row-level — same one-click pattern.
            python_env_payload = _python_env_payload()
            if python_env_payload != original_python_env_id:
                await client.set_tool_python_env(str(tool["alias"]), python_env_payload)
        except ApiError as exc:
            message.text = exc.detail
            return
        ui.notify("Tool updated.", type="positive")
        ui.navigate.to("/admin/tools")

    with ui.row().classes("w-full justify-end gap-2"):
        ui.button(
            "Cancel",
            on_click=lambda: ui.navigate.to("/admin/tools"),
        ).props("flat")
        save_button = ui.button("Save changes", on_click=_save).props("color=primary")
    # Muted line under the buttons spelling out every reason Save is
    # blocked (mirrors the Submit-job gate). Hidden when nothing's wrong.
    gate_hint = (
        ui.label("").classes("w-full text-right").style("color: #9a7d0a; font-size: 0.85rem;")
    )
    gate_hint.visible = False
    _refresh_validation()


def _render_icon_section(tool: dict[str, Any]) -> dict[str, Any]:
    """Icon preview + uploader for the Edit Tool page.

    Renders the currently-saved icon (if any) and a file picker. When
    the admin uploads a new PNG the preview swaps to it immediately
    so they see what they're about to save. The returned ``pending``
    dict carries ``bytes`` + ``filename`` for the main "Save changes"
    handler to post via ``update_tool_icon`` — no separate Save icon
    button; everything ships in one click.
    """
    import base64  # noqa: PLC0415

    alias = str(tool["alias"])
    descriptor = dict(tool.get("descriptor") or {})
    current_icon = descriptor.get("icon_filename")

    pending: dict[str, Any] = {"bytes": None, "filename": None, "on_change": None}

    status_label = ui.label("").style("color: #888; font-size: 0.85rem;")
    if current_icon:
        status_label.text = f"Current icon: {current_icon}"
    else:
        status_label.text = "No icon set yet — upload one below."
        status_label.style("color: #b58900; font-size: 0.85rem;")

    # Preview swatch — populated from the current saved icon on load
    # and replaced with the freshly-uploaded bytes on each upload.
    preview_box = (
        ui.column()
        .classes("items-center justify-center")
        .style(
            "width: 120px; height: 120px; "
            "border: 1px dashed var(--matofx-card-border); "
            "border-radius: 6px; padding: 4px;"
        )
    )

    def _set_preview(data_url: str | None) -> None:
        preview_box.clear()
        with preview_box:
            if data_url:
                ui.image(data_url).style("width: 100%; height: 100%; object-fit: contain;")
            else:
                ui.icon("image_not_supported").style("font-size: 2.6rem; color: #888;")

    _set_preview(None)

    async def _load_current_preview() -> None:
        if not current_icon:
            return
        client = ApiClient(get_app(), token=current_token())
        try:
            result = await client.get_tool_icon_bytes(alias)
        except Exception:
            return
        if not result:
            return
        raw, mime = result
        b64 = base64.b64encode(raw).decode("ascii")
        _set_preview(f"data:{mime};base64,{b64}")

    ui.timer(0.05, _load_current_preview, once=True)

    def _on_pending_change() -> None:
        if pending["bytes"]:
            b64 = base64.b64encode(bytes(pending["bytes"])).decode("ascii")
            _set_preview(f"data:image/png;base64,{b64}")
            status_label.text = (
                f"New icon pending — saved when you click 'Save changes' ({pending['filename']})."
            )
            status_label.style("color: #43a047; font-size: 0.85rem;")

    pending["on_change"] = _on_pending_change
    _file_uploader(
        label="Icon (PNG)",
        accept=".png,image/png",
        helper="100x100 recommended. Saved into the bundle as <alias>.png.",
        target=pending,
    )
    return pending


_GUIDE_MIME_BY_EXT: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


def _guide_mime(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _GUIDE_MIME_BY_EXT.get(ext, "application/octet-stream")


def _guides_payload(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[str, bytes]]]:
    """Split the pending guide items into a (manifest, new_images) pair.

    ``manifest`` is the ordered desired list for the API; ``new_images``
    are the ``(filename, bytes)`` for entries that are fresh uploads, in
    the same order the manifest lists them.
    """
    manifest: list[dict[str, Any]] = []
    new_images: list[tuple[str, bytes]] = []
    for it in items:
        entry: dict[str, Any] = {"caption": str(it.get("caption") or "")}
        if it.get("existing_filename"):
            entry["existing_filename"] = str(it["existing_filename"])
        else:
            new_images.append((str(it["name"]), bytes(it["upload_bytes"])))
        manifest.append(entry)
    return manifest, new_images


def _render_guides_section(tool: dict[str, Any]) -> dict[str, Any]:
    """Guide-images manager for the Edit Tool page (Metadata tab).

    Lists the tool's current guide images with editable captions, lets
    the admin reorder / delete them and add more, and returns a pending
    dict the main "Save changes" handler posts via ``update_tool_guides``
    — one click, mirroring the icon section. Nothing is written until
    Save, so a cancel discards it all.
    """
    import base64  # noqa: PLC0415

    alias = str(tool["alias"])
    descriptor = dict(tool.get("descriptor") or {})
    existing = descriptor.get("input_guides") or []

    items: list[dict[str, Any]] = []
    for idx, g in enumerate(existing):
        if not isinstance(g, dict):
            continue
        items.append(
            {
                "existing_filename": str(g.get("filename") or ""),
                "orig_index": idx,
                "upload_bytes": None,
                "name": str(g.get("filename") or ""),
                "caption": str(g.get("caption") or ""),
                "data_url": None,
            }
        )
    pending: dict[str, Any] = {"items": items, "dirty": False}

    ui.label(
        "Shown above the input form as visual guides for the parameters — e.g. a "
        f"diagram of what a measurement refers to. Optional; up to {MAX_GUIDE_IMAGES}."
    ).style("color: #888; font-size: 0.85rem;")
    list_box = ui.column().classes("w-full gap-2")

    def _rebuild() -> None:
        list_box.clear()
        with list_box:
            if not items:
                ui.label("No guide images yet.").style("color: #888; font-size: 0.85rem;")
            for idx, it in enumerate(items):
                _render_guide_row(idx, it)

    def _render_guide_row(idx: int, it: dict[str, Any]) -> None:
        with (
            ui.row()
            .classes("w-full items-center gap-2 p-2")
            .style("border: 1px solid var(--matofx-card-border); border-radius: 6px;")
        ):
            thumb = (
                ui.column()
                .classes("items-center justify-center")
                .style("width: 72px; height: 72px; flex: 0 0 72px;")
            )
            with thumb:
                if it.get("data_url"):
                    ui.image(str(it["data_url"])).style(
                        "width: 100%; height: 100%; object-fit: contain;"
                    )
                else:
                    ui.icon("image").style("font-size: 2rem; color: #888;")
            cap = (
                ui.input("Caption", value=str(it.get("caption") or ""))
                .classes("flex-grow")
                .props("dense")
            )

            def _on_caption(_e: Any = None, _it: dict[str, Any] = it, _w: Any = cap) -> None:
                _it["caption"] = _w.value
                pending["dirty"] = True

            cap.on("update:model-value", _on_caption)
            up = ui.button(
                icon="arrow_upward", on_click=lambda _e=None, _i=idx: _move(_i, -1)
            ).props("flat dense")
            if idx == 0:
                up.disable()
            down = ui.button(
                icon="arrow_downward", on_click=lambda _e=None, _i=idx: _move(_i, 1)
            ).props("flat dense")
            if idx == len(items) - 1:
                down.disable()
            ui.button(icon="delete", on_click=lambda _e=None, _i=idx: _remove(_i)).props(
                "flat dense color=negative"
            )

    def _move(i: int, delta: int) -> None:
        j = i + delta
        if 0 <= j < len(items):
            items[i], items[j] = items[j], items[i]
            pending["dirty"] = True
            _rebuild()

    def _remove(i: int) -> None:
        if 0 <= i < len(items):
            items.pop(i)
            pending["dirty"] = True
            _rebuild()

    async def _on_add(e: Any) -> None:
        if len(items) >= MAX_GUIDE_IMAGES:
            ui.notify(f"At most {MAX_GUIDE_IMAGES} guide images.", type="warning")
            return
        try:
            data = await e.file.read()
            name = e.file.name
        except Exception as exc:
            ui.notify(f"Couldn't read the image: {exc}", type="negative")
            return
        b64 = base64.b64encode(data).decode("ascii")
        items.append(
            {
                "existing_filename": None,
                "orig_index": None,
                "upload_bytes": data,
                "name": name,
                "caption": "",
                "data_url": f"data:{_guide_mime(name)};base64,{b64}",
            }
        )
        pending["dirty"] = True
        _rebuild()

    _rebuild()
    (
        ui.upload(on_upload=_on_add, auto_upload=True, multiple=True)
        .props('accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp" flat dense')
        .classes("w-full")
    )
    ui.label("PNG, JPG or WebP. Saved into the tool bundle when you click 'Save changes'.").style(
        "color: #888; font-size: 0.8rem;"
    )

    async def _load_thumbs() -> None:
        client = ApiClient(get_app(), token=current_token())
        for it in items:
            if not it.get("existing_filename") or it.get("data_url"):
                continue
            try:
                result = await client.get_tool_guide_image_bytes(alias, int(it["orig_index"]))
            except Exception:  # noqa: S112 — a missing thumbnail is non-fatal, skip it
                continue
            if not result:
                continue
            raw, mime = result
            b64 = base64.b64encode(raw).decode("ascii")
            it["data_url"] = f"data:{mime};base64,{b64}"
        _rebuild()

    ui.timer(0.05, _load_thumbs, once=True)
    return pending


def _open_replace_bundle_dialog(alias: str, tool: dict[str, Any]) -> None:
    """Modal: upload a fresh static ZIP (+ optional descriptor / icon) and replace.

    Only the static ZIP is required — omit the descriptor JSON to keep the
    tool's current input/output settings and just swap the bundle files.
    The form metadata block is rebuilt from the existing descriptor —
    replace-bundle is a *file* swap, not a metadata edit. If you want
    to change resource limits or approvers at the same time, do that
    in the metadata form first, then replace the bundle. Presets travel
    on the descriptor itself now — author them on the wizard's *Presets*
    tab instead of via a separate JSON upload here.
    """
    descriptor = dict(tool.get("descriptor") or {})
    resources = dict(descriptor.get("resources") or {})
    # Presets used to ride on a separate JSON upload in this dialog;
    # they're now authored on the descriptor wizard's *Presets* tab and
    # travel with the descriptor itself. The replace-bundle path keeps
    # whatever presets the new descriptor JSON declares (or any legacy
    # `<alias>_presets.json` left inside the static ZIP, for tools the
    # operator hasn't migrated to the wizard yet).
    uploads: dict[str, dict[str, Any]] = {
        "descriptor": {"bytes": None, "filename": None},
        "static": {"bytes": None, "filename": None},
        "icon": {"bytes": None, "filename": None},
    }
    with ui.dialog() as dialog, ui.card().style("min-width: 540px; max-height: 80vh;"):
        ui.label(f"Replace bundle: {alias}").style("font-size: 1.2rem; font-weight: 600;")
        ui.label(
            "Replace the on-disk bundle for this tool. Existing job rows that "
            "captured the previous bundle keep resolving against it; new jobs "
            "use the new bundle. Only the ZIP is required — leave the descriptor "
            "empty to keep the current inputs / outputs. Metadata is left as-is — "
            "change it in the main form first if you also want to update limits."
        ).style("color: #888; font-size: 0.85rem;")

        _file_uploader(
            label="Descriptor JSON (optional)",
            accept=".json",
            helper=(
                "Leave empty to keep the current input / output settings. Only "
                "supply one if you want to change the input/output contract."
            ),
            target=uploads["descriptor"],
        )
        _file_uploader(
            label="Static files (ZIP) *",
            accept=".zip",
            helper=(
                "Replacement static-files ZIP. The current bundle directory will "
                "be wiped + rewritten."
            ),
            target=uploads["static"],
        )
        _file_uploader(
            label="Icon (PNG, optional)",
            accept=".png",
            helper="If supplied, replaces <alias>.png in the new bundle.",
            target=uploads["icon"],
        )

        message = ui.label("").style("color: #c0392b;")

        async def _go() -> None:
            message.text = ""
            if uploads["static"]["bytes"] is None:
                message.text = "A replacement static-files ZIP is required."
                return
            metadata: dict[str, Any] = {
                "alias": alias,
                "name": str(tool.get("name") or alias),
                "version": str(tool.get("version") or "1.0"),
                "short_description": tool_descriptions(descriptor)[0],
                "long_description": tool_descriptions(descriptor)[1],
                "main_script": str(descriptor.get("main_script") or "main.py"),
                "teams": list(descriptor.get("teams") or []),
                "downstream_tools": list(descriptor.get("downstream_tools") or []),
                "cpu_cores": int(resources.get("cpu_cores") or 1),
                "ram_mb": int(resources.get("ram_mb") or 512),
                "gpu_memory_mb": int(resources.get("gpu_memory_mb") or 0),
                "licences_required": list(descriptor.get("licences_required") or []),
                "duration_class": str(descriptor.get("duration_class") or "medium"),
                "approvals_policy": str(descriptor.get("approvals_policy") or "none"),
                "approver_usernames": list(descriptor.get("approver_usernames") or []),
                "email_notifications": bool(descriptor.get("email_notifications", False)),
            }
            client = ApiClient(get_app(), token=current_token())
            try:
                await client.replace_tool_bundle(
                    alias,
                    metadata=metadata,
                    descriptor_json_bytes=(
                        bytes(uploads["descriptor"]["bytes"])
                        if uploads["descriptor"]["bytes"] is not None
                        else None
                    ),
                    descriptor_filename=uploads["descriptor"]["filename"],
                    static_zip_bytes=bytes(uploads["static"]["bytes"]),
                    static_zip_filename=str(uploads["static"]["filename"]),
                    icon_bytes=(
                        bytes(uploads["icon"]["bytes"])
                        if uploads["icon"]["bytes"] is not None
                        else None
                    ),
                    icon_filename=uploads["icon"]["filename"],
                    # presets_bytes / presets_filename intentionally omitted —
                    # the wizard's Presets tab is the v2 authoring surface.
                    # ApiClient.replace_tool_bundle defaults both to None.
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Bundle replaced.", type="positive")
            ui.navigate.to(f"/admin/tools/{alias}/edit")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Replace bundle", on_click=_go).props("color=warning")
    dialog.open()


def _wire_world_mutual_exclusion(teams_picker: ui.select) -> None:
    """Same World/specific-team mutual exclusion as on the New Tool page."""
    state: dict[str, list[str]] = {"prev": list(teams_picker.value or [])}

    def _on_change(_e: Any = None) -> None:
        current = list(teams_picker.value or [])
        prev = state["prev"]
        added = [t for t in current if t not in prev]
        if not added or len(current) <= 1:
            state["prev"] = current
            return
        if _WORLD_TEAM_NAME in added:
            new_value = [_WORLD_TEAM_NAME]
        elif _WORLD_TEAM_NAME in current:
            new_value = [t for t in current if t != _WORLD_TEAM_NAME]
        else:
            new_value = current
        if new_value != current:
            teams_picker.value = new_value
        state["prev"] = list(new_value)

    teams_picker.on("update:model-value", _on_change)


_UPLOAD_MAX_FILE_BYTES = 500 * 1024 * 1024
"""Per-file ceiling — matches the New Tool form."""


def _file_uploader(
    *,
    label: str,
    accept: str,
    helper: str,
    target: dict[str, Any],
) -> None:
    """Same uploader pattern as on the New Tool page (kept duplicated for now).

    Status line walks "No file selected." → "Uploading…" → "Selected: …",
    and surfaces rejections / handler exceptions inline so an upload
    never silently disappears.
    """
    ui.label(label).style("font-weight: 500; margin-top: 0.5rem;")
    status_label = ui.label("No file selected.").style("color: #888; font-size: 0.85rem;")

    def _set_status(text: str, *, colour: str) -> None:
        status_label.text = text
        status_label.style(f"color: {colour}; font-size: 0.85rem;")

    def _on_begin_upload(_e: Any) -> None:
        _set_status("Uploading…", colour="#888")

    async def _on_upload(e: Any) -> None:
        # NiceGUI 3.x: e.file is a FileUpload; read() is async.
        try:
            data = await e.file.read()
            name = e.file.name
        except Exception as exc:
            _set_status(f"Upload failed: {exc}", colour="#c0392b")
            return
        target["bytes"] = data
        target["filename"] = name
        _set_status(f"Selected: {name} ({len(data):,} bytes)", colour="#43a047")
        callback = target.get("on_change")
        if callable(callback):
            try:
                callback()
            except Exception as exc:
                _set_status(
                    f"Selected: {name} ({len(data):,} bytes) — validation error: {exc}",
                    colour="#c0392b",
                )

    def _on_rejected(_e: Any) -> None:
        _set_status(
            "That file was rejected — check the type/size against the hint below.",
            colour="#c0392b",
        )

    (
        ui.upload(
            on_upload=_on_upload,
            on_begin_upload=_on_begin_upload,
            on_rejected=_on_rejected,
            auto_upload=True,
            max_files=1,
            max_file_size=_UPLOAD_MAX_FILE_BYTES,
        )
        .props(f'accept="{accept}" flat dense')
        .classes("w-full")
    )
    ui.label(helper).style("color: #888; font-size: 0.8rem;")


# --- Session C: in-browser file editor ----------------------------------


_LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "Python",
    ".pyi": "Python",
    ".json": "JSON",
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".toml": "TOML",
    ".ini": "Properties",
    ".cfg": "Properties",
    ".sql": "SQL",
    ".sh": "Shell",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".html": "HTML",
    ".css": "CSS",
    ".xml": "XML",
}


def _language_for(rel_path: str) -> str:
    """Map a relative path to a CodeMirror language string. Falls back to plain text."""
    suffix = ("." + rel_path.rsplit(".", 1)[-1]).lower() if "." in rel_path else ""
    return _LANGUAGE_BY_EXTENSION.get(suffix, "")


async def _render_files_tab(
    client: ApiClient,
    *,
    alias: str,
    tool: dict[str, Any],
) -> None:
    """Files tab: pick a bundle file on the left, edit it on the right.

    Save calls :meth:`ApiClient.save_tool_file` which goes through
    :meth:`ToolService.apply_file_edit` — clones the bundle to a fresh
    per-version directory, applies the edit, minor-bumps the tool, and
    writes a snapshot to the change log. After a successful save the
    page reloads so the header version + bundle path are accurate.
    """
    try:
        listing = await client.list_tool_files(alias)
    except ApiError as exc:
        ui.label(f"We couldn't list the bundle's files: {exc.detail}").style("color: #c0392b;")
        return

    files: list[dict[str, Any]] = list(listing.get("files") or [])
    text_files = [f for f in files if f.get("is_text")]
    if not text_files:
        ui.label(
            "There are no text files in this tool's bundle. Use Replace bundle "
            "to upload a fresh ZIP."
        ).style("color: #888;")
        return

    selected: dict[str, Any] = {"path": None, "original": ""}

    with ui.row().classes("w-full no-wrap gap-4 items-start"):
        with ui.column().classes("gap-2").style("flex: 0 0 280px;"):
            ui.label("Bundle files").style("font-weight: 600;")
            ui.label(
                "Pick a file to edit. Saving any change minor-bumps the tool "
                "(e.g. 0.1.0 → 0.2.0) and keeps the previous version on disk "
                "so old jobs still resolve to it."
            ).style("color: #888; font-size: 0.8rem;")
            file_list_container = ui.column().classes("w-full gap-1")
        with ui.column().classes("gap-2").style("flex: 1 1 0; min-width: 0;"):
            ui.label("Editor").style("font-weight: 600;")
            current_label = ui.label("No file selected.").style("color: #888; font-size: 0.85rem;")
            # Editor: vscodeDark matches the app's _PAGE_BG (#1e1e1e),
            # so the editor doesn't visually pop out as a foreign panel.
            # min-height 600px gives ~30 lines of code visible without
            # scrolling; resize: vertical lets the operator drag it
            # taller for files that don't fit comfortably.
            editor = (
                ui.codemirror(value="", line_wrapping=False, theme="vscodeDark")
                .classes("w-full")
                .style(
                    "min-height: 600px; resize: vertical; "
                    "overflow: auto; border: 1px solid rgba(255,255,255,0.35); "
                    "border-radius: 4px;"
                )
            )
            editor.disable()
            bump_hint = ui.label("Pick a file to start editing.").style(
                "color: #b58900; font-size: 0.85rem; min-height: 1.1em;"
            )
            # Inline syntax-error panel. Hidden until a save fails with
            # a 422 from SyntaxValidationError; rendered as a list of
            # "line X, col Y: message" rows so the operator can fix
            # without leaving the editor or losing their work.
            with ui.column().classes("w-full gap-1") as syntax_errors_panel:
                pass
            syntax_errors_panel.style(
                "border: 1px solid rgba(255,107,107,0.55); "
                "background-color: rgba(255,107,107,0.10); "
                "padding: 0.5rem 0.75rem; border-radius: 4px;"
            )
            syntax_errors_panel.set_visibility(False)
            with ui.row().classes("gap-2"):
                save_button = ui.button("Save").props("color=primary unelevated")
                save_button.disable()
                reset_button = ui.button("Reset").props("flat")
                reset_button.disable()

    def _refresh_bump_hint() -> None:
        if selected["path"] is None:
            bump_hint.text = "Pick a file to start editing."
            return
        if str(editor.value) == selected["original"]:
            bump_hint.text = "No edits — saving has no effect."
        else:
            current_version = str(tool.get("version") or "0.1.0")
            try:
                from matofx.tools.versioning import bump_minor  # noqa: PLC0415

                projected = bump_minor(current_version)
                bump_hint.text = f"Saving will bump version to {projected} (minor)."
            except ValueError:
                bump_hint.text = "Saving will minor-bump the tool."

    editor.on_value_change(lambda _e: _refresh_bump_hint())

    def _clear_syntax_errors() -> None:
        syntax_errors_panel.clear()
        syntax_errors_panel.set_visibility(False)

    def _show_syntax_errors(rel_path: str, errors: list[dict[str, Any]]) -> None:
        syntax_errors_panel.clear()
        with syntax_errors_panel:
            ui.label(f"Couldn't save {rel_path} — syntax errors:").style(
                "color: #ff6b6b; font-weight: 600;"
            )
            for issue in errors:
                line = issue.get("line", "?")
                column = issue.get("column", "?")
                message = issue.get("message", "")
                prefix = f"line {line}, col {column}" if column and column != 0 else f"line {line}"
                ui.label(f"• {prefix}: {message}").style(
                    "color: #ffb4b4; font-size: 0.9rem; font-family: monospace;"
                )
        syntax_errors_panel.set_visibility(True)

    async def _open_file(rel_path: str) -> None:
        try:
            data = await client.read_tool_file(alias, rel_path)
        except ApiError as exc:
            ui.notify(f"Couldn't open {rel_path}: {exc.detail}", type="negative")
            return
        contents = str(data.get("contents") or "")
        selected["path"] = rel_path
        selected["original"] = contents
        current_label.text = f"Editing: {rel_path}"
        editor.set_value(contents)
        editor.props(f'language="{_language_for(rel_path)}"')
        editor.enable()
        save_button.enable()
        reset_button.enable()
        _clear_syntax_errors()
        _refresh_bump_hint()

    async def _save() -> None:
        path = selected["path"]
        if path is None:
            return
        if str(editor.value) == selected["original"]:
            ui.notify("No edits to save.", type="info")
            return
        _clear_syntax_errors()
        try:
            await client.save_tool_file(alias, path, contents=str(editor.value))
        except ApiError as exc:
            # 422 with a structured ``errors`` list = syntax validation
            # failed. Render the issues inline below the editor so the
            # operator can fix-and-retry without losing their work.
            errors = exc.payload.get("errors") if isinstance(exc.payload, dict) else None
            if exc.status_code == 422 and isinstance(errors, list) and errors:
                _show_syntax_errors(path, errors)
                return
            ui.notify(f"Save failed: {exc.detail}", type="negative")
            return
        ui.notify(f"Saved {path} — tool version bumped.", type="positive")
        # Full page reload so the header + version + bundle path reflect
        # the new state and the file list points at the new bundle dir.
        ui.navigate.to(f"/admin/tools/{alias}/edit")

    def _reset() -> None:
        if selected["path"] is None:
            return
        editor.set_value(selected["original"])
        _clear_syntax_errors()
        _refresh_bump_hint()

    save_button.on_click(_save)
    reset_button.on_click(_reset)

    with file_list_container:
        for entry in text_files:
            rel = str(entry["relative_path"])
            ui.button(
                rel,
                on_click=lambda _e=None, _r=rel: _open_file(_r),
            ).props("flat align=left dense").classes("w-full")
        # Binary files are listed for visibility but not clickable.
        binary_files = [f for f in files if not f.get("is_text")]
        if binary_files:
            ui.label("Binary files (not editable):").style(
                "color: #888; font-size: 0.8rem; margin-top: 0.5rem;"
            )
            for entry in binary_files:
                ui.label(str(entry["relative_path"])).style("color: #aaa; font-size: 0.8rem;")


# --- Session C C3: inputs / outputs wizard tab -------------------------


async def _render_wizard_tab(
    client: ApiClient,
    *,
    alias: str,
    tool: dict[str, Any],
    existing_tools: list[dict[str, Any]],
    material_classes_with_props: dict[str, list[dict[str, str]]] | None = None,
) -> None:
    """Pre-populated wizard for editing inputs / outputs / groups / layout.

    Save calls :meth:`ApiClient.update_tool_descriptor` with a body
    that carries the descriptor-body fields the wizard owns. The
    server's ``update_descriptor`` merges those over the existing
    descriptor and patch-bumps the version. Without this tab there's
    no path to edit ``inputs[]`` / ``outputs[]`` post-install since
    the file editor explicitly excludes ``descriptor.json``.
    """
    descriptor = dict(tool.get("descriptor") or {})
    state = state_from_descriptor(descriptor)

    # Upstream wiring: candidate tools (those that declare this tool
    # downstream) + their outputs, prefetched alongside any upstream this
    # descriptor already wires to so their output dropdowns aren't empty.
    wiring = await build_upstream_wiring(
        existing_tools=existing_tools,
        current_alias=alias,
        fetch_detail=client.get_admin_tool,
        extra_prefetch_aliases=wired_upstream_aliases(state),
    )

    ui.label(
        "Edit your tool's input form, output panel, retain/delete file "
        "globs, and execution backend across the six tabs below. Saving "
        "patch-bumps the tool (e.g. 1.0.0 → 1.0.1) and writes a snapshot "
        "to the change log."
    ).style("color: #888; font-size: 0.85rem;")
    render_descriptor_wizard(
        state,
        wiring=wiring,
        material_classes_with_props=material_classes_with_props,
    )

    save_message = ui.label("").style("color: #c0392b;")

    async def _save() -> None:
        wizard_payload = build_descriptor_payload(state)
        # Body shape mirrors AdminToolDescriptorUpdate — wizard fields
        # are tacked on as optional descriptor-body fields. Metadata
        # fields are pulled from the loaded tool so they round-trip
        # unchanged (the metadata tab edits them via its own save).
        body: dict[str, Any] = {
            "name": str(tool.get("name") or ""),
            "version": str(tool.get("version") or "0.1.0"),
            "short_description": tool_descriptions(descriptor)[0],
            "long_description": tool_descriptions(descriptor)[1],
            "teams": list(descriptor.get("teams") or []),
            "downstream_tools": list(descriptor.get("downstream_tools") or []),
            "cpu_cores": int((descriptor.get("resources") or {}).get("cpu_cores", 1)),
            "ram_mb": int((descriptor.get("resources") or {}).get("ram_mb", 512)),
            "gpu_memory_mb": int((descriptor.get("resources") or {}).get("gpu_memory_mb", 0)),
            "licences_required": list(descriptor.get("licences_required") or []),
            "duration_class": str(descriptor.get("duration_class") or "medium"),
            "approvals_policy": str(descriptor.get("approvals_policy") or "none"),
            "approver_usernames": list(descriptor.get("approver_usernames") or []),
            "email_notifications": bool(descriptor.get("email_notifications", False)),
            **wizard_payload,
        }
        try:
            await client.update_tool_descriptor(alias, body=body)
        except ApiError as exc:
            save_message.text = exc.detail
            return
        # Additive handshake: make sure every tool wired as an upstream
        # source lists this tool in its downstream_tools (so it keeps
        # showing as a source next time). Best-effort, never blocks save.
        patched = await reconcile_downstream_registrations(
            state=state,
            current_alias=alias,
            register=lambda src, dst: client.register_downstream_tool(
                source_alias=src, downstream_alias=dst
            ),
        )
        if patched:
            names = ", ".join(wiring.all_tools.get(a, a) for a in patched)
            ui.notify(
                f"Inputs / outputs saved — tool patch-bumped. Also updated: {names}.",
                type="positive",
            )
        else:
            ui.notify("Inputs / outputs saved — tool patch-bumped.", type="positive")
        ui.navigate.to(f"/admin/tools/{alias}/edit")

    with ui.row().classes("w-full justify-end gap-2"):
        ui.button("Save", on_click=_save).props("color=primary unelevated")


# ---------------------------- V&V tab ---------------------------------


_VV_BADGE_LABEL: dict[str, str] = {
    "verified_validated": "Verified & validated",
    "verified_only": "Verified",
    "pending": "V&V pending",
    "none": "No V&V",
    "retired": "V&V retired",
}
_VV_BADGE_COLOR: dict[str, str] = {
    "verified_validated": "positive",
    "verified_only": "info",
    "pending": "warning",
    "none": "grey",
    "retired": "grey",
}
_VV_STATE_LABEL: dict[str, str] = {
    "draft": "Draft",
    "pending": "Pending review",
    "approved": "Approved",
    "rejected": "Rejected",
    "retired": "Retired",
}
_VV_STATE_COLOR: dict[str, str] = {
    "draft": "grey",
    "pending": "warning",
    "approved": "positive",
    "rejected": "negative",
    "retired": "grey",
}
_VV_TOOL_KIND_LABEL: dict[str, str] = {
    "verification": "Verification",
    "validation": "Validation",
}


async def _render_vv_tab(client: ApiClient, *, tool: dict[str, Any]) -> None:
    """Per-tool V&V tab: inheritance-aware badge + every record on this version.

    ADR-0011 §8 — surface per-tool V&V so a curator working on a tool
    can see at a glance which versions in its cohort carry approved
    records (the badge) and the raw list of records on the current
    released version (the inheritance-expanded badge fills the
    "what else contributes?" question via tooltip).
    """
    raw = tool.get("tool_version_id")
    tool_version_id = int(raw) if raw is not None else None
    if tool_version_id is None:
        ui.label(
            "This tool has no ``ToolVersion`` snapshot row yet — V&V records "
            "key off the snapshot, so there is nothing to show until the first "
            "version bump."
        ).style("color: #888;")
        return

    tool_label = f"{tool.get('name') or tool.get('alias') or ''} v{tool.get('version') or '?'}"
    body = ui.column().classes("w-full gap-3")

    async def _reload() -> None:
        await _render_vv_tab_body(
            client, body, tool_version_id=tool_version_id, tool_label=tool_label
        )

    await _render_vv_tab_body(client, body, tool_version_id=tool_version_id, tool_label=tool_label)
    _ = _reload  # closure kept available for future per-action refreshes


async def _render_vv_tab_body(
    client: ApiClient, body: ui.column, *, tool_version_id: int, tool_label: str
) -> None:
    body.clear()
    try:
        me = await client.me()
    except ApiError:
        me = {}
    perms = me.get("permissions") or []
    is_curator = "vv.curate" in perms
    is_author = is_curator or "vv.create" in perms

    with body:
        await _render_vv_badge_panel(client, tool_version_id)
        await _render_vv_records_panel(
            client,
            tool_version_id=tool_version_id,
            tool_label=tool_label,
            is_curator=is_curator,
            is_author=is_author,
        )


async def _render_vv_badge_panel(client: ApiClient, tool_version_id: int) -> None:
    try:
        badge = await client.vv_tool_status(tool_version_id)
    except ApiError as exc:
        ui.label(f"Couldn't load V&V status: {exc.detail}").style(
            "color: var(--matofx-error-text);"
        )
        return
    kind = str(badge.get("kind") or "none")
    label = _VV_BADGE_LABEL.get(kind, kind.title())
    color = _VV_BADGE_COLOR.get(kind, "grey")
    inherited = bool(badge.get("inherited"))
    contributing = list(badge.get("contributing_version_ids") or [])
    cohort_hash = str(badge.get("cohort_bundle_hash") or "")
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-3"):
            ui.chip(label, icon="verified").props(f"color={color} text-color=white")
            if inherited and contributing:
                ui.label(
                    f"Inherited from cohort versions: "
                    f"{', '.join(f'#{v}' for v in contributing[:6])}"
                ).style("color: #888; font-size: 0.85rem;")
            ui.space()
            if cohort_hash:
                ui.label(f"cohort hash: {cohort_hash[:8]}…").style(
                    "color: #aaa; font-size: 0.78rem; font-family: monospace;"
                )
        ui.label(
            "The badge reads across every tool version sharing the same bundle "
            "hash. A record on a sibling version contributes "
            "to this one's badge."
        ).style("color: #888; font-size: 0.8rem;")


async def _render_vv_records_panel(
    client: ApiClient,
    *,
    tool_version_id: int,
    tool_label: str,
    is_curator: bool,
    is_author: bool,
) -> None:
    from matofx.ui.pages.vv_records import open_vv_create_dialog  # noqa: PLC0415

    try:
        records = await client.vv_list_tool_records(
            tool_version_id=tool_version_id, include_retired=False
        )
    except ApiError as exc:
        ui.label(f"Couldn't load records: {exc.detail}").style("color: var(--matofx-error-text);")
        return

    with ui.row().classes("w-full items-center justify-between"):
        ui.label(f"Records on this version ({len(records)})").style("font-weight: 600;")
        with ui.row().classes("items-center gap-2"):
            if is_author:
                ui.button(
                    "Add V&V record",
                    icon="add",
                    on_click=lambda: open_vv_create_dialog(
                        client,
                        prefill_tool_version_id=tool_version_id,
                        prefill_tool_label=tool_label,
                    ),
                ).props("flat dense color=primary")
            if is_curator:
                ui.button(
                    "Open V&V workspace",
                    icon="open_in_new",
                    on_click=lambda: ui.navigate.to("/vv-records"),
                ).props("flat dense")

    if not records:
        ui.label(
            "No V&V records on this version yet — use “Add V&V record” to author one "
            "against this version."
        ).style("color: #888; font-size: 0.85rem;")
        return

    for r in records:
        _render_vv_record_row(r)


def _render_vv_record_row(r: dict[str, Any]) -> None:
    rid = int(r.get("id") or 0)
    kind_label = _VV_TOOL_KIND_LABEL.get(str(r.get("kind")), str(r.get("kind")))
    state = str(r.get("state") or "draft")
    state_label = _VV_STATE_LABEL.get(state, state.title())
    state_color = _VV_STATE_COLOR.get(state, "grey")
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center gap-3"):
            ui.label(kind_label).style("font-weight: 600;")
            ui.chip(state_label).props(f"color={state_color} text-color=white dense")
            ui.space()
            ui.button(
                "Open",
                icon="open_in_new",
                on_click=lambda _e=None, _rid=rid: ui.navigate.to(f"/vv-records/tool/{_rid}"),
            ).props("flat dense")
        notes = str(r.get("notes") or "").strip()
        if notes:
            ui.label(notes[:200] + ("…" if len(notes) > 200 else "")).style(
                "color: #888; font-size: 0.85rem;"
            )
        standards = list(r.get("standards_cited") or [])
        if standards:
            with ui.row().classes("items-center gap-1"):
                for s in standards[:6]:
                    ui.chip(str(s)).props("dense outline")
