"""Admin: Users (``/ui/admin/users``).

The list page. Lists every user with badges (active / deactivated,
role, approver flag) and a controls row above the cards for search /
filter / sort — the page stays usable when an install grows to
hundreds of accounts.

Two action buttons sit in the page header:

* **New user** — navigates to ``/admin/users/new``.
* **Edit user** — opens a picker dialog showing every user as
  ``Full Name (username)``; choosing one navigates to
  ``/admin/users/{id}/edit``. Useful when the admin already knows the
  name of the person they're looking for and doesn't want to scroll
  the list.

Per-row "Edit" buttons stay in place so the click-on-the-row flow is
unchanged.

The actual create / edit forms live on dedicated pages
(:mod:`matofx.ui.pages.admin_user_new` and
:mod:`matofx.ui.pages.admin_user_edit`) — this page just lists and
links to them. Single sidebar entry, three routes.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import format_role_label, render_shell, require_logged_in
from matofx.ui.state import current_token

# The status filter options shown in the toolbar select. Order matters
# — "All" should be the default and visually first.
_STATUS_FILTERS: tuple[str, ...] = ("All", "Active", "Deactivated", "Approvers")

# Sort options. The label is what the admin picks; the lambda extracts
# the comparable key from each user dict. The three name-based options
# are explicit (first / last / username) so the admin doesn't have to
# guess which name field "alphabetic" means. ``Recently active`` and
# ``Newest first`` are descending; the name-based ones are ascending.
_SORT_KEYS: dict[str, Any] = {
    "By first name": (lambda u: str(u.get("first_name") or "").lower(), False),
    "By last name": (lambda u: str(u.get("last_name") or "").lower(), False),
    "By username": (lambda u: str(u.get("username") or "").lower(), False),
    "Recently active": (lambda u: str(u.get("last_login_at") or ""), True),
    "Newest first": (lambda u: str(u.get("created_at") or ""), True),
}


@ui.page("/admin/users")
async def admin_users_page() -> None:
    """List every user; link to the dedicated new / edit pages."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())
    try:
        users = await client.list_users()
    except ApiError as exc:
        with ui.column().classes("w-full max-w-5xl mx-auto p-6"):
            ui.label(f"We couldn't load the users list: {exc.detail}").style("color: #c0392b;")
        return

    # Floating-assignment capacity, when the licence meters it. Shown
    # before the admin opens the new-user form, so they don't discover
    # the cap when the save is refused (ADR-0019 slice 5). Best-effort:
    # a failure here must never block the users list itself.
    capacity: dict[str, Any] = {}
    try:
        capacity = await client.get_licence_capacity()
    except Exception:  # capacity is a hint, not the page
        capacity = {}

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Users").style("font-size: 1.5rem; font-weight: 600;")
            with ui.row().classes("gap-2"):
                ui.button(
                    "Edit user",
                    icon="edit",
                    on_click=lambda: _open_user_picker(users),
                ).props("flat")
                ui.button(
                    "New user",
                    icon="person_add",
                    on_click=lambda: ui.navigate.to("/admin/users/new"),
                ).props("color=primary")

        _render_licence_capacity(capacity)

        if not users:
            ui.label("No users yet.").style("color: #555;")
            return

        _render_controls_and_list(users)


def _render_controls_and_list(users: list[dict[str, Any]]) -> None:
    """Toolbar (search / status / sort) + reactive card list + count."""
    state: dict[str, Any] = {
        "search": "",
        "status": _STATUS_FILTERS[0],
        "sort": next(iter(_SORT_KEYS)),
    }

    with ui.row().classes("w-full items-end gap-3"):
        search = (
            ui.input(
                "Search",
                placeholder="By full name, username, or email",
            )
            .classes("flex-1")
            .props("dense clearable")
        )
        status = (
            ui.select(list(_STATUS_FILTERS), label="Status", value=state["status"])
            .classes("w-44")
            .props("dense")
        )
        sort = (
            ui.select(list(_SORT_KEYS.keys()), label="Sort by", value=state["sort"])
            .classes("w-56")
            .props("dense")
        )

    count_label = ui.label("").style("color: #888; font-size: 0.85rem;")
    list_container = ui.column().classes("w-full gap-3")

    def _rerender() -> None:
        state["search"] = str(search.value or "")
        state["status"] = str(status.value or _STATUS_FILTERS[0])
        state["sort"] = str(sort.value or next(iter(_SORT_KEYS)))
        filtered = _apply_filters(users, state["search"], state["status"], state["sort"])
        suffix = "" if len(users) == 1 else "s"
        count_label.text = f"Showing {len(filtered):,} of {len(users):,} user{suffix}"
        list_container.clear()
        with list_container:
            if not filtered:
                ui.label(
                    "No users match those filters. Try clearing the search "
                    "or switching the status filter."
                ).style("color: #555;")
                return
            for user in filtered:
                _render_user_card(user)

    search.on("update:model-value", lambda _e=None: _rerender())
    status.on("update:model-value", lambda _e=None: _rerender())
    sort.on("update:model-value", lambda _e=None: _rerender())

    _rerender()


def _apply_filters(
    users: list[dict[str, Any]],
    search: str,
    status: str,
    sort: str,
) -> list[dict[str, Any]]:
    """Pure filter pipeline: substring search → status filter → sort."""
    needle = search.strip().lower()
    out = users
    if needle:
        out = [
            u
            for u in out
            if needle in str(u.get("username") or "").lower()
            or needle in str(u.get("full_name") or "").lower()
            or needle in str(u.get("email") or "").lower()
        ]
    if status == "Active":
        out = [u for u in out if u.get("is_active")]
    elif status == "Deactivated":
        out = [u for u in out if not u.get("is_active")]
    elif status == "Approvers":
        out = [u for u in out if u.get("is_approver")]

    key_fn, reverse = _SORT_KEYS.get(sort, next(iter(_SORT_KEYS.values())))
    return sorted(out, key=key_fn, reverse=reverse)


def _render_licence_capacity(capacity: dict[str, Any]) -> None:
    """Show floating-assignment usage, when the licence meters it.

    Silent on every other install: a concurrent-session licence meters
    logins, not accounts, so a "places used" line here would be
    misleading. Turns amber at the cap because the next **New user**
    will be refused.
    """
    if not capacity.get("metered"):
        return
    in_use = int(capacity.get("in_use") or 0)
    n_slots = int(capacity.get("n_slots") or 0)
    remaining = int(capacity.get("remaining") or 0)
    if remaining == 0:
        text = (
            f"{in_use} of {n_slots} licence places assigned — all of them. Remove access "
            "from someone who no longer needs it before adding another person."
        )
        colour, icon = "#d6a93b", "warning"
    else:
        text = (
            f"{in_use} of {n_slots} licence places assigned ({remaining} left). "
            "Deactivating an account frees its place."
        )
        colour, icon = "var(--matofx-muted-text)", "how_to_reg"
    with ui.row().classes("w-full items-center no-wrap").style("gap: 0.5rem;"):
        ui.icon(icon).style(f"color: {colour}; font-size: 1.1rem;")
        ui.label(text).style(f"color: {colour}; font-size: 0.87rem;")


def _render_user_card(user: dict[str, Any]) -> None:
    """One row in the list — identity badges + Edit button.

    Identity line is rendered as ``Full Name (username)`` so the
    person's display name leads and the username sits as the
    parenthesised reference key the admin uses for typing into the
    picker / referring to in tickets.
    """
    user_id = int(user["id"])
    is_active = bool(user.get("is_active"))
    is_approver = bool(user.get("is_approver"))
    teams = user.get("teams") or []
    full_name = str(user.get("full_name") or "").strip()
    username = str(user["username"])
    headline = f"{full_name} ({username})" if full_name else username
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-start justify-between"):
            with ui.column().classes("gap-1 flex-1"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(headline).style("font-weight: 600;")
                    ui.badge(format_role_label(str(user.get("role") or "")) or "—").props(
                        "color=primary"
                    )
                    ui.badge("ACTIVE" if is_active else "DEACTIVATED").props(
                        "color=positive" if is_active else "color=grey"
                    )
                    if is_approver:
                        ui.badge("APPROVER").props("color=info")
                if user.get("email"):
                    ui.label(str(user["email"])).style("color: #555; font-size: 0.85rem;")
                if teams:
                    ui.label("Teams: " + ", ".join(str(t["name"]) for t in teams)).style(
                        "color: #888; font-size: 0.8rem;"
                    )
                if user.get("last_login_at"):
                    ui.label(f"Last login: {user['last_login_at']}").style(
                        "color: #888; font-size: 0.8rem;"
                    )
            with ui.column().classes("gap-2 items-end"):
                ui.button(
                    "Edit",
                    icon="edit",
                    on_click=lambda _e=None, _uid=user_id: ui.navigate.to(
                        f"/admin/users/{_uid}/edit"
                    ),
                ).props("flat")


def _open_user_picker(users: list[dict[str, Any]]) -> None:
    """Modal: pick a user as ``Full Name (username)`` → navigate to the edit page.

    The ``with_input=True`` prop turns the select into a filter-as-you-type
    combobox, so an admin who knows the name doesn't need to scroll the
    full list — same approach the legacy "Edit User" tab used.
    """
    options = {
        int(u["id"]): f"{u.get('full_name') or u['username']} ({u['username']})" for u in users
    }
    with ui.dialog() as dialog, ui.card().style("min-width: 480px;"):
        ui.label("Edit user").style("font-size: 1.2rem; font-weight: 600;")
        ui.label("Pick the user you want to edit.").style("color: #888; font-size: 0.85rem;")
        picker = (
            ui.select(
                options=options,
                label="User",
                with_input=True,
            )
            .classes("w-full")
            .props("dense")
        )

        def _go() -> None:
            if picker.value is None:
                return
            uid = int(picker.value)
            dialog.close()
            ui.navigate.to(f"/admin/users/{uid}/edit")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Edit", on_click=_go).props("color=primary")
    dialog.open()
