"""My Account Settings (``/ui/account``) — show + edit the logged-in user's profile.

Hits ``GET /api/auth/me`` for identity + team membership + preferences;
the team list and the email-notifications switch are bundled into the
same payload so the page renders with one round-trip. Edits route
through the self-service endpoints:

- ``POST /api/auth/me/preferences`` — toggle the email opt-in.
- ``POST /api/auth/me/theme`` — flip dark/light theme.
- ``POST /api/auth/me/change-password`` — current + new password.

Identity edits (name / email) and admin actions (role, teams, approver
flag, deactivate) live on the admin user-edit page; this page only
exposes what the user can change about themselves.

The look follows the home-redesign shell (ADR-0016): themed cards with
an icon-chip header, colours pulled from the theme CSS variables so the
page tracks the user's light/dark choice — the same preference the
header toggle and the Display card below both write through.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import (
    format_role_label,
    render_shell,
    require_logged_in,
    section_card,
)
from matofx.ui.pages._password_rules import (
    render_password_match_indicator,
    render_password_rule_list,
)
from matofx.ui.state import current_token, store_locale, store_timezone

#: Page title shown in-app and used as the standalone label everywhere
#: this page is linked from (sidebar footer, home tile menu, docs).
PAGE_TITLE = "My Account Settings"


@ui.page("/account")
async def account_page() -> None:
    """Render the user's identity, teams, and preferences."""
    if not require_logged_in():
        return
    # The shell renders the theme toggle in the header (it persists the
    # choice and the page re-tints live), so this page no longer carries
    # its own Display card.
    render_shell(help_topic="account-page")

    client = ApiClient(get_app(), token=current_token())
    try:
        me = await client.me()
        general_settings = await client.get_general_settings()
    except ApiError as exc:
        with ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;"):
            ui.label(f"We couldn't load your account: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
        return

    password_policy = dict(general_settings.get("values") or {})

    with ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;"):
        # Header in the home-page idiom: a bold title + a muted subtitle.
        ui.label(PAGE_TITLE).style(
            "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
        )
        ui.label("Your profile and personal preferences — visible only to you.").style(
            "color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;"
        )

        # Two-column card grid (mirrors the home "My Work" grid); the
        # wide password card sits full-width beneath it.
        with ui.element("div").classes("w-full").style(
            "display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; align-items: start;"
        ):
            _render_identity_card(me)
            _render_teams_card(me)
            _render_regional_settings_card(client, me)
            _render_preferences_card(client, me)
            # Only offer the AI-summary opt-out when the install has enrichment on —
            # otherwise there is nothing to opt out of (auto-summaries.md §9).
            if bool(password_policy.get("llm_summaries_enabled")):
                _render_summary_enrichment_card(client, me)
        _render_password_card(client, password_policy)


def _render_identity_card(me: dict[str, Any]) -> None:
    with section_card("badge", "Identity"):
        _row("Username", str(me.get("username") or ""))
        _row("Email", str(me.get("email") or ""))
        _row("Full name", str(me.get("full_name") or ""))
        _row("Role", format_role_label(str(me.get("role") or "")))
        _render_approver_row(me)
        _row("Member since", _format_datetime(me.get("created_at")))
        _row("Last sign-in", _format_datetime(me.get("last_login_at")) or "—")
        _row("User ID", str(me.get("user_id") or ""))


def _render_approver_row(me: dict[str, Any]) -> None:
    """Identity-card row showing approver status and, when applicable, the tools.

    A job approver is shown the actual tools they're listed on (by tool
    name) rather than a bare "eligible" flag, so they can see at a glance
    what they're on the hook to sign off. An eligible user with no tool
    assignments and a non-approver each get a one-line explanation.
    """
    is_approver = bool(me.get("is_approver"))
    tools = [str(t) for t in (me.get("approver_tools") or [])]
    with ui.row().classes("w-full items-start"):
        ui.label("Approver").style(
            "font-weight: 600; min-width: 9rem; color: var(--matofx-label-text);"
        )
        with ui.column().classes("gap-1"):
            if not is_approver:
                ui.label("Not currently eligible to be a tool approver")
            elif tools:
                ui.label("Listed as an approver for these tools:")
                with ui.row().classes("gap-2 flex-wrap"):
                    for name in tools:
                        ui.chip(name, icon="task_alt").props("outline")
            else:
                ui.label("Eligible to approve tools, but not yet listed on any.")


def _render_teams_card(me: dict[str, Any]) -> None:
    teams = list(me.get("teams") or [])
    with section_card("groups", "Teams"):
        if not teams:
            ui.label(
                "You're not a member of any team yet. An administrator can add "
                "you to teams under Admin → Teams."
            ).style("color: var(--matofx-muted-text); font-size: 0.9rem;")
            return
        with ui.row().classes("w-full gap-2 flex-wrap"):
            for team in teams:
                ui.chip(str(team.get("name") or ""), icon="group").props("outline")


_LOCALE_OPTIONS: dict[str, str] = {
    "auto": "Use my browser's language",
    "en-GB": "English (United Kingdom)",
    "en-US": "English (United States)",
    "en-CA": "English (Canada)",
    "en-AU": "English (Australia)",
    "en-IE": "English (Ireland)",
    "en-IN": "English (India)",
    "fr-FR": "Français (France)",
    "fr-CA": "Français (Canada)",
    "de-DE": "Deutsch (Deutschland)",
    "de-AT": "Deutsch (Österreich)",
    "de-CH": "Deutsch (Schweiz)",
    "es-ES": "Español (España)",
    "es-MX": "Español (México)",
    "it-IT": "Italiano (Italia)",
    "nl-NL": "Nederlands (Nederland)",
    "pt-PT": "Português (Portugal)",
    "pt-BR": "Português (Brasil)",
    "sv-SE": "Svenska (Sverige)",
    "da-DK": "Dansk (Danmark)",
    "no-NO": "Norsk (Norge)",
    "fi-FI": "Suomi (Suomi)",
    "pl-PL": "Polski (Polska)",
    "cs-CZ": "Čeština (Česko)",
    "tr-TR": "Türkçe (Türkiye)",
    "ru-RU": "Русский (Россия)",
    "ja-JP": "日本語 (日本)",
    "zh-CN": "中文 (简体)",
    "zh-TW": "中文 (繁體)",
    "ko-KR": "한국어 (대한민국)",
    "ar-SA": "العربية (السعودية)",
}

_TIMEZONE_OPTIONS: dict[str, str] = {
    "auto": "Use my browser's time zone",
    "UTC": "UTC — Coordinated Universal Time",
    "Europe/London": "Europe/London (UK)",
    "Europe/Dublin": "Europe/Dublin (Ireland)",
    "Europe/Paris": "Europe/Paris (France)",
    "Europe/Berlin": "Europe/Berlin (Germany)",
    "Europe/Madrid": "Europe/Madrid (Spain)",
    "Europe/Rome": "Europe/Rome (Italy)",
    "Europe/Amsterdam": "Europe/Amsterdam (Netherlands)",
    "Europe/Stockholm": "Europe/Stockholm (Sweden)",
    "Europe/Helsinki": "Europe/Helsinki (Finland)",
    "Europe/Warsaw": "Europe/Warsaw (Poland)",
    "Europe/Istanbul": "Europe/Istanbul (Türkiye)",
    "Europe/Moscow": "Europe/Moscow (Russia)",
    "Africa/Johannesburg": "Africa/Johannesburg (South Africa)",
    "Africa/Cairo": "Africa/Cairo (Egypt)",
    "Asia/Dubai": "Asia/Dubai (UAE)",
    "Asia/Riyadh": "Asia/Riyadh (Saudi Arabia)",
    "Asia/Kolkata": "Asia/Kolkata (India)",
    "Asia/Singapore": "Asia/Singapore (Singapore)",
    "Asia/Hong_Kong": "Asia/Hong_Kong (Hong Kong)",
    "Asia/Shanghai": "Asia/Shanghai (China)",
    "Asia/Tokyo": "Asia/Tokyo (Japan)",
    "Asia/Seoul": "Asia/Seoul (South Korea)",
    "Australia/Perth": "Australia/Perth",
    "Australia/Sydney": "Australia/Sydney",
    "Pacific/Auckland": "Pacific/Auckland (New Zealand)",
    "America/Toronto": "America/Toronto (Eastern, Canada)",
    "America/New_York": "America/New_York (US East)",
    "America/Chicago": "America/Chicago (US Central)",
    "America/Denver": "America/Denver (US Mountain)",
    "America/Los_Angeles": "America/Los_Angeles (US West)",
    "America/Anchorage": "America/Anchorage (Alaska)",
    "Pacific/Honolulu": "Pacific/Honolulu (Hawaii)",
    "America/Mexico_City": "America/Mexico_City (Mexico)",
    "America/Sao_Paulo": "America/Sao_Paulo (Brazil)",
    "America/Buenos_Aires": "America/Buenos_Aires (Argentina)",
}


def _render_regional_settings_card(client: ApiClient, me: dict[str, Any]) -> None:
    """Locale + timezone pickers. Persist via the API; chrome re-reads on next render.

    Each dropdown opens with the user's stored value pre-selected. The
    very first option is **"Use my browser's…"** — the literal
    ``'auto'`` — so a user signing in from a laptop that's been
    flying around the world keeps seeing local time without having to
    update their account each time.

    Selection changes hit the API right away so the next page render
    picks them up. The chrome clock JS is recomputed on every render,
    so a /home navigate (or even an in-page reload of this card) is
    enough to see the new format — no full page refresh required.
    """
    with section_card("schedule", "Region & time"):
        ui.label(
            "These choices control the language for dates and numbers and the "
            "time zone the clock at the top of the page shows. Pick "
            '"Use my browser\'s…" and MATOFX will follow whatever your '
            "device says."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        message = ui.label("").style("color: var(--matofx-error-text); font-size: 0.85rem;")

        current_locale_value = str(me.get("locale_preference") or "auto")
        current_timezone_value = str(me.get("timezone_preference") or "auto")
        # The stored value might be something we don't surface in the
        # curated list (e.g. an admin set a rarely-used IANA zone via
        # the API). Add it as a one-off entry so the dropdown still
        # opens with the right selection rather than silently flipping
        # to 'auto'.
        locale_options = dict(_LOCALE_OPTIONS)
        if current_locale_value not in locale_options:
            locale_options[current_locale_value] = current_locale_value
        timezone_options = dict(_TIMEZONE_OPTIONS)
        if current_timezone_value not in timezone_options:
            timezone_options[current_timezone_value] = current_timezone_value

        with ui.row().classes("w-full gap-3 no-wrap"):
            locale_select = (
                ui.select(
                    options=locale_options,
                    label="Language / region",
                    value=current_locale_value,
                    with_input=True,
                )
                .classes("w-full")
                .props("dense")
            )
            timezone_select = (
                ui.select(
                    options=timezone_options,
                    label="Time zone",
                    value=current_timezone_value,
                    with_input=True,
                )
                .classes("w-full")
                .props("dense")
            )

        async def _persist(event: Any) -> None:
            del event
            message.text = ""
            chosen_locale = str(locale_select.value or "auto")
            chosen_tz = str(timezone_select.value or "auto")
            try:
                refreshed = await client.set_regional_preferences(
                    locale=chosen_locale, timezone=chosen_tz
                )
            except ApiError as exc:
                message.text = f"We couldn't save that: {exc.detail}"
                return
            persisted_locale = str(refreshed.get("locale_preference") or chosen_locale)
            persisted_tz = str(refreshed.get("timezone_preference") or chosen_tz)
            store_locale(persisted_locale)
            store_timezone(persisted_tz)
            ui.notify(
                "Region and time-zone updated. The clock at the top will refresh.",
                type="positive",
            )

        locale_select.on_value_change(_persist)
        timezone_select.on_value_change(_persist)


def _render_preferences_card(client: ApiClient, me: dict[str, Any]) -> None:
    with section_card("notifications", "Notification preferences"):
        ui.label(
            "When this is on, MATOFX emails you when one of your jobs starts, "
            "finishes, or fails — but only for tools whose author has opted in "
            "to email notifications. Switch this off to silence every job-state "
            "email, regardless of the tool."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        message = ui.label("").style("color: var(--matofx-error-text); font-size: 0.85rem;")
        switch = ui.switch(
            "Send me job-state emails",
            value=bool(me.get("email_notifications_enabled", True)),
        )

        async def _on_change(event: Any) -> None:
            del event
            message.text = ""
            try:
                await client.update_my_preferences(email_notifications_enabled=bool(switch.value))
            except ApiError as exc:
                # Roll the UI back so the toggle reflects the persisted state.
                switch.value = not bool(switch.value)
                message.text = f"We couldn't save that: {exc.detail}"
                return
            ui.notify(
                "Email notifications turned on."
                if switch.value
                else "Email notifications turned off.",
                type="positive",
            )

        switch.on_value_change(_on_change)


def _render_summary_enrichment_card(client: ApiClient, me: dict[str, Any]) -> None:
    with section_card("auto_awesome", "Result summaries"):
        ui.label(
            "Your administrator has turned on AI-assisted result summaries. When "
            "this is on, the short summary at the top of your job and study "
            "results is rewritten for readability by the configured AI service "
            "(from the same audited figures — no numbers are invented). Switch "
            "this off to always see the plain, machine-written summary instead."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        message = ui.label("").style("color: var(--matofx-error-text); font-size: 0.85rem;")
        switch = ui.switch(
            "Use AI-enriched result summaries",
            value=bool(me.get("summary_enrichment_enabled", True)),
        )

        async def _on_change(event: Any) -> None:
            del event
            message.text = ""
            try:
                await client.set_summary_enrichment_enabled(enabled=bool(switch.value))
            except ApiError as exc:
                switch.value = not bool(switch.value)
                message.text = f"We couldn't save that: {exc.detail}"
                return
            ui.notify(
                "AI-enriched summaries turned on."
                if switch.value
                else "Plain summaries turned on.",
                type="positive",
            )

        switch.on_value_change(_on_change)


def _render_password_card(client: ApiClient, password_policy: dict[str, Any]) -> None:
    with section_card("lock", "Change password"):
        ui.label(
            "Enter your current password, then choose a new one. The new "
            "password must meet your install's password policy."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

        current = (
            ui.input("Current password *", password=True, password_toggle_button=True)
            .classes("w-full")
            .props("dense")
        )
        new_pw = (
            ui.input("New password *", password=True, password_toggle_button=True)
            .classes("w-full")
            .props("dense")
        )
        password_rules = render_password_rule_list(new_pw, password_policy)
        confirm = (
            ui.input("Re-type new password *", password=True, password_toggle_button=True)
            .classes("w-full")
            .props("dense")
        )
        password_match = render_password_match_indicator(new_pw, confirm)
        message = ui.label("").style("color: var(--matofx-error-text); font-size: 0.85rem;")

        async def _submit() -> None:
            message.text = ""
            if not current.value:
                message.text = "Enter your current password."
                return
            if not new_pw.value:
                message.text = "Enter a new password."
                return
            if new_pw.value != confirm.value:
                message.text = "The two new-password fields don't match."
                return
            try:
                await client.change_my_password(
                    current_password=str(current.value),
                    new_password=str(new_pw.value),
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            current.value = ""
            new_pw.value = ""
            confirm.value = ""
            ui.notify("Password updated.", type="positive")
            _refresh_update_button()

        update_button: ui.button | None = None

        def _refresh_update_button() -> None:
            """Disable Update until every precondition for the change is in place.

            Requires a non-empty current password, every password rule
            passing, and the new + confirm fields matching.
            """
            if update_button is None:
                return
            blocked = (
                not bool(current.value) or not password_rules.valid() or not password_match.valid()
            )
            if blocked:
                update_button.disable()
            else:
                update_button.enable()

        password_rules.on_change(_refresh_update_button)
        password_match.on_change(_refresh_update_button)
        current.on_value_change(lambda _e=None: _refresh_update_button())

        with ui.row().classes("w-full justify-end"):
            update_button = ui.button("Update password", on_click=_submit).props("color=primary")
        _refresh_update_button()


def _row(label: str, value: str) -> None:
    with ui.row().classes("w-full items-baseline"):
        ui.label(label).style(
            "font-weight: 600; min-width: 9rem; color: var(--matofx-label-text);"
        )
        ui.label(value).style("color: var(--matofx-body-text);")


def _format_datetime(value: Any) -> str:
    """Render an ISO-8601 string or ``datetime`` as ``YYYY-MM-DD HH:MM UTC``.

    Returns an empty string for ``None``; pass-through for already-empty
    values so the caller can choose its own placeholder ("—" etc.).
    """
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        try:
            parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    elif isinstance(value, _dt.datetime):
        parsed = value
    else:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed.astimezone(_dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
