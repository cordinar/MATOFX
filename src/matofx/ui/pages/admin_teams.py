"""Admin: Teams, roles & permissions (``/ui/admin/teams``).

Two tabs:

1. **Teams & Access Rights Groups** — three side-by-side, transparent
   cards that read as visual twins: **Teams**, **Access Rights Groups -
   Base Roles**, and **Access Rights Groups - Capabilities**. Each group
   is its own expandable sub-card listing its members (sortable by first
   name, last name, or username and rendered as ``Full Name (username)``)
   with a multi-select *Add members* control. A global search bar at the
   top filters all three sections instantly to just the groups the
   matched user(s) belong to. Nothing here can be deleted — teams and
   groups are permanent for audit.
2. **Permissions** — choose a group; edit its description + the
   permission set that matches its kind (base or capability) as labelled
   checkboxes grouped by area. ``super_user`` is read-only with an
   explanatory note (its permission set is sentinel-managed in code).

The closed permission catalog is fetched once at page load via
``GET /api/admin/permissions`` so the editor reflects the current
codebase without us having to mirror the catalog in the frontend.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import (
    format_role_label,
    order_base_role_names,
    render_shell,
    require_logged_in,
)
from matofx.ui.state import current_token

# Sort options shared by all three sections. The label is what the
# operator picks; the lambda extracts the comparable key from a user dict
# (which carries first_name / last_name / username).
_MEMBER_SORTS: dict[str, Any] = {
    "By first name": lambda u: str(u.get("first_name") or "").lower(),
    "By last name": lambda u: str(u.get("last_name") or "").lower(),
    "By username": lambda u: str(u.get("username") or "").lower(),
}

_WORLD_TEAM_NAME = "World"
"""The seeded universal-visibility marker (see migration 0014). It's
a wire-format team row so the tool-edit team picker can show it
alongside real teams, but it carries no membership and shouldn't be
manageable from this page — admins can't add users to "everyone"."""

_REGULAR_USER = "regular_user"
"""The default base role. A user must always hold exactly one base role,
so removing them from any other base role moves them here."""

# Section icons.
_ICON_TEAMS = "groups"
_ICON_BASE = "shield"
_ICON_CAPS = "extension"

# Per-base-role expansion icons, echoing the sidebar privilege tiers:
# shield = Super User, person+gear = Administrator, lab flask = Beta
# Tester, single person = Regular User; custom base roles get a badge.
_BASE_ROLE_ICONS: dict[str, str] = {
    "super_user": "shield",
    "administrator": "manage_accounts",
    "beta_tester": "science",
    "regular_user": "person",
}


def _base_role_icon(role_name: str) -> str:
    """Material icon for a base role's card (custom roles fall back to a badge)."""
    return _BASE_ROLE_ICONS.get(role_name, "badge")


# Permission-editor section-header labels: whole-section overrides (renames
# + spelled-out V&V) plus per-token acronyms (HPC/AOS/KPI).
_SECTION_HEADER_OVERRIDES: dict[str, str] = {
    "vv": "Verification & Validation",
    "library": "File Library",
    "models": "Surrogate Models",
}
_SECTION_TOKEN_ACRONYMS: dict[str, str] = {"hpc": "HPC", "aos": "AOS", "kpi": "KPI"}


def _section_label(section: str) -> str:
    """Header label for a permission section (acronym- and rename-aware)."""
    if section in _SECTION_HEADER_OVERRIDES:
        return _SECTION_HEADER_OVERRIDES[section]
    return " ".join(
        _SECTION_TOKEN_ACRONYMS.get(token, token.title()) for token in section.split("_")
    )


@ui.page("/admin/teams")
async def admin_teams_page() -> None:
    """Render the two-tab teams + roles + permissions admin page."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())
    try:
        teams = await client.list_teams()
        users = await client.list_users()
        roles = await client.list_roles()
        permissions = await client.list_permissions()
        # Preload every team's membership up front so the global search +
        # member lists are fully in-memory (no per-keystroke round-trips).
        team_members_by_id: dict[int, set[int]] = {}
        for team in teams:
            if str(team.get("name") or "") == _WORLD_TEAM_NAME:
                continue
            try:
                rows = await client.list_team_members(int(team["id"]))
            except ApiError:
                rows = []
            team_members_by_id[int(team["id"])] = {int(r["user_id"]) for r in rows}
    except ApiError as exc:
        with ui.column().classes("w-full max-w-5xl mx-auto p-6"):
            ui.label(f"We couldn't load this page: {exc.detail}").style("color: #c0392b;")
        return

    # "World" is a visibility wildcard, not a manageable team — hide it
    # from this page so admins don't try to add users to "everyone".
    # The tool-edit team picker still surfaces it via its own fetch.
    teams = [t for t in teams if str(t.get("name") or "") != _WORLD_TEAM_NAME]
    users_by_id = {int(u["id"]): u for u in users}

    # Full screen width — the three side-by-side sections want the room.
    with ui.column().classes("w-full p-6 gap-4"):
        ui.label("Teams, roles & permissions").style("font-size: 1.5rem; font-weight: 600;")
        # Tabs aligned left, sentence-case (no-caps), with the brand teal
        # active colour + indicator (mirrors the Compare Jobs page).
        with (
            ui.tabs()
            .classes("w-full")
            .props("align=left active-color=primary indicator-color=primary")
            .style("border-bottom: 1px solid var(--matofx-card-border);") as tabs
        ):
            tab_overview = ui.tab("Teams & Access Rights Groups", icon="groups").props("no-caps")
            tab_perms = ui.tab("Permissions", icon="vpn_key").props("no-caps")
        # Transparent panels so the page background shows through (mirrors
        # the Compare Jobs page) rather than reading as a tinted block.
        with (
            ui.tab_panels(tabs, value=tab_overview)
            .classes("w-full")
            .style("background-color: transparent;")
        ):
            with ui.tab_panel(tab_overview).style("background-color: transparent;"):
                _render_overview_tab(
                    teams, roles, users, users_by_id, team_members_by_id, permissions
                )
            with ui.tab_panel(tab_perms).style("background-color: transparent;"):
                _render_permissions_tab(roles, permissions)


# ---------- Tab 1: Teams + access-rights overview --------------------


def _user_matches(user: dict[str, Any], query: str) -> bool:
    """True when ``query`` (already lower-cased) is in the user's names/username."""
    return (
        query in str(user.get("first_name") or "").lower()
        or query in str(user.get("last_name") or "").lower()
        or query in str(user.get("username") or "").lower()
    )


def _render_overview_tab(
    teams: list[dict[str, Any]],
    roles: list[dict[str, Any]],
    users: list[dict[str, Any]],
    users_by_id: dict[int, dict[str, Any]],
    team_members_by_id: dict[int, set[int]],
    permissions: list[dict[str, Any]],
) -> None:
    """Search bar + three side-by-side sections (Teams / Base Roles / Capabilities).

    The render engine is built from closures so all per-request state
    (search query, per-section sort key, the live element handles) stays
    local to this page request — nothing leaks across users.
    """
    base_roles, capability_roles = _ordered_roles_by_kind(roles)
    perms_for_dialog = list(permissions)

    # Shared, per-request state. ``ids`` is the matched user-id set while a
    # search is active, or ``None`` when the box is empty.
    search_state: dict[str, Any] = {"ids": None}
    sections: list[dict[str, Any]] = []

    def _apply_entry(entry: dict[str, Any], sort_key: str) -> bool:
        """Re-render one group's member list in place; return whether it's visible.

        Only the inner member box is cleared/rebuilt — the expansion
        wrapper survives, so a sort change keeps each group's open/closed
        state. During a search the whole sub-card is hidden unless it holds
        a matched user, and surviving cards are forced open.
        """
        sort_fn = _MEMBER_SORTS.get(sort_key, next(iter(_MEMBER_SORTS.values())))
        members = sorted(entry["members"], key=sort_fn)
        ids = search_state["ids"]
        if ids is None:
            visible = members
            entry["card"].set_visibility(True)
        else:
            visible = [m for m in members if int(m["id"]) in ids]
            entry["card"].set_visibility(bool(visible))
            if visible:
                entry["expansion"].set_value(True)
        entry["members_box"].clear()
        with entry["members_box"]:
            if visible:
                for member in visible:
                    entry["render_row"](member)
            elif ids is None:
                ui.label(entry["empty_msg"]).style("color: #555; font-size: 0.85rem;")
        return bool(visible)

    def _rerender(section: dict[str, Any]) -> None:
        any_visible = False
        for entry in section["entries"]:
            any_visible = _apply_entry(entry, section["sort_key"]) or any_visible
        if section["no_match"] is not None:
            searching = search_state["ids"] is not None
            section["no_match"].set_visibility(searching and not any_visible)

    def _rerender_all() -> None:
        for section in sections:
            _rerender(section)

    # ---- search bar -------------------------------------------------
    with ui.row().classes("w-full items-center gap-2"):
        search_input = (
            ui.input(placeholder="Search members by first name, last name or username…")
            .props("clearable dense outlined")
            .classes("flex-1")
        )
        with search_input.add_slot("prepend"):
            ui.icon("search")

    def _on_search(_e: Any = None) -> None:
        query = str(search_input.value or "").strip().lower()
        search_state["ids"] = (
            {int(u["id"]) for u in users if _user_matches(u, query)} if query else None
        )
        _rerender_all()

    # ``on_value_change`` fires on every keystroke *and* on the clearable
    # "✕", so emptying the box reliably restores all groups/members.
    search_input.on_value_change(_on_search)

    # ---- three sections --------------------------------------------
    with ui.row().classes("w-full gap-4 items-start").style("flex-wrap: wrap;"):
        _build_section(
            sections=sections,
            rerender=_rerender,
            title="Teams",
            icon=_ICON_TEAMS,
            description=(
                "Teams are named groups of users used to scope visibility — Tools and "
                "administrator-released Workflows can be restricted so only their team "
                "members can see them. Users can belong to multiple teams; the 'World' "
                "wildcard isn't listed here."
            ),
            new_button=("New team", "group_add", _open_new_team_dialog),
            groups=teams,
            kind="team",
            users=users,
            users_by_id=users_by_id,
            team_members_by_id=team_members_by_id,
        )
        _build_section(
            sections=sections,
            rerender=_rerender,
            title="Access Rights Groups - Base Roles",
            icon=_ICON_BASE,
            description=(
                "Base roles set a user's single privilege level (Super User, "
                "Administrator, Beta Tester, Regular User can't be deleted). Super "
                "users and Administrators add more Access Rights Groups. Tune "
                "permissions in the Permissions tab — all but the Super User can be "
                "edited in Permissions. A user must always be a member of a Group — if "
                "a member is deleted from a Group, by default the user gets "
                "automatically added to the Regular User Access Rights Group. If a user "
                "is added to a new Base Role group they are automatically removed from "
                "their old group."
            ),
            new_button=(
                "New base role",
                "add_moderator",
                lambda: _open_new_role_dialog(perms_for_dialog, kind="base"),
            ),
            groups=base_roles,
            kind="base",
            users=users,
            users_by_id=users_by_id,
            team_members_by_id=team_members_by_id,
        )
        _build_section(
            sections=sections,
            rerender=_rerender,
            title="Access Rights Groups - Capabilities",
            icon=_ICON_CAPS,
            description=(
                "Capabilities are fixed MATOFX functionalities — they can't be "
                "created or removed. Tune their permissions in the Permissions tab."
            ),
            new_button=None,
            groups=capability_roles,
            kind="capability",
            users=users,
            users_by_id=users_by_id,
            team_members_by_id=team_members_by_id,
        )


def _build_section(
    *,
    sections: list[dict[str, Any]],
    rerender: Callable[[dict[str, Any]], None],
    title: str,
    icon: str,
    description: str,
    new_button: tuple[str, str, Callable[[], None]] | None,
    groups: list[dict[str, Any]],
    kind: str,
    users: list[dict[str, Any]],
    users_by_id: dict[int, dict[str, Any]],
    team_members_by_id: dict[int, set[int]],
) -> None:
    """Build one titled, icon-headed section card with its group sub-cards.

    Registers its entries + sort state into ``sections`` so the global
    search re-renders them; wires the section's own *Sort members* select
    to re-render just this section.
    """
    section: dict[str, Any] = {
        "entries": [],
        "sort_key": next(iter(_MEMBER_SORTS)),
        "no_match": None,
    }
    sections.append(section)

    # Each section takes a third of a wide screen but wraps to its own row
    # when the window is too narrow (responsive, never clipped).
    with (
        ui.card().classes("gap-3").style("flex: 1 1 360px; min-width: 320px;"),
        ui.column().classes("w-full gap-3"),
    ):
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            ui.icon(icon).style("font-size: 1.4rem;")
            ui.label(title).style("font-size: 1.05rem; font-weight: 600;")
            ui.element("div").style("flex: 1;")
            if new_button is not None:
                label, btn_icon, handler = new_button
                ui.button(label, icon=btn_icon, on_click=handler).props("color=primary dense")

        ui.label(description).style("color: #888; font-size: 0.8rem;")

        sort_select = (
            ui.select(
                list(_MEMBER_SORTS.keys()),
                label="Sort members",
                value=section["sort_key"],
            )
            .classes("w-full")
            .props("dense")
        )

        if not groups:
            ui.label("Nothing here yet.").style("color: #555;")
            return

        section["no_match"] = ui.label("No matches in this section.").style(
            "color: #555; font-size: 0.85rem;"
        )
        section["no_match"].set_visibility(False)

        for group in groups:
            _build_group_card(
                section=section,
                group=group,
                kind=kind,
                users=users,
                users_by_id=users_by_id,
                team_members_by_id=team_members_by_id,
            )

        def _on_sort(_e: Any = None) -> None:
            section["sort_key"] = str(sort_select.value or next(iter(_MEMBER_SORTS)))
            rerender(section)

        sort_select.on("update:model-value", _on_sort)
        rerender(section)


def _members_for(
    group: dict[str, Any],
    kind: str,
    users: list[dict[str, Any]],
    users_by_id: dict[int, dict[str, Any]],
    team_members_by_id: dict[int, set[int]],
) -> list[dict[str, Any]]:
    """Resolve a group's members to full user dicts (unsorted)."""
    if kind == "team":
        ids = team_members_by_id.get(int(group["id"]), set())
        return [users_by_id[i] for i in ids if i in users_by_id]
    name = str(group["name"])
    if kind == "capability":
        return [u for u in users if name in (u.get("capability_roles") or [])]
    return [u for u in users if str(u.get("role") or "") == name]


def _build_group_card(
    *,
    section: dict[str, Any],
    group: dict[str, Any],
    kind: str,
    users: list[dict[str, Any]],
    users_by_id: dict[int, dict[str, Any]],
    team_members_by_id: dict[int, set[int]],
) -> None:
    """One expandable sub-card for a single team / base role / capability."""
    members = _members_for(group, kind, users, users_by_id, team_members_by_id)
    member_ids = {int(m["id"]) for m in members}
    is_built_in = bool(group.get("is_built_in"))

    if kind == "team":
        team_id = int(group["id"])
        heading = f"{group['name']} ({len(members)})"
        head_icon = "group"
    else:
        role_id = int(group["id"])
        role_name = str(group["name"])
        heading = f"{format_role_label(role_name)} ({len(members)})"
        head_icon = _base_role_icon(role_name) if kind == "base" else _ICON_CAPS

    # ---- per-member row renderer (depends on kind) ------------------
    def _edit_user_button(user_id: int) -> None:
        ui.button(
            "Edit user",
            icon="edit",
            on_click=lambda _e=None, _u=user_id: ui.navigate.to(f"/admin/users/{_u}/edit"),
        ).props("flat dense")

    def _render_row(member: dict[str, Any]) -> None:
        user_id = int(member["id"])
        with ui.row().classes("w-full items-center justify-between gap-2"):
            ui.label(_full_name_with_username(member)).style("font-size: 0.9rem;")
            with ui.row().classes("items-center gap-1 no-wrap"):
                _edit_user_button(user_id)
                if kind == "team":
                    ui.button(
                        "Remove",
                        icon="person_remove",
                        on_click=_make_remove_team_member(team_id, user_id),
                    ).props("flat dense color=negative")
                elif kind == "base" and role_name != _REGULAR_USER:
                    ui.button(
                        "Remove",
                        icon="person_remove",
                        on_click=_make_reassign_to_regular(user_id),
                    ).props("flat dense color=negative").tooltip("Move to Regular User")
                elif kind == "capability":
                    ui.button(
                        "Remove",
                        icon="person_remove",
                        on_click=_make_remove_capability_member(role_id, user_id),
                    ).props("flat dense color=negative").tooltip(
                        "Remove from this capability"
                    )

    # ---- the sub-card + expansion -----------------------------------
    with ui.card().classes("w-full").props("flat bordered") as card:
        with ui.expansion(heading, icon=head_icon).classes("w-full") as expansion:
            # Edit (rename / description): teams always; custom groups yes;
            # built-in base roles have locked names (edit their description
            # in the Permissions tab) so they get no Edit button here.
            if kind == "team":
                with ui.row().classes("w-full justify-end"):
                    ui.button(
                        "Edit",
                        icon="edit",
                        on_click=lambda _e=None, _t=group: _open_edit_team_dialog(_t),
                    ).props("flat dense")
            elif not is_built_in:
                with ui.row().classes("w-full justify-end"):
                    ui.button(
                        "Edit",
                        icon="edit",
                        on_click=lambda _e=None, _r=group: _open_edit_role_dialog(_r),
                    ).props("flat dense")

            if group.get("description"):
                ui.label(str(group["description"])).style(
                    "color: #888; font-size: 0.85rem; margin-bottom: 0.25rem;"
                )

            members_box = ui.column().classes("w-full gap-1")

            # Add-members control — always shown (even when everyone is
            # already a member), multi-select, available for every group.
            _render_add_control(
                kind=kind,
                group=group,
                users=users,
                member_ids=member_ids,
            )

    entry = {
        "card": card,
        "expansion": expansion,
        "members_box": members_box,
        "members": members,
        "render_row": _render_row,
        "empty_msg": ("No members yet." if kind == "team" else "No users in this group yet."),
    }
    section["entries"].append(entry)


def _render_add_control(
    *,
    kind: str,
    group: dict[str, Any],
    users: list[dict[str, Any]],
    member_ids: set[int],
) -> None:
    """Multi-select 'add many, Add once' control, shown for every group.

    For a base role the candidate set is *every other user* (each is moved
    here from their current base role); for teams and capabilities it's the
    users not already members. The widget is always rendered — when there
    are no candidates it shows disabled with a short note, so the affordance
    is visibly present even when everyone is already assigned.
    """
    if kind == "base":
        name = str(group["name"])
        candidates = {
            int(u["id"]): _full_name_with_username(u)
            for u in users
            if str(u.get("role") or "") != name
        }
    else:
        candidates = {
            int(u["id"]): _full_name_with_username(u)
            for u in users
            if int(u["id"]) not in member_ids
        }

    with ui.row().classes("w-full items-center gap-2 mt-2"):
        pick = (
            ui.select(
                options=candidates,
                label="Add members",
                multiple=True,
                with_input=True,
            )
            .classes("flex-1")
            .props("dense use-chips")
        )
        if not candidates:
            pick.props("disable")
            ui.label("Everyone is already a member.").style("color: #777; font-size: 0.78rem;")

        async def _add(_e: Any = None) -> None:
            user_ids = [int(v) for v in (pick.value or [])]
            if not user_ids:
                return
            client = ApiClient(get_app(), token=current_token())
            try:
                if kind == "team":
                    await client.add_team_members(int(group["id"]), user_ids=user_ids)
                elif kind == "capability":
                    await client.add_role_members(int(group["id"]), user_ids=user_ids)
                else:  # base role — reassign each user's single base role
                    for uid in user_ids:
                        await client.set_user_role(uid, role=str(group["name"]))
            except ApiError as exc:
                ui.notify(f"Add failed: {exc.detail}", type="negative")
                return
            added = len(user_ids)
            ui.notify(
                "Member added." if added == 1 else f"{added} members added.",
                type="positive",
            )
            ui.navigate.to("/admin/teams")

        ui.button("Add", on_click=_add).props("color=primary dense")


def _ordered_roles_by_kind(
    roles: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split roles into (base, capability), each in display order.

    Base roles follow the canonical privilege order (Super User,
    Administrator, Beta Tester, Regular User, then custom A→Z);
    capabilities are alphabetical.
    """
    base = [r for r in roles if str(r.get("kind") or "base") == "base"]
    caps = [r for r in roles if str(r.get("kind") or "") == "capability"]
    base_order = order_base_role_names([str(r["name"]) for r in base])
    base_sorted = sorted(base, key=lambda r: base_order.index(str(r["name"])))
    caps_sorted = sorted(caps, key=lambda r: format_role_label(str(r["name"])).lower())
    return base_sorted, caps_sorted


# ---------- Tab 2: Permission editor ---------------------------------


def _render_permissions_tab(
    roles: list[dict[str, Any]],
    permissions: list[dict[str, Any]],
) -> None:
    """Choose a group; edit its description + the permission set for its kind."""
    if not roles:
        ui.label("No groups to edit.").style("color: #555;")
        return
    if not permissions:
        ui.label("The permission catalog came back empty — that shouldn't happen.").style(
            "color: #c0392b;"
        )
        return

    # Group the picker: base roles first (canonical order), then
    # capabilities (alpha), each option tagged with its kind so the two
    # tiers read as separate groups in the flat dropdown.
    base_roles, capability_roles = _ordered_roles_by_kind(roles)
    role_options: dict[int, str] = {}
    for r in base_roles:
        role_options[int(r["id"])] = f"Base · {format_role_label(str(r['name']))}"
    for r in capability_roles:
        role_options[int(r["id"])] = f"Capability · {format_role_label(str(r['name']))}"
    role_by_id = {int(r["id"]): r for r in roles}

    # Selector lives in its own titled card, body below — same convention
    # everywhere a selector drives a dependent body slot.
    with ui.card().classes("w-full"):
        ui.label("Choose a group to edit their permissions").style(
            "font-size: 1.1rem; font-weight: 600;"
        )
        picker = (
            ui.select(role_options, label="Choose the group you want to edit")
            .classes("w-96")
            .props("dense")
        )

    body = ui.column().classes("w-full gap-3")

    def _on_pick(_e: Any = None) -> None:
        body.clear()
        if picker.value is None:
            return
        role = role_by_id[int(picker.value)]
        with body:
            _render_role_editor(role, permissions)

    picker.on("update:model-value", _on_pick)

    with body:
        ui.label("Choose a group above to edit its description and permissions.").style(
            "color: #888;"
        )


def _render_perm_card(
    title: str,
    perms: list[dict[str, Any]],
    starting_perms: set[str],
    *,
    active: bool,
    show_ticked_when_disabled: bool,
    collect_into: dict[str, ui.checkbox] | None,
    section_order: tuple[str, ...] | None = None,
) -> None:
    """Render one permission card, grouped by area.

    ``active`` → the card's checkboxes are editable and harvested into
    ``collect_into`` on save. The super-user view is rendered disabled but
    fully ticked (``show_ticked_when_disabled``) to show its always-on set.
    ``section_order`` lays the sections out in that order (any extras follow
    alphabetically); without it sections are alphabetical.
    """
    with ui.card().classes("w-full"):
        ui.label(title).style("font-weight: 600;")
        if not perms:
            ui.label("No permissions in this group.").style("color: #888; font-size: 0.8rem;")
            return

        # Group permissions by their section (the prefix before the first
        # dot — e.g. ``projects.see_all`` → ``projects``).
        grouped: dict[str, list[dict[str, Any]]] = {}
        for p in perms:
            grouped.setdefault(str(p["slug"]).partition(".")[0], []).append(p)

        if section_order is None:
            ordered_sections = sorted(grouped)
        else:
            ordered_sections = [s for s in section_order if s in grouped]
            ordered_sections += [s for s in sorted(grouped) if s not in section_order]

        # CSS multi-column layout: up to 3 columns ≥18em wide; sections
        # don't split across columns.
        with ui.element("div").style(
            "columns: 18em 3; column-gap: 1.5rem; margin-top: 0.5rem; width: 100%;"
        ):
            for section in ordered_sections:
                with ui.element("div").style(
                    "break-inside: avoid; -webkit-column-break-inside: avoid; "
                    "page-break-inside: avoid; margin-bottom: 0.75rem;"
                ):
                    ui.label(_section_label(section)).style(
                        "font-weight: 600; margin-bottom: 0.25rem;"
                    )
                    for perm in sorted(grouped[section], key=lambda x: str(x["slug"])):
                        slug = str(perm["slug"])
                        value = (
                            (slug in starting_perms)
                            if (active or show_ticked_when_disabled)
                            else False
                        )
                        cb = ui.checkbox(str(perm.get("label") or slug), value=value)
                        if perm.get("description"):
                            ui.label(str(perm["description"])).style(
                                "color: #888; font-size: 0.8rem; margin-left: 1.6rem;"
                            )
                        if not active:
                            cb.disable()
                        if collect_into is not None:
                            collect_into[slug] = cb


# Base-role permission sections bucketed into sidebar groups, in sidebar
# top-to-bottom order, so the editor reads like the rail. Every base-tier
# permission section maps to exactly one group; an unmapped section (a
# future addition) falls through to an "Other" card so nothing is hidden.
_PERM_SECTION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Dashboard", ("projects", "custom_workflows", "jobs")),
    # Order chosen so the CSS balanced columns read well: with four small
    # one-line sections the first column takes the first two, so Cohorts +
    # Parametric Studies sit together in column 1.
    ("Studies", ("cohorts", "parametric_studies", "optimisation", "models")),
    ("Libraries", ("library", "materials", "database")),
    ("Governance", ("vv", "audit")),
    (
        "Admin",
        (
            "tools",
            "users",
            "roles",
            "teams",
            "workflows",
            "runtime_settings",
            "aos_hosts",
            "hpc_clusters",
            "storage_locations",
            "python_envs",
            "live_jobs",
            "lineage",
            "usage",
            "tutorials",
            "migrate",
            "notifications",
        ),
    ),
)


def _render_base_perm_cards(
    base_perms: list[dict[str, Any]],
    starting_perms: set[str],
    *,
    active: bool,
    show_ticked_when_disabled: bool,
    collect_into: dict[str, ui.checkbox] | None,
) -> None:
    """Render base-role permissions as one card per sidebar group (in rail order).

    Each section (slug prefix) is placed in its sidebar group's card so the
    editor mirrors the navigation; unmapped sections fall into an ``Other``
    card. Reuses :func:`_render_perm_card` per group, so the checkbox
    harvesting / super-user read-only behaviour is identical.
    """
    by_section: dict[str, list[dict[str, Any]]] = {}
    for perm in base_perms:
        by_section.setdefault(str(perm["slug"]).partition(".")[0], []).append(perm)

    placed: set[str] = set()
    for group_title, sections in _PERM_SECTION_GROUPS:
        group_perms: list[dict[str, Any]] = []
        for section in sections:
            if section in by_section:
                group_perms.extend(by_section[section])
                placed.add(section)
        if group_perms:
            _render_perm_card(
                group_title,
                group_perms,
                starting_perms,
                active=active,
                show_ticked_when_disabled=show_ticked_when_disabled,
                collect_into=collect_into,
                section_order=sections,
            )

    leftover: list[dict[str, Any]] = []
    for section, perms in by_section.items():
        if section not in placed:
            leftover.extend(perms)
    if leftover:
        _render_perm_card(
            "Other",
            leftover,
            starting_perms,
            active=active,
            show_ticked_when_disabled=show_ticked_when_disabled,
            collect_into=collect_into,
        )


def _render_role_editor(role: dict[str, Any], permissions: list[dict[str, Any]]) -> None:
    """Edit one group: description + the permission card for its kind only.

    A base role shows only the base-tier permissions; a capability shows
    only the capability-tier (``*.curate``) ones — the tiers grant strictly
    non-overlapping sets and the server enforces the same split.
    ``super_user`` is fully read-only.
    """
    role_id = int(role["id"])
    role_name = str(role["name"])
    role_kind = str(role.get("kind") or "base")
    is_super_user = role_name == "super_user"
    is_capability = role_kind == "capability"
    starting_perms = set(role.get("permissions") or [])

    if is_capability:
        relevant_perms = [p for p in permissions if bool(p.get("is_capability"))]
    else:
        relevant_perms = [p for p in permissions if not bool(p.get("is_capability"))]

    with ui.card().classes("w-full"):
        kind_label = "Capability" if is_capability else "Base role"
        ui.label(f"{format_role_label(role_name)}  ·  {kind_label}").style(
            "font-size: 1.2rem; font-weight: 600;"
        )
        if is_super_user:
            ui.label(
                "The Super User group is the install's break-glass admin and "
                "always holds every permission. Its permission set can't be "
                "edited; the description below can."
            ).style("color: #b58900; font-size: 0.85rem;")

        description = (
            ui.textarea("Description", value=str(role.get("description") or ""))
            .classes("w-full")
            .props("dense outlined")
        )

        checkboxes: dict[str, ui.checkbox] = {}
        active = not is_super_user
        # Capabilities are a short, flat list (one card); base roles are
        # bucketed into sidebar-group cards so they're easy to scan.
        if is_capability:
            _render_perm_card(
                "Capability permissions",
                relevant_perms,
                starting_perms,
                active=active,
                show_ticked_when_disabled=is_super_user,
                collect_into=checkboxes if active else None,
            )
        else:
            _render_base_perm_cards(
                relevant_perms,
                starting_perms,
                active=active,
                show_ticked_when_disabled=is_super_user,
                collect_into=checkboxes if active else None,
            )

        message = ui.label("").style("color: #c0392b;")

        async def _save() -> None:
            message.text = ""
            client = ApiClient(get_app(), token=current_token())
            new_description = str(description.value or "")
            try:
                if new_description != str(role.get("description") or ""):
                    await client.update_role_description(role_id, description=new_description)
                if not is_super_user:
                    selected = sorted(slug for slug, cb in checkboxes.items() if cb.value)
                    if set(selected) != starting_perms:
                        await client.set_role_permissions(role_id, permissions=selected)
            except ApiError as exc:
                message.text = exc.detail
                return
            ui.notify("Saved.", type="positive")
            ui.navigate.to("/admin/teams")

        with ui.row().classes("w-full justify-end mt-4 gap-2"):
            ui.button("Cancel", on_click=lambda: ui.navigate.to("/admin/teams")).props("flat")
            ui.button("Save", on_click=_save).props("color=primary")


# ---------- Dialogs --------------------------------------------------


def _open_new_team_dialog() -> None:
    """Modal to create a new team."""
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label("New team").style("font-size: 1.2rem; font-weight: 600;")
        name = ui.input("Team name *").props("autofocus dense").classes("w-full")
        description = ui.input("Description (optional)").classes("w-full").props("dense")
        message = ui.label("").style("color: #c0392b;")

        async def _create() -> None:
            if not name.value or not str(name.value).strip():
                message.text = "Team name is required."
                return
            client = ApiClient(get_app(), token=current_token())
            try:
                await client.create_team(
                    name=str(name.value).strip(),
                    description=str(description.value or "").strip(),
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Team created.", type="positive")
            ui.navigate.to("/admin/teams")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create", on_click=_create).props("color=primary")
    dialog.open()


def _open_edit_team_dialog(team: dict[str, Any]) -> None:
    """Modal to rename a team / fix its description. Pre-filled from ``team``."""
    team_id = int(team["id"])
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label("Edit team").style("font-size: 1.2rem; font-weight: 600;")
        name = (
            ui.input("Team name *", value=str(team.get("name") or ""))
            .props("autofocus dense")
            .classes("w-full")
        )
        description = (
            ui.input("Description (optional)", value=str(team.get("description") or ""))
            .classes("w-full")
            .props("dense")
        )
        message = ui.label("").style("color: #c0392b;")

        async def _save() -> None:
            if not name.value or not str(name.value).strip():
                message.text = "Team name is required."
                return
            client = ApiClient(get_app(), token=current_token())
            try:
                await client.update_team(
                    team_id,
                    name=str(name.value).strip(),
                    description=str(description.value or "").strip(),
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Team updated.", type="positive")
            ui.navigate.to("/admin/teams")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=_save).props("color=primary")
    dialog.open()


def _open_edit_role_dialog(role: dict[str, Any]) -> None:
    """Modal to rename a custom group / fix its description.

    Name + description only — permissions are tuned in the Permissions
    tab. Calls ``rename`` and/or ``update-description`` depending on what
    actually changed, so an unchanged field issues no round-trip.
    """
    role_id = int(role["id"])
    original_name = str(role.get("name") or "")
    original_description = str(role.get("description") or "")
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label("Edit access-rights group").style("font-size: 1.2rem; font-weight: 600;")
        name = (
            ui.input("Group name *", value=original_name).props("autofocus dense").classes("w-full")
        )
        description = (
            ui.textarea("Description (optional)", value=original_description)
            .classes("w-full")
            .props("dense outlined")
        )
        message = ui.label("").style("color: #c0392b;")

        async def _save() -> None:
            message.text = ""
            new_name = str(name.value or "").strip()
            if not new_name:
                message.text = "Group name is required."
                return
            client = ApiClient(get_app(), token=current_token())
            new_description = str(description.value or "").strip()
            try:
                if new_name != original_name:
                    await client.rename_role(role_id, name=new_name)
                if new_description != original_description:
                    await client.update_role_description(role_id, description=new_description)
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Group updated.", type="positive")
            ui.navigate.to("/admin/teams")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=_save).props("color=primary")
    dialog.open()


def _open_new_role_dialog(permissions_cache: list[dict[str, Any]], *, kind: str) -> None:
    """Modal to create a new access-rights group of a fixed ``kind``.

    Opened from a section header, so the kind (``base`` or ``capability``)
    is fixed by which section the operator clicked — no type toggle. The
    permission list is filtered to that kind (base roles see only base-tier
    permissions, capabilities only the ``*.curate`` ones); the server
    enforces the same split. Permissions are tunable later in the
    Permissions tab.
    """
    is_capability = kind == "capability"
    title = "New capability" if is_capability else "New base role"
    with ui.dialog() as dialog, ui.card().style("min-width: 520px; max-height: 80vh;"):
        ui.label(title).style("font-size: 1.2rem; font-weight: 600;")
        ui.label(
            "Pick a name (letters / numbers / underscores), describe what it's for, "
            "and tick the permissions it should hold. You can tune the permissions "
            "later in the Permissions tab."
        ).style("color: #888; font-size: 0.85rem;")
        ui.label(
            "Capabilities carry only curator permissions and stack on top of a user's base role."
            if is_capability
            else "Base roles set a user's single privilege level and carry every "
            "permission except the curator ones."
        ).style("color: #888; font-size: 0.8rem;")
        name = (
            ui.input("Group name *", placeholder="e.g. finance_team_lead")
            .props("autofocus dense")
            .classes("w-full")
        )
        description = (
            ui.textarea("Description (optional)").classes("w-full").props("dense outlined")
        )

        perm_container = ui.column().classes("w-full gap-1 max-h-96 overflow-auto")
        checkboxes: dict[str, ui.checkbox] = {}
        perms_holder: list[dict[str, Any]] = []

        def _repopulate() -> None:
            perm_container.clear()
            checkboxes.clear()
            relevant = [p for p in perms_holder if bool(p.get("is_capability")) == is_capability]
            with perm_container:
                if not relevant:
                    ui.label("No permissions available for this group type.").style(
                        "color: #888; font-size: 0.85rem;"
                    )
                    return
                grouped: dict[str, list[dict[str, Any]]] = {}
                for p in relevant:
                    grouped.setdefault(str(p["slug"]).partition(".")[0], []).append(p)
                for section in sorted(grouped):
                    ui.label(_section_label(section)).style("font-weight: 600; margin-top: 0.5rem;")
                    for perm in sorted(grouped[section], key=lambda x: str(x["slug"])):
                        slug = str(perm["slug"])
                        checkboxes[slug] = ui.checkbox(str(perm.get("label") or slug), value=False)

        async def _populate_permissions() -> None:
            client = ApiClient(get_app(), token=current_token())
            try:
                perms = permissions_cache if permissions_cache else await client.list_permissions()
            except ApiError as exc:
                with perm_container:
                    ui.label(f"Couldn't load permissions: {exc.detail}").style("color: #c0392b;")
                return
            perms_holder.extend(perms)
            _repopulate()

        ui.timer(0.0, _populate_permissions, once=True)
        message = ui.label("").style("color: #c0392b;")

        async def _create() -> None:
            message.text = ""
            raw_name = str(name.value or "").strip()
            if not raw_name:
                message.text = "Group name is required."
                return
            client = ApiClient(get_app(), token=current_token())
            selected = sorted(slug for slug, cb in checkboxes.items() if cb.value)
            try:
                await client.create_role(
                    name=raw_name,
                    description=str(description.value or "").strip(),
                    permissions=selected,
                    kind=kind,
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Group created.", type="positive")
            ui.navigate.to("/admin/teams")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create", on_click=_create).props("color=primary")
    dialog.open()


# ---------- Helpers --------------------------------------------------


def _full_name_with_username(user: dict[str, Any]) -> str:
    """Render ``Alice Smith (alice)`` or just ``alice`` if no full name set."""
    full = str(user.get("full_name") or "").strip()
    username = str(user.get("username") or "")
    return f"{full} ({username})" if full else username


def _make_remove_team_member(team_id: int, user_id: int) -> Any:
    """Closure factory: returns the ``on_click`` for one team's Remove button."""

    async def _remove(_e: Any = None) -> None:
        client = ApiClient(get_app(), token=current_token())
        try:
            await client.remove_team_member(team_id, user_id=user_id)
        except ApiError as exc:
            ui.notify(f"Remove failed: {exc.detail}", type="negative")
            return
        ui.notify("Member removed.", type="positive")
        ui.navigate.to("/admin/teams")

    return _remove


def _make_remove_capability_member(role_id: int, user_id: int) -> Any:
    """Closure factory: returns the ``on_click`` for a capability's Remove button.

    A capability is optional and stacks on top of a user's base role, so
    removing them simply revokes that one capability grant (idempotent
    server-side) — unlike a base role, no reassignment is needed.
    """

    async def _remove(_e: Any = None) -> None:
        client = ApiClient(get_app(), token=current_token())
        try:
            await client.remove_role_member(role_id, user_id=user_id)
        except ApiError as exc:
            ui.notify(f"Remove failed: {exc.detail}", type="negative")
            return
        ui.notify("Removed from capability.", type="positive")
        ui.navigate.to("/admin/teams")

    return _remove


def _make_reassign_to_regular(user_id: int) -> Any:
    """Closure factory: Remove from a base role → move the user to Regular User.

    A user must always hold exactly one base role, so removing them from a
    base role moves them to ``regular_user`` rather than leaving them with
    none. The super-user invariants still apply server-side (you can't move
    the last super-user off the role).
    """

    async def _remove(_e: Any = None) -> None:
        client = ApiClient(get_app(), token=current_token())
        try:
            await client.set_user_role(user_id, role=_REGULAR_USER)
        except ApiError as exc:
            ui.notify(f"Couldn't move user: {exc.detail}", type="negative")
            return
        ui.notify("Moved to Regular User.", type="positive")
        ui.navigate.to("/admin/teams")

    return _remove
