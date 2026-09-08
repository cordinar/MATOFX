"""Live Jobs tab body for the Usage & Statistics page.

An install-wide list of in-flight jobs / workflow runs / parametric
studies (queued, running, or awaiting approval) with Type / Submitter /
Submitted-date filters and a manual Refresh. Holders of
``live_jobs.cancel`` get a multi-select table + a "Cancel selected"
button that opens a confirmation modal summarising the chosen items
before anything is cancelled.

Rendered by :mod:`matofx.ui.pages.admin_usage` only when the viewer
holds ``live_jobs.view``; the cancel affordances appear only when they
also hold ``live_jobs.cancel`` (the server enforces both regardless).
"""

from __future__ import annotations

from typing import Any, Literal

from nicegui import ui

from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_user_datetime

# Type filter label → wire ``kind``; and the reverse for display.
_TYPE_OPTIONS: dict[str, str] = {
    "Single job": "job",
    "Workflow": "workflow",
    "Parametric study": "study",
    "Optimisation study": "optimisation_study",
}
_KIND_LABELS: dict[str, str] = {
    "job": "Single job",
    "workflow": "Workflow",
    "study": "Parametric study",
    "optimisation_study": "Optimisation study",
}


def _humanize_state(state: str) -> str:
    """Render a snake_case state as plain words (``pending_approval`` → ``Pending approval``)."""
    return state.replace("_", " ").capitalize()


def _date_input(label: str) -> ui.input:
    """A text input backed by a calendar popup; value is ``YYYY-MM-DD``."""
    with ui.input(label).props("dense outlined clearable").style("min-width: 160px;") as date_in:
        with ui.menu().props("no-parent-event") as menu:
            ui.date().bind_value(date_in)
        with date_in.add_slot("append"):
            ui.icon("event").classes("cursor-pointer").on("click", menu.open)
    return date_in


def _row_for(item: dict[str, Any]) -> dict[str, Any]:
    """Map an API live-job item to a table row dict."""
    kind = str(item.get("kind"))
    item_id = int(item.get("id") or 0)
    return {
        "rowkey": f"{kind}:{item_id}",
        "kind": kind,
        "id": item_id,
        "type": _KIND_LABELS.get(kind, kind),
        "name": str(item.get("name") or ""),
        "submitter": str(item.get("submitter_name") or "—"),
        "submitted": format_user_datetime(item.get("submitted_at")),
        "project": str(item.get("project_name") or "—"),
        "scenario": str(item.get("scenario_name") or "—"),
        "state": _humanize_state(str(item.get("state") or "")),
        "cancellable": bool(item.get("cancellable")),
    }


def _columns(*, show_cancellable: bool) -> list[dict[str, Any]]:
    """The Quasar table columns; the cancellable flag is shown only to cancellers."""
    columns: list[dict[str, Any]] = [
        {"name": "type", "label": "Type", "field": "type", "align": "left", "sortable": True},
        {"name": "id", "label": "ID", "field": "id", "align": "right", "sortable": True},
        {"name": "name", "label": "Name", "field": "name", "align": "left", "sortable": True},
        {
            "name": "submitter",
            "label": "Submitter",
            "field": "submitter",
            "align": "left",
            "sortable": True,
        },
        {
            "name": "submitted",
            "label": "Submitted",
            "field": "submitted",
            "align": "left",
            "sortable": True,
        },
        {"name": "project", "label": "Project", "field": "project", "align": "left"},
        {"name": "scenario", "label": "Scenario", "field": "scenario", "align": "left"},
        {"name": "state", "label": "State", "field": "state", "align": "left", "sortable": True},
    ]
    if show_cancellable:
        columns.append(
            {"name": "cancel_ok", "label": "Cancellable", "field": "cancel_ok", "align": "center"}
        )
    return columns


async def render_live_jobs_tab(client: ApiClient, slot: ui.column, *, can_cancel: bool) -> None:
    """Build the Live Jobs tab (filters + table + optional bulk cancel) into ``slot``."""
    state: dict[str, Any] = {"rows": []}
    table_holder: dict[str, ui.table | None] = {"table": None}

    with slot:
        ui.label(
            "Every queued, running, or approval-pending job, workflow run, and parametric "
            "study across the install. Use Refresh to re-check."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

        with ui.card().classes("w-full"), ui.row().classes("w-full items-end gap-3 flex-wrap"):
            type_sel = (
                ui.select(_TYPE_OPTIONS, label="Type", multiple=True)
                .props("dense outlined use-chips clearable")
                .style("min-width: 220px;")
            )
            submitter_sel = (
                ui.select({}, label="Submitter", multiple=True)
                .props("dense outlined use-chips clearable")
                .style("min-width: 220px;")
            )
            from_in = _date_input("Submitted from")
            to_in = _date_input("Submitted to")
            ui.button(
                "Refresh",
                icon="refresh",
                on_click=lambda: _refresh(),  # noqa: PLW0108 - _refresh is defined below
            ).props("color=primary")
            if can_cancel:
                ui.button(
                    "Cancel selected",
                    icon="cancel",
                    on_click=lambda: _confirm(),  # noqa: PLW0108 - _confirm is defined below
                ).props("color=warning")

        count_label = ui.label("").style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        results = ui.column().classes("w-full")

    def _filter_kwargs() -> dict[str, Any]:
        kinds = list(type_sel.value or [])
        submitters = [int(s) for s in (submitter_sel.value or [])]
        since = f"{from_in.value}T00:00:00" if from_in.value else None
        until = f"{to_in.value}T23:59:59" if to_in.value else None
        return {
            "kinds": kinds or None,
            "submitter_ids": submitters or None,
            "since": since,
            "until": until,
        }

    async def _refresh() -> None:
        results.clear()
        with results:
            ui.label("Loading…").style("color: var(--matofx-muted-text);")
        try:
            data = await client.live_jobs_list(**_filter_kwargs())
        except ApiError as exc:
            results.clear()
            with results:
                ui.label(f"Couldn't load live jobs: {exc.detail}").style("color: #ff6b6b;")
            return

        # Keep the submitter dropdown populated with everyone who has an
        # in-flight item (computed server-side before the submitter filter).
        submitter_sel.options = {
            int(s["user_id"]): str(s["name"]) for s in data.get("submitters", [])
        }
        submitter_sel.update()

        rows = [_row_for(i) for i in data.get("items", [])]
        for row in rows:
            row["cancel_ok"] = "Yes" if row["cancellable"] else "No"
        state["rows"] = rows
        count_label.text = f"{len(rows)} in-flight item(s)."

        results.clear()
        with results:
            if not rows:
                ui.label("Nothing is in-flight right now.").style(
                    "color: var(--matofx-muted-text);"
                )
                table_holder["table"] = None
                return
            selection: Literal["single", "multiple"] | None = "multiple" if can_cancel else None
            table = ui.table(
                columns=_columns(show_cancellable=can_cancel),
                rows=rows,
                row_key="rowkey",
                selection=selection,
            ).classes("w-full")
            table_holder["table"] = table

    def _selected_rows() -> list[dict[str, Any]]:
        table = table_holder["table"]
        if table is None:
            return []
        return list(table.selected or [])

    def _confirm() -> None:
        selected = _selected_rows()
        if not selected:
            ui.notify("Pick at least one item to cancel.", type="warning")
            return
        cancellable = [r for r in selected if r["cancellable"]]
        skipped = [r for r in selected if not r["cancellable"]]
        if not cancellable:
            ui.notify(
                "None of the selected items can be cancelled here "
                "(items awaiting approval are resolved on the Approvals page).",
                type="warning",
            )
            return
        _open_confirm_dialog(cancellable, skipped)

    def _open_confirm_dialog(
        cancellable: list[dict[str, Any]], skipped: list[dict[str, Any]]
    ) -> None:
        with ui.dialog() as dialog, ui.card().style("min-width: 720px; max-width: 95vw;"):
            ui.label(f"Cancel {len(cancellable)} item(s)?").style(
                "font-size: 1.1rem; font-weight: 600;"
            )
            ui.label(
                "The runner will stop these on its next pass. Cancelling a workflow run or "
                "study also cancels its in-flight child jobs."
            ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
            summary_cols = _columns(show_cancellable=False)
            ui.table(columns=summary_cols, rows=cancellable, row_key="rowkey").props(
                "dense flat"
            ).classes("w-full")
            if skipped:
                ui.label(
                    f"{len(skipped)} selected item(s) are awaiting approval and will be left "
                    "unchanged — resolve those on the Approvals page."
                ).style("color: #c0392b; font-size: 0.85rem;")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Keep them", on_click=dialog.close).props("flat")
                ui.button(
                    "Confirm cancel",
                    icon="cancel",
                    on_click=lambda: _do_cancel(dialog, cancellable),
                ).props("color=warning")
        dialog.open()

    async def _do_cancel(dialog: ui.dialog, rows: list[dict[str, Any]]) -> None:
        job_ids = [r["id"] for r in rows if r["kind"] == "job"]
        run_ids = [r["id"] for r in rows if r["kind"] == "workflow"]
        study_ids = [r["id"] for r in rows if r["kind"] == "study"]
        try:
            result = await client.live_jobs_cancel(
                job_ids=job_ids, run_ids=run_ids, study_ids=study_ids
            )
        except ApiError as exc:
            ui.notify(f"Cancel failed: {exc.detail}", type="negative")
            return
        dialog.close()
        cancelled = int(result.get("cancelled") or 0)
        failures = [o for o in result.get("outcomes", []) if not o.get("cancelled")]
        if failures:
            ui.notify(
                f"Requested cancel for {cancelled} item(s); "
                f"{len(failures)} could not be cancelled.",
                type="warning",
            )
        else:
            ui.notify(f"Requested cancel for {cancelled} item(s).", type="positive")
        await _refresh()

    await _refresh()
