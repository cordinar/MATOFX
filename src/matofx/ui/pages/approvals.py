"""Simulation Approvals page (``/ui/approvals``) — the approver inbox.

The one place an approver acts (§16.15). A batch table of everything
pending on the caller (pre + post mixed, kind-tagged); each row
expands to the rich review panel (identity, resource/licence
footprint, the grouped inputs the requestor submitted, their note);
per row the approver picks Approve / Deny, then one "Submit
decisions" applies the whole batch. A denial needs a reason — it's
emailed back to the requestor (Phase 5) and is mandatory here.

Per the UI Conventions: the filter row sits above the list; the tool
**name** (never the alias) is what's shown.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_job_id, format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token

# Tool-less gates (ADR-0006 / ADR-0007) have no job / tool — the inbox
# tags them by what they govern instead of a job id.
_TARGET_LABEL: dict[str, str] = {"library": "Library file", "material": "Material"}


@ui.page("/approvals")
async def approvals_page() -> None:
    """Render the approver inbox."""
    if not require_logged_in():
        return
    render_shell()
    client = ApiClient(get_app(), token=current_token())

    with ui.column().classes("w-full mx-auto p-6 gap-4").style("max-width: 1100px;"):
        ui.label("Simulation Approvals").style("font-size: 1.6rem; font-weight: 600;")

        try:
            items = await client.list_my_approvals()
        except ApiError as exc:
            ui.label(f"Couldn't load approvals: {exc.detail}").style("color: #c00;")
            ui.button("Back to Home", on_click=lambda: ui.navigate.to("/home")).props("flat")
            return

        if not items:
            with ui.card().classes("w-full"):
                ui.label("You're all caught up — no simulations are waiting on your approval.")
            ui.button("Back to Home", on_click=lambda: ui.navigate.to("/home")).props("flat")
            return

        ui.label(
            "Choose Approve or Deny for each simulation, then submit. A "
            "denial needs a reason — it is sent to the requestor."
        ).style("color: #888;")

        # request_id -> {"decision": None|'approve'|'deny', "reason": str}
        decisions: dict[int, dict[str, Any]] = {}
        # One control handle per row: card + radio + reason widgets, the
        # shared decision state, the row's item, and its current filter
        # visibility (bulk actions only touch *shown* rows).
        controls: list[dict[str, Any]] = []

        # Filter row — above the list (UI Conventions).
        with ui.row().classes("w-full items-center gap-3"):
            search = (
                ui.input(placeholder="Filter by job, tool or requestor…")
                .props("clearable dense outlined")
                .classes("grow")
            )
            kind_sel = ui.select(
                {"all": "All", "pre": "Approve to run", "post": "Release results"},
                value="all",
            ).props("dense outlined")

        def _apply_filter() -> None:
            q = (search.value or "").strip().lower()
            k = kind_sel.value
            for c in controls:
                item = c["item"]
                jid = item.get("job_id")
                haystack = (
                    f"{format_job_id(jid) if jid else ''} {item.get('job_name', '')} "
                    f"{item.get('tool_name') or ''} {item.get('requestor_name', '')}"
                ).lower()
                vis = (q in haystack) and (k == "all" or item["kind"] == k)
                c["visible"] = vis
                c["card"].set_visibility(vis)

        search.on_value_change(lambda _e: _apply_filter())
        kind_sel.on_value_change(lambda _e: _apply_filter())

        # Bulk decide — set every *shown* row at once, then Submit applies
        # the whole batch. Approve is irreversible-ish and may skip opening
        # each panel, so it confirms first; deny prompts for one shared
        # reason (mandatory). The per-row controls stay editable after.
        def _set_decision(c: dict[str, Any], decision: str, reason: str) -> None:
            c["state"]["decision"] = decision
            c["state"]["reason"] = reason
            c["radio"].value = decision
            c["reason"].value = reason
            c["reason"].set_visibility(decision == "deny")

        def _shown() -> list[dict[str, Any]]:
            return [c for c in controls if c.get("visible", True)]

        async def _approve_all() -> None:
            shown = _shown()
            if not shown:
                ui.notify("No simulations are shown to approve.", type="warning")
                return
            with ui.dialog() as dialog, ui.card():
                ui.label(f"Approve {len(shown)} shown simulation(s) without opening each?")
                ui.label("You can still change individual rows before submitting.").style(
                    "color: #888; font-size: 0.8rem;"
                )
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat")
                    ui.button("Approve all shown", on_click=lambda: dialog.submit(True)).props(
                        "color=primary"
                    )
            if await dialog:
                for c in shown:
                    _set_decision(c, "approve", "")
                ui.notify(
                    f"{len(shown)} marked to approve — review and Submit decisions.",
                    type="positive",
                )

        async def _deny_all() -> None:
            shown = _shown()
            if not shown:
                ui.notify("No simulations are shown to deny.", type="warning")
                return
            with ui.dialog() as dialog, ui.card().classes("w-full").style("max-width: 460px;"):
                ui.label(f"Deny {len(shown)} shown simulation(s)").style("font-weight: 600;")
                reason_box = (
                    ui.textarea(placeholder="Reason — sent to each requestor (required)")
                    .props("outlined autogrow")
                    .classes("w-full")
                )
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=lambda: dialog.submit(None)).props("flat")
                    ui.button(
                        "Deny all shown",
                        on_click=lambda: dialog.submit(reason_box.value or ""),
                    ).props("color=negative")
            reason = await dialog
            if reason is None:
                return
            if not reason.strip():
                ui.notify("A denial needs a reason.", type="negative")
                return
            for c in shown:
                _set_decision(c, "deny", reason.strip())
            ui.notify(f"{len(shown)} marked to deny — review and Submit decisions.", type="warning")

        with ui.row().classes("w-full items-center gap-2"):
            ui.label("Select all shown:").style("color: #888; font-size: 0.85rem;")
            ui.button("Approve all", icon="done_all", on_click=_approve_all).props(
                "flat dense no-caps color=primary"
            )
            ui.button("Deny all", icon="block", on_click=_deny_all).props(
                "flat dense no-caps color=negative"
            )

        with ui.column().classes("w-full gap-2"):
            for item in items:
                rid = int(item["request_id"])
                decisions[rid] = {"decision": None, "reason": ""}
                controls.append(_render_row(client, item, decisions[rid]))

        async def _submit() -> None:
            payload: list[dict[str, Any]] = []
            for rid, st in decisions.items():
                if st["decision"] is None:
                    continue
                if st["decision"] == "deny" and not (st["reason"] or "").strip():
                    ui.notify(
                        "Every denied simulation needs a reason.",
                        type="negative",
                    )
                    return
                payload.append(
                    {
                        "request_id": rid,
                        "decision": st["decision"],
                        "reason": (st["reason"] or "").strip() or None,
                    }
                )
            if not payload:
                ui.notify(
                    "Nothing selected — pick Approve or Deny on at least one simulation.",
                    type="warning",
                )
                return
            try:
                resp = await client.submit_approval_decisions(payload)
            except ApiError as exc:
                ui.notify(f"Submit failed: {exc.detail}", type="negative")
                return
            results = resp.get("results", [])
            ok = [r for r in results if r.get("ok")]
            bad = [r for r in results if not r.get("ok")]
            if ok:
                ui.notify(f"{len(ok)} decision(s) applied.", type="positive")
            for r in bad:
                ui.notify(
                    f"Request {r['request_id']}: {r.get('error', 'failed')}",
                    type="negative",
                )
            # Re-render so decided rows drop off and any per-item failures
            # reflect the live state. ``navigate.to`` to the *current* path
            # is a client-side no-op (the page never re-runs, so the just-
            # decided rows linger) — force a real reload instead.
            ui.navigate.reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Back to Home", on_click=lambda: ui.navigate.to("/home")).props("flat")
            ui.button("Submit decisions", on_click=_submit).props("color=primary")


def _render_row(client: ApiClient, item: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """One pending request: header + decision controls + lazy review.

    Returns a control handle (``card`` / ``radio`` / ``reason`` widgets +
    shared ``state`` + ``item`` + filter ``visible`` flag) so the page's
    filter and bulk-decide actions can drive the row.
    """
    rid = int(item["request_id"])
    is_pre = item["kind"] == "pre"
    kind_label = "Approve to run" if is_pre else "Release results"

    target_kind = item.get("target_kind", "job")
    with ui.card().classes("w-full") as card:
        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            ui.badge(kind_label).props(f"color={'teal' if is_pre else 'deep-orange'}")
            if target_kind in _TARGET_LABEL:
                # Tool-less gate (library / material): no job id, no tool.
                ui.badge(_TARGET_LABEL[target_kind]).props("color=indigo")
                ui.label(item.get("job_name") or "").style("font-weight: 600;")
            else:
                jid = item.get("job_id")
                head = (
                    f"{format_job_id(jid)} · {item['job_name']}"
                    if jid
                    else item.get("job_name", "")
                )
                ui.label(head).style("font-weight: 600;")
                if item.get("tool_name"):
                    ui.label(item["tool_name"]).style("color: #888;")
            ui.space()
            ui.label(item["requestor_name"]).style("color: #888;")
            ui.label(format_user_datetime(item["requested_at"])).style(
                "color: #888; font-size: 0.8rem;"
            )

        with ui.row().classes("w-full items-center gap-4 no-wrap"):
            radio = ui.radio({"approve": "Approve", "deny": "Deny"}, value=None).props("inline")
            reason = (
                ui.textarea(placeholder="Reason (required to deny) — sent to the requestor")
                .props("dense outlined autogrow")
                .classes("grow")
            )
            reason.set_visibility(False)

            def _on_decision(e: Any) -> None:
                state["decision"] = e.value
                reason.set_visibility(e.value == "deny")

            def _on_reason(e: Any) -> None:
                state["reason"] = e.value or ""

            radio.on_value_change(_on_decision)
            reason.on_value_change(_on_reason)

        exp = ui.expansion("Review inputs & details").classes("w-full")
        loaded = {"done": False}

        async def _maybe_load(e: Any) -> None:
            if not e.value or loaded["done"]:
                return
            loaded["done"] = True
            with exp:
                body = ui.column().classes("w-full gap-2")
                with body:
                    ui.spinner()
            try:
                detail = await client.get_approval_detail(rid)
            except ApiError as exc:
                body.clear()
                with body:
                    ui.label(f"Couldn't load details: {exc.detail}").style("color: #c00;")
                return
            body.clear()
            with body:
                _render_detail(client, detail)

        exp.on_value_change(_maybe_load)
    return {
        "card": card,
        "radio": radio,
        "reason": reason,
        "state": state,
        "item": item,
        "visible": True,
    }


def _kv(label: str, value: str) -> None:
    with ui.column().classes("gap-0"):
        ui.label(label).style("color: #888; font-size: 0.72rem;")
        ui.label(value or "—").style("font-weight: 500;")


def _fmt_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _render_input_groups(d: dict[str, Any]) -> None:
    """Render the submitted ``input_groups`` rows (label · value · unit)."""
    for group in d.get("input_groups") or []:
        ui.label(group["label"]).style("font-weight: 600; margin-top: 0.4rem;")
        with ui.column().classes("w-full gap-0"):
            for r in group.get("rows") or []:
                with ui.row().classes("w-full items-baseline gap-2 no-wrap"):
                    ui.label(r["label"]).style("flex: 0 0 240px; color: #555;")
                    ui.label(_fmt_value(r.get("value"))).style("font-weight: 500;")
                    if r.get("unit"):
                        ui.label(r["unit"]).style("color: #888;")
                    if r.get("changed_from_default"):
                        ui.badge("changed").props("color=amber text-color=black").style(
                            "font-size: 0.65rem;"
                        )


def _render_tool_less_detail(d: dict[str, Any], target_kind: str) -> None:
    """Review panel for a library / material publish gate (no job / tool)."""
    with ui.row().classes("w-full gap-6 flex-wrap"):
        _kv(_TARGET_LABEL.get(target_kind, target_kind.title()), d.get("job_name", ""))
        _kv("Requested by", d.get("requestor_name", ""))
        _kv("Requested", format_user_datetime(d.get("requested_at")))
    if (d.get("justification") or "").strip():
        with ui.card().classes("w-full").style("background: rgba(0,0,0,0.03);"):
            ui.label("Requestor note").style("font-weight: 600; font-size: 0.85rem;")
            ui.label(d["justification"])
    _render_input_groups(d)


def _render_detail(client: ApiClient, d: dict[str, Any]) -> None:
    """Render the rich review payload inside an expanded row.

    For a ``post`` request the panel also shows the run's produced
    results (the approver is deciding whether to release them); file
    outputs get an authenticated download button. A tool-less gate
    (library / material publish) renders the simpler tool-less panel.
    """
    target_kind = d.get("target_kind", "job")
    if target_kind in _TARGET_LABEL:
        _render_tool_less_detail(d, target_kind)
        return
    if target_kind == "surrogate_model":
        _render_surrogate_detail(d)
        return
    with ui.row().classes("w-full gap-6 flex-wrap"):
        _kv("Project", d.get("project_name", ""))
        if target_kind != "optimisation":
            _kv("Scenario", d.get("scenario_name", ""))
        _kv(
            "Tool",
            f"{d.get('tool_name', '')} (v{d.get('tool_version', '')})",
        )
        _kv("Requested by", d.get("requestor_name", ""))
        _kv("Requested", format_user_datetime(d.get("requested_at")))

    # An optimisation start/results gate carries its footprint in the
    # structured block (the blast-radius the approver is authorising);
    # every other gate shows the per-run footprint one-liner.
    if target_kind != "optimisation":
        footprint = f"{d.get('cpu_cores', 0)} CPU · {d.get('ram_mb', 0)} MB RAM"
        if d.get("gpu_memory_mb"):
            footprint += f" · GPU {d['gpu_memory_mb']} MB"
        licences = ", ".join(f"{lic['name']} x{lic['count']}" for lic in d.get("licences") or [])
        if licences:
            footprint += f" · Licences: {licences}"
        ui.label(footprint).style("color: #888;")

    if (d.get("justification") or "").strip():
        with ui.card().classes("w-full").style("background: rgba(0,0,0,0.03);"):
            ui.label("Requestor note").style("font-weight: 600; font-size: 0.85rem;")
            ui.label(d["justification"])

    if target_kind == "optimisation":
        _render_optimisation_review(d.get("optimisation") or {})
    if target_kind == "study" and (d.get("factors") or []):
        _render_factors(d.get("factors") or [])

    if (d.get("input_groups") or []) and d.get("kind") == "post":
        ui.label("Inputs").style("font-size: 1.05rem; font-weight: 600; margin-top: 0.2rem;")
    for group in d.get("input_groups") or []:
        ui.label(group["label"]).style("font-weight: 600; margin-top: 0.4rem;")
        with ui.column().classes("w-full gap-0"):
            for r in group.get("rows") or []:
                with ui.row().classes("w-full items-baseline gap-2 no-wrap"):
                    ui.label(r["label"]).style("flex: 0 0 240px; color: #555;")
                    ui.label(_fmt_value(r.get("value"))).style("font-weight: 500;")
                    if r.get("unit"):
                        ui.label(r["unit"]).style("color: #888;")
                    if r.get("changed_from_default"):
                        ui.badge("changed").props("color=amber text-color=black").style(
                            "font-size: 0.65rem;"
                        )

    if d.get("run_matrix"):
        _render_run_matrix(d["run_matrix"])

    # An optimisation post gate's results view *is* the runs matrix; the
    # job/study results section would only show an empty "Results" block.
    if d.get("kind") == "post" and target_kind != "optimisation":
        _render_results_section(client, d)


def _render_results_section(client: ApiClient, d: dict[str, Any]) -> None:
    """The produced-results block shown to a ``post`` approver."""
    ui.separator().style("margin: 0.6rem 0 0.2rem;")
    ui.label("Results").style("font-size: 1.05rem; font-weight: 600;")
    ui.label("Approving releases these results to the requestor; denying withholds them.").style(
        "color: #888; font-size: 0.8rem;"
    )

    result_groups = d.get("result_groups") or []
    if not any(g.get("rows") for g in result_groups):
        ui.label("No results were recorded for this run.").style("color: #888;")
        return

    job_id = int(d.get("job_id") or 0)  # a post gate always has a job
    for group in result_groups:
        rows = group.get("rows") or []
        if not rows:
            continue
        ui.label(group["label"]).style("font-weight: 600; margin-top: 0.4rem;")
        with ui.column().classes("w-full gap-0"):
            for r in rows:
                _render_result_row(client, job_id, r)


def _render_result_row(client: ApiClient, job_id: int, r: dict[str, Any]) -> None:
    """One produced output: a scalar value, or a file download button."""
    filename = r.get("filename")
    with ui.row().classes("w-full items-baseline gap-2 no-wrap"):
        ui.label(r["label"]).style("flex: 0 0 240px; color: #555;")
        if filename:
            leaf = str(filename).replace("\\", "/").rsplit("/", 1)[-1]

            async def _download(
                _e: Any = None,
                _fn: str = str(filename),
                _leaf: str = leaf,
            ) -> None:
                try:
                    data = await client.download_job_output(job_id, _fn)
                except ApiError as exc:
                    ui.notify(
                        f"Couldn't download {_leaf}: {exc.detail}",
                        type="negative",
                    )
                    return
                ui.download.content(data, _leaf)

            ui.button(leaf, icon="download", on_click=_download).props(
                "flat dense no-caps size=sm color=primary"
            )
        else:
            ui.label(_fmt_value(r.get("value"))).style("font-weight: 500;")
            if r.get("unit"):
                ui.label(r["unit"]).style("color: #888;")


# --- optimisation / surrogate / factor review blocks (D2) -------------------

_HEADING = "font-weight: 600; margin-top: 0.5rem;"
_KEYCOL = "flex: 0 0 240px; color: #555;"
_SENSE_LABELS = {
    "minimise": "Minimise",
    "maximise": "Maximise",
    "track": "Track (not optimised)",
}


def _variable_domain_text(v: dict[str, Any]) -> str:
    """A factor's domain as one human string — range / level set / fixed value."""
    if v.get("role") == "constant":
        return f"= {_fmt_value(v.get('value'))}"
    levels = v.get("levels") or []
    if levels:
        return "{" + ", ".join(_fmt_value(x) for x in levels) + "}"
    lower, upper = v.get("lower"), v.get("upper")
    if lower is not None or upper is not None:
        return f"{_fmt_value(lower)} to {_fmt_value(upper)}"
    if v.get("value") is not None:
        return str(v["value"])
    return "—"


def _render_variable_row(v: dict[str, Any]) -> None:
    """One design variable / factor: label · domain · unit · role badge."""
    with ui.row().classes("w-full items-baseline gap-2 no-wrap"):
        ui.label(v.get("label") or v.get("name") or "").style(_KEYCOL)
        ui.label(_variable_domain_text(v)).style("font-weight: 500;")
        if v.get("unit"):
            ui.label(v["unit"]).style("color: #888;")
        if v.get("role") == "constant":
            ui.badge("constant").props("color=blue-grey").style("font-size: 0.65rem;")


def _render_factors(factors: list[dict[str, Any]]) -> None:
    """Parametric-study factors — varied first, then held-constant."""
    ui.label("Factors").style(_HEADING)
    with ui.column().classes("w-full gap-0"):
        for v in sorted(factors, key=lambda f: f.get("role") != "vary"):
            _render_variable_row(v)


def _render_footprint(fp: dict[str, Any]) -> None:
    """The estimated compute blast-radius for an optimisation start gate."""
    if not fp:
        return
    lo, hi = fp.get("est_runs_min"), fp.get("est_runs_max")
    runs = (
        f"≈ {lo} to {hi} runs" if lo is not None and hi is not None and lo != hi else f"≈ {hi} runs"
    )
    per = f"{fp.get('cpu_cores_per_run', 0)} CPU · {fp.get('ram_mb_per_run', 0)} MB RAM"
    if fp.get("gpu_memory_mb_per_run"):
        per += f" · GPU {fp['gpu_memory_mb_per_run']} MB"
    licences = ", ".join(f"{lic['name']} x{lic['count']}" for lic in fp.get("licences") or [])
    with ui.card().classes("w-full").style("background: rgba(255,153,0,0.06);"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.icon("speed").style("color: #c80;")
            ui.label("Estimated compute footprint").style("font-weight: 600;")
            if fp.get("approximate"):
                ui.badge("approximate").props("color=amber text-color=black").style(
                    "font-size: 0.65rem;"
                )
        ui.label(f"{runs} · {per} each").style("font-weight: 500;")
        if fp.get("est_core_hours") is not None:
            ui.label(f"Up to ≈ {fp['est_core_hours']} core-hours").style("color: #555;")
        if licences:
            ui.label(f"Licences per run: {licences}").style("color: #555;")
        if fp.get("note"):
            ui.label(fp["note"]).style("color: #888; font-size: 0.8rem;")


def _render_optimisation_review(opt: dict[str, Any]) -> None:
    """Variables / objectives / constraints / strategy / budget + footprint."""
    _render_footprint(opt.get("footprint") or {})

    variables = opt.get("variables") or []
    if variables:
        ui.label("Design variables").style(_HEADING)
        with ui.column().classes("w-full gap-0"):
            for v in variables:
                _render_variable_row(v)

    objectives = opt.get("objectives") or []
    if objectives:
        ui.label("Objectives").style(_HEADING)
        with ui.column().classes("w-full gap-0"):
            for o in objectives:
                with ui.row().classes("w-full items-baseline gap-2 no-wrap"):
                    ui.label(o.get("label") or o.get("name") or "").style(_KEYCOL)
                    ui.label(_SENSE_LABELS.get(o.get("sense"), o.get("sense") or "")).style(
                        "font-weight: 500;"
                    )
                    if o.get("unit"):
                        ui.label(o["unit"]).style("color: #888;")

    constraints = opt.get("constraints") or []
    if constraints:
        ui.label("Constraints").style(_HEADING)
        with ui.column().classes("w-full gap-0"):
            for c in constraints:
                ui.label(_constraint_text(c)).style("font-weight: 500;")

    _render_strategy(opt.get("strategy") or {})
    _render_stopping(opt.get("stopping") or {})


def _constraint_text(c: dict[str, Any]) -> str:
    """One constraint as a plain-language line."""
    if c.get("kind") == "output" and c.get("op") is not None:
        unit = f" {c['unit']}" if c.get("unit") else ""
        return f"{c.get('label', '')} {c['op']} {_fmt_value(c.get('value'))}{unit}"
    if c.get("expression"):
        return f"{c.get('label', '')}: {c['expression']}"
    return str(c.get("label", ""))


def _render_strategy(strat: dict[str, Any]) -> None:
    if not strat:
        return
    ui.label("Strategy").style(_HEADING)
    with ui.column().classes("w-full gap-0"):
        _kv_row("Algorithm", strat.get("algorithm"))
        _kv_row("Evaluation mode", strat.get("run_mode"))
        _kv_row("Acquisition", strat.get("acquisition"))
        if strat.get("batch_size"):
            _kv_row("Batch size", str(strat["batch_size"]))
        _kv_row("Variable reduction", strat.get("variable_reduction"))


def _render_stopping(stop: dict[str, Any]) -> None:
    criteria = stop.get("criteria") or []
    if not criteria and not stop.get("seeding"):
        return
    ui.label("Run budget").style(_HEADING)
    with ui.column().classes("w-full gap-0"):
        for crit in criteria:
            ui.label(f"• {crit}").style("font-weight: 500;")
        if stop.get("seeding"):
            _kv_row("Seeding", stop["seeding"])


def _kv_row(label: str, value: Any) -> None:
    """An inline label · value row, skipped entirely when value is empty."""
    if value in (None, ""):
        return
    with ui.row().classes("w-full items-baseline gap-2 no-wrap"):
        ui.label(label).style(_KEYCOL)
        ui.label(str(value)).style("font-weight: 500;")


def _render_surrogate_detail(d: dict[str, Any]) -> None:
    """Review panel for a surrogate-model publish gate (full block, D5)."""
    sur = d.get("surrogate") or {}
    with ui.row().classes("w-full gap-6 flex-wrap"):
        _kv("Surrogate model", d.get("job_name", ""))
        _kv("Tool", d.get("tool_name") or "")
        _kv("Requested by", d.get("requestor_name", ""))
        _kv("Requested", format_user_datetime(d.get("requested_at")))
    if (d.get("justification") or "").strip():
        with ui.card().classes("w-full").style("background: rgba(0,0,0,0.03);"):
            ui.label("Requestor note").style("font-weight: 600; font-size: 0.85rem;")
            ui.label(d["justification"])

    with ui.column().classes("w-full gap-0"):
        _kv_row("Model type", sur.get("model_kind"))
        _kv_row("Predicts", ", ".join(sur.get("predicts") or []))
        if sur.get("training_samples") is not None:
            src = f" from {sur['training_source']}" if sur.get("training_source") else ""
            _kv_row("Training data", f"{sur['training_samples']} runs{src}")

    domain = sur.get("domain") or []
    if domain:
        ui.label("Validity domain").style(_HEADING)
        with ui.column().classes("w-full gap-0"):
            for v in domain:
                _render_variable_row(v)

    metrics = sur.get("metrics") or []
    if metrics:
        ui.label("Validation").style(_HEADING)
        with ui.column().classes("w-full gap-0"):
            for m in metrics:
                with ui.row().classes("w-full items-baseline gap-2 no-wrap"):
                    ui.label(m.get("response_label") or "").style(_KEYCOL)
                    ui.label(f"{m.get('metric', '')}: {_fmt_value(m.get('value'))}").style(
                        "font-weight: 500;"
                    )
                    if m.get("unit"):
                        ui.label(m["unit"]).style("color: #888;")


def _matrix_col_header(c: dict[str, Any]) -> str:
    return f"{c.get('label', '')} ({c['unit']})" if c.get("unit") else str(c.get("label", ""))


def _render_run_matrix(matrix: dict[str, Any]) -> None:
    """The condensed one-row-per-run review + per-run drill-in (§9).

    A scannable table (varied inputs + key outputs, labels + units) with
    failed / outlier rows flagged and per-output aggregates above it. One
    "Release results" decision covers every run; clicking a row opens that
    run's values in full.
    """
    cols = matrix.get("columns") or []
    rows = matrix.get("rows") or []
    total, failed = matrix.get("total", 0), matrix.get("failed", 0)
    ui.separator().style("margin: 0.6rem 0 0.2rem;")
    head = f"Runs — {total} total"
    if failed:
        head += f", {failed} failed"
    if matrix.get("hidden"):
        head += f", {matrix['hidden']} hidden"
    ui.label(head).style("font-size: 1.05rem; font-weight: 600;")
    ui.label("Approving releases every run's results; denying withholds them.").style(
        "color: #888; font-size: 0.8rem;"
    )

    for agg in matrix.get("aggregates") or []:
        if agg.get("mean") is None:
            continue
        unit = f" {agg['unit']}" if agg.get("unit") else ""
        ui.label(
            f"{agg['label']}: {_fmt_value(agg['minimum'])} to {_fmt_value(agg['maximum'])} "
            f"(mean {_fmt_value(agg['mean'])}){unit}"
        ).style("color: #555; font-size: 0.8rem;")

    table_cols: list[dict[str, Any]] = [
        {"name": "run", "label": "Run", "field": "run", "align": "left", "sortable": True},
        {"name": "state", "label": "State", "field": "state", "align": "left", "sortable": True},
        {"name": "flag", "label": "", "field": "flag", "align": "left"},
    ]
    table_cols += [
        {
            "name": c["key"],
            "label": _matrix_col_header(c),
            "field": c["key"],
            "align": "left",
            "sortable": True,
        }
        for c in cols
    ]
    table_rows = []
    for r in rows:
        flag = "⚠ failed" if r.get("failed") else ("⚠ outlier" if r.get("outlier") else "")
        row = {"run": r.get("label"), "state": r.get("state"), "flag": flag}
        cells = r.get("cells") or {}
        for c in cols:
            row[c["key"]] = _fmt_value(cells.get(c["key"]))
        table_rows.append(row)

    table = (
        ui.table(columns=table_cols, rows=table_rows, row_key="run")
        .classes("w-full")
        .props("dense flat bordered")
    )
    # Drill-in: a row click opens that run's values in full (no extra
    # fetch — the matrix already carries the run's cells).
    col_headers = {c["key"]: _matrix_col_header(c) for c in cols}

    def _on_row(e: Any) -> None:
        row = e.args[1] if isinstance(e.args, list) and len(e.args) > 1 else None
        if not isinstance(row, dict):
            return
        with ui.dialog() as dialog, ui.card().classes("w-full").style("max-width: 520px;"):
            ui.label(str(row.get("run", "Run"))).style("font-weight: 600;")
            ui.label(f"State: {row.get('state', '')}").style("color: #888; font-size: 0.8rem;")
            with ui.column().classes("w-full gap-0"):
                for key, header in col_headers.items():
                    with ui.row().classes("w-full items-baseline gap-2 no-wrap"):
                        ui.label(header).style(_KEYCOL)
                        ui.label(str(row.get(key, "—"))).style("font-weight: 500;")
            ui.button("Close", on_click=dialog.close).props("flat")
        dialog.open()

    table.on("rowClick", _on_row)
