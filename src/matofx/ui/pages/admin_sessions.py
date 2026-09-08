"""Admin: Active sessions (``/ui/admin/sessions``) — ADR-0019 slice 4.

Who is signed in right now, and — under a Concurrent Session Licence —
how many of the licence's places that uses. The page exists for one
concrete moment: a Super User has just been admitted **over** the cap
by break-glass, and needs to free a real place. Everything here serves
that: capacity first, longest-idle sessions first, one click to sign
someone out.

Ordering is deliberate. The API returns most-recently-active first,
which is exactly backwards for this task — the person you can most
safely sign out is the one who has been idle longest — so the page
re-sorts by idle time descending.

Permissions: ``users.manage``, the same right that already covers
enabling and disabling accounts. The API enforces it; this page just
avoids showing a button that would 403.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_user_datetime
from matofx.ui.pages._chrome import format_role_label, render_shell, require_logged_in
from matofx.ui.state import current_token

_IDLE_WARN_MINUTES = 15
"""Idle time past which a session is tinted as a likely candidate to free."""


@ui.page("/admin/sessions")
async def admin_sessions_page() -> None:
    """Render the active-sessions surface."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())

    with ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;"):
        with ui.column().classes("gap-1"):
            ui.label("Active sessions").style(
                "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
            )
            ui.label(
                "Everyone signed in right now. Signing someone out ends their session "
                "immediately and, on a concurrent-session licence, frees their place."
            ).style("color: var(--matofx-muted-text); font-size: 0.9rem;")

        body = ui.column().classes("w-full gap-4")

        async def refresh() -> None:
            body.clear()
            try:
                data = await client.list_active_sessions()
            except ApiError as exc:
                with body:
                    ui.label(f"Couldn't load sessions: {exc.detail}").style(
                        "color: var(--matofx-danger-text, #f0716b);"
                    )
                return
            except Exception as exc:  # pragma: no cover - defensive
                with body:
                    ui.label(f"Couldn't load sessions: {type(exc).__name__}: {exc}")
                return
            with body:
                _render_capacity(data)
                _render_table(data, refresh)

        await refresh()
        ui.button("Refresh", icon="refresh", on_click=refresh).props(
            "flat dense no-caps color=primary"
        )


def _render_capacity(data: dict[str, Any]) -> None:
    """The headline: places used against the cap, when there is one."""
    in_use = int(data.get("in_use") or 0)
    n_slots = data.get("n_slots")
    metered = bool(data.get("metered"))
    over_cap = bool(data.get("over_cap"))

    if not metered:
        caption = (
            f"{in_use} session(s) open. This install isn't metered on concurrent "
            "sessions, so there's no cap to stay under."
        )
        colour = "var(--matofx-muted-text)"
        icon = "info"
    elif over_cap:
        caption = (
            f"{in_use} of {n_slots} places in use — over the licence cap. Someone was "
            "admitted by break-glass; sign an idle session out to get back within it."
        )
        colour = "#d6a93b"
        icon = "warning"
    else:
        caption = f"{in_use} of {n_slots} places in use."
        colour = "var(--matofx-body-text)"
        icon = "verified_user"

    with ui.card().classes("w-full").style("padding: 0.9rem 1.1rem;"):
        with ui.row().classes("items-center no-wrap").style("gap: 0.7rem;"):
            ui.icon(icon).style(f"color: {colour}; font-size: 1.4rem;")
            ui.label(caption).style(f"color: {colour}; font-weight: 550;")


def _render_table(data: dict[str, Any], refresh: Any) -> None:
    """One row per live session, longest-idle first."""
    rows = list(data.get("sessions") or [])
    if not rows:
        ui.label("Nobody is signed in.").style("color: var(--matofx-muted-text);")
        return
    # Longest-idle first: the safest session to end is the top one.
    rows.sort(key=lambda r: int(r.get("idle_minutes") or 0), reverse=True)

    with ui.card().classes("w-full").style("padding: 0; overflow: hidden;"):
        with ui.column().classes("w-full").style("gap: 0;"):
            _header_row()
            for row in rows:
                _session_row(row, refresh)


def _header_row() -> None:
    with (
        ui.row()
        .classes("w-full items-center no-wrap")
        .style(
            "padding: 0.55rem 1.1rem; gap: 0.75rem; font-size: 0.78rem; font-weight: 650; "
            "letter-spacing: 0.02em; text-transform: uppercase; "
            "color: var(--matofx-muted-text); border-bottom: 1px solid var(--matofx-border);"
        )
    ):
        ui.label("User").style("flex: 2 1 0;")
        ui.label("Role").style("flex: 1 1 0;")
        ui.label("Signed in").style("flex: 1.4 1 0;")
        ui.label("Idle").style("flex: 0.8 1 0;")
        ui.label("From").style("flex: 1 1 0;")
        ui.label("").style("flex: 0 0 96px;")


def _session_row(row: dict[str, Any], refresh: Any) -> None:
    idle_minutes = int(row.get("idle_minutes") or 0)
    is_you = bool(row.get("is_you"))
    idle_colour = "#d6a93b" if idle_minutes >= _IDLE_WARN_MINUTES else "var(--matofx-muted-text)"

    async def revoke() -> None:
        client = ApiClient(get_app(), token=current_token())
        try:
            await client.revoke_active_session(int(row["id"]))
        except ApiError as exc:
            ui.notify(f"Couldn't sign them out: {exc.detail}", type="negative")
            return
        ui.notify(f"Signed {row.get('username')} out — their place is free.", type="positive")
        await refresh()

    with (
        ui.row()
        .classes("w-full items-center no-wrap")
        .style(
            "padding: 0.6rem 1.1rem; gap: 0.75rem; font-size: 0.88rem; "
            "border-bottom: 1px solid var(--matofx-border);"
        )
    ):
        with ui.column().classes("gap-0").style("flex: 2 1 0; min-width: 0;"):
            ui.label(str(row.get("full_name") or row.get("username") or "")).style(
                "font-weight: 600; overflow: hidden; text-overflow: ellipsis;"
            )
            ui.label(str(row.get("username") or "")).style(
                "color: var(--matofx-muted-text); font-size: 0.78rem;"
            )
        with ui.column().classes("gap-0").style("flex: 1 1 0; min-width: 0;"):
            ui.label(format_role_label(str(row.get("role") or "")))
            if row.get("breakglass"):
                # This page's job is getting back under the cap; these are
                # the sessions that put the site over it.
                ui.label("over the limit").style(
                    "color: #d6a93b; font-size: 0.72rem; font-weight: 600;"
                ).tooltip(
                    "Admitted over the licence limit as a Super User. Ends "
                    "automatically within 12 hours."
                )
        ui.label(format_user_datetime(str(row.get("started_at") or ""))).style(
            "flex: 1.4 1 0; color: var(--matofx-muted-text);"
        )
        ui.label(_idle_label(idle_minutes)).style(f"flex: 0.8 1 0; color: {idle_colour};")
        ui.label(str(row.get("ip_address") or "—")).style(
            "flex: 1 1 0; color: var(--matofx-muted-text); font-size: 0.8rem;"
        )
        with ui.row().classes("items-center justify-end").style("flex: 0 0 96px;"):
            if is_you:
                # Never offer to sign an admin out of the page they are
                # using — especially not the one they opened to fix a
                # full licence.
                ui.label("You").style(
                    "color: var(--matofx-muted-text); font-size: 0.8rem; font-style: italic;"
                )
            else:
                ui.button("Sign out", on_click=revoke).props("flat dense no-caps color=negative")


def _idle_label(minutes: int) -> str:
    """``just now`` / ``12m`` / ``3h 05m`` — short enough for a table cell."""
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"
