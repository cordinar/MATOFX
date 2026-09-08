"""Admin → Usage Statistics (``/ui/admin/usage``).

Aggregate analytics over how the install is being used: job throughput,
compute consumption, reliability, adoption, and seat usage. A shared
filter bar (period + multi-select tool / team / role / execution target)
applies across six tabs: Overview, Compute & Throughput, Reliability,
People & Adoption, Seats & Licence, and Infrastructure.

Permission-gated by ``usage.view`` (read-only, aggregate-only). The
sidebar already hides Admin Space from non-admins, but a logged-in user
who types the URL directly gets a clean permission notice rather than a
blank page — the API returns 403 and the page surfaces it.

Display rule: every label the operator sees is pretty-printed (no raw
aliases or snake_case) — roles via :func:`format_role_label`, execution
targets via :data:`_TARGET_LABELS`, and job states via :func:`_humanize`.
The wire format stays canonical; only the rendering is humanised.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import plotly.graph_objects as go
from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import format_role_label, render_shell, require_logged_in
from matofx.ui.pages._live_jobs_tab import render_live_jobs_tab
from matofx.ui.state import current_token
from matofx.ui.theme import themed_plotly

# Period presets → look-back length in days (12 months ≈ 365 days).
_PERIODS: dict[str, int] = {
    "Last 7 days": 7,
    "Last 30 days": 30,
    "Last 90 days": 90,
    "Last 12 months": 365,
}
_DEFAULT_PERIOD = "Last 30 days"
_CUSTOM_PERIOD = "Custom range…"

# Pretty labels for the execution-target vocabulary.
_TARGET_LABELS: dict[str, str] = {"local": "Local", "aos": "AOS", "hpc": "HPC"}

# Exportable tabs (id → label), mirrors the server's export catalog.
_EXPORT_TABS: tuple[tuple[str, str], ...] = (
    ("overview", "Overview"),
    ("compute", "Compute & Throughput"),
    ("reliability", "Reliability"),
    ("people", "People & Adoption"),
    ("seats", "Seats & Licence"),
    ("infra", "Infrastructure"),
)


@ui.page("/admin/usage")
async def admin_usage_page() -> None:
    """Render the Usage Statistics page (filter bar + tabs)."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())

    # The Live Jobs tab is an optional extra gated by its own permissions
    # (separate from the analytics tabs' usage.view). Decide visibility +
    # cancel affordances up front from /auth/me; the server enforces both
    # regardless of what the UI shows.
    can_view_live = False
    can_cancel_live = False
    try:
        me = await client.me()
        perms = set(me.get("permissions") or [])
        can_view_live = "live_jobs.view" in perms
        can_cancel_live = "live_jobs.cancel" in perms
    except ApiError:
        pass

    live_slot: ui.column | None = None

    with ui.column().classes("w-full gap-4 p-6"):
        # Title only — the Export / Email digest actions live in the filter
        # bar below, clear of the chrome's fixed top-right theme + help cluster.
        with ui.column().classes("gap-0"):
            ui.label("Usage Statistics").style("font-size: 1.5rem; font-weight: 600;")
            ui.label(
                "Aggregate analytics over jobs, tools, users, and compute. "
                "Read-only — no individual records are shown here."
            ).style("color: var(--matofx-muted-text);")

        try:
            options = await client.usage_filters()
        except ApiError as exc:
            ui.label(f"Couldn't open Usage Statistics: {exc.detail}").style("color: #ff6b6b;")
            return

        state: dict[str, Any] = {
            "period": _DEFAULT_PERIOD,
            "since": None,
            "until": None,
            "tool_aliases": [],
            "team_ids": [],
            "roles": [],
            "execution_targets": [],
        }

        # Each tab's body lives inside its panel; the filter bar (above
        # the tabs) reaches them through this holder so Apply can
        # re-render without forward references.
        slots: dict[str, ui.column] = {}

        renderers = {
            "overview": _render_overview,
            "compute": _render_compute,
            "reliability": _render_reliability,
            "people": _render_people,
            "seats": _render_seats,
            "infra": _render_infra,
        }

        async def _apply() -> None:
            for key, render in renderers.items():
                if key in slots:
                    await render(client, state, slots[key])

        _render_filter_bar(client, options, state, on_apply=_apply)

        # Tabs aligned left, sentence-case (no-caps), transparent, with the
        # brand teal active colour + indicator (mirrors the admin pages).
        with (
            ui.tabs()
            .classes("w-full")
            .props("align=left active-color=primary indicator-color=primary")
            .style(
                "background-color: transparent; border-bottom: 1px solid var(--matofx-card-border);"
            ) as tabs
        ):
            t_overview = ui.tab("Overview", icon="dashboard").props("no-caps")
            t_compute = ui.tab("Compute & throughput", icon="memory").props("no-caps")
            t_reliability = ui.tab("Reliability", icon="fact_check").props("no-caps")
            t_people = ui.tab("People & adoption", icon="groups").props("no-caps")
            t_seats = ui.tab("Seats & licence", icon="badge").props("no-caps")
            t_infra = ui.tab("Infrastructure", icon="dns").props("no-caps")
            t_live = (
                ui.tab("Live jobs", icon="pending_actions").props("no-caps")
                if can_view_live
                else None
            )

        with (
            ui.tab_panels(tabs, value=t_overview)
            .classes("w-full")
            .style("background-color: transparent;")
        ):
            with ui.tab_panel(t_overview):
                slots["overview"] = ui.column().classes("w-full gap-4")
            with ui.tab_panel(t_compute):
                slots["compute"] = ui.column().classes("w-full gap-4")
            with ui.tab_panel(t_reliability):
                slots["reliability"] = ui.column().classes("w-full gap-4")
            with ui.tab_panel(t_people):
                slots["people"] = ui.column().classes("w-full gap-4")
            with ui.tab_panel(t_seats):
                slots["seats"] = ui.column().classes("w-full gap-4")
            with ui.tab_panel(t_infra):
                slots["infra"] = ui.column().classes("w-full gap-4")
            if t_live is not None:
                with ui.tab_panel(t_live):
                    live_slot = ui.column().classes("w-full gap-4")

    await _apply()

    if live_slot is not None:
        await render_live_jobs_tab(client, live_slot, can_cancel=can_cancel_live)


# ---- filter bar ------------------------------------------------------------


def _render_filter_bar(
    client: ApiClient,
    options: dict[str, Any],
    state: dict[str, Any],
    *,
    on_apply: Any,
) -> None:
    """Build the shared filter bar; ``on_apply`` re-renders the active tabs.

    Tool / team / role / target are multi-select (empty ⇒ all). The
    period select reveals two date pickers when "Custom range…" is
    chosen. Every option label is pretty-printed.
    """
    tool_options: dict[Any, str] = {t["alias"]: t["name"] for t in options.get("tools", [])}
    team_options: dict[Any, str] = {t["id"]: t["name"] for t in options.get("teams", [])}
    role_options: dict[Any, str] = {r: format_role_label(r) for r in options.get("roles", [])}
    target_options: dict[Any, str] = {
        t: _target_label(t) for t in options.get("execution_targets", [])
    }

    with ui.card().classes("w-full"), ui.column().classes("w-full gap-3"):
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            period_sel = (
                ui.select([*_PERIODS, _CUSTOM_PERIOD], value=_DEFAULT_PERIOD, label="Period")
                .props("dense outlined")
                .style("min-width: 150px;")
            )
            with ui.row().classes("items-end gap-3") as date_row:
                since_in = _date_input("From")
                until_in = _date_input("To")
            date_row.bind_visibility_from(
                period_sel, "value", backward=lambda v: v == _CUSTOM_PERIOD
            )

        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            tool_sel = _multiselect(tool_options, "Tools")
            team_sel = _multiselect(team_options, "Teams")
            role_sel = _multiselect(role_options, "Roles")
            target_sel = _multiselect(target_options, "Targets")

            async def _apply() -> None:
                state["period"] = period_sel.value or _DEFAULT_PERIOD
                state["since"] = since_in.value or None
                state["until"] = until_in.value or None
                state["tool_aliases"] = list(tool_sel.value or [])
                state["team_ids"] = list(team_sel.value or [])
                state["roles"] = list(role_sel.value or [])
                state["execution_targets"] = list(target_sel.value or [])
                await on_apply()

            ui.button("Apply", icon="filter_alt", on_click=_apply).props("color=primary")
            # Export / Email digest sit at the far right of the filter row —
            # the dialogs reuse the current filter selection.
            ui.space()
            ui.button(
                "Export",
                icon="download",
                on_click=lambda: _open_export_dialog(client, state),
            ).props("outline")
            ui.button(
                "Email digest",
                icon="mail",
                on_click=lambda: _open_digest_dialog(client, state),
            ).props("outline")


def _multiselect(options: dict[Any, str], label: str) -> ui.select:
    """A clearable multi-select dropdown (empty selection ⇒ all)."""
    return (
        ui.select(options, value=[], label=label, multiple=True)
        .props("dense outlined use-chips clearable")
        .style("min-width: 200px;")
    )


def _date_input(label: str) -> ui.input:
    """A text input backed by a calendar popup; value is ``YYYY-MM-DD``."""
    with ui.input(label).props("dense outlined").style("min-width: 150px;") as date_in:
        with ui.menu().props("no-parent-event") as menu:
            ui.date().bind_value(date_in)
        with date_in.add_slot("append"):
            ui.icon("event").classes("cursor-pointer").on("click", menu.open)
    return date_in


def _window_iso(state: dict[str, Any]) -> tuple[str, str]:
    """Resolve the (since, until) ISO strings for the current filter state.

    A complete custom range wins; otherwise the period preset applies
    (also the fallback when "Custom range…" is chosen without both dates).
    """
    if state["period"] == _CUSTOM_PERIOD and state["since"] and state["until"]:
        return (f"{state['since']}T00:00:00", f"{state['until']}T23:59:59")
    until = _dt.datetime.now(tz=_dt.UTC)
    days = _PERIODS.get(state["period"], _PERIODS[_DEFAULT_PERIOD])
    since = until - _dt.timedelta(days=days)
    return (since.isoformat(), until.isoformat())


def _filter_kwargs(state: dict[str, Any]) -> dict[str, Any]:
    """The keyword args shared by the overview / compute client calls."""
    since, until = _window_iso(state)
    return {
        "since": since,
        "until": until,
        "tool_aliases": state["tool_aliases"] or None,
        "team_ids": state["team_ids"] or None,
        "roles": state["roles"] or None,
        "execution_targets": state["execution_targets"] or None,
    }


def _open_export_dialog(client: ApiClient, state: dict[str, Any]) -> None:
    """Open the export dialog: pick tabs + format, then download.

    The current filter selection is applied to the export, so what you
    download matches what's on screen.
    """
    with ui.dialog() as dialog, ui.card().style("min-width: 360px;"):
        ui.label("Export usage data").style("font-size: 1.1rem; font-weight: 600;")
        ui.label("Choose which tabs to include — the current filters apply.").style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )
        checks = {tab_id: ui.checkbox(label, value=True) for tab_id, label in _EXPORT_TABS}
        ui.separator()
        fmt = ui.radio(
            {"csv": "CSV (ZIP of tables)", "html": "Printable report (HTML — print to PDF)"},
            value="csv",
        ).props("inline")

        async def _download() -> None:
            tabs = [tab_id for tab_id, chk in checks.items() if chk.value]
            if not tabs:
                ui.notify("Pick at least one tab to export.", type="warning")
                return
            chosen = fmt.value or "csv"
            try:
                data = await client.usage_export(fmt=chosen, tabs=tabs, **_filter_kwargs(state))
            except ApiError as exc:
                ui.notify(f"Export failed: {exc.detail}", type="negative")
                return
            stamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%d-%H%M")
            if chosen == "html":
                ui.download(data, f"usage-{stamp}.html", "text/html")
            else:
                ui.download(data, f"usage-{stamp}.zip", "application/zip")
            dialog.close()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Download", icon="download", on_click=_download).props("color=primary")
    dialog.open()


def _open_digest_dialog(client: ApiClient, state: dict[str, Any]) -> None:
    """Open the email-digest dialog: recipients + tabs, then send now.

    The emailed report uses the page's current filters. Recurring
    delivery on a schedule arrives with the cron subsystem (v1.0); for
    now this sends the report immediately to the addresses entered.
    """
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label("Email usage digest").style("font-size: 1.1rem; font-weight: 600;")
        ui.label(
            "Send the report (current filters applied) to the addresses below. "
            "Scheduled delivery lands in v1.0 — this sends it now."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        recipients_in = (
            ui.textarea(
                label="Recipients",
                placeholder="alice@example.com, bob@example.com",
            )
            .props("outlined autogrow")
            .classes("w-full")
        )
        ui.label("Include tabs").style("font-size: 0.85rem; font-weight: 600;")
        checks = {tab_id: ui.checkbox(label, value=True) for tab_id, label in _EXPORT_TABS}

        async def _send() -> None:
            recipients = [
                r.strip() for r in _split_recipients(recipients_in.value or "") if r.strip()
            ]
            if not recipients:
                ui.notify("Enter at least one recipient email.", type="warning")
                return
            tabs = [tab_id for tab_id, chk in checks.items() if chk.value]
            if not tabs:
                ui.notify("Pick at least one tab to include.", type="warning")
                return
            try:
                result = await client.usage_digest_send(
                    recipients=recipients, tabs=tabs, **_filter_kwargs(state)
                )
            except ApiError as exc:
                ui.notify(f"Digest failed: {exc.detail}", type="negative")
                return
            if result.get("disabled"):
                ui.notify(
                    "Email is disabled on this install — ask an admin to configure SMTP.",
                    type="warning",
                )
                return
            sent = int(result.get("sent") or 0)
            failed = result.get("failed") or []
            if failed:
                ui.notify(f"Sent {sent}; {len(failed)} failed.", type="warning")
            else:
                ui.notify(f"Digest sent to {sent} recipient(s).", type="positive")
            dialog.close()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Send now", icon="send", on_click=_send).props("color=primary")
    dialog.open()


def _split_recipients(raw: str) -> list[str]:
    """Split a free-text recipient list on commas, semicolons, and newlines."""
    for sep in (";", "\n"):
        raw = raw.replace(sep, ",")
    return raw.split(",")


# ---- Overview tab ----------------------------------------------------------


async def _render_overview(client: ApiClient, state: dict[str, Any], slot: ui.column) -> None:
    """Fetch + render the Overview tab for the current filter state."""
    slot.clear()
    with slot:
        ui.label("Loading…").style("color: var(--matofx-muted-text);")
    try:
        data = await client.usage_overview(**_filter_kwargs(state))
    except ApiError as exc:
        slot.clear()
        with slot:
            ui.label(f"Couldn't load usage data: {exc.detail}").style("color: #ff6b6b;")
        return

    slot.clear()
    with slot:
        _kpi_strip(data)
        with ui.row().classes("w-full gap-4 flex-wrap"):
            with ui.card().classes("flex-1").style("min-width: 480px;"):
                ui.label("Jobs over time (by state)").style("font-weight: 600;")
                _jobs_over_time_chart(list(data.get("series") or []))
            with ui.card().classes("flex-1").style("min-width: 360px;"):
                ui.label("Top tools").style("font-weight: 600;")
                _top_tools_chart(list(data.get("top_tools") or []))


def _kpi_strip(data: dict[str, Any]) -> None:
    """Render the five headline KPI tiles."""
    registered = int(data.get("registered_users") or 0)
    with ui.row().classes("w-full gap-3 flex-wrap"):
        _kpi_tile(
            "Active users",
            data.get("active_users"),
            fmt=_fmt_int,
            suffix=f"of {registered:,} registered",
        )
        _kpi_tile("Jobs run", data.get("jobs_total"), fmt=_fmt_int)
        _kpi_tile("Success rate", data.get("success_rate"), fmt=_fmt_pct)
        _kpi_tile("Core-hours", data.get("core_hours"), fmt=_fmt_num)
        _kpi_tile("GPU-hours", data.get("gpu_hours"), fmt=_fmt_num)


def _kpi_tile(
    label: str,
    kpi: dict[str, Any] | None,
    *,
    fmt: Any,
    suffix: str | None = None,
) -> None:
    """One KPI card: label, big value, period-over-period delta, suffix."""
    kpi = kpi or {}
    with ui.card().classes("flex-1").style("min-width: 150px;"):
        ui.label(label).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        ui.label(fmt(kpi.get("value"))).style("font-size: 1.6rem; font-weight: 700;")
        delta = kpi.get("delta_pct")
        if delta is not None:
            up = delta >= 0
            ui.label(f"{'▲' if up else '▼'} {abs(delta):.0f}%").style(
                f"font-size: 0.8rem; color: {'#21ba45' if up else '#ff6b6b'};"
            )
        else:
            ui.label("—").style("font-size: 0.8rem; color: var(--matofx-muted-text);")
        if suffix:
            ui.label(suffix).style("color: var(--matofx-muted-text); font-size: 0.75rem;")


def _jobs_over_time_chart(series: list[dict[str, Any]]) -> None:
    """Stacked bar of per-day job counts, one trace per run state."""
    if not series:
        ui.label("No jobs in this period.").style("color: var(--matofx-muted-text);")
        return
    dates = sorted({str(p["date"]) for p in series})
    states = sorted({str(p["state"]) for p in series})
    lookup = {(str(p["date"]), str(p["state"])): int(p["count"]) for p in series}
    fig = go.Figure()
    for st in states:
        fig.add_bar(x=dates, y=[lookup.get((d, st), 0) for d in dates], name=_humanize(st))
    fig.update_layout(
        barmode="stack",
        margin={"t": 10, "l": 40, "r": 10, "b": 40},
        legend={"orientation": "h"},
        height=360,
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


def _top_tools_chart(tools: list[dict[str, Any]]) -> None:
    """Horizontal bar of the most-used tools (highest at the top)."""
    if not tools:
        ui.label("No tool usage in this period.").style("color: var(--matofx-muted-text);")
        return
    ordered = list(reversed(tools))  # plotly draws bottom-up; reverse for desc
    fig = go.Figure(
        go.Bar(
            x=[int(t["count"]) for t in ordered],
            y=[str(t["tool_name"]) for t in ordered],
            orientation="h",
        )
    )
    fig.update_layout(margin={"t": 10, "l": 10, "r": 10, "b": 40}, height=360)
    ui.plotly(themed_plotly(fig)).classes("w-full")


# ---- Compute & Throughput tab ----------------------------------------------

# Consumption metric → (the response field, the axis label).
_METRICS: dict[str, tuple[str, str]] = {
    "Core-hours": ("core_hours", "Core-hours"),
    "GPU-hours": ("gpu_hours", "GPU-hours"),
    "Jobs": ("jobs", "Jobs"),
}
_CONSUMPTION_LIMIT = 15


async def _render_compute(client: ApiClient, state: dict[str, Any], slot: ui.column) -> None:
    """Fetch + render the Compute & Throughput tab for the current filters."""
    slot.clear()
    with slot:
        ui.label("Loading…").style("color: var(--matofx-muted-text);")
    try:
        data = await client.usage_compute(**_filter_kwargs(state))
    except ApiError as exc:
        slot.clear()
        with slot:
            ui.label(f"Couldn't load compute data: {exc.detail}").style("color: #ff6b6b;")
        return

    slot.clear()
    with slot:
        with ui.card().classes("w-full"):
            ui.label("Compute consumption").style("font-weight: 600;")
            with ui.row().classes("items-center gap-4 flex-wrap"):
                metric = ui.toggle(list(_METRICS), value="Core-hours").props("dense")
                group = ui.toggle(["Tool", "Team"], value="Tool").props("dense")
            chart_slot = ui.column().classes("w-full")

            def _redraw() -> None:
                chart_slot.clear()
                with chart_slot:
                    _consumption_chart(data, metric.value or "Core-hours", group.value or "Tool")

            metric.on_value_change(_redraw)
            group.on_value_change(_redraw)
            _redraw()

        with ui.row().classes("w-full gap-4 flex-wrap"):
            with ui.card().classes("flex-1").style("min-width: 460px;"):
                ui.label("Runtime distribution (per tool)").style("font-weight: 600;")
                _runtime_table(list(data.get("runtime") or []))
            with ui.card().classes("flex-1").style("min-width: 360px;"):
                ui.label("Queue wait (avg per day)").style("font-weight: 600;")
                _queue_wait_chart(list(data.get("queue_wait") or []))

        with ui.card().classes("w-full"):
            ui.label("Throughput — submitted vs completed").style("font-weight: 600;")
            _throughput_chart(list(data.get("throughput") or []))


def _consumption_chart(data: dict[str, Any], metric: str, group: str) -> None:
    """Horizontal bar of consumption by tool or team for the chosen metric."""
    field, axis = _METRICS.get(metric, _METRICS["Core-hours"])
    if group == "Team":
        items = list(data.get("by_team") or [])
        name_key = "team_name"
    else:
        items = list(data.get("by_tool") or [])
        name_key = "tool_name"
    if not items:
        ui.label("Nothing ran in this period.").style("color: var(--matofx-muted-text);")
        return
    ranked = sorted(items, key=lambda i: float(i.get(field) or 0), reverse=True)
    ranked = ranked[:_CONSUMPTION_LIMIT]
    ordered = list(reversed(ranked))  # plotly draws bottom-up
    fig = go.Figure(
        go.Bar(
            x=[float(i.get(field) or 0) for i in ordered],
            y=[str(i.get(name_key)) for i in ordered],
            orientation="h",
        )
    )
    fig.update_layout(
        margin={"t": 10, "l": 10, "r": 10, "b": 40},
        height=380,
        xaxis={"title": axis},
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


def _runtime_table(runtime: list[dict[str, Any]]) -> None:
    """A compact p50 / p95 / max table, one row per tool."""
    if not runtime:
        ui.label("No completed runs in this period.").style("color: var(--matofx-muted-text);")
        return
    columns = [
        {"name": "tool", "label": "Tool", "field": "tool", "align": "left"},
        {"name": "p50", "label": "p50", "field": "p50"},
        {"name": "p95", "label": "p95", "field": "p95"},
        {"name": "max", "label": "Max", "field": "max"},
        {"name": "runs", "label": "Runs", "field": "runs"},
    ]
    rows = [
        {
            "tool": str(r.get("tool_name")),
            "p50": _fmt_hours(float(r.get("p50_hours") or 0)),
            "p95": _fmt_hours(float(r.get("p95_hours") or 0)),
            "max": _fmt_hours(float(r.get("max_hours") or 0)),
            "runs": int(r.get("runs") or 0),
        }
        for r in runtime
    ]
    ui.table(columns=columns, rows=rows, row_key="tool").props("dense flat").classes("w-full")


def _queue_wait_chart(points: list[dict[str, Any]]) -> None:
    """Line of average queue wait (minutes) per day."""
    if not points:
        ui.label("No started jobs in this period.").style("color: var(--matofx-muted-text);")
        return
    dates = [str(p["date"]) for p in points]
    minutes = [float(p["value"]) * 60.0 for p in points]
    fig = go.Figure(go.Scatter(x=dates, y=minutes, mode="lines+markers"))
    fig.update_layout(
        margin={"t": 10, "l": 40, "r": 10, "b": 40},
        height=360,
        yaxis={"title": "Minutes"},
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


def _throughput_chart(points: list[dict[str, Any]]) -> None:
    """Grouped bars of jobs submitted vs completed per day."""
    if not points:
        ui.label("No activity in this period.").style("color: var(--matofx-muted-text);")
        return
    dates = [str(p["date"]) for p in points]
    fig = go.Figure()
    fig.add_bar(x=dates, y=[int(p["submitted"]) for p in points], name="Submitted")
    fig.add_bar(x=dates, y=[int(p["completed"]) for p in points], name="Completed")
    fig.update_layout(
        barmode="group",
        margin={"t": 10, "l": 40, "r": 10, "b": 40},
        legend={"orientation": "h"},
        height=340,
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


# ---- Reliability tab -------------------------------------------------------

# Pretty labels for approval gate kinds.
_APPROVAL_KINDS: dict[str, str] = {"pre": "Pre-run", "post": "Post-results"}


async def _render_reliability(client: ApiClient, state: dict[str, Any], slot: ui.column) -> None:
    """Fetch + render the Reliability tab for the current filter state."""
    slot.clear()
    with slot:
        ui.label("Loading…").style("color: var(--matofx-muted-text);")
    try:
        data = await client.usage_reliability(**_filter_kwargs(state))
    except ApiError as exc:
        slot.clear()
        with slot:
            ui.label(f"Couldn't load reliability data: {exc.detail}").style("color: #ff6b6b;")
        return

    slot.clear()
    with slot:
        with ui.row().classes("w-full gap-3 flex-wrap"):
            _kpi_tile("Success rate", data.get("success_rate"), fmt=_fmt_pct)
        with ui.row().classes("w-full gap-4 flex-wrap"):
            with ui.card().classes("flex-1").style("min-width: 460px;"):
                ui.label("Success rate over time").style("font-weight: 600;")
                _success_trend_chart(list(data.get("success_trend") or []))
            with ui.card().classes("flex-1").style("min-width: 320px;"):
                ui.label("Exit codes").style("font-weight: 600;")
                _exit_codes_chart(list(data.get("exit_codes") or []))
        with ui.row().classes("w-full gap-4 flex-wrap"):
            with ui.card().classes("flex-1").style("min-width: 460px;"):
                ui.label("Top failing tools").style("font-weight: 600;")
                _failing_table(list(data.get("top_failing") or []))
            with ui.card().classes("flex-1").style("min-width: 320px;"):
                ui.label("Approval funnel").style("font-weight: 600;")
                _approval_funnel_table(list(data.get("approval_funnel") or []))


def _success_trend_chart(points: list[dict[str, Any]]) -> None:
    """Line of daily success rate (percent)."""
    if not points:
        ui.label("No completed or failed jobs in this period.").style(
            "color: var(--matofx-muted-text);"
        )
        return
    dates = [str(p["date"]) for p in points]
    values = [float(p["value"]) for p in points]
    fig = go.Figure(go.Scatter(x=dates, y=values, mode="lines+markers"))
    fig.update_layout(
        margin={"t": 10, "l": 40, "r": 10, "b": 40},
        height=340,
        yaxis={"title": "Success %", "range": [0, 100]},
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


def _exit_codes_chart(codes: list[dict[str, Any]]) -> None:
    """Bar of job counts per process exit code."""
    if not codes:
        ui.label("No jobs recorded an exit code.").style("color: var(--matofx-muted-text);")
        return
    fig = go.Figure(
        go.Bar(x=[str(c["exit_code"]) for c in codes], y=[int(c["count"]) for c in codes])
    )
    fig.update_layout(
        margin={"t": 10, "l": 40, "r": 10, "b": 40},
        height=340,
        xaxis={"title": "Exit code", "type": "category"},
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


def _failing_table(failing: list[dict[str, Any]]) -> None:
    """Table of tools with failures, most failures first."""
    if not failing:
        ui.label("No failures in this period. 🎉").style("color: var(--matofx-muted-text);")
        return
    columns = [
        {"name": "tool", "label": "Tool", "field": "tool", "align": "left"},
        {"name": "failures", "label": "Failures", "field": "failures"},
        {"name": "rate", "label": "Failure rate", "field": "rate"},
        {"name": "last", "label": "Last failed", "field": "last", "align": "left"},
    ]
    rows = [
        {
            "tool": str(f.get("tool_name")),
            "failures": int(f.get("failures") or 0),
            "rate": _fmt_pct(f.get("failure_rate")),
            "last": _fmt_date(f.get("last_failed_at")),
        }
        for f in failing
    ]
    ui.table(columns=columns, rows=rows, row_key="tool").props("dense flat").classes("w-full")


def _approval_funnel_table(funnel: list[dict[str, Any]]) -> None:
    """Per-kind approval outcomes (requested / approved / denied / pending)."""
    if not funnel:
        ui.label("No approval requests in this period.").style("color: var(--matofx-muted-text);")
        return
    columns = [
        {"name": "kind", "label": "Gate", "field": "kind", "align": "left"},
        {"name": "requested", "label": "Requested", "field": "requested"},
        {"name": "approved", "label": "Approved", "field": "approved"},
        {"name": "denied", "label": "Denied", "field": "denied"},
        {"name": "pending", "label": "Pending", "field": "pending"},
    ]
    rows = [
        {
            "kind": _APPROVAL_KINDS.get(str(f.get("kind")), _humanize(str(f.get("kind")))),
            "requested": int(f.get("requested") or 0),
            "approved": int(f.get("approved") or 0),
            "denied": int(f.get("denied") or 0),
            "pending": int(f.get("pending") or 0),
        }
        for f in funnel
    ]
    ui.table(columns=columns, rows=rows, row_key="kind").props("dense flat").classes("w-full")


# ---- People & Adoption tab -------------------------------------------------

_PEOPLE_GROUPS: dict[str, str] = {"User": "by_user", "Team": "by_team", "Role": "by_role"}


async def _render_people(client: ApiClient, state: dict[str, Any], slot: ui.column) -> None:
    """Fetch + render the People & Adoption tab for the current filters."""
    slot.clear()
    with slot:
        ui.label("Loading…").style("color: var(--matofx-muted-text);")
    try:
        data = await client.usage_people(**_filter_kwargs(state))
    except ApiError as exc:
        slot.clear()
        with slot:
            ui.label(f"Couldn't load people data: {exc.detail}").style("color: #ff6b6b;")
        return

    registered = int(data.get("registered_users") or 0)
    slot.clear()
    with slot:
        with ui.row().classes("w-full gap-3 flex-wrap"):
            _kpi_tile(
                "Active users",
                data.get("active_users"),
                fmt=_fmt_int,
                suffix=f"of {registered:,} registered",
            )
            _kpi_tile("New users", data.get("new_users"), fmt=_fmt_int)
            _plain_tile("Peak concurrent sessions", _fmt_int(data.get("peak_concurrency")))
        with ui.row().classes("w-full gap-4 flex-wrap"):
            with ui.card().classes("flex-1").style("min-width: 460px;"):
                ui.label("Active users over time").style("font-weight: 600;")
                _day_value_chart(list(data.get("active_trend") or []), "Active users")
            with ui.card().classes("flex-1").style("min-width: 360px;"):
                ui.label("Usage by").style("font-weight: 600;")
                group = ui.toggle(list(_PEOPLE_GROUPS), value="User").props("dense")
                group_slot = ui.column().classes("w-full")

                def _redraw() -> None:
                    group_slot.clear()
                    key = _PEOPLE_GROUPS.get(group.value or "User", "by_user")
                    with group_slot:
                        _named_count_chart(list(data.get(key) or []), "Jobs")

                group.on_value_change(_redraw)
                _redraw()


# ---- Seats & Licence tab ---------------------------------------------------


def _cap_suffix(data: dict[str, Any]) -> str:
    """Say which figure the cap governs — they aren't interchangeable.

    Under a concurrent-session licence the cap bounds *peak concurrent
    sessions*; under floating access it bounds *enabled seats*. Showing
    a bare number next to both would invite reading it against the wrong
    one.
    """
    if data.get("cap") is None:
        return "no licence installed"
    licence_type = str(data.get("licence_type") or "").lower()
    state = str(data.get("licence_state") or "")
    governs = (
        "peak concurrent sessions"
        if licence_type == "csl"
        else "enabled seats"
        if licence_type == "fal"
        else "seats"
    )
    return f"{licence_type.upper()} — limits {governs}" + (
        f" ({state})" if state and state != "active" else ""
    )


async def _render_seats(client: ApiClient, state: dict[str, Any], slot: ui.column) -> None:
    """Fetch + render the Seats & Licence tab for the current filters."""
    slot.clear()
    with slot:
        ui.label("Loading…").style("color: var(--matofx-muted-text);")
    try:
        data = await client.usage_seats(**_filter_kwargs(state))
    except ApiError as exc:
        slot.clear()
        with slot:
            ui.label(f"Couldn't load seats data: {exc.detail}").style("color: #ff6b6b;")
        return

    cap = data.get("cap")
    slot.clear()
    with slot:
        with ui.row().classes("w-full gap-3 flex-wrap"):
            _plain_tile("Enabled seats", _fmt_int(data.get("enabled_seats")))
            _plain_tile("Peak concurrent sessions", _fmt_int(data.get("peak_concurrency")))
            _plain_tile("Registered users", _fmt_int(data.get("registered_users")))
            _plain_tile(
                "Licensed cap",
                _fmt_int(cap) if cap is not None else "—",
                suffix=_cap_suffix(data),
            )
        with ui.row().classes("w-full gap-4 flex-wrap"):
            with ui.card().classes("flex-1").style("min-width: 460px;"):
                ui.label("Peak concurrent sessions over time").style("font-weight: 600;")
                _day_value_chart(list(data.get("concurrency_trend") or []), "Sessions")
            with ui.card().classes("flex-1").style("min-width: 360px;"):
                ui.label("Logon access — grants vs revocations").style("font-weight: 600;")
                _logon_events_chart(list(data.get("logon_events") or []))


# ---- Infrastructure tab ----------------------------------------------------


async def _render_infra(client: ApiClient, state: dict[str, Any], slot: ui.column) -> None:
    """Fetch + render the Infrastructure tab for the current filters."""
    slot.clear()
    with slot:
        ui.label("Loading…").style("color: var(--matofx-muted-text);")
    try:
        data = await client.usage_infra(**_filter_kwargs(state))
    except ApiError as exc:
        slot.clear()
        with slot:
            ui.label(f"Couldn't load infrastructure data: {exc.detail}").style("color: #ff6b6b;")
        return

    slot.clear()
    with slot:
        with ui.card().classes("w-full"):
            ui.label("By execution target").style("font-weight: 600;")
            metric = ui.toggle(list(_METRICS), value="Core-hours").props("dense")
            target_slot = ui.column().classes("w-full")

            def _redraw() -> None:
                target_slot.clear()
                with target_slot:
                    _target_chart(list(data.get("by_target") or []), metric.value or "Core-hours")

            metric.on_value_change(_redraw)
            _redraw()

        with ui.card().classes("w-full"):
            ui.label("CPU vs GPU").style("font-weight: 600;")
            with ui.row().classes("w-full gap-3 flex-wrap"):
                _plain_tile("CPU jobs", _fmt_int(data.get("cpu_jobs")))
                _plain_tile("GPU jobs", _fmt_int(data.get("gpu_jobs")))
                _plain_tile("CPU core-hours", _fmt_num(data.get("cpu_core_hours")))
                _plain_tile("GPU core-hours", _fmt_num(data.get("gpu_core_hours")))


def _target_chart(targets: list[dict[str, Any]], metric: str) -> None:
    """Bar of jobs / core-hours / GPU-hours per execution target."""
    if not targets:
        ui.label("Nothing ran in this period.").style("color: var(--matofx-muted-text);")
        return
    field, axis = _METRICS.get(metric, _METRICS["Core-hours"])
    fig = go.Figure(
        go.Bar(
            x=[_target_label(str(t["target"])) for t in targets],
            y=[float(t.get(field) or 0) for t in targets],
        )
    )
    fig.update_layout(
        margin={"t": 10, "l": 40, "r": 10, "b": 40}, height=340, yaxis={"title": axis}
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


def _logon_events_chart(points: list[dict[str, Any]]) -> None:
    """Grouped bars of logon-access grants vs revocations per day."""
    if not points:
        ui.label("No logon-access changes in this period.").style(
            "color: var(--matofx-muted-text);"
        )
        return
    dates = [str(p["date"]) for p in points]
    fig = go.Figure()
    fig.add_bar(x=dates, y=[int(p["granted"]) for p in points], name="Granted")
    fig.add_bar(x=dates, y=[int(p["revoked"]) for p in points], name="Revoked")
    fig.update_layout(
        barmode="group",
        margin={"t": 10, "l": 40, "r": 10, "b": 40},
        legend={"orientation": "h"},
        height=340,
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


def _day_value_chart(points: list[dict[str, Any]], axis: str) -> None:
    """Generic line chart of a (date, value) series."""
    if not points:
        ui.label("No data in this period.").style("color: var(--matofx-muted-text);")
        return
    dates = [str(p["date"]) for p in points]
    values = [float(p["value"]) for p in points]
    fig = go.Figure(go.Scatter(x=dates, y=values, mode="lines+markers"))
    fig.update_layout(
        margin={"t": 10, "l": 40, "r": 10, "b": 40}, height=340, yaxis={"title": axis}
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


def _named_count_chart(items: list[dict[str, Any]], axis: str) -> None:
    """Horizontal bar of name → count (highest at the top)."""
    if not items:
        ui.label("No activity in this period.").style("color: var(--matofx-muted-text);")
        return
    ordered = list(reversed(items))  # plotly draws bottom-up
    fig = go.Figure(
        go.Bar(
            x=[int(i["count"]) for i in ordered],
            y=[str(i["name"]) for i in ordered],
            orientation="h",
        )
    )
    fig.update_layout(
        margin={"t": 10, "l": 10, "r": 10, "b": 40}, height=360, xaxis={"title": axis}
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


# ---- shared helpers --------------------------------------------------------


def _plain_tile(label: str, value: str, *, suffix: str | None = None) -> None:
    """A KPI-style card for a single value with no delta."""
    with ui.card().classes("flex-1").style("min-width: 150px;"):
        ui.label(label).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        ui.label(value).style("font-size: 1.6rem; font-weight: 700;")
        if suffix:
            ui.label(suffix).style("color: var(--matofx-muted-text); font-size: 0.75rem;")


def _humanize(value: str) -> str:
    """Render a snake_case canonical value as Title Case for display."""
    return value.replace("_", " ").title()


def _target_label(target: str) -> str:
    """Pretty label for an execution target (Local / AOS / HPC)."""
    return _TARGET_LABELS.get(target, _humanize(target))


def _fmt_int(value: Any) -> str:
    """Format an integer KPI, ``—`` when undefined."""
    if value is None:
        return "—"
    return f"{round(float(value)):,}"


def _fmt_pct(value: Any) -> str:
    """Format a percentage KPI to one decimal place, ``—`` when undefined."""
    if value is None:
        return "—"
    return f"{float(value):.1f}%"


def _fmt_num(value: Any) -> str:
    """Format a large number KPI (no decimals), ``—`` when undefined."""
    if value is None:
        return "—"
    return f"{float(value):,.0f}"


def _fmt_date(value: Any) -> str:
    """Format an ISO datetime string as ``YYYY-MM-DD HH:MM``; ``—`` when absent."""
    if not value:
        return "—"
    try:
        moment = _dt.datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return moment.strftime("%Y-%m-%d %H:%M")


def _fmt_hours(hours: float) -> str:
    """Humanise a duration in hours: ``42m`` / ``1h 05m`` / ``12s``."""
    minutes = hours * 60.0
    if minutes < 1.0:
        return f"{hours * 3600.0:.0f}s"
    if minutes < 60.0:
        return f"{minutes:.0f}m"
    whole = int(minutes // 60)
    rem = int(minutes % 60)
    return f"{whole}h {rem:02d}m"
