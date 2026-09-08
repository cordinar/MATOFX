"""Admin → Audit log (``/ui/admin/audit``).

Browser-side view onto the hash-chained, signed audit log. Lets an
admin filter rows by user / actor type / action / resource / outcome /
time range, page through results, expand a row to inspect its payload +
hash chain links, export the filtered view as CSV, and run the chain
verifier on demand.

Permission-gated by ``database.explore`` (same scope as Admin →
Explore Database). The actual integrity check uses the same primitive
as ``matofx audit verify`` on the CLI, so the UI button gives a
read-out an operator can hand off without leaving the browser.

Action and Resource-type filters are multi-selects populated from the
values actually present in the log (``/admin/audit/filters``); the User
filter resolves a typed id / name / username to the matching actor(s)
server-side, so operators never type the internal ``user:<id>`` form.
"""

from __future__ import annotations

import json
from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token

_PAGE_SIZE = 50
_OUTCOME_OPTIONS: tuple[str, ...] = ("Any", "success", "failure", "denied")


@ui.page("/admin/audit")
async def admin_audit_page() -> None:
    """Render the audit log browser."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())

    # Fetch the dropdown option lists up front so the Action / Resource
    # type multi-selects only offer values that exist. A failure here just
    # yields empty option lists — the rest of the page still works.
    actions_opts: list[str] = []
    rtype_opts: list[str] = []
    actor_type_opts: list[str] = ["Any", "user", "service", "system", "anonymous"]
    try:
        filters = await client.audit_log_filters()
        actions_opts = list(filters.get("actions") or [])
        rtype_opts = list(filters.get("resource_types") or [])
        present = list(filters.get("actor_types") or [])
        if present:
            actor_type_opts = ["Any", *present]
    except ApiError:
        pass

    # Mutable view state — kept on a single dict so the inner closures
    # can read and mutate without juggling ``nonlocal`` declarations.
    state: dict[str, Any] = {
        "page": 1,
        "user": "",
        "actor_type": "Any",
        "actions": [],
        "resource_types": [],
        "resource_id": "",
        "outcome": "Any",
        "correlation_id": "",
        "since": "",
        "until": "",
    }

    with ui.column().classes("w-full mx-auto p-6 gap-4").style("max-width: 1700px;"):
        ui.label("Audit log").style("font-size: 1.5rem; font-weight: 600;")
        ui.label(
            "Hash-chained, signed record of every audit-worthy action on this "
            "install. Newest entries first. Click a row to expand it and see "
            "the payload, prior-hash link, and signature."
        ).style("color: var(--matofx-muted-text);")

        filter_panel = ui.column().classes("w-full gap-2")
        results_panel = ui.column().classes("w-full gap-2")
        verify_panel = ui.column().classes("w-full gap-2")

    async def _refresh() -> None:
        await _render_results(client, state, results_panel)

    with filter_panel, ui.card().classes("w-full"):
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            user_in = (
                ui.input(label="User", placeholder="id, name or username")
                .props("dense outlined clearable")
                .style("min-width: 200px;")
            )
            actor_type_sel = (
                ui.select(actor_type_opts, value="Any", label="Actor type")
                .props("dense outlined")
                .style("min-width: 150px;")
            )
            action_sel = (
                ui.select(
                    actions_opts, value=[], label="Action", multiple=True, clearable=True
                )
                .props("dense outlined use-chips")
                .style("min-width: 240px;")
            )
            rtype_sel = (
                ui.select(
                    rtype_opts, value=[], label="Resource type", multiple=True, clearable=True
                )
                .props("dense outlined use-chips")
                .style("min-width: 200px;")
            )
            rid_in = (
                ui.input(label="Resource id", placeholder="42")
                .props("dense outlined clearable")
                .style("min-width: 140px;")
            )
            outcome_sel = (
                ui.select(list(_OUTCOME_OPTIONS), value="Any", label="Outcome")
                .props("dense outlined")
                .style("min-width: 140px;")
            )
            corr_in = (
                ui.input(label="Correlation id")
                .props("dense outlined clearable")
                .style("min-width: 160px;")
            )
            since_in = _date_input("Since")
            until_in = _date_input("Until")

        async def _apply() -> None:
            state["user"] = (user_in.value or "").strip()
            state["actor_type"] = actor_type_sel.value or "Any"
            state["actions"] = list(action_sel.value or [])
            state["resource_types"] = list(rtype_sel.value or [])
            state["resource_id"] = (rid_in.value or "").strip()
            state["outcome"] = outcome_sel.value or "Any"
            state["correlation_id"] = (corr_in.value or "").strip()
            state["since"] = (since_in.value or "").strip()
            state["until"] = (until_in.value or "").strip()
            state["page"] = 1
            await _refresh()

        async def _clear() -> None:
            user_in.value = ""
            rid_in.value = ""
            corr_in.value = ""
            since_in.value = ""
            until_in.value = ""
            action_sel.value = []
            rtype_sel.value = []
            actor_type_sel.value = "Any"
            outcome_sel.value = "Any"
            await _apply()

        async def _export() -> None:
            try:
                data = await client.export_audit_log(**_request_kwargs(state))
            except ApiError as exc:
                ui.notify(f"Export failed: {exc.detail}", type="negative")
                return
            ui.download(data, "audit-log.csv", "text/csv")

        with ui.row().classes("gap-2"):
            ui.button("Apply", icon="filter_alt", on_click=_apply).props("color=primary")
            ui.button("Clear", icon="clear", on_click=_clear).props("flat")
            ui.element("div").style("flex: 1;")
            ui.button("Export CSV", icon="download", on_click=_export).props("outline").tooltip(
                "Download the current filtered view (up to 50,000 rows) as CSV."
            )

    with verify_panel, ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center gap-3"):
            ui.label("Chain integrity").style("font-weight: 600;")
            ui.label(
                "Recompute hashes, check chain links, and validate Ed25519 "
                "signatures (when a signing key is configured)."
            ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
            ui.element("div").style("flex: 1;")
            verify_result = ui.label("").style("font-family: monospace;")

            async def _verify() -> None:
                verify_result.text = "Verifying…"
                verify_result.style("color: var(--matofx-muted-text);")
                try:
                    data = await client.verify_audit_chain()
                except ApiError as exc:
                    verify_result.text = f"Verify failed: {exc.detail}"
                    verify_result.style("color: #ff6b6b;")
                    return
                ok = bool(data.get("ok"))
                checked = int(data.get("records_checked") or 0)
                signed = bool(data.get("signing_key_configured"))
                sig_note = "signatures checked" if signed else "signatures skipped (no key)"
                if ok:
                    verify_result.text = f"OK — {checked} record(s), {sig_note}."
                    verify_result.style("color: #21ba45;")
                    return
                failures = data.get("failures") or []
                first = failures[0] if failures else {}
                first_text = (
                    f" first: record {first.get('record_id')}: {first.get('reason')}"
                    if first
                    else ""
                )
                verify_result.text = (
                    f"FAILED — {checked} checked, {len(failures)} failure(s).{first_text}"
                )
                verify_result.style("color: #ff6b6b;")

            ui.button("Verify chain", icon="verified", on_click=_verify).props("color=primary")

    await _refresh()


def _date_input(label: str) -> ui.input:
    """A text input backed by a calendar popup; value is ``YYYY-MM-DD``.

    Editable so a power user can still paste a full ISO-8601 timestamp;
    :func:`_request_kwargs` widens a bare date to a full-day bound.
    """
    with ui.input(label).props("dense outlined clearable").style("min-width: 160px;") as date_in:
        with ui.menu().props("no-parent-event") as menu:
            ui.date().bind_value(date_in)
        with date_in.add_slot("append"):
            ui.icon("event").classes("cursor-pointer").on("click", menu.open)
    return date_in


def _iso_bound(value: str, *, end: bool) -> str | None:
    """Normalise a Since/Until field to an ISO-8601 string, or ``None``.

    A bare ``YYYY-MM-DD`` from the date picker is widened to the start
    (``T00:00:00``) or end (``T23:59:59``) of that day so an ``Until`` of
    a date is inclusive. A value the user typed with a time is left as-is.
    """
    value = (value or "").strip()
    if not value:
        return None
    if "T" not in value and " " not in value:
        return f"{value}T23:59:59" if end else f"{value}T00:00:00"
    return value


def _request_kwargs(state: dict[str, Any]) -> dict[str, Any]:
    """The filter kwargs shared by the list + export client calls."""
    return {
        "user": state["user"] or None,
        "actor_type": (
            state["actor_type"] if state["actor_type"] and state["actor_type"] != "Any" else None
        ),
        "action": list(state["actions"]) or None,
        "resource_type": list(state["resource_types"]) or None,
        "resource_id": state["resource_id"] or None,
        "outcome": state["outcome"] if state["outcome"] and state["outcome"] != "Any" else None,
        "correlation_id": state["correlation_id"] or None,
        "since": _iso_bound(state["since"], end=False),
        "until": _iso_bound(state["until"], end=True),
    }


async def _render_results(
    client: ApiClient,
    state: dict[str, Any],
    slot: ui.column,
) -> None:
    """Fetch + render the table for the current filter / page state."""
    slot.clear()
    with slot:
        ui.label("Loading…").style("color: var(--matofx-muted-text);")

    try:
        data = await client.list_audit_log(
            page=int(state["page"]),
            page_size=_PAGE_SIZE,
            **_request_kwargs(state),
        )
    except ApiError as exc:
        slot.clear()
        with slot:
            ui.label(f"Couldn't load audit log: {exc.detail}").style("color: #ff6b6b;")
        return

    rows = list(data.get("rows") or [])
    total = int(data.get("total_rows") or 0)
    page = int(data.get("page") or 1)
    page_size = int(data.get("page_size") or _PAGE_SIZE)
    last_page = max(1, (total + page_size - 1) // page_size) if total else 1

    slot.clear()
    with slot:
        with ui.row().classes("w-full items-center gap-2"):
            ui.label(
                f"{total:,} record(s)"
                + (
                    f" · showing {((page - 1) * page_size) + 1}-{min(total, page * page_size)}"
                    if total
                    else ""
                )
            ).style("color: var(--matofx-muted-text);")
            ui.element("div").style("flex: 1;")

            async def _prev() -> None:
                if state["page"] > 1:
                    state["page"] = int(state["page"]) - 1
                    await _render_results(client, state, slot)

            async def _next() -> None:
                if page < last_page:
                    state["page"] = int(state["page"]) + 1
                    await _render_results(client, state, slot)

            ui.button(icon="chevron_left", on_click=_prev).props("flat dense")
            ui.label(f"Page {page} / {last_page}").style(
                "color: var(--matofx-muted-text); font-size: 0.85rem;"
            )
            ui.button(icon="chevron_right", on_click=_next).props("flat dense")

        if not rows:
            ui.label("No audit rows match these filters.").style("color: var(--matofx-muted-text);")
            return

        for row in rows:
            _render_row(row)


def _render_row(row: dict[str, Any]) -> None:
    """One expandable card per audit record."""
    outcome = str(row.get("outcome") or "")
    outcome_color = {
        "success": "#21ba45",
        "failure": "#ff6b6b",
        "denied": "#f2c037",
    }.get(outcome, "var(--matofx-muted-text)")

    actor_label = row.get("actor_label") or row.get("actor") or "—"
    header = (
        f"#{int(row.get('id') or 0):>06d}  "
        f"{row.get('timestamp') or ''}  ·  "
        f"{row.get('action') or '—'}  ·  "
        f"{actor_label}"
    )
    with ui.expansion(header).classes("w-full").props('header-class="q-px-sm" dense'):
        with ui.column().classes("w-full gap-1"):
            with ui.row().classes("w-full gap-4 flex-wrap"):
                _field("Outcome", outcome, color=outcome_color)
                _field("User", actor_label)
                _field("Actor type", row.get("actor_type"))
                _field("Resource", f"{row.get('resource_type')}:{row.get('resource_id')}")
                _field("Request id", row.get("request_id") or "—")
                _field("Session id", row.get("session_id") or "—")
                _field("Source IP", row.get("source_ip") or "—")
                _field("Correlation id", row.get("correlation_id") or "—")
                _field("Schema", str(row.get("schema_version") or ""))
            # The raw actor token is still worth showing for non-user actors
            # and for anyone cross-referencing the CLI / raw table.
            if row.get("actor") and row.get("actor") != actor_label:
                _field("Actor token", row.get("actor"))
            if row.get("reason"):
                _field("Reason", row.get("reason"))
            ui.separator()
            with ui.column().classes("w-full gap-1"):
                ui.label("Hash chain").style("font-weight: 600;")
                _mono("hash", row.get("hash"))
                _mono("prior_hash", row.get("prior_hash"))
                _mono("signature", row.get("signature") or "(unsigned)")
                _mono("key_id", row.get("key_id") or "(none)")
            ui.separator()
            ui.label("Payload").style("font-weight: 600;")
            ui.code(json.dumps(row.get("payload") or {}, indent=2, sort_keys=True)).classes(
                "w-full"
            )


def _field(label: str, value: Any, *, color: str | None = None) -> None:
    with ui.column().classes("gap-0").style("min-width: 160px;"):
        ui.label(label).style("color: var(--matofx-muted-text); font-size: 0.75rem;")
        ui.label(str(value or "—")).style(
            f"font-size: 0.9rem;{f'color: {color};' if color else ''}"
        )


def _mono(label: str, value: Any) -> None:
    with ui.row().classes("w-full items-center gap-2"):
        ui.label(label).style(
            "color: var(--matofx-muted-text); font-size: 0.8rem; "
            "min-width: 110px; font-family: monospace;"
        )
        ui.label(str(value or "")).style(
            "font-family: monospace; font-size: 0.8rem; word-break: break-all;"
        )
