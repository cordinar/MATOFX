"""Admin: Edit user (``/ui/admin/users/{user_id}/edit``).

Single-page edit form, two-column layout matching the legacy "Edit
user" tab. Pre-populated from the existing user record.

Sections:

* Identity (left column) — first / last name, email. ``username`` is
  *display-only*: usernames are referential keys (audit ``actor``
  strings, tool approver lists), renaming them is a Phase 2 concern.
* Credentials (left column) — password + re-type, both inline. Leave
  blank to keep the existing password; otherwise the new value is
  validated against the password policy before it's accepted.
* Role + teams + flags (right column) — role select (labels rendered
  user-facing as ``Regular User`` etc. via :func:`format_role_label`,
  values stay canonical), team multi-select, approver checkbox,
  "Allow log in" toggle (maps to deactivate / reactivate).

The page issues separate API calls per section that *changed* — so
editing only the role doesn't pointlessly rewrite the email or roll
the password. Any failed call surfaces a per-field message and aborts
the rest of the save (the user can retry without re-entering work).
"""

from __future__ import annotations

import re
from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import (
    format_role_label,
    order_base_role_names,
    render_capabilities_card,
    render_shell,
    require_logged_in,
)
from matofx.ui.pages._password_rules import (
    render_password_match_indicator,
    render_password_rule_list,
)
from matofx.ui.state import current_token

_WHITESPACE_RE = re.compile(r"\s+")


@ui.page("/admin/users/{user_id}/edit")
async def admin_user_edit_page(user_id: int) -> None:
    """Render the edit-user form for ``user_id``."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())
    try:
        user = await client.get_user(user_id)
        roles = await client.list_roles()
        teams = await client.list_teams()
        me = await client.me()
        # Tools list drives the sole-approver pre-flight in _submit
        # (does removing this user from approvers leave any tool with
        # no approvers?). Other approvers' usernames come from the
        # users list we already load below.
        all_users = await client.list_users()
        tools = await client.list_admin_tools()
        general_settings = await client.get_general_settings()
    except ApiError as exc:
        with ui.column().classes("w-full max-w-5xl mx-auto p-6"):
            ui.label(f"We couldn't load that user: {exc.detail}").style("color: #c0392b;")
        return

    # Base roles fill the single Role select; capability roles (ADR-0005)
    # stack on top via a separate card shown only to admins who can manage
    # roles (the API enforces ``roles.manage`` regardless). Base roles use
    # the canonical privilege order (SU, Admin, Beta, Regular, then custom
    # A→Z); capabilities are alphabetical.
    role_names = order_base_role_names(
        [str(r["name"]) for r in roles if str(r.get("kind") or "base") == "base"]
    )
    capability_role_names = sorted(
        str(r["name"]) for r in roles if str(r.get("kind") or "") == "capability"
    )
    can_manage_roles = "roles.manage" in (me.get("permissions") or [])
    # "World" is a visibility wildcard, not a team admins assign people
    # to — see migration 0014. Hide it from the team picker so the form
    # only exposes membership-capable teams.
    team_options = {
        int(t["id"]): str(t["name"]) for t in teams if str(t.get("name") or "") != "World"
    }

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.button(
                icon="arrow_back",
                on_click=lambda: ui.navigate.to("/admin/users"),
            ).props("flat round dense")
            ui.label(f"Edit user: {user.get('username', '')}").style(
                "font-size: 1.5rem; font-weight: 600;"
            )

        password_policy = dict(general_settings.get("values") or {})
        await _render_form(
            user,
            role_names,
            capability_role_names,
            can_manage_roles,
            team_options,
            all_users,
            tools,
            password_policy,
        )


async def _render_form(
    user: dict[str, Any],
    role_names: list[str],
    capability_role_names: list[str],
    can_manage_roles: bool,
    team_options: dict[int, str],
    all_users: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    password_policy: dict[str, Any],
) -> None:
    """Two-column form pre-populated from the user record.

    Capabilities (ADR-0005) render in their own card *below* the
    base-role card so the two tiers read as distinct concepts.
    """
    user_id = int(user["id"])
    current_role = str(user.get("role") or "")
    current_email = str(user.get("email") or "")
    current_first = str(user.get("first_name") or "")
    current_last = str(user.get("last_name") or "")
    is_active = bool(user.get("is_active"))
    is_approver = bool(user.get("is_approver"))
    current_team_ids = sorted(int(t["id"]) for t in (user.get("teams") or []))
    current_capability_roles = sorted(str(c) for c in (user.get("capability_roles") or []))

    # Activity-provenance link (ADR-0010 slice 5 §7). Opens the
    # admin standalone lineage page centred on the user — surfaces
    # every job submitted, approval acted on, library file / material
    # curated. Requires the existing admin nav presence anyway, so no
    # extra permission gate beyond what brought the user here.
    with ui.row().classes("items-center gap-2 w-full").style("padding-bottom: 0.5rem;"):
        ui.button(
            "↗ Activity provenance",
            icon="device_hub",
            on_click=lambda uid=user_id: ui.navigate.to(f"/admin/lineage/user/{uid}", new_tab=True),
        ).props("flat dense color=primary").tooltip(
            "Open the user's lineage / provenance graph in a new tab."
        )

    capability_picker: ui.select | None = None
    with ui.card().classes("w-full"), ui.row().classes("w-full no-wrap gap-6"):
        with ui.column().classes("gap-3").style("flex: 1 1 0; min-width: 0;"):
            ui.label(f"Username: {user.get('username', '')} (read-only)").style(
                "color: #888; font-size: 0.85rem;"
            )
            first_name = (
                ui.input("First name *", value=current_first).classes("w-full").props("dense")
            )
            last_name = ui.input("Last name *", value=current_last).classes("w-full").props("dense")
            email = ui.input("Email *", value=current_email).classes("w-full").props("dense")

            password = (
                ui.input("New password", password=True, password_toggle_button=True)
                .classes("w-full")
                .props("dense")
            )
            password_rules = render_password_rule_list(
                password, password_policy, show_when_empty=False
            )
            password2 = (
                ui.input("Re-type new password", password=True, password_toggle_button=True)
                .classes("w-full")
                .props("dense")
            )
            password_match = render_password_match_indicator(
                password, password2, show_when_empty=False
            )
            ui.label("Leave the password fields blank to keep the existing password.").style(
                "color: #888; font-size: 0.8rem;"
            )

        with ui.column().classes("gap-3").style("flex: 1 1 0; min-width: 0;"):
            default_role = (
                current_role
                if current_role in role_names
                else (role_names[0] if role_names else None)
            )
            role_options = {name: format_role_label(name) for name in role_names}
            role = (
                ui.select(role_options, label="Role *", value=default_role)
                .classes("w-full")
                .props("dense")
            )

            ui.label("Teams").style("font-weight: 500;")
            if team_options:
                team_picker = (
                    ui.select(
                        options=team_options,
                        multiple=True,
                        label="Teams",
                        with_input=True,
                    )
                    .classes("w-full")
                    .props("dense use-chips")
                )
                team_picker.value = list(current_team_ids)
            else:
                team_picker = None
                ui.label(
                    "No teams configured yet — create them first under Admin Space → Teams."
                ).style("color: #888; font-size: 0.85rem;")

            approver = ui.checkbox("Eligible to be a tool approver", value=is_approver)
            logon_enabled = ui.checkbox(
                "Allow this user to log in",
                value=is_active,
            ).tooltip(
                "Unchecking deactivates the account. Existing sessions are revoked immediately."
            )

    # Full-width, below the card. Kept *outside* the two-column ``no-wrap``
    # row on purpose: a long error (e.g. the sole-approver warning) placed
    # inside that row becomes a third flex item and squeezes the columns into
    # an unreadable, overlapping mess. As its own block it wraps cleanly.
    message = ui.label("").classes("w-full").style("color: #c0392b; margin-top: 0.25rem;")

    if can_manage_roles and capability_role_names:
        capability_picker = render_capabilities_card(
            capability_role_names, selected=list(current_capability_roles)
        )

    async def _submit() -> None:
        message.text = ""
        normalised_first = _normalise_whitespace(first_name.value or "")
        normalised_last = _normalise_whitespace(last_name.value or "")
        normalised_email = (email.value or "").strip().lower()

        if not normalised_first:
            message.text = "First name is required."
            return
        if not normalised_last:
            message.text = "Last name is required."
            return
        if not normalised_email or "@" not in normalised_email:
            message.text = "Enter a complete email address (like alice@example.com)."
            return
        if password.value and password.value != password2.value:
            message.text = "The two password fields don't match."
            return

        target_team_ids = sorted(int(t) for t in (team_picker.value or [])) if team_picker else []
        # ``None`` when the picker isn't shown (actor lacks roles.manage) so
        # the save leaves capability membership untouched; ``[]`` clears it.
        target_capability_roles = (
            sorted(str(c) for c in (capability_picker.value or []))
            if capability_picker is not None
            else None
        )
        new_role = str(role.value)
        new_active = bool(logon_enabled.value)
        new_approver = bool(approver.value)
        target_username = str(user.get("username") or "").lower()

        # Sole-approver pre-flight. The cascade in deactivate / set_approver(False)
        # raises SoleApproverError if the user is the only approver on a
        # tool — surface that *before* the call so we can show a modal
        # offering replacements rather than failing mid-save.
        client = ApiClient(get_app(), token=current_token())
        cascade_will_fire = is_active and is_approver and (not new_active or not new_approver)
        if cascade_will_fire:
            sole_tools = [
                t
                for t in tools
                if target_username in {a.lower() for a in (t.get("approver_usernames") or [])}
                and len({a.lower() for a in (t.get("approver_usernames") or [])}) == 1
            ]
            if sole_tools:
                replacements = {
                    str(u["username"]): (f"{u.get('full_name') or u['username']} ({u['username']})")
                    for u in all_users
                    if u.get("is_approver")
                    and u.get("is_active")
                    and str(u["username"]).lower() != target_username
                }
                if not replacements:
                    tool_names = ", ".join(str(t.get("name") or t["alias"]) for t in sole_tools)
                    plural = "tool" if len(sole_tools) == 1 else "tools"
                    message.text = (
                        f"This user is the only approver on {len(sole_tools)} {plural}: "
                        f"{tool_names}. No one else is flagged as an eligible approver, so "
                        "removing their approver status (or deactivating them) would leave "
                        f"{'that tool' if len(sole_tools) == 1 else 'those tools'} with no "
                        "approver. Open Edit User on another person, tick 'Eligible to be a "
                        "tool approver', and save — then retry this change."
                    )
                    return
                chosen = await _ask_for_replacement_approvers(sole_tools, replacements)
                if chosen is None:
                    return  # operator cancelled the modal
                # Swap in the replacement approver per tool BEFORE the
                # cascade runs. Each call goes through the audit pipeline.
                for alias, replacement in chosen.items():
                    try:
                        await client.set_tool_approvers(alias, approver_usernames=[replacement])
                    except ApiError as exc:
                        message.text = f"Couldn't swap the approver on {alias}: {exc.detail}"
                        return

        try:
            if (
                normalised_first != current_first
                or normalised_last != current_last
                or normalised_email != current_email
            ):
                await client.update_user_identity(
                    user_id,
                    first_name=normalised_first,
                    last_name=normalised_last,
                    email=normalised_email,
                )
            if password.value:
                await client.set_user_password(user_id, password=str(password.value))
            if new_role != current_role:
                await client.set_user_role(user_id, role=new_role)
            if new_approver != is_approver:
                await client.set_user_approver(user_id, is_approver=new_approver)
            if target_team_ids != current_team_ids:
                await client.set_user_teams(user_id, team_ids=target_team_ids)
            if (
                target_capability_roles is not None
                and target_capability_roles != current_capability_roles
            ):
                await client.set_user_capability_roles(user_id, role_names=target_capability_roles)
            if new_active != is_active:
                if new_active:
                    await client.reactivate_user(user_id)
                else:
                    await client.deactivate_user(user_id)
        except ApiError as exc:
            message.text = exc.detail
            return

        ui.notify("User updated.", type="positive")
        ui.navigate.to("/admin/users")

    save_button: ui.button | None = None

    def _refresh_save_button() -> None:
        """Disable Save when the admin started a password change that's incomplete.

        Empty password fields mean "don't change it" — Save stays
        enabled. Once either field is touched, Save is blocked until
        the rules pass and the two fields match.
        """
        if save_button is None:
            return
        if password_rules.relevant() or password_match.relevant():
            if password_rules.valid() and password_match.valid():
                save_button.enable()
            else:
                save_button.disable()
        else:
            save_button.enable()

    password_rules.on_change(_refresh_save_button)
    password_match.on_change(_refresh_save_button)

    with ui.row().classes("w-full justify-end gap-2"):
        ui.button(
            "Cancel",
            on_click=lambda: ui.navigate.to("/admin/users"),
        ).props("flat")
        save_button = ui.button("Save changes", on_click=_submit).props("color=primary")
    _refresh_save_button()


def _normalise_whitespace(value: str) -> str:
    """Strip + collapse internal whitespace runs to a single space."""
    return _WHITESPACE_RE.sub(" ", value.strip())


async def _ask_for_replacement_approvers(
    sole_tools: list[dict[str, Any]],
    replacements: dict[str, str],
) -> dict[str, str] | None:
    """Modal: pick a replacement approver for each tool in ``sole_tools``.

    Returns a dict ``{tool_alias: replacement_username}`` on confirm,
    or ``None`` on cancel. Pre-selects the first available replacement
    per tool so a one-click "Confirm" is enough when the operator's
    happy with the default choice.
    """
    first_replacement = next(iter(replacements))
    chosen: dict[str, str] = {str(t["alias"]): first_replacement for t in sole_tools}

    with ui.dialog() as dialog, ui.card().style("min-width: 540px; max-height: 80vh;"):
        ui.label("Replacement approvers needed").style("font-size: 1.2rem; font-weight: 600;")
        ui.label(
            "This user is the only approver on the tools below. Pick a "
            "replacement before continuing — the change would fail otherwise. "
            "This user is excluded from the list of options."
        ).style("color: #b58900; font-size: 0.9rem;")

        for tool in sole_tools:
            alias = str(tool["alias"])
            with ui.row().classes("w-full items-center gap-2"):
                with ui.column().classes("flex-1 gap-0"):
                    ui.label(str(tool.get("name") or alias)).style("font-weight: 600;")
                    ui.label(f"alias: {alias}").style("color: #888; font-size: 0.8rem;")

                def _on_change(e: Any, _alias: str = alias) -> None:
                    chosen[_alias] = str(e.value)

                (
                    ui.select(replacements, value=first_replacement)
                    .classes("w-72")
                    .props("dense")
                    .on_value_change(_on_change)
                )

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat")
            ui.button(
                "Swap approvers and continue",
                on_click=lambda: dialog.submit(chosen),
            ).props("color=primary")

    result = await dialog
    return result if isinstance(result, dict) else None


_REGISTERED: list[Any] = [admin_user_edit_page]
