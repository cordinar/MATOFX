"""Admin: New user (``/ui/admin/users/new``).

Single-page form for creating a user, replacing the cramped modal
that lived on the users list page. Two-column layout matches the
legacy "Add User" page:

* Left column — identity + credentials (first / last name, username,
  email, password + re-type).
* Right column — role, teams (multi-select), approver flag,
  logon-enabled toggle.

Per the ``users_are_not_modellers`` rule every error is per-field and
plain English.

Mapping notes vs the legacy:

* Legacy split "Email (alias)" + ``EMAIL_DOMAIN`` config to compose
  the full address. v2 has no per-install email-domain setting — the
  field accepts a complete address (``alice@example.com``).
* Logon-enabled "No" maps to *create then immediately deactivate*
  rather than a separate boolean column. The audit trail records
  both events; the user appears as DISABLED in the list page.
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


@ui.page("/admin/users/new")
async def admin_user_new_page() -> None:
    """Render the create-user form."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())
    try:
        roles = await client.list_roles()
        teams = await client.list_teams()
        me = await client.me()
        # Used for live duplicate-detection on the username + email
        # fields. Server-side uniqueness checks (409 from the API)
        # remain authoritative — these sets just give the admin
        # immediate feedback before they press Create.
        existing_users = await client.list_users()
        general_settings = await client.get_general_settings()
    except ApiError as exc:
        with ui.column().classes("w-full max-w-5xl mx-auto p-6"):
            ui.label(f"We couldn't load the form: {exc.detail}").style("color: #c0392b;")
        return

    # Base roles fill the single Role select; capability roles (ADR-0005)
    # are stacked on top via a separate card shown only to admins who can
    # manage roles (the API enforces ``roles.manage`` regardless). Base
    # roles use the canonical privilege order (SU, Admin, Beta, Regular,
    # then custom A→Z); capabilities are alphabetical.
    base_role_names = order_base_role_names(
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
    taken_usernames = {str(u["username"]).lower() for u in existing_users}
    taken_emails = {str(u["email"]).lower() for u in existing_users}
    password_policy = dict(general_settings.get("values") or {})

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.button(
                icon="arrow_back",
                on_click=lambda: ui.navigate.to("/admin/users"),
            ).props("flat round dense")
            ui.label("New user").style("font-size: 1.5rem; font-weight: 600;")

        ui.label("Create a new MATOFX user. Required fields are marked with an asterisk.").style(
            "color: #b0b3b8; font-size: 0.85rem;"
        )

        await _render_form(
            base_role_names,
            capability_role_names,
            can_manage_roles,
            team_options,
            taken_usernames,
            taken_emails,
            password_policy,
        )


async def _render_form(
    role_names: list[str],
    capability_role_names: list[str],
    can_manage_roles: bool,
    team_options: dict[int, str],
    taken_usernames: set[str],
    taken_emails: set[str],
    password_policy: dict[str, Any],
) -> None:
    """Two-column form: identity/credentials left, role/teams/flags right.

    Capabilities (ADR-0005) render in their own card *below* the base-role
    card so the two tiers read as distinct concepts.
    """
    capability_picker: ui.select | None = None
    with ui.card().classes("w-full"), ui.row().classes("w-full no-wrap gap-6"):
        with ui.column().classes("gap-3").style("flex: 1 1 0; min-width: 0;"):
            first_name = ui.input("First name *").classes("w-full").props("dense")
            last_name = ui.input("Last name *").classes("w-full").props("dense")
            username = (
                ui.input("Username *", placeholder="lowercase, no spaces")
                .classes("w-full")
                .props("dense")
            )
            ui.label(
                "Pick this carefully — the username can't be changed later. "
                "It's used in audit records and tool approver lists."
            ).style("color: #b58900; font-size: 0.8rem;")
            username_hint = ui.label("").style(
                "color: #c0392b; font-size: 0.8rem; min-height: 1.1em;"
            )
            email = (
                ui.input("Email *", placeholder="alice@example.com")
                .classes("w-full")
                .props("dense")
            )
            email_hint = ui.label("").style("color: #c0392b; font-size: 0.8rem; min-height: 1.1em;")
            password = (
                ui.input(
                    "Password *",
                    password=True,
                    password_toggle_button=True,
                )
                .classes("w-full")
                .props("dense")
            )
            password_rules = render_password_rule_list(password, password_policy)
            password2 = (
                ui.input(
                    "Re-type password *",
                    password=True,
                    password_toggle_button=True,
                )
                .classes("w-full")
                .props("dense")
            )
            password_match = render_password_match_indicator(password, password2)

        with ui.column().classes("gap-3").style("flex: 1 1 0; min-width: 0;"):
            # Pre-select the admin-configured "Default role for new
            # accounts" (General Settings) when it's a valid base role;
            # otherwise fall back to Regular User, then the first role.
            configured_default = str(password_policy.get("new_account_default_role") or "")
            default_role = (
                configured_default
                if configured_default in role_names
                else (
                    "regular_user"
                    if "regular_user" in role_names
                    else (role_names[0] if role_names else None)
                )
            )
            # Render labels as "Regular User" while the value the form
            # submits is the canonical "regular_user" the DB stores.
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
                team_picker.value = []
            else:
                team_picker = None
                ui.label(
                    "No teams configured yet — create them first under Admin Space → Teams."
                ).style("color: #888; font-size: 0.85rem;")

            approver = ui.checkbox("Eligible to be a tool approver", value=False)
            logon_enabled = ui.checkbox(
                "Allow this user to log in",
                value=True,
            ).tooltip("Uncheck to create the account as deactivated.")

        message = ui.label("").style("color: #c0392b;")

    if can_manage_roles and capability_role_names:
        capability_picker = render_capabilities_card(capability_role_names, selected=[])

    create_button: ui.button | None = None  # populated below; referenced by the live checks

    def _check_username() -> None:
        """Live check: empty / has spaces / already taken."""
        raw = (username.value or "").strip().lower()
        if not raw:
            username_hint.text = ""
        elif any(c.isspace() for c in raw):
            username_hint.text = "Username cannot contain spaces."
        elif raw in taken_usernames:
            username_hint.text = f"That username is already taken — pick a different one for {raw}."
        else:
            username_hint.text = ""
        _refresh_create_button()

    def _check_email() -> None:
        """Live check: format ok / already taken."""
        raw = (email.value or "").strip().lower()
        if not raw:
            email_hint.text = ""
        elif "@" not in raw:
            email_hint.text = "Enter a complete email address (like alice@example.com)."
        elif raw in taken_emails:
            email_hint.text = "That email is already in use by another account."
        else:
            email_hint.text = ""
        _refresh_create_button()

    def _refresh_create_button() -> None:
        """Disable Create unless every live check passes.

        Blocks on a username/email conflict or any unmet password rule.
        """
        if create_button is None:
            return
        blocked = (
            bool(username_hint.text)
            or bool(email_hint.text)
            or not password_rules.valid()
            or not password_match.valid()
        )
        if blocked:
            create_button.disable()
        else:
            create_button.enable()

    password_rules.on_change(_refresh_create_button)
    password_match.on_change(_refresh_create_button)
    username.on("blur", lambda _e=None: _check_username())
    username.on("update:model-value", lambda _e=None: _check_username())
    email.on("blur", lambda _e=None: _check_email())
    email.on("update:model-value", lambda _e=None: _check_email())

    async def _submit() -> None:
        message.text = ""
        normalised_first = _normalise_whitespace(first_name.value or "")
        normalised_last = _normalise_whitespace(last_name.value or "")
        normalised_username = (username.value or "").strip().lower()
        normalised_email = (email.value or "").strip().lower()

        # Per-field validation, plain English (users-are-not-modellers).
        if not normalised_first:
            message.text = "First name is required."
            return
        if not normalised_last:
            message.text = "Last name is required."
            return
        if not normalised_username:
            message.text = "Username is required."
            return
        if any(c.isspace() for c in normalised_username):
            message.text = "Username cannot contain spaces."
            return
        if normalised_username in taken_usernames:
            message.text = "That username is already taken. Pick a different one."
            return
        if not normalised_email or "@" not in normalised_email:
            message.text = "Enter a complete email address (like alice@example.com)."
            return
        if normalised_email in taken_emails:
            message.text = "That email is already in use by another account."
            return
        if not password.value:
            message.text = "Password is required."
            return
        if password.value != password2.value:
            message.text = "The two password fields don't match."
            return
        team_ids = [int(t) for t in (team_picker.value or [])] if team_picker else []
        capability_roles = (
            [str(c) for c in (capability_picker.value or [])] if capability_picker else []
        )

        client = ApiClient(get_app(), token=current_token())
        try:
            created = await client.create_user(
                username=normalised_username,
                email=normalised_email,
                first_name=normalised_first,
                last_name=normalised_last,
                password=str(password.value),
                role=str(role.value),
                is_approver=bool(approver.value),
                team_ids=team_ids,
                capability_roles=capability_roles,
            )
            if not bool(logon_enabled.value):
                await client.deactivate_user(int(created["id"]))
        except ApiError as exc:
            # Server-side uniqueness check is authoritative — surface
            # whatever message it returned (e.g. our 409 mapping for
            # DuplicateUserError).
            message.text = exc.detail
            return

        ui.notify(
            f"User {normalised_first} {normalised_last} created.",
            type="positive",
        )
        ui.navigate.to("/admin/users")

    with ui.row().classes("w-full justify-end gap-2"):
        ui.button(
            "Cancel",
            on_click=lambda: ui.navigate.to("/admin/users"),
        ).props("flat")
        create_button = ui.button("Create user", on_click=_submit).props("color=primary")
    # The rule and match callbacks fired during render before create_button
    # existed; run one more pass now so the button starts in the right state.
    _refresh_create_button()


def _normalise_whitespace(value: str) -> str:
    """Strip + collapse internal whitespace runs to a single space."""
    return _WHITESPACE_RE.sub(" ", value.strip())


# Suppress unused-import warning when the file is only imported for the
# decorator side effect (registers the page route on import).
_REGISTERED: list[Any] = [admin_user_new_page]
