"""V&V records workspace (ADR-0011 §8 / designs/vv-tracking.md §8).

The workspace was reworked from an id-lookup form into a browsable,
label-driven register (2026-07), styled to the home-shell idiom (theme
CSS variables, ``section_card`` blocks, no-caps tabs, row-bg rows).
Surfaces, all behind the ``features.vv_tracking`` flag (nav entry + these
pages are absent when it's off, matching the API which 404s every route —
a disabled-feature visit redirects home):

- ``GET /vv-records`` — the V&V **register**: one filterable list across
  both tool and job records (Type / State / Kind / search facets), every
  id resolved to a human label. Curators get a "Review queue" tab (the
  records they can decide); ``?tab=queue`` deep-links straight to it. One
  "New record" flow picks its target by name and version, never by id.
- ``GET /vv-records/tool/{id}`` and ``GET /vv-records/job/{id}`` — one
  record's detail + lifecycle actions (submit / approve / reject /
  retire), reached from the register, the tool/job pages, or a deep link.

Reads are open to any authenticated user; create / edit / submit / decide
/ retire require ``vv.curate`` (or base-tier ``vv.create`` to author a
draft) — the UI hides curator controls when the caller lacks it, and the
service stays the authoritative gate.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui._features import vv_tracking_enabled
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_job_id, format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in, section_card
from matofx.ui.state import current_token

_VV_CURATE = "vv.curate"
_VV_CREATE = "vv.create"
_PAGE_SIZE = 25

_STATE_LABEL: dict[str, str] = {
    "draft": "Draft",
    "pending": "Pending review",
    "approved": "Approved",
    "rejected": "Rejected",
    "retired": "Retired",
}
_STATE_COLOR: dict[str, str] = {
    "draft": "grey",
    "pending": "orange",
    "approved": "green",
    "rejected": "red",
    "retired": "blue-grey",
}

# Closed vocabularies — mirror matofx.vv.orm. Kept inline so the UI
# bundle doesn't pull the service module just for a few tuples.
_TOOL_VV_KINDS: tuple[str, ...] = ("verification", "validation")
_JOB_VV_KINDS: tuple[str, ...] = ("validated_against", "calibrated_from", "sanity_checked")

_TOOL_KIND_LABEL: dict[str, str] = {
    "verification": "Verification",
    "validation": "Validation",
}
_JOB_KIND_LABEL: dict[str, str] = {
    "validated_against": "Validated against",
    "calibrated_from": "Calibrated from",
    "sanity_checked": "Sanity checked",
}
# Combined kind → label for the register's Kind filter (both vocabularies).
_ALL_KIND_LABEL: dict[str, str] = {**_TOOL_KIND_LABEL, **_JOB_KIND_LABEL}


def _client() -> ApiClient:
    return ApiClient(get_app(), token=current_token())


def _feature_ok() -> bool:
    """Redirect home when the V&V feature is off (page *absent*, design §8)."""
    if not vv_tracking_enabled():
        ui.navigate.to("/home")
        return False
    return True


def _state_chip(state: str) -> None:
    label = _STATE_LABEL.get(state, state.title())
    color = _STATE_COLOR.get(state, "grey")
    ui.chip(label).props(f"color={color} text-color=white dense")


def _kind_label(record_kind: str, vv_kind: str) -> str:
    table = _TOOL_KIND_LABEL if record_kind == "tool" else _JOB_KIND_LABEL
    return table.get(vv_kind, vv_kind.replace("_", " ").title())


def _subject_label(entry: dict[str, Any]) -> str:
    """Human subject for a register row — a tool version or a formatted job."""
    label = str(entry.get("target_label") or "")
    if entry.get("record_kind") == "job":
        jid = format_job_id(int(entry.get("target_id") or 0))
        return f"{jid} — {label}" if label else jid
    return label or f"tool version #{entry.get('target_id')}"


def _parse_list(text: str) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def _collect_range(
    controls: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, float]], str | None]:
    """Assemble the applicable-range payload from the live editor controls.

    Returns ``(range_by_factor_name, error)``. A factor is included only
    when at least one bound is set; a factor whose min exceeds its max
    yields an error (with the label, per the labels-not-names rule) so the
    submit aborts. Empty when nothing was entered — the caller treats that
    as "no envelope".
    """
    out: dict[str, dict[str, float]] = {}
    for c in controls:
        lo_raw = c["min"].value
        hi_raw = c["max"].value
        bounds: dict[str, float] = {}
        if lo_raw is not None and lo_raw != "":
            bounds["min"] = float(lo_raw)
        if hi_raw is not None and hi_raw != "":
            bounds["max"] = float(hi_raw)
        if not bounds:
            continue
        if "min" in bounds and "max" in bounds and bounds["min"] > bounds["max"]:
            return {}, f"For “{c['label']}”, the minimum must be ≤ the maximum."
        out[str(c["name"])] = bounds
    return out, None


# ===================== workspace (register) =========================


@ui.page("/vv-records")
async def vv_records_page(tab: str = "") -> None:
    """The V&V register — one browsable list + a curator review queue.

    ``tab=queue`` opens the Review queue directly (the sidebar's "V&V
    records inbox" entry deep-links here); it falls back to the register
    for a caller who isn't a curator.
    """
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    try:
        me = await client.me()
    except ApiError:
        me = {}
    perms = me.get("permissions") or []
    is_curator = _VV_CURATE in perms
    is_author = is_curator or _VV_CREATE in perms

    with ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;"):
        with ui.column().classes("gap-0"):
            ui.label("V&V records").style(
                "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
            )
            ui.label(
                "Verification & validation claims about tool versions and job runs — "
                "curated, evidence-backed, and inherited across identical tool bundles."
            ).style("color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;")

        # Tabs on the left, the create action on the right of the same row —
        # kept clear of the shell's top-right theme / help controls.
        with (
            ui.row()
            .classes("w-full items-end justify-between no-wrap")
            .style("border-bottom: 1px solid var(--matofx-card-border);")
        ):
            with (
                ui.tabs().props(
                    "align=left active-color=primary indicator-color=primary"
                ) as tabs
            ):
                register_tab = ui.tab("All records", icon="fact_check").props("no-caps")
                queue_tab = (
                    ui.tab("Review queue", icon="inbox").props("no-caps") if is_curator else None
                )
            with ui.row().classes("items-end gap-2").style("margin-bottom: 0.35rem;"):
                if is_curator:
                    ui.button(
                        "Migrate to new version",
                        icon="move_up",
                        on_click=lambda: open_vv_migrate_dialog(client),
                    ).props("flat color=primary no-caps").tooltip(
                        "Carry approved V&V records forward from one tool version to another"
                    )
                if is_author:
                    ui.button(
                        "New record",
                        icon="add",
                        on_click=lambda: open_vv_create_dialog(client),
                    ).props("color=primary no-caps unelevated")

        start = queue_tab if (tab == "queue" and queue_tab is not None) else register_tab
        with (
            ui.tab_panels(tabs, value=start)
            .classes("w-full")
            .style("background-color: transparent;")
        ):
            with ui.tab_panel(register_tab).style("background-color: transparent;"):
                await _render_register(client)
            if queue_tab is not None:
                with ui.tab_panel(queue_tab).style("background-color: transparent;"):
                    await _render_review_queue(client)


# ------------------------- register list -----------------------------


async def _render_register(client: ApiClient) -> None:
    """Filter bar + a filterable, paginated list of every V&V record."""
    state: dict[str, Any] = {
        "page": 1,
        "record_kind": "all",
        "states": [],
        "kinds": [],
        "search": "",
    }
    # ``results`` is a mutable handle so the filter closures can reference it
    # before it's created — the column itself is built *after* the filter card
    # below, so the Filter renders at the top of the page (design §8).
    results_ref: dict[str, ui.column] = {}

    async def _refresh() -> None:
        await _render_register_results(client, state, results_ref["col"])

    with section_card("filter_alt", "Filter"):
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            type_sel = (
                ui.select(
                    {"all": "All types", "tool": "Tool versions", "job": "Job runs"},
                    value="all",
                    label="Type",
                )
                .props("dense outlined")
                .style("min-width: 150px;")
            )
            state_sel = (
                ui.select(_STATE_LABEL, value=[], label="State", multiple=True, clearable=True)
                .props("dense outlined use-chips")
                .style("min-width: 230px;")
            )
            kind_sel = (
                ui.select(_ALL_KIND_LABEL, value=[], label="Kind", multiple=True, clearable=True)
                .props("dense outlined use-chips")
                .style("min-width: 230px;")
            )
            search_in = (
                ui.input(label="Search", placeholder="tool, job, author, notes…")
                .props("dense outlined clearable")
                .style("min-width: 230px;")
            )

            async def _apply() -> None:
                state["record_kind"] = type_sel.value or "all"
                state["states"] = list(state_sel.value or [])
                state["kinds"] = list(kind_sel.value or [])
                state["search"] = (search_in.value or "").strip()
                state["page"] = 1
                await _refresh()

            async def _clear() -> None:
                type_sel.value = "all"
                state_sel.value = []
                kind_sel.value = []
                search_in.value = ""
                await _apply()

            with ui.row().classes("gap-2"):
                ui.button("Apply", icon="filter_alt", on_click=_apply).props(
                    "color=primary no-caps unelevated"
                )
                ui.button("Clear", icon="clear", on_click=_clear).props("flat no-caps")

    # Built after the filter card so the records list sits below it.
    results_ref["col"] = ui.column().classes("w-full gap-3").style("margin-top: 0.75rem;")

    await _refresh()


async def _render_register_results(
    client: ApiClient,
    state: dict[str, Any],
    slot: ui.column,
) -> None:
    slot.clear()
    with slot:
        ui.label("Loading…").style("color: var(--matofx-muted-text);")
    try:
        data = await client.vv_list_records(
            record_kind=state["record_kind"],
            state=list(state["states"]) or None,
            kind=list(state["kinds"]) or None,
            search=state["search"] or None,
            include_retired=bool(state["states"]),  # explicit states may include retired
            page=int(state["page"]),
            page_size=_PAGE_SIZE,
        )
    except ApiError as exc:
        slot.clear()
        with slot:
            ui.label(f"Couldn't load V&V records: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
        return

    rows = list(data.get("rows") or [])
    total = int(data.get("total") or 0)
    page = int(data.get("page") or 1)
    page_size = int(data.get("page_size") or _PAGE_SIZE)
    last_page = max(1, (total + page_size - 1) // page_size) if total else 1

    slot.clear()
    with slot, section_card("fact_check", f"{total:,} record" + ("" if total == 1 else "s")):
        with ui.row().classes("w-full items-center gap-2"):
            ui.element("div").style("flex: 1;")

            async def _prev() -> None:
                if state["page"] > 1:
                    state["page"] = int(state["page"]) - 1
                    await _render_register_results(client, state, slot)

            async def _next() -> None:
                if page < last_page:
                    state["page"] = int(state["page"]) + 1
                    await _render_register_results(client, state, slot)

            ui.button(icon="chevron_left", on_click=_prev).props("flat dense round")
            ui.label(f"Page {page} / {last_page}").style(
                "color: var(--matofx-muted-text); font-size: 0.85rem;"
            )
            ui.button(icon="chevron_right", on_click=_next).props("flat dense round")

        if not rows:
            ui.label("No V&V records match these filters.").style(
                "color: var(--matofx-muted-text);"
            )
            return
        for entry in rows:
            _render_register_row(entry)


def _render_register_row(entry: dict[str, Any]) -> None:
    """One clickable register row — labels only, deep-links to the detail page."""
    record_kind = str(entry.get("record_kind") or "tool")
    rid = int(entry.get("record_id") or 0)
    href = f"/vv-records/{record_kind}/{rid}"
    with (
        ui.row()
        .classes("w-full items-center gap-3 p-2 rounded cursor-pointer no-wrap")
        .style("background-color: var(--matofx-row-bg);")
        .on("click", lambda _e=None, _h=href: ui.navigate.to(_h))
    ):
        ui.badge("Tool" if record_kind == "tool" else "Job").props(
            f"color={'indigo' if record_kind == 'tool' else 'teal'}"
        )
        with ui.column().classes("gap-0 flex-grow").style("min-width: 0;"):
            ui.label(_subject_label(entry)).style("font-weight: 600;")
            ui.label(str(entry.get("vv_kind_label") or "")).style(
                "color: var(--matofx-muted-text); font-size: 0.8rem;"
            )
        with ui.column().classes("gap-0 items-end").style("min-width: 190px;"):
            ui.label(str(entry.get("recorded_by_label") or "")).style("font-size: 0.8rem;")
            ui.label(format_user_datetime(str(entry.get("recorded_at") or ""))).style(
                "color: var(--matofx-muted-text); font-size: 0.75rem;"
            )
        evidence = str(entry.get("evidence_label") or "").strip()
        if evidence:
            ui.icon("attach_file").style("color: var(--matofx-muted-text);").tooltip(
                f"Evidence: {evidence}"
            )
        _state_chip(str(entry.get("state") or "draft"))


# ------------------------- review queue ------------------------------


async def _render_review_queue(client: ApiClient) -> None:
    """Pending records the curator can actually decide (labelled).

    Combines two reads: ``/vv/pending`` gives the records *this* curator
    can act on (excludes self-authored — the directed-approver gate
    forbids self-approval), and ``/vv/records?state=pending`` supplies the
    resolved labels. We intersect on ``record_id`` so the queue reads in
    names, not ids.
    """
    box = ui.column().classes("w-full gap-3").style("margin-top: 0.75rem;")
    with box, section_card("inbox", "Awaiting your review"):
        try:
            pending = await client.vv_list_pending()
            labelled = await client.vv_list_records(state=["pending"], page_size=200)
        except ApiError as exc:
            ui.label(f"Couldn't load the review queue: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return

        actionable = {(str(p.get("record_kind")), int(p.get("record_id") or 0)) for p in pending}
        rows = [
            e
            for e in (labelled.get("rows") or [])
            if (str(e.get("record_kind")), int(e.get("record_id") or 0)) in actionable
        ]
        if not rows:
            ui.label("Nothing to review — you're all caught up.").style(
                "color: var(--matofx-muted-text);"
            )
            return
        ui.label(
            "Records you authored are excluded — the directed-approver gate forbids "
            "self-approval."
        ).style("color: var(--matofx-muted-text); font-size: 0.82rem;")
        for entry in rows:
            _render_register_row(entry)


# ============================= create ================================


def open_vv_create_dialog(
    client: ApiClient,
    *,
    prefill_tool_version_id: int | None = None,
    prefill_tool_label: str | None = None,
    prefill_job_id: int | None = None,
    prefill_job_label: str | None = None,
) -> None:
    """One authoring dialog for both record kinds, target picked by name.

    Reused by the register's "New record" button and by the pre-filled
    "Add V&V record" affordances on the tool-version and job pages. When a
    ``prefill_*`` target is supplied the picker is skipped and the target
    is fixed; otherwise the author chooses a tool (→ a specific version) or
    a job (→ one of that tool's runs) by name. Evidence is optional: pick a
    library file, paste a URL, or upload a file (which lands in the global
    File Library and may require curator approval).
    """
    fixed_tool = prefill_tool_version_id is not None
    fixed_job = prefill_job_id is not None
    sel: dict[str, Any] = {
        "record_kind": "job" if fixed_job else "tool",
        "tool_version_id": prefill_tool_version_id,
        "job_id": prefill_job_id,
        # Each entry is a live "evidence row" controller (see _add_evidence_row).
        "evidence_rows": [],
        # Live per-factor range controllers, rebuilt whenever the target
        # version or kind changes (see _refresh_range_editor below).
        "range_controls": [],
    }

    with ui.dialog() as dialog, ui.card().style("min-width: 580px; max-width: 94vw;"):
        ui.label("New V&V record").style("font-size: 1.15rem; font-weight: 650;")

        # ---- target ----
        if fixed_tool:
            ui.label(f"Tool version: {prefill_tool_label or prefill_tool_version_id}").style(
                "font-weight: 500;"
            )
        elif fixed_job:
            ui.label(f"Job: {prefill_job_label or format_job_id(int(prefill_job_id or 0))}").style(
                "font-weight: 500;"
            )
        else:
            with ui.column().classes("w-full gap-1").style("margin-bottom: 0.5rem;"):
                ui.label("Record about").style(
                    "color: var(--matofx-muted-text); font-size: 0.8rem;"
                )
                type_toggle = ui.toggle(
                    {"tool": "Tool version", "job": "Job run"}, value="tool"
                ).props("no-caps")
            target_box = ui.column().classes("w-full gap-2")

        kind_sel = ui.select({}, label="Kind").props("dense outlined").classes("w-full")
        notes_in = ui.textarea("Notes").props("outlined").classes("w-full")
        standards_in = (
            ui.input("Standards cited (comma-separated)").props("dense outlined").classes("w-full")
        )
        confidence_in = (
            ui.number("Confidence band (± value, optional)")
            .props("dense outlined")
            .classes("w-full")
        )

        # ---- applicable range (validation-of-a-tool only) ----
        # The input envelope the validation covers. Shown only for a tool
        # *validation* claim; drives the submit-time envelope soft-warning.
        range_expansion = ui.expansion(
            "Applicable range (optional)", icon="straighten"
        ).classes("w-full")
        with range_expansion:
            ui.label(
                "The input envelope this validation covers. Running the tool with "
                "values outside this range later shows a soft, non-blocking warning. "
                "Leave a bound blank for open-ended, or leave the whole section empty "
                "if it doesn't apply."
            ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
            range_box = ui.column().classes("w-full gap-2").style("margin-top: 0.4rem;")
        range_expansion.visible = False

        # ---- evidence (optional, repeatable) ----
        with ui.expansion("Evidence (optional)", icon="attach_file").classes("w-full"):
            ui.label(
                "Attach one or more files or links backing this claim — add as many "
                "as you need, of any mix of types."
            ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
            evidence_list = ui.column().classes("w-full gap-2").style("margin-top: 0.4rem;")
            ui.button(
                "Add evidence",
                icon="add",
                on_click=lambda: _add_evidence_row(client, evidence_list, sel["evidence_rows"]),
            ).props("flat dense no-caps color=primary").style("margin-top: 0.3rem;")

        message = ui.label("").style("color: var(--matofx-error-text);")

        def _apply_kind_options() -> None:
            if sel["record_kind"] == "tool":
                kind_sel.options = dict(_TOOL_KIND_LABEL)
                kind_sel.value = "verification"
                standards_in.visible = True
                confidence_in.visible = True
            else:
                kind_sel.options = dict(_JOB_KIND_LABEL)
                kind_sel.value = "validated_against"
                standards_in.visible = False
                confidence_in.visible = False
            kind_sel.update()

        async def _refresh_range_editor() -> None:
            """(Re)build the per-factor range editor for the current target/kind.

            Visible only for a tool *validation* claim. Loads the selected
            tool version's numeric inputs (by label) and shows a Min/Max
            pair per factor; the controllers are stashed on
            ``sel['range_controls']`` for :func:`_collect_range` at submit.
            """
            is_validation = sel["record_kind"] == "tool" and kind_sel.value == "validation"
            range_expansion.visible = bool(is_validation)
            range_box.clear()
            sel["range_controls"] = []
            if not is_validation:
                return
            tv_id = sel.get("tool_version_id")
            if not tv_id:
                with range_box:
                    ui.label("Pick a tool version first.").style(
                        "color: var(--matofx-muted-text); font-size: 0.8rem;"
                    )
                return
            try:
                factors = await client.vv_numeric_factors(int(tv_id))
            except ApiError as exc:
                with range_box:
                    ui.label(f"Couldn't load factors: {exc.detail}").style(
                        "color: var(--matofx-error-text);"
                    )
                return
            if not factors:
                with range_box:
                    ui.label("This tool version has no numeric inputs to bound.").style(
                        "color: var(--matofx-muted-text); font-size: 0.8rem;"
                    )
                return
            with range_box:
                for f in factors:
                    name = str(f.get("name") or "")
                    label = str(f.get("label") or name)
                    unit = f.get("unit")
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        ui.label(f"{label}{f' ({unit})' if unit else ''}").style(
                            "flex: 1 1 0; min-width: 0; font-size: 0.85rem;"
                        )
                        min_in = ui.number("Min").props("dense outlined").style("width: 120px;")
                        max_in = ui.number("Max").props("dense outlined").style("width: 120px;")
                    sel["range_controls"].append(
                        {"name": name, "label": label, "min": min_in, "max": max_in}
                    )

        async def _build_target_picker() -> None:
            target_box.clear()
            with target_box:
                try:
                    tools = await client.list_tools()
                except ApiError as exc:
                    ui.label(f"Couldn't load tools: {exc.detail}").style(
                        "color: var(--matofx-error-text);"
                    )
                    return
                tool_opts = {
                    str(t.get("alias")): str(t.get("name") or t.get("alias")) for t in tools
                }
                tool_sel = (
                    ui.select(tool_opts, label="Tool", with_input=True)
                    .props("dense outlined")
                    .classes("w-full")
                )
                version_sel = (
                    ui.select({}, label="Version").props("dense outlined").classes("w-full")
                )
                version_sel.visible = sel["record_kind"] == "tool"
                job_sel = (
                    ui.select({}, label="Job run", with_input=True)
                    .props("dense outlined")
                    .classes("w-full")
                )
                job_sel.visible = sel["record_kind"] == "job"
                target_note = ui.label("").style(
                    "color: var(--matofx-muted-text); font-size: 0.8rem;"
                )

                async def _on_tool(_e: Any = None) -> None:
                    alias = tool_sel.value
                    if not alias:
                        return
                    if sel["record_kind"] == "tool":
                        try:
                            versions = await client.list_tool_version_options(alias)
                        except ApiError as exc:
                            target_note.text = f"Couldn't load versions: {exc.detail}"
                            return
                        version_sel.options = {
                            int(v["tool_version_id"]): (
                                f"v{v['version']}" + (" (current)" if v.get("is_current") else "")
                            )
                            for v in versions
                        }
                        current = next(
                            (int(v["tool_version_id"]) for v in versions if v.get("is_current")),
                            (int(versions[0]["tool_version_id"]) if versions else None),
                        )
                        version_sel.value = current
                        sel["tool_version_id"] = current
                        version_sel.update()
                        target_note.text = (
                            "" if versions else "This tool has no versions to record against yet."
                        )
                        await _refresh_range_editor()
                    else:
                        try:
                            jobs = await client.search_jobs_by_inputs(alias, filters={})
                        except ApiError as exc:
                            target_note.text = f"Couldn't load jobs: {exc.detail}"
                            return
                        job_sel.options = {
                            int(j["id"]): f"{format_job_id(int(j['id']))} — {j.get('name') or ''}"
                            for j in jobs
                        }
                        job_sel.update()
                        target_note.text = (
                            f"{len(jobs)} completed run(s) for this tool."
                            if jobs
                            else "This tool has no completed runs to record against."
                        )

                async def _on_version(_e: Any = None) -> None:
                    sel["tool_version_id"] = int(version_sel.value) if version_sel.value else None
                    await _refresh_range_editor()

                def _on_job(_e: Any = None) -> None:
                    sel["job_id"] = int(job_sel.value) if job_sel.value else None

                tool_sel.on("update:model-value", _on_tool)
                version_sel.on("update:model-value", _on_version)
                job_sel.on("update:model-value", _on_job)

        async def _submit() -> None:
            message.text = ""
            if not fixed_tool and not fixed_job:
                sel["record_kind"] = type_toggle.value or "tool"
            if sel["record_kind"] == "tool" and not sel.get("tool_version_id"):
                message.text = "Pick a tool and version."
                return
            if sel["record_kind"] == "job" and not sel.get("job_id"):
                message.text = "Pick a job run."
                return

            applicable_range, range_err = _collect_range(sel["range_controls"])
            if range_err:
                message.text = range_err
                return

            evidence = await _collect_evidence(client, sel["evidence_rows"], message)
            if evidence is None:  # an upload failed; message already set
                return

            body: dict[str, Any] = {
                "kind": kind_sel.value,
                "notes": (notes_in.value or "").strip() or None,
                "evidence": evidence or None,
            }
            try:
                if sel["record_kind"] == "tool":
                    body["tool_version_id"] = int(sel["tool_version_id"])
                    body["standards_cited"] = _parse_list(standards_in.value or "") or None
                    body["confidence_band"] = (
                        float(confidence_in.value) if confidence_in.value is not None else None
                    )
                    body["applicable_range"] = applicable_range or None
                    created = await client.vv_create_tool_record(body)
                    path = f"/vv-records/tool/{int(created['id'])}"
                else:
                    body["job_id"] = int(sel["job_id"])
                    created = await client.vv_create_job_record(body)
                    path = f"/vv-records/job/{int(created['id'])}"
            except ApiError as exc:
                message.text = exc.detail or "Couldn't create the record."
                return
            dialog.close()
            ui.notify("Draft created.", type="positive")
            ui.navigate.to(path)

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button("Create draft", icon="save", on_click=_submit).props(
                "color=primary no-caps unelevated"
            )

    async def _on_kind(_e: Any = None) -> None:
        await _refresh_range_editor()

    kind_sel.on("update:model-value", _on_kind)

    _apply_kind_options()
    if not fixed_tool and not fixed_job:

        async def _on_type(_e: Any = None) -> None:
            sel["record_kind"] = type_toggle.value or "tool"
            _apply_kind_options()
            await _build_target_picker()
            await _refresh_range_editor()

        type_toggle.on("update:model-value", _on_type)
        ui.timer(0.0, _build_target_picker, once=True)
    # A fixed (pre-filled) tool target already knows its version, so the
    # range editor can settle immediately once the user picks validation.
    ui.timer(0.0, _refresh_range_editor, once=True)

    dialog.open()


def _add_evidence_row(
    client: ApiClient, evidence_list: ui.column, rows: list[dict[str, Any]]
) -> None:
    """Append one live evidence row (type + inputs + remove) to the dialog.

    Each row is a self-contained controller stored on ``rows``; the input
    area rebuilds when its type changes. Library / URL rows resolve to a
    value immediately; Upload rows hold the bytes and are turned into a
    global library file at submit time (see :func:`_collect_evidence`).
    """
    row_state: dict[str, Any] = {
        "kind": "url",
        "file_version_id": None,
        "upload": {"filename": "", "bytes": b""},
        "url_input": None,
        "name_input": None,
        "approver_sel": None,
    }

    with evidence_list, ui.row().classes("w-full items-start gap-2 no-wrap") as row_el:
        type_sel = (
            ui.select(
                {"url": "External URL", "library": "Library file", "upload": "Upload file"},
                value="url",
            )
            .props("dense outlined")
            .style("min-width: 150px;")
        )
        input_area = ui.column().classes("gap-1 grow").style("min-width: 0;")

        def _remove() -> None:
            row_el.delete()
            if row_state in rows:
                rows.remove(row_state)

        ui.button(icon="close", on_click=_remove).props("flat dense round").tooltip(
            "Remove this evidence"
        )

    def _rebuild() -> None:
        input_area.clear()
        row_state["kind"] = type_sel.value or "url"
        row_state["file_version_id"] = None
        row_state["upload"] = {"filename": "", "bytes": b""}
        row_state["url_input"] = None
        row_state["name_input"] = None
        row_state["approver_sel"] = None
        with input_area:
            if row_state["kind"] == "url":
                row_state["url_input"] = (
                    ui.input("URL").props("dense outlined").classes("w-full")
                )
            elif row_state["kind"] == "library":
                _build_library_row(client, row_state)
            else:
                _build_upload_row(client, row_state)

    type_sel.on("update:model-value", lambda _e=None: _rebuild())
    row_state["type_sel"] = type_sel
    rows.append(row_state)
    _rebuild()


def _build_library_row(client: ApiClient, row_state: dict[str, Any]) -> None:
    """Library-file + version pickers for one evidence row."""
    file_sel = (
        ui.select({}, label="Library file", with_input=True)
        .props("dense outlined")
        .classes("w-full")
    )
    ver_sel = ui.select({}, label="Version").props("dense outlined").classes("w-full")

    async def _load_files() -> None:
        try:
            files = await client.list_library_files()
        except ApiError as exc:
            ui.notify(f"Couldn't load library: {exc.detail}", type="negative")
            return
        file_sel.options = {int(f["id"]): str(f.get("name") or "") for f in files}
        file_sel.update()

    async def _on_file(_e: Any = None) -> None:
        if not file_sel.value:
            return
        try:
            detail = await client.get_library_file(int(file_sel.value))
        except ApiError:
            return
        ver_sel.options = {
            int(v["id"]): f"v{v.get('ordinal', '?')} · {v.get('original_filename', '')}"
            for v in (detail.get("versions") or [])
        }
        ver_sel.update()

    def _on_ver(_e: Any = None) -> None:
        row_state["file_version_id"] = int(ver_sel.value) if ver_sel.value else None

    file_sel.on("update:model-value", _on_file)
    ver_sel.on("update:model-value", _on_ver)
    ui.timer(0.0, _load_files, once=True)


def _build_upload_row(client: ApiClient, row_state: dict[str, Any]) -> None:
    """Upload widget for one evidence row (lands in the global File Library)."""
    ui.label(
        "Added to the global File Library (immutable + content-hashed); may need a "
        "curator's sign-off if library approval is on."
    ).style("color: var(--matofx-muted-text); font-size: 0.75rem;")
    name_in = ui.input("Evidence name").props("dense outlined").classes("w-full")
    row_state["name_input"] = name_in
    file_label = ui.label("No file selected.").style(
        "color: var(--matofx-muted-text); font-size: 0.78rem;"
    )

    async def _on_upload(event: Any) -> None:
        try:
            data = await event.file.read()
            name = event.file.name or "evidence"
        except AttributeError:
            ui.notify("Couldn't read that upload (unexpected event shape).", type="negative")
            return
        row_state["upload"] = {"filename": name, "bytes": data}
        if not name_in.value:
            name_in.value = name
        file_label.set_text(f"Selected: {name} ({len(data):,} bytes)")

    ui.upload(
        on_upload=_on_upload, label="Upload evidence", auto_upload=True, max_files=1
    ).props("flat").classes("w-full")
    approver_sel = (
        ui.select({}, label="Reviewer for the global publish (if required)")
        .props("dense outlined")
        .classes("w-full")
    )
    row_state["approver_sel"] = approver_sel

    async def _load_approvers() -> None:
        try:
            approvers = await client.library_approvers()
        except ApiError:
            approvers = []
        approver_sel.options = {
            int(a["user_id"]): f"{a.get('full_name') or a['username']} ({a['username']})"
            for a in approvers
        }
        approver_sel.update()

    ui.timer(0.0, _load_approvers, once=True)


async def _collect_evidence(
    client: ApiClient, rows: list[dict[str, Any]], message: ui.label
) -> list[dict[str, Any]] | None:
    """Turn the live evidence rows into an ``evidence`` payload list.

    Uploads are created in the global library here and folded in as library
    references. Returns ``None`` (with ``message`` set) if an upload fails so
    the caller aborts; empty entries are skipped.
    """
    items: list[dict[str, Any]] = []
    for rs in rows:
        kind = rs.get("kind")
        if kind == "url":
            url_in = rs.get("url_input")
            url = (url_in.value or "").strip() if url_in is not None else ""
            if url:
                items.append({"kind": "url", "url": url})
        elif kind == "library":
            if rs.get("file_version_id"):
                items.append({"kind": "library", "file_version_id": int(rs["file_version_id"])})
        elif kind == "upload" and rs["upload"]["bytes"]:
            version_id = await _upload_row_to_library(client, rs, message)
            if version_id is None:
                return None
            items.append({"kind": "library", "file_version_id": version_id})
    return items


async def _upload_row_to_library(
    client: ApiClient, row_state: dict[str, Any], message: ui.label
) -> int | None:
    """Create a global library file from an upload row; return its version id."""
    upload = row_state["upload"]
    name_input = row_state.get("name_input")
    approver_sel = row_state.get("approver_sel")
    name = (name_input.value if name_input is not None else "") or upload["filename"] or "evidence"
    meta: dict[str, Any] = {
        "name": str(name).strip(),
        "provenance": "Uploaded as V&V evidence",
        "scope": "global",
    }
    if approver_sel is not None and approver_sel.value:
        meta["approver_user_id"] = int(approver_sel.value)
    try:
        created = await client.create_library_file(
            metadata=meta,
            filename=str(upload["filename"] or "evidence"),
            content=bytes(upload["bytes"]),
        )
    except ApiError as exc:
        message.text = exc.detail or "Couldn't add the uploaded evidence to the library."
        return None
    version_id = _first_version_id(created)
    if version_id is None:
        try:
            detail = await client.get_library_file(int(created["file"]["id"]))
            version_id = _first_version_id(detail)
        except (ApiError, KeyError, TypeError):
            version_id = None
    if version_id is None:
        message.text = "Uploaded a file but couldn't resolve its version — try again."
    return version_id


def _first_version_id(payload: dict[str, Any]) -> int | None:
    versions = payload.get("versions") or []
    if versions and isinstance(versions[0], dict) and "id" in versions[0]:
        return int(versions[0]["id"])
    return None


# ============================ migrate ================================


def _migrate_record_caption(rec: dict[str, Any]) -> str:
    """One-line description of an approved source record for the pick list."""
    kind = _TOOL_KIND_LABEL.get(str(rec.get("kind")), str(rec.get("kind")))
    bits = [kind]
    notes = str(rec.get("notes") or "").strip()
    if notes:
        bits.append(notes if len(notes) <= 60 else notes[:57] + "…")
    rng = dict(rec.get("applicable_range") or {})
    if rng:
        bits.append(f"{len(rng)} bounded factor" + ("" if len(rng) == 1 else "s"))
    return " · ".join(bits)


def open_vv_migrate_dialog(client: ApiClient) -> None:
    """Curator flow to carry approved V&V records forward to a new tool version.

    Pick a tool, a **source** version and a different **target** version,
    tick the approved records to bring forward, and migrate. Copies land as
    fresh drafts on the target (re-review required); any records that can't
    be brought forward come back as per-row skips. Curate-gated at the API.
    """
    state: dict[str, Any] = {"alias": None, "source_id": None, "target_id": None, "checks": {}}

    with ui.dialog() as dialog, ui.card().style("min-width: 600px; max-width: 94vw;"):
        ui.label("Migrate V&V to a new version").style("font-size: 1.15rem; font-weight: 650;")
        ui.label(
            "Bring approved V&V records forward from one version of a tool to another. "
            "Copies arrive as drafts on the target version and must be re-submitted and "
            "re-approved — the tool bundle changed, so the original sign-off doesn't carry over."
        ).style("color: var(--matofx-muted-text); font-size: 0.82rem;")

        tool_sel = (
            ui.select({}, label="Tool", with_input=True).props("dense outlined").classes("w-full")
        )
        with ui.row().classes("w-full gap-2 no-wrap"):
            source_sel = (
                ui.select({}, label="From version (source)")
                .props("dense outlined")
                .classes("flex-1")
            )
            target_sel = (
                ui.select({}, label="To version (target)")
                .props("dense outlined")
                .classes("flex-1")
            )

        ui.label("Approved records to bring forward").style(
            "font-weight: 600; font-size: 0.9rem; margin-top: 0.3rem;"
        )
        records_box = ui.column().classes("w-full gap-1")
        with records_box:
            ui.label("Pick a source version to list its approved records.").style(
                "color: var(--matofx-muted-text); font-size: 0.82rem;"
            )
        message = ui.label("").style("color: var(--matofx-error-text);")
        result_box = ui.column().classes("w-full gap-1")

        async def _load_tools() -> None:
            try:
                tools = await client.list_tools()
            except ApiError as exc:
                message.text = f"Couldn't load tools: {exc.detail}"
                return
            tool_sel.options = {
                str(t.get("alias")): str(t.get("name") or t.get("alias")) for t in tools
            }
            tool_sel.update()

        async def _load_versions() -> None:
            alias = tool_sel.value
            state["alias"] = alias
            state["source_id"] = None
            state["target_id"] = None
            state["checks"] = {}
            source_sel.options = {}
            target_sel.options = {}
            source_sel.value = None
            target_sel.value = None
            records_box.clear()
            if not alias:
                return
            try:
                versions = await client.list_tool_version_options(alias)
            except ApiError as exc:
                message.text = f"Couldn't load versions: {exc.detail}"
                return
            opts = {
                int(v["tool_version_id"]): (
                    f"v{v['version']}" + (" (current)" if v.get("is_current") else "")
                )
                for v in versions
            }
            source_sel.options = dict(opts)
            target_sel.options = dict(opts)
            source_sel.update()
            target_sel.update()
            if len(opts) < 2:
                message.text = "This tool has only one version — nothing to migrate between yet."

        async def _load_records() -> None:
            message.text = ""
            state["source_id"] = int(source_sel.value) if source_sel.value else None
            records_box.clear()
            state["checks"] = {}
            src = state["source_id"]
            if not src:
                return
            try:
                records = await client.vv_list_tool_records(tool_version_id=int(src))
            except ApiError as exc:
                with records_box:
                    ui.label(f"Couldn't load records: {exc.detail}").style(
                        "color: var(--matofx-error-text);"
                    )
                return
            approved = [r for r in records if str(r.get("state")) == "approved"]
            if not approved:
                with records_box:
                    ui.label("This version has no approved V&V records to migrate.").style(
                        "color: var(--matofx-muted-text); font-size: 0.82rem;"
                    )
                return
            with records_box:
                for rec in approved:
                    rid = int(rec.get("id") or 0)
                    cb = ui.checkbox(_migrate_record_caption(rec)).props("dense")
                    state["checks"][rid] = cb

        def _on_target(_e: Any = None) -> None:
            state["target_id"] = int(target_sel.value) if target_sel.value else None

        async def _migrate() -> None:
            message.text = ""
            result_box.clear()
            src = state.get("source_id")
            tgt = state.get("target_id")
            if not src or not tgt:
                message.text = "Pick both a source and a target version."
                return
            if src == tgt:
                message.text = "The source and target versions must be different."
                return
            chosen = [rid for rid, cb in state["checks"].items() if cb.value]
            if not chosen:
                message.text = "Tick at least one approved record to bring forward."
                return
            try:
                out = await client.vv_migrate_tool_records(
                    source_tool_version_id=int(src),
                    target_tool_version_id=int(tgt),
                    record_ids=chosen,
                )
            except ApiError as exc:
                message.text = exc.detail or "Couldn't migrate the records."
                return
            migrated = list(out.get("migrated") or [])
            skipped = list(out.get("skipped") or [])
            if not skipped:
                dialog.close()
                ui.notify(
                    f"Migrated {len(migrated)} record"
                    + ("" if len(migrated) == 1 else "s")
                    + " as drafts on the target version.",
                    type="positive",
                )
                ui.navigate.to("/vv-records")
                return
            # Some skips — keep the dialog open and report them.
            with result_box:
                ui.label(
                    f"Migrated {len(migrated)}; skipped {len(skipped)}:"
                ).style("font-weight: 600; font-size: 0.85rem; color: var(--matofx-text);")
                for sk in skipped:
                    ui.label(f"• Record #{sk.get('source_record_id')}: {sk.get('reason')}").style(
                        "color: var(--matofx-muted-text); font-size: 0.8rem;"
                    )
                ui.button("Done", on_click=lambda: ui.navigate.to("/vv-records")).props(
                    "flat no-caps color=primary"
                )

        tool_sel.on("update:model-value", _load_versions)
        source_sel.on("update:model-value", _load_records)
        target_sel.on("update:model-value", _on_target)

        with ui.row().classes("w-full justify-end gap-2").style("margin-top: 0.4rem;"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button("Migrate", icon="move_up", on_click=_migrate).props(
                "color=primary no-caps unelevated"
            )

    dialog.open()
    ui.timer(0.0, _load_tools, once=True)


# ============================= detail =================================


@ui.page("/vv-records/tool/{record_id}")
async def vv_tool_detail_page(record_id: int) -> None:
    """One tool V&V record — fields + state + curator actions."""
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    try:
        me = await client.me()
    except ApiError:
        me = {}
    perms = me.get("permissions") or []
    is_curator = _VV_CURATE in perms
    is_author = is_curator or _VV_CREATE in perms
    my_user_id = int(me.get("user_id") or 0)

    with ui.column().classes("w-full max-w-4xl mx-auto gap-3").style(
        "padding: 0.85rem 1.5rem 1.5rem;"
    ):
        body = ui.column().classes("w-full gap-3")
    await _render_tool_detail(client, int(record_id), body, is_curator, is_author, my_user_id)


@ui.page("/vv-records/job/{record_id}")
async def vv_job_detail_page(record_id: int) -> None:
    """One job V&V record — fields + state + curator actions."""
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    try:
        me = await client.me()
    except ApiError:
        me = {}
    perms = me.get("permissions") or []
    is_curator = _VV_CURATE in perms
    is_author = is_curator or _VV_CREATE in perms
    my_user_id = int(me.get("user_id") or 0)

    with ui.column().classes("w-full max-w-4xl mx-auto gap-3").style(
        "padding: 0.85rem 1.5rem 1.5rem;"
    ):
        body = ui.column().classes("w-full gap-3")
    await _render_job_detail(client, int(record_id), body, is_curator, is_author, my_user_id)


async def _render_tool_detail(
    client: ApiClient,
    record_id: int,
    body: ui.column,
    is_curator: bool,
    is_author: bool,
    my_user_id: int,
) -> None:
    body.clear()
    try:
        r = await client.vv_get_tool_record(record_id)
    except ApiError as exc:
        with body:
            ui.label(f"Couldn't load this record: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
        return

    async def _reload() -> None:
        await _render_tool_detail(client, record_id, body, is_curator, is_author, my_user_id)

    with body:
        _render_detail_header("tool", r, on_back=lambda: ui.navigate.to("/vv-records"))
        with section_card("fact_check", "Details"):
            _kv("Kind", _TOOL_KIND_LABEL.get(str(r.get("kind")), str(r.get("kind"))))
            _kv("Tool version", str(r.get("target_label") or f"#{r.get('tool_version_id')}"))
            _kv("Notes", str(r.get("notes") or "—"))
            band = r.get("confidence_band")
            if band is not None:
                _kv("Confidence band", f"±{band}")
            standards = list(r.get("standards_cited") or [])
            if standards:
                with ui.row().classes("items-center gap-1"):
                    ui.label("Standards cited:").style(
                        "color: var(--matofx-muted-text); font-size: 0.82rem;"
                    )
                    for s in standards:
                        ui.chip(str(s)).props("dense outline")
            applicable = dict(r.get("applicable_range") or {})
            if applicable:
                ui.label("Applicable range").style("font-weight: 600; margin-top: 0.5rem;")
                _render_applicable_range(applicable)
            _render_evidence(r)
        _render_decision_card(r)
        _render_actions(
            client,
            r,
            kind="tool",
            is_curator=is_curator,
            is_author=is_author,
            my_user_id=my_user_id,
            reload=_reload,
        )


async def _render_job_detail(
    client: ApiClient,
    record_id: int,
    body: ui.column,
    is_curator: bool,
    is_author: bool,
    my_user_id: int,
) -> None:
    body.clear()
    try:
        r = await client.vv_get_job_record(record_id)
    except ApiError as exc:
        with body:
            ui.label(f"Couldn't load this record: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
        return

    async def _reload() -> None:
        await _render_job_detail(client, record_id, body, is_curator, is_author, my_user_id)

    with body:
        _render_detail_header("job", r, on_back=lambda: ui.navigate.to("/vv-records"))
        with section_card("fact_check", "Details"):
            _kv("Kind", _JOB_KIND_LABEL.get(str(r.get("kind")), str(r.get("kind"))))
            _kv("Job", str(r.get("target_label") or format_job_id(int(r.get("job_id") or 0))))
            _kv("Notes", str(r.get("notes") or "—"))
            _render_evidence(r)
        _render_decision_card(r)
        _render_actions(
            client,
            r,
            kind="job",
            is_curator=is_curator,
            is_author=is_author,
            my_user_id=my_user_id,
            reload=_reload,
        )


def _render_evidence(r: dict[str, Any]) -> None:
    items = list(r.get("evidence_items") or [])
    if not items:
        return
    ui.label("Evidence").style("font-weight: 600; margin-top: 0.5rem;")
    with ui.column().classes("gap-1"):
        for it in items:
            icon = "link" if it.get("kind") == "url" else "attach_file"
            label = str(it.get("label") or "")
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon(icon).style("color: var(--matofx-muted-text); font-size: 1rem;")
                if it.get("kind") == "url" and it.get("url"):
                    ui.link(label or str(it["url"]), str(it["url"])).props("target=_blank")
                else:
                    ui.label(label or "—")


def _render_detail_header(kind: str, r: dict[str, Any], *, on_back: Any) -> None:
    state = str(r.get("state") or "draft")
    vv_kind = str(r.get("kind") or "")
    title = _kind_label(kind, vv_kind)
    recorded_by = str(r.get("recorded_by_label") or f"user #{r.get('recorded_by_user_id')}")
    with ui.row().classes("w-full items-center justify-between gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.button(icon="arrow_back", on_click=on_back).props("flat round dense").tooltip(
                "Back to V&V records"
            )
            ui.label(title).style("font-size: 1.3rem; font-weight: 650;")
            ui.label(f"#{r.get('id')}").style("color: var(--matofx-muted-text);")
            _state_chip(state)
        ui.label(
            f"Recorded {format_user_datetime(str(r.get('recorded_at') or ''))} by {recorded_by}"
        ).style("color: var(--matofx-muted-text); font-size: 0.82rem;")


def _render_decision_card(r: dict[str, Any]) -> None:
    state = str(r.get("state") or "")
    if state not in ("approved", "rejected"):
        return
    decided_by = str(r.get("decided_by_label") or f"user #{r.get('decided_by_user_id')}")
    with section_card("gavel", "Decision"):
        _kv("State", _STATE_LABEL.get(state, state.title()))
        _kv("Decided by", decided_by)
        _kv("Decided at", format_user_datetime(str(r.get("decided_at") or "")))
        reason = str(r.get("decision_reason") or "").strip()
        if reason:
            _kv("Reason", reason)


def _render_actions(
    client: ApiClient,
    r: dict[str, Any],
    *,
    kind: str,
    is_curator: bool,
    is_author: bool,
    my_user_id: int,
    reload: Any,
) -> None:
    """Lifecycle buttons — visible only when the caller can act on this state.

    When a pending record was submitted by the viewer, the Approve / Reject
    buttons still render but come up greyed-out and disabled: the directed-
    approver gate forbids self-approval, so the service would 403 the click.
    A muted note explains why. Any other curator sees them live.
    """
    if not (is_author or is_curator):
        return
    rid = int(r.get("id") or 0)
    state = str(r.get("state") or "")
    i_submitted = int(r.get("recorded_by_user_id") or 0) == my_user_id
    # (label, icon, handler, props, disabled_reason|None)
    actions: list[tuple[str, str, Any, str, str | None]] = []
    if state == "draft":
        if is_author:
            actions.append(
                (
                    "Submit for review",
                    "send",
                    lambda: _open_submit_dialog(client, rid, kind=kind, reload=reload),
                    "color=primary no-caps unelevated",
                    None,
                )
            )
        if is_curator:
            actions.append(
                (
                    "Retire",
                    "delete_outline",
                    lambda: _confirm_retire(client, rid, kind=kind, state=state, reload=reload),
                    "flat dense no-caps color=negative",
                    None,
                )
            )
    elif state == "pending" and is_curator:
        # Self-approval is forbidden — grey out (don't hide) the decision
        # buttons for the submitter so the constraint reads clearly.
        block = (
            "You submitted this record, so you can't approve or reject it — "
            "the directed-approver gate forbids self-approval."
            if i_submitted
            else None
        )
        actions.append(
            (
                "Approve",
                "check",
                lambda: _open_decide_dialog(
                    client, rid, kind=kind, decision="approve", reload=reload
                ),
                "color=positive no-caps unelevated",
                block,
            )
        )
        actions.append(
            (
                "Reject",
                "close",
                lambda: _open_decide_dialog(
                    client, rid, kind=kind, decision="reject", reload=reload
                ),
                "color=negative no-caps unelevated",
                block,
            )
        )
        # Retire is a withdrawal, not a decision — a curator may pull a
        # pending record out of review whether or not they submitted it,
        # so it's never blocked by the self-approval rule above.
        actions.append(
            (
                "Retire",
                "delete_outline",
                lambda: _confirm_retire(client, rid, kind=kind, state=state, reload=reload),
                "flat dense no-caps color=negative",
                None,
            )
        )
    elif state in ("approved", "rejected") and is_curator:
        actions.append(
            (
                "Retire",
                "delete_outline",
                lambda: _confirm_retire(client, rid, kind=kind, state=state, reload=reload),
                "flat dense no-caps color=negative",
                None,
            )
        )
    if not actions:
        return
    disabled_note = next((reason for *_rest, reason in actions if reason), None)
    with ui.row().classes("w-full items-center justify-end gap-2"):
        if disabled_note:
            ui.label(disabled_note).style(
                "flex: 1; color: var(--matofx-muted-text); font-size: 0.8rem;"
            )
        for label, icon, handler, props, reason in actions:
            btn = ui.button(label, icon=icon, on_click=handler).props(props)
            if reason:
                # Quasar swallows hover events on a disabled button, so pair
                # the disable with the muted note above (rendered once).
                btn.props("disable").tooltip(reason)


def _render_applicable_range(rng: dict[str, dict[str, float]]) -> None:
    ui.table(
        columns=[
            {"name": "factor", "label": "Factor", "field": "factor", "align": "left"},
            {"name": "min", "label": "Min", "field": "min"},
            {"name": "max", "label": "Max", "field": "max"},
        ],
        rows=[
            {"factor": str(name), "min": bounds.get("min"), "max": bounds.get("max")}
            for name, bounds in rng.items()
        ],
    ).classes("w-full").props("dense flat")


def _kv(label: str, value: str) -> None:
    with ui.row().classes("w-full items-baseline gap-2 no-wrap"):
        ui.label(label).style(
            "flex: 0 0 160px; color: var(--matofx-muted-text); font-size: 0.82rem;"
        )
        ui.label(value or "—").style("font-weight: 500;")


# ----------------------------- dialogs -------------------------------


def _open_submit_dialog(client: ApiClient, record_id: int, *, kind: str, reload: Any) -> None:
    """Submit a draft for review with a designated approver."""
    with ui.dialog() as dialog, ui.card().style("min-width: 460px;"):
        ui.label("Submit for review").style("font-size: 1.1rem; font-weight: 650;")
        ui.label(
            "Pick a curator to review this claim. They can't be you — the directed-"
            "approver gate forbids self-approval (the secure default)."
        ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        approver_sel = ui.select({}, label="Reviewer *").props("dense outlined").classes("w-full")
        message = ui.label("").style("color: var(--matofx-error-text);")

        async def _load() -> None:
            try:
                curators = await client.vv_list_curators()
            except ApiError as exc:
                message.text = f"Couldn't load curators: {exc.detail}"
                return
            opts = {int(c["user_id"]): str(c["username"]) for c in curators}
            if not opts:
                message.text = (
                    "No other V&V curators are available — ask an admin to grant "
                    "vv.curate to at least one other user."
                )
                return
            approver_sel.options = opts
            approver_sel.update()

        async def _submit() -> None:
            message.text = ""
            if not approver_sel.value:
                message.text = "Pick a reviewer."
                return
            user_id = int(approver_sel.value)
            opts_map = approver_sel.options if isinstance(approver_sel.options, dict) else {}
            username = str(opts_map.get(user_id, ""))
            try:
                if kind == "tool":
                    await client.vv_submit_tool_record(
                        record_id,
                        designated_approver_user_id=user_id,
                        designated_approver_username=username,
                    )
                else:
                    await client.vv_submit_job_record(
                        record_id,
                        designated_approver_user_id=user_id,
                        designated_approver_username=username,
                    )
            except ApiError as exc:
                message.text = exc.detail or "Couldn't submit."
                return
            dialog.close()
            ui.notify("Submitted for review.", type="positive")
            await reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button("Submit", icon="send", on_click=_submit).props(
                "color=primary no-caps unelevated"
            )
    dialog.open()
    ui.timer(0.0, _load, once=True)


def _open_decide_dialog(
    client: ApiClient,
    record_id: int,
    *,
    kind: str,
    decision: str,
    reload: Any,
) -> None:
    """Approve / reject a pending record. Reason required on reject."""
    title = "Approve record" if decision == "approve" else "Reject record"
    reason_required = decision == "reject"
    with ui.dialog() as dialog, ui.card().style("min-width: 440px;"):
        ui.label(title).style("font-size: 1.1rem; font-weight: 650;")
        if reason_required:
            ui.label(
                "A rejection needs a reason — it's recorded in the audit log and "
                "shown to the author."
            ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        reason_in = (
            ui.textarea("Reason" if reason_required else "Reason (optional)")
            .props("outlined")
            .classes("w-full")
        )
        message = ui.label("").style("color: var(--matofx-error-text);")

        async def _go() -> None:
            reason = (reason_in.value or "").strip()
            if reason_required and not reason:
                message.text = "Reason is required to reject."
                return
            try:
                if decision == "approve":
                    if kind == "tool":
                        await client.vv_approve_tool_record(record_id, reason=reason or None)
                    else:
                        await client.vv_approve_job_record(record_id, reason=reason or None)
                elif kind == "tool":
                    await client.vv_reject_tool_record(record_id, reason=reason)
                else:
                    await client.vv_reject_job_record(record_id, reason=reason)
            except ApiError as exc:
                message.text = exc.detail or "Couldn't decide."
                return
            dialog.close()
            ui.notify(
                "Approved." if decision == "approve" else "Rejected.",
                type="positive" if decision == "approve" else "warning",
            )
            await reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button(
                "Approve" if decision == "approve" else "Reject",
                icon="check" if decision == "approve" else "close",
                on_click=_go,
            ).props(
                f"color={'positive' if decision == 'approve' else 'negative'} no-caps unelevated"
            )
    dialog.open()


def _confirm_retire(
    client: ApiClient, record_id: int, *, kind: str, state: str = "", reload: Any
) -> None:
    """Retire a record from any live state.

    Copy adapts to the record's state: a *pending* record's retire also
    withdraws its open review, and only an *approved* tool record's
    retirement is truly cohort-wide (a never-approved record contributes
    to no badge).
    """
    with ui.dialog() as dialog, ui.card().style("min-width: 440px;"):
        ui.label("Retire this V&V record?").style("font-size: 1.1rem; font-weight: 650;")
        if state == "pending":
            ui.label(
                "This record is in review. Retiring it withdraws it from the "
                "reviewer's queue and closes the request — no decision is recorded. "
                "Re-record + re-submit to bring it back."
            ).style("color: var(--matofx-muted-text);")
        elif kind == "tool":
            ui.label(
                "Retirement is cohort-wide — every tool version that inherited this "
                "claim will stop showing it. Re-record + re-submit to bring it back."
            ).style("color: var(--matofx-muted-text);")
        else:
            ui.label(
                "Retirement removes this claim from the job's V&V card. Re-record + "
                "re-submit to bring it back."
            ).style("color: var(--matofx-muted-text);")

        async def _confirm() -> None:
            try:
                if kind == "tool":
                    await client.vv_retire_tool_record(record_id)
                else:
                    await client.vv_retire_job_record(record_id)
            except ApiError as exc:
                ui.notify(f"Couldn't retire: {exc.detail}", type="negative")
                return
            dialog.close()
            ui.notify("Retired.", type="info")
            await reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button("Retire", icon="delete_outline", on_click=_confirm).props(
                "color=negative no-caps unelevated"
            )
    dialog.open()
