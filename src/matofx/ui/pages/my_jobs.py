"""My Jobs (``/ui/my-jobs``) — the unified workspace.

Replaces the Phase-1 flat list. Single screen where a user picks a
Project → Scenario → Tool/Workflow → Job-ID and the right pane
re-shapes itself based on the selected job's state — direct port of
the legacy ``my_jobs.py`` workspace pattern locked in
``feedback_my_jobs_unified_workspace.md``.

URL contract (deep-link aliases for ``/jobs/{id}`` and
``/scenarios/{id}/submit`` resolve to query-string variants of this
page in :mod:`matofx.ui.pages.job` and :mod:`matofx.ui.pages.submit`):

- ``?proj=<int>&scen=<int>&tool=<alias>&job=<int>``  — fully-qualified
  selection.
- ``?job=<int>``  — workspace fetches the job, back-fills proj/scen/tool
  from its row, and shows the state-shaped right pane.
- ``?scenario=<int>&pick_tool=1``  — opens the tool-picker on the right
  pane (no draft created until the user clicks a tool, per Q2=ii in
  the rewrite plan).
- no params  — empty-state right pane shows "Recent activity"
  (last ~10 jobs the user touched).

Every label flows through :mod:`matofx.ui.format` so timestamps render
``08 May 2026 14:32:11 BST (13:32:11 UTC)``, job IDs zero-pad to seven
digits, and tool **names** (not aliases) are surfaced everywhere.
"""

from __future__ import annotations

import base64
from typing import Any

from nicegui import ui

from matofx.ui._features import (
    file_library_enabled,
    materials_database_enabled,
    vv_tracking_enabled,
)
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_job_id, format_job_state, format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.pages.compare.selection import (
    MAX_SELECTION,
    MIN_SELECTION,
    detect_cross_tool,
)
from matofx.ui.pages.job import _TERMINAL_STATES, _render_body
from matofx.ui.state import current_is_approver, current_token, is_admin

_RECENT_ACTIVITY_LIMIT = 10

#: How often the recent-activity panel re-checks the state of the jobs it
#: is showing. Slower than the selected job's own 2 s tick: this is an
#: at-a-glance list, not something anyone watches line by line.
_ACTIVITY_REFRESH_INTERVAL_S = 5.0


# --- Page entry point ----------------------------------------------------


@ui.page("/my-jobs")
async def my_jobs_workspace(
    proj: int | None = None,
    scen: int | None = None,
    tool: str | None = None,
    job: int | None = None,
    workflow: str | None = None,
    workflow_run: int | None = None,
    pick_tool: int = 0,
    new: int = 0,
) -> None:
    """Render the unified workspace.

    Reads URL state, fetches the supporting catalogues, builds the
    selector bar at the top, and dispatches the right pane based on
    the resolved selection.

    The ``new=1`` query flag suppresses the auto-pick of the most
    recent existing job (Rule 3 in :func:`_resolve_selection`) so the
    workspace falls through to the submit-form branch. Drives the
    "+ New run" button in the selector bar.

    ``workflow_run=<id>`` opens the tabbed workflow-run view inline
    in the right pane. Workflow runs render exactly like standalone
    jobs except the right pane carries one tab per member tool — the
    locked-in pattern from the legacy app.
    """
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())

    # When the URL gave us only ``job=...`` we back-fill the cascading
    # selectors from the job's row so the picker labels match the
    # current view (per the bidirectional-sync requirement).
    selection = await _resolve_selection(
        client,
        proj=proj,
        scen=scen,
        tool_alias=tool,
        job_id=job,
        workflow=workflow,
        workflow_run_id=workflow_run,
        suppress_auto_pick=bool(new),
    )

    # A specific job / workflow run is open when either id resolved.
    # Drives both the Back button (next to the title) and whether the
    # free-text search card shows — the two are complements: search to
    # find a job on the "main" view, Back to return there from a job.
    viewing_job = selection.job_id is not None or selection.workflow_run_id is not None

    with ui.column().classes("w-full mx-auto p-6 gap-4").style("max-width: 1700px;"):
        with ui.row().classes("items-center gap-2"):
            if viewing_job:
                # Clears every selector by navigating to /my-jobs with no
                # query string, landing back on the Recent-activity +
                # search "main" view the user clicked the job from.
                ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/my-jobs")).props(
                    "flat round dense"
                ).tooltip("Back to My Jobs")
            ui.label("My Jobs").style("font-size: 1.5rem; font-weight: 600;")

        # Selector bar — fetches the catalogues fresh on every render so
        # newly-created projects / scenarios / jobs show up without a
        # full page refresh.
        try:
            await _render_selector_bar(client, selection)
        except ApiError as exc:
            ui.label(f"Couldn't load the selectors: {exc.detail}").style("color: #c0392b;")
            return

        # Free-text job search in its own card directly below the selector
        # bar — shown only on the "main" view (no job open) so a job's
        # results pane stays focused once the user drills in.
        if not viewing_job:
            await _render_search_card(client)

        right_pane = ui.column().classes("w-full gap-4")
        with right_pane:
            await _render_right_pane(client, selection, pick_tool_mode=bool(pick_tool))


# --- Selection resolution ------------------------------------------------


class _Selection:
    """Live-resolved state of the workspace's cascading pickers.

    Built up from URL params + (when only ``job`` is supplied)
    follow-up lookups against the API. Carries the IDs, names, and
    the loaded ``Job`` / ``WorkflowRun`` dicts so the right-pane
    router doesn't have to re-fetch.
    """

    def __init__(self) -> None:
        self.project_id: int | None = None
        self.scenario_id: int | None = None
        self.tool_alias: str | None = None
        self.workflow_name: str | None = None
        self.workflow_run_id: int | None = None
        self.workflow_run: dict[str, Any] | None = None
        self.job_id: int | None = None
        self.job: dict[str, Any] | None = None


async def _resolve_selection(
    client: ApiClient,
    *,
    proj: int | None,
    scen: int | None,
    tool_alias: str | None,
    job_id: int | None,
    workflow: str | None,
    workflow_run_id: int | None = None,
    suppress_auto_pick: bool = False,
) -> _Selection:
    """Build a :class:`_Selection` from URL params, back-filling as needed.

    Back-fill rules:

    1. ``workflow_run=`` → fetch the run + back-fill scenario / project
       so the selector bar pickers match. Auto-picks the first non-
       terminal child as the active tab (matches the legacy default).
    2. ``job=`` only → fetch the job and copy ``scenario_id`` /
       ``tool_alias`` off the row. If the job is a workflow child,
       back-fill ``workflow_run_id`` too so the right pane opens the
       tabbed view.
    3. ``scen=`` only → fetch the scenario to fill ``project_id``.
    4. Auto-pick a Job-ID when ``proj`` + ``scen`` + ``tool`` are all
       set but no ``job`` — the picker should land on the most-recent
       run of that tool in that scenario.
    """
    sel = _Selection()
    sel.project_id = proj
    sel.scenario_id = scen
    sel.tool_alias = tool_alias
    sel.workflow_name = workflow
    sel.workflow_run_id = workflow_run_id
    sel.job_id = job_id

    # Rule 1: workflow_run-only / workflow_run-leading → back-fill the
    # cascading pickers from the run row. **Tool selector stays empty**
    # when a workflow is active — the two are at the same conceptual
    # level (you're either looking at a standalone tool or a workflow,
    # never both), so the Tool dropdown reads as empty to signal the
    # user is in workflow mode.
    if workflow_run_id is not None:
        try:
            sel.workflow_run = await client.get_workflow_run(int(workflow_run_id))
        except ApiError:
            sel.workflow_run = None
        if sel.workflow_run is not None:
            sel.scenario_id = sel.scenario_id or int(sel.workflow_run.get("scenario_id") or 0)
            sel.project_id = sel.project_id or int(sel.workflow_run.get("project_id") or 0)
            sel.workflow_name = (
                sel.workflow_name or str(sel.workflow_run.get("workflow_alias") or "") or None
            )
            # Auto-pick the run's first non-terminal child as the
            # active Job so the Job-ID picker matches the visible tab
            # — same heuristic the workflow_run renderer uses for its
            # default active tab. Callers that passed ``job=`` win.
            # We deliberately *don't* back-fill ``sel.tool_alias`` here:
            # the Tool selector stays empty in workflow mode (see the
            # docstring above).
            if sel.job_id is None:
                children = list(sel.workflow_run.get("children") or [])
                chosen = next(
                    (c for c in children if c.get("state") not in ("complete", "failed")),
                    children[0] if children else None,
                )
                if chosen is not None:
                    sel.job_id = int(chosen.get("id") or 0) or None

    # Rule 2: job-only → back-fill scenario from the row. If the job
    # is a workflow child, also back-fill workflow_run_id so the right
    # pane opens the tabbed workspace view on it; we *don't* back-fill
    # the Tool selector in that case because the Tool selector tracks
    # standalone jobs (Tool and Workflow are mutually exclusive).
    if job_id is not None:
        try:
            sel.job = await client.get_job(job_id)
        except ApiError:
            return sel  # sel.job stays None — right pane shows the error.
        # A parametric-study / optimisation-study child carries
        # ``scenario_id = None`` (its home is the study, not a scenario),
        # so guard the back-fill — ``int(None)`` would crash the whole
        # page and is exactly why some cohort → My Jobs links 500'd. The
        # cascade just shows an empty Scenario for such a run; the right
        # pane still opens the job from ``sel.job``.
        job_scen = sel.job.get("scenario_id")
        if sel.scenario_id is None and job_scen is not None:
            sel.scenario_id = int(job_scen)
        job_run_id = sel.job.get("workflow_run_id")
        if job_run_id and sel.workflow_run_id is None:
            sel.workflow_run_id = int(job_run_id)
            try:
                sel.workflow_run = await client.get_workflow_run(int(job_run_id))
            except ApiError:
                sel.workflow_run = None
        if not job_run_id:
            # Standalone job — back-fill the Tool selector so the
            # active tool is visible in the cascade.
            sel.tool_alias = sel.tool_alias or str(sel.job["tool_alias"])

    # Rule 3: have scenario, missing project → look it up.
    if sel.project_id is None and sel.scenario_id is not None:
        try:
            scenario = await client.get_scenario(sel.scenario_id)
            sel.project_id = int(scenario["project_id"])
        except ApiError:
            pass

    # Rule 4: scenario + tool picked but no job yet → auto-pick the
    # most recent **standalone** run of that tool in that scenario.
    # Drops the user straight into "show me what I did last time"
    # mode. Skipped when the caller passed ``new=1`` (the "+ New run"
    # button) — there the user wants the form, not the existing run.
    # Also skipped on workflow_run paths — the workflow view picks
    # its own active tab. Workflow-child jobs are deliberately
    # filtered out: they're surfaced via the Workflow dropdown, not
    # the Tool one (the two selectors are mutually exclusive).
    #
    # We list the **scenario's** jobs (newest-first, ACL-scoped) and
    # take the first standalone run of this tool. This is deliberately
    # scenario-scoped rather than a cross-project visible-jobs query:
    # a user who has run the same tool many times in *other* scenarios
    # would push the one run in *this* scenario outside any global
    # recent-N window, the auto-pick would miss it, and the right pane
    # would silently fall through to creating a fresh draft instead of
    # opening the existing job. Scoping to the scenario keeps the
    # "one tool, one run, populate immediately" promise regardless of
    # how busy the install is elsewhere.
    if (
        not suppress_auto_pick
        and sel.workflow_run_id is None
        and sel.job_id is None
        and sel.scenario_id is not None
        and sel.tool_alias is not None
    ):
        try:
            scenario_jobs = await client.list_jobs(int(sel.scenario_id))
        except ApiError:
            scenario_jobs = []
        match = next(
            (
                j
                for j in scenario_jobs
                if str(j["tool_alias"]) == sel.tool_alias and not j.get("workflow_run_id")
            ),
            None,
        )
        if match is not None:
            sel.job_id = int(match["id"])
            try:
                sel.job = await client.get_job(sel.job_id)
            except ApiError:
                sel.job = None

    return sel


# --- Selector bar --------------------------------------------------------


async def _render_selector_bar(client: ApiClient, selection: _Selection) -> None:
    """Five horizontal selectors: Project, Scenario, Tool, Workflow, Job-ID."""
    projects = await client.list_projects()
    project_options = {int(p["id"]): str(p["name"]) for p in projects}

    scenarios: list[dict[str, Any]] = []
    if selection.project_id is not None:
        try:
            scenarios = await client.list_scenarios(selection.project_id)
        except ApiError:
            scenarios = []
    scenario_options = {int(s["id"]): str(s["name"]) for s in scenarios}

    # Tool catalogue (workspace's right pane uses tool names not aliases
    # per the pretty-printing rule). Labels carry the version too —
    # "2D Heat Transfer v1.0.0" — so the user can distinguish runs
    # against different installed versions of the same tool without
    # having to open the job.
    try:
        tools = await client.list_tools()
    except ApiError:
        tools = []
    all_tool_labels = {str(t["alias"]): f"{t['name']} v{t['version']}" for t in tools}

    # The Tool dropdown is for navigating between existing **standalone**
    # tool runs in the selected Scenario, never the full install
    # catalogue and never the children of workflow runs. We only
    # populate it when both Project and Scenario are picked, and we
    # restrict it to aliases that have a non-workflow job in that
    # scenario. Workflow children are reached via the Workflow
    # dropdown — the two selectors are mutually exclusive at the
    # conceptual level (you're looking at *either* a standalone tool
    # *or* a workflow run, never both).
    tool_options: dict[str, str] = {}
    if selection.project_id is not None and selection.scenario_id is not None:
        try:
            scenario_jobs = await client.list_jobs(int(selection.scenario_id))
        except ApiError:
            scenario_jobs = []
        used_aliases = {str(j["tool_alias"]) for j in scenario_jobs if not j.get("workflow_run_id")}
        tool_options = {a: n for a, n in all_tool_labels.items() if a in used_aliases}

    with (
        ui.card().classes("w-full"),
        ui.row().classes("w-full items-center gap-3 no-wrap"),
    ):
        # Project picker — always enabled; this is the entry point of
        # the cascade.
        ui.select(
            project_options or {0: "(no projects)"},
            label="Project",
            value=selection.project_id if selection.project_id in project_options else None,
            on_change=lambda e: _navigate(proj=int(e.value) if e.value else None),
        ).classes("min-w-[180px]")

        # Scenario picker — only meaningful once a Project is chosen.
        # Dropdown is grayed out until that happens, with an inline hint
        # in the placeholder so the user knows what to do next.
        scenario_disabled = selection.project_id is None
        scenario_placeholder = "(pick a project first)" if scenario_disabled else "(no scenarios)"
        scenario_select = ui.select(
            scenario_options or {0: scenario_placeholder},
            label="Scenario",
            value=selection.scenario_id if selection.scenario_id in scenario_options else None,
            on_change=lambda e: _navigate(
                proj=selection.project_id, scen=int(e.value) if e.value else None
            ),
        ).classes("min-w-[180px]")
        if scenario_disabled:
            scenario_select.props("disable")

        # Tool picker — only meaningful once a Scenario is chosen, and
        # then narrowed to tools that already have a job in that
        # Scenario. Adding a new tool to a Scenario is a separate
        # launch flow, not this dropdown.
        tool_disabled = selection.project_id is None or selection.scenario_id is None
        if tool_disabled:
            tool_placeholder = "(pick a scenario first)"
        elif not tool_options:
            tool_placeholder = "(no tools used in this scenario yet)"
        else:
            tool_placeholder = "(pick a tool)"
        tool_options_for_select: dict[str, str] = (
            tool_options if tool_options else {"": tool_placeholder}
        )
        tool_select = ui.select(
            tool_options_for_select,
            label="Tool",
            value=selection.tool_alias if selection.tool_alias in tool_options else None,
            on_change=lambda e: _navigate(
                proj=selection.project_id,
                scen=selection.scenario_id,
                tool=str(e.value) if e.value else None,
            ),
        ).classes("min-w-[200px]")
        if tool_disabled:
            tool_select.props("disable")

        # Workflow picker — when both Project + Scenario are picked,
        # lists every workflow run on that scenario so the user can
        # switch between them inline. Selecting a run navigates to the
        # tabbed workspace view (``?workflow_run=<id>``).
        workflow_runs: list[dict[str, Any]] = []
        if selection.project_id is not None and selection.scenario_id is not None:
            try:
                workflow_runs = await client.list_workflow_runs_for_scenario(
                    int(selection.scenario_id)
                )
            except ApiError:
                workflow_runs = []
        workflow_run_options: dict[int, str] = {
            int(r["id"]): _workflow_run_picker_label(r) for r in workflow_runs
        }
        workflow_disabled = (
            selection.project_id is None
            or selection.scenario_id is None
            or not workflow_run_options
        )
        if selection.project_id is None or selection.scenario_id is None:
            workflow_placeholder = "(pick a scenario first)"
        elif not workflow_run_options:
            workflow_placeholder = "(no workflow runs in this scenario)"
        else:
            workflow_placeholder = "(pick a workflow run)"
        workflow_options_for_select: dict[int, str] = (
            workflow_run_options if workflow_run_options else {0: workflow_placeholder}
        )
        workflow_select = ui.select(
            workflow_options_for_select,
            label="Workflow",
            value=(
                selection.workflow_run_id
                if selection.workflow_run_id in workflow_run_options
                else None
            ),
            on_change=lambda e: _navigate(
                proj=selection.project_id,
                scen=selection.scenario_id,
                workflow_run=int(e.value) if e.value else None,
            ),
        ).classes("min-w-[220px]")
        if workflow_disabled:
            workflow_select.props("disable")

        # Job-ID search-as-you-type — calls /api/users/me/visible-jobs
        # with a `q=` filter on every keystroke (debounced by Quasar's
        # default). The dropdown shows just the zero-padded Job ID,
        # because the Project / Scenario / Tool selectors above
        # already carry the rest of the context.
        await _render_job_id_picker(client, selection)


async def _render_job_id_picker(client: ApiClient, selection: _Selection) -> None:  # noqa: ARG001
    """Typed Job-ID lookup that jumps straight to *any* visible job.

    The earlier dropdown only filtered a handful of pre-loaded recent
    jobs client-side, so typing an ID that wasn't in that slice did
    nothing. This is a plain field: type a Job ID, press Enter, and the
    workspace navigates to ``?job=<id>`` — which back-fills Project /
    Scenario / Tool (or Workflow) from the job's row. Zero-padding is
    tolerated (``0000008`` → 8); a job in a project you can't see, or a
    bad ID, surfaces as the right pane's "couldn't load" message rather
    than a silent no-op. Discovery by name lives in the search card
    below the selector bar.
    """
    current = format_job_id(selection.job_id) if selection.job_id is not None else ""
    job_input = (
        ui.input(label="Job ID", value=current, placeholder="type an ID + Enter")
        .props("dense outlined clearable")
        .classes("min-w-[180px]")
    )
    job_input.tooltip("Type a Job ID and press Enter to jump straight to it")

    def _go(_e: Any = None) -> None:
        digits = "".join(ch for ch in str(job_input.value or "") if ch.isdigit())
        _navigate(job=int(digits) if digits else None)

    job_input.on("keydown.enter", _go)


def _workflow_run_picker_label(run: dict[str, Any]) -> str:
    """Label for a workflow run in the selector bar's Workflow dropdown.

    Friendly user-facing form: the workflow name (or alias as
    fallback) + the run's own name in parentheses so two runs of the
    same workflow on the same scenario can be told apart. (Today
    the schema forbids that, but future tweaks might allow it.)
    """
    workflow_name = str(run.get("workflow_name") or run.get("workflow_alias") or "Workflow")
    own_name = str(run.get("name") or "")
    if own_name and own_name != workflow_name:
        return f"{workflow_name} — {own_name}"
    return workflow_name


# --- Free-text search card -----------------------------------------------


_SEARCH_RESULT_LIMIT = 25
"""How many matches the free-text job search returns at once."""


async def _render_search_card(client: ApiClient) -> None:
    """Free-text job search, in its own card below the selector bar.

    Looks jobs up by name via the visible-jobs endpoint's ``q=`` filter
    (case-insensitive substring on the job name, ACL-scoped to projects
    the user can see). Distinct from the selector bar's Job-ID picker —
    that one navigates by zero-padded ID; this is the "I remember part
    of the name, not the number" path. Matches render as clickable rows
    that drop the workspace onto the chosen job via the same
    ``?job=<id>`` deep-link the recent-activity cards use.

    The query runs ``debounce``-d on the Quasar side (300 ms) so a
    burst of keystrokes collapses into one round-trip; an empty box
    clears the results rather than listing everything.
    """
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        search_input = (
            ui.input(placeholder="Search jobs by name…")
            .props("clearable dense outlined debounce=300")
            .classes("w-full")
        )
        with search_input.add_slot("prepend"):
            ui.icon("search")
        results_container = ui.column().classes("w-full gap-1")

        async def _on_search() -> None:
            term = (search_input.value or "").strip()
            results_container.clear()
            if not term:
                return
            try:
                rows = await client.list_my_visible_jobs(q=term, limit=_SEARCH_RESULT_LIMIT)
            except ApiError as exc:
                with results_container:
                    ui.label(f"Search failed: {exc.detail}").style("color: #c0392b;")
                return
            with results_container:
                if not rows:
                    ui.label(f"No jobs match “{term}”.").style("color: #888;")
                    return
                for row in rows:
                    _render_search_result_row(row)

        search_input.on_value_change(_on_search)


def _render_search_result_row(row: dict[str, Any]) -> None:
    """One clickable match in the free-text search results.

    Same card shape as the recent-activity rows so the two
    list-of-jobs surfaces read consistently; clicking navigates the
    workspace onto the job.
    """
    job_id = int(row["id"])
    with (
        ui.card()
        .classes("w-full cursor-pointer")
        .on("click", lambda _e=None, _jid=job_id: _navigate(job=_jid)),
        ui.row().classes("items-center justify-between w-full no-wrap"),
    ):
        with ui.column().classes("gap-1"):
            ui.label(f"{format_job_id(job_id)} · {row['name']}").style("font-weight: 600;")
            ui.label(f"{row['tool_name']} · {row['project_name']} / {row['scenario_name']}").style(
                "color: #888; font-size: 0.85rem;"
            )
        ui.badge(format_job_state(row["state"])).props(
            _state_badge_color(str(row.get("state") or ""))
        )


# --- URL navigation ------------------------------------------------------


def _navigate(
    *,
    proj: int | None = None,
    scen: int | None = None,
    tool: str | None = None,
    workflow: str | None = None,
    workflow_run: int | None = None,
    job: int | None = None,
    new: int | None = None,
) -> None:
    """Push a new workspace URL with the supplied query params.

    Per the locked navigation contract: every selection change
    ``ui.navigate.to`` -s the new URL. Browser back-button steps
    through every selection — we accept this slightly noisier
    history for the simpler, more reliable implementation; a future
    pass can split push-vs-replace per the Q4 middle-ground rule.

    ``new=1`` forces the workspace into "submit a fresh run" mode by
    suppressing Rule 4's auto-pick.

    ``workflow_run=<id>`` opens the tabbed workflow-run view in the
    right pane. Combined with ``job=<id>`` it preselects which child
    tab opens first.
    """
    parts: list[str] = []
    if proj is not None:
        parts.append(f"proj={proj}")
    if scen is not None:
        parts.append(f"scen={scen}")
    if tool is not None:
        parts.append(f"tool={tool}")
    if workflow is not None:
        parts.append(f"workflow={workflow}")
    if workflow_run is not None:
        parts.append(f"workflow_run={workflow_run}")
    if job is not None:
        parts.append(f"job={job}")
    if new:
        parts.append(f"new={new}")
    qs = "?" + "&".join(parts) if parts else ""
    ui.navigate.to(f"/my-jobs{qs}")


# --- Right-pane router ---------------------------------------------------


_APPROVAL_NOTICES: dict[str, tuple[str, str]] = {
    "pending_approval": (
        "Awaiting approval",
        "This simulation needs an approver's sign-off before it can run. "
        "The approver(s) have been notified; you'll be emailed when it's "
        "approved or denied.",
    ),
    "denied": (
        "Denied",
        "This simulation was denied and did not run. The approver's "
        "reason was sent to you by email.",
    ),
    "pending_results": (
        "Results awaiting approval",
        "This simulation finished, but its results are held for an "
        "approver to review before release. You'll be emailed when they "
        "are released.",
    ),
    "results_withheld": (
        "Results withheld",
        "An approver reviewed this simulation and withheld its results, "
        "so they aren't available. Contact an administrator if you "
        "believe this is a mistake.",
    ),
}


def _render_approval_notice(state: str, job: dict[str, Any] | None) -> None:
    """Read-only notice for an approval-gated job (§16.15).

    My Jobs never *acts* on these states — the dedicated ``/approvals``
    page is the only place to decide. The requestor gets a plain
    explanation; a user who is themselves an approver gets a shortcut
    into the inbox.
    """
    title, body = _APPROVAL_NOTICES.get(
        state, ("Awaiting approval", "This simulation is awaiting approval.")
    )
    job_id = int(job["id"]) if job and "id" in job else None
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.badge(format_job_state(state)).props("color=deep-orange")
            if job_id is not None:
                ui.label(format_job_id(job_id)).style("color: #888;")
            # Name it, like every other state now does — an approval
            # notice that only quotes an id makes the user go and look
            # up which run it is talking about.
            job_name = str((job or {}).get("name") or "")
            if job_name:
                ui.label(job_name).style("font-weight: 600;")
        ui.label(title).style("font-size: 1.1rem; font-weight: 600;")
        ui.label(body).style("color: #888;")
        if current_is_approver():
            ui.button(
                "Go to Approvals",
                on_click=lambda: ui.navigate.to("/approvals"),
            ).props("color=primary")


async def _render_right_pane(
    client: ApiClient, selection: _Selection, *, pick_tool_mode: bool
) -> None:
    """Dispatch to the right pane for the current selection."""
    # 0. Workflow run picked → tabbed workspace view. The tab body
    # uses the same per-job rendering you'd see for a standalone job;
    # the tab strip is what makes the workflow case visually distinct.
    if selection.workflow_run_id is not None:
        from matofx.ui.pages.workflow_run import (  # noqa: PLC0415
            render_workflow_run_workspace,
        )

        body_container = ui.column().classes("w-full gap-4")
        await render_workflow_run_workspace(
            client,
            int(selection.workflow_run_id),
            body_container,
            active_job_id=selection.job_id,
        )
        return

    # 1. Job picked → branch on state.
    if selection.job_id is not None and selection.job is not None:
        state = str(selection.job.get("state") or "")
        if state == "created":
            # Draft job — show the form for the user to fill in. On
            # submit the existing draft is promoted to ``queued`` via
            # ``POST /api/jobs/{id}/queue``.
            await _render_submit_form(client, selection)
            return
        if state in (
            "pending_approval",
            "denied",
            "pending_results",
            "results_withheld",
        ):
            # Approval-gated states (§16.15). My Jobs is read-only for
            # these — the dedicated /approvals page is the only place
            # to act. Show the requestor an explanation; give an
            # approver a shortcut over to the inbox.
            _render_approval_notice(state, selection.job)
            return
        # Non-draft → state-shaped body (delegates to job.py's renderer).
        # The body container is given a *named* variable here (rather
        # than a throwaway inline ``ui.column()``) so the polling
        # closure inside ``_render_inflight`` has an unambiguous,
        # garbage-collection-anchored reference to clear + re-render
        # when the job state transitions to ``complete`` / ``failed``.
        body_container = ui.column().classes("w-full gap-4")
        await _render_body(client, selection.job, selection.job_id, body_container)
        if state in ("queued", "running"):
            # Belt-and-braces outer refresh — see _wire_outer_refresh.
            await _wire_outer_refresh(client, selection.job_id, body_container)
        return
    if selection.job_id is not None and selection.job is None:
        ui.label(
            f"We couldn't load Job {format_job_id(selection.job_id)}. "
            "It may have been archived, or you may not have access."
        ).style("color: #c0392b;")
        return

    # 2. Scenario + tool picked, no job → create a draft on the fly
    #    so the Job-ID picker populates from the moment the form opens.
    #    Idempotent endpoint — if a draft already exists for this
    #    (scenario, tool) it's returned and we navigate onto it; the
    #    next render falls into branch 1 with state="created".
    if selection.scenario_id is not None and selection.tool_alias is not None:
        await _create_draft_and_navigate(client, selection)
        return

    # 3. Scenario picked, pick_tool_mode → tool picker for a new job.
    if selection.scenario_id is not None and pick_tool_mode:
        await _render_tool_picker(client, selection)
        return

    # 4. Otherwise → recent activity panel (empty-state).
    await _render_recent_activity(client)


async def _create_draft_and_navigate(client: ApiClient, selection: _Selection) -> None:
    """POST a draft to the API, then navigate the workspace onto it.

    Idempotent under the unique constraint locked 2026-05-08 — if a
    job for this (scenario, tool) already exists, the API returns it
    and we land on the existing one.
    """
    if selection.scenario_id is None or selection.tool_alias is None:
        return
    try:
        draft = await client.create_or_get_draft_job(
            selection.scenario_id, tool_alias=selection.tool_alias
        )
    except ApiError as exc:
        ui.label(f"Couldn't open the form for this tool: {exc.detail}").style("color: #c0392b;")
        return
    _navigate(
        proj=selection.project_id,
        scen=selection.scenario_id,
        tool=selection.tool_alias,
        job=int(draft["id"]),
    )


async def _wire_outer_refresh(client: ApiClient, job_id: int, body_container: Any) -> None:
    """Outer-scope polling that catches the in-flight → terminal transition.

    Belt-and-braces against the inner timer in ``_render_inflight``
    being cleared mid-tick when ``container.clear()`` happens. Polls
    every 5 s; on a terminal state, calls ``_render_body`` again with
    the fresh job and stops. Because the inner timer also fires every
    2 s, the typical case has the inner timer winning the race; the
    outer is a safety net.
    """
    state_holder: dict[str, Any] = {"timer": None}

    async def _outer_tick() -> None:
        try:
            fresh = await client.get_job(job_id)
        except ApiError:
            return
        state = str(fresh.get("state") or "")
        if state in _TERMINAL_STATES:
            await _render_body(client, fresh, job_id, body_container)
            outer_timer = state_holder.get("timer")
            if outer_timer is not None:
                outer_timer.deactivate()

    state_holder["timer"] = ui.timer(5.0, _outer_tick)


async def _render_recent_activity(client: ApiClient) -> None:
    """Empty-state panel: the user's last ~10 jobs across every project.

    Each card has a small checkbox in the top-right corner. Ticking
    cards adds them to a session-scoped selection list — when ≥2 are
    ticked, a floating "Compare these N" bar appears at the bottom of
    the panel and routes to ``/compare?mode=tool&jobs=…``. Clicking
    on the card body still navigates to the job (the checkbox event
    is contained — see ``_render_recent_activity_row``).

    Two guards keep the selection valid without a red error banner:
    once :data:`MAX_SELECTION` boxes are ticked every *unticked* box is
    disabled (un-ticking one re-enables them), and ticking a job whose
    tool differs from the ones already picked reverts the tick and pops
    a friendly modal instead.
    """
    try:
        rows = await client.list_my_visible_jobs(limit=_RECENT_ACTIVITY_LIMIT)
    except ApiError as exc:
        ui.label(f"Couldn't load recent activity: {exc.detail}").style("color: #c0392b;")
        return

    if not rows:
        ui.label("No jobs yet. Pick a project on the left to start a new one.").style(
            "color: #888;"
        )
        return

    ui.label("Recent activity").style("font-size: 1.1rem; font-weight: 600;")
    ui.label(
        "The 10 most recent jobs you can see, newest first. "
        "Tick the boxes to compare two or more side-by-side."
    ).style("color: #888;")

    rows_by_id = {int(r["id"]): r for r in rows}
    selected_ids: set[int] = set()
    checkboxes: dict[int, Any] = {}
    bar_holder: list[ui.row | None] = [None]

    # Friendly modal shown when the user ticks a job whose tool differs
    # from the ones already ticked. Built once, reopened on each offence.
    cross_tool_dialog = _build_cross_tool_dialog()

    def _refresh_bar() -> None:
        if bar_holder[0] is not None:
            bar_holder[0].clear()
            with bar_holder[0]:
                _render_compare_bar(selected_ids)

    def _sync_checkbox_state() -> None:
        # At the ceiling the only valid move is to un-tick something, so
        # grey out every box that isn't already ticked. Dropping back
        # below the ceiling re-enables them.
        at_max = len(selected_ids) >= MAX_SELECTION
        for jid, checkbox in checkboxes.items():
            if at_max and jid not in selected_ids:
                checkbox.disable()
            else:
                checkbox.enable()

    def _on_toggle() -> None:
        _sync_checkbox_state()
        _refresh_bar()

    badges: dict[int, Any] = {}
    for row in rows:
        _render_recent_activity_row(
            row, rows_by_id, selected_ids, checkboxes, cross_tool_dialog, _on_toggle, badges
        )
    bar_holder[0] = ui.row().classes("w-full justify-end q-mt-md")
    _refresh_bar()
    _wire_activity_state_refresh(client, badges, rows)


def _wire_activity_state_refresh(
    client: ApiClient, badges: dict[int, Any], rows: list[dict[str, Any]]
) -> None:
    """Keep the recent-activity badges honest while jobs are in flight.

    Without this the panel is a photograph: submit a job, come back to
    the workspace with nothing selected, and the card says *Queued* until
    the page is reloaded by hand — the same complaint as the right-hand
    pane, one surface over.

    Only the badge text and colour are touched. The cards, and the
    compare tick-boxes on them, are left alone. The timer stops itself
    once no job on screen can change again, so an idle page settles to
    no polling at all rather than asking the server about ten finished
    jobs for ever.
    """
    live = {int(r["id"]) for r in rows if str(r.get("state") or "") not in _TERMINAL_STATES}
    if not live:
        return
    holder: dict[str, Any] = {"timer": None}

    async def _tick() -> None:
        try:
            fresh = await client.list_my_visible_jobs(limit=_RECENT_ACTIVITY_LIMIT)
        except ApiError:
            return  # transient; the next tick tries again
        for row in fresh:
            job_id = int(row["id"])
            badge = badges.get(job_id)
            if badge is None or job_id not in live:
                continue
            state = str(row.get("state") or "")
            badge.text = format_job_state(state)
            badge.props(_state_badge_color(state))
            if state in _TERMINAL_STATES:
                live.discard(job_id)
        if not live and holder["timer"] is not None:
            holder["timer"].deactivate()

    holder["timer"] = ui.timer(_ACTIVITY_REFRESH_INTERVAL_S, _tick)


def _build_cross_tool_dialog() -> Any:
    """Friendly modal explaining why mixed-tool selections can't compare.

    Comparing only makes sense within one tool — inputs and outputs
    follow per-tool schemas, so a cross-tool diff would have nothing to
    line up. Rather than a red banner at the bottom of the panel, we
    revert the offending tick and pop this dialog.
    """
    with ui.dialog() as dialog, ui.card().classes("max-w-md gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("info").style("color: #f39c12; font-size: 1.5rem;")
            ui.label("These jobs use different tools").style("font-size: 1.1rem; font-weight: 600;")
        ui.label(
            "Comparing works for jobs of a single tool — their inputs and "
            "results share the same shape. The job you just ticked uses a "
            "different tool, so it wasn't added. Un-tick the others first "
            "if you'd like to compare a different tool's jobs."
        ).style("color: #555;")
        with ui.row().classes("w-full justify-end"):
            ui.button("Got it", on_click=dialog.close).props("flat color=primary")
    return dialog


def _render_recent_activity_row(
    row: dict[str, Any],
    rows_by_id: dict[int, dict[str, Any]],
    selected_ids: set[int],
    checkboxes: dict[int, Any],
    cross_tool_dialog: Any,
    on_toggle: object,
    badges: dict[int, Any] | None = None,
) -> None:
    """One card in the recent-activity panel.

    ``badges`` collects the state badge per job id so the panel can keep
    them current without rebuilding the cards — rebuilding would throw
    away the compare tick-boxes the user had just set, which is the same
    mistake the live log view used to make with its expansions.

    Layout matches the legacy: title + subtitle + state badge. The
    Compare-multi-select adds a single Quasar checkbox tucked into the
    top-right of the row. The checkbox uses ``.props('@click.stop')``
    so clicking the box doesn't bubble up to the card-body click
    handler that navigates to the job.

    Ticking a job whose tool differs from the already-selected ones
    reverts the tick and opens ``cross_tool_dialog`` rather than adding
    an un-comparable job to the set.
    """
    job_id = int(row["id"])
    with (
        ui.card()
        .classes("w-full cursor-pointer")
        .on("click", lambda _e=None, _jid=job_id: _navigate(job=_jid)),
        ui.row().classes("items-center justify-between w-full no-wrap"),
    ):
        with ui.column().classes("gap-1"):
            ui.label(f"{format_job_id(job_id)} · {row['name']}").style("font-weight: 600;")
            ui.label(f"{row['tool_name']} · {row['project_name']} / {row['scenario_name']}").style(
                "color: #888; font-size: 0.85rem;"
            )
            ui.label(format_user_datetime(row.get("created_at"))).style(
                "color: #888; font-size: 0.8rem;"
            )
        with ui.row().classes("items-center gap-2"):
            badge = ui.badge(format_job_state(row["state"])).props(
                _state_badge_color(str(row.get("state") or ""))
            )
            if badges is not None:
                badges[job_id] = badge
            checkbox = ui.checkbox(value=job_id in selected_ids).props("@click.stop dense")
            checkboxes[job_id] = checkbox

            # Guards the value-change handler against re-entering when we
            # programmatically revert a cross-tool tick below.
            suppress = {"on": False}

            def _on_change(event: Any, _jid: int = job_id) -> None:
                if suppress["on"]:
                    return
                if bool(event.value):
                    prospective = [
                        str(rows_by_id[s].get("tool_alias") or "")
                        for s in (selected_ids | {_jid})
                        if s in rows_by_id
                    ]
                    if detect_cross_tool(prospective):
                        # Never silently add an un-comparable job: revert
                        # the tick and explain via the modal.
                        suppress["on"] = True
                        checkbox.value = False
                        suppress["on"] = False
                        cross_tool_dialog.open()
                        return
                    selected_ids.add(_jid)
                else:
                    selected_ids.discard(_jid)
                if callable(on_toggle):
                    on_toggle()

            checkbox.on_value_change(_on_change)
            ui.tooltip("Tick to add to a Compare selection").classes("text-xs")


def _render_compare_bar(selected_ids: set[int]) -> None:
    """Floating bar at the bottom of the recent-activity panel.

    Hidden until at least :data:`MIN_SELECTION` cards are ticked. The
    selection can't go cross-tool or past :data:`MAX_SELECTION` — both
    are blocked at tick time (a friendly modal for the former, disabled
    checkboxes for the latter) — so the bar only ever shows the
    go-ahead button.
    """
    if len(selected_ids) < MIN_SELECTION:
        return

    ids_csv = ",".join(str(i) for i in sorted(selected_ids))
    ui.button(
        f"Compare these {len(selected_ids)} →",
        on_click=lambda _e=None: ui.navigate.to(f"/compare?mode=tool&jobs={ids_csv}"),
    ).props("color=primary")


async def _render_tool_picker(client: ApiClient, selection: _Selection) -> None:
    """Cards-of-tools picker; selecting one moves to the form."""
    try:
        tools = await client.list_tools()
    except ApiError as exc:
        ui.label(f"Couldn't load tools: {exc.detail}").style("color: #c0392b;")
        return
    if not tools:
        ui.label("No tools are installed. Ask an administrator.").style("color: #888;")
        return
    ui.label("Pick a tool to start a new job").style("font-size: 1.1rem; font-weight: 600;")
    for tool in tools:
        alias = str(tool["alias"])
        with (
            ui.card()
            .classes("w-full cursor-pointer")
            .on(
                "click",
                lambda _e=None, _a=alias: _navigate(
                    proj=selection.project_id, scen=selection.scenario_id, tool=_a
                ),
            )
        ):
            ui.label(str(tool["name"])).style("font-weight: 600;")
            if tool.get("short_description"):
                ui.label(str(tool["short_description"])).style("color: #888; font-size: 0.9rem;")


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


async def _render_vv_header(client: ApiClient, tool_version_id: int) -> None:
    """Render the V&V status badge for this tool version above the form.

    Click → navigate to the V&V workspace. Failures and missing badges
    degrade gracefully — V&V is a supporting surface here, never on the
    critical path of submitting a job.
    """
    try:
        badge = await client.vv_tool_status(tool_version_id)
    except ApiError:
        return
    kind = str(badge.get("kind") or "none")
    label = _VV_BADGE_LABEL.get(kind, kind.title())
    color = _VV_BADGE_COLOR.get(kind, "grey")
    inherited = bool(badge.get("inherited"))
    with ui.row().classes("items-center gap-2 q-my-xs"):
        chip = ui.chip(label, icon="verified").props(f"color={color} text-color=white dense")
        chip.on(
            "click",
            lambda _e=None: ui.navigate.to("/vv-records"),
        )
        chip.style("cursor: pointer;")
        if inherited:
            contributing = list(badge.get("contributing_version_ids") or [])
            tip = (
                "Inherited from cohort versions: " + ", ".join(f"#{v}" for v in contributing[:5])
                if contributing
                else "Inherited from cohort"
            )
            ui.label("(inherited)").style("color: #888; font-size: 0.78rem;").tooltip(tip)


def _render_envelope_banner_slot() -> Any:
    """Reserve a row for the envelope-warn banner; populated reactively."""
    return ui.row().classes("w-full q-my-xs")


def _render_envelope_warning(slot: Any, warning: dict[str, Any] | None) -> None:
    """Re-render the banner row from the latest envelope-check result."""
    slot.clear()
    if not warning:
        return
    out_of_range = list(warning.get("out_of_range") or [])
    if not out_of_range:
        return
    with (
        slot,
        ui.card()
        .classes("w-full")
        .style("background: rgba(255, 193, 7, 0.12); border-left: 4px solid #ffb300;"),
    ):
        with ui.row().classes("items-center gap-2"):
            ui.icon("warning").style("color: #b78103;")
            ui.label("Outside the approved V&V envelope").style("font-weight: 600; color: #b78103;")
        ui.label(
            "These inputs fall outside the cohort's approved validation range. "
            "Submission is not blocked — but record the rationale before queueing."
        ).style("color: #6c5300; font-size: 0.85rem;")
        with ui.column().classes("gap-0"):
            for entry in out_of_range[:6]:
                factor = str(entry.get("factor") or "")
                value = entry.get("value")
                lo = entry.get("envelope_min")
                hi = entry.get("envelope_max")
                ui.label(f"• {factor}: {value} (envelope {lo} to {hi})").style(
                    "color: #6c5300; font-size: 0.82rem;"
                )


async def _render_submit_form(client: ApiClient, selection: _Selection) -> None:
    """Render the input form against the existing draft job.

    Invariant by the time we get here: ``selection.job`` is the
    draft (state ``"created"``) the user is filling in, with a real
    DB id surfaced in the workspace's Job-ID picker. Form fields seed
    from the draft's existing ``inputs`` (so navigating away and back
    doesn't lose work). On Submit, the draft is promoted to ``queued``
    via ``POST /api/jobs/{id}/queue`` rather than creating a fresh
    row.
    """
    from matofx.tools.descriptor import ToolDescriptor  # noqa: PLC0415
    from matofx.tools.forms import (  # noqa: PLC0415
        FileInputContext,
        FormValidationError,
        GuideImageView,
        MaterialInputContext,
        render_form,
        validate_inputs,
    )

    if selection.scenario_id is None or selection.tool_alias is None:
        return  # caller already gated on this; defensive type narrow.
    if selection.job_id is None or selection.job is None:
        # The draft branch in ``_render_right_pane`` always has a job
        # by this point. Defensive return so a future caller change
        # doesn't crash the render.
        return

    try:
        tool_detail = await client.get_tool(selection.tool_alias)
    except ApiError as exc:
        ui.label(f"Couldn't load the tool: {exc.detail}").style("color: #c0392b;")
        return

    descriptor_dict = dict(tool_detail.get("descriptor") or {})
    try:
        descriptor = ToolDescriptor.model_validate(descriptor_dict)
    except ValueError as exc:
        ui.label(f"This tool's descriptor is invalid: {exc}").style("color: #c0392b;")
        return

    draft_inputs = dict(selection.job.get("inputs") or {})
    draft_name = str(selection.job.get("name") or f"{descriptor.name} run")

    ui.label(f"New {descriptor.name} v{descriptor.version} job").style(
        "font-size: 1.25rem; font-weight: 600;"
    )
    blurb = descriptor.long_description or descriptor.short_description
    if blurb:
        ui.label(blurb).style("color: #888; white-space: pre-line;")

    # V&V cohort key for the badge + envelope check (ADR-0011 §6.5 / §8).
    # ``tool_version_id`` is returned by GET /api/tools/{alias} when the
    # feature is on; ``None`` when the install is missing the snapshot
    # row (test fixtures only) — we just skip the V&V surface in that
    # case rather than wiring a broken click.
    tool_version_id_raw = tool_detail.get("tool_version_id")
    tool_version_id = int(tool_version_id_raw) if tool_version_id_raw is not None else None
    envelope_banner: Any = None
    if vv_tracking_enabled() and tool_version_id is not None:
        await _render_vv_header(client, tool_version_id)
        envelope_banner = _render_envelope_banner_slot()

    job_name = ui.input("Job name *", value=draft_name).classes("w-full")

    # Wire the rich file-input widget to the draft-upload endpoints.
    # The callbacks close over ``client`` and the current draft's id so
    # the form widget can stage uploads and discover previously-used
    # files for each ``file``-typed input without the forms package
    # importing the API client itself.
    draft_job_id = int(selection.job_id)
    tool_alias_for_picker = descriptor.alias

    async def _upload_file_input(field_name: str, filename: str, content: bytes) -> str:
        result = await client.upload_draft_file_input(
            draft_job_id,
            field_name=field_name,
            filename=filename,
            content=content,
        )
        return str((result.get("inputs") or {}).get(field_name) or "")

    async def _clear_file_input(field_name: str) -> None:
        await client.clear_draft_file_input(draft_job_id, field_name=field_name)

    async def _list_previous_file_inputs(field_name: str) -> list[dict[str, Any]]:
        entries = await client.list_file_input_history(tool_alias_for_picker, field=field_name)
        # Filter to entries with a non-empty value — defensive against
        # legacy rows where ``Job.inputs[field]`` was an empty string.
        return [e for e in entries if e.get("value")]

    async def _fetch_file_preview(path: str) -> bytes:
        return await client.fetch_draft_file_preview(draft_job_id, path=path)

    async def _fetch_cad_preview(path: str) -> bytes:
        return await client.fetch_draft_cad_preview(draft_job_id, path=path)

    async def _list_library_candidates(field_name: str) -> list[dict[str, Any]]:
        # The 4th "Pick from library" source mode (ADR-0006 §6) — the
        # published library versions assigned to this tool's field.
        return await client.library_picker(tool=tool_alias_for_picker, field=field_name)

    async def _list_other_output_candidates(
        accepted_file_types: list[str],
        expected_field_array: str | None,
    ) -> list[dict[str, Any]]:
        # Cross-tool "use a file from another job" — any visible completed
        # job's result file (e.g. a standalone Bracket Stress run reusing
        # Thermal Soak's temperature field), filtered to this input's types
        # and (for a field input) the nodal array it needs.
        return await client.list_file_output_candidates(
            accepted_file_types=accepted_file_types,
            expected_field_array=expected_field_array,
        )

    async def _list_material_candidates(
        material_class: str, required_props: list[str] | None
    ) -> list[dict[str, Any]]:
        # The material-input picker (ADR-0007 §6) — published materials of
        # the field's class, optionally filtered by required properties.
        return await client.materials_picker(
            material_class=material_class, required_props=required_props
        )

    file_ctx = FileInputContext(
        upload=_upload_file_input,
        clear=_clear_file_input,
        list_previous=_list_previous_file_inputs,
        fetch_preview=_fetch_file_preview,
        fetch_cad_preview=_fetch_cad_preview,
        # Feature-gated: the mode is hidden (callback ``None``) when the
        # file_library feature is off, matching the absent /api/library.
        list_library=_list_library_candidates if file_library_enabled() else None,
        list_other_outputs=_list_other_output_candidates,
    )
    # Feature-gated material-input picker (``None`` → plain fallback when
    # materials_database is off, matching the absent /api/materials).
    material_ctx = (
        MaterialInputContext(list_materials=_list_material_candidates)
        if materials_database_enabled()
        else None
    )
    # Static guide images (visual explainers for the parameters) — fetched
    # before any form widget exists so a navigate-away mid-fetch tears down
    # nothing. Best-effort: a failed image just doesn't show.
    guide_views: list[GuideImageView] = []
    for _i, _g in enumerate(getattr(descriptor, "input_guides", []) or []):
        try:
            _res = await client.get_tool_guide_image_bytes(descriptor.alias, _i)
        except ApiError:
            continue
        if not _res:
            continue
        _raw, _mime = _res
        _b64 = base64.b64encode(_raw).decode("ascii")
        guide_views.append(
            GuideImageView(caption=_g.caption, data_url=f"data:{_mime};base64,{_b64}")
        )
    handle = render_form(
        descriptor,
        file_input_context=file_ctx,
        material_input_context=material_ctx,
        guide_images=guide_views,
    )
    # Re-seed the form with whatever the user typed before navigating
    # away (descriptor defaults already populated by ``render_form``;
    # the draft's stored inputs override them where present).
    if draft_inputs:
        handle.seed(draft_inputs)

    # Surrogate-tool coverage gauge (ADR-0008 §7.3): when this is a
    # synthesised surrogate tool, show where the user's inputs sit
    # relative to the model's training range. Advisory + best-effort. The
    # factor-range fetch is deferred into a placeholder via a one-shot
    # timer (the page's load-into-placeholder convention) so its network
    # round-trip never sits as a suspension point in the main render path
    # — otherwise a navigate-away mid-fetch would leave the rest of the
    # form (its pollers, the submit button) building on a torn-down slot.
    surrogate_version_id = getattr(descriptor, "surrogate_model_version_id", None)
    if surrogate_version_id is not None:
        from matofx.ui._coverage_gauge import render_coverage_gauge  # noqa: PLC0415
        from matofx.ui._features import surrogate_family_enabled  # noqa: PLC0415

        if surrogate_family_enabled():
            gauge_version_id = int(surrogate_version_id)
            gauge_slot = ui.column().classes("w-full")

            async def _load_gauge(_vid: int = gauge_version_id) -> None:
                try:
                    coverage = await client.model_version_coverage(_vid)
                except ApiError:
                    return
                with gauge_slot:
                    render_coverage_gauge(
                        list(coverage.get("factors") or []),
                        values=handle.state,
                        widgets=handle.widgets,
                        # The labels this very form renders its fields with.
                        labels={str(f.name): str(f.label or f.name) for f in descriptor.inputs},
                    )

            ui.timer(0.0, _load_gauge, once=True)

    # Preset / previous-job / defaults popovers — locked behaviour
    # from Q3 of the rewrite plan. Each one calls ``handle.seed`` with
    # the chosen source's values; widgets update reactively.
    await _render_seed_popovers(client, descriptor, handle)

    # Optional justification — only when this tool's policy will hold
    # the job for approval (§16.15). Shown to the approver in their
    # review panel; pointless clutter for non-approval tools.
    needs_approval = descriptor.approvals_policy in ("pre", "both")
    justification_box = None
    if needs_approval:
        with ui.column().classes("w-full gap-1"):
            ui.label("Justification (optional) — sent to the approver").style(
                "font-size: 0.8rem; color: #888;"
            )
            justification_box = (
                ui.textarea(placeholder="Why this run? Context that helps the approver decide.")
                .props("dense outlined autogrow")
                .classes("w-full")
            )

    error_banner = ui.label("").style("color: #c0392b;")

    async def _do_submit(*, priority: bool) -> None:
        """Validate, then promote the draft to ``queued`` via the API."""
        if not (job_name.value or "").strip():
            error_banner.text = "Please enter a name for this job."
            return
        try:
            values = validate_inputs(descriptor, handle.values())
        except FormValidationError as exc:
            handle.show_errors(exc)
            error_banner.text = "Please correct the highlighted fields and try again."
            return
        handle.clear_errors()
        error_banner.text = ""
        if selection.job_id is None:
            return  # impossible — gated above.
        try:
            queued = await client.queue_draft_job(
                selection.job_id,
                inputs=values,
                name=str(job_name.value),
                priority=priority,
                justification=(
                    str(justification_box.value or "").strip()
                    if justification_box is not None
                    else None
                ),
            )
        except ApiError as exc:
            error_banner.text = exc.detail or "Submitting the job failed."
            return
        ui.notify(
            "Job submitted (skip-queue priority)." if priority else "Job submitted.",
            type="positive",
        )
        _navigate(
            proj=selection.project_id,
            scen=selection.scenario_id,
            tool=descriptor.alias,
            job=int(queued["id"]),
        )

    async def _on_submit() -> None:
        await _do_submit(priority=False)

    async def _on_skip_queue() -> None:
        # Same validation + submit flow, just sets the priority flag so
        # the runner picks the row up ahead of FIFO peers. Server-side
        # the API rejects the flag for unprivileged users — the UI hides
        # the button so they never see it.
        await _do_submit(priority=True)

    async def _on_save_inputs() -> None:
        """Persist the form to the draft without queueing.

        Saves the typed values + current job name onto the row so
        re-opening the draft later repopulates everything. Skips
        validation — a half-finished form is a legitimate save (the
        user might be partway through entering values and want to
        pick up tomorrow). The API still refuses a save against a
        non-draft job, but that's a server-side check; the UI button
        is only shown while the form is rendered (draft state).
        """
        if selection.job_id is None:
            return
        try:
            await client.save_draft_inputs(
                selection.job_id,
                inputs=dict(handle.values()),
                name=str(job_name.value or "") or None,
            )
        except ApiError as exc:
            error_banner.text = exc.detail or "Couldn't save the inputs."
            return
        error_banner.text = ""
        ui.notify("Inputs saved. Re-open the draft to pick up where you left off.", type="positive")

    skip_queue_button: Any = None
    with ui.row().classes("w-full justify-end gap-2"):
        ui.button(
            "Cancel",
            on_click=lambda: _navigate(proj=selection.project_id, scen=selection.scenario_id),
        ).props("flat")
        ui.button(
            "Save Inputs",
            icon="save",
            on_click=_on_save_inputs,
        ).props("flat color=primary")
        # Skip-queue button only renders for admins / super-users so a
        # regular user never sees it. The API enforces the permission
        # again on the server (the UI guard is a courtesy, not the
        # gate).
        if is_admin():
            skip_queue_button = ui.button(
                "Skip Queue",
                icon="bolt",
                on_click=_on_skip_queue,
            ).props("color=warning")
            skip_queue_button.tooltip(
                "Submits this job and pushes it ahead of the regular queue. "
                "Resource and licence checks still apply."
            )
        submit_button = ui.button("Submit", on_click=_on_submit).props("color=primary")

    # A muted, right-aligned line under the buttons spelling out *every*
    # reason Submit is currently disabled — missing job name, empty
    # required fields, sections not yet reviewed. Per the 2026-05-16
    # requirement the hint enumerates all outstanding gates at once
    # (not just the first), so the user never has to satisfy one only
    # to discover another. Hidden the instant nothing is outstanding.
    gate_hint = (
        ui.label("").classes("w-full text-right").style("color: #9a7d0a; font-size: 0.85rem;")
    )
    gate_hint.visible = False

    # Submit and Skip Queue stay disabled until the shared gate
    # (:meth:`FormHandle.submit_blockers`) reports no outstanding
    # reasons — job name, required fields, out-of-range / invalid
    # values, and (when the tool opts in) every section reviewed. The
    # very same gate runs on the workflow-run surface, so a tool's form
    # gates identically wherever it is run and any future gate is
    # inherited by both. We re-evaluate on a short timer rather than
    # wiring per-widget on-change events because NiceGUI's reactive
    # bindings don't fan out to a derived predicate cleanly across the
    # mixed widget types we render (number / select / multiselect / file
    # / checkbox / string); the 0.3 s tick is well under user-perceptible
    # latency and cheaper than wiring six per-type change handlers.
    def _set_gated_buttons(*, enabled: bool) -> None:
        """Enable or disable Submit + Skip Queue together.

        Both buttons fire the same validation + queue-draft flow; the
        only difference is the priority flag. Keeping them in lockstep
        means a user with the skip-queue permission can't fast-track a
        half-filled form just because the button happened to be live.
        """
        if enabled:
            submit_button.enable()
            if skip_queue_button is not None:
                skip_queue_button.enable()
        else:
            submit_button.disable()
            if skip_queue_button is not None:
                skip_queue_button.disable()

    # Track the last hint text so the 0.3 s tick only touches the DOM
    # when the reason set actually changes — avoids needless re-renders.
    last_hint: dict[str, str] = {"text": ""}

    def _enable_submit_if_complete() -> None:
        reasons = handle.submit_blockers(require_job_name=True, job_name=job_name.value)
        _set_gated_buttons(enabled=not reasons)
        text = ("Before submitting — " + "  •  ".join(reasons)) if reasons else ""
        if text != last_hint["text"]:
            last_hint["text"] = text
            gate_hint.text = text
            gate_hint.visible = bool(text)

    _enable_submit_if_complete()
    ui.timer(0.3, _enable_submit_if_complete)

    # V&V envelope-warn polling (ADR-0011 §6.5 — soft warn, never blocks).
    # Re-runs every 1.5 s while the form is open; on a tick the current
    # numeric form values are snapshotted and shipped to
    # /api/vv/envelope-check, which returns ``None`` when no approved
    # validation exists, no envelope is declared, or every value is
    # inside the cohort union. We bail when the feature is off or the
    # tool version row didn't resolve. The check is intentionally
    # advisory; the blocking gate is the server-side
    # ``vv_required_for_submit`` policy enforced in
    # ``JobService.queue_for_runner``.
    if envelope_banner is not None and tool_version_id is not None:
        last_signature: dict[str, str] = {"signature": ""}

        async def _check_envelope() -> None:
            numeric_values: dict[str, float] = {}
            for field in descriptor.inputs:
                if field.type not in ("integer", "float"):
                    continue
                value = handle.state.get(field.name)
                if value is None or value == "":
                    continue
                try:
                    numeric_values[field.name] = float(value)
                except (TypeError, ValueError):
                    continue
            signature = ",".join(f"{k}={v}" for k, v in sorted(numeric_values.items()))
            if signature == last_signature["signature"]:
                return
            last_signature["signature"] = signature
            try:
                warning = await client.vv_envelope_check(
                    tool_version_id=tool_version_id, form_values=numeric_values
                )
            except ApiError:
                _render_envelope_warning(envelope_banner, None)
                return
            _render_envelope_warning(envelope_banner, warning)

        ui.timer(1.5, _check_envelope)


async def _render_seed_popovers(
    client: ApiClient,
    descriptor: Any,
    handle: Any,
) -> None:
    """Up to three buttons above the form: preset / previous-job / defaults.

    Pre-fetches the preset and previous-job lists so we can hide each
    button entirely when its source is empty — a "Use a preset" button
    that only ever says "no presets" is just noise to a non-modeller
    user. "Reset to defaults" is always shown.
    """
    from matofx.tools.forms import from_descriptor_defaults  # noqa: PLC0415

    try:
        presets = await client.list_tool_presets(descriptor.alias)
    except ApiError:
        presets = []

    try:
        prior_jobs = await client.list_my_visible_jobs(
            tool_alias=descriptor.alias, state="complete", limit=15
        )
    except ApiError:
        prior_jobs = []

    with ui.row().classes("w-full gap-2 q-my-sm"):
        if presets:
            preset_button = ui.button("Use a preset", icon="bookmark")
            preset_button.props("flat color=primary")
            with preset_button, ui.menu(), ui.column().classes("min-w-[260px] gap-1 q-pa-sm"):
                for preset in presets:
                    ui.button(
                        preset.get("name") or "preset",
                        on_click=lambda _e=None, _p=preset: handle.seed(_p.get("values") or {}),
                    ).props("flat dense").classes("justify-start text-left")

        if prior_jobs:
            previous_button = ui.button("Use a previous job's inputs", icon="history")
            previous_button.props("flat color=primary")
            with previous_button, ui.menu(), ui.column().classes("min-w-[280px] gap-1 q-pa-sm"):
                for row in prior_jobs:
                    ui.button(
                        f"{format_job_id(row['id'])} · {row['name']}",
                        on_click=lambda _e=None, _jid=int(row["id"]): _seed_from_previous_job(
                            client, _jid, handle
                        ),
                    ).props("flat dense").classes("justify-start text-left")

        ui.button(
            "Reset to defaults",
            icon="restart_alt",
            on_click=lambda: handle.seed(
                from_descriptor_defaults(descriptor), fill_missing_with_defaults=True
            ),
        ).props("flat color=primary")


async def _seed_from_previous_job(client: ApiClient, job_id: int, handle: Any) -> None:
    """Fetch a previous job and seed the form from its ``inputs``."""
    from matofx.tools.forms import from_previous_job_inputs  # noqa: PLC0415

    try:
        job = await client.get_job(job_id)
    except ApiError as exc:
        ui.notify(f"Couldn't load that job: {exc.detail}", type="negative")
        return
    # ``JobResponse.inputs`` is a flat dict of field-name → value; the
    # seeder accepts that shape directly via the dict-form branch.
    handle.seed(
        from_previous_job_inputs(
            {"inputs": [{"name": k, "value": v} for k, v in (job.get("inputs") or {}).items()]}
        )
    )
    ui.notify("Form pre-filled from the previous job.", type="positive")


# --- Misc helpers --------------------------------------------------------


def _state_badge_color(state: str) -> str:
    """Map a job state to a Quasar badge colour prop."""
    return {
        "created": "color=grey",
        "queued": "color=info",
        "running": "color=warning",
        "complete": "color=positive",
        "failed": "color=negative",
        "cancelled": "color=grey",
    }.get(state, "color=grey")
