"""Workflow run rendering — invoked from My Jobs and via deep link.

The interactive surface for one in-flight workflow run. One tab per
member tool (labelled by the tool's pretty name); per-tab content is
gated by the child job's state + upstream completion:

- ``created`` + every upstream complete → form, with ``downstream_input``
  fields pre-filled from upstream ``outputs.json`` and disabled. The
  free inputs stay editable; the user clicks **Submit**.
- ``created`` but upstream not all complete → "waiting for X" banner.
- ``queued`` / ``running`` → status line; auto-refresh.
- ``complete`` → outputs panel.
- ``failed`` → error banner + link to the standard job page for
  re-submission / logs.

Two entry points:

- :func:`render_workflow_run_workspace` — embedded in the My Jobs
  workspace right pane (the new canonical surface). Returns once the
  initial body is wired up; auto-refresh continues in the background.
- ``GET /ui/workflow-runs/{run_id}`` — deep-link alias that redirects
  to ``/my-jobs?workflow_run={run_id}`` so the workspace handles every
  workflow-run path.

Job submission goes through the existing per-job ``queue`` endpoint —
the workflow rendering just decides *when* the form is submittable
and what to lock + pre-fill.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_job_state
from matofx.ui.pages._chrome import require_logged_in

_REFRESH_INTERVAL_S = 2.5
"""How often the workspace polls the run while any child is
``queued`` or ``running``. Stops polling effects once every child
reaches a terminal state — the page reads as static at rest."""

_ACTIVE_CHILD_STATES = frozenset({"queued", "running"})
"""Child states that can still transition *on their own* — the runner
drives ``queued → running → complete`` server-side, and polling is the
only way the UI learns of it. While any child is in one of these, the
poll keeps running; otherwise it can rest."""

_TERMINAL_RUN_STATES = frozenset({"complete", "failed", "partial", "cancelled"})
"""Rolled-up run states for which the summary endpoint produces a card.
Mirrors ``matofx.summaries.facts.TERMINAL_STATES`` — the summary 404s for
anything else, so we only attempt the fetch once the run lands here."""


def _poll_can_rest(run: dict[str, Any]) -> bool:
    """True when no child is in flight, so the poll can stop until the user acts.

    The poll exists solely to catch a child's runner-driven
    ``queued → running → complete``/``failed`` transition. When no child
    is ``queued``/``running`` — every child is terminal
    (complete/failed/cancelled) *or* a ``created`` draft awaiting the
    user — nothing changes on its own, so we deactivate the timer rather
    than hammer the API for the life of the tab.

    This is deliberately *not* keyed on the run-level state:
    ``compute_state`` only reports ``"complete"`` when **every** child is
    complete, so a multi-step run where the user has finished one tool
    but not yet submitted the downstream drafts would otherwise poll
    forever. Any user action that puts a child back in flight (submitting
    a draft) navigates + re-renders, which re-establishes the poll.
    """
    children = run.get("children") or []
    return not any(str(c.get("state") or "") in _ACTIVE_CHILD_STATES for c in children)


async def _refresh_summary_card(state: dict[str, Any]) -> None:
    """Draw the run-level summary once the run is terminal — idempotent.

    The summary endpoint 404s while the run is in flight, so the card starts
    blank. This fills it in the moment the run reaches a terminal state and
    then latches (``summary_done``) so we don't re-fetch every poll. A no-op
    while the run is still running, already filled, or if the fetch comes back
    empty (we retry on the next terminal tick).
    """
    if state.get("summary_done"):
        return
    run_state = str(state["run"].get("state") or "")
    if run_state not in _TERMINAL_RUN_STATES:
        return

    from matofx.ui._summary_card import render_summary_card  # noqa: PLC0415

    summary_box: ui.column = state["summary_box"]
    summary_box.clear()
    with summary_box:
        rendered = await render_summary_card(
            state["client"], "workflow_run", int(state["run_id"])
        )
    state["summary_done"] = rendered


@ui.page("/workflow-runs/{run_id}")
async def workflow_run_page(run_id: int) -> None:
    """Deep-link alias — redirects into the My Jobs workspace.

    Workflow runs live inside the unified workspace at
    ``/my-jobs?workflow_run={id}`` per the pattern locked by
    ``feedback_my_jobs_unified_workspace.md``. This route stays as a
    stable bookmark / sharing target and forwards into the workspace
    on every load.
    """
    if not require_logged_in():
        return
    ui.navigate.to(f"/my-jobs?workflow_run={int(run_id)}")


async def render_workflow_run_workspace(
    client: ApiClient,
    run_id: int,
    body: ui.column,
    *,
    active_job_id: int | None = None,
) -> None:
    """Render the tabbed workflow-run view inside the supplied container.

    ``body`` is cleared and rebuilt as the run state evolves. Auto-
    refresh polling lives on this function — it re-renders the tab
    strip and the active body independently, only when each one's
    signature changes (see :func:`_tabs_signature` /
    :func:`_body_signature`), and stops polling entirely once nothing is
    in flight (see :func:`_poll_can_rest`).

    ``active_job_id`` lets the caller specify which child tab opens
    first (useful when the URL was ``?workflow_run=N&job=M``); when
    ``None`` the function picks the first non-terminal child as a
    sensible default.
    """
    try:
        run = await client.get_workflow_run(run_id)
    except ApiError as exc:
        body.clear()
        with body:
            ui.label(f"We couldn't load this workflow run: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
        return

    descriptors_by_alias: dict[str, dict[str, Any]] = {}
    member_aliases = [str(c.get("tool_alias") or "") for c in run.get("children") or []]
    for alias in member_aliases:
        if not alias or alias in descriptors_by_alias:
            continue
        try:
            tool = await client.get_tool(alias)
        except ApiError:
            continue
        descriptors_by_alias[alias] = tool.get("descriptor") or {}

    body.clear()
    with body:
        header_container = ui.column().classes("w-full gap-2")
        # Auto-generated results summary (designs/auto-summaries.md).
        # The summary 404s (renders nothing) while the run is still in flight
        # and only becomes available once the run reaches a terminal state.
        # It lives in its own re-renderable container so the poll can fill it
        # in the moment the run finishes — otherwise it would stay blank until
        # a manual page refresh (the workflow-outputs-don't-render bug).
        summary_box = ui.column().classes("w-full")
        # The tab strip and the active-tab body live in *separate*
        # re-renderable containers so a poll can refresh one without
        # tearing down the other. Rebuilding the active body on every
        # tick (e.g. when a downstream member transitions) would wipe an
        # in-progress form and re-fetch the embedded job needlessly, so
        # the body is gated on its own signature — see
        # ``_body_signature`` / ``_rerender_all``.
        shell = ui.column().classes("w-full gap-3")
        with shell:
            tabs_box = ui.column().classes("w-full gap-2")
            body_box = ui.column().classes("w-full")
        # Lineage / provenance section (ADR-0010 slice 5). Sits below
        # the tab strip so it stays mounted across child-tab switches;
        # lazy-loaded so the API call only fires on open.
        from matofx.ui.lineage_view import render_provenance_section  # noqa: PLC0415

        render_provenance_section(kind="workflow_run", obj_id=run_id)

    state: dict[str, Any] = {
        "run": run,
        "descriptors": descriptors_by_alias,
        "active_position": None,
        "tabs_box": tabs_box,
        "body_box": body_box,
        "header": header_container,
        "summary_box": summary_box,
        # Set once the run-level summary card has actually been drawn, so the
        # poll stops re-fetching the summary endpoint for the life of the tab.
        "summary_done": False,
        "client": client,
        "run_id": run_id,
        # ``"tool"`` (default) → render the per-tool tab body for
        # ``active_position``; ``"graph"`` → render the workflow-
        # structure graph view (ADR-0013). The tab strip always shows
        # both kinds of tabs so the user can switch back and forth
        # without losing the run context.
        "view_kind": "tool",
        # Signatures of the last header+tab-strip render and the last
        # active-body render. The poll path re-renders each container
        # independently, only when its own signature changes — so a
        # downstream member transitioning refreshes the strip's badges
        # without wiping the body (and the in-progress form / embedded
        # job inside it).
        "last_tabs_sig": None,
        "last_body_sig": None,
    }

    children = run.get("children") or []
    # When the caller specified an active child, honour that. Otherwise
    # pick the first non-terminal child — drops the user straight into
    # what they probably want to do next.
    chosen = None
    if active_job_id is not None:
        chosen = next(
            (c for c in children if int(c.get("id") or 0) == int(active_job_id)),
            None,
        )
    if chosen is None:
        chosen = next(
            (c for c in children if c.get("state") not in ("complete", "failed")),
            children[0] if children else None,
        )
    if chosen is not None:
        state["active_position"] = int(chosen.get("workflow_position") or 1)

    _rerender_all(state, force=True)
    # Fill the summary card now in case the run is already terminal on load
    # (e.g. a deep link to a finished run); otherwise the poll fills it the
    # moment the run reaches a terminal state.
    await _refresh_summary_card(state)

    async def _poll() -> None:
        try:
            fresh = await client.get_workflow_run(run_id)
        except ApiError:
            return
        state["run"] = fresh
        _rerender_all(state)
        # Draw the run-level summary as soon as the run finishes. This must
        # happen on the same tick that observes the terminal state, *before*
        # the can-rest check below deactivates the timer — otherwise the
        # summary would stay blank until a manual refresh.
        await _refresh_summary_card(state)
        # When nothing is in flight, stop polling — otherwise the page
        # keeps hitting the API every few seconds for the life of the
        # tab (the bug: a run with one finished tool and the rest still
        # ``created`` drafts never reaches a terminal *run* state). The
        # re-render above already painted the latest state, including
        # flipping any downstream tab to "ready", before we go quiet.
        # Mirrors the self-deactivating pollers in job.py (_tick) and
        # my_jobs.py (_outer_tick).
        if _poll_can_rest(fresh):
            timer.deactivate()

    timer = ui.timer(_REFRESH_INTERVAL_S, _poll)


# ---------- header ------------------------------------------------------


def _render_header(client: ApiClient, run: dict[str, Any]) -> None:
    """Top bar: title, breadcrumb, run state, cancel button."""
    name = str(run.get("name") or "")
    workflow_name = str(run.get("workflow_name") or "")
    workflow_version = str(run.get("workflow_version_snapshot") or "")
    project_id = int(run.get("project_id") or 0)
    scenario_id = int(run.get("scenario_id") or 0)
    state = str(run.get("state") or "draft")

    with ui.row().classes("w-full items-center justify-between gap-2"):
        with ui.column().classes("gap-0"):
            ui.label(name).style("font-size: 1.5rem; font-weight: 600;")
            ui.label(
                f"{workflow_name} v{workflow_version} · "
                f"Project #{project_id} · Scenario #{scenario_id}"
            ).style("color: #888; font-size: 0.9rem;")
        _run_state_chip(state)
        if state in ("draft", "running"):
            ui.button(
                "Cancel run",
                icon="cancel",
                on_click=lambda: _confirm_cancel(client, int(run["id"])),
            ).props("flat dense color=negative")

    if run.get("cancel_requested_at"):
        ui.label(
            "This run was cancelled. Pending tabs are locked; results from "
            "completed tabs are still readable."
        ).style("color: var(--matofx-error-text); font-size: 0.9rem;")


_RUN_STATE_LABEL: dict[str, str] = {
    "draft": "Draft",
    "running": "Running",
    "complete": "Complete",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
_RUN_STATE_COLOR: dict[str, str] = {
    "draft": "grey",
    "running": "warning",
    "complete": "positive",
    "failed": "negative",
    "cancelled": "grey",
}


def _run_state_chip(state: str) -> None:
    label = _RUN_STATE_LABEL.get(state, state.title())
    color = _RUN_STATE_COLOR.get(state, "grey")
    ui.chip(label).props(f"color={color} text-color=white")


def _confirm_cancel(client: ApiClient, run_id: int) -> None:
    """Pop a confirmation modal before firing the cancel call."""
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label("Cancel this workflow run?").style("font-size: 1.1rem; font-weight: 600;")
        ui.label(
            "Pending tabs will be locked. Tabs already running will be "
            "asked to stop; tabs already complete keep their results."
        ).style("color: #888;")

        async def _confirm() -> None:
            try:
                await client.cancel_workflow_run(run_id)
            except ApiError as exc:
                ui.notify(f"Couldn't cancel: {exc.detail}", type="negative")
                return
            dialog.close()
            ui.notify("Workflow run cancelled.", type="info")
            ui.navigate.to(f"/my-jobs?workflow_run={int(run_id)}")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Back", on_click=dialog.close).props("flat")
            ui.button("Cancel run", on_click=_confirm).props("color=negative")
    dialog.open()


# ---------- tabs strip --------------------------------------------------


def _render_tabs_strip(state: dict[str, Any]) -> None:
    """Render the tab buttons across the top, in workflow_position order.

    A final "Workflow" tab is appended after the per-tool tabs — clicking
    it swaps the body for the workflow-structure graph view (ADR-0013).
    The graph tab doesn't carry a child-job state, so its icon /
    colour are kind-neutral.
    """
    run = state["run"]
    children = run.get("children") or []
    completed_aliases = {
        str(c.get("tool_alias")): c for c in children if c.get("state") == "complete"
    }
    state["completed_aliases"] = completed_aliases
    view_kind = state.get("view_kind", "tool")

    with ui.row().classes("w-full gap-1 flex-wrap"):
        for child in children:
            alias = str(child.get("tool_alias") or "")
            position = int(child.get("workflow_position") or 0)
            tool_name = str(child.get("tool_name") or alias)
            child_state = str(child.get("state") or "created")
            is_active = (
                view_kind == "tool" and state.get("active_position") == position
            )

            ready = _all_upstream_ready(state, child)
            tab_status = _tab_status_for(child_state, ready)

            icon = _TAB_STATE_ICON.get(tab_status, "build")
            color = _TAB_STATE_COLOR.get(tab_status, "primary")

            label = f"{position}. {tool_name}"
            btn = ui.button(label, icon=icon).props(
                f"{'unelevated' if is_active else 'outline'} dense color={color}"
            )
            if not is_active:
                btn.on(
                    "click",
                    lambda _e=None, _p=position: _on_tab_click(state, _p),
                )

        # Workflow-graph tab — sits at the end so the per-tool tabs
        # stay anchored in their familiar order. Clicking flips
        # ``view_kind`` and re-renders the body in place; no URL
        # navigation, the user's tool-tab position is preserved on
        # the way back.
        is_graph_active = view_kind == "graph"
        graph_btn = ui.button("Workflow", icon="account_tree").props(
            f"{'unelevated' if is_graph_active else 'outline'} dense color=primary"
        )
        if not is_graph_active:
            graph_btn.on("click", lambda _e=None: _on_graph_tab_click(state))


_TAB_STATE_ICON: dict[str, str] = {
    "ready": "edit",
    "waiting": "lock",
    "queued": "schedule",
    "running": "hourglass_top",
    "complete": "check_circle",
    "failed": "error",
}
_TAB_STATE_COLOR: dict[str, str] = {
    "ready": "primary",
    "waiting": "grey",
    "queued": "info",
    "running": "warning",
    "complete": "positive",
    "failed": "negative",
}


def _tab_status_for(child_state: str, upstream_ready: bool) -> str:
    """Classify a tab into one of the colour-coded statuses."""
    if child_state == "complete":
        return "complete"
    if child_state == "failed":
        return "failed"
    if child_state == "running":
        return "running"
    if child_state == "queued":
        return "queued"
    # state == "created" — depends on upstream readiness.
    return "ready" if upstream_ready else "waiting"


def _all_upstream_ready(state: dict[str, Any], child: dict[str, Any]) -> bool:
    """True when every gating upstream tool for this child is complete.

    A ``downstream_input`` field on the child's descriptor whose
    ``upstream_tool`` is also in this workflow must have a corresponding
    *complete* child. Fields whose upstream isn't in the workflow are
    free-entry — they don't gate readiness.
    """
    alias = str(child.get("tool_alias") or "")
    descriptor = state["descriptors"].get(alias) or {}
    inputs = descriptor.get("inputs") or []
    completed = state.get("completed_aliases") or {}
    children_by_alias = {str(c.get("tool_alias")): c for c in (state["run"].get("children") or [])}
    for inp in inputs:
        if not isinstance(inp, dict):
            continue
        if not bool(inp.get("downstream_input")):
            continue
        upstream_alias = inp.get("upstream_tool")
        if not isinstance(upstream_alias, str) or not upstream_alias:
            continue
        if upstream_alias not in children_by_alias:
            # Upstream isn't part of this workflow — falls back to
            # free entry; doesn't block readiness.
            continue
        if upstream_alias not in completed:
            return False
    return True


def _on_graph_tab_click(state: dict[str, Any]) -> None:
    """Switch to the workflow-graph view (ADR-0013).

    No URL navigation — unlike the per-tool tabs which represent a
    specific child Job, the graph tab is a local-view switch only.
    A user moving from graph back to a tool tab re-enters the existing
    ``_on_tab_click`` path (which DOES navigate, picking up the
    child's URL).
    """
    state["view_kind"] = "graph"
    _rerender_all(state, force=True)


def _on_tab_click(state: dict[str, Any], position: int) -> None:
    """Switch the active tab — navigates so the URL + selector bar stay in sync.

    Clicking a tab inside a workflow run isn't just a local re-render
    in workspace mode — it's a context switch. The workspace's Job-ID
    selector should land on the new tab's child job, the URL should
    record ``?workflow_run=N&job=M`` so the back-button reflects the
    history, and the standalone-job branch of the right-pane router
    still gets to inspect the child job's state.

    We resolve the new tab's job_id off ``state["run"].children`` and
    delegate to ``ui.navigate.to``. The page handler re-runs end-to-
    end on the new URL — which re-renders the selector bar +
    re-loads the workflow run from the freshest DB state, picking
    up any in-flight transitions on every tab switch.
    """
    state["view_kind"] = "tool"
    state["active_position"] = position
    children = state["run"].get("children") or []
    active = next(
        (c for c in children if int(c.get("workflow_position") or 0) == position),
        None,
    )
    new_job_id = int(active.get("id") or 0) if active is not None else 0
    ui.navigate.to(
        f"/my-jobs?workflow_run={int(state['run_id'])}&job={new_job_id}"
        if new_job_id
        else f"/my-jobs?workflow_run={int(state['run_id'])}"
    )


def _tabs_signature(state: dict[str, Any]) -> tuple[Any, ...]:
    """Comparable snapshot of what the header + tab strip render from.

    Captures the active tab, the view kind, the cancel flag, the
    run-level state (drives the header chip), and the ``(id, state)``
    of *every* child — a downstream member transitioning can flip a
    gating badge anywhere on the strip.

    What it **must not** include: anything that changes on every poll
    regardless of state (e.g. ``updated_at`` on the run row), or the
    strip would rebuild needlessly.
    """
    run = state["run"]
    children = run.get("children") or []
    return (
        state.get("active_position"),
        state.get("view_kind", "tool"),
        run.get("cancel_requested_at"),
        str(run.get("state") or ""),
        tuple((int(c.get("id") or 0), str(c.get("state") or "")) for c in children),
    )


_TERMINAL_CHILD_STATES = frozenset({"complete", "failed"})
"""Child states the embedded job renderer paints as a *final* result
(results/plots for ``complete``, logs for ``failed``) — see
:func:`matofx.ui.pages.job._render_body`. Reaching one of these is the
moment the body must be re-embedded so the results appear."""


def _active_body_phase(state: dict[str, Any], active: dict[str, Any]) -> str:
    """Which of the body's render paths the active tab is on.

    The active body is rendered one of a few ways, and the signature must
    track every transition *this page* is responsible for re-rendering:

    - ``created`` → we render the waiting banner or the submit form
      ourselves, so the waiting↔ready flip is a real re-render.
      Distinguished as ``"waiting"`` / ``"ready"``.
    - ``queued`` / ``running`` → ``"inflight"``: we embed the standalone
      job renderer once and let its own poll swap the status line → logs
      in place. A ``queued → running`` move stays in this phase so we
      don't re-embed (and re-fetch the job) needlessly while it's still
      working.
    - ``complete`` / ``failed`` → ``"terminal"``: a **distinct** phase so
      the moment the active child finishes, the page's own (reliable)
      poll re-embeds the body once and the results/plots appear. We do
      *not* lean on the embedded renderer's inner timer for this — that
      timer can be torn down mid-tick (the standalone My Jobs path adds a
      belt-and-braces outer refresh for exactly this reason), which left
      a finished workflow tab stuck on "running" until a manual refresh.
      Splitting the phase makes the in→terminal transition a real
      signature change, so the body rebuilds exactly once on completion.
    """
    child_state = str(active.get("state") or "created")
    if child_state == "created":
        return "ready" if _all_upstream_ready(state, active) else "waiting"
    if child_state in _TERMINAL_CHILD_STATES:
        return "terminal"
    return "inflight"


def _body_signature(state: dict[str, Any]) -> tuple[Any, ...]:
    """Comparable snapshot of what the *active tab body* renders from.

    Deliberately narrower than :func:`_tabs_signature`: the body only
    needs to rebuild when the *active* tab changes — a different child
    (tab switch), the view kind, the cancel flag, or a change in the
    active tab's *render phase* (see :func:`_active_body_phase`). A
    *downstream* member finishing must NOT rebuild the body. The active
    tab's own ``queued → running`` move also stays put (the embedded
    renderer updates its logs in place), but its ``running → complete`` /
    ``running → failed`` move flips the phase ``inflight → terminal``,
    which re-embeds the body once so the results appear.
    """
    run = state["run"]
    children = run.get("children") or []
    active_position = state.get("active_position")
    active = next(
        (c for c in children if int(c.get("workflow_position") or 0) == active_position),
        None,
    )
    if active is None:
        return (
            active_position,
            state.get("view_kind", "tool"),
            run.get("cancel_requested_at"),
            None,
        )
    return (
        active_position,
        state.get("view_kind", "tool"),
        run.get("cancel_requested_at"),
        int(active.get("id") or 0),
        _active_body_phase(state, active),
    )


def _rerender_all(state: dict[str, Any], *, force: bool = False) -> None:
    """Re-render the header + tab strip and the active body — each gated.

    Called on tab click (with ``force=True``) and on every poll tick
    (without force). The header+strip and the active body are gated on
    *separate* signatures: a poll that only moved a downstream member
    rebuilds the strip's badges without disturbing the active body (and
    the in-progress form / embedded job inside it). When neither
    signature moved, the DOM is left entirely alone and the page reads
    as static.

    Cheap to render fresh because the page only has a handful of
    children — not hundreds — and re-rendering the strip in one shot
    keeps every badge + gating decision in lockstep with one DB
    snapshot.
    """
    children = state["run"].get("children") or []
    # Refreshed up front so the gating helpers (``_all_upstream_ready``,
    # used by both the strip render and ``_body_signature``) see the
    # current completion set regardless of which branch renders below.
    state["completed_aliases"] = {
        str(c.get("tool_alias")): c for c in children if c.get("state") == "complete"
    }

    client = state["client"]

    tabs_sig = _tabs_signature(state)
    if force or tabs_sig != state.get("last_tabs_sig"):
        state["last_tabs_sig"] = tabs_sig
        header = state["header"]
        header.clear()
        with header:
            _render_header(client, state["run"])
        tabs_box = state["tabs_box"]
        tabs_box.clear()
        with tabs_box:
            _render_tabs_strip(state)

    body_sig = _body_signature(state)
    if force or body_sig != state.get("last_body_sig"):
        state["last_body_sig"] = body_sig
        body_box = state["body_box"]
        body_box.clear()
        with body_box:
            _render_active_tab(state)


def _render_active_tab(state: dict[str, Any]) -> None:
    """Render the body for the currently-active tab.

    Form rendering for ``ready`` tabs is workflow-specific (needs the
    upstream-auto-fill seam) so we own it. Everything else — running
    status, complete results with inputs / outputs / downloads,
    failed banner with logs — delegates to the *same renderer the
    standalone job page uses* so users see the identical surface
    inside or outside a workflow.

    When ``view_kind == "graph"``, the body is the workflow-structure
    graph view (ADR-0013) instead. The graph view does its own polling
    and lives entirely inside this body slot, so flipping back to a
    tool tab tears it down cleanly.
    """
    if state.get("view_kind") == "graph":
        _render_graph_body(state)
        return

    run = state["run"]
    children = run.get("children") or []
    active_position = state.get("active_position")
    active = next(
        (c for c in children if int(c.get("workflow_position") or 0) == active_position),
        None,
    )
    if active is None:
        ui.label("Pick a tab above.").style("color: #888;")
        return

    child_state = str(active.get("state") or "created")
    upstream_ready = _all_upstream_ready(state, active)
    tab_status = _tab_status_for(child_state, upstream_ready)

    if tab_status == "waiting":
        with ui.card().classes("w-full"), ui.column().classes("w-full gap-3"):
            _render_tab_header(active, tab_status)
            _render_waiting(state, active)
        return
    if tab_status == "ready":
        with ui.card().classes("w-full"), ui.column().classes("w-full gap-3"):
            _render_tab_header(active, tab_status)
            _render_form(state, active)
        return

    # In-flight / complete / failed → delegate to the standalone job
    # renderer so the tab carries the same inputs+outputs+logs layout
    # users see in My Jobs. Fetch the full job row (the workflow API
    # returns a compact child shape; the renderer wants the full
    # ``JobResponse``).
    client: ApiClient = state["client"]

    async def _embed_standalone_body() -> None:
        from matofx.ui.pages.job import _render_body  # noqa: PLC0415

        try:
            full_job = await client.get_job(int(active.get("id") or 0))
        except ApiError as exc:
            ui.label(f"Couldn't load job details: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return
        body = ui.column().classes("w-full gap-3")
        await _render_body(client, full_job, int(full_job["id"]), body)

    ui.timer(0.0, _embed_standalone_body, once=True)


def _render_graph_body(state: dict[str, Any]) -> None:
    """Render the workflow-structure graph view (ADR-0013) into the tab body.

    Wires ``render_workflow_graph`` with the run-overlay enabled (the
    user is looking at a specific run, so per-node state badges are
    the whole point). The graph view does its own polling — the
    workflow-run page's own poller continues to drive the tab strip
    while the graph view drives its own canvas.

    Embedded via a timer so the async call doesn't block the outer
    re-render — the same pattern the inflight job-body delegation uses.
    """
    run = state["run"]
    workflow_id = int(run.get("workflow_id") or 0)
    run_id = int(state["run_id"])

    if not workflow_id:
        # Defensive: shouldn't happen since the run row carries the FK.
        ui.label("Couldn't determine which workflow this run belongs to.").style(
            "color: var(--matofx-error-text);"
        )
        return

    with ui.card().classes("w-full"), ui.column().classes("w-full gap-3"):
        body = ui.column().classes("w-full gap-3")

        async def _mount() -> None:
            from matofx.ui.workflow_graph_view import (  # noqa: PLC0415
                render_workflow_graph,
            )

            await render_workflow_graph(
                body,
                workflow_id=workflow_id,
                run_id=run_id,
                rank_dir="LR",
                height_px=480,
                show_header=False,  # the page header already names the workflow
            )

        ui.timer(0.0, _mount, once=True)


def _render_tab_header(child: dict[str, Any], tab_status: str) -> None:
    """Small heading inside the tab body — the tool name + status."""
    tool_name = str(child.get("tool_name") or child.get("tool_alias") or "")
    icon = _TAB_STATE_ICON.get(tab_status, "build")
    color = _TAB_STATE_COLOR.get(tab_status, "primary")
    with ui.row().classes("items-center gap-2"):
        ui.icon(icon).style(f"color: var(--q-{color}, #40A1A4); font-size: 1.6rem;")
        ui.label(tool_name).style("font-size: 1.2rem; font-weight: 600;")
        ui.chip(tab_status.title()).props(f"color={color} text-color=white dense")


def _render_waiting(state: dict[str, Any], child: dict[str, Any]) -> None:
    """Show which upstream tools are still pending."""
    alias = str(child.get("tool_alias") or "")
    descriptor = state["descriptors"].get(alias) or {}
    inputs = descriptor.get("inputs") or []
    children_by_alias = {str(c.get("tool_alias")): c for c in (state["run"].get("children") or [])}
    pending: list[str] = []
    seen: set[str] = set()
    for inp in inputs:
        if not isinstance(inp, dict) or not bool(inp.get("downstream_input")):
            continue
        upstream_alias = inp.get("upstream_tool")
        if not isinstance(upstream_alias, str) or upstream_alias in seen:
            continue
        seen.add(upstream_alias)
        upstream_child = children_by_alias.get(upstream_alias)
        if upstream_child is None:
            continue
        if upstream_child.get("state") != "complete":
            pretty = str(upstream_child.get("tool_name") or upstream_alias)
            badge = format_job_state(str(upstream_child.get("state") or ""))
            pending.append(f"{pretty} ({badge})")

    ui.label("Waiting for the upstream tools to finish before this tab can run:").style(
        "color: #888;"
    )
    if pending:
        with ui.column().classes("gap-1"):
            for entry in pending:
                ui.label(f"• {entry}")
    else:
        ui.label("(No specific upstreams identified — refresh the page.)").style("color: #888;")


def _render_form(state: dict[str, Any], child: dict[str, Any]) -> None:
    """Render the submit form using the *same* renderer as a standalone job.

    The only workflow-specific behaviour layered on top of the unified
    :func:`matofx.tools.forms.render_form` is the upstream seam:
    ``downstream_input`` fields whose ``upstream_tool`` is part of this
    run are locked and pre-filled from that tool's outputs. Everything
    else — grouped ``ui.expansion`` layout, sub-columns, units, the rich
    file widget, inline per-field errors and the live cross-field
    (sum-check) checks — is identical to My Jobs, so a tool's form looks
    and behaves the same inside or outside a workflow.
    """
    from matofx.tools.descriptor import ToolDescriptor  # noqa: PLC0415
    from matofx.tools.forms import (  # noqa: PLC0415
        FileInputContext,
        FormValidationError,
        MaterialInputContext,
        render_form,
        validate_inputs,
    )
    from matofx.ui._features import materials_database_enabled  # noqa: PLC0415

    client: ApiClient = state["client"]
    alias = str(child.get("tool_alias") or "")
    job_id = int(child.get("id") or 0)
    descriptor_dict = state["descriptors"].get(alias) or {}
    children_by_alias = {str(c.get("tool_alias")): c for c in (state["run"].get("children") or [])}

    try:
        descriptor = ToolDescriptor.model_validate(dict(descriptor_dict))
    except ValueError as exc:
        ui.label(f"This tool's descriptor is invalid: {exc}").style(
            "color: var(--matofx-error-text);"
        )
        return

    blurb = descriptor.long_description or descriptor.short_description
    if blurb:
        ui.label(blurb).style("color: #888; font-size: 0.9rem; white-space: pre-line;")

    # File inputs talk to *this child draft's* upload endpoints — the
    # same wiring the standalone form uses, so file fields behave
    # identically (drag/drop, browse, pick-from-previous, preview).
    async def _upload_file_input(field_name: str, filename: str, content: bytes) -> str:
        result = await client.upload_draft_file_input(
            job_id, field_name=field_name, filename=filename, content=content
        )
        return str((result.get("inputs") or {}).get(field_name) or "")

    async def _clear_file_input(field_name: str) -> None:
        await client.clear_draft_file_input(job_id, field_name=field_name)

    async def _list_previous_file_inputs(field_name: str) -> list[dict[str, Any]]:
        entries = await client.list_file_input_history(alias, field=field_name)
        return [e for e in entries if e.get("value")]

    async def _fetch_file_preview(path: str) -> bytes:
        return await client.fetch_draft_file_preview(job_id, path=path)

    async def _fetch_cad_preview(path: str) -> bytes:
        return await client.fetch_draft_cad_preview(job_id, path=path)

    async def _list_other_output_candidates(
        accepted_file_types: list[str],
        expected_field_array: str | None,
    ) -> list[dict[str, Any]]:
        # Cross-tool "use a file from another job" for an *un-wired* file
        # input in a workflow tab (a wired input is locked and hides this).
        return await client.list_file_output_candidates(
            accepted_file_types=accepted_file_types,
            expected_field_array=expected_field_array,
        )

    file_ctx = FileInputContext(
        upload=_upload_file_input,
        clear=_clear_file_input,
        list_previous=_list_previous_file_inputs,
        fetch_preview=_fetch_file_preview,
        fetch_cad_preview=_fetch_cad_preview,
        list_other_outputs=_list_other_output_candidates,
    )

    # ``material``-typed fields need the same picker wiring as the
    # standalone form (My Jobs) so a tool's form behaves identically
    # inside a workflow. Without this, the field falls through to the
    # inert fallback widget. Feature-gated: ``None`` → plain fallback
    # when materials_database is off, matching the absent /api/materials.
    async def _list_material_candidates(
        material_class: str, required_props: list[str] | None
    ) -> list[dict[str, Any]]:
        return await client.materials_picker(
            material_class=material_class, required_props=required_props
        )

    material_ctx = (
        MaterialInputContext(list_materials=_list_material_candidates)
        if materials_database_enabled()
        else None
    )
    handle = render_form(
        descriptor, file_input_context=file_ctx, material_input_context=material_ctx
    )

    # Identify the downstream-input fields fed by an upstream tool that
    # is part of this run: those get locked + pre-filled. A downstream
    # field whose upstream isn't in this workflow stays free-entry.
    spec_by_name = {f.name: f for f in descriptor.inputs}
    # (field_name, upstream_alias, upstream_output) — ``upstream_output``
    # falls back to the field's own name for legacy descriptors that
    # predate explicit output selection (the name-equality convention).
    locked_specs: list[tuple[str, str, str]] = []
    for spec in descriptor.inputs:
        if not spec.downstream_input:
            continue
        upstream_alias = spec.upstream_tool
        if upstream_alias and upstream_alias in children_by_alias:
            locked_specs.append((spec.name, upstream_alias, spec.upstream_output or spec.name))
    if locked_specs:
        handle.lock(name for name, _, _ in locked_specs)
        for name, _, _ in locked_specs:
            note = handle.auto_notes.get(name)
            if note is not None:
                note.text = "Provided automatically by an upstream tool."
                note.visible = True

    error_banner = ui.label("").style("color: var(--matofx-error-text);")

    # Pre-fetch upstream outputs once per unique upstream tool, then seed
    # the locked fields. Keyed by upstream alias.
    upstream_cache: dict[str, dict[str, Any]] = {}

    async def _load_upstream(upstream_alias: str) -> dict[str, Any]:
        if upstream_alias in upstream_cache:
            return upstream_cache[upstream_alias]
        upstream_child = children_by_alias.get(upstream_alias)
        by_name: dict[str, Any] = {}
        if upstream_child is not None:
            try:
                doc = await client.get_job_output_document(int(upstream_child.get("id") or 0))
            except ApiError:
                doc = {}
            for entry in doc.get("outputs") or []:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    by_name[entry["name"]] = entry.get("value")
        upstream_cache[upstream_alias] = by_name
        return by_name

    async def _populate_locked() -> None:
        seed_values: dict[str, Any] = {}
        for name, upstream_alias, output_name in locked_specs:
            values = await _load_upstream(upstream_alias)
            if values.get(output_name) is not None:
                seed_values[name] = _cast_for_spec(spec_by_name.get(name), values[output_name])
        if seed_values:
            handle.seed(seed_values)

    async def _submit() -> None:
        error_banner.text = ""
        try:
            values = validate_inputs(descriptor, handle.values())
        except FormValidationError as exc:
            handle.show_errors(exc)
            error_banner.text = "Please correct the highlighted fields and try again."
            return
        handle.clear_errors()
        try:
            await client.queue_draft_job(job_id, inputs=values)
        except ApiError as exc:
            error_banner.text = exc.detail or "Submitting the job failed."
            return
        ui.notify(f"{child.get('tool_name', alias)} submitted.", type="positive")
        ui.navigate.to(f"/my-jobs?workflow_run={int(state['run_id'])}")

    with ui.row().classes("w-full justify-end"):
        submit_button = ui.button("Submit", icon="send", on_click=_submit).props("color=primary")

    # Submit stays disabled until the shared gate reports no outstanding
    # reasons — the *same* :meth:`FormHandle.submit_blockers` the
    # standalone My Jobs form uses, so every gate (required fields,
    # out-of-range / invalid values, section review, and any future gate)
    # applies identically inside a workflow. Job name is exempt: a
    # workflow auto-names its members. Locked upstream-fed fields are
    # skipped by the gate (the user can't fill them; submit-time
    # validation still catches a genuinely missing upstream value). The
    # gate re-evaluates on a short timer to pick async upstream fills up.
    gate_hint = (
        ui.label("").classes("w-full text-right").style("color: #9a7d0a; font-size: 0.85rem;")
    )
    gate_hint.visible = False
    last_hint: dict[str, str] = {"text": ""}

    def _refresh_gate() -> None:
        reasons = handle.submit_blockers(require_job_name=False)
        if reasons:
            submit_button.disable()
            text = "Before submitting — " + "  •  ".join(reasons)
        else:
            submit_button.enable()
            text = ""
        if text != last_hint["text"]:
            last_hint["text"] = text
            gate_hint.text = text
            gate_hint.visible = bool(text)

    _refresh_gate()
    ui.timer(0.3, _refresh_gate)

    if locked_specs:
        ui.timer(0.05, _populate_locked, once=True)


# ---------- form helpers ------------------------------------------------


def _cast_for_spec(spec: Any, value: Any) -> Any:
    """Cast an upstream output value into the shape its input field expects.

    The renderer's widgets are typed (``ui.number`` wants a number, a
    ``select`` wants its option string), so a JSON output value is cast
    to the descriptor field's declared type before it's seeded into the
    locked widget. Best-effort: an uncastable value passes through and
    the field is left for the user / submit validation to flag.
    """
    if spec is None or value is None:
        return value
    try:
        if spec.type == "integer":
            return int(value)
        if spec.type == "float":
            return float(value)
        if spec.type == "boolean":
            return bool(value)
    except (TypeError, ValueError):
        return value
    return value
