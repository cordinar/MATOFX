"""Home page (``/ui/home``) — landing screen after login.

Faithful port of the legacy 5-column dashboard, simplified into three
visible columns (legacy's two 1%-wide spacers become Quasar gaps):

- **Left rail** — stacked action cards: ``Create``, ``Job Search``,
  ``Tutorials``. (Legacy also has a ``Simulation Approvals`` card for
  approver users; v2 has no approval workflow yet, so that card stays
  out until Phase 2.)
- **Centre column** — ``Job Status`` table (every job the user can
  see, newest first) and ``Projects`` table. Clicking a row navigates
  to the matching detail page.
- **Right column** — two stacked Quick Access cards: ``Workflow
  Quick Access`` (top) and ``Single Tool Quick Access`` (bottom).
  Each card lists one launch row per visible workflow or installed
  tool with the legacy two-button affordance ("Launch in existing
  project" / "Launch in new project"). When the admin has turned
  the ``favourites_enabled`` general setting on, an inline star
  next to each item's name lets the user pin it; favourites float
  to the top of their section (alphabetical within the favourited
  group, then alphabetical within the rest).

Every data fetch goes through the API — no shortcuts — so a permission
denial on one slice (say, listing scenarios in a project the user
can't see) doesn't blow up the whole page.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from dataclasses import dataclass, field
from typing import Any

from nicegui import ui

from matofx.ui import metadata_form, share_form
from matofx.ui._features import (
    optimisation_framework_enabled,
    parametric_studies_enabled,
    surrogate_family_enabled,
)
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import (
    icon_chip,
    render_global_search,
    render_shell,
    require_logged_in,
)
from matofx.ui.pages.dashboard import _open_new_project_dialog
from matofx.ui.scenario_naming import next_scenario_name
from matofx.ui.state import (
    current_first_name,
    current_quick_access_filter,
    current_token,
    current_username,
    store_first_name,
    store_quick_access_filter,
)
from matofx.ui.workflow_launch import open_launch_in_existing_project_dialog

_RECENT_JOB_LIMIT = 50
"""How many of the most-recent jobs the home table holds. Bigger numbers
mean longer tail history but also more API roundtrips at page load."""

_STATE_BADGE: dict[str, str] = {
    "created": "color=grey",
    "queued": "color=info",
    "running": "color=warning",
    "complete": "color=positive",
    "failed": "color=negative",
}


@ui.page("/home")
async def home_page() -> None:
    """Render the redesigned landing page (home-redesign §7).

    Layout, top→bottom: a greeting + a live summary line; a row of
    action tiles (Create · Studies · Job Search · My Account); a Quick
    Access card (favourited tools + workflows) beside an approver-only
    Approvals card; then the four My-Work tables (My Projects, My Jobs,
    and — when their feature flag is on — Parametric Studies and
    Optimisations). Every datum comes from an API call, filtered
    server-side by the same ACL / role / feature rules the rest of the
    app enforces.
    """
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())

    greeting_name = await _resolve_greeting_name(client)
    tutorials = await _load_active_tutorials(client)
    favourites = await _load_favourites(client)
    approvals_summary = await _load_approvals_summary(client)
    summary = await _load_home_summary(client)

    with ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;"):
        _render_greeting(greeting_name, summary)
        render_global_search()
        _render_action_tiles(client)
        with ui.row().classes("w-full items-stretch gap-4 no-wrap"):
            with ui.column().classes("gap-4").style("flex: 1 1 0; min-width: 0;"):
                await _render_quick_access_card(client, tutorials, favourites)
            if approvals_summary.get("can_approve"):
                with ui.column().classes("gap-4").style("flex: 1 1 0; min-width: 0;"):
                    await _render_approvals_card(client, approvals_summary)

        _render_section_label("My Work")
        with ui.element("div").classes("w-full").style(
            "display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;"
        ):
            await _render_my_projects_table(client)
            await _render_my_jobs_table(client)
            if parametric_studies_enabled():
                await _render_parametric_studies_table(client)
            if optimisation_framework_enabled():
                await _render_optimisations_table(client)


async def _load_home_summary(client: ApiClient) -> dict[str, Any]:
    """Fetch the header summary counts, degrading to empty on error."""
    try:
        return await client.home_summary()
    except Exception:  # noqa: BLE001 — the greeting must render regardless
        return {}


def _render_greeting(name: str, summary: dict[str, Any]) -> None:
    """Greeting + the live summary line (home-redesign §4).

    Each summary clause is shown only when it's relevant — its count is
    > 0 and (for the feature-gated ones) the feature is enabled; the
    approvals clause additionally only for approvers. With nothing in
    flight the line falls back to a neutral subtitle.
    """
    ui.label(f"{_time_of_day_greeting()}, {name}.").style(
        "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
    )
    clauses: list[str] = []
    active_jobs = int(summary.get("active_jobs") or 0)
    running_opt = int(summary.get("running_optimisations") or 0)
    running_studies = int(summary.get("running_parametric_studies") or 0)
    pending = int(summary.get("pending_approvals") or 0)
    if active_jobs:
        clauses.append(f"{active_jobs} job{'s' if active_jobs != 1 else ''} active")
    if optimisation_framework_enabled() and running_opt:
        clauses.append(f"{running_opt} optimisation{'s' if running_opt != 1 else ''} running")
    if parametric_studies_enabled() and running_studies:
        clauses.append(
            f"{running_studies} parametric stud{'ies' if running_studies != 1 else 'y'} running"
        )
    if summary.get("can_approve") and pending:
        clauses.append(f"{pending} approval{'s' if pending != 1 else ''} waiting")
    import datetime as _dt  # noqa: PLC0415 — cheap, narrow scope

    now = _dt.datetime.now()
    # Cross-platform "Tuesday, 16 June 2026" (no leading zero on the day).
    today = f"{now.strftime('%A')}, {now.day} {now.strftime('%B %Y')}"
    tail = " · ".join(clauses) if clauses else "Submit, track, and compare engineering jobs."
    ui.label(f"{today} — {tail}").style(
        "color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;"
    )


def _time_of_day_greeting() -> str:
    """A 'Good morning/afternoon/evening' greeting from the local hour."""
    import datetime as _dt  # noqa: PLC0415 — cheap, narrow scope

    hour = _dt.datetime.now().hour
    if hour < 12:  # noqa: PLR2004
        return "Good morning"
    if hour < 18:  # noqa: PLR2004
        return "Good afternoon"
    return "Good evening"


def _render_section_label(text: str) -> None:
    """A small uppercase section divider with a trailing rule (mockup)."""
    with ui.row().classes("items-center w-full no-wrap").style("gap: 0.7rem; margin-top: 0.3rem;"):
        ui.label(text).style(
            "font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.09em; "
            "font-weight: 700; color: var(--matofx-muted-text);"
        )
        ui.element("div").style(
            "flex: 1 1 0; height: 1px; background: var(--matofx-card-border);"
        )


# ---------- action tiles -----------------------------------------------------


def _render_action_tiles(client: ApiClient) -> None:
    """The row of action tiles: Create · Studies · Job Search · My Account (§7.1–7.4).

    Each tile is a card; the dropdown tiles open a menu on click, the
    Job Search tile navigates directly. Studies-menu items are absent
    when their feature flag is off (§7.2). All handlers wire to the real
    create dialogs / routes — nothing is a placeholder.
    """
    with ui.row().classes("w-full items-stretch gap-4 no-wrap").style("overflow: visible;"):
        _render_tile(
            "add",
            "Create",
            "Project · Scenario · Job · Workflow",
            primary=True,
            menu=[
                ("Project", "folder", lambda: _open_new_project(client)),
                ("Scenario", "tab", lambda: _open_new_scenario_chooser(client)),
                ("Job", "assignment", lambda: _open_job_chooser(client)),
                ("Custom Workflow", "account_tree", lambda: ui.navigate.to("/my-workflows")),
            ],
        )
        studies_menu: list[tuple[str, str, Any]] = [
            ("Compare Jobs", "compare", lambda: ui.navigate.to("/compare")),
        ]
        if surrogate_family_enabled():
            studies_menu.append(
                ("Job Cohorts", "dataset_linked", lambda: ui.navigate.to("/cohorts"))
            )
        if parametric_studies_enabled():
            studies_menu.append(
                ("Parametric Studies", "science", lambda: ui.navigate.to("/studies"))
            )
        if optimisation_framework_enabled():
            studies_menu.append(("Optimisation", "tune", lambda: ui.navigate.to("/optimisation")))
        if surrogate_family_enabled():
            studies_menu.append(
                ("Surrogate Models", "model_training", lambda: ui.navigate.to("/models"))
            )
        _render_tile(
            "bar_chart",
            "Studies",
            "Explore & launch study types",
            menu=studies_menu,
        )
        _render_tile(
            "search",
            "Job Search",
            "Find runs by tool & inputs",
            on_click=lambda: ui.navigate.to("/job-search"),
        )
        _render_tile(
            "account_circle",
            "My Account",
            "Profile & preferences",
            menu=[
                ("My Account Settings", "settings", lambda: ui.navigate.to("/account")),
                ("Log out", "logout", _logout_from_home),
            ],
        )


def _render_tile(
    icon: str,
    title: str,
    subtitle: str,
    *,
    primary: bool = False,
    on_click: Any = None,
    menu: list[tuple[str, str, Any]] | None = None,
) -> None:
    """One action tile — a card with an icon, title, subtitle and chevron.

    A ``menu`` makes it a **CSS-hover dropdown** (opens on hover, closes
    when the cursor leaves — no shadow, matching the mockup); an
    ``on_click`` makes it a direct-navigation tile. ``primary`` paints it
    in the accent colour (the Create tile).
    """
    bg = "var(--matofx-accent)" if primary else "var(--matofx-card-bg)"
    fg = "#ffffff" if primary else "var(--matofx-body-text)"
    sub_fg = "rgba(255,255,255,0.85)" if primary else "var(--matofx-muted-text)"
    icon_bg = "rgba(255,255,255,0.18)" if primary else "rgba(64,161,164,0.14)"
    icon_fg = "#ffffff" if primary else "var(--matofx-accent)"
    card = (
        ui.card()
        .classes("items-center no-wrap cursor-pointer matofx-tile")
        .style(
            f"flex: 1 1 0; min-width: 0; flex-direction: row; gap: 0.8rem; "
            f"padding: 0.9rem 1rem; background-color: {bg};"
        )
    )
    if on_click is not None:
        card.on("click", on_click)
    with card:
        icon_chip(icon, size=38, radius=10, icon_size="1.3rem", bg=icon_bg, fg=icon_fg)
        with ui.column().classes("gap-0").style("flex: 1 1 0; min-width: 0;"):
            ui.label(title).style(f"font-size: 0.95rem; font-weight: 650; color: {fg};")
            ui.label(subtitle).style(
                f"font-size: 0.72rem; color: {sub_fg}; white-space: nowrap; "
                f"overflow: hidden; text-overflow: ellipsis;"
            )
        if menu is not None:
            ui.icon("expand_more").style(f"color: {sub_fg}; flex: none;")
            # CSS-hover dropdown: a positioned panel inside the tile that
            # ``.matofx-tile:hover`` reveals and a mouse-leave hides.
            with ui.element("div").classes("matofx-tilemenu"):
                for label, item_icon, handler in menu:
                    with ui.element("div").classes("matofx-tileitem").on("click", handler):
                        ui.icon(item_icon).style(
                            "color: var(--matofx-muted-text); font-size: 1rem;"
                        )
                        ui.label(label)
        elif on_click is not None:
            ui.icon("chevron_right").style(f"color: {sub_fg}; flex: none;")


async def _logout_from_home() -> None:
    """Revoke the session and return to /login (My Account → Log out, §7.4)."""
    from matofx.ui.state import clear_login  # noqa: PLC0415

    token = current_token()
    if token:
        client = ApiClient(get_app(), token=token)
        # Still drop local state even if the revoke fails (network blip,
        # already-expired token) — the session is reaped by the sweeper.
        with contextlib.suppress(Exception):
            await client.logout()
    clear_login()
    ui.navigate.to("/login")


# ---------- Quick Access (favourites) ----------------------------------------


async def _render_quick_access_card(
    client: ApiClient,
    tutorials: _TutorialBuckets,
    favourites: _FavouritesState,
) -> None:
    """Quick Access card — every visible tool + workflow, favourites on top.

    Shows all tools and workflows the user can see (not only favourites);
    starred items float to the top, and the list scrolls. A split pill
    (Tools | Workflows | All) filters by kind and is remembered per user
    (server-side prefs). Each row carries: name + a favourite star, a
    Tutorials button (only when active tutorials exist), and a Launch
    hover-menu (in existing / in new project).
    """
    with ui.card().classes("w-full").style("overflow: hidden;"), ui.column().classes(
        "w-full gap-2"
    ).style("min-width: 0;"):
        seg_buttons: dict[str, Any] = {}
        with ui.row().classes("w-full items-center no-wrap").style("gap: 0.7rem;"):
            ui.icon("star").style("color: var(--matofx-accent);")
            ui.label("Quick Access").style("font-size: 0.95rem; font-weight: 650;")
            # Segmented filter pill, right next to the title (mockup .qaseg).
            current_filter = current_quick_access_filter()
            with ui.element("div").classes("matofx-qaseg"):
                for kind, kind_label in (("tool", "Tools"), ("workflow", "Workflows"), ("all", "All")):
                    btn = ui.element("div").classes("seg-btn")
                    if kind == current_filter:
                        btn.classes(add="on")
                    with btn:
                        ui.label(kind_label)
                    btn.on("click", lambda _e=None, _k=kind: _on_filter(_k))
                    seg_buttons[kind] = btn

        try:
            tools = await client.list_tools()
        except ApiError:
            tools = []
        try:
            workflows = await client.list_visible_workflows()
        except ApiError:
            workflows = []

        # Each tool/workflow that has an uploaded icon shows it; the rest
        # fall back to a generic glyph. Fetched concurrently in one pass.
        tool_icons = await _gather_icon_data_urls(tools, fetch=client.get_tool_icon_bytes)
        wf_icons = await _gather_icon_data_urls(workflows, fetch=client.get_workflow_icon_bytes)

        # Build a combined, sorted list: favourites first, then A→Z by
        # label, tools and workflows interleaved by that order.
        items: list[dict[str, Any]] = []
        for tool in tools:
            alias = str(tool.get("alias") or "")
            items.append(
                {
                    "kind": "tool",
                    "item": tool,
                    "alias": alias,
                    "label": _tool_display_label(tool),
                    "is_favourite": alias in favourites.tools,
                    "tutorials": tutorials.by_tool.get(alias, []),
                    "icon_url": tool_icons.get(alias),
                }
            )
        for wf in workflows:
            alias = str(wf.get("alias") or "")
            items.append(
                {
                    "kind": "workflow",
                    "item": wf,
                    "alias": alias,
                    "label": _workflow_display_label(wf),
                    "is_favourite": alias in favourites.workflows,
                    "tutorials": tutorials.by_workflow.get(alias, []),
                    "icon_url": wf_icons.get(alias),
                }
            )
        items.sort(key=lambda it: (0 if it["is_favourite"] else 1, str(it["label"]).lower()))

        # Scrollable list (favourites on top); ~4 rows visible, rest scroll.
        list_box = (
            ui.element("div")
            .classes("w-full matofx-scroll")
            .style("max-height: 320px; overflow-y: auto;")
        )
        empty = ui.label(
            "No tools or workflows are available to you yet."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem; padding: 0.5rem 0;")

        rows: list[dict[str, Any]] = []

        def _resort() -> None:
            # Re-order so favourites float to the top immediately on a
            # star toggle (no refresh needed), alphabetical within group.
            ordered = sorted(
                rows,
                key=lambda h: (
                    0 if h["meta"]["is_favourite"] else 1,
                    str(h["meta"]["display_label"]).lower(),
                ),
            )
            for idx, h in enumerate(ordered):
                h["row"].move(list_box, target_index=idx)

        with list_box:
            for it in items:
                rows.append(
                    _render_quick_access_row(
                        client,
                        kind=str(it["kind"]),
                        item=it["item"],
                        favourites=favourites,
                        is_favourite=bool(it["is_favourite"]),
                        tutorials=it["tutorials"],
                        icon_url=it.get("icon_url"),
                        resort=_resort,
                    )
                )

        def _apply_filter(kind: str) -> None:
            any_visible = False
            for row in rows:
                visible = kind == "all" or row["kind"] == kind
                row["row"].set_visibility(visible)
                any_visible = any_visible or visible
            empty.set_visibility(not any_visible)

        def _on_filter(kind: str) -> None:
            for k, btn in seg_buttons.items():
                if k == kind:
                    btn.classes(add="on")
                else:
                    btn.classes(remove="on")
            store_quick_access_filter(kind)
            _persist_quick_access_filter(kind)
            _apply_filter(kind)

        _apply_filter(current_quick_access_filter())


def _persist_quick_access_filter(kind: str) -> None:
    """Write the Quick Access filter through to the server prefs (§9)."""
    token = current_token()
    if not token:
        return
    client = ApiClient(get_app(), token=token)

    async def _write() -> None:
        # A cosmetic preference — never worth interrupting the user for.
        with contextlib.suppress(Exception):
            await client.set_ui_preferences(quick_access_filter=kind)

    asyncio.create_task(_write())


def _tutorial_icon(tutorial: dict[str, Any]) -> str:
    """Material icon for a tutorial by media type (PDF vs video)."""
    return "picture_as_pdf" if str(tutorial.get("media_type") or "") == "document" else "play_circle"


def _render_qa_tutorials_button(client: ApiClient, tutorials: list[dict[str, Any]]) -> None:
    """Tutorials button on a Quick Access row.

    One tutorial → a plain button that opens it on **click**. Two or more
    → a **hover** popup listing each one (pick which to view). Zero is
    handled by the caller (no button at all).
    """
    if len(tutorials) == 1:
        tid = int(tutorials[0]["id"])
        with ui.element("div").classes("matofx-tbtn").on(
            "click", lambda _e=None, _id=tid: _open_tutorial(client, _id)
        ):
            ui.icon("school").style("font-size: 0.95rem;")
            ui.label("Tutorials")
        return
    # Multiple — flat CSS hover popup (reuses the Launch menu styling).
    with ui.element("div").classes("matofx-launchwrap"):
        with ui.element("div").classes("matofx-tbtn"):
            ui.icon("school").style("font-size: 0.95rem;")
            ui.label("Tutorials")
            ui.icon("expand_more").style("font-size: 0.95rem;")
        with ui.element("div").classes("matofx-launchmenu"):
            for tut in tutorials:
                tid = int(tut["id"])
                title = str(tut.get("title") or tut.get("original_filename") or "Tutorial")
                with ui.element("div").classes("matofx-launchitem").on(
                    "click", lambda _e=None, _id=tid: _open_tutorial(client, _id)
                ):
                    ui.icon(_tutorial_icon(tut)).style("font-size: 0.95rem;")
                    ui.label(title)


def _render_quick_access_row(
    client: ApiClient,
    *,
    kind: str,
    item: dict[str, Any],
    favourites: _FavouritesState,
    is_favourite: bool,
    tutorials: list[dict[str, Any]],
    icon_url: str | None = None,
    resort: Any = None,
) -> dict[str, Any]:
    """One Quick Access row; returns a handle dict the filter/star/resort use.

    The star sits **after** the name (mockup): filled when favourited,
    outline otherwise — clicking it toggles the favourite in place and
    calls ``resort`` so it floats to the top immediately. When the
    tool/workflow has an uploaded icon (``icon_url``) it shows instead of
    the generic glyph. The Launch button is a flat CSS **hover** menu;
    Tutorials (when present) follows the single-click / multi-hover rule.
    """
    alias = str(item.get("alias") or "")
    if kind == "tool":
        label = _tool_display_label(item)
        short = _truncate(str(item.get("short_description") or item.get("description") or ""), 48)
        subtitle = f"Tool · {short}" if short else "Tool"
        icon = "build"
    else:
        label = _workflow_display_label(item)
        n_tools = len(item.get("member_chain") or []) or int(item.get("member_count") or 0)
        subtitle = f"Workflow · {n_tools} tool{'s' if n_tools != 1 else ''}"
        icon = "account_tree"

    meta: dict[str, Any] = {"alias": alias, "is_favourite": is_favourite, "display_label": label}
    handle: dict[str, Any] = {"kind": kind, "meta": meta}

    row = (
        ui.row()
        .classes("items-center w-full no-wrap matofx-qarow")
        .style(
            "gap: 0.7rem; padding: 0.5rem 0.4rem; "
            "border-bottom: 1px solid var(--matofx-card-border);"
        )
    )
    handle["row"] = row
    with row:
        icon_chip(icon, image_url=icon_url, size=32)
        with ui.column().classes("gap-0").style("flex: 1 1 0; min-width: 0;"):
            with ui.row().classes("items-center no-wrap").style("gap: 0.4rem; min-width: 0;"):
                ui.label(label).style(
                    "font-size: 0.85rem; font-weight: 600; white-space: nowrap; "
                    "overflow: hidden; text-overflow: ellipsis;"
                )
                # Favourite star AFTER the name (mockup); toggles in place
                # and re-sorts so it floats to the top immediately.
                _render_favourite_star(
                    meta=meta,
                    on_toggle=lambda _e=None: _toggle_favourite(
                        client, favourites, kind, meta, resort or (lambda: None)
                    ),
                )
            ui.label(subtitle).style(
                "font-size: 0.72rem; color: var(--matofx-muted-text); white-space: nowrap; "
                "overflow: hidden; text-overflow: ellipsis;"
            )
        if tutorials:
            _render_qa_tutorials_button(client, tutorials)
        # Launch — a flat CSS hover menu (no click, no shadow).
        if kind == "tool":
            existing = lambda _e=None, _a=alias, _n=label: _launch_existing_project(_a, _n)  # noqa: E731
            new = lambda _e=None, _a=alias, _n=label: _launch_new_project(_a, _n)  # noqa: E731
        else:
            existing = lambda _e=None, _a=alias, _n=label: _launch_workflow_existing(  # noqa: E731
                client, _a, _n
            )
            new = lambda _e=None, _a=alias, _n=label: _launch_workflow_new(client, _a, _n)  # noqa: E731
        with ui.element("div").classes("matofx-launchwrap"):
            with ui.element("div").classes("matofx-lbtn"):
                ui.label("Launch")
                ui.icon("expand_more").style("font-size: 0.95rem;")
            with ui.element("div").classes("matofx-launchmenu"):
                with ui.element("div").classes("matofx-launchitem").on("click", existing):
                    ui.icon("folder").style("font-size: 0.95rem;")
                    ui.label("In existing project")
                with ui.element("div").classes("matofx-launchitem").on("click", new):
                    ui.icon("add").style("font-size: 0.95rem;")
                    ui.label("In new project")
    return handle


@dataclass(frozen=True)
class _TutorialBuckets:
    """Active tutorials split by scope, so renderers can index by alias.

    The bucket is built once from a single ``/api/tutorials`` call and
    passed down so the per-tool / per-workflow cards don't fan out to
    N+1 fetches.
    """

    general: list[dict[str, Any]] = field(default_factory=list)
    by_tool: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    by_workflow: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


async def _load_active_tutorials(client: ApiClient) -> _TutorialBuckets:
    """Fetch every active tutorial and bucket by scope.

    Tutorials have one of three scopes — general, per-tool, or
    per-workflow — encoded by which of ``tool_alias`` /
    ``workflow_alias`` is set (server-side CHECK forbids both). The
    bucket dataclass lets the caller index by alias without
    re-walking the list.

    Any error degrades silently to empty buckets — the page must keep
    rendering even if the tutorials endpoint is mid-restart.
    """
    try:
        rows = await client.list_active_tutorials()
    except ApiError:
        return _TutorialBuckets()
    general: list[dict[str, Any]] = []
    by_tool: dict[str, list[dict[str, Any]]] = {}
    by_workflow: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        tool_alias = row.get("tool_alias")
        workflow_alias = row.get("workflow_alias")
        if tool_alias:
            by_tool.setdefault(str(tool_alias), []).append(row)
        elif workflow_alias:
            by_workflow.setdefault(str(workflow_alias), []).append(row)
        else:
            general.append(row)
    return _TutorialBuckets(general=general, by_tool=by_tool, by_workflow=by_workflow)


@dataclass
class _FavouritesState:
    """Snapshot of the user's favourites + the site-wide enabled flag.

    Mutable on purpose — the star toggle handlers update ``tools``
    / ``workflows`` in place so a re-sort sees the live set without
    a re-fetch. ``enabled`` reflects the value at page load; an
    admin flipping the toggle while the user is on Home doesn't
    propagate until the next render (acceptable — the flag is rare
    and the next nav refreshes it).
    """

    enabled: bool = False
    tools: set[str] = field(default_factory=set)
    workflows: set[str] = field(default_factory=set)


async def _load_favourites(client: ApiClient) -> _FavouritesState:
    """Fetch the caller's favourites snapshot, degrading silently on error.

    A failure here leaves the feature flag at ``False`` — the stars
    won't render but the rest of the page works. We catch broadly
    (not just :class:`ApiError`) because this helper sits BEFORE
    the page body's outer ``ui.column`` is opened: any uncaught
    exception here leaves the body blank with only the sidebar
    visible. The favourites surface is a nice-to-have; an
    early-startup migration gap or a transient transport error
    must never take the whole Home page down with it.
    """
    try:
        snap = await client.get_my_favourites()
    except Exception:  # noqa: BLE001 — favourites must never block the page body
        return _FavouritesState()
    if not isinstance(snap, dict):
        return _FavouritesState()
    return _FavouritesState(
        enabled=bool(snap.get("enabled", False)),
        tools=set(snap.get("tools") or []),
        workflows=set(snap.get("workflows") or []),
    )


_STAR_FILLED_COLOUR = "#FB8C00"
"""Orange used for the filled-star (favourited) state. Matches Quasar's
default ``warning`` palette so it reads as accent against the card's
neutral surface without needing a custom Tailwind tweak."""

_STAR_OUTLINE_COLOUR = "#888"
"""Grey used for the empty-outline star — same colour as the muted
secondary text in each card."""


def _render_favourite_star(*, meta: dict[str, Any], on_toggle: Any) -> None:
    """Inline star toggle next to a Quick Access card's name.

    Renders both glyphs (``star`` filled-orange, ``star_outline``
    grey) and toggles which one is visible based on
    ``meta['is_favourite']``. Storing both in the DOM means the
    flip is a one-line visibility swap rather than a tear-down +
    rebuild — much less surface area for things to go wrong, and
    the click handler is bound exactly once.

    References to the two icons are stashed back onto ``meta`` so
    :func:`_apply_star_state` can swap them after a toggle without
    walking the card tree.
    """
    filled = ui.icon("star").style(
        f"cursor: pointer; font-size: 1.2rem; color: {_STAR_FILLED_COLOUR};"
    )
    outline = ui.icon("star_outline").style(
        f"cursor: pointer; font-size: 1.2rem; color: {_STAR_OUTLINE_COLOUR};"
    )
    filled.on("click", on_toggle).tooltip("Unfavourite")
    outline.on("click", on_toggle).tooltip("Favourite")
    meta["__star_filled"] = filled
    meta["__star_outline"] = outline
    _apply_star_state(meta)


def _apply_star_state(meta: dict[str, Any]) -> None:
    """Reflect ``meta['is_favourite']`` onto the card's star icons."""
    is_fav = bool(meta["is_favourite"])
    filled = meta.get("__star_filled")
    outline = meta.get("__star_outline")
    if filled is not None:
        filled.set_visibility(is_fav)
    if outline is not None:
        outline.set_visibility(not is_fav)


def _toggle_favourite(
    client: ApiClient,
    favourites: _FavouritesState,
    kind: str,
    meta: dict[str, Any],
    resort: Any,
) -> None:
    """Optimistically flip a card's favourite state and reconcile via API.

    Three steps:

    1. **Optimistic** — flip ``meta['is_favourite']``, swap the
       star glyph (filled ⇄ outline), re-sort the section so the
       card jumps to / leaves the favourites group immediately.
    2. **Reconcile** — call the API in a background task.
    3. **Revert** — on failure undo the optimistic flip and toast
       the reason; the section re-sorts back to its prior order.

    The card itself is never rebuilt, so search-box state and
    scroll position survive every toggle.
    """
    alias = str(meta["alias"])
    target_set = favourites.tools if kind == "tool" else favourites.workflows
    was_favourite = bool(meta["is_favourite"])

    # Step 1 — optimistic flip + UI swap + re-sort.
    meta["is_favourite"] = not was_favourite
    if meta["is_favourite"]:
        target_set.add(alias)
    else:
        target_set.discard(alias)
    _apply_star_state(meta)
    resort()

    async def _reconcile() -> None:
        try:
            if meta["is_favourite"]:
                await client.add_favourite(kind=kind, alias=alias)
            else:
                await client.remove_favourite(kind=kind, alias=alias)
        except ApiError as exc:
            meta["is_favourite"] = was_favourite
            if was_favourite:
                target_set.add(alias)
            else:
                target_set.discard(alias)
            _apply_star_state(meta)
            resort()
            ui.notify(f"Couldn't update favourite: {exc.detail}", type="negative")

    asyncio.create_task(_reconcile())


async def _resolve_greeting_name(client: ApiClient) -> str:
    """First name for the greeting; falls back to username, then ``'there'``.

    Cached on per-browser storage after the first hit so we don't pay
    a /me round-trip on every home-page render. A user editing their
    first name on the Account page won't see the new greeting until
    the next sign-in — acceptable: identity edits are rare and the
    chrome rebuilds carry the latest stored value forward.
    """
    cached = current_first_name()
    if cached:
        return cached
    try:
        me = await client.me()
    except ApiError:
        return current_username() or "there"
    first_name = str(me.get("first_name") or "").strip()
    if first_name:
        store_first_name(first_name)
        return first_name
    return current_username() or "there"


# ---------- left rail --------------------------------------------------------


def _render_create_card(client: ApiClient) -> None:
    """Stacked button list for the legacy "Create" card.

    Every button now *creates in place* rather than bouncing to another
    page. The **Project** button opens the dashboard's New-project dialog
    overlaid on the home page. The **Scenario** button opens a chooser
    that asks for the parent project first (a scenario can't exist
    without one) and then the scenario name. The **Job** button opens a
    chooser listing every installed tool + every admin-released workflow
    the user can see — clicking one funnels into the matching tool/
    workflow launch flow.

    The **Custom Workflow** button jumps to ``/my-workflows`` — the
    dedicated owner page for picking 2–10 tools, computing the
    descriptor-driven run order, and optionally sharing the saved
    grouping with named users.
    """
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label("Create").style("font-size: 1.05rem; font-weight: 600;")
        ui.button("Project", on_click=lambda: _open_new_project(client)).props(
            "color=primary"
        ).classes("w-full").tooltip("Create a new project.")
        ui.button(
            "Scenario",
            on_click=lambda: _open_new_scenario_chooser(client),
        ).props("color=primary").classes("w-full").tooltip(
            "Create a new scenario inside one of your projects."
        )
        ui.button(
            "Job(s)",
            on_click=lambda: _open_job_chooser(client),
        ).props("color=primary").classes("w-full").tooltip("Pick a tool or workflow to launch.")
        ui.button(
            "Custom Workflow",
            on_click=lambda: ui.navigate.to("/my-workflows"),
        ).props("color=primary").classes("w-full").tooltip(
            "Group 2–10 tools you already have access to into a saved "
            "workflow you can re-launch and (optionally) share with "
            "named users."
        )


async def _open_new_project(client: ApiClient) -> None:
    """Open the dashboard's New-project dialog overlaid on the home page.

    The dialog needs the metadata taxonomy, the user directory (for the
    optional share step), and the actor's id, so we fetch those first and
    then reuse the canonical dialog from the dashboard page rather than
    duplicate its metadata/share wiring here.
    """
    try:
        me = await client.me()
        fields = await client.list_metadata_fields()
        directory_list = await client.list_user_directory()
    except ApiError as exc:
        ui.notify(f"Couldn't open the New project form: {exc.detail}", type="negative")
        return
    _open_new_project_dialog(client, fields, directory_list, int(me["user_id"]))


async def _open_new_scenario_chooser(client: ApiClient) -> None:
    """Pick a parent project, then create a scenario inside it.

    A scenario can't exist without a project and the home page carries no
    project context, so this dialog asks *where* first. Picking a project
    pre-fills the next ``Scenario <n>`` default; Create posts the scenario
    and jumps straight to it. If the user has no projects yet, the dialog
    says so and offers to open the New-project flow instead.
    """
    scenario_names: list[str] = []

    with ui.dialog() as dialog, ui.card().style("min-width: 460px;"):
        ui.label("New scenario").style("font-size: 1.2rem; font-weight: 600;")

        no_projects_hint = ui.row().classes("w-full items-center gap-2")
        no_projects_hint.set_visibility(False)

        project_select = (
            ui.select(options={}, label="Project", with_input=True).props("dense").classes("w-full")
        )
        name = ui.input("Scenario name *").props("autofocus").classes("w-full")
        description = ui.input("Description (optional)").classes("w-full")
        message = ui.label("").style("color: #c0392b;")

        async def _switch_to_new_project() -> None:
            dialog.close()
            await _open_new_project(client)

        with no_projects_hint:
            ui.label("You have no projects yet — a scenario needs one.").style("color: #555;")
            ui.button("New project", on_click=_switch_to_new_project).props(
                "flat dense color=primary"
            )

        async def _on_project_change(_e: Any = None) -> None:
            message.text = ""
            if project_select.value is None:
                return
            try:
                scenarios = await client.list_scenarios(int(project_select.value))
            except ApiError as exc:
                message.text = exc.detail
                return
            scenario_names[:] = [str(s.get("name") or "") for s in scenarios]
            name.value = next_scenario_name(scenario_names)

        async def _create() -> None:
            if project_select.value is None:
                message.text = "Please choose a project for the scenario."
                return
            if not name.value or not name.value.strip():
                message.text = "Please enter a name for the scenario."
                return
            try:
                scenario = await client.create_scenario(
                    int(project_select.value),
                    name=name.value,
                    description=description.value or "",
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Scenario created.", type="positive")
            ui.navigate.to(f"/scenarios/{scenario['id']}")

        project_select.on("update:model-value", _on_project_change)

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            create_btn = ui.button("Create", on_click=_create).props("color=primary")

    try:
        projects = await client.list_projects()
    except ApiError as exc:
        ui.notify(f"Couldn't open the New scenario form: {exc.detail}", type="negative")
        return
    if not projects:
        no_projects_hint.set_visibility(True)
        project_select.set_visibility(False)
        name.set_visibility(False)
        description.set_visibility(False)
        create_btn.props("disable")
    else:
        project_select.options = {int(p["id"]): str(p["name"]) for p in projects}
        project_select.update()
    dialog.open()


async def _load_approvals_summary(client: ApiClient) -> dict[str, Any]:
    """Fetch {can_approve, pending} for the Simulation Approvals card.

    Best-effort: any error degrades to "not an approver" so a hiccup
    in the approvals API never blanks the whole home page.
    """
    try:
        return await client.approvals_summary()
    except Exception:  # noqa: BLE001 — home must render regardless
        return {"can_approve": False, "pending": 0}


_APPROVAL_KIND_ICONS: dict[str, str] = {
    "job": "assignment_turned_in",
    "study": "science",
    "optimisation": "tune",
    "library": "folder_shared",
    "material": "dataset",
    "vv_tool": "verified",
    "vv_job": "verified",
    "surrogate_model": "model_training",
}


async def _render_approvals_card(client: ApiClient, summary: dict[str, Any]) -> None:
    """Approvals card — pending items addressed to the caller (§7.6).

    Approver-only (the call site gates on ``can_approve``). Lists each
    pending request with its type icon, what's awaiting, and a
    requester · project/scenario · age sub-line, plus a Review button.
    Review navigates to the Approvals queue (the dedicated review
    surface); a footer link opens the full queue. An empty queue shows a
    quiet "all caught up" line.
    """
    with ui.card().classes("w-full").style("overflow: visible;"), ui.column().classes(
        "w-full gap-2"
    ):
        with ui.row().classes("w-full items-center no-wrap").style("gap: 0.6rem;"):
            ui.icon("fact_check").style("color: var(--matofx-accent);")
            ui.label("Approvals").style("font-size: 0.95rem; font-weight: 650;")
            pending = int(summary.get("pending", 0) or 0)
            if pending:
                ui.label(f"{pending} pending").style(
                    "font-size: 0.72rem; color: var(--matofx-muted-text);"
                )
                ui.space()
                ui.badge(str(pending)).props("color=orange")

        try:
            items = await client.list_my_approvals()
        except ApiError:
            items = []

        if not items:
            ui.label("You're all caught up — nothing is waiting on your approval.").style(
                "color: var(--matofx-muted-text); font-size: 0.85rem; padding: 0.4rem 0;"
            )
            return

        for item in items[:6]:
            _render_approval_row(item)

        with ui.row().classes("w-full items-center cursor-pointer").style(
            "padding-top: 0.4rem; color: var(--matofx-accent); font-size: 0.8rem; font-weight: 600;"
        ).on("click", lambda: ui.navigate.to("/approvals")):
            ui.label("Open approvals queue")
            ui.icon("chevron_right")


def _render_approval_row(item: dict[str, Any]) -> None:
    """One pending-approval row: type icon + title + sub + Review."""
    target_kind = str(item.get("target_kind") or "job")
    icon = _APPROVAL_KIND_ICONS.get(target_kind, "fact_check")
    title = str(item.get("job_name") or item.get("tool_name") or "Awaiting approval")
    requester = str(item.get("requestor_name") or "")
    project = str(item.get("project_name") or "")
    scenario = str(item.get("scenario_name") or "")
    where = f"{project} / {scenario}".strip(" /")
    age = _relative_age(item.get("requested_at"))
    sub = " · ".join(p for p in (requester, where, age) if p)
    request_id = int(item.get("request_id") or 0)
    with ui.row().classes("items-center w-full no-wrap").style(
        "gap: 0.7rem; padding: 0.5rem 0.25rem; border-bottom: 1px solid var(--matofx-card-border);"
    ):
        with ui.element("div").classes("items-center justify-center").style(
            "width: 30px; height: 30px; border-radius: 8px; flex: none; display: flex; "
            "background: rgba(214,169,59,0.16); color: #d6a93b;"
        ):
            ui.icon(icon).style("font-size: 1rem;")
        with ui.column().classes("gap-0").style("flex: 1 1 0; min-width: 0;"):
            ui.label(title).style(
                "font-size: 0.83rem; font-weight: 600; white-space: nowrap; "
                "overflow: hidden; text-overflow: ellipsis;"
            )
            if sub:
                ui.label(sub).style(
                    "font-size: 0.7rem; color: var(--matofx-muted-text); white-space: nowrap; "
                    "overflow: hidden; text-overflow: ellipsis;"
                )
        ui.button(
            "Review",
            on_click=lambda _e=None, _r=request_id: ui.navigate.to("/approvals"),
        ).props("outline dense color=primary size=sm")


def _relative_age(value: Any) -> str:
    """A compact 'Nm/Nh/Nd ago' from an ISO timestamp (best-effort)."""
    if not value:
        return ""
    import datetime as _dt  # noqa: PLC0415

    try:
        ts = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    now = _dt.datetime.now(tz=ts.tzinfo) if ts.tzinfo else _dt.datetime.now()
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 60:  # noqa: PLR2004
        return "just now"
    if secs < 3600:  # noqa: PLR2004
        return f"{secs // 60}m ago"
    if secs < 86400:  # noqa: PLR2004
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


# ---------- My-Work tables ---------------------------------------------------

# Quasar badge colour per job / study state, for the State pill column.
_STATE_PILL_COLOUR: dict[str, str] = {
    "running": "blue",
    "running - repeat": "blue",
    "complete": "green",
    "queued": "orange",
    "created": "grey",
    "draft": "grey",
    "failed": "red",
    "denied": "red",
    "cancelled": "grey",
    "rejected": "red",
    "partial": "orange",
    "pending approval": "orange",
    "pending_approval": "orange",
    "pending_start_approval": "orange",
}


def _state_colour(state: str) -> str:
    """Quasar colour name for a state pill (defaults to grey)."""
    return _STATE_PILL_COLOUR.get(state.strip().lower(), "grey")


_PILL_CLASS: dict[str, str] = {
    "running": "p-run",
    "running - repeat": "p-run",
    "complete": "p-ok",
    "queued": "p-q",
    "created": "p-cr",
    "draft": "p-cr",
    "failed": "p-fail",
    "denied": "p-fail",
    "cancelled": "p-cr",
    "rejected": "p-fail",
    "partial": "p-q",
    "pending approval": "p-q",
    "pending_approval": "p-q",
    "pending_start_approval": "p-q",
}


def _pill_class(state: str) -> str:
    """CSS pill class for a state (mockup p-run / p-ok / …; defaults p-cr)."""
    return _PILL_CLASS.get(state.strip().lower(), "p-cr")


def _render_work_table_card(
    *,
    icon: str,
    title: str,
    count: int | None,
    grid: str,
    columns: list[tuple[str, str, str]],
    rows: list[dict[str, Any]],
    on_row_click: Any,
    new_handler: Any,
    footer_text: str,
    footer_handler: Any,
    empty_text: str,
) -> None:
    """A My-Work table card built from the mockup's CSS grid (§7.7–7.10).

    ``grid`` is the CSS ``grid-template-columns`` shared by the header
    and every row, so columns line up exactly. ``columns`` is a list of
    ``(label, key, kind)`` where ``kind`` is ``"name"`` (a two-line
    label + ``<key>_sub`` cell — the sub line always reserves its height
    so every row is the same height even when it's blank), ``"pill"`` (a
    coloured state pill), or ``"text"``. Row-click and the **New** /
    footer links wire to real routes.
    """
    with ui.card().classes("w-full").style("height: 420px; overflow: hidden; padding: 0;"):
        with ui.column().classes("w-full no-wrap").style("height: 100%; gap: 0;"):
            with ui.row().classes("w-full items-center no-wrap").style(
                "gap: 0.55rem; padding: 12px 15px; flex: none; "
                "border-bottom: 1px solid var(--matofx-card-border);"
            ):
                ui.icon(icon).style("color: var(--matofx-accent); font-size: 1.15rem;")
                ui.label(title).style("font-size: 0.95rem; font-weight: 650;")
                if count is not None:
                    ui.label(str(count)).style(
                        "font-size: 11px; color: var(--matofx-muted-text); "
                        "background: rgba(127,127,127,0.12); padding: 1px 8px; border-radius: 10px;"
                    )
                ui.space()
                ui.button("New", icon="add", on_click=new_handler).props(
                    "outline color=primary no-caps"
                ).style("font-size: 12px; padding: 4px 11px;")
            if not rows:
                with ui.element("div").classes("matofx-colhead").style(
                    f"grid-template-columns: {grid};"
                ):
                    for label, _key, _kind in columns:
                        ui.label(label)
                ui.label(empty_text).style(
                    "color: var(--matofx-muted-text); font-size: 0.85rem; padding: 15px;"
                )
                return
            with ui.element("div").classes("matofx-tablescroll matofx-scroll"):
                # Sticky header inside the scroll → identical content width
                # to the rows, so columns line up exactly.
                with ui.element("div").classes("matofx-colhead").style(
                    f"grid-template-columns: {grid};"
                ):
                    for label, _key, _kind in columns:
                        ui.label(label)
                for row in rows:
                    erow = (
                        ui.element("div")
                        .classes("matofx-erow")
                        .style(f"grid-template-columns: {grid};")
                        .on("click", lambda _e=None, _r=row: on_row_click(_r))
                    )
                    with erow:
                        for _label, key, kind in columns:
                            _render_table_cell(row, key, kind)
            with ui.element("div").classes("matofx-tablefoot").on("click", footer_handler):
                ui.label(footer_text)
                ui.icon("chevron_right").style("font-size: 1rem;")


def _render_table_cell(row: dict[str, Any], key: str, kind: str) -> None:
    """Render one grid cell for a My-Work table row."""
    if kind == "name":
        with ui.element("div").classes("matofx-ename"):
            ui.label(str(row.get(key, "") or "")).classes("matofx-nlink")
            # Always present (min-height in CSS) so rows stay equal height
            # even when there's no sub-text (e.g. a project with no
            # description).
            ui.label(str(row.get(f"{key}_sub", "") or "")).classes("matofx-nsub")
    elif kind == "pill":
        value = str(row.get(key, "") or "")
        import html as _html  # noqa: PLC0415

        ui.html(
            f'<span class="matofx-pill {_pill_class(value)}">{_html.escape(value)}</span>',
            sanitize=False,
        )
    else:
        ui.label(str(row.get(key, "") or "")).classes("matofx-col")


_MY_ROLE_LABEL: dict[str, str] = {"owner": "Owner", "member": "Member", "viewer": "Viewer"}


async def _render_my_projects_table(client: ApiClient) -> None:
    """My Projects table — Project · Owner · My role · # Scenarios (§7.7)."""
    try:
        projects = await client.list_projects()
    except ApiError:
        projects = []
    rows: list[dict[str, Any]] = []
    for p in projects:
        scenario_count = 0
        try:
            scenario_count = len(await client.list_scenarios(int(p["id"])))
        except ApiError:
            scenario_count = 0
        rows.append(
            {
                "id": int(p["id"]),
                "project": str(p.get("name") or "—"),
                "project_sub": _truncate(str(p.get("description") or ""), 60),
                "owner": str(p.get("owner_name") or "—"),
                "role": _MY_ROLE_LABEL.get(str(p.get("my_role") or ""), "—"),
                "scenarios": scenario_count,
            }
        )
    _render_work_table_card(
        icon="folder",
        title="My Projects",
        count=len(rows),
        grid="minmax(0,1.6fr) minmax(0,1fr) 90px 100px",
        columns=[
            ("Project", "project", "name"),
            ("Owner", "owner", "text"),
            ("My role", "role", "text"),
            ("# Scenarios", "scenarios", "text"),
        ],
        rows=rows,
        on_row_click=lambda row: ui.navigate.to(f"/projects/{int(row['id'])}"),
        new_handler=lambda: _open_new_project(client),
        footer_text=f"Show all {len(rows)} projects",
        footer_handler=lambda: ui.navigate.to("/dashboard"),
        empty_text="No projects yet — use Create → Project to start one.",
    )


async def _render_my_jobs_table(client: ApiClient) -> None:
    """My Jobs table — Job · Project / Scenario · State · When (§7.8)."""
    job_rows = await _collect_job_rows(client)
    rows: list[dict[str, Any]] = []
    for j in job_rows:
        state = str(j.get("state") or "").strip()
        rows.append(
            {
                "id": int(j["id"]),
                "workflow_run_id": j.get("workflow_run_id"),
                "job": str(j.get("tool") or "—"),
                "job_sub": f"#{int(j['id']):07d}",
                "where": f"{j.get('project') or '—'} / {j.get('scenario') or '—'}",
                "state": state.title(),
                "when": _relative_age(j.get("created_at_raw")) or str(j.get("created") or ""),
            }
        )
    _render_work_table_card(
        icon="assignment",
        title="My Jobs",
        count=len(rows),
        grid="minmax(0,1.6fr) minmax(0,1.3fr) 90px 84px",
        columns=[
            ("Job", "job", "name"),
            ("Project / Scenario", "where", "text"),
            ("State", "state", "pill"),
            ("When", "when", "text"),
        ],
        rows=rows,
        on_row_click=_row_click_navigate,
        new_handler=lambda: _open_job_chooser(client),
        footer_text="Show all my jobs",
        footer_handler=lambda: ui.navigate.to("/my-jobs"),
        empty_text="No jobs yet — submit one from Create → Job.",
    )


async def _render_parametric_studies_table(client: ApiClient) -> None:
    """Parametric Studies table — Study · Project · Runs · State · When (§7.9)."""
    try:
        projects = await client.list_projects()
    except ApiError:
        projects = []
    project_names = {int(p["id"]): str(p.get("name") or p["id"]) for p in projects}
    try:
        tool_names = {
            str(t["alias"]): str(t.get("name") or t["alias"]) for t in await client.list_tools()
        }
    except ApiError:
        tool_names = {}
    studies: list[dict[str, Any]] = []
    for p in projects:
        try:
            studies.extend(await client.list_studies(int(p["id"])))
        except ApiError:
            continue
    studies.sort(key=lambda s: str(s.get("created_at") or ""), reverse=True)
    rows: list[dict[str, Any]] = []
    for s in studies[:25]:
        state = str(s.get("state") or "")
        generated = int(s.get("generated_count") or 0)
        skipped = int(s.get("skipped_invalid_count") or 0)
        rows.append(
            {
                "id": int(s.get("id") or 0),
                "study": str(s.get("name") or "—"),
                "study_sub": (
                    f"{tool_names.get(str(s.get('tool_alias') or ''), '')} · #{int(s.get('id') or 0)}"
                ),
                "project": project_names.get(int(s.get("project_id") or 0), "—"),
                "runs": f"{generated}" + (f" (+{skipped} skipped)" if skipped else ""),
                "state": _STUDY_STATE_LABEL.get(state, state.title()),
                "state_colour": _state_colour(state),
                "when": _relative_age(s.get("created_at")),
            }
        )
    _render_work_table_card(
        icon="science",
        title="Parametric Studies",
        count=len(rows),
        grid="minmax(0,1.5fr) minmax(0,1fr) 90px 90px 70px",
        columns=[
            ("Study", "study", "name"),
            ("Project", "project", "text"),
            ("Runs", "runs", "text"),
            ("State", "state", "pill"),
            ("When", "when", "text"),
        ],
        rows=rows,
        on_row_click=lambda row: ui.navigate.to(f"/studies/{int(row['id'])}"),
        new_handler=lambda: ui.navigate.to("/studies"),
        footer_text=f"Show all {len(rows)} studies",
        footer_handler=lambda: ui.navigate.to("/studies"),
        empty_text="No studies yet — use Create → Parametric Studies.",
    )


async def _render_optimisations_table(client: ApiClient) -> None:
    """Optimisations table — Study · Best · Batch · State (§7.10)."""
    try:
        projects = await client.list_projects()
    except ApiError:
        projects = []
    try:
        tool_names = {
            str(t["alias"]): str(t.get("name") or t["alias"]) for t in await client.list_tools()
        }
        wf_names = {
            str(w["alias"]): str(w.get("name") or w["alias"])
            for w in await client.list_workflows()
        }
    except ApiError:
        tool_names, wf_names = {}, {}
    studies: list[dict[str, Any]] = []
    for p in projects:
        try:
            studies.extend(await client.list_optimisation_studies(int(p["id"])))
        except ApiError:
            continue
    studies.sort(key=lambda s: str(s.get("created_at") or ""), reverse=True)
    rows: list[dict[str, Any]] = []
    for s in studies[:25]:
        state = str(s.get("state") or "")
        alias = str(s.get("tool_alias") or s.get("workflow_alias") or "")
        target = tool_names.get(alias) or wf_names.get(alias) or alias or "—"
        rows.append(
            {
                "id": int(s.get("id") or 0),
                "study": str(s.get("name") or "—"),
                "study_sub": f"{target} · #{int(s.get('id') or 0)}",
                "best": _format_best(s.get("current_best_json")),
                "batch": int(s.get("last_batch_number") or 0),
                "state": _STUDY_STATE_LABEL.get(state, state.title()),
                "state_colour": _state_colour(state),
            }
        )
    _render_work_table_card(
        icon="tune",
        title="Optimisations",
        count=len(rows),
        grid="minmax(0,1.6fr) 100px 60px 90px",
        columns=[
            ("Study", "study", "name"),
            ("Best", "best", "text"),
            ("Batch", "batch", "text"),
            ("State", "state", "pill"),
        ],
        rows=rows,
        on_row_click=lambda row: ui.navigate.to(f"/optimisation/{int(row['id'])}"),
        new_handler=lambda: ui.navigate.to("/optimisation"),
        footer_text=f"Show all {len(rows)} optimisations",
        footer_handler=lambda: ui.navigate.to("/optimisation"),
        empty_text="No optimisation studies yet — use Create → Optimisation.",
    )


def _format_best(best_json: Any) -> str:
    """Compact rendering of an optimisation's current best objective."""
    if best_json is None:
        return "—"
    if isinstance(best_json, dict):
        for key in ("objective", "loss", "value", "best"):
            if key in best_json:
                val = best_json[key]
                break
        else:
            return "—"
    else:
        val = best_json
    try:
        return f"{float(val):.4g}"
    except (TypeError, ValueError):
        return str(val)[:12]


def _render_job_search_card() -> None:
    """Tiny card linking to the dedicated Job Search page.

    Job Search lives at ``/job-search`` (port of legacy
    ``functions/job_search.py``) — pick a tool, filter by input
    values, see matching completed runs. Distinct from ``/my-jobs``
    (which is navigation, not discovery): a user looking for "every
    run that used Steel at 25 °C" lands here, not in the workspace.
    """
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label("Job Search").style("font-size: 1.05rem; font-weight: 600;")
        ui.button(
            "Search for Jobs",
            on_click=lambda: ui.navigate.to("/job-search"),
        ).props("color=primary").classes("w-full")


def _render_tutorials_card(
    client: ApiClient,
    general_tutorials: list[dict[str, Any]],
) -> None:
    """Tutorials card — shows every active general (non-tool-scoped) tutorial.

    Each entry opens in a new browser tab via a blob URL since the
    file endpoint needs a bearer token (see ``_open_tutorial`` for the
    pattern). Tool-scoped tutorials surface from the Quick Access
    cards' new *Tutorials* button instead — keeping the two surfaces
    distinct matches the user's mental model: "What's on the rail
    applies to everything; what's on a tool card applies to that
    tool."
    """
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label("Tutorials").style("font-size: 1.05rem; font-weight: 600;")
        if not general_tutorials:
            ui.label(
                "No general tutorials yet — they'll appear here once an administrator uploads one."
            ).style("color: #888; font-size: 0.85rem;")
            return
        for tutorial in general_tutorials:
            _render_tutorial_entry(client, tutorial)


def _render_tutorial_entry(client: ApiClient, tutorial: dict[str, Any]) -> None:
    """One row in the rail's tutorials list.

    Icon + title + open button; the click handler streams the bytes
    via the API client and pops the file in a new tab.
    """
    media_type = str(tutorial.get("media_type") or "")
    icon = "picture_as_pdf" if media_type == "document" else "play_circle"
    title = str(tutorial.get("title") or tutorial.get("original_filename") or "Tutorial")
    tutorial_id = int(tutorial["id"])
    ui.button(
        title,
        icon=icon,
        on_click=lambda _e=None, _id=tutorial_id: _open_tutorial(client, _id),
    ).props("flat dense color=primary align=left").classes("w-full justify-start")


# ---------- centre column ----------------------------------------------------


async def _render_job_status_table(client: ApiClient) -> None:
    """One ``ui.table`` for every job the user can see — click to open."""
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label("Job Status").style("font-size: 1.05rem; font-weight: 600;")

        rows = await _collect_job_rows(client)
        if not rows:
            ui.label("No jobs yet — submit one from a scenario.").style("color: #888;")
            return

        columns = [
            {"name": "id", "label": "Job ID", "field": "id_text", "align": "left", "sortable": True},
            {"name": "tool", "label": "Tool", "field": "tool", "align": "left", "sortable": True},
            {
                "name": "workflow",
                "label": "Workflow",
                "field": "workflow",
                "align": "left",
                "sortable": True,
            },
            {"name": "state", "label": "State", "field": "state", "align": "left", "sortable": True},
            {
                "name": "created",
                "label": "Created",
                "field": "created",
                "align": "left",
                "sortable": True,
            },
            {
                "name": "project",
                "label": "Project",
                "field": "project",
                "align": "left",
                "sortable": True,
            },
            {
                "name": "scenario",
                "label": "Scenario",
                "field": "scenario",
                "align": "left",
                "sortable": True,
            },
        ]
        table = ui.table(columns=columns, rows=rows, row_key="id").classes("w-full")
        # Hide the row-key column from the rendered grid (we keep it on
        # the row dicts so row-click can read it back).
        table.props("dense flat hide-bottom").style("max-height: 360px; overflow: auto;")
        table.on(
            "row-click",
            lambda e: _row_click_navigate(e.args[1]),
        )


def _row_click_navigate(row: dict[str, Any]) -> None:
    """Open either the workflow run (My Jobs) or the standalone job page."""
    workflow_run_id = row.get("workflow_run_id")
    if workflow_run_id:
        ui.navigate.to(f"/my-jobs?workflow_run={int(workflow_run_id)}&job={int(row['id'])}")
        return
    ui.navigate.to(f"/jobs/{int(row['id'])}")


async def _collect_job_rows(client: ApiClient) -> list[dict[str, Any]]:
    """Walk visible projects → scenarios → jobs, sort newest first.

    Same shape as ``my_jobs.py`` — kept independent so changes to that
    page don't ripple into the home table.

    Resolves **tool name** (not alias) and **workflow name** columns
    inline by building per-page lookup maps from the tools + workflows
    catalogues. Each lookup is at most one API call per render, so the
    row-level resolution is cheap even with 50+ rows.
    """
    rows: list[dict[str, Any]] = []
    try:
        projects = await client.list_projects()
    except ApiError:
        return rows

    # Tool catalogue → ``alias → display name`` map. We use the
    # admin-facing listing because it shows every tool (released or
    # not) the user might have submitted a job against. Falls back to
    # the visible-tool listing if admin access is denied.
    tool_name_by_alias: dict[str, str] = {}
    try:
        tools = await client.list_tools()
    except ApiError:
        tools = []
    for tool in tools:
        alias = str(tool.get("alias") or "")
        if alias:
            tool_name_by_alias[alias] = str(tool.get("name") or alias)

    # Workflow-run-id → workflow display name map. Built by walking
    # the caller's visible workflow runs (cross-project) once. Empty
    # when the user has no runs / the endpoint errors — the column
    # just renders blank in those cases.
    workflow_name_by_run_id: dict[int, str] = {}
    try:
        wf_runs = await client.list_my_visible_workflow_runs()
    except ApiError:
        wf_runs = []
    for wf_run in wf_runs:
        rid = int(wf_run.get("id") or 0)
        if rid:
            wf_name = str(wf_run.get("workflow_name") or wf_run.get("workflow_alias") or "")
            workflow_name_by_run_id[rid] = wf_name

    for project in projects:
        try:
            scenarios = await client.list_scenarios(int(project["id"]))
        except ApiError:
            continue
        for scenario in scenarios:
            try:
                jobs = await client.list_jobs(int(scenario["id"]))
            except ApiError:
                continue
            for job in jobs:
                alias = str(job.get("tool_alias") or "")
                tool_label = tool_name_by_alias.get(alias) or alias or "—"
                workflow_run_id = job.get("workflow_run_id")
                workflow_label = (
                    workflow_name_by_run_id.get(int(workflow_run_id)) if workflow_run_id else ""
                ) or ""
                rows.append(
                    {
                        "id": int(job["id"]),
                        "id_text": f"{int(job['id']):07d}",
                        "tool": tool_label,
                        "workflow": workflow_label,
                        "workflow_run_id": int(workflow_run_id) if workflow_run_id else None,
                        "state": str(job.get("state") or "—").upper(),
                        "created": _short_timestamp(job.get("created_at")),
                        "created_at_raw": job.get("created_at"),
                        "project": str(project.get("name") or "—"),
                        "scenario": str(scenario.get("name") or "—"),
                    }
                )

    rows.sort(key=lambda r: r["id"], reverse=True)
    return rows[:_RECENT_JOB_LIMIT]


async def _render_projects_table(client: ApiClient) -> None:
    """One ``ui.table`` for every visible project — click to open."""
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label("Projects").style("font-size: 1.05rem; font-weight: 600;")

        rows = await _collect_project_rows(client)
        if not rows:
            ui.label("No projects yet — click 'Project' on the left to start one.").style(
                "color: #888;"
            )
            return

        columns = [
            {
                "name": "name",
                "label": "Project Name",
                "field": "name",
                "align": "left",
                "sortable": True,
            },
            {
                "name": "description",
                "label": "Description",
                "field": "description",
                "align": "left",
                "sortable": True,
            },
            {
                "name": "created",
                "label": "Created",
                "field": "created",
                "align": "left",
                "sortable": True,
            },
            {
                "name": "scenarios",
                "label": "# Scenarios",
                "field": "scenarios",
                "align": "left",
                "sortable": True,
            },
        ]
        table = ui.table(columns=columns, rows=rows, row_key="id").classes("w-full")
        table.props("dense flat hide-bottom").style("max-height: 360px; overflow: auto;")
        table.on(
            "row-click",
            lambda e: ui.navigate.to(f"/projects/{e.args[1]['id']}"),
        )


async def _collect_project_rows(client: ApiClient) -> list[dict[str, Any]]:
    """Build the projects-table row dicts; ``# Scenarios`` is computed."""
    rows: list[dict[str, Any]] = []
    try:
        projects = await client.list_projects()
    except ApiError:
        return rows
    for project in projects:
        scenario_count = 0
        try:
            scenarios = await client.list_scenarios(int(project["id"]))
            scenario_count = len(scenarios)
        except ApiError:
            scenario_count = 0
        rows.append(
            {
                "id": int(project["id"]),
                "name": str(project.get("name") or "—"),
                "description": _truncate(str(project.get("description") or ""), 80),
                "created": _short_timestamp(project.get("created_at")),
                "scenarios": scenario_count,
            }
        )
    rows.sort(key=lambda r: r["created"], reverse=True)
    return rows


async def _render_parametric_studies_card(client: ApiClient) -> None:
    """Recent parametric studies across visible projects (design §9).

    Only rendered when the feature flag is on (gated at the call site).
    Click a row → the study detail page; the header button → the
    create workspace.
    """
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Parametric Studies").style(
                "font-size: 1.05rem; font-weight: 600;"
            )
            ui.button(
                "New study",
                icon="science",
                on_click=lambda: ui.navigate.to("/studies"),
            ).props("flat dense color=primary")

        studies: list[dict[str, Any]] = []
        try:
            projects = await client.list_projects()
        except ApiError:
            projects = []
        # {project id: name} so the Project column reads as a name, not an id.
        project_names = {int(p["id"]): str(p.get("name") or p["id"]) for p in projects}
        # One lookup (not per study) so the Tool column shows the label.
        try:
            tool_names = {
                str(t["alias"]): str(t.get("name") or t["alias"]) for t in await client.list_tools()
            }
        except ApiError:
            tool_names = {}
        for project in projects:
            try:
                studies.extend(await client.list_studies(int(project["id"])))
            except ApiError:
                continue
        if not studies:
            ui.label("No studies yet — click 'New study' to start one.").style(
                "color: #888;"
            )
            return
        studies.sort(key=lambda s: str(s.get("created_at") or ""), reverse=True)

        rows = [
            {
                "id": int(s.get("id") or 0),
                "name": str(s.get("name") or "—"),
                "project": project_names.get(int(s.get("project_id") or 0), "—"),
                "tool": tool_names.get(
                    str(s.get("tool_alias") or ""), str(s.get("tool_alias") or "")
                ),
                "runs": (
                    f"{int(s.get('generated_count') or 0)}"
                    + (
                        f" (+{int(s.get('skipped_invalid_count') or 0)} skipped)"
                        if int(s.get("skipped_invalid_count") or 0)
                        else ""
                    )
                ),
                "state": _STUDY_STATE_LABEL.get(
                    str(s.get("state") or ""), str(s.get("state") or "")
                ),
            }
            for s in studies[:25]
        ]
        columns = [
            {"name": "name", "label": "Study", "field": "name", "align": "left", "sortable": True},
            {
                "name": "project",
                "label": "Project",
                "field": "project",
                "align": "left",
                "sortable": True,
            },
            {"name": "tool", "label": "Tool", "field": "tool", "align": "left", "sortable": True},
            {"name": "runs", "label": "Runs", "field": "runs", "align": "left", "sortable": True},
            {
                "name": "state",
                "label": "State",
                "field": "state",
                "align": "left",
                "sortable": True,
            },
        ]
        table = ui.table(columns=columns, rows=rows, row_key="id").classes("w-full")
        table.props("dense flat hide-bottom").style("max-height: 320px; overflow: auto;")
        table.on(
            "row-click",
            lambda e: ui.navigate.to(f"/studies/{e.args[1]['id']}"),
        )


_STUDY_STATE_LABEL: dict[str, str] = {
    "draft": "Draft",
    "pending_approval": "Pending approval",
    "pending_start_approval": "Pending approval",
    "running": "Running",
    "complete": "Complete",
    "partial": "Partial",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "rejected": "Rejected",
}


async def _render_optimisation_studies_card(client: ApiClient) -> None:
    """Recent optimisation studies across visible projects.

    Mirrors :func:`_render_parametric_studies_card`; only rendered when the
    optimisation feature flag is on (gated at the call site). Row → study
    detail; header button → the create workspace. Target shows the tool /
    workflow *label*, not its alias (feedback_use_labels_not_names).
    """
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Optimisation Studies").style("font-size: 1.05rem; font-weight: 600;")
            ui.button(
                "New optimisation",
                icon="tune",
                on_click=lambda: ui.navigate.to("/optimisation"),
            ).props("flat dense color=primary")

        try:
            projects = await client.list_projects()
        except ApiError:
            projects = []
        # {project id: name} so the Project column reads as a name, not an id.
        project_names = {int(p["id"]): str(p.get("name") or p["id"]) for p in projects}
        # One lookup each (not per study) so the Target column can read in
        # human terms without an N+1 fan-out.
        try:
            tool_names = {
                str(t["alias"]): str(t.get("name") or t["alias"]) for t in await client.list_tools()
            }
            wf_names = {
                str(w["alias"]): str(w.get("name") or w["alias"])
                for w in await client.list_workflows()
            }
        except ApiError:
            tool_names, wf_names = {}, {}

        studies: list[dict[str, Any]] = []
        for project in projects:
            try:
                studies.extend(await client.list_optimisation_studies(int(project["id"])))
            except ApiError:
                continue
        if not studies:
            ui.label("No optimisation studies yet — click 'New optimisation' to start one.").style(
                "color: #888;"
            )
            return
        studies.sort(key=lambda s: str(s.get("created_at") or ""), reverse=True)

        rows = []
        for s in studies[:25]:
            alias = str(s.get("tool_alias") or s.get("workflow_alias") or "")
            rows.append(
                {
                    "id": int(s.get("id") or 0),
                    "name": str(s.get("name") or "—"),
                    "project": project_names.get(int(s.get("project_id") or 0), "—"),
                    "target": tool_names.get(alias) or wf_names.get(alias) or alias or "—",
                    "batch": int(s.get("last_batch_number") or 0),
                    "state": _STUDY_STATE_LABEL.get(
                        str(s.get("state") or ""), str(s.get("state") or "")
                    ),
                }
            )
        columns = [
            {"name": "name", "label": "Study", "field": "name", "align": "left", "sortable": True},
            {
                "name": "project",
                "label": "Project",
                "field": "project",
                "align": "left",
                "sortable": True,
            },
            {
                "name": "target",
                "label": "Target",
                "field": "target",
                "align": "left",
                "sortable": True,
            },
            {
                "name": "batch",
                "label": "Batch",
                "field": "batch",
                "align": "left",
                "sortable": True,
            },
            {
                "name": "state",
                "label": "State",
                "field": "state",
                "align": "left",
                "sortable": True,
            },
        ]
        table = ui.table(columns=columns, rows=rows, row_key="id").classes("w-full")
        table.props("dense flat hide-bottom").style("max-height: 320px; overflow: auto;")
        table.on(
            "row-click",
            lambda e: ui.navigate.to(f"/optimisation/{e.args[1]['id']}"),
        )


# ---------- right column -----------------------------------------------------


async def _render_workflow_quick_access(
    client: ApiClient,
    tutorials_by_workflow: dict[str, list[dict[str, Any]]],
    favourites: _FavouritesState,
) -> None:
    """Per-workflow launch card with the same two-button pattern as tools.

    Workflows surface here when the user can see them (state-gated +
    team-gated + the new-tool-cascade rule already enforced by
    ``WorkflowService.list_visible``). Each card carries:

    * **Launch in existing project** — chooser modal scoped to
      scenarios that don't already host a run of this workflow.
    * **Launch in new project** — quick path that creates a fresh
      project + scenario in one go.
    * **Tutorials** — only when active workflow-scoped tutorials
      exist; direct-opens when one, chooser dialog when both
      document + video are active. Same flow as the tool button.

    A search box at the top filters the visible cards live (matching
    name + version + description, case-insensitive). The card list is
    height-capped with an inner scrollbar so the column stays compact
    even on installs with dozens of released workflows.

    When ``favourites.enabled`` is true, each card carries an inline
    star next to the workflow name. Starred items float to the top
    (alphabetical within group); clicking a star toggles the
    favourite optimistically and re-sorts in place without a section
    rebuild — search-box state and scroll position survive.
    """
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label("Workflow Quick Access").style("font-size: 1.05rem; font-weight: 600;")
        try:
            workflows = await client.list_visible_workflows()
        except ApiError as exc:
            ui.label(f"Couldn't load the workflows list: {exc.detail}").style("color: #ff6b6b;")
            return
        if not workflows:
            ui.label(
                "No workflows yet — ask an administrator to define one from "
                "Admin Space → New/Edit workflow."
            ).style("color: #888;")
            return

        icon_data_urls = await _gather_icon_data_urls(
            workflows, fetch=client.get_workflow_icon_bytes
        )

        search = (
            ui.input(placeholder="Search workflows…").props("dense clearable").classes("w-full")
        )
        no_match = ui.label("No workflows match the search.").style(
            "color: #888; font-size: 0.85rem; padding: 0.25rem 0;"
        )
        no_match.set_visibility(False)

        list_box = (
            ui.column()
            .classes("w-full gap-3")
            .style("max-height: 520px; overflow-y: auto; padding-right: 0.25rem;")
        )
        cards_meta: list[dict[str, Any]] = []

        def _sort_key(meta: dict[str, Any]) -> tuple[int, str]:
            # Favourited first (False sorts before True; invert), then
            # alphabetical by display label so the user reads each
            # group in a predictable order.
            return (0 if meta["is_favourite"] else 1, meta["display_label"].lower())

        def _resort() -> None:
            for idx, meta in enumerate(sorted(cards_meta, key=_sort_key)):
                meta["card"].move(list_box, target_index=idx)

        with list_box:
            for wf in workflows:
                alias = str(wf.get("alias") or "")
                display_label = _workflow_display_label(wf)
                wf_tutorials = tutorials_by_workflow.get(alias, [])
                meta: dict[str, Any] = {
                    "alias": alias,
                    "display_label": display_label,
                    "is_favourite": alias in favourites.workflows,
                }
                card = _render_workflow_card(
                    client,
                    wf,
                    wf_tutorials,
                    icon_data_urls.get(alias),
                    display_label=display_label,
                    favourites_enabled=favourites.enabled,
                    meta=meta,
                    on_toggle=lambda _e=None, _m=meta: _toggle_favourite(
                        client, favourites, "workflow", _m, _resort
                    ),
                )
                meta["card"] = card
                searchable = " ".join(
                    [
                        str(wf.get("name") or ""),
                        str(wf.get("alias") or ""),
                        str(wf.get("version") or ""),
                        str(wf.get("description") or ""),
                    ]
                ).lower()
                meta["searchable"] = searchable
                cards_meta.append(meta)

        _resort()

        def _refilter() -> None:
            needle = (search.value or "").strip().lower()
            any_visible = False
            for meta in cards_meta:
                visible = (not needle) or (needle in meta["searchable"])
                meta["card"].set_visibility(visible)
                if visible:
                    any_visible = True
            no_match.set_visibility(not any_visible)

        search.on_value_change(lambda _e: _refilter())


def _workflow_display_label(workflow: dict[str, Any]) -> str:
    """Pretty ``name v<version>`` label or just ``name`` when versionless."""
    name = str(workflow.get("name") or workflow.get("alias") or "")
    version = str(workflow.get("version") or "")
    return f"{name} v{version}" if version else name


def _render_workflow_chain(chain: list[dict[str, Any]]) -> None:
    """Render a workflow's member tools as a flow-ordered chain line.

    Replaces the old bare ``N tools`` count (the user asked to see
    *which* tools, in the order outputs feed inputs — not how many).
    Tools are listed in workflow ``position`` order using their display
    **labels**, never aliases. The separator drawn before each tool is
    an arrow ``→`` when the previous tool's output feeds it
    (``flows_from_prev``), and a neutral dot ``·`` when the two aren't
    directly chained — so a standalone tool reads as separated rather
    than sequenced. Archived members render struck-through and dimmed.

    The chain wraps to multiple lines for big workflows (full chain, no
    truncation). An empty chain (workflow with no resolvable members)
    renders nothing.
    """
    if not chain:
        return
    with ui.row().classes("items-center gap-1").style(
        "flex-wrap: wrap; row-gap: 0.15rem; font-size: 0.8rem;"
    ):
        for idx, entry in enumerate(chain):
            if idx > 0:
                if entry.get("flows_from_prev"):
                    ui.label("→").style("color: #40A1A4; font-weight: 600;")
                else:
                    ui.label("·").style("color: #888;")
            label = str(entry.get("label") or "")
            if entry.get("archived"):
                ui.label(label).style(
                    "color: #888; text-decoration: line-through; opacity: 0.65;"
                ).tooltip("Archived tool")
            else:
                ui.label(label).style("color: #aaa;")


def _render_workflow_card(
    client: ApiClient,
    workflow: dict[str, Any],
    tutorials: list[dict[str, Any]],
    icon_data_url: str | None,
    *,
    display_label: str,
    favourites_enabled: bool,
    meta: dict[str, Any],
    on_toggle: Any,
) -> Any:
    """One Workflow Quick Access card: identity + launch + optional Tutorials.

    Mirrors :func:`_render_tool_card` so the user's eye reads the two
    sections together. ``display_label`` is the workflow's pretty
    ``name v<version>`` — the alias never reaches the user-facing
    surface. The Tutorials button only appears when ``tutorials`` is
    non-empty; it uses the same async chooser-dialog flow as the tool
    card.

    When ``favourites_enabled`` is true, an inline star renders next
    to the display name; clicking it invokes ``on_toggle`` (which is
    expected to do the optimistic flip + API call + re-sort). When
    false the star is omitted entirely — disabling the feature
    leaves no visual residue on existing rows.

    Returns the outer ``ui.card`` so the parent section can toggle its
    visibility from the search-box filter and re-order it from the
    favourites sort without rebuilding the card tree.
    """
    alias = str(workflow.get("alias") or "")
    description = str(workflow.get("description") or "")

    card = ui.card().classes("w-full")
    with card, ui.row().classes("items-center w-full gap-3 no-wrap"):
        with (
            ui.column()
            .classes("items-center justify-center")
            .style("flex: 0 0 96px; width: 96px; height: 96px;")
        ):
            if icon_data_url:
                ui.image(icon_data_url).style("width: 100%; height: 100%; object-fit: contain;")
            else:
                ui.icon("schema").style("font-size: 4rem; color: #40A1A4;")
        with ui.column().classes("gap-1").style("flex: 1 1 auto; min-width: 0;"):
            with ui.row().classes("items-center gap-2 no-wrap"):
                if favourites_enabled:
                    _render_favourite_star(meta=meta, on_toggle=on_toggle)
                ui.label(display_label).style("font-size: 1.05rem; font-weight: 600;")
            if description:
                ui.label(description).style("color: #888; font-size: 0.85rem;")
            _render_workflow_chain(list(workflow.get("member_chain") or []))
        with ui.column().classes("gap-1").style("flex: 0 0 auto;"):
            ui.button(
                "Launch in existing project",
                on_click=lambda _e=None, _a=alias, _n=display_label: _launch_workflow_existing(
                    client, _a, _n
                ),
            ).props("color=primary outline dense").classes("w-full")
            ui.button(
                "Launch in new project",
                on_click=lambda _e=None, _a=alias, _n=display_label: _launch_workflow_new(
                    client, _a, _n
                ),
            ).props("color=primary dense").classes("w-full")
            if tutorials:
                ui.button(
                    "Tutorials",
                    icon="school",
                    on_click=lambda _e=None, _ts=tutorials, _n=display_label: _open_tool_tutorials(
                        client, _ts, _n
                    ),
                ).props("color=primary outline dense").classes("w-full")
    return card


def _launch_workflow_existing(client: ApiClient, workflow_alias: str, display_label: str) -> None:
    """Pick an existing project + scenario, then start the workflow run.

    Thin wrapper over the shared launch dialog (also used by the *My
    Custom Workflows* workspace) — see
    :func:`matofx.ui.workflow_launch.open_launch_in_existing_project_dialog`.
    """
    open_launch_in_existing_project_dialog(client, workflow_alias, display_label)


def _launch_workflow_new(client: ApiClient, workflow_alias: str, display_label: str) -> None:
    """Quick path: create a new project + scenario, then start the workflow."""
    with ui.dialog() as dialog, ui.card().style("min-width: 460px;"):
        ui.label(f"Launch '{display_label}' in a new project").style(
            "font-size: 1.1rem; font-weight: 600;"
        )
        ui.label(
            "Give the new project and its first scenario a name. We'll "
            "create both and start the workflow run."
        ).style("color: #888;")

        project_name = ui.input("Project name *").props("autofocus dense").classes("w-full")
        project_desc = ui.input("Project description (optional)").props("dense").classes("w-full")
        scenario_name = (
            ui.input("Scenario name *", value="Scenario 1").props("dense").classes("w-full")
        )
        meta_box = ui.column().classes("w-full gap-2")
        meta_state: dict[str, Any] = {
            "fields": [],
            "widgets": {},
            "share": {"rows": []},
        }
        message = ui.label("").style("color: #ff6b6b;")

        async def _load_meta() -> None:
            fields = await metadata_form.load_fields(client)
            meta_state["fields"] = fields
            try:
                directory = await client.list_user_directory()
                whoami = await client.me()
            except ApiError:
                directory, whoami = [], {}
            with meta_box:
                if fields:
                    ui.separator()
                    ui.label("Project metadata").style("font-weight: 600;")
                    meta_state["widgets"] = metadata_form.render_fields(fields)
                ui.separator()
                ui.label("Share with (optional)").style("font-weight: 600;")
                meta_state["share"] = share_form.render(
                    directory, int(whoami["user_id"]) if whoami else None
                )

        async def _create_and_launch() -> None:
            if not (project_name.value or "").strip():
                message.text = "Please enter a name for the project."
                return
            if not (scenario_name.value or "").strip():
                message.text = "Please enter a name for the first scenario."
                return
            try:
                selections = metadata_form.collect(meta_state["fields"], meta_state["widgets"])
            except metadata_form.MetadataInputError as exc:
                message.text = str(exc)
                return
            try:
                project = await client.create_project(
                    name=str(project_name.value), description=str(project_desc.value or "")
                )
                if selections or metadata_form.has_required(meta_state["fields"]):
                    await client.set_project_metadata(int(project["id"]), selections=selections)
                await share_form.apply(
                    client,
                    int(project["id"]),
                    share_form.collect(meta_state["share"]),
                )
                scenario = await client.create_scenario(
                    int(project["id"]), name=str(scenario_name.value), description=""
                )
                run = await client.start_workflow_run(
                    int(scenario["id"]), workflow_alias=workflow_alias
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.navigate.to(f"/my-jobs?workflow_run={int(run['id'])}")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create and launch", on_click=_create_and_launch).props("color=primary")
    dialog.open()
    ui.timer(0.05, _load_meta, once=True)


def _open_job_chooser(client: ApiClient) -> None:
    """Project → Scenario → tick tools/workflows → Launch.

    Inverts the earlier item-first chooser: the user picks **where** the
    work goes (project + scenario, with an inline "+ New scenario"
    affordance) and then ticks every tool and admin-released workflow
    they want added to that scenario in one click.

    Items already present in the chosen scenario are filtered out so the
    one-job-per-tool-per-scenario invariant still holds and the same
    workflow can't be queued twice. Each section carries its own search
    box for installs with many installed tools.

    Launching N items creates N drafts / runs in the same scenario; the
    page then navigates to ``/my-jobs`` opened on the first new item so
    the user starts filling in its form straight away.
    """
    state: dict[str, Any] = {
        # Each entry: {"alias", "label", "checkbox", "row", "search"}
        "tool_entries": [],
        "workflow_entries": [],
    }

    with ui.dialog() as dialog, ui.card().style("min-width: 720px; max-width: 95vw;"):
        ui.label("Create Job(s)").style("font-size: 1.15rem; font-weight: 600;")
        ui.label(
            "Pick where the work goes, then tick every tool or workflow "
            "you'd like to add to that scenario."
        ).style("color: #888; font-size: 0.9rem;")

        message = ui.label("").style("color: var(--matofx-error-text);")

        # ---- Project + Scenario row -----------------------------------
        with ui.row().classes("w-full items-end gap-2 no-wrap"):
            project_select = (
                ui.select(options={}, label="Project", with_input=True)
                .props("dense")
                .style("flex: 1 1 0; min-width: 0;")
            )
            scenario_select = (
                ui.select(options={}, label="Scenario", with_input=True)
                .props("disable dense")
                .style("flex: 1 1 0; min-width: 0;")
            )
            new_scenario_btn = ui.button("New scenario", icon="add").props(
                "flat dense color=primary disable"
            )

        new_scenario_panel = ui.row().classes("w-full items-end gap-2 no-wrap")
        new_scenario_panel.set_visibility(False)
        with new_scenario_panel:
            new_scenario_name = (
                ui.input("New scenario name", value="Scenario 1")
                .props("dense autofocus")
                .style("flex: 1 1 0; min-width: 0;")
            )
            create_scenario_btn = ui.button("Create").props("color=primary dense")
            cancel_new_scenario_btn = ui.button("Cancel").props("flat dense")

        # ---- Catalog (tools + workflows, hidden until scenario picked) -
        empty_hint = ui.label("Pick a project and scenario above to see what you can add.").style(
            "color: #888; padding: 0.5rem 0;"
        )

        catalog = ui.column().classes("w-full gap-3")
        catalog.set_visibility(False)
        with catalog:
            with ui.column().classes("w-full gap-1"):
                ui.label("Tools").style("font-weight: 600; margin-top: 0.5rem;")
                tool_search = (
                    ui.input(placeholder="Search tools…").props("dense clearable").classes("w-full")
                )
                tool_empty = ui.label("").style(
                    "color: #888; font-size: 0.85rem; padding: 0.25rem 0;"
                )
                tool_empty.set_visibility(False)
                tool_list = (
                    ui.column()
                    .classes("w-full gap-0")
                    .style(
                        "max-height: 220px; overflow-y: auto; "
                        "border: 1px solid rgba(255,255,255,0.15); "
                        "border-radius: 4px; padding: 0.25rem;"
                    )
                )

            with ui.column().classes("w-full gap-1"):
                ui.label("Workflows").style("font-weight: 600; margin-top: 0.5rem;")
                workflow_search = (
                    ui.input(placeholder="Search workflows…")
                    .props("dense clearable")
                    .classes("w-full")
                )
                workflow_empty = ui.label("").style(
                    "color: #888; font-size: 0.85rem; padding: 0.25rem 0;"
                )
                workflow_empty.set_visibility(False)
                workflow_list = (
                    ui.column()
                    .classes("w-full gap-0")
                    .style(
                        "max-height: 220px; overflow-y: auto; "
                        "border: 1px solid rgba(255,255,255,0.15); "
                        "border-radius: 4px; padding: 0.25rem;"
                    )
                )

        with ui.row().classes("w-full justify-between items-center"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            launch_btn = ui.button("Launch").props("color=primary disable").classes("w-32")

        # ---- helpers --------------------------------------------------

        # All scenario names in the picked project, for the next-number
        # default on the "New scenario" input.
        scenario_names: list[str] = []

        async def _load_projects() -> None:
            try:
                projects = await client.list_projects()
            except ApiError as exc:
                message.text = exc.detail
                return
            project_select.options = {int(p["id"]): str(p["name"]) for p in projects}
            project_select.update()

        def _hide_catalog() -> None:
            catalog.set_visibility(False)
            empty_hint.set_visibility(True)
            launch_btn.props("disable color=primary")
            launch_btn.text = "Launch"

        def _show_catalog() -> None:
            catalog.set_visibility(True)
            empty_hint.set_visibility(False)

        async def _on_project_change(_e: Any = None) -> None:
            scenario_select.value = None
            scenario_select.options = {}
            scenario_select.props("disable dense")
            new_scenario_btn.props("disable")
            _hide_new_scenario_panel()
            _hide_catalog()
            message.text = ""
            if project_select.value is None:
                scenario_select.update()
                return
            try:
                scenarios = await client.list_scenarios(int(project_select.value))
            except ApiError as exc:
                message.text = exc.detail
                return
            scenario_select.options = {int(s["id"]): str(s.get("name") or "—") for s in scenarios}
            scenario_names[:] = [str(s.get("name") or "") for s in scenarios]
            new_scenario_btn.props(remove="disable")
            if scenario_select.options:
                scenario_select.props(remove="disable")
            scenario_select.update()

        async def _on_scenario_change(_e: Any = None) -> None:
            if scenario_select.value is None:
                _hide_catalog()
                return
            await _populate_catalog(int(scenario_select.value))

        async def _populate_catalog(scenario_id: int) -> None:
            message.text = ""
            try:
                tools = await client.list_tools()
            except ApiError as exc:
                message.text = f"Couldn't load tools: {exc.detail}"
                tools = []
            try:
                workflows = await client.list_visible_workflows()
            except ApiError as exc:
                message.text = (message.text + " ").strip() + (f" Workflows: {exc.detail}")
                workflows = []
            try:
                jobs = await client.list_jobs(scenario_id)
            except ApiError:
                jobs = []
            try:
                runs = await client.list_workflow_runs_for_scenario(scenario_id)
            except ApiError:
                runs = []

            existing_tool_aliases = {str(j.get("tool_alias") or "") for j in jobs}
            existing_workflow_aliases = {str(r.get("workflow_alias") or "") for r in runs}

            # ---- Tools -----------------------------------------------
            tool_list.clear()
            state["tool_entries"] = []
            available_tools = [
                t for t in tools if str(t.get("alias") or "") not in existing_tool_aliases
            ]
            if not available_tools:
                tool_empty.text = (
                    "Every visible tool is already in this scenario."
                    if tools
                    else "No tools available."
                )
                tool_empty.set_visibility(True)
            else:
                tool_empty.set_visibility(False)
                with tool_list:
                    for tool in available_tools:
                        _add_catalog_entry(tool, state["tool_entries"], _refresh_launch_state)

            # ---- Workflows -------------------------------------------
            workflow_list.clear()
            state["workflow_entries"] = []
            available_workflows = [
                w for w in workflows if str(w.get("alias") or "") not in existing_workflow_aliases
            ]
            if not available_workflows:
                workflow_empty.text = (
                    "Every visible workflow is already in this scenario."
                    if workflows
                    else "No workflows available."
                )
                workflow_empty.set_visibility(True)
            else:
                workflow_empty.set_visibility(False)
                with workflow_list:
                    for wf in available_workflows:
                        _add_catalog_entry(wf, state["workflow_entries"], _refresh_launch_state)

            _show_catalog()
            _refresh_search()
            _refresh_launch_state()

        def _refresh_search() -> None:
            t_needle = (tool_search.value or "").strip().lower()
            for entry in state["tool_entries"]:
                visible = (not t_needle) or (t_needle in entry["search"])
                entry["row"].set_visibility(visible)
            w_needle = (workflow_search.value or "").strip().lower()
            for entry in state["workflow_entries"]:
                visible = (not w_needle) or (w_needle in entry["search"])
                entry["row"].set_visibility(visible)

        def _refresh_launch_state() -> None:
            picked = sum(1 for e in state["tool_entries"] if e["checkbox"].value) + sum(
                1 for e in state["workflow_entries"] if e["checkbox"].value
            )
            if picked > 0:
                launch_btn.props(remove="disable")
                launch_btn.text = f"Launch ({picked})"
            else:
                launch_btn.props("disable color=primary")
                launch_btn.text = "Launch"

        def _show_new_scenario_panel() -> None:
            new_scenario_panel.set_visibility(True)
            new_scenario_name.value = next_scenario_name(scenario_names)

        def _hide_new_scenario_panel() -> None:
            new_scenario_panel.set_visibility(False)

        async def _create_new_scenario() -> None:
            name = (new_scenario_name.value or "").strip()
            if not name:
                message.text = "Please enter a name for the new scenario."
                return
            project_id = project_select.value
            if project_id is None:
                message.text = "Please pick a project first."
                return
            try:
                scenario = await client.create_scenario(int(project_id), name=name, description="")
            except ApiError as exc:
                message.text = exc.detail
                return
            sid = int(scenario["id"])
            _hide_new_scenario_panel()
            try:
                scenarios = await client.list_scenarios(int(project_id))
            except ApiError as exc:
                message.text = exc.detail
                return
            scenario_select.options = {int(s["id"]): str(s.get("name") or "—") for s in scenarios}
            scenario_names[:] = [str(s.get("name") or "") for s in scenarios]
            scenario_select.props(remove="disable")
            scenario_select.value = sid
            scenario_select.update()
            # Value-change handler will populate the catalog.

        async def _launch_all() -> None:
            scenario_id = scenario_select.value
            if scenario_id is None:
                message.text = "Please pick a scenario."
                return
            sid = int(scenario_id)
            picked_tools = [e for e in state["tool_entries"] if e["checkbox"].value]
            picked_workflows = [e for e in state["workflow_entries"] if e["checkbox"].value]
            if not picked_tools and not picked_workflows:
                message.text = "Please tick at least one tool or workflow."
                return

            launch_btn.props("disable color=primary")
            launch_btn.text = "Launching…"

            errors: list[str] = []
            first_job_id: int | None = None
            first_workflow_run_id: int | None = None

            for entry in picked_tools:
                try:
                    job = await client.create_or_get_draft_job(sid, tool_alias=entry["alias"])
                    if first_job_id is None:
                        first_job_id = int(job["id"])
                except ApiError as exc:
                    errors.append(f"{entry['label']}: {exc.detail}")

            for entry in picked_workflows:
                try:
                    run = await client.start_workflow_run(sid, workflow_alias=entry["alias"])
                    if first_workflow_run_id is None:
                        first_workflow_run_id = int(run["id"])
                except ApiError as exc:
                    errors.append(f"{entry['label']}: {exc.detail}")

            if errors:
                message.text = " | ".join(errors)
                _refresh_launch_state()
                return

            dialog.close()
            # Land on the first new item so the user starts filling its
            # form / sees its tabs straight away. Tools win over
            # workflows when both kinds were ticked — the workspace's
            # Job-ID picker pivots cleanly to the workflow tabs from the
            # standalone job view if the user wants the workflow next.
            if first_job_id is not None:
                ui.navigate.to(f"/my-jobs?job={first_job_id}")
            elif first_workflow_run_id is not None:
                ui.navigate.to(f"/my-jobs?workflow_run={first_workflow_run_id}")

        # ---- wire events ---------------------------------------------
        project_select.on_value_change(_on_project_change)
        scenario_select.on_value_change(_on_scenario_change)
        new_scenario_btn.on_click(_show_new_scenario_panel)
        create_scenario_btn.on_click(_create_new_scenario)
        cancel_new_scenario_btn.on_click(_hide_new_scenario_panel)
        tool_search.on_value_change(lambda _e: _refresh_search())
        workflow_search.on_value_change(lambda _e: _refresh_search())
        launch_btn.on_click(_launch_all)

        ui.timer(0.05, _load_projects, once=True)
    dialog.open()


def _add_catalog_entry(
    item: dict[str, Any],
    bucket: list[dict[str, Any]],
    on_check: Any,
) -> None:
    """Append one tickbox row for a tool / workflow into ``bucket``.

    Captures the row, checkbox, and a lowercased search blob so the
    chooser can toggle visibility on each keystroke without rebuilding
    the row tree (preserves checkbox state during filtering, per the
    "set_visibility keeps focus stable" rule).
    """
    alias = str(item.get("alias") or "")
    name = str(item.get("name") or alias)
    version = str(item.get("version") or "")
    # Tools carry ``short_description`` post-split; workflows still use a
    # single ``description``. Prefer the former, fall back to the latter
    # so this shared row works for both pickers.
    description = str(item.get("short_description") or item.get("description") or "")
    label = f"{name} v{version}" if version else name
    with (
        ui.row()
        .classes("w-full items-center gap-2 no-wrap")
        .style("padding: 0.15rem 0.25rem;") as row
    ):
        checkbox = ui.checkbox(label, on_change=lambda _e: on_check())
        if description:
            checkbox.tooltip(description)
    searchable = f"{label} {description}".lower()
    bucket.append(
        {
            "alias": alias,
            "label": label,
            "checkbox": checkbox,
            "row": row,
            "search": searchable,
        }
    )


async def _render_tool_quick_access(
    client: ApiClient,
    tutorials_by_tool: dict[str, list[dict[str, Any]]],
    favourites: _FavouritesState,
) -> None:
    """Card per tool with the legacy two-launch-button affordance.

    A search box at the top filters the visible cards live (matching
    name + version + description, case-insensitive). The card list is
    height-capped with an inner scrollbar so the column stays compact
    on installs with dozens of installed tools.

    Star UX mirrors :func:`_render_workflow_quick_access`: when
    ``favourites.enabled`` is true, each card carries an inline star
    that floats the tool to the top of the section on toggle.
    """
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label("Single Tool Quick Access").style("font-size: 1.05rem; font-weight: 600;")
        try:
            tools = await client.list_tools()
        except ApiError as exc:
            ui.label(f"Couldn't load the tools list: {exc.detail}").style("color: #ff6b6b;")
            return
        if not tools:
            ui.label(
                "No tools installed yet — ask an administrator to install one from "
                "the Admin Space → Tools page."
            ).style("color: #888;")
            return

        # Fetch every uploaded icon concurrently before drawing the cards.
        # Tools whose descriptor advertises an icon_filename but whose
        # file is missing on disk silently fall back to the default
        # wrench (the endpoint 404s, which becomes None below).
        icon_data_urls = await _gather_icon_data_urls(tools, fetch=client.get_tool_icon_bytes)

        search = ui.input(placeholder="Search tools…").props("dense clearable").classes("w-full")
        no_match = ui.label("No tools match the search.").style(
            "color: #888; font-size: 0.85rem; padding: 0.25rem 0;"
        )
        no_match.set_visibility(False)

        list_box = (
            ui.column()
            .classes("w-full gap-3")
            .style("max-height: 520px; overflow-y: auto; padding-right: 0.25rem;")
        )
        cards_meta: list[dict[str, Any]] = []

        def _sort_key(meta: dict[str, Any]) -> tuple[int, str]:
            return (0 if meta["is_favourite"] else 1, meta["display_label"].lower())

        def _resort() -> None:
            for idx, meta in enumerate(sorted(cards_meta, key=_sort_key)):
                meta["card"].move(list_box, target_index=idx)

        with list_box:
            for tool in tools:
                alias = str(tool.get("alias") or "")
                display_label = _tool_display_label(tool)
                tool_tutorials = tutorials_by_tool.get(alias, [])
                meta: dict[str, Any] = {
                    "alias": alias,
                    "display_label": display_label,
                    "is_favourite": alias in favourites.tools,
                }
                card = _render_tool_card(
                    client,
                    tool,
                    tool_tutorials,
                    icon_data_urls.get(alias),
                    display_label=display_label,
                    favourites_enabled=favourites.enabled,
                    meta=meta,
                    on_toggle=lambda _e=None, _m=meta: _toggle_favourite(
                        client, favourites, "tool", _m, _resort
                    ),
                )
                meta["card"] = card
                searchable = " ".join(
                    [
                        str(tool.get("name") or ""),
                        str(tool.get("alias") or ""),
                        str(tool.get("version") or ""),
                        str(tool.get("short_description") or ""),
                        str(tool.get("long_description") or ""),
                    ]
                ).lower()
                meta["searchable"] = searchable
                cards_meta.append(meta)

        _resort()

        def _refilter() -> None:
            needle = (search.value or "").strip().lower()
            any_visible = False
            for meta in cards_meta:
                visible = (not needle) or (needle in meta["searchable"])
                meta["card"].set_visibility(visible)
                if visible:
                    any_visible = True
            no_match.set_visibility(not any_visible)

        search.on_value_change(lambda _e: _refilter())


def _tool_display_label(tool: dict[str, Any]) -> str:
    """Pretty ``name v<version>`` label or just ``name`` when versionless."""
    name = str(tool.get("name") or tool.get("alias") or "")
    version = str(tool.get("version") or "")
    return f"{name} v{version}" if version else name


def _render_tool_card(
    client: ApiClient,
    tool: dict[str, Any],
    tutorials: list[dict[str, Any]],
    icon_data_url: str | None,
    *,
    display_label: str,
    favourites_enabled: bool,
    meta: dict[str, Any],
    on_toggle: Any,
) -> Any:
    """One quick-launch card: icon + name/version/description + two launch buttons.

    Card label is the tool's *name + version* (e.g. ``2D Heat Transfer
    v1.0.0``) — never the alias — so users distinguish runs against
    different installed versions of the same tool without opening a
    job, per the locked pretty-printing rule.

    ``icon_data_url`` is the uploaded icon embedded as a ``data:``
    URL (so the bearer-token-only endpoint doesn't have to be hit
    again per ``<img>``). When ``None`` the card falls back to the
    default wrench glyph — same fallback is triggered when the
    descriptor advertises an icon but the file on disk is missing.

    When ``favourites_enabled`` is true an inline star renders next
    to the display label; clicking it invokes ``on_toggle`` (which
    handles the optimistic flip + API call + re-sort). When false
    the star is omitted entirely.

    Returns the outer ``ui.card`` so the parent section can toggle its
    visibility from the search-box filter and re-order it from the
    favourites sort without rebuilding the card tree.
    """
    alias = str(tool.get("alias") or "")
    # The card stays tidy: only the mandatory short_description shows as
    # the subtitle. The optional long_description (when authored) is one
    # click away behind the info button — see _show_tool_info_dialog.
    short_description = str(tool.get("short_description") or "")
    long_description = str(tool.get("long_description") or "")
    card = ui.card().classes("w-full")
    with card, ui.row().classes("items-center w-full gap-3 no-wrap"):
        with (
            ui.column()
            .classes("items-center justify-center")
            .style("flex: 0 0 96px; width: 96px; height: 96px;")
        ):
            if icon_data_url:
                ui.image(icon_data_url).style("width: 100%; height: 100%; object-fit: contain;")
            else:
                ui.icon("build").style("font-size: 4rem; color: #40A1A4;")
        with ui.column().classes("gap-1").style("flex: 1 1 auto; min-width: 0;"):
            with ui.row().classes("items-center gap-2 no-wrap"):
                if favourites_enabled:
                    _render_favourite_star(meta=meta, on_toggle=on_toggle)
                ui.label(display_label).style("font-size: 1.05rem; font-weight: 600;")
                # Info affordance (design §②): only when the author wrote a
                # long description — otherwise the short one on the card is
                # already the whole story and a button would just be noise.
                if long_description:
                    # display_label / long_description are stable in this
                    # per-card scope (not a loop var), so no default-arg
                    # capture is needed.
                    ui.icon("info").classes("cursor-pointer").style(
                        "font-size: 1.1rem; color: #40A1A4;"
                    ).on(
                        "click",
                        lambda _e=None: _show_tool_info_dialog(display_label, long_description),
                    ).tooltip("More about this tool")
            if short_description:
                ui.label(short_description).style("color: #888; font-size: 0.85rem;")
        with ui.column().classes("gap-1").style("flex: 0 0 auto;"):
            ui.button(
                "Launch in existing project",
                on_click=lambda _e=None, _a=alias, _n=display_label: _launch_existing_project(
                    _a, _n
                ),
            ).props("color=primary outline dense").classes("w-full")
            ui.button(
                "Launch in new project",
                on_click=lambda _e=None, _a=alias, _n=display_label: _launch_new_project(_a, _n),
            ).props("color=primary dense").classes("w-full")
            if tutorials:
                ui.button(
                    "Tutorials",
                    icon="school",
                    on_click=lambda _e=None, _ts=tutorials, _n=display_label: _open_tool_tutorials(
                        client, _ts, _n
                    ),
                ).props("color=primary outline dense").classes("w-full")
    return card


def _show_tool_info_dialog(display_label: str, long_description: str) -> None:
    """Modal showing a tool's full ``long_description`` (design §②).

    Opened from the Quick Access card's info button. Browsing users who
    want more than the one-line short_description before committing to a
    launch read the fuller text here, without the card list having to
    carry a paragraph per tool. Plain-text only (``white-space:
    pre-line`` preserves authored line breaks) — markdown is a later
    call, see ``docs/admin/30-adding-tools.md``.
    """
    with ui.dialog() as dialog, ui.card().style("min-width: 420px; max-width: 640px;"):
        ui.label(display_label).style("font-size: 1.1rem; font-weight: 600;")
        ui.label(long_description).style("white-space: pre-line; color: #444;")
        with ui.row().classes("w-full justify-end"):
            ui.button("Close", on_click=dialog.close).props("flat color=primary")
    dialog.open()


def _launch_existing_project(alias: str, display_label: str) -> None:
    """Modal: pick an existing project + scenario, then jump to the submit form.

    The Scenario dropdown only shows scenarios in the chosen Project
    where this Tool is **not already used** — one tool per scenario is
    the locked invariant (see ``feedback_my_jobs_unified_workspace.md``
    polish round, 2026-05-08). A "+ New scenario" affordance below the
    dropdown lets the user create a fresh scenario in the same project
    without leaving the dialog.

    ``display_label`` is the tool's pretty name + version (e.g.
    ``2D Heat Transfer v1.0.0``), surfaced in the dialog title so the
    user is never shown the bare alias.
    """
    client = ApiClient(get_app(), token=current_token())

    with ui.dialog() as dialog, ui.card().style("min-width: 480px;"):
        ui.label(f"Launch '{display_label}' in an existing project").style(
            "font-size: 1.1rem; font-weight: 600;"
        )
        ui.label(
            "Pick the project and scenario the new job belongs to. "
            "Scenarios that already have a run of this tool are hidden — "
            "click 'New scenario' to add one."
        ).style("color: #888;")

        project_select = (
            ui.select(options={}, label="Project", with_input=True).classes("w-full").props("dense")
        )
        scenario_select = (
            ui.select(options={}, label="Scenario", with_input=True)
            .classes("w-full")
            .props("disable dense")
        )
        no_scenarios_hint = ui.label("").style("color: #888; font-size: 0.85rem;")
        no_scenarios_hint.visible = False

        # New-scenario inline panel — hidden by default, revealed when
        # the user clicks "New scenario". Creating it disables the
        # existing-scenario dropdown so the two paths can't fight.
        with ui.row().classes("w-full items-center gap-2"):
            new_scenario_btn = ui.button("New scenario", icon="add").props(
                "flat dense color=primary disable"
            )
        new_scenario_panel = ui.column().classes("w-full gap-1")
        new_scenario_panel.visible = False
        with new_scenario_panel:
            new_scenario_name = (
                ui.input("New scenario name", value="Scenario 1").props("dense").classes("w-full")
            )

        message = ui.label("").style("color: #ff6b6b;")
        launch_btn = (
            ui.button(
                "Launch",
                on_click=lambda: _launch_now(),
            )
            .props("color=primary disable")
            .classes("w-32")
        )

        async def _load_projects() -> None:
            try:
                projects = await client.list_projects()
            except ApiError as exc:
                message.text = exc.detail
                return
            project_select.options = {int(p["id"]): str(p["name"]) for p in projects}
            project_select.update()

        # All scenario names in the picked project, kept so the "New
        # scenario" default can suggest the next number even though the
        # dropdown above only shows the ones not already running this tool.
        scenario_names: list[str] = []

        async def _load_scenarios() -> None:
            """Refresh the scenario dropdown for the currently-picked project.

            Filters out scenarios that already have a job with this
            tool — the one-tool-per-scenario invariant means launching
            into one of those would only land on the existing draft.
            """
            project_id = project_select.value
            scenario_select.value = None
            scenario_select.options = {}
            scenario_select.props("disable dense")
            new_scenario_btn.props("disable")
            _hide_new_scenario_panel()
            no_scenarios_hint.visible = False
            launch_btn.props("disable color=primary")
            if project_id is None:
                scenario_select.update()
                return
            try:
                scenarios = await client.list_scenarios(int(project_id))
            except ApiError as exc:
                message.text = exc.detail
                return

            # Walk each scenario's jobs to figure out which ones already
            # carry this tool. Cheap because one launch dialog is open
            # at a time and projects rarely have hundreds of scenarios.
            available: dict[int, str] = {}
            for scenario in scenarios:
                sid = int(scenario["id"])
                try:
                    jobs = await client.list_jobs(sid)
                except ApiError:
                    jobs = []
                if any(str(j.get("tool_alias")) == alias for j in jobs):
                    continue
                available[sid] = str(scenario.get("name") or "—")

            scenario_names[:] = [str(s.get("name") or "") for s in scenarios]
            new_scenario_name.value = next_scenario_name(scenario_names)
            scenario_select.options = available
            new_scenario_btn.props(remove="disable")
            if available:
                scenario_select.props(remove="disable")
                no_scenarios_hint.visible = False
            else:
                no_scenarios_hint.text = (
                    "Every scenario in this project already has a run of this tool. "
                    "Click 'New scenario' to add a fresh one."
                )
                no_scenarios_hint.visible = True
            scenario_select.update()

        def _show_new_scenario_panel() -> None:
            new_scenario_panel.visible = True
            scenario_select.value = None
            scenario_select.props("disable dense")
            scenario_select.update()
            _refresh_launch_state()

        def _hide_new_scenario_panel() -> None:
            new_scenario_panel.visible = False
            new_scenario_name.value = next_scenario_name(scenario_names)
            if project_select.value is not None and scenario_select.options:
                scenario_select.props(remove="disable")
            scenario_select.update()

        def _refresh_launch_state() -> None:
            """Enable Launch when either path has a usable selection."""
            if new_scenario_panel.visible:
                ok = bool((new_scenario_name.value or "").strip())
            else:
                ok = scenario_select.value is not None
            if ok:
                launch_btn.props(remove="disable")
            else:
                launch_btn.props("disable color=primary")

        async def _launch_now() -> None:
            if new_scenario_panel.visible:
                # Create-then-launch path.
                name = (new_scenario_name.value or "").strip()
                if not name:
                    message.text = "Please enter a name for the new scenario."
                    return
                project_id = project_select.value
                if project_id is None:
                    message.text = "Please pick a project."
                    return
                try:
                    scenario = await client.create_scenario(
                        int(project_id), name=name, description=""
                    )
                except ApiError as exc:
                    message.text = exc.detail
                    return
                _navigate_to_submit(int(scenario["id"]), alias, dialog)
                return
            # Existing-scenario path.
            _navigate_to_submit(scenario_select.value, alias, dialog)

        project_select.on_value_change(lambda _e: _load_scenarios())
        scenario_select.on_value_change(lambda _e: _refresh_launch_state())
        new_scenario_name.on_value_change(lambda _e: _refresh_launch_state())
        new_scenario_btn.on_click(lambda: _show_new_scenario_panel())

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
        ui.timer(0.05, _load_projects, once=True)

    dialog.open()


def _launch_new_project(alias: str, display_label: str) -> None:
    """Modal: name a new project + scenario, create both, jump to the submit form.

    ``display_label`` is the tool's pretty name + version so the dialog
    title doesn't surface the bare alias.
    """
    client = ApiClient(get_app(), token=current_token())

    with ui.dialog() as dialog, ui.card().style("min-width: 460px;"):
        ui.label(f"Launch '{display_label}' in a new project").style(
            "font-size: 1.1rem; font-weight: 600;"
        )
        ui.label(
            "Give the new project and its first scenario a name. We'll create both, "
            "then take you straight to the submit form."
        ).style("color: #888;")

        project_name = ui.input("Project name *").props("autofocus dense").classes("w-full")
        project_desc = ui.input("Project description (optional)").props("dense").classes("w-full")
        scenario_name = (
            ui.input("Scenario name *", value="Scenario 1").props("dense").classes("w-full")
        )
        meta_box = ui.column().classes("w-full gap-2")
        meta_state: dict[str, Any] = {
            "fields": [],
            "widgets": {},
            "share": {"rows": []},
        }
        message = ui.label("").style("color: #ff6b6b;")

        async def _load_meta() -> None:
            fields = await metadata_form.load_fields(client)
            meta_state["fields"] = fields
            try:
                directory = await client.list_user_directory()
                whoami = await client.me()
            except ApiError:
                directory, whoami = [], {}
            with meta_box:
                if fields:
                    ui.separator()
                    ui.label("Project metadata").style("font-weight: 600;")
                    meta_state["widgets"] = metadata_form.render_fields(fields)
                ui.separator()
                ui.label("Share with (optional)").style("font-weight: 600;")
                meta_state["share"] = share_form.render(
                    directory, int(whoami["user_id"]) if whoami else None
                )

        async def _create_and_launch() -> None:
            if not (project_name.value or "").strip():
                message.text = "Please enter a name for the project."
                return
            if not (scenario_name.value or "").strip():
                message.text = "Please enter a name for the first scenario."
                return
            try:
                selections = metadata_form.collect(meta_state["fields"], meta_state["widgets"])
            except metadata_form.MetadataInputError as exc:
                message.text = str(exc)
                return
            try:
                project = await client.create_project(
                    name=project_name.value, description=project_desc.value or ""
                )
                if selections or metadata_form.has_required(meta_state["fields"]):
                    await client.set_project_metadata(int(project["id"]), selections=selections)
                await share_form.apply(
                    client,
                    int(project["id"]),
                    share_form.collect(meta_state["share"]),
                )
                scenario = await client.create_scenario(
                    int(project["id"]), name=scenario_name.value, description=""
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.navigate.to(f"/scenarios/{scenario['id']}/submit?tool={alias}")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create and launch", on_click=_create_and_launch).props("color=primary")

    dialog.open()
    ui.timer(0.05, _load_meta, once=True)


def _navigate_to_submit(scenario_id: Any, alias: str, dialog: ui.dialog) -> None:
    """Close the dialog and jump to ``/scenarios/{id}/submit?tool=<alias>``."""
    if scenario_id is None:
        return
    dialog.close()
    ui.navigate.to(f"/scenarios/{int(scenario_id)}/submit?tool={alias}")


# ---------- tutorials helpers -----------------------------------------------


_MEDIA_TYPE_ICON: dict[str, str] = {
    "document": "picture_as_pdf",
    "video": "play_circle",
}
_MEDIA_TYPE_LABEL: dict[str, str] = {
    "document": "Document",
    "video": "Video",
}


async def _open_tool_tutorials(
    client: ApiClient,
    tutorials: list[dict[str, Any]],
    display_label: str,
) -> None:
    """Click handler for a Quick Access card's *Tutorials* button.

    Direct-opens when only one tutorial is active for the tool; opens
    a small chooser dialog when both a document and a video are
    available (the typical case for a fully-documented tool).
    """
    if len(tutorials) == 1:
        await _open_tutorial(client, int(tutorials[0]["id"]))
        return
    _show_tutorial_chooser(client, tutorials, display_label)


def _show_tutorial_chooser(
    client: ApiClient,
    tutorials: list[dict[str, Any]],
    display_label: str,
) -> None:
    """Small dialog: one button per available tutorial; closes on selection.

    Used when the tool has *both* an active document and an active
    video; the user picks which to open. Each click runs the same
    blob-URL open as a single-tutorial direct click.
    """
    with ui.dialog() as dialog, ui.card().style("min-width: 360px;"):
        ui.label(f"Open tutorial for '{display_label}'").style(
            "font-size: 1.05rem; font-weight: 600;"
        )
        ui.label("Pick which one you'd like to view.").style("color: #888;")
        for tutorial in tutorials:
            media_type = str(tutorial.get("media_type") or "")
            icon = _MEDIA_TYPE_ICON.get(media_type, "description")
            label = _MEDIA_TYPE_LABEL.get(media_type, media_type.title())
            title = str(tutorial.get("title") or label)
            tutorial_id = int(tutorial["id"])

            async def _click(_e: Any = None, _id: int = tutorial_id) -> None:
                dialog.close()
                await _open_tutorial(client, _id)

            ui.button(
                f"{label}: {title}",
                icon=icon,
                on_click=_click,
            ).props("color=primary").classes("w-full")
        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
    dialog.open()


async def _open_tutorial(client: ApiClient, tutorial_id: int) -> None:
    """Stream the bytes via the authenticated API client and open in a new tab.

    The file endpoint requires a bearer token; a plain
    ``<a target='_blank'>`` would lose it. The blob-URL pattern is the
    same one the compare-report PDF button uses (see
    ``feedback_no_kaleido_browser_print_pdf.md``).
    """
    import base64  # noqa: PLC0415
    import json  # noqa: PLC0415

    try:
        async with await client._client() as http:  # noqa: SLF001
            response = await http.get(
                f"/api/tutorials/{tutorial_id}/file",
                headers=client._headers(),  # noqa: SLF001
            )
            response.raise_for_status()
            data = response.content
            mime = response.headers.get("content-type", "application/octet-stream")
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Couldn't open the tutorial: {exc}", type="negative")
        return

    b64 = base64.b64encode(data).decode("ascii")
    js = (
        "(function() {"
        f"  const b64 = {json.dumps(b64)};"
        f"  const mime = {json.dumps(mime)};"
        "  const bin = atob(b64);"
        "  const bytes = new Uint8Array(bin.length);"
        "  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);"
        "  const blob = new Blob([bytes], {type: mime});"
        "  const url = URL.createObjectURL(blob);"
        "  window.open(url, '_blank');"
        "})();"
    )
    ui.run_javascript(js)


# ---------- icon fetching ---------------------------------------------------


async def _gather_icon_data_urls(
    items: list[dict[str, Any]],
    *,
    fetch: Any,
) -> dict[str, str]:
    """Concurrently fetch every advertised icon and embed as a ``data:`` URL.

    Skips items whose summary doesn't carry ``icon_filename`` (no icon
    uploaded — fall back to the default glyph). Items where the
    descriptor *does* advertise an icon but the file 404s on disk
    return ``None`` from ``fetch`` and also fall back — same
    behaviour as "no icon uploaded".

    Returned mapping is keyed by alias; missing entries are absent
    rather than ``None`` to keep ``.get(alias)`` semantics clean at
    the caller.
    """
    aliases_to_fetch = [
        str(it.get("alias") or "") for it in items if it.get("icon_filename") and it.get("alias")
    ]
    if not aliases_to_fetch:
        return {}

    async def _one(alias: str) -> tuple[str, str | None]:
        try:
            result = await fetch(alias)
        except Exception:  # noqa: BLE001
            return alias, None
        if not result:
            return alias, None
        raw, mime = result
        b64 = base64.b64encode(raw).decode("ascii")
        return alias, f"data:{mime};base64,{b64}"

    results = await asyncio.gather(*(_one(a) for a in aliases_to_fetch))
    return {alias: url for alias, url in results if url is not None}


# ---------- shared formatting -----------------------------------------------


def _short_timestamp(value: Any) -> str:
    """``YYYY-MM-DD HH:MM`` for ISO 8601 strings; pass-through otherwise."""
    if value is None:
        return ""
    text = str(value)
    if "T" in text and len(text) >= 16:
        return text[:16].replace("T", " ")
    return text


def _truncate(text: str, max_len: int) -> str:
    """Trim long descriptions so the table column doesn't blow up."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"
