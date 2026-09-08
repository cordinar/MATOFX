"""Admin: New tool (``/ui/admin/tools/new``).

Single-page form mirroring the legacy "New Tool" tab. Two-column
layout:

* **Left column** — identity (alias, name, description, main script,
  version, icon), category / team membership, downstream tools, and
  the bundle file uploads (descriptor JSON, static ZIP, optional icon).
  Tool-author presets are now authored on the descriptor wizard's
  *Presets* tab and travel with the descriptor itself.
* **Right column** — resources (CPU / RAM / GPU), licences, approvals
  policy + approvers, and notifications.

The form is the operator-friendly path to the same install that
``matofx admin install-tool`` exposes from the CLI: form values become
the descriptor's metadata, the uploaded ``<alias>_descriptors.json``
contributes inputs / outputs / groups / layout, and the static ZIP
becomes the bundle's runtime files. Server stages
``<bundles_dir>/<alias>/`` and calls
:meth:`matofx.tools.service.ToolService.install`.
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from nicegui import ui

from matofx.ui._features import materials_database_enabled
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.pages._descriptor_wizard import (
    build_descriptor_payload,
    build_upstream_wiring,
    empty_state,
    reconcile_downstream_registrations,
    render_descriptor_wizard,
    state_from_descriptor,
)
from matofx.ui.state import current_token

_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Approvals-policy labels — copy is operator-facing.
_APPROVALS_OPTIONS: dict[str, str] = {
    "none": "None — jobs run without approval",
    "pre": "Pre-run approval required",
    "post": "Post-run approval required",
    "both": "Pre + post-run approval required",
}

_WORLD_TEAM_NAME = "World"
"""Universal-visibility team — see migration 0014 + ``matofx.tools.service``.

Always exposed in the team picker even when the migration hasn't seeded
the row yet (e.g. on a long-running install upgraded across versions),
so the operator can always pick "everyone".
"""

# Duration class is part of the descriptor schema but isn't surfaced
# in the form — Phase 1 has no scheduler that consumes it (jobs run in
# submission order; admins skip the queue manually for testing).
_DEFAULT_DURATION_CLASS = "medium"


@ui.page("/admin/tools/new")
async def admin_tool_new_page(draft: int | None = None) -> None:
    """Render the New Tool form.

    ``?draft=<id>`` resumes a previously saved draft: the form + wizard
    are pre-populated from it, and "Save draft" updates that same row.
    """
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())
    try:
        teams = await client.list_teams()
        users = await client.list_users()
        existing_tools = await client.list_admin_tools()
    except ApiError as exc:
        with ui.column().classes("w-full max-w-5xl mx-auto p-6"):
            ui.label(f"We couldn't load the form: {exc.detail}").style("color: #c0392b;")
        return

    # Resume path: load the draft (and its payload) up front so the form
    # builds pre-populated. A draft that's vanished (deleted in another
    # tab) degrades to a fresh form with a heads-up rather than erroring.
    draft_id: int | None = None
    initial: dict[str, Any] | None = None
    if draft is not None:
        try:
            loaded = await client.get_tool_draft(int(draft))
            draft_id = int(loaded["id"])
            initial = dict(loaded.get("payload") or {})
        except ApiError:
            ui.notify("That draft couldn't be loaded — starting a fresh tool.", type="warning")

    team_names = {str(t["name"]) for t in teams}
    # Always offer World as a virtual team — the visibility filter
    # treats it as a wildcard, so it works even when the seed migration
    # hasn't run yet on this install.
    team_names.add(_WORLD_TEAM_NAME)
    team_options = sorted(team_names)
    approver_options = {
        str(u["username"]): f"{u.get('full_name') or u['username']} ({u['username']})"
        for u in users
        if u.get("is_approver") and u.get("is_active")
    }
    # Downstream picker: keys are aliases (the value submitted),
    # labels are tool names (what the operator reads). Sorted by name.
    downstream_options = {
        str(t["alias"]): str(t["name"])
        for t in sorted(existing_tools, key=lambda t: str(t.get("name") or ""))
        if not t.get("archived_at")
    }
    taken_aliases = {str(t["alias"]).lower() for t in existing_tools}
    # Pre-fetch the live materials catalogue (when the feature is on) so
    # a `material` input renders a class select + a class-scoped
    # multiselect of required properties instead of free-text inputs.
    # An empty dict means "feature on but couldn't load" — the wizard
    # falls back to text inputs in that case (graceful degradation).
    # ``{class_name: [{"slug": str, "prop_name": str}, ...]}`` — the
    # wizard's multiselect shows ``prop_name`` (display label) but the
    # value it stores in ``required_properties`` is the immutable slug.
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

    with ui.column().classes("w-full mx-auto p-6 gap-4").style("max-width: 1700px;"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.button(
                icon="arrow_back",
                on_click=lambda: ui.navigate.to("/admin/tools"),
            ).props("flat round dense")
            ui.label("New tool" if draft_id is None else "New tool (resumed draft)").style(
                "font-size: 1.5rem; font-weight: 600;"
            )
        ui.label(
            "Fill in the metadata below, upload the descriptor JSON + static-files ZIP, "
            "and optionally an icon. Tool-author presets are authored on the "
            "descriptor wizard's Presets tab. Required fields are marked *. "
            "Use Save draft to keep partial work — the uploaded files aren't "
            "part of the draft, so re-attach them when you come back."
        ).style("color: #b0b3b8; font-size: 0.85rem;")

        await _render_form(
            team_options,
            approver_options,
            downstream_options,
            taken_aliases,
            client=client,
            existing_tools=existing_tools,
            material_classes_with_props=material_classes_with_props,
            initial=initial,
            draft_id=draft_id,
        )


async def _render_form(
    team_options: list[str],
    approver_options: dict[str, str],
    downstream_options: dict[str, str],
    taken_aliases: set[str],
    *,
    client: ApiClient,
    existing_tools: list[dict[str, Any]],
    material_classes_with_props: dict[str, list[dict[str, str]]] | None = None,
    initial: dict[str, Any] | None = None,
    draft_id: int | None = None,
) -> None:
    """The two-column form body.

    When ``initial`` is supplied (a resumed draft's payload) the fields
    and wizard tabs are pre-populated from it; ``draft_id`` (when set)
    makes "Save draft" overwrite that draft in place rather than create
    a new one.
    """
    # File-upload buffers — populated by the on_upload handlers below.
    # Presets used to ride on a separate JSON upload here; tool-author
    # presets are now authored on the descriptor wizard's *Presets* tab
    # and travel with the descriptor itself (see ADR-free roadmap row
    # "Tool-preset save/recall" / `_descriptor_wizard._render_presets_tab`).
    uploads: dict[str, dict[str, Any]] = {
        "descriptor": {"bytes": None, "filename": None},
        "static": {"bytes": None, "filename": None},
        "icon": {"bytes": None, "filename": None},
    }

    # Wizard authoring state — populated when the operator picks the
    # "Build with wizard" mode toggle. Submit assembles a descriptor
    # JSON from {metadata + wizard payload} and sends it as the
    # descriptor_json field, so the server contract stays unchanged.
    # On a resumed draft, rebuild the wizard state from the saved
    # descriptor-shaped blob (the exact round-trip the Edit page uses).
    wizard_seed = initial.get("wizard") if initial else None
    wizard_state: dict[str, Any] = (
        state_from_descriptor(dict(wizard_seed)) if isinstance(wizard_seed, dict) else empty_state()
    )
    _initial_mode = str(initial.get("mode") or "wizard") if initial else "wizard"
    mode: dict[str, str] = {
        "value": _initial_mode if _initial_mode in ("wizard", "upload") else "wizard"
    }

    # Top-of-form mode toggle. The wizard is the recommended path; the
    # JSON-upload path is preserved for power users with a hand-authored
    # descriptor file (pasted from the legacy MATOFX install, etc.).
    with ui.card().classes("w-full"):
        ui.label("How will you describe inputs and outputs?").style("font-weight: 600;")
        mode_radio = (
            ui.radio(
                {
                    "wizard": "Build with wizard (recommended) — authors inputs / outputs / "
                    "groups / layout / advanced settings via tabs.",
                    "upload": "Upload descriptors.json — power users with a hand-authored "
                    "descriptor file.",
                },
                value=mode["value"],
                on_change=lambda e: mode.__setitem__("value", str(e.value)),
            )
            .classes("w-full")
            .props("dense")
        )
        _ = mode_radio  # value already seeded from the draft via mode["value"]

    with ui.row().classes("w-full no-wrap gap-4 items-start"):
        with ui.column().classes("gap-3").style("flex: 1 1 0; min-width: 0;"):
            with ui.card().classes("w-full"):
                ui.label("Identity").style("font-weight: 600;")
                alias = (
                    ui.input(
                        "Alias *",
                        placeholder=("Internal ID, fixed forever — e.g. material_properties"),
                    )
                    .classes("w-full")
                    .props("dense")
                )
                ui.label(
                    "The system's stable identifier for this tool — lowercase letters, "
                    "digits, and underscores only. It can't be changed later and becomes "
                    "the bundle directory name on disk. (The Name, below, is the friendly "
                    "label users actually see.)"
                ).style("color: #b58900; font-size: 0.8rem;")
                alias_hint = ui.label("").style(
                    "color: #c0392b; font-size: 0.8rem; min-height: 1.1em;"
                )
                name = (
                    ui.input(
                        "Name *",
                        placeholder=(
                            "Display name users see — e.g. Material properties calculator"
                        ),
                    )
                    .classes("w-full")
                    .props("dense")
                )
                ui.label(
                    "The human-friendly title shown in menus, on Quick Access cards, and "
                    "in reports. Free text — you can change it later, and it needn't match "
                    "the alias."
                ).style("color: #888; font-size: 0.8rem;")
                short_description = (
                    ui.textarea(
                        "Short description *",
                        placeholder="One-line summary shown on the home-page Quick Access card.",
                    )
                    .classes("w-full")
                    .props("dense outlined maxlength=200 counter")
                )
                ui.label(
                    "Mandatory, max 200 characters. This is the only description "
                    "shown on the Quick Access card, so keep it tight."
                ).style("color: #888; font-size: 0.8rem;")
                long_description = (
                    ui.textarea(
                        "Long description",
                        placeholder=(
                            "Optional fuller description shown above the input form on "
                            "the run page and in the card's info dialog."
                        ),
                    )
                    .classes("w-full")
                    .props("dense outlined maxlength=2048 counter")
                )
                main_script = (
                    ui.input("Main script *", placeholder="<alias>.py")
                    .classes("w-full")
                    .props("dense")
                )
                ui.label("Path inside the static-files ZIP to the tool's entry point.").style(
                    "color: #888; font-size: 0.8rem;"
                )
                ui.label(
                    "New tools start at version 0.1.0 — server-controlled "
                    "from here on. Edits bump the version automatically: "
                    "metadata changes → patch (0.1.1), in-UI file edits → "
                    "minor (0.2.0), bundle replacement → major (1.0.0). "
                    "First promotion to Released anchors the version at 1.0.0."
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
                # New tools default to ["World"] — the universal-visibility
                # marker — so an admin who picks no specific team still
                # gets a sensible visibility. Matches the legacy default.
                teams_picker.value = [_WORLD_TEAM_NAME]
                _wire_world_mutual_exclusion(teams_picker)
                ui.label(
                    "Pick 'World' (the default) to make this tool visible to "
                    "everyone. Pick specific teams to restrict tool access to "
                    "Beta Testers + Regular Users members of those teams. "
                    "Admins and Super Users always see every tool."
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
                downstream_picker.value = []
                ui.label("Tools whose inputs may be populated from this tool's outputs.").style(
                    "color: #888; font-size: 0.8rem;"
                )

        with ui.column().classes("gap-3").style("flex: 1 1 0; min-width: 0;"):
            with ui.card().classes("w-full"):
                ui.label("Resources").style("font-weight: 600;")
                with ui.row().classes("w-full gap-2"):
                    cpu_cores = (
                        ui.number("CPU cores *", value=1, min=1, max=128, step=1)
                        .classes("flex-1")
                        .props("dense")
                    )
                    ram_mb = (
                        ui.number("RAM (MB) *", value=512, min=32, max=1024 * 1024, step=64)
                        .classes("flex-1")
                        .props("dense")
                    )
                gpu_memory_mb = (
                    ui.number(
                        "GPU memory (MB, leave 0 for none)",
                        value=0,
                        min=0,
                        max=512 * 1024,
                        step=64,
                    )
                    .classes("w-full")
                    .props("dense")
                )

            with ui.card().classes("w-full"):
                ui.label("Licences").style("font-weight: 600;")
                licences = (
                    ui.input(
                        "Licence names (comma-separated)", placeholder="ANSYS-Mechanical, Abaqus"
                    )
                    .classes("w-full")
                    .props("dense")
                )
                ui.label(
                    "Each licence is reserved with count = 1. Use the descriptor JSON for "
                    "richer counts; the form's path is the simple case."
                ).style("color: #888; font-size: 0.8rem;")

            with ui.card().classes("w-full"):
                ui.label("Approvals").style("font-weight: 600;")
                approvals_policy = (
                    ui.select(
                        _APPROVALS_OPTIONS,
                        label="Approval policy",
                        value="none",
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
                approvers_picker.value = []
                approvers_picker.disable()  # default policy is none → picker inactive
                ui.label(
                    "Only users flagged as approvers (Edit User → 'Eligible to be a "
                    "tool approver') appear here. The picker activates when the "
                    "policy is anything other than None."
                ).style("color: #888; font-size: 0.8rem;")

                def _on_policy_change(_e: Any = None) -> None:
                    """Activate/clear the approvers picker based on the policy."""
                    if str(approvals_policy.value) == "none":
                        approvers_picker.value = []
                        approvers_picker.disable()
                    else:
                        approvers_picker.enable()

                approvals_policy.on("update:model-value", _on_policy_change)

            with ui.card().classes("w-full"):
                ui.label("Notifications").style("font-weight: 600;")
                # Off by default: a brand-new tool shouldn't start
                # mailing its submitters before anyone has decided it
                # should. Tick it here, or later in the tool editor.
                email_notifications = ui.checkbox(
                    "Send completion / failure emails for this tool",
                    value=False,
                )
                ui.label(
                    "Off by default. When on, the tool's submitter is emailed as "
                    "their job starts, completes or fails — subject to the site-wide "
                    "job-email setting and each user's own preference."
                ).style("color: #888; font-size: 0.85rem;")

            with ui.card().classes("w-full"):
                ui.label("Parametric studies - run cap").style("font-weight: 600;")
                ui.label(
                    "Maximum number of runs a single parametric study of this "
                    "tool may generate. Leave empty to use the install-wide "
                    "default (currently 500). Set higher for tools that complete "
                    "quickly, or lower for expensive ones."
                ).style("color: #888; font-size: 0.85rem;")
                run_cap_in = (
                    ui.number(
                        "Run cap (leave empty for default)",
                        value=None,
                        min=1,
                    )
                    .classes("w-full")
                    .props("dense")
                )

        with ui.column().classes("gap-3").style("flex: 1 1 0; min-width: 0;"):
            with ui.card().classes("w-full"):
                ui.label("Bundle files").style("font-weight: 600;")
                # Descriptor JSON upload is only meaningful in upload
                # mode; the wizard generates the equivalent payload at
                # save time.
                with ui.column().classes("w-full") as descriptor_upload_box:
                    _file_uploader(
                        label="Descriptor JSON *",
                        accept=".json",
                        helper=(
                            "Upload <alias>_descriptors.json — the inputs / outputs "
                            "/ layout schema."
                        ),
                        target=uploads["descriptor"],
                    )
                descriptor_upload_box.bind_visibility_from(mode, "value", lambda v: v == "upload")
                _file_uploader(
                    label="Static files (ZIP) *",
                    accept=".zip",
                    helper="Upload the bundle's static files (scripts, data, configs) as a ZIP.",
                    target=uploads["static"],
                )
                _file_uploader(
                    label="Icon (PNG, optional)",
                    accept=".png",
                    helper="100x100 PNG. Saved as <alias>.png in the bundle.",
                    target=uploads["icon"],
                )

    # Descriptor wizard — visible only in wizard mode. Mutates
    # ``wizard_state`` in place; on save the page assembles a
    # descriptor JSON from {form metadata + wizard payload} and
    # submits it as the descriptor_json field of the create_tool API.
    with ui.card().classes("w-full") as wizard_card:
        ui.label("Tool inputs and outputs").style("font-weight: 600;")
        ui.label(
            "Author the input form, output panel, retain/delete file globs, "
            "and execution backend across the six tabs below. The Inputs "
            "tab pulls its group dropdown from the Input Groups tab; the "
            "Outputs tab pulls from Output Groups. Save assembles a complete "
            "descriptor at the bottom of the page."
        ).style("color: #888; font-size: 0.85rem;")
        # Upstream wiring. A brand-new tool has no alias yet, so no
        # existing tool declares it downstream — the candidate list starts
        # empty and the author uses "Make another tool a source" to wire
        # one in. Those picks are reconciled into each source tool's
        # downstream_tools after the tool is created (see _save).
        wiring = await build_upstream_wiring(
            existing_tools=existing_tools,
            current_alias="",
            fetch_detail=client.get_admin_tool,
        )
        render_descriptor_wizard(
            wizard_state,
            wiring=wiring,
            material_classes_with_props=material_classes_with_props,
        )
    wizard_card.bind_visibility_from(mode, "value", lambda v: v == "wizard")

    # Resume: pour the saved metadata into the just-built widgets. The
    # wizard tabs were already seeded from ``wizard_state`` at the top;
    # this only covers the left/right column form fields. Uploads are
    # never part of a draft, so they stay empty for re-attachment.
    if initial:
        alias.value = str(initial.get("alias") or "")
        name.value = str(initial.get("name") or "")
        short_description.value = str(initial.get("short_description") or "")
        long_description.value = str(initial.get("long_description") or "")
        main_script.value = str(initial.get("main_script") or "")
        if initial.get("teams") is not None:
            teams_picker.value = list(initial.get("teams") or [])
        downstream_picker.value = list(initial.get("downstream_tools") or [])
        cpu_cores.value = int(initial.get("cpu_cores") or 1)
        ram_mb.value = int(initial.get("ram_mb") or 512)
        gpu_memory_mb.value = int(initial.get("gpu_memory_mb") or 0)
        licences.value = str(initial.get("licences") or "")
        approvals_policy.value = str(initial.get("approvals_policy") or "none")
        if str(approvals_policy.value) != "none":
            approvers_picker.enable()
        approvers_picker.value = list(initial.get("approvers") or [])
        email_notifications.value = bool(initial.get("email_notifications", False))
        _rc = initial.get("run_cap")
        run_cap_in.value = int(_rc) if _rc is not None and _rc != "" else None

    # Mutable holder so repeated "Save draft" clicks overwrite the same
    # row instead of stacking up new ones; seeded from the resumed draft.
    draft_ref: dict[str, int | None] = {"id": draft_id}

    save_button: ui.button | None = None  # populated below; wired to validation

    def _validation_error() -> str | None:
        """Return the first validation error or ``None`` when the form is OK."""
        raw_alias = (alias.value or "").strip().lower()
        if not raw_alias or not _ALIAS_RE.match(raw_alias):
            return "Alias must start with a lowercase letter and contain only a-z, 0-9, and _."
        if raw_alias in taken_aliases:
            return "That alias is already in use by another tool."
        if not (name.value or "").strip():
            return "Name is required."
        if not (short_description.value or "").strip():
            return "Short description is required."
        if len((short_description.value or "").strip()) > 200:
            return "Short description must be 200 characters or fewer."
        if not (main_script.value or "").strip():
            return "Main script is required."
        if mode["value"] == "upload" and uploads["descriptor"]["bytes"] is None:
            return "A descriptor JSON file is required."
        if uploads["static"]["bytes"] is None:
            return "A static-files ZIP is required."
        return None

    # Track whether the operator has manually edited the main_script
    # field. While untouched (or matching the alias-derived default),
    # we keep it synced to ``<alias>.py`` as the operator types.
    main_script_user_edited = {"flag": False}
    current_alias = {"value": ""}  # latest committed alias value (mirrors alias.value).

    def _on_main_script_edit(e: Any = None) -> None:
        # Anything that doesn't match the current alias-derived suggestion
        # counts as a manual edit and locks the field from auto-sync.
        # (When _sync_main_script writes ``<alias>.py`` programmatically
        # the value matches the suggestion, so the flag stays unset.)
        typed = "" if e is None else str(getattr(e, "value", "") or "")
        suggested = f"{current_alias['value']}.py" if current_alias["value"] else ""
        if typed != suggested:
            main_script_user_edited["flag"] = True

    def _sync_main_script() -> None:
        """Set ``main_script`` to ``<alias>.py`` while still in sync mode."""
        if main_script_user_edited["flag"]:
            return
        main_script.value = f"{current_alias['value']}.py" if current_alias["value"] else ""

    main_script.on_value_change(_on_main_script_edit)

    def _on_alias_change(e: Any = None) -> None:
        """Refresh alias-driven UI from the just-committed value.

        ``on_value_change`` fires after NiceGUI writes the new value
        into ``alias.value``, and the event arguments carry the value
        directly — both are reliable, unlike the raw
        ``update:model-value`` Vue event which can land before the
        framework finishes committing.
        """
        raw = ("" if e is None else str(getattr(e, "value", "") or "")).strip().lower()
        current_alias["value"] = raw
        if not raw:
            alias_hint.text = ""
        elif not _ALIAS_RE.match(raw):
            alias_hint.text = (
                "Alias must start with a lowercase letter and contain only a-z, 0-9, and _."
            )
        elif raw in taken_aliases:
            alias_hint.text = "That alias is already in use by another tool."
        else:
            alias_hint.text = ""

        _sync_main_script()
        _refresh_save_button()

    def _refresh_save_button() -> None:
        err = _validation_error()
        if save_button is not None:
            if err is None:
                save_button.enable()
            else:
                save_button.disable()
        message.text = err or ""

    def _refresh_validation(_e: Any = None) -> None:
        """Re-evaluate the Save button state and surface the first missing field."""
        _refresh_save_button()

    # Alias has its own dedicated handler (drives the main_script sync).
    alias.on_value_change(_on_alias_change)
    # Other fields just trigger a button-state refresh.
    for field in (name, short_description, long_description, main_script, licences):
        field.on("update:model-value", _refresh_validation)
    for slot in uploads.values():
        slot["on_change"] = _refresh_validation

    async def _save() -> None:
        if _validation_error() is not None:
            # The button is disabled when invalid; this is belt-and-braces.
            return
        raw_alias = (alias.value or "").strip().lower()
        licence_names = [s.strip() for s in (licences.value or "").split(",") if s.strip()]
        # Initial version is server-controlled — backend forces 0.1.0
        # regardless of what we send. We pass the canonical value here
        # so the schema's required-field validation is satisfied.
        metadata: dict[str, Any] = {
            "alias": raw_alias,
            "name": str(name.value).strip(),
            "version": "0.1.0",
            "short_description": str(short_description.value).strip(),
            "long_description": (str(long_description.value).strip() or None),
            "main_script": str(main_script.value).strip(),
            "teams": list(teams_picker.value or []),
            "downstream_tools": list(downstream_picker.value or []),
            "cpu_cores": int(cpu_cores.value or 1),
            "ram_mb": int(ram_mb.value or 512),
            "gpu_memory_mb": int(gpu_memory_mb.value or 0),
            "licences_required": [{"name": n, "count": 1} for n in licence_names],
            "duration_class": _DEFAULT_DURATION_CLASS,
            "approvals_policy": str(approvals_policy.value or "none"),
            "approver_usernames": list(approvers_picker.value or []),
            "email_notifications": bool(email_notifications.value),
        }

        # Wizard mode → assemble a descriptor JSON from {form metadata
        # + wizard payload}; upload mode → use the operator-uploaded
        # descriptor file as-is. Either way the create_tool API
        # contract stays the same: a multipart with a descriptor_json
        # file plus the form metadata.
        if mode["value"] == "wizard":
            wizard_payload = build_descriptor_payload(wizard_state)
            descriptor_dict = {
                "schema_version": 1,
                "alias": raw_alias,
                "name": metadata["name"],
                "version": metadata["version"],
                "short_description": metadata["short_description"],
                "long_description": metadata["long_description"],
                "main_script": metadata["main_script"],
                "teams": metadata["teams"],
                "downstream_tools": metadata["downstream_tools"],
                "resources": {
                    "cpu_cores": metadata["cpu_cores"],
                    "ram_mb": metadata["ram_mb"],
                    "gpu_memory_mb": metadata["gpu_memory_mb"],
                },
                "licences_required": metadata["licences_required"],
                "approvals_policy": metadata["approvals_policy"],
                "approver_usernames": metadata["approver_usernames"],
                "email_notifications": metadata["email_notifications"],
                **wizard_payload,
            }
            descriptor_bytes = json.dumps(descriptor_dict).encode("utf-8")
            descriptor_filename = f"{raw_alias}_descriptors.json"
        else:
            descriptor_bytes = bytes(uploads["descriptor"]["bytes"])
            descriptor_filename = str(uploads["descriptor"]["filename"])

        client = ApiClient(get_app(), token=current_token())
        try:
            await client.create_tool(
                metadata=metadata,
                descriptor_json_bytes=descriptor_bytes,
                descriptor_filename=descriptor_filename,
                static_zip_bytes=bytes(uploads["static"]["bytes"]),
                static_zip_filename=str(uploads["static"]["filename"]),
                icon_bytes=(
                    bytes(uploads["icon"]["bytes"])
                    if uploads["icon"]["bytes"] is not None
                    else None
                ),
                icon_filename=uploads["icon"]["filename"],
                # presets_bytes / presets_filename intentionally omitted —
                # the wizard's Presets tab is the v2 authoring surface;
                # new bundles never ship a separate presets JSON file.
                # ApiClient.create_tool defaults both to None.
            )
        except ApiError as exc:
            message.text = exc.detail
            return

        # Run cap is a row-level setting applied via its own endpoint
        # (no descriptor version bump), so it's saved after the create
        # succeeds — only when the operator entered one.
        run_cap_value = run_cap_in.value
        run_cap_payload: int | None = (
            int(run_cap_value) if run_cap_value is not None and str(run_cap_value) != "" else None
        )
        if run_cap_payload is not None:
            try:
                await client.set_tool_run_cap(raw_alias, run_cap_payload)
            except ApiError as exc:
                ui.notify(
                    f"Tool {raw_alias} installed, but its run cap wasn't saved "
                    f"({exc.detail}). Set it from the tool's Edit page.",
                    type="warning",
                )
                ui.navigate.to("/admin/tools")
                return

        # Now the alias exists, reconcile the upstream-wiring handshake:
        # make each tool the author wired as a source list this new tool
        # in its downstream_tools. Additive + best-effort (a failure here
        # doesn't undo the successful install).
        patched = await reconcile_downstream_registrations(
            state=wizard_state,
            current_alias=raw_alias,
            register=lambda src, dst: client.register_downstream_tool(
                source_alias=src, downstream_alias=dst
            ),
        )
        # The tool now exists, so the draft (if any) has served its
        # purpose — clear it best-effort so it doesn't linger in the list.
        if draft_ref["id"] is not None:
            with contextlib.suppress(ApiError):
                await client.delete_tool_draft(int(draft_ref["id"]))
            draft_ref["id"] = None
        if patched:
            ui.notify(
                f"Tool {raw_alias} installed. Updated source tools: {', '.join(patched)}.",
                type="positive",
            )
        else:
            ui.notify(f"Tool {raw_alias} installed.", type="positive")
        ui.navigate.to("/admin/tools")

    def _collect_draft_payload() -> dict[str, Any]:
        """Snapshot the current form + wizard state as a draft payload.

        Mirrors the install metadata minus the uploaded files (which a
        draft never carries). The wizard's contribution is the same
        descriptor-shaped blob the Edit page round-trips, so resuming
        rebuilds the tabs exactly.
        """
        run_cap_value = run_cap_in.value
        return {
            "alias": str(alias.value or "").strip(),
            "name": str(name.value or "").strip(),
            "short_description": str(short_description.value or "").strip(),
            "long_description": str(long_description.value or "").strip(),
            "main_script": str(main_script.value or "").strip(),
            "teams": list(teams_picker.value or []),
            "downstream_tools": list(downstream_picker.value or []),
            "cpu_cores": int(cpu_cores.value or 1),
            "ram_mb": int(ram_mb.value or 512),
            "gpu_memory_mb": int(gpu_memory_mb.value or 0),
            "licences": str(licences.value or ""),
            "approvals_policy": str(approvals_policy.value or "none"),
            "approvers": list(approvers_picker.value or []),
            "email_notifications": bool(email_notifications.value),
            "run_cap": (
                int(run_cap_value)
                if run_cap_value is not None and str(run_cap_value) != ""
                else None
            ),
            "mode": mode["value"],
            "wizard": build_descriptor_payload(wizard_state),
        }

    async def _save_draft() -> None:
        """Persist the in-progress setup so it can be resumed later.

        No validation gate — a draft is partial by definition. Repeated
        saves overwrite the same row via ``draft_ref``.
        """
        title = str(name.value or "").strip() or str(alias.value or "").strip()
        draft_client = ApiClient(get_app(), token=current_token())
        try:
            saved = await draft_client.save_tool_draft(
                title=title,
                alias=str(alias.value or "").strip(),
                payload=_collect_draft_payload(),
                draft_id=draft_ref["id"],
            )
        except ApiError as exc:
            message.text = f"Couldn't save draft: {exc.detail}"
            return
        draft_ref["id"] = int(saved["id"])
        ui.notify("Draft saved — resume it any time from the Tools page.", type="positive")

    with ui.row().classes("w-full justify-end gap-2"):
        ui.button(
            "Cancel",
            on_click=lambda: ui.navigate.to("/admin/tools"),
        ).props("flat")
        ui.button(
            "Save draft",
            icon="save",
            on_click=_save_draft,
        ).props("flat color=primary").tooltip(
            "Keep your progress without installing. Uploaded files aren't "
            "saved — re-attach them when you resume."
        )
        save_button = ui.button("Save new tool", on_click=_save).props("color=primary")

    # Validation / error messages live directly under the Save button so
    # the operator reads them right where they act.
    message = ui.label("").style("color: #c0392b;")

    # Run an initial validation pass so the button starts disabled
    # (we know it's invalid — alias is empty) and the message field
    # tells the operator what's missing.
    _refresh_validation()


def _wire_world_mutual_exclusion(teams_picker: ui.select) -> None:
    """Make 'World' mutually exclusive with specific teams in the picker.

    Picking 'World' when other teams are chosen → keep only 'World'.
    Picking another team when 'World' is selected → drop 'World'.
    Both make sense intuitively: 'World' means "everyone", so combining
    it with a specific team is contradictory.
    """
    state: dict[str, list[str]] = {"prev": list(teams_picker.value or [])}

    def _on_change(_e: Any = None) -> None:
        current = list(teams_picker.value or [])
        prev = state["prev"]
        added = [t for t in current if t not in prev]
        if not added or len(current) <= 1:
            state["prev"] = current
            return
        if _WORLD_TEAM_NAME in added:
            # World was just added alongside others — keep only World.
            new_value = [_WORLD_TEAM_NAME]
        elif _WORLD_TEAM_NAME in current:
            # A non-World team was just added while World was present
            # — drop World, keep the rest.
            new_value = [t for t in current if t != _WORLD_TEAM_NAME]
        else:
            new_value = current
        if new_value != current:
            teams_picker.value = new_value
        state["prev"] = list(new_value)

    teams_picker.on("update:model-value", _on_change)


_UPLOAD_MAX_FILE_BYTES = 500 * 1024 * 1024
"""Belt-and-braces ceiling on per-file uploads through the New Tool form.

Static-files ZIPs are normally a few MB; setting an explicit high
ceiling stops Quasar's default "Filter" check from silently dropping
a multi-hundred-MB bundle and makes the rejection callback the
single place a refused file shows up.
"""


def _file_uploader(
    *,
    label: str,
    accept: str,
    helper: str,
    target: dict[str, Any],
) -> None:
    """One labelled file upload widget that captures bytes into ``target``.

    When ``target["on_change"]`` is set, it's invoked after each
    upload — that's how the form's reactive validation re-runs when a
    new file is selected. The status line walks through three states:
    "No file selected." → "Uploading <name>…" (on transfer begin) →
    "Selected: <name> (N bytes)" (on completion). A rejection or a
    handler exception writes the reason into the same row so the
    operator never sees an upload silently disappear.
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
