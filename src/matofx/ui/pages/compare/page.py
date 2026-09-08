"""Compare workspace page (``/ui/compare``).

Two tabs:

- **Select** — pick what to compare. Plain-English mode radio: "Jobs of
  one tool", "Runs of one workflow", or "Scenarios" (scenario rendering
  lands in a follow-up). Choose a set, tick 2-6 items, hit "Compare".
  Friendly violation messages, never silent failure.
- **Compare** — the side-by-side. CSV overlays at the top of each row
  by default; below them the per-job cells laid out in the descriptor's
  group order, with differing values bold + accent-red. The "Show only
  differences" toggle in the toolbar collapses each cell to just the
  rows that changed.

URL state is parsed via :mod:`.selection` so deep links and the My-Jobs
floating-bar entry path land on the right tab without bespoke routing
in either caller. The data fetch for the Compare tab is parallelised
(``asyncio.gather``) — one round-trip per artefact rather than serial
waits, which matters once the user picks five or six jobs.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from nicegui import run as ng_run
from nicegui import ui

from matofx.ui._features import vv_tracking_enabled
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in, section_card
from matofx.ui.pages.compare.diff import JobBundle, compute_differing_keys
from matofx.ui.pages.compare.field_overlay import JobFieldOutput
from matofx.ui.pages.compare.overlay import (
    JobCsvOutput,
    JobHtmlOutput,
    render_html_overlay_section,
    render_overlay_section,
)
from matofx.ui.pages.compare.render import (
    JobCellModel,
    render_job_header,
    render_job_inputs_section,
    render_job_outputs_section,
)
from matofx.ui.pages.compare.report import (
    ReportGroup,
    ReportImage,
    ReportJobEntry,
    ReportVideo,
    build_compare_report,
    build_multi_compare_report,
)
from matofx.ui.pages.compare.scenario_groups import (
    NotComparableItem,
    ScenarioComparison,
    match_scenarios,
)
from matofx.ui.pages.compare.selection import (
    CROSS_TOOL_MESSAGE,
    MAX_SELECTION,
    MIN_SELECTION,
    Selection,
    SelectionError,
    build_query,
    detect_cross_tool,
    parse_selection,
)
from matofx.ui.pages.compare.workflow_groups import (
    WorkflowAlignmentError,
    align_workflow_runs,
)
from matofx.ui.state import current_theme, current_token

_COLUMN_WIDTH_PX = 440
"""Render width for the CSV / HTML **overlay** charts (merged + the
"Show individual charts" expander), so the per-job charts size
consistently. The inputs/outputs grid above them is now full-width with
flexible (``1fr``) columns (2026-05-24) — it no longer uses this fixed
width — but the overlay charts still need a concrete pixel width at
first paint (relying on ``responsive: true`` alone overflows)."""


@dataclass(frozen=True)
class GroupColumn:
    """One column of a comparison group (a same-tool side-by-side block).

    ``job_id`` is the job to render in this column, or ``None`` when the
    participant (a scenario, or a workflow run) has no job for the
    group's tool — that column renders a top-aligned "not present here"
    placeholder so the surrounding columns stay aligned. ``participant_label``
    is the comparison dimension shown atop the column (scenario / run
    name); ``None`` in plain tool mode, where the job's own label is the
    heading. ``missing_message`` is the placeholder text shown when
    ``job_id`` is ``None``.
    """

    job_id: int | None
    participant_label: str | None = None
    missing_message: str = "Not available here."


@ui.page("/compare")
async def compare_page() -> None:
    """Render the Compare workspace."""
    if not require_logged_in():
        return
    render_shell()

    raw_query = dict(ui.context.client.request.query_params)
    parsed = parse_selection(raw_query)
    client = ApiClient(get_app(), token=current_token())

    with ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;"):
        # Header in the home-page idiom: a bold title + a muted subtitle.
        ui.label("Compare").style(
            "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
        )
        ui.label(
            "Put finished jobs, workflow runs, or scenarios side-by-side — "
            "differences are highlighted, with overlaid charts beneath."
        ).style("color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;")
        if isinstance(parsed, SelectionError):
            ui.label(parsed.message).style("color: var(--matofx-error-text); font-size: 0.95rem;")
            await _render_select_tab_only(client, Selection(mode="tool", ids=()))
            return

        # Live selection shared between the two tabs. The Select tab updates
        # it as the user ticks; the Compare tab re-renders from it whenever
        # it's opened — so the comparison reflects the current picks without
        # needing the "Compare these" button (which is now just a shortcut to
        # the Compare tab). ``rendered`` is the selection the Compare panel
        # currently shows, so flipping back and forth without changing the
        # picks doesn't re-fetch.
        state: dict[str, Any] = {"selection": parsed, "rendered": None}
        compare_holder: dict[str, Any] = {}
        background_tasks: set[asyncio.Task[None]] = set()

        def _spawn(coro: Any) -> None:
            task = asyncio.create_task(coro)
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)

        with (
            ui.tabs()
            .classes("w-full")
            .props("align=left active-color=primary indicator-color=primary")
            .style("border-bottom: 1px solid var(--matofx-card-border);") as tabs
        ):
            # ``no-caps`` keeps the labels in the app's normal sentence
            # case + font rather than Quasar's default ALL-CAPS tab style.
            select_tab = ui.tab("Select", icon="rule").props("no-caps")
            compare_tab = ui.tab("Compare", icon="compare").props("no-caps")

        def _sync_url(selection: Selection) -> None:
            # Keep the address bar in step with the live selection (no reload)
            # so a refresh or a copied link reproduces the same comparison.
            # Reuse the live ``window.location.pathname`` rather than a
            # hard-coded "/compare" — the NiceGUI app is mounted under "/ui",
            # so the real path is "/ui/compare"; writing a bare "/compare"
            # would point a reload at a route that doesn't exist (404).
            params = build_query(selection)
            query = "&".join(f"{k}={v}" for k, v in params.items())
            ui.run_javascript(
                f"history.replaceState(null, '', window.location.pathname + '?{query}')"
            )

        async def _render_compare_now() -> None:
            selection = state["selection"]
            if state["rendered"] == selection:
                return
            state["rendered"] = selection
            container = compare_holder["container"]
            container.clear()
            with container:
                await _render_compare_tab(client, selection)

        def _on_selection_change(selection: Selection) -> None:
            state["selection"] = selection
            _sync_url(selection)

        def _on_request_compare(selection: Selection) -> None:
            state["selection"] = selection
            _sync_url(selection)
            tabs.set_value(compare_tab)
            _spawn(_render_compare_now())

        # Quasar paints tab panels with its own surface colour; force them
        # transparent so the page background (light or dark) shows through
        # and the panel doesn't read as a differently-tinted block.
        with (
            ui.tab_panels(tabs, value=compare_tab if parsed.ids else select_tab)
            .classes("w-full")
            .style("background-color: transparent;")
        ):
            with ui.tab_panel(select_tab).style("background-color: transparent;"):
                await _render_select_tab(
                    client,
                    parsed,
                    on_change=_on_selection_change,
                    on_compare=_on_request_compare,
                )
            with ui.tab_panel(compare_tab).style("background-color: transparent;"):
                compare_holder["container"] = ui.column().classes("w-full")
                if parsed.ids:
                    state["rendered"] = parsed
                    with compare_holder["container"]:
                        await _render_compare_tab(client, parsed)

        def _on_tab_change(tab_name: str) -> None:
            if tab_name == "Compare":
                _spawn(_render_compare_now())

        tabs.on_value_change(lambda e: _on_tab_change(str(e.value)))


# ---------- Select tab ------------------------------------------------------


def _noop_selection(_selection: Selection) -> None:
    """Default selection callback — used by the error-path Select panel."""


async def _render_select_tab_only(client: ApiClient, selection: Selection) -> None:
    """Render the Select panel without the tabbed shell — used for the error path."""
    await _render_select_tab(client, selection)


async def _render_select_tab(
    client: ApiClient,
    selection: Selection,
    *,
    on_change: Callable[[Selection], None] = _noop_selection,
    on_compare: Callable[[Selection], None] = _noop_selection,
) -> None:
    """Mode picker + filter dropdown + tickable list of items.

    ``on_change`` fires on every tick with the live :class:`Selection`, so
    the Compare tab can reflect the picks the moment the user switches to
    it — no button press required. ``on_compare`` fires when the "Compare
    these" button is clicked (valid 2-6 only): a shortcut that also flips
    to the Compare tab. Both carry the picker's current mode.
    """
    with section_card("tune", "What would you like to compare?"):
        mode_radio = ui.radio(
            options={
                "tool": "Jobs of one tool",
                "scenario": "Scenarios",
                "workflow": "Runs of one workflow",
            },
            value=selection.mode,
        ).props("inline")

        ui.label(
            f"Tick between {MIN_SELECTION} and {MAX_SELECTION} items — the comparison "
            "updates as you pick. Open the Compare tab (or use the button) to see it "
            "side-by-side."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

        body = ui.column().classes("w-full gap-2")
        message = ui.label("").style("color: var(--matofx-error-text); font-size: 0.85rem;")
        background_tasks: set[asyncio.Task[None]] = set()
        refresh_task: dict[str, asyncio.Task[None] | None] = {"task": None}

        async def _refresh() -> None:
            body.clear()
            message.text = ""
            mode = str(mode_radio.value or "tool")
            # Re-tick whatever the URL selection carries so the Select tab
            # reflects the current comparison — landing here from My Jobs,
            # or flipping back from the Compare tab, keeps the picks. The
            # preselect only applies to the matching mode; switching the
            # mode radio starts that picker blank.
            preselect = set(selection.ids) if (mode == selection.mode and selection.ids) else set()

            def _changed(ids: list[int]) -> None:
                on_change(Selection(mode=mode, ids=tuple(ids)))  # type: ignore[arg-type]

            def _compare(ids: list[int]) -> None:
                on_compare(Selection(mode=mode, ids=tuple(ids)))  # type: ignore[arg-type]

            if mode == "tool":
                await _render_tool_picker(client, body, message, preselect, _changed, _compare)
            elif mode == "scenario":
                await _render_scenario_picker(client, body, message, preselect, _changed, _compare)
            elif mode == "workflow":
                await _render_workflow_picker(client, body, message, preselect, _changed, _compare)

        def _on_mode_change(_e: Any) -> None:
            # Switching the comparison mode empties the (now-irrelevant) live
            # selection so the Compare tab doesn't show stale picks from the
            # previous mode, then rebuilds the picker.
            #
            # Cancel any in-flight refresh first: each picker fetches its own
            # data (jobs / scenarios / runs) across awaits and renders into
            # ``body`` at the end. Without cancelling, a slower previous
            # refresh (e.g. workflow runs at limit=500) can finish *after* the
            # new one and append its picker beneath the current one — which is
            # how the workflow picker was leaking under Scenarios.
            on_change(Selection(mode=str(mode_radio.value or "tool"), ids=()))  # type: ignore[arg-type]
            prev = refresh_task["task"]
            if prev is not None and not prev.done():
                prev.cancel()
            task = asyncio.create_task(_refresh())
            refresh_task["task"] = task
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)

        mode_radio.on_value_change(_on_mode_change)
        await _refresh()


async def _render_tool_picker(
    client: ApiClient,
    body: ui.column,
    message: ui.label,
    preselect_ids: set[int] | None = None,
    on_change: Callable[[list[int]], None] = lambda _ids: None,
    on_compare: Callable[[list[int]], None] = lambda _ids: None,
) -> None:
    """Mode=tool picker: scope + tool dropdowns + search → tickable jobs.

    Controls row (left→right): a "Whose jobs" scope dropdown, the Tool
    selector that narrows that scope to one tool, and the search box.
    Below it the shared :class:`_PickerList` body (counts header,
    scrollable list, Compare button).

    ``preselect_ids`` (the job ids in the current URL selection) are
    re-ticked on first load and the tool dropdown defaults to the tool
    that owns them, so returning from the Compare tab — or arriving from
    My Jobs — keeps the selection. The preselect is consumed once
    (``preselect_pending``); thereafter changing the scope or tool clears
    the ticks as before.
    """
    background_tasks: set[asyncio.Task[None]] = set()
    state: dict[str, Any] = {
        "mine": False,
        "alias": None,
        "ticked": set(preselect_ids or ()),
        "preselect_pending": bool(preselect_ids),
        "jobs_by_tool": {},
    }
    picker = _PickerList(
        noun="job", ticked=state["ticked"], on_change=on_change, on_compare=on_compare
    )

    def _on_tool_change(event: Any) -> None:
        state["alias"] = str(event.value)
        state["ticked"].clear()
        on_change([])  # a different tool starts a fresh comparison
        picker.set_items(_job_items(state["jobs_by_tool"].get(state["alias"], [])))

    async def _refresh() -> None:
        tool_holder.clear()
        try:
            jobs = await client.list_my_visible_jobs(
                state="complete", mine=bool(state["mine"]), limit=500
            )
        except ApiError as exc:
            message.text = f"Couldn't load your jobs: {exc.detail}"
            return
        message.text = ""

        tools_by_alias: dict[str, str] = {}
        jobs_by_tool: dict[str, list[dict[str, Any]]] = {}
        for job in jobs:
            alias = str(job.get("tool_alias") or "")
            if not alias:
                continue
            tools_by_alias.setdefault(alias, str(job.get("tool_name") or alias))
            jobs_by_tool.setdefault(alias, []).append(job)
        state["jobs_by_tool"] = jobs_by_tool

        if not tools_by_alias:
            state["alias"] = None
            with tool_holder:
                ui.select(options={"": "No tools"}, value="", label="Tool").props(
                    "dense outlined"
                ).style("min-width: 14rem;").disable()
            picker.set_items([])
            return

        if state["preselect_pending"]:
            # First load with a URL selection: default the dropdown to the
            # tool that owns the preselected jobs and keep only the ticks
            # that actually exist under it. If none match (the jobs aren't
            # in this scope), fall through to the default.
            state["preselect_pending"] = False
            matched_alias: str | None = None
            for alias, alias_jobs in jobs_by_tool.items():
                ids_here = {int(j["id"]) for j in alias_jobs}
                if state["ticked"] & ids_here:
                    matched_alias = alias
                    keep = set(state["ticked"]) & ids_here
                    state["ticked"].clear()
                    state["ticked"].update(keep)
                    break
            if matched_alias is not None:
                state["alias"] = matched_alias
            else:
                state["alias"] = next(iter(tools_by_alias))
                state["ticked"].clear()
        else:
            # Reset tool + ticks when the scope changes; surviving the
            # transition would let stale ids through to /compare.
            if state["alias"] not in tools_by_alias:
                state["alias"] = next(iter(tools_by_alias))
            state["ticked"].clear()

        with tool_holder:
            ui.select(
                options=tools_by_alias,
                value=state["alias"],
                label="Tool",
            ).props("dense outlined").style("min-width: 14rem;").on_value_change(_on_tool_change)

        picker.set_items(_job_items(jobs_by_tool.get(state["alias"], [])))
        on_change(sorted(state["ticked"]))  # reflect any preselected ticks

    def _on_scope_change(event: Any) -> None:
        state["mine"] = str(event.value) == "mine"
        on_change([])  # scope switch resets the ticks, so reset the comparison
        task = asyncio.create_task(_refresh())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    with body:
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            ui.select(
                options={"all": "All I can access", "mine": "Jobs I submitted"},
                value="all",
                label="Whose jobs",
            ).props("dense outlined").style("min-width: 12rem;").on_value_change(_on_scope_change)
            tool_holder = ui.element("div").style("min-width: 14rem;")
            picker.render_search()
        picker.render_body()
        await _refresh()


async def _render_scenario_picker(
    client: ApiClient,
    body: ui.column,
    message: ui.label,
    preselect_ids: set[int] | None = None,
    on_change: Callable[[list[int]], None] = lambda _ids: None,
    on_compare: Callable[[list[int]], None] = lambda _ids: None,
) -> None:
    """Mode=scenario picker: project dropdown → multi-select of scenarios.

    Selection spans projects: ticks are kept when the user switches the
    project dropdown, so a cross-project comparison can be assembled by
    ticking scenarios from one project, switching, and ticking more
    (G36 cl.3 — cross-project comparison is allowed). The Compare button
    counts every ticked scenario across projects.

    ``preselect_ids`` (the scenario ids in the current URL selection)
    seed the ticks so returning from the Compare tab keeps the picks;
    the count is correct immediately even for scenarios in a project the
    dropdown isn't currently showing.
    """
    try:
        projects = await client.list_projects()
    except ApiError as exc:
        message.text = f"Couldn't load your projects: {exc.detail}"
        return
    if not projects:
        with body:
            ui.label(
                "You don't have any projects yet — create one first, "
                "then come back here to compare its scenarios."
            ).style("color: var(--matofx-muted-text);")
        return

    project_options = {int(p["id"]): str(p.get("name") or p["id"]) for p in projects}
    chosen_project: dict[str, int] = {"id": next(iter(project_options))}
    ticked: set[int] = set(preselect_ids or ())
    picker = _PickerList(
        noun="scenario", ticked=ticked, on_change=on_change, on_compare=on_compare
    )
    background_tasks: set[asyncio.Task[None]] = set()

    async def _refresh_scenarios() -> None:
        try:
            scenarios = await client.list_scenarios(int(chosen_project["id"]))
        except ApiError as exc:
            message.text = f"Couldn't load scenarios: {exc.detail}"
            picker.set_items([])
            return
        message.text = ""
        picker.set_items(_scenario_items(scenarios))

    def _on_project_change(event: Any) -> None:
        # Keep ticks across projects so a cross-project set can be built.
        chosen_project["id"] = int(event.value)
        task = asyncio.create_task(_refresh_scenarios())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    with body:
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            ui.select(
                options=project_options,
                value=chosen_project["id"],
                label="Project",
            ).props("dense outlined").style("min-width: 14rem;").on_value_change(_on_project_change)
            picker.render_search()
        ui.label(
            "Pick from more than one project if you like — switch projects and keep "
            "ticking; your selection is kept."
        ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        picker.render_body()
        await _refresh_scenarios()


def _render_picker_row(
    *,
    primary: str,
    meta: str,
    checked: bool,
    on_toggle: Callable[[bool], None],
) -> None:
    """One themed, tickable row: checkbox + name + a muted metadata line.

    Mirrors the home Quick-Access row look (padded row, hairline divider,
    name over muted detail) so the Select picker reads as part of the
    redesigned shell rather than a bare checkbox list. The combined
    "name · ids · context" string the list used before is split into a
    bold ``primary`` line and a muted ``meta`` line here.
    """
    with ui.row().classes("items-center no-wrap w-full matofx-qarow").style(
        "gap: 0.6rem; padding: 0.4rem 0.3rem; "
        "border-bottom: 1px solid var(--matofx-card-border);"
    ):
        ui.checkbox(value=checked).props("dense").on_value_change(
            lambda e: on_toggle(bool(e.value))
        )
        with ui.column().classes("gap-0").style("flex: 1 1 0; min-width: 0;"):
            ui.label(primary).style(
                "font-size: 0.9rem; font-weight: 600; color: var(--matofx-body-text); "
                "white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            if meta:
                ui.label(meta).style(
                    "font-size: 0.75rem; color: var(--matofx-muted-text); "
                    "white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
                )


_PICKER_RENDER_CAP = 75
"""Max picker rows rendered at once. The list can hold hundreds (jobs /
runs are fetched at ``limit=500``); building them all synchronously
blocks the event loop and freezes the UI. We render this many and lean
on the search box for the rest — see :meth:`_PickerList._refresh_rows`."""


@dataclass(frozen=True)
class _PickerItem:
    """One tickable row in a Select-tab picker (job / scenario / run)."""

    id: int
    primary: str
    meta: str

    @property
    def haystack(self) -> str:
        """Lower-cased search text: number + name + context."""
        return f"{self.id} {self.primary} {self.meta}".lower()


def _job_items(jobs: list[dict[str, Any]]) -> list[_PickerItem]:
    items: list[_PickerItem] = []
    for job in jobs:
        jid = int(job["id"])
        meta = (
            f"#{jid:07d} · {job.get('project_name') or ''} / {job.get('scenario_name') or ''}"
        )
        items.append(_PickerItem(id=jid, primary=str(job.get("name") or "(no name)"), meta=meta))
    return items


def _scenario_items(scenarios: list[dict[str, Any]]) -> list[_PickerItem]:
    items: list[_PickerItem] = []
    for scenario in scenarios:
        sid = int(scenario["id"])
        items.append(
            _PickerItem(
                id=sid,
                primary=str(scenario.get("name") or sid),
                meta=str(scenario.get("description") or ""),
            )
        )
    return items


def _run_items(runs: list[dict[str, Any]]) -> list[_PickerItem]:
    items: list[_PickerItem] = []
    for run in runs:
        rid = int(run["id"])
        steps = len(run.get("children") or [])
        # Mirror the job rows' context: number · project / scenario · steps.
        context = " / ".join(
            part for part in (run.get("project_name"), run.get("scenario_name")) if part
        )
        bits = [f"#{rid:07d}"]
        if context:
            bits.append(context)
        bits.append(f"{steps} steps")
        items.append(
            _PickerItem(
                id=rid,
                primary=str(run.get("name") or "(no name)"),
                meta=" · ".join(bits),
            )
        )
    return items


class _PickerList:
    """Shared 'search + counts + scrollable list + Compare' block.

    One instance per Select-tab picker (tool / scenario / workflow). The
    mode picker renders the search box (:meth:`render_search`) into its
    controls row and the body (:meth:`render_body`) below it, then feeds
    the instance items via :meth:`set_items` whenever its selector
    changes. The instance owns the ticked set (shared by reference with
    the caller so cross-project / preselect ticks survive), the live
    search filter, the counts line, the Clear action, and the
    cap-aware Compare button.

    ``noun`` is the singular item word for the counts line and search
    placeholder ("job" / "scenario" / "run").
    """

    def __init__(
        self,
        *,
        noun: str,
        ticked: set[int],
        on_change: Callable[[list[int]], None],
        on_compare: Callable[[list[int]], None],
    ) -> None:
        self._noun = noun
        self._ticked = ticked
        self._on_change = on_change
        self._on_compare = on_compare
        self._items: list[_PickerItem] = []
        self._query = ""
        self._count_label: ui.label | None = None
        self._list: ui.column | None = None
        self._button: ui.button | None = None

    def render_search(self) -> None:
        """Render the search input — call inside the picker's controls row."""
        search = (
            ui.input(label="Search", placeholder=f"{self._noun}s by name, number, or context…")
            .props("clearable dense outlined debounce=200")
            .style("flex: 1 1 220px; min-width: 220px;")
        )

        def _on(event: Any) -> None:
            # Read the synced value off the change event rather than the
            # element — ``on_value_change`` fires after NiceGUI has applied
            # the new value, so this is reliable (and ``None`` on a clear).
            value = getattr(event, "value", None)
            self._query = (value or "").strip().lower()
            self._refresh_rows()

        search.on_value_change(_on)

    def render_body(self) -> None:
        """Render the counts header, the scrollable list, and the Compare footer."""
        with ui.row().classes("w-full items-center").style("padding: 0.1rem 0.1rem 0;"):
            self._count_label = ui.label("").style(
                "color: var(--matofx-muted-text); font-size: 0.8rem;"
            )
            ui.space()
            ui.button("Clear", on_click=self._clear).props("flat dense no-caps").style(
                "color: var(--matofx-muted-text);"
            )
        self._list = (
            ui.column()
            .classes("w-full gap-0")
            .style(
                "max-height: 360px; overflow-y: auto; "
                "border: 1px solid var(--matofx-card-border); border-radius: 8px; "
                "padding: 0 0.5rem;"
            )
        )
        with ui.row().classes("w-full justify-end").style("margin-top: 0.25rem;"):
            self._button = ui.button(
                "Compare these",
                on_click=lambda _e=None: self._on_compare(sorted(self._ticked)),
            ).props("color=primary no-caps")
        self._refresh_rows()

    def set_items(self, items: list[_PickerItem]) -> None:
        """Replace the full item set (the current selector's list) and redraw."""
        self._items = items
        self._refresh_rows()

    def _filtered(self) -> list[_PickerItem]:
        if not self._query:
            return self._items
        return [it for it in self._items if self._query in it.haystack]

    def _refresh_rows(self) -> None:
        if self._list is None:
            return
        self._list.clear()
        filtered = self._filtered()
        # Cap the synchronous render: building every row at once (the list can
        # hold hundreds) blocks the single event loop long enough to freeze
        # the UI and drop the NiceGUI websocket. When we have to cap, pull
        # ticked rows to the front so a selection is never hidden, and tell
        # the user to use Search to reach the rest.
        if len(filtered) > _PICKER_RENDER_CAP:
            ordered = sorted(filtered, key=lambda it: it.id not in self._ticked)
        else:
            ordered = filtered
        shown = ordered[:_PICKER_RENDER_CAP]
        hidden = len(ordered) - len(shown)
        with self._list:
            if not self._items:
                ui.label(f"No {self._noun}s to choose from yet.").style(
                    "color: var(--matofx-muted-text); font-size: 0.85rem; padding: 0.7rem 0.2rem;"
                )
            elif not filtered:
                ui.label("No matches for your search.").style(
                    "color: var(--matofx-muted-text); font-size: 0.85rem; padding: 0.7rem 0.2rem;"
                )
            else:
                for it in shown:
                    _render_picker_row(
                        primary=it.primary,
                        meta=it.meta,
                        checked=it.id in self._ticked,
                        on_toggle=lambda value, _id=it.id: self._toggle(_id, value),
                    )
                if hidden > 0:
                    ui.label(
                        f"+{hidden} more not shown — type in Search to narrow the list."
                    ).style(
                        "color: var(--matofx-muted-text); font-size: 0.8rem; "
                        "font-style: italic; padding: 0.5rem 0.2rem;"
                    )
        self._sync()

    def _toggle(self, item_id: int, value: bool) -> None:
        if value:
            self._ticked.add(item_id)
        else:
            self._ticked.discard(item_id)
        self._sync()
        self._on_change(sorted(self._ticked))

    def _clear(self, _e: Any = None) -> None:
        if not self._ticked:
            return
        self._ticked.clear()
        self._on_change([])
        self._refresh_rows()

    def _sync(self) -> None:
        if self._count_label is not None:
            total = len(self._items)
            shown = len(self._filtered())
            selected = len(self._ticked)
            plural = f"{self._noun}s"
            if self._query and shown != total:
                self._count_label.text = f"{shown} of {total} {plural} · {selected} selected"
            else:
                self._count_label.text = f"{total} {plural} · {selected} selected"
        if self._button is not None:
            if MIN_SELECTION <= len(self._ticked) <= MAX_SELECTION:
                self._button.text = f"Compare these {len(self._ticked)}"
                self._button.enable()
            else:
                self._button.text = "Compare these"
                self._button.disable()


async def _render_workflow_picker(
    client: ApiClient,
    body: ui.column,
    message: ui.label,
    preselect_ids: set[int] | None = None,
    on_change: Callable[[list[int]], None] = lambda _ids: None,
    on_compare: Callable[[list[int]], None] = lambda _ids: None,
) -> None:
    """Mode=workflow picker: workflow-definition dropdown → tickable runs.

    Lists the workflow runs visible to the user (cross-project), grouped
    by workflow definition. Only **complete** runs are offered —
    mirroring the tool picker's complete-only rule, since a finished run
    is what you compare. Picking a definition shows its runs; ticking
    2-6 and clicking Compare deep-links to
    ``?mode=workflow&workflows=<run ids>``.
    """
    try:
        runs = await client.list_my_visible_workflow_runs(limit=500)
    except ApiError as exc:
        message.text = f"Couldn't load your workflow runs: {exc.detail}"
        return

    complete_runs = [r for r in runs if str(r.get("state") or "") == "complete"]
    if not complete_runs:
        with body:
            ui.label(
                "No finished workflow runs to compare yet. Run a workflow to "
                "completion first, then come back here."
            ).style("color: var(--matofx-muted-text);")
        return

    # Group runs by workflow definition (alias). The dropdown label is
    # the human workflow name; the alias is the wire key only.
    runs_by_alias: dict[str, list[dict[str, Any]]] = {}
    names_by_alias: dict[str, str] = {}
    for run in complete_runs:
        alias = str(run.get("workflow_alias") or "")
        if not alias:
            continue
        names_by_alias.setdefault(alias, str(run.get("workflow_name") or alias))
        runs_by_alias.setdefault(alias, []).append(run)

    # Default the dropdown to the workflow that owns the preselected runs
    # (carried in from the URL) and keep only the ticks that exist under
    # it, so returning from the Compare tab keeps the selection.
    default_alias = next(iter(names_by_alias))
    ticked_runs: set[int] = set()
    if preselect_ids:
        for alias, alias_runs in runs_by_alias.items():
            ids_here = {int(r["id"]) for r in alias_runs}
            if preselect_ids & ids_here:
                default_alias = alias
                ticked_runs = set(preselect_ids) & ids_here
                break
    state: dict[str, Any] = {"alias": default_alias, "ticked": ticked_runs}
    picker = _PickerList(
        noun="run", ticked=state["ticked"], on_change=on_change, on_compare=on_compare
    )

    def _on_def_change(event: Any) -> None:
        state["alias"] = str(event.value)
        state["ticked"].clear()
        on_change([])  # a different workflow starts a fresh comparison
        picker.set_items(_run_items(runs_by_alias.get(state["alias"], [])))

    with body:
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            ui.select(
                options=names_by_alias,
                value=state["alias"],
                label="Workflow",
            ).props("dense outlined").style("min-width: 14rem;").on_value_change(_on_def_change)
            picker.render_search()
        ui.label(
            "Runs are compared step-by-step, lining up the matching stage across runs."
        ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        picker.render_body()
        picker.set_items(_run_items(runs_by_alias.get(state["alias"], [])))
        on_change(sorted(state["ticked"]))  # reflect any preselected ticks


# ---------- Compare tab -----------------------------------------------------


async def _render_compare_tab(client: ApiClient, selection: Selection) -> None:
    """Resolve the selection into same-tool job groups and render them.

    Each Compare mode resolves to one or more *job groups* — a set of
    jobs that all run the same tool, rendered by :func:`_render_job_group`
    (the diff grid + overlays + report engine shared by every mode):

    - ``tool``     → one group: the jobs the user ticked.
    - ``workflow`` → one group per workflow step, aligned across runs.
    - ``scenario`` → one group per tool / workflow shared across scenarios.
    """
    if not selection.ids:
        ui.label("Pick a few items on the Select tab to start comparing.").style(
            "color: var(--matofx-muted-text);"
        )
        return
    # The live (in-page) selection bypasses the URL parser's 2-6 guard, so
    # enforce the bounds here — a single tick can't be compared, and the
    # per-row grid gets unreadable past the cap.
    if len(selection.ids) < MIN_SELECTION:
        ui.label(
            f"Pick at least {MIN_SELECTION} items on the Select tab — you need two or "
            "more to see how they differ."
        ).style("color: var(--matofx-muted-text);")
        return
    if len(selection.ids) > MAX_SELECTION:
        ui.label(
            f"The most you can compare at once is {MAX_SELECTION}. Untick a few on the "
            "Select tab."
        ).style("color: var(--matofx-muted-text);")
        return
    if selection.mode == "scenario":
        await _render_scenario_mode(client, list(selection.ids))
        return
    if selection.mode == "workflow":
        await _render_workflow_mode(client, list(selection.ids))
        return
    await _render_job_group(client, [GroupColumn(job_id=jid) for jid in selection.ids])


async def _render_job_group(
    client: ApiClient,
    columns: list[GroupColumn],
    *,
    show_report_buttons: bool = True,
    report_heading: str = "",
) -> ReportGroup | None:
    """Load + render one same-tool job comparison block in the current slot.

    The shared engine behind all three Compare modes. Each
    :class:`GroupColumn` is one column of the grid — a present job, or a
    missing-participant placeholder. Fetches every present job's
    metadata + descriptor + output document, guards against a cross-tool
    set, computes the differing keys, pre-fetches every CSV / HTML /
    file-preview blob once, then lays out the per-column header / inputs /
    outputs grid (missing participants get a top-aligned "not present"
    cell so the columns stay aligned) with the CSV + HTML overlay charts
    beneath. Renders into the current NiceGUI parent — callers wrap it in
    whatever section / expansion the mode needs.

    ``show_report_buttons`` renders the per-group HTML / PDF report
    buttons at the top — ``True`` for the single-group Jobs mode;
    ``False`` for Workflow / Scenario modes, which render one *overall*
    report covering every group. Returns the :class:`ReportGroup` (its
    descriptor + per-column :class:`ReportJobEntry` list, with
    ``report_heading`` as the section heading) so a caller can fold it
    into that overall report — or ``None`` when there's nothing to render
    (no present jobs / load error / cross-tool set).
    """
    present_ids = [c.job_id for c in columns if c.job_id is not None]
    spinner = ui.spinner("dots", size="lg")
    container = ui.column().classes("w-full gap-4")
    if not present_ids:
        spinner.set_visibility(False)
        with container:
            ui.label("None of the compared items has a job for this tool.").style(
                "color: var(--matofx-muted-text);"
            )
        return None
    try:
        bundles = await _load_jobs(client, present_ids)
    except ApiError as exc:
        spinner.set_visibility(False)
        with container:
            ui.label(f"Couldn't load the selected jobs: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
        return None
    spinner.set_visibility(False)

    aliases = [str(b.job.get("tool_alias") or "") for b in bundles]
    if detect_cross_tool(aliases):
        with container:
            ui.label(CROSS_TOOL_MESSAGE).style("color: var(--matofx-error-text); font-weight: 600;")
            ui.button(
                "Clear selection",
                on_click=lambda _e=None: ui.navigate.to("/compare"),
            ).props("flat")
        return None

    bundle_by_id: dict[int, _LoadedJob] = {b.cell_model.job_id: b for b in bundles}
    diff_bundles = [
        JobBundle(
            job_id=b.job["id"],
            inputs=dict(b.job.get("inputs") or {}),
            outputs=_flatten_output_values(b.output_document),
        )
        for b in bundles
    ]
    differing = compute_differing_keys(diff_bundles)
    descriptor = bundles[0].descriptor

    # Download every CSV output once, up-front, so the overlay charts
    # and the HTML / PDF report can share a single in-memory copy.
    csv_outputs = await _load_csv_outputs(client, bundles)
    # Single pre-fetch pass for every previewable file — outputs
    # (html_plot / csv_table / image / cad_file STL) and inputs
    # (image / stl). The cell renderer reads from each bundle's
    # ``cell_model.output_preview_blobs`` / ``input_preview_blobs``
    # to embed the right viewer inline. ``JobCellModel`` is frozen
    # but the dicts are mutable, so the ``[name] = bytes`` writes
    # below are well-defined under the dataclass contract.
    output_blob_map = await _load_output_preview_blobs(client, bundles)
    for (jid, name), content in output_blob_map.items():
        for bundle in bundles:
            if bundle.cell_model.job_id == jid:
                bundle.cell_model.output_preview_blobs[name] = content
                break
    input_blob_map = await _load_input_preview_blobs(client, bundles)
    for (jid, name), content in input_blob_map.items():
        for bundle in bundles:
            if bundle.cell_model.job_id == jid:
                bundle.cell_model.input_preview_blobs[name] = content
                break
    # Build the html_outputs list used by the HTML overlay expander
    # below the grid from the pre-fetched output blobs — saves a
    # second round trip for the same .html files.
    html_outputs = _build_html_overlay_entries(bundles)
    # Field outputs (transient ``.vtp`` series etc.) can't ride the blob
    # pre-loader — they come from the parsed field-frames endpoint. Fetch
    # them once, then hand each job its frames so the cell renderer can
    # draw the animation inline in the Outputs card (the same place every
    # other output lives). The same list also feeds the HTML report.
    field_outputs = await _load_field_outputs(client, bundles)
    for field_output in field_outputs:
        for bundle in bundles:
            if bundle.cell_model.job_id == field_output.job_id:
                bundle.cell_model.field_frames[field_output.field_name] = field_output.data
                break

    # Assemble the report projection for this group — one entry per
    # column (present jobs carry their context + embedded images; a
    # missing participant becomes a placeholder). Returned to the
    # caller so Workflow / Scenario modes can fold it into one overall
    # report; also feeds this group's own buttons in Jobs mode.
    report_group = ReportGroup(
        descriptor=descriptor,
        jobs=[
            _column_report_entry(
                column,
                bundle_by_id.get(column.job_id) if column.job_id is not None else None,
            )
            for column in columns
        ],
        differing_keys=differing,
        csv_outputs=csv_outputs,
        html_outputs=html_outputs,
        field_outputs=field_outputs,
        heading=report_heading,
    )

    with container:
        # Framed toolbar strip: what's being compared on the left, the
        # diff-only toggle on the right. The card-bg fill makes it read
        # as an intentional frame now that the tab panel is transparent.
        with ui.row().classes("w-full items-center no-wrap").style(
            "gap: 0.75rem; padding: 0.55rem 0.85rem; border-radius: 10px; "
            "background: var(--matofx-card-bg); border: 1px solid var(--matofx-card-border);"
        ):
            job_word = "job" if len(bundles) == 1 else "jobs"
            ui.icon("compare").style("color: var(--matofx-accent); font-size: 1.1rem;")
            ui.label(
                f"Comparing {len(bundles)} {job_word} of "
                f"{descriptor.get('name') or descriptor.get('alias') or ''}."
            ).style("color: var(--matofx-body-text); font-weight: 600; font-size: 0.9rem;")
            ui.space()
            only_diffs_switch = ui.switch("Show only differences", value=False).props("dense")

        # Report download buttons live at the top of the comparison so
        # the user can grab a snapshot before scrolling through the
        # detail. Only the single-group Jobs mode renders them here;
        # Workflow / Scenario modes render one overall report instead.
        if show_report_buttons:
            tool_alias = str(descriptor.get("alias") or "tool")
            # Snapshot the theme here in the UI context — the build runs in
            # a worker thread (``ng_run.io_bound``) where ``current_theme()``
            # can't reach ``app.storage.user``.
            report_theme = current_theme()

            def _build_single(auto_print: bool) -> bytes:
                return build_compare_report(
                    descriptor=report_group.descriptor,
                    jobs=report_group.jobs,
                    differing_keys=report_group.differing_keys,
                    csv_outputs=report_group.csv_outputs,
                    html_outputs=report_group.html_outputs,
                    field_outputs=report_group.field_outputs,
                    theme=report_theme,
                    auto_print=auto_print,
                )

            _render_report_buttons(
                build=_build_single,
                base_filename=f"matofx_compare_{tool_alias}_{len(bundles)}_cols",
            )

        # Inputs / outputs grid first — that's where the user looks for
        # "what's different about each run". Plots and visual outputs
        # belong below: they take vertical space and are secondary to
        # the row-by-row variable comparison.
        #
        # Layout is a CSS grid with one column per participant and three
        # rows (header / Inputs card / Outputs card). With three rows,
        # every card sits in its own grid track, so the Inputs card on
        # the left top-aligns with the Inputs card on the right
        # regardless of cell height — same for Outputs. ``align-items:
        # start`` keeps short cards anchored to the top. Columns are
        # ``1fr`` so the grid fills the full panel width (no horizontal
        # scroll); ``minmax(0, 1fr)`` lets long values wrap rather than
        # overflow.
        n_cols = len(columns)
        grid_container = (
            ui.element("div")
            .classes("w-full")
            .style(
                "display: grid; width: 100%; "
                f"grid-template-columns: repeat({n_cols}, minmax(0, 1fr)); "
                "column-gap: 0.75rem; row-gap: 0.75rem; "
                "align-items: start;"
            )
        )

        def _bundle_for(column: GroupColumn) -> _LoadedJob | None:
            if column.job_id is None:
                return None
            return bundle_by_id.get(column.job_id)

        def _redraw_grid() -> None:
            grid_container.clear()
            with grid_container:
                # Row 1 — per-column header. The optional participant
                # label (scenario / run name) sits above the job's own
                # label + state; a missing column shows the participant
                # label over an italic "not present" note. One cell per
                # column keeps the CSS grid's auto-flow aligned.
                for column in columns:
                    bundle = _bundle_for(column)
                    with ui.column().classes("gap-1"):
                        if column.participant_label:
                            ui.label(column.participant_label).style("font-weight: 700;")
                        if bundle is not None:
                            render_job_header(bundle.cell_model)
                        else:
                            ui.label(column.missing_message).style(
                                "color: var(--matofx-muted-text); "
                                "font-size: 0.85rem; font-style: italic;"
                            )
                # Row 2 — Inputs card per column, top-aligned. Missing
                # participants get a muted placeholder card carrying the
                # "not present" message, so the empty space is visible
                # and the present columns' cards stay lined up.
                for column in columns:
                    bundle = _bundle_for(column)
                    if bundle is not None:
                        render_job_inputs_section(
                            bundle.cell_model,
                            differing_keys=differing,
                            only_differences=bool(only_diffs_switch.value),
                        )
                    else:
                        _render_missing_cell(column.missing_message)
                # Row 3 — Outputs card per column. A missing column gets
                # an empty placeholder element so the grid keeps exactly
                # one cell per column in this row (auto-flow alignment).
                for column in columns:
                    bundle = _bundle_for(column)
                    if bundle is not None:
                        render_job_outputs_section(
                            bundle.cell_model,
                            differing_keys=differing,
                            only_differences=bool(only_diffs_switch.value),
                            download_callback=_make_download_callback(
                                client, bundle.cell_model.job_id
                            ),
                        )
                    else:
                        ui.element("div")

        only_diffs_switch.on_value_change(lambda _e: _redraw_grid())
        _redraw_grid()

        # Charts at the bottom — CSV overlays first, then any HTML
        # outputs the tools wrote (typically plotly write_html reports
        # the cell-grid surfaces as bare filenames). 3D overlays
        # come later.
        with ui.column().classes("w-full q-mt-md"):
            render_overlay_section(
                csv_outputs,
                job_order=[b.cell_model.job_id for b in bundles],
                column_width_px=_COLUMN_WIDTH_PX,
            )
            render_html_overlay_section(
                html_outputs,
                job_order=[b.cell_model.job_id for b in bundles],
                column_width_px=_COLUMN_WIDTH_PX,
            )

    return report_group


def _column_report_entry(column: GroupColumn, bundle: _LoadedJob | None) -> ReportJobEntry:
    """Project one grid column into a :class:`ReportJobEntry` for the report.

    A present column carries the job's context (project / scenario / tool
    version / completion) and any embedded image previews; a missing
    participant becomes a ``present=False`` placeholder carrying its
    "not present" message so the report column stays aligned.
    """
    if bundle is None:
        return ReportJobEntry(
            job_id=column.job_id or 0,
            job_label="",
            state="",
            participant_label=column.participant_label or "",
            present=False,
            missing_message=column.missing_message,
        )
    cell = bundle.cell_model
    return ReportJobEntry(
        job_id=cell.job_id,
        job_label=cell.job_label,
        state=cell.state,
        participant_label=column.participant_label or "",
        project_name=cell.project_name,
        scenario_name=cell.scenario_name,
        tool_name=cell.tool_name,
        tool_version=cell.tool_version,
        completed_display=cell.completed_display,
        inputs=cell.inputs,
        output_values=cell.output_values,
        output_filenames=cell.output_filenames,
        images=_collect_report_images(bundle),
        videos=_collect_report_videos(bundle),
        present=True,
    )


_REPORT_IMAGE_MIME: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}


def _image_data_url(blob: bytes, extension: str) -> str:
    """Base64 ``data:`` URL for an image blob, keyed by file extension."""
    mime = _REPORT_IMAGE_MIME.get(extension, "application/octet-stream")
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


_REPORT_VIDEO_MIME: dict[str, str] = {
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "webm": "video/webm",
    "ogg": "video/ogg",
    "ogv": "video/ogg",
    "mov": "video/quicktime",
}


def _video_data_url(blob: bytes, extension: str) -> str:
    """Base64 ``data:`` URL for a video blob, keyed by file extension."""
    mime = _REPORT_VIDEO_MIME.get(extension, "application/octet-stream")
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def _collect_report_images(bundle: _LoadedJob) -> tuple[ReportImage, ...]:
    """Build the embeddable image set for a job's report column.

    Reuses the bytes already pre-fetched for the on-screen previews
    (``output_preview_blobs`` / ``input_preview_blobs``); keeps only the
    image-typed files (the report can't embed a 3-D scene / video / PDF
    the way the live page does). Each becomes a base64 ``data:`` URL.
    """
    cell = bundle.cell_model
    images: list[ReportImage] = []
    for name, blob in cell.output_preview_blobs.items():
        filename = cell.output_filenames.get(name) or name
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in _IMAGE_EXTENSIONS_FOR_PRELOAD:
            images.append(ReportImage(caption=name, data_url=_image_data_url(blob, ext)))
    for field_spec in cell.descriptor.get("inputs") or []:
        if field_spec.get("type") != "file":
            continue
        name = str(field_spec.get("name") or "")
        if not name:
            continue
        input_blob = cell.input_preview_blobs.get(name)
        value = cell.inputs.get(name)
        if input_blob is None or not isinstance(value, str):
            continue
        ext = value.rsplit(".", 1)[-1].lower() if "." in value else ""
        if ext in _IMAGE_EXTENSIONS_FOR_PRELOAD:
            label = str(field_spec.get("label") or name)
            images.append(ReportImage(caption=label, data_url=_image_data_url(input_blob, ext)))
    return tuple(images)


def _collect_report_videos(bundle: _LoadedJob) -> tuple[ReportVideo, ...]:
    """Build the embeddable video set for a job's report column.

    Reuses the bytes already pre-fetched for the on-screen previews
    (``output_preview_blobs`` / ``input_preview_blobs``); keeps only the
    video-typed files and inlines each as a base64 ``data:`` URL. These
    ride the HTML report only — the report builder drops them from the
    print/PDF flavour.
    """
    cell = bundle.cell_model
    videos: list[ReportVideo] = []
    for name, blob in cell.output_preview_blobs.items():
        filename = cell.output_filenames.get(name) or name
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in _VIDEO_EXTENSIONS_FOR_PRELOAD:
            videos.append(ReportVideo(caption=name, data_url=_video_data_url(blob, ext)))
    for field_spec in cell.descriptor.get("inputs") or []:
        if field_spec.get("type") != "file":
            continue
        name = str(field_spec.get("name") or "")
        if not name:
            continue
        input_blob = cell.input_preview_blobs.get(name)
        value = cell.inputs.get(name)
        if input_blob is None or not isinstance(value, str):
            continue
        ext = value.rsplit(".", 1)[-1].lower() if "." in value else ""
        if ext in _VIDEO_EXTENSIONS_FOR_PRELOAD:
            label = str(field_spec.get("label") or name)
            videos.append(ReportVideo(caption=label, data_url=_video_data_url(input_blob, ext)))
    return tuple(videos)


def _render_missing_cell(message: str) -> None:
    """Top-aligned placeholder for a participant with no job in this group.

    Keeps one cell per column so the CSS grid's row stays aligned (every
    logical row contributes exactly one element per column). The muted
    card sits at the top of its track — ``align-items: start`` on the
    grid leaves empty space below it where the present columns' inputs /
    outputs cards extend.
    """
    with ui.card().classes("w-full"):
        ui.label(message).style(
            "color: var(--matofx-muted-text); font-size: 0.85rem; font-style: italic;"
        )


# ---------- workflow comparison ---------------------------------------------


def _run_participant_label(run: dict[str, Any]) -> str:
    """Column heading for a workflow run: its name + id, plainly."""
    name = str(run.get("name") or "").strip()
    run_id = run.get("id")
    if name:
        return f"{name} · run #{run_id}"
    return f"Run #{run_id}"


async def _render_workflow_mode(client: ApiClient, run_ids: list[int]) -> None:
    """Resolve workflow-run ids → run payloads → per-step comparison."""
    spinner = ui.spinner("dots", size="lg")
    try:
        runs = list(await asyncio.gather(*[client.get_workflow_run(rid) for rid in run_ids]))
    except ApiError as exc:
        spinner.set_visibility(False)
        ui.label(f"Couldn't load the selected workflow runs: {exc.detail}").style(
            "color: var(--matofx-error-text);"
        )
        return
    spinner.set_visibility(False)
    await _render_workflow_comparison(client, runs, [_run_participant_label(r) for r in runs])


async def _render_workflow_comparison(
    client: ApiClient,
    runs: Sequence[dict[str, Any] | None],
    participant_labels: Sequence[str],
    *,
    show_report_buttons: bool = True,
    heading_prefix: str = "",
) -> list[ReportGroup]:
    """Render aligned runs of one workflow as one job group per step.

    Shared by Compare Workflows (every participant is a run; labels =
    run names) and the workflow side of Compare Scenarios (one
    participant per scenario; labels = scenario names — a scenario that
    never ran this workflow is a ``None`` entry in ``runs``). The
    alignment over the *present* runs is computed by
    :func:`compare.workflow_groups.align_workflow_runs`; each step
    becomes a collapsible :func:`_render_job_group` with one column per
    participant. A column shows a top-aligned placeholder when the
    participant has no run at all ("workflow not in this scenario") or
    has a run that lacks this step ("step not in this run" — version
    drift).

    Returns the per-step :class:`ReportGroup` list. Workflow mode (the
    default) also renders **one overall report** covering every step;
    Scenario mode passes ``show_report_buttons=False`` and folds the
    returned groups into the scenario-wide overall report, with
    ``heading_prefix`` namespacing each step heading by workflow name.
    """
    present_runs = [run for run in runs if run is not None]
    alignment = align_workflow_runs(present_runs)
    if isinstance(alignment, WorkflowAlignmentError):
        ui.label(alignment.message).style("color: var(--matofx-error-text); font-weight: 600;")
        ui.button(
            "Clear selection",
            on_click=lambda _e=None: ui.navigate.to("/compare"),
        ).props("flat")
        return []

    # Map each participant column to its index into the present-runs list
    # (which is what ``step.job_ids`` is parallel to); ``None`` for a
    # participant with no run.
    present_index_by_participant: list[int | None] = []
    next_present = 0
    for run in runs:
        if run is None:
            present_index_by_participant.append(None)
        else:
            present_index_by_participant.append(next_present)
            next_present += 1

    run_word = "run" if len(present_runs) == 1 else "runs"
    ui.label(f"Comparing {len(present_runs)} {run_word} of {alignment.workflow_name}.").style(
        "color: var(--matofx-muted-text);"
    )
    if alignment.version_mix:
        ui.label(
            "⚠ These runs use different versions of the workflow definition — "
            "their steps may differ for that reason."
        ).style("color: #FB8C00; font-size: 0.85rem; font-weight: 600;")

    # Report buttons (Workflow mode only) sit above the steps but are
    # wired after every step has loaded, so the overall report covers
    # all of them.
    report_holder = ui.row().classes("w-full justify-end gap-2") if show_report_buttons else None

    collected: list[ReportGroup] = []
    for step in alignment.steps:
        step_columns: list[GroupColumn] = []
        for participant_idx, label in enumerate(participant_labels):
            present_idx = present_index_by_participant[participant_idx]
            if present_idx is None:
                step_columns.append(
                    GroupColumn(
                        job_id=None,
                        participant_label=label,
                        missing_message="This workflow isn't part of this scenario.",
                    )
                )
            else:
                step_columns.append(
                    GroupColumn(
                        job_id=step.job_ids[present_idx],
                        participant_label=label,
                        missing_message="This step isn't part of this run.",
                    )
                )
        heading = f"Step {step.position} · {step.tool_name}" if step.position else step.tool_name
        with (
            ui.expansion(heading, value=True, icon="widgets")
            .classes("w-full")
            .props("header-class=text-weight-medium")
            .style(
                "border: 1px solid var(--matofx-card-border); "
                "border-radius: 10px; overflow: hidden;"
            )
        ):
            group = await _render_job_group(
                client,
                step_columns,
                show_report_buttons=False,
                report_heading=f"{heading_prefix}{heading}",
            )
        if group is not None:
            collected.append(group)

    if report_holder is not None and collected:
        title = f"Compare report — {alignment.workflow_name}"
        subtitle = f"{len(present_runs)} {run_word} · " + ", ".join(participant_labels)
        # Snapshot the theme in the UI context — the build runs off-thread.
        report_theme = current_theme()

        def _build_workflow(auto_print: bool) -> bytes:
            return build_multi_compare_report(
                title=title,
                subtitle=subtitle,
                groups=collected,
                theme=report_theme,
                auto_print=auto_print,
            )

        with report_holder:
            _render_report_buttons(
                build=_build_workflow,
                base_filename=f"matofx_compare_workflow_{alignment.workflow_alias}",
            )

    return collected


# ---------- scenario comparison ---------------------------------------------


async def _render_scenario_mode(client: ApiClient, scenario_ids: list[int]) -> None:
    """Resolve scenario ids → per-tool + per-workflow comparison blocks.

    Fetches each scenario's metadata, its standalone jobs, and its
    workflow runs; :func:`compare.scenario_groups.match_scenarios` works
    out which tools / workflows are shared across two or more scenarios.
    Each shared tool renders as one :func:`_render_job_group` (a column
    per scenario); each shared workflow renders through
    :func:`_render_workflow_comparison`. Tools / workflows present in
    only one scenario are listed under "Not comparable" so nothing
    silently disappears.
    """
    spinner = ui.spinner("dots", size="lg")
    try:
        scenarios = list(await asyncio.gather(*[client.get_scenario(sid) for sid in scenario_ids]))
        jobs_per_scenario = await asyncio.gather(*[client.list_jobs(sid) for sid in scenario_ids])
        runs_per_scenario = await asyncio.gather(
            *[client.list_workflow_runs_for_scenario(sid) for sid in scenario_ids]
        )
        projects = await client.list_projects()
    except ApiError as exc:
        spinner.set_visibility(False)
        ui.label(f"Couldn't load the selected scenarios: {exc.detail}").style(
            "color: var(--matofx-error-text);"
        )
        return
    spinner.set_visibility(False)

    standalone_by_scenario: dict[int, list[dict[str, Any]]] = {}
    runs_by_scenario: dict[int, list[dict[str, Any]]] = {}
    for sid, jobs, runs in zip(scenario_ids, jobs_per_scenario, runs_per_scenario, strict=True):
        standalone_by_scenario[sid] = [j for j in jobs if j.get("workflow_run_id") is None]
        runs_by_scenario[sid] = list(runs)

    comparison = match_scenarios(scenarios, standalone_by_scenario, runs_by_scenario)

    # Resolve human tool names for section headings + the not-comparable
    # list (JobResponse carries only the alias).
    tool_aliases = {group.tool_alias for group in comparison.tool_groups}
    tool_aliases.update(item.alias for item in comparison.not_comparable if item.kind == "tool")
    tool_names = await _resolve_tool_names(client, sorted(tool_aliases))

    project_names = {int(p["id"]): str(p.get("name") or p["id"]) for p in projects}
    participant_labels = _scenario_participant_labels(comparison, scenarios, project_names)

    _render_scenario_header(comparison, participant_labels)

    if not comparison.tool_groups and not comparison.workflow_groups:
        ui.label(
            "These scenarios don't share a tool or workflow to compare. Pick "
            "scenarios that ran the same tool or workflow."
        ).style("color: var(--matofx-muted-text);")

    # Overall report buttons sit above the groups but are wired after
    # every group has loaded, so one report covers every shared tool +
    # workflow step across the scenarios.
    report_holder = ui.row().classes("w-full justify-end gap-2")
    collected: list[ReportGroup] = []

    for group in comparison.tool_groups:
        columns = [
            GroupColumn(
                job_id=job_id,
                participant_label=label,
                missing_message="This tool isn't part of this scenario.",
            )
            for job_id, label in zip(group.job_ids, participant_labels, strict=True)
        ]
        heading = tool_names.get(group.tool_alias, group.tool_alias)
        with (
            ui.expansion(heading, value=True, icon="widgets")
            .classes("w-full")
            .props("header-class=text-weight-medium")
            .style(
                "border: 1px solid var(--matofx-card-border); "
                "border-radius: 10px; overflow: hidden;"
            )
        ):
            tool_group = await _render_job_group(
                client, columns, show_report_buttons=False, report_heading=heading
            )
        if tool_group is not None:
            collected.append(tool_group)

    for wf_group in comparison.workflow_groups:
        with ui.column().classes("w-full gap-2 q-mt-md"):
            ui.label(f"Workflow: {wf_group.workflow_name}").style(
                "font-weight: 700; font-size: 1.05rem;"
            )
            step_groups = await _render_workflow_comparison(
                client,
                list(wf_group.runs),
                participant_labels,
                show_report_buttons=False,
                heading_prefix=f"{wf_group.workflow_name} · ",
            )
            collected.extend(step_groups)

    if collected:
        subtitle = f"{len(scenario_ids)} scenarios · " + ", ".join(participant_labels)
        # Snapshot the theme in the UI context — the build runs off-thread.
        report_theme = current_theme()

        def _build_scenarios(auto_print: bool) -> bytes:
            return build_multi_compare_report(
                title="Compare report — scenarios",
                subtitle=subtitle,
                groups=collected,
                theme=report_theme,
                auto_print=auto_print,
            )

        with report_holder:
            _render_report_buttons(
                build=_build_scenarios,
                base_filename=f"matofx_compare_scenarios_{len(scenario_ids)}",
            )

    _render_not_comparable(comparison.not_comparable, tool_names)


async def _resolve_tool_names(client: ApiClient, aliases: list[str]) -> dict[str, str]:
    """Best-effort ``{alias: human name}`` for section headings.

    ``JobResponse`` carries only the tool alias; the human name lives on
    the descriptor. Fetch each distinct tool once; on failure fall back
    to the alias so a missing / archived tool never blanks the heading.
    """
    if not aliases:
        return {}
    payloads = await asyncio.gather(
        *[client.get_tool(alias) for alias in aliases], return_exceptions=True
    )
    names: dict[str, str] = {}
    for alias, payload in zip(aliases, payloads, strict=True):
        if isinstance(payload, BaseException):
            names[alias] = alias
        else:
            names[alias] = str(payload.get("name") or alias)
    return names


def _scenario_participant_labels(
    comparison: ScenarioComparison,
    scenarios: list[dict[str, Any]],
    project_names: dict[int, str],
) -> list[str]:
    """Column headings for scenarios — name, project-prefixed when cross-project.

    The project prefix is added only when the set spans more than one
    project, so identically-named scenarios in different projects stay
    distinguishable.
    """
    labels: list[str] = []
    for name, scenario in zip(comparison.scenario_names, scenarios, strict=True):
        if comparison.cross_project:
            project_id = scenario.get("project_id")
            project = project_names.get(int(project_id), "") if project_id is not None else ""
            labels.append(f"{project} · {name}" if project else name)
        else:
            labels.append(name)
    return labels


def _render_scenario_header(comparison: ScenarioComparison, participant_labels: list[str]) -> None:
    """Top-of-view summary of which scenarios are being compared."""
    count = len(comparison.scenario_ids)
    summary = ", ".join(participant_labels)
    ui.label(f"Comparing {count} scenarios: {summary}.").style("color: var(--matofx-muted-text);")
    if comparison.cross_project:
        ui.label(
            "These scenarios span more than one project — each column shows its "
            "project for context."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")


def _render_not_comparable(
    items: tuple[NotComparableItem, ...], tool_names: dict[str, str]
) -> None:
    """List tools / workflows that are in only one of the chosen scenarios.

    Surfaced (rather than silently dropped) so the user understands why a
    tool they expected isn't shown as a comparison — there's only one
    instance of it, so there's nothing to compare it against.
    """
    if not items:
        return
    with ui.column().classes("w-full gap-1 q-mt-md"):
        ui.label("Not comparable (present in only one scenario)").style(
            "font-weight: 600; color: var(--matofx-muted-text);"
        )
        for item in items:
            display = tool_names.get(item.alias, item.alias) if item.kind == "tool" else item.name
            kind_word = "Tool" if item.kind == "tool" else "Workflow"
            ui.label(f"• {kind_word} {display} — only in {item.scenario_label}").style(
                "color: var(--matofx-muted-text); font-size: 0.85rem;"
            )


# ---------- data loading ----------------------------------------------------


class _LoadedJob:
    """One job's view-model + raw payload, assembled by :func:`_load_jobs`."""

    def __init__(
        self,
        *,
        job: dict[str, Any],
        descriptor: dict[str, Any],
        output_document: dict[str, Any],
        project_name: str = "",
        scenario_name: str = "",
        completed_display: str = "",
        vv_badge_kind: str = "",
    ) -> None:
        self.job = job
        self.descriptor = descriptor
        self.output_document = output_document
        self.cell_model = JobCellModel(
            job_id=int(job["id"]),
            job_label=f"Job {int(job['id']):07d} · {job.get('name') or ''}",
            state=str(job.get("state") or ""),
            descriptor=descriptor,
            project_name=project_name,
            scenario_name=scenario_name,
            tool_name=str(descriptor.get("name") or job.get("tool_alias") or ""),
            tool_version=str(job.get("tool_version") or ""),
            vv_badge_kind=vv_badge_kind,
            completed_display=completed_display,
            inputs=dict(job.get("inputs") or {}),
            output_values=_flatten_output_values(output_document),
            output_filenames=_flatten_output_filenames(output_document),
        )


async def _load_jobs(client: ApiClient, ids: list[int]) -> list[_LoadedJob]:
    """Fetch every selected job's metadata + descriptor + output document in parallel.

    ``GET /api/tools/{alias}`` returns a :class:`ToolDetailResponse`
    wrapper (``{alias, name, version, state, descriptor}``); we unwrap
    the inner ``descriptor`` here because the renderer walks
    ``descriptor.inputs`` / ``descriptor.input_groups`` directly. A
    missing wrapper or an empty inner descriptor falls back to ``{}``
    so the cell renderer surfaces "(none declared)" rather than
    crashing on ``None.get(...)``.
    """
    job_payloads = await asyncio.gather(*[client.get_job(jid) for jid in ids])
    aliases = list({str(j.get("tool_alias") or "") for j in job_payloads if j.get("tool_alias")})
    tool_payloads = await asyncio.gather(*[client.get_tool(alias) for alias in aliases])
    descriptor_by_alias: dict[str, dict[str, Any]] = {
        alias: dict(payload.get("descriptor") or {})
        for alias, payload in zip(aliases, tool_payloads, strict=True)
    }
    docs = await asyncio.gather(*[_safe_get_document(client, jid) for jid in ids])

    # V&V badges keyed by the job's pinned ``tool_version_id`` (ADR-0011 §8).
    # Fetched in one batched pass over the *distinct* version ids across
    # the selected columns so the Compare grid never produces an N+1
    # against ``/api/vv/status/tool/{id}``. Feature-off / API failure
    # collapses to empty so columns just don't show a badge.
    vv_badge_by_version_id: dict[int, str] = {}
    if vv_tracking_enabled():
        distinct_version_ids = sorted(
            {
                int(j["tool_version_id"])
                for j in job_payloads
                if j.get("tool_version_id") is not None
            }
        )
        if distinct_version_ids:
            badge_results = await asyncio.gather(
                *[client.vv_tool_status(vid) for vid in distinct_version_ids],
                return_exceptions=True,
            )
            for vid, result in zip(distinct_version_ids, badge_results, strict=True):
                if isinstance(result, BaseException):
                    continue
                vv_badge_by_version_id[vid] = str(result.get("kind") or "")

    # Resolve scenario + project names for the column headers. Each job
    # carries only ``scenario_id``; the name comes from the scenario row
    # (which also gives the project id) and the project list maps that to
    # a project name. Best-effort — a failed lookup leaves the header
    # line blank rather than aborting the comparison.
    scenario_ids = list(
        {int(j["scenario_id"]) for j in job_payloads if j.get("scenario_id") is not None}
    )
    scenario_payloads = await asyncio.gather(
        *[client.get_scenario(sid) for sid in scenario_ids], return_exceptions=True
    )
    scenario_by_id: dict[int, dict[str, Any]] = {}
    for sid, payload in zip(scenario_ids, scenario_payloads, strict=True):
        if not isinstance(payload, BaseException):
            scenario_by_id[sid] = payload
    try:
        project_name_by_id = {
            int(p["id"]): str(p.get("name") or p["id"]) for p in await client.list_projects()
        }
    except ApiError:
        project_name_by_id = {}

    def _context_for(job: dict[str, Any]) -> tuple[str, str, str]:
        """Return (project_name, scenario_name, completed_display) for a job."""
        scenario_id = job.get("scenario_id")
        scenario = scenario_by_id.get(int(scenario_id)) if scenario_id is not None else None
        scenario_name = str(scenario.get("name") or "") if scenario else ""
        project_id = scenario.get("project_id") if scenario else None
        project_name = project_name_by_id.get(int(project_id), "") if project_id is not None else ""
        completed_at = job.get("completed_at")
        completed_display = format_user_datetime(completed_at) if completed_at else ""
        return project_name, scenario_name, completed_display

    loaded: list[_LoadedJob] = []
    for job, doc in zip(job_payloads, docs, strict=True):
        project_name, scenario_name, completed_display = _context_for(job)
        job_vid = job.get("tool_version_id")
        vv_badge_kind = (
            vv_badge_by_version_id.get(int(job_vid), "") if job_vid is not None else ""
        )
        loaded.append(
            _LoadedJob(
                job=job,
                descriptor=descriptor_by_alias.get(str(job.get("tool_alias") or ""), {}),
                output_document=doc,
                project_name=project_name,
                scenario_name=scenario_name,
                completed_display=completed_display,
                vv_badge_kind=vv_badge_kind,
            )
        )
    return loaded


def _make_download_callback(client: ApiClient, job_id: int) -> Callable[[str], Any]:
    """Build a per-cell download callback the cell renderer can invoke.

    The renderer is sync but ``ApiClient.download_job_output`` is async —
    we return an async function the renderer's ``ui.button(on_click=...)``
    awaits. Errors surface as a ``ui.notify`` toast so a single failed
    file doesn't break the rest of the comparison view.
    """

    async def _download(filename: str) -> None:
        try:
            content = await client.download_job_output(job_id, filename)
        except ApiError as exc:
            ui.notify(
                f"Couldn't download {filename}: {exc.detail}",
                type="negative",
            )
            return
        ui.download.content(content, filename)

    return _download


async def _safe_get_document(client: ApiClient, job_id: int) -> dict[str, Any]:
    """Degrade gracefully so one job never aborts the whole comparison.

    - A missing-document 404 → empty doc (see the route docstring).
    - A post-approval lockdown (``ResultsWithheldError`` → 403 with a
      ``results_state`` discriminator, §16.15) → empty doc too. The
      endpoint already refused to serve the data; rendering an empty
      cell keeps the rest of the comparison intact rather than
      blowing up on a job whose results are withheld/pending.
    """
    try:
        return await client.get_job_output_document(job_id)
    except ApiError as exc:
        if exc.status_code == 404:
            return {"schema_version": 1, "tool_alias": "", "outputs": []}
        if exc.status_code == 403 and exc.payload.get("results_state"):
            return {"schema_version": 1, "tool_alias": "", "outputs": []}
        raise


def _flatten_output_values(doc: dict[str, Any]) -> dict[str, Any]:
    """Map output ``name`` → scalar value (or filename for file outputs)."""
    out: dict[str, Any] = {}
    for o in doc.get("outputs") or []:
        name = o.get("name")
        if not name:
            continue
        if o.get("type") == "file":
            out[name] = o.get("filename")
        else:
            out[name] = o.get("value")
    return out


def _flatten_output_filenames(doc: dict[str, Any]) -> dict[str, str]:
    """Map output ``name`` → filename, restricted to file-typed outputs."""
    return {
        o["name"]: o["filename"]
        for o in doc.get("outputs") or []
        if o.get("name") and o.get("type") == "file" and o.get("filename")
    }


# ---------- overlays / CSV download ----------------------------------------


async def _load_csv_outputs(client: ApiClient, bundles: list[_LoadedJob]) -> list[JobCsvOutput]:
    """Download every selected job's CSV file outputs once, up-front.

    The same list feeds the on-screen overlay charts and the HTML / PDF
    report builder, so the page only pays the network round-trip once.
    A failed download for one CSV is logged-and-skipped (via the
    ``return_exceptions=True`` gather) so the rest of the comparison
    still renders.

    Descriptor plot config (``plot_title`` / ``xaxis_title`` /
    ``yaxis_title`` / 0-indexed ``xaxis_column`` / ``yaxis_column``) is
    threaded into each :class:`JobCsvOutput` so the overlay can label
    its axes from the descriptor instead of the raw column name.
    """
    # Build a fast {output_name -> descriptor_field_dict} lookup so we
    # only pay one descriptor walk per job rather than re-scanning the
    # descriptor for every output.
    descriptor_outputs_by_job: dict[int, dict[str, dict[str, Any]]] = {}
    for bundle in bundles:
        outputs = bundle.cell_model.descriptor.get("outputs") or []
        descriptor_outputs_by_job[bundle.cell_model.job_id] = {
            str(o.get("name") or ""): o for o in outputs if o.get("name")
        }

    csv_descriptors: list[tuple[_LoadedJob, str, str, dict[str, Any]]] = []
    for bundle in bundles:
        descriptor_by_name = descriptor_outputs_by_job.get(bundle.cell_model.job_id, {})
        for output in bundle.output_document.get("outputs") or []:
            if output.get("type") != "file":
                continue
            filename = str(output.get("filename") or "")
            if not filename.lower().endswith(".csv"):
                continue
            output_name = str(output.get("name") or filename)
            descriptor_field = descriptor_by_name.get(output_name) or {}
            # Only csv_plot outputs feed the line-chart overlay below
            # the grid. ``csv_table`` outputs ride the unified output
            # preview loader and render as inline tables in the
            # per-cell view — shipping them through the line-chart
            # pipeline would surface them as plots regardless of the
            # descriptor's explicit choice, which was the 2026-05-14
            # feedback.
            declared_file_type = str(descriptor_field.get("file_type") or "")
            if declared_file_type and declared_file_type != "csv_plot":
                continue
            csv_descriptors.append((bundle, output_name, filename, descriptor_field))

    if not csv_descriptors:
        return []

    contents = await asyncio.gather(
        *[
            client.download_job_output(bundle.cell_model.job_id, filename)
            for bundle, _, filename, _ in csv_descriptors
        ],
        return_exceptions=True,
    )
    overlay_inputs: list[JobCsvOutput] = []
    for (bundle, csv_name, filename, descriptor_field), content in zip(
        csv_descriptors, contents, strict=True
    ):
        if isinstance(content, BaseException):
            continue
        overlay_inputs.append(
            JobCsvOutput(
                job_id=bundle.cell_model.job_id,
                job_label=bundle.cell_model.job_label,
                csv_name=csv_name,
                csv_filename=filename,
                content=content,
                plot_title=descriptor_field.get("plot_title") or None,
                xaxis_title=descriptor_field.get("xaxis_title") or None,
                yaxis_title=descriptor_field.get("yaxis_title") or None,
                xaxis_column=(
                    descriptor_field["xaxis_column"]
                    if isinstance(descriptor_field.get("xaxis_column"), int)
                    else None
                ),
                yaxis_column=(
                    descriptor_field["yaxis_column"]
                    if isinstance(descriptor_field.get("yaxis_column"), int)
                    else None
                ),
            )
        )
    return overlay_inputs


_IMAGE_EXTENSIONS_FOR_PRELOAD: frozenset[str] = frozenset(
    {"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg"}
)
_CAD_EXTENSIONS_FOR_PRELOAD: frozenset[str] = frozenset(
    {"stl", "stp", "step", "iges", "igs", "obj"}
)
_PDF_EXTENSIONS_FOR_PRELOAD: frozenset[str] = frozenset({"pdf"})
_ANIM3D_EXTENSIONS_FOR_PRELOAD: frozenset[str] = frozenset({"gltf", "glb"})
_VIDEO_EXTENSIONS_FOR_PRELOAD: frozenset[str] = frozenset(
    {"mp4", "webm", "ogg", "ogv", "mov", "m4v"}
)
_RICH_PREVIEW_EXTENSIONS_FOR_PRELOAD: frozenset[str] = (
    _IMAGE_EXTENSIONS_FOR_PRELOAD
    | _PDF_EXTENSIONS_FOR_PRELOAD
    | _ANIM3D_EXTENSIONS_FOR_PRELOAD
    | _VIDEO_EXTENSIONS_FOR_PRELOAD
)
"""Union of every extension the compare grid pre-fetches through the
plain ``/outputs/{filename}`` (or draft file-preview) endpoint — i.e.
no server-side transform needed, the bytes go to the browser as-is."""


def _is_previewable_output(file_type: str, extension: str) -> bool:
    """``True`` when an output should be pre-fetched for inline preview.

    Honours the descriptor's explicit ``file_type`` hint first; falls
    back to the extension for outputs that didn't declare one (legacy
    descriptors, or outputs the tool author marked as plain ``file``).
    csv_plot stays out — it rides the separate overlay-charts path
    below the grid.
    """
    if file_type in {"html_plot", "csv_table", "image", "pdf", "video"}:
        return True
    if file_type == "animation_3d":
        return extension in _ANIM3D_EXTENSIONS_FOR_PRELOAD
    if file_type == "cad_file":
        return extension in _CAD_EXTENSIONS_FOR_PRELOAD
    if file_type == "csv_plot":
        return False
    # No file_type declared → extension-based decision.
    return (
        extension in _RICH_PREVIEW_EXTENSIONS_FOR_PRELOAD
        or extension in _CAD_EXTENSIONS_FOR_PRELOAD
    )


def _is_cad_extension(extension: str) -> bool:
    return extension in _CAD_EXTENSIONS_FOR_PRELOAD


async def _load_output_preview_blobs(
    client: ApiClient, bundles: list[_LoadedJob]
) -> dict[tuple[int, str], bytes]:
    """Pre-fetch every previewable output's bytes in one pass.

    Returned dict is keyed by ``(job_id, output_name)``. Non-CAD
    outputs (HTML / CSV / image) come straight from the
    ``/outputs/{filename}`` download endpoint. CAD outputs (STL /
    STP / STEP / IGES / OBJ) come from the ``/outputs/{filename}/cad-preview``
    endpoint instead, which returns **binary STL** regardless of the
    source format — the cell renderer feeds these into the one STL
    viewer. Failed downloads are silently dropped so one broken
    artefact doesn't blank the whole comparison.
    """
    descriptor_outputs_by_job: dict[int, dict[str, dict[str, Any]]] = {}
    for bundle in bundles:
        descriptor_outputs_by_job[bundle.cell_model.job_id] = {
            str(o.get("name") or ""): o
            for o in (bundle.cell_model.descriptor.get("outputs") or [])
            if o.get("name")
        }

    # Split the descriptors into two buckets so each goes through the
    # right endpoint. CAD files route through the cad-preview endpoint
    # (which tessellates server-side); everything else goes through
    # the plain output download.
    cad_descriptors: list[tuple[int, str, str]] = []
    plain_descriptors: list[tuple[int, str, str]] = []
    for bundle in bundles:
        by_name = descriptor_outputs_by_job.get(bundle.cell_model.job_id, {})
        for output in bundle.output_document.get("outputs") or []:
            if output.get("type") != "file":
                continue
            filename = str(output.get("filename") or "")
            if not filename:
                continue
            output_name = str(output.get("name") or filename)
            descriptor_field = by_name.get(output_name) or {}
            file_type = str(descriptor_field.get("file_type") or "")
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if not _is_previewable_output(file_type, ext):
                continue
            entry = (bundle.cell_model.job_id, output_name, filename)
            if _is_cad_extension(ext):
                cad_descriptors.append(entry)
            else:
                plain_descriptors.append(entry)

    out: dict[tuple[int, str], bytes] = {}
    if plain_descriptors:
        contents = await asyncio.gather(
            *[client.download_job_output(jid, fn) for jid, _, fn in plain_descriptors],
            return_exceptions=True,
        )
        for (jid, name, _filename), content in zip(plain_descriptors, contents, strict=True):
            if isinstance(content, BaseException):
                continue
            out[(jid, name)] = content
    if cad_descriptors:
        cad_contents = await asyncio.gather(
            *[client.fetch_output_cad_preview(jid, filename=fn) for jid, _, fn in cad_descriptors],
            return_exceptions=True,
        )
        for (jid, name, _filename), content in zip(cad_descriptors, cad_contents, strict=True):
            if isinstance(content, BaseException):
                continue
            out[(jid, name)] = content
    return out


async def _load_input_preview_blobs(
    client: ApiClient, bundles: list[_LoadedJob]
) -> dict[tuple[int, str], bytes]:
    """Pre-fetch bytes for previewable ``file``-typed inputs.

    Walks each bundle's descriptor inputs, finds the ``file`` ones
    whose stored value path has a recognised preview extension, and
    routes each through the right endpoint:

    - Image / PDF / GLTF / GLB / video extensions go through the
      file-preview endpoint as-is.
    - CAD extensions (``.stl`` / ``.stp`` / ``.step`` / ``.iges`` /
      ``.igs`` / ``.obj``) go through the cad-preview endpoint, which
      returns **STL bytes** regardless of source format.

    The cell renderer then dispatches by the original filename
    extension — image rows still see image bytes; CAD rows see STL
    bytes and route to the STL viewer; PDF / GLTF / video rows route
    to their respective inline embedders. Returned dict is keyed by
    ``(job_id, field_name)``; failures drop silently.
    """
    cad_descriptors: list[tuple[int, str, str]] = []
    plain_descriptors: list[tuple[int, str, str]] = []
    for bundle in bundles:
        for field_spec in bundle.cell_model.descriptor.get("inputs") or []:
            if field_spec.get("type") != "file":
                continue
            field_name = str(field_spec.get("name") or "")
            if not field_name:
                continue
            value = bundle.cell_model.inputs.get(field_name)
            if not isinstance(value, str) or not value:
                continue
            ext = value.rsplit(".", 1)[-1].lower() if "." in value else ""
            entry = (bundle.cell_model.job_id, field_name, value)
            if _is_cad_extension(ext):
                cad_descriptors.append(entry)
            elif ext in _RICH_PREVIEW_EXTENSIONS_FOR_PRELOAD:
                plain_descriptors.append(entry)

    out: dict[tuple[int, str], bytes] = {}
    if plain_descriptors:
        contents = await asyncio.gather(
            *[client.fetch_draft_file_preview(jid, path=p) for jid, _, p in plain_descriptors],
            return_exceptions=True,
        )
        for (jid, name, _path), content in zip(plain_descriptors, contents, strict=True):
            if isinstance(content, BaseException):
                continue
            out[(jid, name)] = content
    if cad_descriptors:
        cad_contents = await asyncio.gather(
            *[client.fetch_draft_cad_preview(jid, path=p) for jid, _, p in cad_descriptors],
            return_exceptions=True,
        )
        for (jid, name, _path), content in zip(cad_descriptors, cad_contents, strict=True):
            if isinstance(content, BaseException):
                continue
            out[(jid, name)] = content
    return out


def _build_html_overlay_entries(bundles: list[_LoadedJob]) -> list[JobHtmlOutput]:
    """Synthesise the HTML overlay-section input list from already-fetched bytes.

    The "Show HTML outputs" expander below the grid wants a list of
    :class:`JobHtmlOutput` so it can title each entry with the job
    label. The bytes already live on each bundle's
    ``cell_model.output_preview_blobs`` after the unified output
    loader ran — we just wrap them in the value-object shape the
    overlay function expects, rather than firing a second download
    round-trip for the same files.
    """
    out: list[JobHtmlOutput] = []
    for bundle in bundles:
        for output in bundle.output_document.get("outputs") or []:
            if output.get("type") != "file":
                continue
            filename = str(output.get("filename") or "")
            if not filename.lower().endswith(".html"):
                continue
            name = str(output.get("name") or filename)
            content = bundle.cell_model.output_preview_blobs.get(name)
            if content is None:
                continue
            out.append(
                JobHtmlOutput(
                    job_id=bundle.cell_model.job_id,
                    job_label=bundle.cell_model.job_label,
                    html_name=name,
                    html_filename=filename,
                    content=content,
                )
            )
    return out


async def _load_field_outputs(
    client: ApiClient, bundles: list[_LoadedJob]
) -> list[JobFieldOutput]:
    """Fetch every selected job's ``field`` outputs' parsed frames in parallel.

    A ``field``-typed output (declared via the descriptor's
    ``file_type = "field"``) isn't a plain previewable blob — it's a
    mesh + per-timestep node values served by the ``field-frames``
    endpoint. We resolve which outputs are fields from the *descriptor*
    (the output document carries only ``name`` / ``filename``), then
    fetch each via :meth:`ApiClient.get_job_field_frames`. A failed
    fetch for one field is dropped (``return_exceptions=True``) so one
    broken series doesn't blank the whole comparison.
    """
    descriptor_outputs_by_job: dict[int, dict[str, dict[str, Any]]] = {}
    for bundle in bundles:
        descriptor_outputs_by_job[bundle.cell_model.job_id] = {
            str(o.get("name") or ""): o
            for o in (bundle.cell_model.descriptor.get("outputs") or [])
            if o.get("name")
        }

    specs: list[tuple[_LoadedJob, str, str, str]] = []
    for bundle in bundles:
        by_name = descriptor_outputs_by_job.get(bundle.cell_model.job_id, {})
        for output in bundle.output_document.get("outputs") or []:
            if output.get("type") != "file":
                continue
            output_name = str(output.get("name") or "")
            if not output_name:
                continue
            descriptor_field = by_name.get(output_name) or {}
            if str(descriptor_field.get("file_type") or "") != "field":
                continue
            filename = str(output.get("filename") or "")
            label = str(descriptor_field.get("label") or output_name)
            specs.append((bundle, output_name, label, filename))

    if not specs:
        return []

    payloads = await asyncio.gather(
        *[client.get_job_field_frames(b.cell_model.job_id, name) for b, name, _, _ in specs],
        return_exceptions=True,
    )
    out: list[JobFieldOutput] = []
    for (bundle, name, label, filename), payload in zip(specs, payloads, strict=True):
        if isinstance(payload, BaseException):
            continue
        out.append(
            JobFieldOutput(
                job_id=bundle.cell_model.job_id,
                job_label=bundle.cell_model.job_label,
                field_name=name,
                field_label=label,
                filename=filename,
                data=payload,
            )
        )
    return out


# ---------- report buttons --------------------------------------------------


def _render_report_buttons(
    *,
    build: Callable[[bool], bytes],
    base_filename: str,
) -> None:
    """Two buttons: download HTML report / download PDF report.

    ``build(auto_print)`` returns the report HTML bytes — the caller
    supplies it so this helper is agnostic to single-group
    (:func:`build_compare_report`) vs. overall multi-group
    (:func:`build_multi_compare_report`) reports.

    The HTML version is a regular ``ui.download.content`` — the user's
    browser saves the file and they open it whenever. The PDF version
    builds the same HTML *with an auto-print script* and opens it in a
    new tab via a blob URL — the browser's print dialog appears, the
    user clicks "Save as PDF". Bypasses kaleido entirely; the rendering
    is whatever the user's browser does, which is rock-solid.
    """

    async def _on_download_html() -> None:
        # Build off the event loop: the report renders every chart + base64s
        # every image into one HTML doc, which is heavy enough to freeze the
        # UI (and drop the websocket) if run inline. ``run.io_bound`` keeps
        # the loop responsive; an ongoing notification covers the wait and is
        # always dismissed.
        note = ui.notification("Building report…", spinner=True, timeout=None)
        try:
            data = await ng_run.io_bound(build, False)
        finally:
            note.dismiss()
        ui.download.content(data, f"{base_filename}.html")

    async def _on_download_pdf() -> None:
        # Build the auto-print HTML, base64-encode, and ask the browser
        # to open it in a new tab via a blob URL. The auto-print script
        # in the HTML head fires the print dialog ~800 ms after load,
        # by which point plotly has rendered the charts. The build runs off
        # the loop (see ``_on_download_html``).
        note = ui.notification("Building report…", spinner=True, timeout=None)
        try:
            html_bytes = await ng_run.io_bound(build, True)
        finally:
            note.dismiss()
        b64 = base64.b64encode(html_bytes).decode("ascii")
        js = (
            "(function() {"
            f"  const b64 = {json.dumps(b64)};"
            "  const bin = atob(b64);"
            "  const bytes = new Uint8Array(bin.length);"
            "  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);"
            "  const blob = new Blob([bytes], {type: 'text/html;charset=utf-8'});"
            "  const url = URL.createObjectURL(blob);"
            "  window.open(url, '_blank');"
            "})();"
        )
        ui.run_javascript(js)

    with ui.row().classes("w-full justify-end gap-2"):
        ui.button(
            "Download HTML report",
            icon="description",
            on_click=_on_download_html,
        ).props("color=primary")
        ui.button(
            "Download PDF report",
            icon="picture_as_pdf",
            on_click=_on_download_pdf,
        ).props("color=primary")
