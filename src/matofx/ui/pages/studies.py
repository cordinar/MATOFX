"""Parametric Studies UI (ADR-0001 / designs/parametric-studies.md §9).

Three surfaces, all behind the ``features.parametric_studies`` flag
(the nav entry + this page are absent when it's off, matching the API,
per design §12 — a disabled-feature visit redirects home):

- ``GET /studies`` — the workspace: your recent studies across visible
  projects + the create form (steps 1-6: tool → project → mode →
  factor table with a live count → plain-English confirm → submit).
- ``GET /studies/{id}`` — the study detail page. Progress tab only in
  2-PS-a (the Results explorer is 2-PS-b): live child-state breakdown
  with per-child retry + a study-level cancel, polled like a workflow
  run. Study children deliberately never appear in My Jobs (§9).

There is no legacy equivalent screen — parametric studies are new in
v2 (the legacy app only had an ad-hoc ``*_repeat`` tool). The polling
detail view mirrors :mod:`matofx.ui.pages.workflow_run`; the factor
editors mirror the submit-form field builders.
"""

from __future__ import annotations

import contextlib
import csv
import io
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import plotly.graph_objects as go
from nicegui import run, ui
from plotly.subplots import make_subplots

from matofx.studies._intsnap import snap_int
from matofx.studies.analysis import pareto_indices, summarize
from matofx.studies.surrogate import (
    Surrogate,
    SurrogateKind,
    fit_surrogate,
    optimize_surrogate,
    sobol_indices,
)
from matofx.ui._axis_picker import (
    guard_section_headers,
    is_section_header,
    sectioned_options,
)
from matofx.ui._categorical_axis import (
    UNSET_LEVEL,
    axis_categories,
    category_axis,
    category_colours,
    category_positions,
    stepped_colorscale,
)
from matofx.ui._features import parametric_studies_enabled, surrogate_family_enabled
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in, section_card
from matofx.ui.pages._input_groups import (
    OTHER_GROUP_LABEL,
    group_sections,
    has_groups,
    render_group_header,
)
from matofx.ui.state import current_token, has_permission
from matofx.ui.theme import themed_plotly

_PARAMETRIC_CREATE = "parametric_studies.create"
_REFRESH_INTERVAL_S = 2.5
_MIN_RSM_FACTORS = 2  # a response-surface contour needs two axes

_MODES: dict[str, str] = {
    "table": "Run table (exact configurations)",
    "list": "Value list (all combinations)",
    "grid": "Grid (full factorial)",
    "frac_factorial": "Fractional factorial",
    "plackett_burman": "Plackett-Burman",
    "lhs": "Latin hypercube",
    "sobol": "Sobol sequence",
    "random": "Random",
    "ccd": "Central composite (RSM)",
    "box_behnken": "Box-Behnken (RSM)",
}

_MODE_NOTES: dict[str, str] = {
    "table": "Enter each run as a row. Runs exactly the rows you specify - no "
    "automatic combinations. Use for specific named configurations.",
    "list": "Give a set of values for each chosen input; runs every combination "
    "(full factorial over your values). No ranges - list the actual values.",
    "grid": "Per numeric input: min/max + number of levels + a spacing (or an "
    "explicit value list); runs every combination of levels.",
    "frac_factorial": "A 2-level (low/high) screening design at a chosen "
    "resolution: find which inputs matter in few runs. Numeric inputs only.",
    "plackett_burman": "A compact 2-level screening design; run count is set "
    "automatically. Numeric inputs only.",
    "lhs": "Spreads N samples evenly across the space. Set the number of runs "
    "and a range per input.",
    "sobol": "Low-discrepancy space-filling; most even when the number of runs is a power of 2.",
    "random": "N independent random draws within each input's range.",
    "ccd": "Corner + axial + centre points to fit a curved response surface. Numeric inputs only.",
    "box_behnken": "3-level response-surface design; needs at least 3 numeric inputs.",
}

_SPACINGS: dict[str, str] = {
    "linear": "Linear (even)",
    "log_min": "Log - dense near min",
    "log_max": "Log - dense near max",
    "edges": "Dense at both ends",
    "center": "Dense in the middle",
}

_SPACE_FILLING = ("lhs", "sobol", "random")

# Which input-importance method leads the Results tab (the rest go under
# "Advanced statistics"). Designed studies with discrete factor levels read
# best through ANOVA; space-filling / table / list studies read best through
# the regression Sensitivity. Sobol is always supplementary (needs a surrogate).
_ANOVA_LEAD_MODES = frozenset({"grid", "frac_factorial", "plackett_burman", "ccd", "box_behnken"})

# Family grouping (design §3 / §9 — pre-b0-gamma): a mode's *family* decides
# the factor-row editor shape and the mode-params panel content. The
# universal Vary editor (slice 5) showed widgets that some modes
# ignore (levels/spacing for LHS, min/max for Run table) — the family
# split fixes that.
_FAMILY_OF: dict[str, str] = {
    "table": "table",
    "list": "list",
    "grid": "grid",
    "frac_factorial": "design",
    "plackett_burman": "design",
    "ccd": "design",
    "box_behnken": "design",
    "lhs": "sampling",
    "sobol": "sampling",
    "random": "sampling",
}

_DISTRIBUTIONS: dict[str, str] = {
    "uniform": "Uniform",
    "normal": "Normal",
    "loguniform": "Log-uniform",
}
_CCD_FACES: dict[str, str] = {
    "faced": "Faced (stays in range)",
    "circumscribed": "Circumscribed",
    "inscribed": "Inscribed",
}
_CCD_ALPHAS: dict[str, str] = {
    "orthogonal": "Orthogonal",
    "rotatable": "Rotatable",
}


def _family_of(mode: str) -> str:
    """Mode -> family slug (``table``/``list``/``grid``/``design``/``sampling``)."""
    return _FAMILY_OF.get(mode, "")


def _can_vary_in_family(ft: str | None, family: str) -> bool:
    """Per-family Vary eligibility (design §3 / §9.3 / capability matrix).

    Family D (screening/RSM) needs a continuous numeric range; every
    other type can only be Constant. Family E (space-filling) cannot
    sample files; numeric / categorical / bool may vary. table / list
    / grid accept any factor type.
    """
    if ft is None:
        return False
    if family == "design":
        return ft == "numeric"
    if family == "sampling":
        return ft != "file"
    return True


def _parse_values(text: str) -> list[float]:
    """Parse a comma-separated list of floats; raise ``ValueError`` on bad input."""
    out: list[float] = []
    for raw in (text or "").split(","):
        tok = raw.strip()
        if not tok:
            continue
        try:
            out.append(float(tok))
        except ValueError as exc:
            raise ValueError(f"'{tok}' is not a number.") from exc
    return out


_STATE_LABEL: dict[str, str] = {
    "draft": "Draft",
    "pending_approval": "Pending approval",
    "running": "Running",
    "complete": "Complete",
    "partial": "Partial",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "rejected": "Rejected",
}
# Vivid, distinct Quasar *palette* colours per state (not the muted brand
# semantic slots) so the chips read unmistakably — Complete green, Failed red,
# Running blue, etc. Built-in palette names, so they don't depend on the brand
# override and stay consistent with the Optimisation page's chips.
_STATE_COLOR: dict[str, str] = {
    "draft": "blue-grey",
    "pending_approval": "amber",
    "running": "blue",
    "complete": "green",
    "partial": "orange",
    "failed": "red",
    "cancelled": "grey",
    "rejected": "deep-orange",
}


def _state_chip(state: str) -> None:
    ui.chip(_STATE_LABEL.get(state, state.title())).props(
        f"color={_STATE_COLOR.get(state, 'grey')} text-color=white"
    )


def _client() -> ApiClient:
    return ApiClient(get_app(), token=current_token())


def _feature_ok() -> bool:
    """Redirect home when the feature is off (page *absent*, design §12)."""
    if not parametric_studies_enabled():
        ui.navigate.to("/home")
        return False
    return True


# ===================== workspace (list + create) =====================


@ui.page("/studies")
async def studies_page() -> None:
    """The Parametric Studies workspace — your studies + the create form."""
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    with ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;"):
        ui.label("Parametric Studies").style(
            "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
        )
        ui.label(
            "Fan one tool over a generated input matrix and explore the results together."
        ).style("color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;")
        # Without create rights there's no New tab to switch to, so a
        # viewer just gets the catalogue (the surface is feature-gated, not
        # permission-gated, so it's visible to read even when you can't add).
        if not has_permission(_PARAMETRIC_CREATE):
            await _render_your_studies(client)
            return
        with (
            ui.tabs()
            .classes("w-full")
            .props("align=left active-color=primary indicator-color=primary")
            .style("border-bottom: 1px solid var(--matofx-card-border);") as tabs
        ):
            catalogue_tab = ui.tab("Catalogue", icon="bar_chart").props("no-caps")
            new_tab = ui.tab("New Parametric Study", icon="add").props("no-caps")
        with (
            ui.tab_panels(tabs, value=catalogue_tab)
            .classes("w-full")
            .style("background-color: transparent;")
        ):
            with ui.tab_panel(catalogue_tab).style("background-color: transparent;"):
                await _render_your_studies(client)
            with ui.tab_panel(new_tab).style("background-color: transparent;"):
                await _render_create_form(client)


async def _render_your_studies(client: ApiClient) -> None:
    """A searchable, filterable list of studies across visible projects."""
    with section_card("bar_chart", "Your studies"):
        try:
            projects = await client.list_projects()
        except ApiError as exc:
            ui.label(f"Couldn't load projects: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return
        rows: list[dict[str, Any]] = []
        for proj in projects:
            try:
                rows.extend(await client.list_studies(int(proj["id"])))
            except ApiError:
                continue
        if not rows:
            ui.label("No studies yet. Create one below.").style("color: var(--matofx-muted-text);")
            return
        rows.sort(key=lambda s: str(s.get("created_at") or ""), reverse=True)

        # Search (name / tool / project / owner) + status filter, applied
        # client-side over every visible study (the list is already fetched).
        filt: dict[str, str] = {"q": "", "bucket": "all"}
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            search = (
                ui.input(label="Search", placeholder="name, tool, project, or owner…")
                .props("clearable dense outlined debounce=200")
                .style("flex: 1 1 260px; min-width: 220px;")
            )
            with search.add_slot("prepend"):
                ui.icon("search")
            ui.select(
                {
                    "all": "All",
                    "draft": "Draft",
                    "running": "Running",
                    "complete": "Complete",
                    "cancelled": "Cancelled",
                },
                value="all",
                label="Status",
            ).props("dense outlined").style("min-width: 11rem;").on_value_change(
                lambda e: _apply(str(e.value or "all"), None)
            )
        count_label = ui.label("").style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        list_box = ui.column().classes("w-full gap-1")

        def _refresh() -> None:
            list_box.clear()
            shown = [s for s in rows if _study_matches(s, filt["q"], filt["bucket"])]
            with list_box:
                if not shown:
                    ui.label("No studies match your search and filter.").style(
                        "color: var(--matofx-muted-text); padding: 0.5rem 0.2rem;"
                    )
                else:
                    for study in shown[:200]:
                        _render_study_row(study)
            count_label.text = f"{len(shown)} of {len(rows)} studies"

        def _apply(bucket: str | None, query: str | None) -> None:
            if bucket is not None:
                filt["bucket"] = bucket
            if query is not None:
                filt["q"] = query.strip().lower()
            _refresh()

        search.on_value_change(lambda e: _apply(None, str(e.value or "")))
        _refresh()


# Plain-English status buckets for the catalogue filter — each folds the
# related lifecycle states (5 buckets, confirmed 2026-06-25). ``all`` is the
# absence of a filter.
_STATE_BUCKETS: dict[str, set[str]] = {
    "draft": {"draft"},
    "running": {"running", "pending_approval"},
    "complete": {"complete", "partial"},
    "cancelled": {"cancelled", "rejected", "failed"},
}


def _study_haystack(study: dict[str, Any]) -> str:
    """Lower-cased search text: name + tool (label & alias) + project + owner + id."""
    sid = int(study.get("id") or 0)
    bits = [
        str(study.get("name") or ""),
        str(study.get("tool_name") or ""),
        str(study.get("tool_alias") or ""),
        str(study.get("project_name") or ""),
        str(study.get("created_by_name") or ""),
        f"#{sid}",
    ]
    return " ".join(bits).lower()


def _study_matches(study: dict[str, Any], query: str, bucket: str) -> bool:
    """Whether a study passes the catalogue search box + status filter."""
    if bucket != "all":
        allowed = _STATE_BUCKETS.get(bucket, set())
        if str(study.get("state") or "") not in allowed:
            return False
    return not query or query in _study_haystack(study)


def _render_study_row(study: dict[str, Any]) -> None:
    sid = int(study.get("id") or 0)
    name = str(study.get("name") or f"Study #{sid}")
    # Tool label, not alias ([[feedback_use_labels_not_names]]).
    tool = str(study.get("tool_name") or study.get("tool_alias") or "")
    project = str(study.get("project_name") or "")
    owner = str(study.get("created_by_name") or "")
    state = str(study.get("state") or "draft")
    generated = int(study.get("generated_count") or 0)
    skipped = int(study.get("skipped_invalid_count") or 0)
    with (
        ui.row()
        .classes("w-full items-center gap-3 p-2 rounded cursor-pointer")
        .style("background-color: var(--matofx-row-bg);")
        .on("click", lambda _e=None, _id=sid: ui.navigate.to(f"/studies/{_id}"))
    ):
        with ui.column().classes("gap-0 flex-grow").style("min-width: 0;"):
            ui.label(name).style("font-weight: 600;")
            bits = [b for b in (tool, project, f"by {owner}" if owner else "") if b]
            if generated or skipped:
                extra = f" ({skipped} skipped)" if skipped else ""
                bits.append(f"{generated} runs{extra}")
            ui.label(" · ".join(bits)).style(
                "color: var(--matofx-muted-text); font-size: 0.8rem; "
                "white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
        _state_chip(state)


def _factor_type(ftype: str) -> str | None:
    """Map a descriptor input type to a study factor type.

    ``None`` = constant-only: a free string / table input can't be a
    sweep axis.
    """
    if ftype in ("integer", "float"):
        return "numeric"
    if ftype in ("select", "multiselect"):
        return "select"
    if ftype == "boolean":
        return "bool"
    if ftype == "file":
        return "file"
    return None


async def _render_create_form(client: ApiClient) -> None:
    """Steps 1-6: tool -> project -> mode -> factors -> preview -> submit."""
    with section_card("add", "New parametric study"):
        try:
            projects = await client.list_projects()
            tools = await client.list_tools()
        except ApiError as exc:
            ui.label(f"Couldn't load the form: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return
        if not projects:
            ui.label("You need a project first - create one from Projects.").style("color: #888;")
            return
        if not tools:
            ui.label("No tools are available to you yet.").style("color: #888;")
            return

        project_opts = {int(p["id"]): str(p.get("name") or p["id"]) for p in projects}
        tool_opts = {str(t["alias"]): str(t.get("name") or t["alias"]) for t in tools}

        name_in = ui.input("Study name *").classes("w-full")
        project_sel = ui.select(project_opts, label="Project *").classes("w-full")
        tool_sel = ui.select(tool_opts, label="Tool *").classes("w-full")
        mode_sel = ui.select(dict(_MODES), label="Sampling mode *", value="grid").classes("w-full")
        mode_note = ui.label("").style("color: #888; font-size: 0.85rem;")
        greyed_note = ui.label("").style("color: #888; font-size: 0.8rem;")

        factor_box = ui.column().classes("w-full gap-2")
        factors: dict[str, dict[str, Any]] = {}
        mp_box = ui.column().classes("w-full gap-2")
        mp_widgets: dict[str, Any] = {}
        table_box = ui.column().classes("w-full gap-1")
        preview_lbl = ui.label("").style("color: #888; font-size: 0.9rem;")
        message = ui.label("").style("color: var(--matofx-error-text);")
        ctx: dict[str, Any] = {
            "count_seq": 0,
            "run_rows": [],
            "inputs": [],
            "family": None,
        }

        def _arm_count() -> None:
            ctx["count_seq"] += 1
            seq = ctx["count_seq"]

            async def _fire() -> None:
                if seq == ctx["count_seq"]:
                    await _refresh_count()

            ui.timer(0.6, _fire, once=True)

        def _seed() -> int | None:
            w = mp_widgets.get("seed")
            v = w.value if w is not None else None
            return int(v) if v is not None and str(v) != "" else None

        def _spec_and_sampling() -> tuple[dict[str, Any], dict[str, Any]]:
            mode = str(mode_sel.value or "")
            family = _family_of(mode)
            spec = _build_factor_spec(factors, family)
            sampling = _build_sampling_params(factors, family, mode, mp_widgets, ctx)
            return spec, sampling

        async def _refresh_count() -> None:
            alias, mode = tool_sel.value, mode_sel.value
            if not alias or not mode:
                preview_lbl.text = "Pick a tool and a mode."
                return
            try:
                spec, sampling = _spec_and_sampling()
            except ValueError as exc:
                preview_lbl.text = str(exc)
                return
            try:
                res = await client.preview_factor_spec(
                    tool_alias=str(alias),
                    mode=str(mode),
                    factor_spec=spec,
                    sampling_params=sampling,
                    seed=_seed(),
                )
            except ApiError as exc:
                preview_lbl.text = exc.detail or "Preview failed."
                return
            preview_lbl.text = _count_text(res)

        async def _open_preview() -> None:
            alias, mode = tool_sel.value, mode_sel.value
            if not alias or not mode:
                ui.notify("Pick a tool and a mode first.", type="warning")
                return
            try:
                spec, sampling = _spec_and_sampling()
            except ValueError as exc:
                ui.notify(str(exc), type="warning")
                return
            try:
                res = await client.preview_factor_spec(
                    tool_alias=str(alias),
                    mode=str(mode),
                    factor_spec=spec,
                    sampling_params=sampling,
                    seed=_seed(),
                )
            except ApiError as exc:
                ui.notify(exc.detail or "Preview failed.", type="negative")
                return
            _show_preview_modal(res, list(ctx.get("inputs") or []))

        def _on_role_change() -> None:
            """Vary set changed - re-render the run-table for family A."""
            if ctx.get("family") == "table":
                _rebuild_run_table(ctx, factors, table_box, "table", _arm_count)
            _arm_count()

        def _rebuild_factor_rows() -> None:
            factors.clear()
            factor_box.clear()
            family = str(ctx.get("family") or "")
            inputs = list(ctx.get("inputs") or [])
            sections = group_sections(inputs, ctx.get("input_groups"))
            show_headers = has_groups(sections)
            with factor_box:
                ui.label("Factors").style("font-weight: 600;")
                ui.label("Hold an input constant, or vary it across the sweep.").style(
                    "color: #888; font-size: 0.85rem;"
                )
                for header, members in sections:
                    if show_headers:
                        render_group_header(header or OTHER_GROUP_LABEL)
                    for inp in members:
                        if isinstance(inp, dict):
                            _render_factor_row(inp, factors, family, _arm_count, _on_role_change)

        def _rebuild_mode_params() -> None:
            mp_widgets.clear()
            mp_box.clear()
            mode = str(mode_sel.value or "")
            family = _family_of(mode)
            with mp_box:
                if family == "design":
                    _render_design_params(mode, mp_widgets, _arm_count)
                elif family == "sampling":
                    _render_sampling_params(mode, mp_widgets, _arm_count)

        def _on_mode() -> None:
            mode = str(mode_sel.value or "")
            mode_note.text = _MODE_NOTES.get(mode, "")
            family = _family_of(mode)
            if family != ctx.get("family"):
                ctx["family"] = family
                _rebuild_factor_rows()
            _rebuild_mode_params()
            if family == "table":
                _rebuild_run_table(ctx, factors, table_box, "table", _arm_count)
            else:
                ctx["run_rows"] = []
                table_box.clear()
            _arm_count()

        async def _load_factors() -> None:
            ctx["family"] = None  # force a family rebuild on the next _on_mode
            factors.clear()
            factor_box.clear()
            ctx["run_rows"] = []
            table_box.clear()
            mp_widgets.clear()
            mp_box.clear()
            alias = tool_sel.value
            if not alias:
                return
            try:
                tool = await client.get_tool(str(alias))
                avail = await client.study_available_modes(str(alias))
            except ApiError as exc:
                with factor_box:
                    ui.label(f"Couldn't load the tool: {exc.detail}").style(
                        "color: var(--matofx-error-text);"
                    )
                return
            allowed = {m: lbl for m, lbl in _MODES.items() if avail.get(m) is None}
            mode_sel.options = allowed or dict(_MODES)
            if mode_sel.value not in (allowed or _MODES):
                mode_sel.value = next(iter(allowed or _MODES))
            mode_sel.update()
            greyed = [f"{_MODES[m]}: {r}" for m, r in avail.items() if r and m in _MODES]
            greyed_note.text = (
                ("Unavailable for this tool - " + " | ".join(greyed)) if greyed else ""
            )
            descriptor = tool.get("descriptor") or {}
            ctx["inputs"] = descriptor.get("inputs") or []
            ctx["input_groups"] = descriptor.get("input_groups") or []
            _on_mode()

        async def _submit() -> None:
            message.text = ""
            name = (name_in.value or "").strip()
            if not name:
                message.text = "Give the study a name."
                return
            if not project_sel.value:
                message.text = "Pick a project."
                return
            if not tool_sel.value or not mode_sel.value:
                message.text = "Pick a tool and a sampling mode."
                return
            try:
                spec, sampling = _spec_and_sampling()
            except ValueError as exc:
                message.text = str(exc)
                return

            # Lock the controls and show progress while the study is created
            # and submitted — this can take a moment (generation + queueing),
            # and a double-click would create a duplicate study.
            def _reset_controls() -> None:
                submit_spinner.set_visibility(False)
                preview_btn.enable()
                submit_btn.enable()

            submit_spinner.set_visibility(True)
            preview_btn.disable()
            submit_btn.disable()
            try:
                study = await client.create_study(
                    int(project_sel.value),
                    name=name,
                    tool_alias=str(tool_sel.value),
                    mode=str(mode_sel.value),
                    factor_spec=spec,
                    sampling_params=sampling,
                    seed=_seed(),
                )
                submitted = await client.submit_study(int(study["id"]))
            except ApiError as exc:
                message.text = exc.detail or "Couldn't create the study."
                _reset_controls()
                return
            except Exception:
                message.text = "Couldn't create the study — an unexpected error occurred."
                _reset_controls()
                return
            # Success: leave the controls disabled (we navigate away) so the
            # form can't be resubmitted during the redirect.
            ui.notify("Study submitted.", type="positive")
            ui.navigate.to(f"/studies/{int(submitted['id'])}")

        tool_sel.on(
            "update:model-value",
            lambda _e=None: ui.timer(0.0, _load_factors, once=True),
        )
        mode_sel.on("update:model-value", lambda _e=None: _on_mode())

        with ui.row().classes("w-full justify-end gap-2 items-center"):
            submit_spinner = ui.spinner(size="sm").props("color=primary")
            submit_spinner.set_visibility(False)
            preview_btn = ui.button("Preview", icon="calculate", on_click=_open_preview).props(
                "flat"
            )
            submit_btn = ui.button("Create & submit", icon="send", on_click=_submit).props(
                "color=primary"
            )


def _count_text(res: dict[str, Any]) -> str:
    gen = int(res.get("generated_count") or 0)
    skipped = int(res.get("skipped_invalid_count") or 0)
    text = f"This study would run {gen} simulation(s)."
    if res.get("over_cap"):
        text += f" Over the cap of {res.get('run_cap')} - reduce the sweep."
    if skipped:
        why = next(iter(res.get("skip_reasons") or {}), "cross-field constraints")
        text += f" {skipped} point(s) skipped because '{why}'."
    for w in res.get("level_warnings") or []:
        text += f" {w}"
    return text


def _input_header(inp: dict[str, Any] | None, name: str) -> str:
    """Friendly column header: ``label (unit)`` if either is set, else name."""
    if inp is None:
        return name
    label = str(inp.get("label") or name)
    unit = inp.get("unit")
    return f"{label} ({unit})" if unit else label


def _factor_label(name: str, st: dict[str, Any]) -> str:
    """The user-facing name for a factor: ``label (unit)``, never the alias.

    ``_render_factor_row`` stashes the rendered header on the factor
    state so every downstream surface — the run-table columns, the
    validation messages — says the same thing the factor list said.
    Falls back to the descriptor name only when a caller built the
    state dict by hand (the pure builder tests do).
    """
    return str(st.get("label") or name)


def _ordered_cols(inputs: list[dict[str, Any]], present: set[str]) -> list[str]:
    """Column order = descriptor input order, intersected with what's in the points."""
    inp_names = [str(i.get("name") or "") for i in inputs if isinstance(i, dict)]
    cols = [n for n in inp_names if n in present]
    # Defensive: include any point key the descriptor didn't list (shouldn't happen).
    cols += [k for k in sorted(present) if k not in cols]
    return cols


def _runs_csv(inputs: list[dict[str, Any]], points: list[dict[str, Any]]) -> bytes:
    """Serialise the preview matrix to CSV with descriptor-ordered label headers."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    inp_by_name = {str(i.get("name") or ""): i for i in inputs if isinstance(i, dict)}
    present = set(points[0].keys()) if points else set()
    cols = _ordered_cols(inputs, present)
    writer.writerow([_input_header(inp_by_name.get(c), c) for c in cols])
    for p in points:
        writer.writerow(["" if p.get(c) is None else p.get(c) for c in cols])
    return buf.getvalue().encode("utf-8")


def _show_preview_modal(res: dict[str, Any], inputs: list[dict[str, Any]]) -> None:
    """The full run matrix (design §9.5) - or the refusal when over cap.

    Beyond the table, a **Plot** tab charts the generated points over up
    to four input variables at once (X / Y / optional Z + optional
    colour), so you can *see* how the inputs actually vary before you
    commit the runs — the quickest way to spot a lop-sided sampling plan.
    """
    with ui.dialog() as dialog, ui.card().style("width: 880px; max-width: 95vw;"):
        ui.label("Preview").style("font-size: 1.1rem; font-weight: 600;")
        ui.label(_count_text(res)).style("color: #888;")
        points = list(res.get("points") or [])
        if res.get("over_cap") or not points:
            if res.get("over_cap"):
                ui.label(
                    f"{res.get('generated_count')} runs exceed the cap of "
                    f"{res.get('run_cap')}. Reduce the ranges/levels or ask an "
                    "administrator to raise the limit."
                ).style("color: var(--matofx-error-text);")
        else:
            inp_by_name = {str(i.get("name") or ""): i for i in inputs if isinstance(i, dict)}
            present = set(points[0].keys())
            cols = _ordered_cols(inputs, present)
            with ui.tabs().props("dense align=left") as ptabs:
                table_tab = ui.tab("Table", icon="table_rows").props("no-caps")
                plot_tab = ui.tab("Plot", icon="scatter_plot").props("no-caps")
            with ui.tab_panels(ptabs, value=table_tab).classes("w-full"):
                with ui.tab_panel(table_tab):
                    ui.table(
                        columns=[
                            {"name": c, "label": _input_header(inp_by_name.get(c), c), "field": c}
                            for c in cols
                        ],
                        rows=[{c: p.get(c) for c in cols} for p in points],
                    ).classes("w-full").style("max-height: 58vh;")
                with ui.tab_panel(plot_tab):
                    _render_preview_plot(points, inputs)
        with ui.row().classes("w-full justify-end gap-2"):
            if points:
                csv_bytes = _runs_csv(inputs, points)
                ui.button(
                    "Download CSV",
                    icon="download",
                    on_click=lambda b=csv_bytes: ui.download.content(b, "preview-runs.csv"),
                ).props("flat")
            ui.button("Close", on_click=dialog.close).props("flat")
    dialog.open()


_PREVIEW_PLOT_W = 820
"""Fixed width (px) of the preview scatter — a modal has no stable width to
size a responsive Plotly chart against (see :func:`_draw_preview_scatter`)."""


def _is_number(v: Any) -> bool:
    """True for a real number (bools excluded — they aren't plot axes)."""
    return isinstance(v, int | float) and not isinstance(v, bool)


def _as_float(v: Any) -> float | None:
    """A plottable float, or ``None`` for a bool / non-number / missing value."""
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _static_plotly(fig: go.Figure) -> dict[str, Any]:
    """Themed Plotly payload with ``responsive`` turned off.

    For fixed-size charts in a modal, where a responsive chart would loop
    against the dialog's still-settling width (see
    :func:`_draw_preview_scatter`).
    """
    payload: dict[str, Any] = dict(themed_plotly(fig))
    cfg_raw = payload.get("config")
    cfg: dict[str, Any] = dict(cfg_raw) if isinstance(cfg_raw, dict) else {}
    cfg["responsive"] = False
    payload["config"] = cfg
    return payload


def _render_preview_plot(points: list[dict[str, Any]], inputs: list[dict[str, Any]]) -> None:
    """X / Y / optional Z + optional colour scatter over the previewed points.

    Only numeric inputs are offerable as axes. X/Y default to the two
    inputs that actually vary across the plan, so the chart lands on the
    sweep dimensions rather than held-constant columns.
    """
    inp_by_name = {str(i.get("name") or ""): i for i in inputs if isinstance(i, dict)}
    present = set(points[0].keys()) if points else set()
    cols = _ordered_cols(inputs, present)
    numeric = [c for c in cols if all(_is_number(p.get(c)) for p in points)]
    if len(numeric) < 2:
        ui.label(
            "Plotting needs at least two numeric inputs in the run matrix. "
            "Categorical, boolean and file inputs aren't plottable here."
        ).style("color: #888;")
        return
    labels = {c: _input_header(inp_by_name.get(c), c) for c in numeric}
    # Prefer columns that vary for the default X/Y (constants are dull axes).
    varying = [c for c in numeric if len({p.get(c) for p in points}) > 1]
    default_x = (varying + numeric)[0]
    default_y = ([c for c in (varying + numeric) if c != default_x] or numeric)[0]
    none_opt = {"": "— none —"}

    _mw = "min-width:150px"
    with ui.row().classes("w-full items-center gap-2 flex-wrap"):
        x_sel = ui.select(labels, value=default_x, label="X").props("dense").style(_mw)
        y_sel = ui.select(labels, value=default_y, label="Y").props("dense").style(_mw)
        zlabels = {**none_opt, **labels}
        z_sel = ui.select(zlabels, value="", label="Z (3D)").props("dense").style(_mw)
        c_sel = ui.select(zlabels, value="", label="Colour").props("dense").style(_mw)
    plot_box = ui.column().classes("w-full")

    def _redraw(_e: Any = None) -> None:
        _draw_preview_scatter(
            plot_box,
            points,
            labels,
            x=str(x_sel.value or ""),
            y=str(y_sel.value or ""),
            z=str(z_sel.value or ""),
            colour=str(c_sel.value or ""),
        )

    for w in (x_sel, y_sel, z_sel, c_sel):
        w.on("update:model-value", _redraw)
    _redraw()


def _spread_overlaps(
    xs: list[float], ys: list[float], keys: list[tuple[float, ...]]
) -> tuple[list[float], list[float], bool]:
    """Fan points that coincide (by ``keys``) into a small ring in X-Y.

    Returns ``(x, y, applied)``. When no two points share a key the inputs
    come back unchanged with ``applied=False`` — space-filling plans never
    coincide, so they are left alone. Coincident points (a factorial's
    stacked runs) are placed on a ring ~1.8% of each axis span across, big
    enough to separate their colours without meaningfully moving them.
    """
    counts: dict[tuple[float, ...], int] = {}
    for k in keys:
        counts[k] = counts.get(k, 0) + 1
    if all(v == 1 for v in counts.values()):
        return xs, ys, False
    spanx = (max(xs) - min(xs)) or abs(xs[0]) or 1.0
    spany = (max(ys) - min(ys)) or abs(ys[0]) or 1.0
    rx, ry = 0.018 * spanx, 0.018 * spany
    seen: dict[tuple[float, ...], int] = {}
    jx: list[float] = []
    jy: list[float] = []
    for xv, yv, k in zip(xs, ys, keys, strict=True):
        n = counts[k]
        m = seen.get(k, 0)
        seen[k] = m + 1
        if n == 1:
            jx.append(xv)
            jy.append(yv)
        else:
            ang = 2.0 * math.pi * m / n
            jx.append(xv + rx * math.cos(ang))
            jy.append(yv + ry * math.sin(ang))
    return jx, jy, True


def _draw_preview_scatter(
    container: ui.column,
    points: list[dict[str, Any]],
    labels: dict[str, str],
    *,
    x: str,
    y: str,
    z: str,
    colour: str,
) -> None:
    """Render the preview scatter into ``container`` for the chosen axes."""
    container.clear()
    with container:
        if not x or not y:
            ui.label("Pick X and Y variables.").style("color: #888;")
            return
        xs: list[float] = []
        ys: list[float] = []
        zs: list[float] = []
        cs: list[float] = []
        idx: list[int] = []
        for i, p in enumerate(points, start=1):
            xv, yv = _as_float(p.get(x)), _as_float(p.get(y))
            zv = _as_float(p.get(z)) if z else None
            if xv is None or yv is None or (z and zv is None):
                continue
            xs.append(xv)
            ys.append(yv)
            if z and zv is not None:
                zs.append(zv)
            cv = _as_float(p.get(colour)) if colour else None
            cs.append(cv if cv is not None else 0.0)
            idx.append(i)
        if not xs:
            ui.label("No points have numeric values for the chosen variables.").style(
                "color: #888;"
            )
            return
        xlab, ylab = labels.get(x, x), labels.get(y, y)
        xf, yf = _decimals_for(xs), _decimals_for(ys)
        cf = _decimals_for(cs)
        # A full-factorial (grid) puts many runs at the SAME plotted position —
        # they differ only in the inputs that aren't on the axes — so markers
        # coincide and only the top point's colour shows. Fan any coincident
        # points into a small ring so every colour is visible; the hover still
        # reports the true (un-jittered) values, carried in customdata.
        keys: list[tuple[float, ...]]
        if z:
            keys = [
                (round(a, 9), round(b, 9), round(c, 9)) for a, b, c in zip(xs, ys, zs, strict=True)
            ]
        else:
            keys = [(round(a, 9), round(b, 9)) for a, b in zip(xs, ys, strict=True)]
        jx, jy, jittered = _spread_overlaps(xs, ys, keys)
        customdata: list[list[float]] = []
        for k, i in enumerate(idx):
            row = [float(i), xs[k], ys[k]]
            if colour:
                row.append(cs[k])
            customdata.append(row)
        ht_colour = f"<br>{labels.get(colour, colour)}: %{{customdata[3]:.{cf}f}}" if colour else ""
        base = 5 if z else 8
        marker: dict[str, Any]
        if colour:
            marker = {
                "size": base,
                "color": cs,
                "colorscale": "Viridis",
                "showscale": True,
                "colorbar": {"title": labels.get(colour, colour)},
                "opacity": 0.9,
            }
        else:
            marker = {"size": base, "opacity": 0.9}
        if z:
            zlab = labels.get(z, z)
            zf = _decimals_for(zs)
            hovertemplate = (
                f"<b>Point %{{customdata[0]:d}}</b>"
                f"<br>{xlab}: %{{customdata[1]:.{xf}f}}"
                f"<br>{ylab}: %{{customdata[2]:.{yf}f}}"
                f"<br>{zlab}: %{{z:.{zf}f}}{ht_colour}<extra></extra>"
            )
            fig = go.Figure(
                go.Scatter3d(
                    x=jx,
                    y=jy,
                    z=zs,
                    mode="markers",
                    marker=marker,
                    customdata=customdata,
                    hovertemplate=hovertemplate,
                )
            )
            fig.update_layout(
                scene={"xaxis_title": xlab, "yaxis_title": ylab, "zaxis_title": zlab},
                width=_PREVIEW_PLOT_W,
                height=520,
                autosize=False,
                margin={"l": 0, "r": 0, "t": 30, "b": 0},
            )
        else:
            hovertemplate = (
                f"<b>Point %{{customdata[0]:d}}</b>"
                f"<br>{xlab}: %{{customdata[1]:.{xf}f}}"
                f"<br>{ylab}: %{{customdata[2]:.{yf}f}}{ht_colour}<extra></extra>"
            )
            fig = go.Figure(
                go.Scatter(
                    x=jx,
                    y=jy,
                    mode="markers",
                    marker=marker,
                    customdata=customdata,
                    hovertemplate=hovertemplate,
                )
            )
            fig.update_layout(
                xaxis_title=xlab,
                yaxis_title=ylab,
                width=_PREVIEW_PLOT_W,
                height=470,
                autosize=False,
                margin={"l": 70, "r": 20, "t": 30, "b": 50},
            )
        # A modal has no definite width until it settles, so a responsive Plotly
        # chart inside it feedback-loops — the dialog creeps wider each frame and
        # the 3-D camera never gets a stable canvas to orbit. Pin a fixed pixel
        # size + turn responsiveness off, and let it scroll on a narrow viewport.
        with ui.element("div").style("width: 100%; overflow-x: auto;"):
            ui.plotly(_static_plotly(fig)).style(f"width: {_PREVIEW_PLOT_W}px;")
        if jittered:
            ui.label(
                "Runs that share the plotted axes (differing only on the inputs "
                "not shown) are fanned out slightly so each colour is visible — "
                "hover reports the true values."
            ).style("color: #888; font-size: 0.78rem;")


def _render_factor_row(
    inp: dict[str, Any],
    factors: dict[str, dict[str, Any]],
    family: str,
    on_change: Any,
    on_role_change: Any,
) -> None:
    """One descriptor input as a Constant | Vary row (design §9 step 4).

    Editor shape is *family-aware* (pre-b0-gamma): A=Column marker;
    B=value list; C=By range OR By values per numeric factor;
    D=min/max only (numeric only — categorical/file Vary disabled);
    E=min/max or distribution (file Vary disabled).
    """
    name = str(inp.get("name") or "")
    label = _input_header(inp, name)  # "label (unit)" so the factor list shows units
    ftype = str(inp.get("type") or "string")
    options = list(inp.get("options") or [])
    default = inp.get("default")
    ft = _factor_type(ftype)
    can_vary = _can_vary_in_family(ft, family)

    # ``label`` rides along so the run-table headers and the validation
    # messages can name the factor the way the user knows it.
    state: dict[str, Any] = {"type": ft, "ftype": ftype, "label": label}
    factors[name] = state

    with ui.row().classes("w-full items-center gap-2"):
        ui.label(label).style("flex: 0 0 240px;")
        role = ui.select(
            ["Constant", "Vary"] if can_vary else ["Constant"],
            value="Constant",
        ).props("dense")
        editor_box = ui.row().classes("items-center gap-2 flex-grow")

    def _rebuild() -> None:
        editor_box.clear()
        state["role"] = role.value
        # Wipe any stale per-family widget refs before re-creating.
        for k in (
            "const",
            "values_text",
            "subset",
            "file_refs",
            "min",
            "max",
            "levels",
            "spacing",
            "by_select",
            "dist_kind",
            "mean",
            "std",
            "low",
            "high",
        ):
            state.pop(k, None)
        with editor_box:
            if role.value != "Vary":
                state["const"] = _const_widget(ftype, options, default)
            elif not can_vary:
                pass  # role select only offered Constant
            else:
                _render_vary_editor(state, inp, options, family, on_change)
        on_change()

    def _on_role(_e: Any = None) -> None:
        _rebuild()
        on_role_change()

    role.on("update:model-value", _on_role)
    _rebuild()


def _render_vary_editor(
    state: dict[str, Any],
    inp: dict[str, Any],
    options: list[Any],
    family: str,
    on_change: Any,
) -> None:
    """Dispatch to the per-family Vary editor for one factor row."""
    if family == "table":
        ui.label("(run-table column - values come from the rows below)").style(
            "color: #888; font-size: 0.85rem;"
        )
        return
    if family == "list":
        _render_list_vary(state, options, on_change)
    elif family == "grid":
        _render_grid_vary(state, inp, options, on_change)
    elif family == "design":
        _render_design_vary(state, inp, on_change)
    else:  # sampling
        _render_sampling_vary(state, inp, options, on_change)


def _int_number_kw(inp: dict[str, Any]) -> dict[str, Any]:
    """``ui.number`` kwargs that pin an integer input to whole values.

    Step 1, no decimals for an integer descriptor input; empty for a float input.
    Keeps a whole-number factor (e.g. *number of fins*) from accepting a
    fractional bound in the form.
    """
    return {"step": 1, "format": "%.0f"} if inp.get("type") == "integer" else {}


def _bounds_number_kw(inp: dict[str, Any], *, positive: bool = False) -> dict[str, Any]:
    """``ui.number`` kwargs that pin a range field to the input's declared bounds.

    Combines the integer-step kwargs with ``min``/``max`` clamps taken
    from the descriptor's declared ``minimum``/``maximum`` so a sweep
    range can't be typed outside the values the tool accepts — the UI
    half of the bounds rule that stops the LHS "all points on one edge"
    pile-up ([[project_field_surrogate_data_quality]] sibling). A field
    with no declared bound is left free. ``positive`` keeps a log-uniform
    ``low`` strictly above zero when the field floor is itself ``<= 0``.
    """
    kw: dict[str, Any] = dict(_int_number_kw(inp))
    lo = inp.get("minimum")
    hi = inp.get("maximum")
    if hi is not None:
        kw["max"] = hi
    if positive:
        kw["min"] = lo if isinstance(lo, int | float) and lo > 0 else 1e-12
    elif lo is not None:
        kw["min"] = lo
    return kw


def _snap_int_value(value: Any, st: dict[str, Any]) -> Any:
    """Snap a numeric value to a whole number for an integer factor.

    Rounds half away from zero when the factor's descriptor type is integer;
    passes everything else through unchanged (so a select / bool / float constant
    is untouched).
    """
    if (
        st.get("ftype") == "integer"
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return snap_int(value)
    return value


def _render_list_vary(state: dict[str, Any], options: list[Any], on_change: Any) -> None:
    """Family B (``list``): explicit value sets per factor (no ranges)."""
    ft = state["type"]
    if ft == "numeric":
        vals_w = ui.input("Values (comma-separated)").classes("flex-grow").props("dense")
        state["values_text"] = vals_w
        vals_w.on("update:model-value", lambda _e=None: on_change())
    elif ft == "select":
        sub_w = ui.select(options, multiple=True, value=list(options)).props("dense")
        state["subset"] = sub_w
        sub_w.on("update:model-value", lambda _e=None: on_change())
    elif ft == "bool":
        ui.label("varies over true / false").style("color: #888; font-size: 0.85rem;")
    else:  # file
        _render_file_list_vary(state, on_change)


def _render_file_list_vary(state: dict[str, Any], on_change: Any) -> None:
    """File factor Vary editor (list / grid): a set of file references.

    Files vary over a discrete set — the tool runs once per reference,
    crossed with the other factors. References are entered the same way
    the run table takes them (free text: a library CAS hash or a path),
    comma-separated here.
    """
    w = ui.input("File references (comma-separated)").classes("flex-grow").props("dense")
    state["file_refs"] = w
    w.on("update:model-value", lambda _e=None: on_change())
    ui.label("one CAS hash or path per comma; the study runs once per file").style(
        "color: #888; font-size: 0.8rem;"
    )


def _render_grid_vary(
    state: dict[str, Any], inp: dict[str, Any], options: list[Any], on_change: Any
) -> None:
    """Family C (``grid``): per numeric factor, ``By range`` or ``By values``."""
    ft = state["type"]
    if ft == "numeric":
        by = ui.select(["By range", "By values"], value="By range").props("dense")
        state["by_select"] = by
        sub = ui.row().classes("items-center gap-2")

        def _rebuild_num() -> None:
            sub.clear()
            for k in ("min", "max", "levels", "spacing", "values_text"):
                state.pop(k, None)
            with sub:
                if str(by.value) == "By values":
                    w = ui.input("Values (comma-separated)").classes("flex-grow").props("dense")
                    state["values_text"] = w
                    w.on("update:model-value", lambda _e=None: on_change())
                else:
                    bkw = _bounds_number_kw(inp)
                    lo = ui.number("min", value=inp.get("minimum"), **bkw).props("dense")
                    hi = ui.number("max", value=inp.get("maximum"), **bkw).props("dense")
                    lv = ui.number("levels", value=3, min=2, step=1, format="%.0f").props("dense")
                    sp = ui.select(dict(_SPACINGS), value="linear", label="spacing").props("dense")
                    state["min"], state["max"] = lo, hi
                    state["levels"], state["spacing"] = lv, sp
                    if _int_number_kw(inp):
                        ui.label("whole numbers").style("color: #888; font-size: 0.8rem;")
                    grid_widgets: list[Any] = [lo, hi, lv, sp]
                    for gw in grid_widgets:
                        gw.on("update:model-value", lambda _e=None: on_change())
            on_change()

        by.on("update:model-value", lambda _e=None: _rebuild_num())
        _rebuild_num()
    elif ft == "select":
        w = ui.select(options, multiple=True, value=list(options)).props("dense")
        state["subset"] = w
        w.on("update:model-value", lambda _e=None: on_change())
    elif ft == "bool":
        ui.label("varies over true / false").style("color: #888; font-size: 0.85rem;")
    else:  # file
        _render_file_list_vary(state, on_change)


def _render_design_vary(state: dict[str, Any], inp: dict[str, Any], on_change: Any) -> None:
    """Family D (screening / RSM): min + max only — no levels, no spacing.

    Only numeric Vary reaches here (``_can_vary_in_family`` blocks the rest).
    """
    bkw = _bounds_number_kw(inp)
    lo = ui.number("min", value=inp.get("minimum"), **bkw).props("dense")
    hi = ui.number("max", value=inp.get("maximum"), **bkw).props("dense")
    state["min"], state["max"] = lo, hi
    if _int_number_kw(inp):
        ui.label("whole numbers").style("color: #888; font-size: 0.8rem;")
    for w in (lo, hi):
        w.on("update:model-value", lambda _e=None: on_change())


def _render_sampling_distribution(
    state: dict[str, Any], inp: dict[str, Any], on_change: Any
) -> None:
    """The ``By distribution`` sub-editor: uniform / normal / log-uniform.

    ``normal`` is truncated to the input's declared bounds at sample time
    (:func:`matofx.studies.generate._inverse_cdf`), so it needs no
    explicit range here; uniform / log-uniform carry min/max clamped to
    the input's bounds.
    """
    dk = ui.select(dict(_DISTRIBUTIONS), value="uniform", label="Distribution").props("dense")
    state["dist_kind"] = dk
    sub = ui.row().classes("items-center gap-2")

    def _rebuild_num() -> None:
        sub.clear()
        for k in ("min", "max", "mean", "std", "low", "high"):
            state.pop(k, None)
        with sub:
            d = str(dk.value or "uniform")
            if d == "normal":
                mean_default = (
                    inp.get("default") if isinstance(inp.get("default"), int | float) else 0
                )
                mean = ui.number("mean", value=mean_default).props("dense")
                std = ui.number("std", value=1, min=1e-9).props("dense")
                state["mean"], state["std"] = mean, std
                for w_ in (mean, std):
                    w_.on("update:model-value", lambda _e=None: on_change())
            elif d == "loguniform":
                bkw = _bounds_number_kw(inp, positive=True)
                lo = ui.number("low (>0)", value=inp.get("minimum") or 1, **bkw).props("dense")
                hi = ui.number("high", value=inp.get("maximum") or 10, **bkw).props("dense")
                state["low"], state["high"] = lo, hi
                for w_ in (lo, hi):
                    w_.on("update:model-value", lambda _e=None: on_change())
            else:  # uniform -> RangeVary (generator treats range as uniform)
                bkw = _bounds_number_kw(inp)
                lo = ui.number("min", value=inp.get("minimum"), **bkw).props("dense")
                hi = ui.number("max", value=inp.get("maximum"), **bkw).props("dense")
                state["min"], state["max"] = lo, hi
                for w_ in (lo, hi):
                    w_.on("update:model-value", lambda _e=None: on_change())
        on_change()

    dk.on("update:model-value", lambda _e=None: _rebuild_num())
    _rebuild_num()


def _render_sampling_vary(
    state: dict[str, Any], inp: dict[str, Any], options: list[Any], on_change: Any
) -> None:
    """Family E (lhs/sobol/random): ``By distribution`` or ``By values``.

    A numeric factor is sampled either from a continuous distribution
    (uniform / normal / log-uniform) or bin-sampled from an explicit set
    of values — the space-filling mirror of the grid ``By range`` / ``By
    values`` toggle (they are the same idea in the two idioms).
    """
    ft = state["type"]
    if ft == "numeric":
        by = ui.select(["By distribution", "By values"], value="By distribution").props("dense")
        state["by_select"] = by
        outer = ui.row().classes("items-center gap-2")

        def _rebuild_outer() -> None:
            outer.clear()
            for k in ("dist_kind", "min", "max", "mean", "std", "low", "high", "values_text"):
                state.pop(k, None)
            with outer:
                if str(by.value) == "By values":
                    w = ui.input("Values (comma-separated)").classes("flex-grow").props("dense")
                    state["values_text"] = w
                    w.on("update:model-value", lambda _e=None: on_change())
                else:
                    _render_sampling_distribution(state, inp, on_change)
            on_change()

        by.on("update:model-value", lambda _e=None: _rebuild_outer())
        _rebuild_outer()
    elif ft == "select":
        w = ui.select(options, multiple=True, value=list(options)).props("dense")
        state["subset"] = w
        w.on("update:model-value", lambda _e=None: on_change())
    elif ft == "bool":
        ui.label("varies over true / false (0.5 unit-cube split)").style(
            "color: #888; font-size: 0.85rem;"
        )
    # file: _can_vary_in_family returned False, so this branch is unreachable.


def _render_design_params(mode: str, mp_widgets: dict[str, Any], on_change: Any) -> None:
    """Mode-specific knobs panel for the screening / RSM modes."""
    with ui.card().classes("w-full"):
        ui.label("Design parameters").style("font-weight: 600;")
        if mode == "frac_factorial":
            res = (
                ui.number("Resolution (3-7)", value=3, min=3, max=7)
                .classes("w-full")
                .props("dense")
            )
            gen = (
                ui.input("Generator string (advanced, optional - e.g. 'a b ab')")
                .classes("w-full")
                .props("dense")
            )
            mp_widgets["frac_resolution"] = res
            mp_widgets["frac_gen"] = gen
            frac_widgets: list[Any] = [res, gen]
            for fw in frac_widgets:
                fw.on("update:model-value", lambda _e=None: on_change())
        elif mode == "plackett_burman":
            ui.label(
                "Run count is set automatically from the number of factors "
                "(next multiple of 4 >= k+1)."
            ).style("color: #888; font-size: 0.85rem;")
        elif mode == "ccd":
            face = (
                ui.select(dict(_CCD_FACES), value="faced", label="Face placement")
                .classes("w-full")
                .props("dense")
            )
            alpha = (
                ui.select(
                    dict(_CCD_ALPHAS),
                    value="orthogonal",
                    label="Alpha rule (ignored when face = Faced)",
                )
                .classes("w-full")
                .props("dense")
            )
            with ui.row().classes("w-full gap-2"):
                cf = ui.number("Factorial centre", value=1, min=0).classes("flex-1").props("dense")
                ca = ui.number("Axial centre", value=1, min=0).classes("flex-1").props("dense")
            mp_widgets["ccd_face"] = face
            mp_widgets["ccd_alpha"] = alpha
            mp_widgets["ccd_center_fact"] = cf
            mp_widgets["ccd_center_axial"] = ca
            ccd_widgets: list[Any] = [face, alpha, cf, ca]
            for cw in ccd_widgets:
                cw.on("update:model-value", lambda _e=None: on_change())
        elif mode == "box_behnken":
            cb = (
                ui.number("Centre replicates (blank = pyDOE3 default)")
                .classes("w-full")
                .props("dense")
            )
            mp_widgets["bb_center"] = cb
            cb.on("update:model-value", lambda _e=None: on_change())


def _render_sampling_params(mode: str, mp_widgets: dict[str, Any], on_change: Any) -> None:
    """Number of runs + seed for the space-filling modes."""
    with ui.card().classes("w-full"):
        ui.label("Sampling parameters").style("font-weight: 600;")
        n = ui.number("Number of runs", value=20, min=1).classes("w-full").props("dense")
        seed = ui.number("Seed (optional)").classes("w-full").props("dense")
        mp_widgets["n_samples"] = n
        mp_widgets["seed"] = seed
        for w in (n, seed):
            w.on("update:model-value", lambda _e=None: on_change())
        if mode == "sobol":
            ui.label(
                "Sobol sequences are most even when the number of runs is a "
                "power of 2 (16, 32, 64, ...)."
            ).style("color: #888; font-size: 0.85rem;")


def _const_widget(ftype: str, options: list[Any], default: Any) -> Any:
    if ftype in ("integer", "float"):
        int_kw: dict[str, Any] = {"step": 1, "format": "%.0f"} if ftype == "integer" else {}
        return ui.number(
            "value", value=default if isinstance(default, int | float) else None, **int_kw
        ).props("dense")
    if ftype == "boolean":
        return ui.checkbox("value", value=bool(default))
    if ftype in ("select", "multiselect"):
        return ui.select(
            options, value=default if default in options else (options[0] if options else None)
        ).props("dense")
    return ui.input("value", value=str(default) if default is not None else "").props("dense")


def _vary_names(factors: dict[str, dict[str, Any]]) -> list[str]:
    return [n for n, st in factors.items() if st.get("role") == "Vary"]


#: Run-table column geometry. Header cells and body cells share it so
#: the label above a column stays over the box it names.
_RUN_CELL_STYLE = "flex: 1 1 0; min-width: 0;"
_RUN_DELETE_STYLE = "flex: 0 0 2.5rem;"


def _aria(text: str) -> str:
    """Escape a label for use inside a double-quoted Quasar prop."""
    return text.replace("\\", "").replace('"', "'")


def _rebuild_run_table(
    ctx: dict[str, Any],
    factors: dict[str, dict[str, Any]],
    box: ui.column,
    mode: str,
    on_change: Any,
) -> None:
    """Render the run-table editor for ``table`` mode.

    Vary inputs become columns; the user adds the exact rows
    (design §3 amendment / §9).
    """
    box.clear()
    ctx["run_rows"] = []
    if mode != "table":
        return
    cols = _vary_names(factors)
    with box:
        ui.label("Run table").style("font-weight: 600;")
        if not cols:
            ui.label("Mark one or more inputs as Vary to add columns.").style(
                "color: #888; font-size: 0.85rem;"
            )
            return
        ui.label("One row per run; values are used exactly as entered.").style(
            "color: #888; font-size: 0.85rem;"
        )

        # Header row: each column named once, in the same "label (unit)"
        # form the factor list above uses. The alternative — labelling
        # every cell — repeats "Inlet velocity (m/s)" on all twenty rows
        # and pushes the table off the side of the page.
        with ui.row().classes("w-full items-end gap-2").style("padding-right: 0.25rem;"):
            for c in cols:
                ui.label(_factor_label(c, factors[c])).style(
                    f"{_RUN_CELL_STYLE} font-weight: 600; font-size: 0.85rem; "
                    "line-height: 1.2; overflow-wrap: anywhere;"
                )
            # Spacer under which the per-row delete buttons line up.
            ui.element("div").style(_RUN_DELETE_STYLE)

        rows_box = ui.column().classes("w-full gap-1")

        def _add_row() -> None:
            row: dict[str, Any] = {}
            with rows_box, ui.row().classes("w-full items-center gap-2") as rw:
                for c in cols:
                    ft = factors[c].get("type")
                    # No per-cell label — the header row names the column.
                    if ft == "numeric":
                        int_kw: dict[str, Any] = (
                            {"step": 1, "format": "%.0f"}
                            if factors[c].get("ftype") == "integer"
                            else {}
                        )
                        row[c] = ui.number(value=0, **int_kw).props("dense")
                    elif ft == "bool":
                        row[c] = ui.checkbox()
                    else:
                        row[c] = ui.input().props("dense")
                    # Every cell carries an accessible name even though
                    # the visible label moved to the header, or a screen
                    # reader reads the row as a line of blank boxes.
                    row[c].props(f'aria-label="{_aria(_factor_label(c, factors[c]))}"')
                    row[c].style(_RUN_CELL_STYLE)
                    row[c].on("update:model-value", lambda _e=None: on_change())

                def _del() -> None:
                    if row in ctx["run_rows"]:
                        ctx["run_rows"].remove(row)
                    rows_box.remove(rw)
                    on_change()

                ui.button(icon="delete", on_click=_del).props("flat dense color=negative").style(
                    _RUN_DELETE_STYLE
                ).tooltip("Remove this run")
            ctx["run_rows"].append(row)
            on_change()

        ui.button("Add run", icon="add", on_click=_add_row).props("flat dense")
        _add_row()


def _collect_runs(ctx: dict[str, Any], factors: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for row in ctx.get("run_rows") or []:
        out: dict[str, Any] = {}
        for col, widget in row.items():
            val = widget.value
            ft = factors.get(col, {}).get("type")
            if ft == "numeric" and val is not None and str(val) != "":
                val = _snap_int_value(float(val), factors.get(col, {}))
            elif ft == "bool":
                val = bool(val)
            out[col] = val
        runs.append(out)
    return runs


def _build_factor_spec(factors: dict[str, dict[str, Any]], family: str) -> dict[str, Any]:
    """Read the factor-row widgets into the design-§3 ``factor_spec`` dict.

    Family-aware (pre-b0-gamma): emits ``column`` for ``table``; explicit
    value lists for ``list``; per-numeric ``range`` (+levels+spacing)
    OR ``list`` for ``grid``; ``range`` (min/max only) for ``design``;
    ``range`` or ``distribution`` for ``sampling``. Raises plain-English
    :class:`ValueError` the form surfaces inline.
    """
    spec: dict[str, Any] = {}
    for name, st in factors.items():
        if st.get("role") != "Vary":
            widget = st.get("const")
            value = widget.value if widget is not None else None
            spec[name] = {
                "role": "constant",
                "value": _snap_int_value(value, st) if value is not None else None,
            }
            continue
        ft = st.get("type")
        if family == "table":
            spec[name] = {"role": "vary", "type": ft, "vary": {"kind": "column"}}
        elif family == "list":
            spec[name] = _build_list_vary(name, ft, st)
        elif family == "grid":
            spec[name] = _build_grid_vary(name, ft, st)
        elif family == "design":
            spec[name] = _build_design_vary(name, ft, st)
        else:  # sampling
            spec[name] = _build_sampling_vary(name, ft, st)
    return spec


def _build_list_vary(name: str, ft: str | None, st: dict[str, Any]) -> dict[str, Any]:
    """Family B (``list``) Vary spec: explicit value sets — no ranges."""
    if ft == "numeric":
        vals = _parse_values(str(st["values_text"].value or ""))
        if not vals:
            raise ValueError(f"'{_factor_label(name, st)}': add at least one value.")
        vals = _dedupe_numeric([_snap_int_value(v, st) for v in vals])
        return {
            "role": "vary",
            "type": "numeric",
            "vary": {"kind": "list", "values": vals},
        }
    if ft == "select":
        chosen = list(st["subset"].value or [])
        if not chosen:
            raise ValueError(f"'{_factor_label(name, st)}': pick at least one option to vary.")
        return {
            "role": "vary",
            "type": "select",
            "vary": {"kind": "subset", "options": chosen},
        }
    if ft == "bool":
        return {"role": "vary", "type": "bool"}
    return _build_file_list_vary(name, st)


def _build_file_list_vary(name: str, st: dict[str, Any]) -> dict[str, Any]:
    """File factor Vary spec (``file_list``) from the comma-separated refs."""
    widget = st.get("file_refs")
    raw = str(widget.value or "") if widget is not None else ""
    refs = [r.strip() for r in raw.split(",") if r.strip()]
    if not refs:
        raise ValueError(
            f"'{_factor_label(name, st)}': add at least one file reference to vary over."
        )
    seen: list[str] = []
    for r in refs:
        if r not in seen:
            seen.append(r)
    return {"role": "vary", "type": "file", "vary": {"kind": "file_list", "cas_hashes": seen}}


def _dedupe_numeric(values: list[Any]) -> list[Any]:
    """Order-preserving de-dupe of a value list.

    Snapping an integer list can collapse neighbours onto the same whole number
    (e.g. 2.4 and 2.6 → 2 and 3 is fine, but 2.5 and 2.6 both → 3), so drop the
    repeats rather than submit a duplicated run.
    """
    out: list[Any] = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def _build_grid_vary(name: str, ft: str | None, st: dict[str, Any]) -> dict[str, Any]:
    """Family C (``grid``) Vary spec: per-numeric ``range`` or explicit list."""
    if ft == "numeric":
        by = st.get("by_select")
        if by is not None and str(by.value) == "By values":
            vals = _parse_values(str(st["values_text"].value or ""))
            if not vals:
                raise ValueError(f"'{_factor_label(name, st)}': add at least one value.")
            vals = _dedupe_numeric([_snap_int_value(v, st) for v in vals])
            return {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "list", "values": vals},
            }
        lo, hi = st["min"].value, st["max"].value
        if lo is None or hi is None or float(hi) <= float(lo):
            raise ValueError(f"'{_factor_label(name, st)}': max must be greater than min.")
        return {
            "role": "vary",
            "type": "numeric",
            "vary": {
                "kind": "range",
                "min": _snap_int_value(lo, st),
                "max": _snap_int_value(hi, st),
                "levels": int(st["levels"].value or 3),
                "spacing": str(st["spacing"].value or "linear"),
            },
        }
    if ft == "select":
        chosen = list(st["subset"].value or [])
        if not chosen:
            raise ValueError(f"'{_factor_label(name, st)}': pick at least one option to vary.")
        return {
            "role": "vary",
            "type": "select",
            "vary": {"kind": "subset", "options": chosen},
        }
    if ft == "bool":
        return {"role": "vary", "type": "bool"}
    return _build_file_list_vary(name, st)


def _build_design_vary(name: str, ft: str | None, st: dict[str, Any]) -> dict[str, Any]:
    """Family D (screening / RSM) Vary spec: ``range`` with min+max only."""
    if ft != "numeric":
        raise ValueError(
            f"'{_factor_label(name, st)}': screening / RSM designs need a continuous numeric range "
            "(set it Constant or pick another mode)."
        )
    lo, hi = st["min"].value, st["max"].value
    if lo is None or hi is None or float(hi) <= float(lo):
        raise ValueError(f"'{_factor_label(name, st)}': max must be greater than min.")
    return {
        "role": "vary",
        "type": "numeric",
        "vary": {"kind": "range", "min": _snap_int_value(lo, st), "max": _snap_int_value(hi, st)},
    }


def _build_sampling_vary(name: str, ft: str | None, st: dict[str, Any]) -> dict[str, Any]:
    """Family E (lhs/sobol/random) Vary spec: values, ``range`` or ``distribution``."""
    if ft == "numeric":
        by = st.get("by_select")
        if by is not None and str(by.value) == "By values":
            vals = _parse_values(str(st["values_text"].value or ""))
            if not vals:
                raise ValueError(f"'{_factor_label(name, st)}': add at least one value.")
            vals = _dedupe_numeric([_snap_int_value(v, st) for v in vals])
            return {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "list", "values": vals},
            }
        d = str(st["dist_kind"].value or "uniform")
        if d == "normal":
            mean = st["mean"].value
            std = st["std"].value
            if mean is None or std is None or float(std) <= 0:
                raise ValueError(f"'{_factor_label(name, st)}': normal needs a mean and std > 0.")
            return {
                "role": "vary",
                "type": "numeric",
                "vary": {
                    "kind": "distribution",
                    "dist": "normal",
                    "mean": float(mean),
                    "std": float(std),
                },
            }
        if d == "loguniform":
            lo = st["low"].value
            hi = st["high"].value
            if lo is None or hi is None or float(lo) <= 0 or float(hi) <= float(lo):
                raise ValueError(
                    f"'{_factor_label(name, st)}': log-uniform needs low > 0 and high > low."
                )
            return {
                "role": "vary",
                "type": "numeric",
                "vary": {
                    "kind": "distribution",
                    "dist": "loguniform",
                    "low": _snap_int_value(lo, st),
                    "high": _snap_int_value(hi, st),
                },
            }
        # uniform -> RangeVary; the generator treats range as uniform in
        # space-filling modes.
        lo, hi = st["min"].value, st["max"].value
        if lo is None or hi is None or float(hi) <= float(lo):
            raise ValueError(f"'{_factor_label(name, st)}': max must be greater than min.")
        return {
            "role": "vary",
            "type": "numeric",
            "vary": {
                "kind": "range",
                "min": _snap_int_value(lo, st),
                "max": _snap_int_value(hi, st),
            },
        }
    if ft == "select":
        chosen = list(st["subset"].value or [])
        if not chosen:
            raise ValueError(f"'{_factor_label(name, st)}': pick at least one option to vary.")
        return {
            "role": "vary",
            "type": "select",
            "vary": {"kind": "subset", "options": chosen},
        }
    if ft == "bool":
        return {"role": "vary", "type": "bool"}
    raise ValueError(
        f"'{_factor_label(name, st)}': file factors aren't sampleable in space-filling modes."
    )


def _build_sampling_params(
    factors: dict[str, dict[str, Any]],
    family: str,
    mode: str,
    mp: dict[str, Any],
    ctx: dict[str, Any],
) -> dict[str, Any]:
    """Family-aware ``sampling_params`` dict (design §3 / §4)."""
    if family == "table":
        return {"runs": _collect_runs(ctx, factors)}
    if family == "sampling":
        n_w = mp.get("n_samples")
        return {"n_samples": int(n_w.value) if n_w is not None and n_w.value is not None else 20}
    if family != "design":
        return {}
    if mode == "frac_factorial":
        out: dict[str, Any] = {}
        gen_w = mp.get("frac_gen")
        gen_val = str(gen_w.value or "").strip() if gen_w is not None else ""
        if gen_val:
            out["frac_gen"] = gen_val
        else:
            res_w = mp.get("frac_resolution")
            if res_w is not None and res_w.value is not None:
                out["frac_resolution"] = int(res_w.value)
        return out
    if mode == "ccd":
        face = mp.get("ccd_face")
        alpha = mp.get("ccd_alpha")
        cf = mp.get("ccd_center_fact")
        ca = mp.get("ccd_center_axial")
        return {
            "ccd_face": str(face.value) if face is not None and face.value else "faced",
            "ccd_alpha": str(alpha.value) if alpha is not None and alpha.value else "orthogonal",
            "ccd_center": (
                int(cf.value) if cf is not None and cf.value is not None else 1,
                int(ca.value) if ca is not None and ca.value is not None else 1,
            ),
        }
    if mode == "box_behnken":
        cb = mp.get("bb_center")
        v = cb.value if cb is not None else None
        return {"bb_center": int(v)} if v is not None and str(v) != "" else {}
    return {}  # plackett_burman


# ========================= study detail =============================


@ui.page("/studies/{study_id}")
async def study_detail_page(study_id: int, tab: str = "progress") -> None:
    """One study — Progress + Results tabs (live), polled like a workflow run.

    Progress tab: live child-state breakdown with per-child retry +
    study-level cancel. Results tab (2-PS-b0): matrix table, response
    -source picker, CSV download, per-child file drill-down,
    partial-data banner (D3). Both tabs share the polling timer.

    ``tab`` (query param) makes the active tab durable: it's written back to
    the URL on every switch, so a full page reload — including the silent one
    NiceGUI does after a websocket re-sync — restores the tab the user was on
    rather than snapping back to the constructed Progress default.
    """
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    # Wider than the rest of the app (max-w-5xl): the Results tab carries a
    # full data grid + side-by-side plots and needs the room (2-PS-b').
    with ui.column().classes("w-full gap-3").style("padding: 0.85rem 1.5rem 1.5rem;"):
        body = ui.column().classes("w-full gap-3")
    await _render_detail(client, int(study_id), body, initial_tab=tab)


# Query-param slug <-> tab label. The slug rides in the URL (?tab=results);
# the label is what the ``ui.tab`` objects are keyed by.
_TAB_LABELS = {"progress": "Progress", "results": "Results", "provenance": "Provenance"}
_TAB_SLUGS = {v: k for k, v in _TAB_LABELS.items()}


async def _render_detail(
    client: ApiClient, study_id: int, body: ui.column, *, initial_tab: str = "progress"
) -> None:
    try:
        detail = await client.get_study(study_id)
    except ApiError as exc:
        body.clear()
        with body:
            ui.label(f"Couldn't load this study: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
        return
    # Optimistic results fetch — empty/None on failure (study may have no
    # rows yet; not-found surfaces here as None and the Results panel
    # renders a friendly empty state).
    results: dict[str, Any] | None
    try:
        results = await client.get_study_results(study_id)
    except ApiError:
        results = None

    body.clear()
    with body:
        header = ui.column().classes("w-full gap-2")
        # Study settings summary — the configuration is fixed at creation, so
        # render it once here (above the tabs) rather than in the poll-rebuilt
        # header; it stays visible on both Progress and Results.
        _render_settings_summary(
            detail.get("study") or {},
            detail.get("input_labels") or {},
            detail.get("input_units") or {},
            detail.get("input_groups") or [],
            detail.get("input_group_of") or {},
        )
        with (
            ui.tabs()
            .classes("w-full")
            .props("align=left active-color=primary indicator-color=primary")
            .style("border-bottom: 1px solid var(--matofx-card-border);") as tabs
        ):
            progress_tab = ui.tab("Progress", icon="trending_up").props("no-caps")
            results_tab = ui.tab("Results", icon="insights").props("no-caps")
            provenance_tab = ui.tab("Provenance", icon="account_tree").props("no-caps")
        # Honour the ?tab= slug so a reload lands on the tab the user was on.
        _tabs_by_label = {
            "Progress": progress_tab,
            "Results": results_tab,
            "Provenance": provenance_tab,
        }
        active_label = _TAB_LABELS.get(str(initial_tab).lower(), "Progress")
        with (
            ui.tab_panels(tabs, value=_tabs_by_label[active_label])
            .classes("w-full")
            .style("background-color: transparent;")
        ):
            with ui.tab_panel(progress_tab).style("background-color: transparent;"):
                progress_content = ui.column().classes("w-full gap-3")
            with ui.tab_panel(results_tab).style("background-color: transparent;"):
                # Auto-generated results summary (OUTPUTS/designs/auto-summaries.md).
                # Method-aware narrative at the top of the explorer; renders only
                # once the study is terminal (the card 404s otherwise).
                from matofx.ui._summary_card import render_summary_card  # noqa: PLC0415

                await render_summary_card(client, "parametric_study", study_id)
                # A slot above the matrix for the "newer results available"
                # hint (see _rerender): while the user is reading the Results
                # tab we don't tear it down under them, so fresh data surfaces
                # as a Refresh button here rather than a silent rebuild.
                results_hint = ui.column().classes("w-full")
                results_content = ui.column().classes("w-full gap-3")
            with ui.tab_panel(provenance_tab).style("background-color: transparent;"):
                # Provenance / lineage tab (ADR-0010 slice 5). The
                # expansion-based ``render_provenance_section`` keeps
                # the fetch lazy — tab activation alone doesn't fire
                # the API call until the user clicks Open.
                from matofx.ui.lineage_view import (  # noqa: PLC0415
                    render_provenance_section,
                )

                render_provenance_section(
                    kind="parametric_study",
                    obj_id=study_id,
                    initially_open=True,
                )

    state: dict[str, Any] = {
        "detail": detail,
        "results": results,
        "header": header,
        "progress_content": progress_content,
        "results_content": results_content,
        "results_hint": results_hint,
        "client": client,
        "study_id": study_id,
        "last_signature": None,
        # Which tab the user is on, so a poll tick rebuilds only that panel
        # and never tears down (and snaps focus to) an inactive one — the
        # bug that kept bouncing reviewers back to Progress.
        "tabs": tabs,
        "active_tab": active_label,
        # The results-slice signature at the last Results rebuild + whether
        # newer data has arrived since (drives the Refresh hint).
        "rendered_results_sig": None,
        "results_dirty": False,
    }
    tabs.on_value_change(lambda e: _on_tab_change(state, str(e.value)))
    _rerender(state, force=True)

    async def _poll() -> None:
        try:
            fresh = await client.get_study(study_id)
        except ApiError:
            return
        state["detail"] = fresh
        # Keep stale results on a failed fetch — matrix won't refresh this
        # tick but the page stays usable (D3 partial-data banner is from
        # the previous results, which is fine).
        with contextlib.suppress(ApiError):
            state["results"] = await client.get_study_results(study_id)
        _rerender(state)

    ui.timer(_REFRESH_INTERVAL_S, _poll)


def _on_tab_change(state: dict[str, Any], tab_name: str) -> None:
    """Record the active tab and bring the panel just entered up to date.

    Inactive panels are left stale while off-screen (the poll loop only
    refreshes the active one); on switch we rebuild the panel the user is
    now looking at so it reflects the latest poll without ever being torn
    down underneath them while they read it.
    """
    state["active_tab"] = tab_name
    # Persist the selection to the URL (no reload) so a later re-sync/refresh
    # restores this tab instead of the constructed Progress default.
    slug = _TAB_SLUGS.get(tab_name, "progress")
    ui.run_javascript(f"history.replaceState(null, '', window.location.pathname + '?tab={slug}')")
    if tab_name == "Progress":
        _rebuild_progress(state)
    elif tab_name == "Results" and (
        state.get("results_dirty") or _results_sig(state) != state.get("rendered_results_sig")
    ):
        _rebuild_results(state)
    # Provenance manages its own lazy fetch — nothing to refresh here.


def _results_sig(state: dict[str, Any]) -> tuple[Any, ...]:
    """Results-slice signature: child count + per-child scalar/file counts.

    Changes whenever the runner writes a new row (b0-3); used both as part
    of the overall render gate and to decide when the Results panel has
    fresher data than what's currently on screen.
    """
    results = state.get("results") or {}
    rchildren = results.get("children") or []
    return tuple(
        (
            int(r.get("job_id") or 0),
            len(r.get("scalars") or {}),
            len(r.get("retained_files") or []),
        )
        for r in rchildren
    )


def _signature(state: dict[str, Any]) -> tuple[Any, ...]:
    detail = state["detail"]
    study = detail.get("study") or {}
    children = detail.get("children") or []
    return (
        str(study.get("state") or ""),
        study.get("cancel_requested_at"),
        tuple((int(c.get("id") or 0), str(c.get("state") or "")) for c in children),
        _results_sig(state),
    )


def _rebuild_header(state: dict[str, Any]) -> None:
    study = (state["detail"].get("study")) or {}
    header = state["header"]
    header.clear()
    with header:
        _render_detail_header(state["client"], study)


def _rebuild_progress(state: dict[str, Any]) -> None:
    detail = state["detail"]
    study = detail.get("study") or {}
    children = detail.get("children") or []
    box = state["progress_content"]
    box.clear()
    with box:
        _render_children_table(state["client"], state, study, children)


def _rebuild_results(state: dict[str, Any]) -> None:
    """Tear down + rebuild the Results panel, clearing the dirty hint.

    Rebuilding resets the in-panel grid selection / column filters / plot
    cards, so this only runs when the user isn't actively reading the tab
    (poll ticks skip it then — see _rerender) or when they explicitly ask
    via the Refresh hint / a tab switch.
    """
    study = (state["detail"].get("study")) or {}
    box = state["results_content"]
    box.clear()
    with box:
        _render_results_panel(state["client"], state, study, state.get("results"))
    state["rendered_results_sig"] = _results_sig(state)
    state["results_dirty"] = False
    _refresh_results_hint(state)


def _refresh_results_hint(state: dict[str, Any]) -> None:
    """Show/clear the "newer results available" Refresh affordance."""
    hint = state.get("results_hint")
    if hint is None:
        return
    hint.clear()
    if not state.get("results_dirty"):
        return
    with (
        hint,
        ui.row()
        .classes("w-full items-center gap-2 p-2 rounded")
        .style("background-color: var(--matofx-row-bg);"),
    ):
        ui.label("Newer results have arrived since you opened this tab.").style(
            "color: #b58900; font-size: 0.9rem;"
        )
        ui.button("Refresh", icon="refresh", on_click=lambda: _rebuild_results(state)).props(
            "flat dense"
        )


def _rerender(state: dict[str, Any], *, force: bool = False) -> None:
    sig = _signature(state)
    if not force and sig == state.get("last_signature"):
        return
    state["last_signature"] = sig

    # The header lives outside the tab strip and carries no user state, so
    # it's always safe (and cheap) to refresh.
    _rebuild_header(state)

    active = state.get("active_tab") or "Progress"

    # Only rebuild the panel the user is actually on. Tearing down an
    # inactive panel on a poll tick was snapping focus back to Progress;
    # inactive panels are refreshed lazily on switch (_on_tab_change).
    if force or active == "Progress":
        _rebuild_progress(state)

    # Don't rebuild the Results panel under the user — that wipes their grid
    # selection / filters / expanded plots. While they're on it, flag newer
    # data via the Refresh hint instead; they pick it up when they choose to.
    if force:
        _rebuild_results(state)
    elif active == "Results" and _results_sig(state) != state.get("rendered_results_sig"):
        state["results_dirty"] = True
        _refresh_results_hint(state)


def _fmt_num(v: Any) -> str:
    """Compact rendering of a factor-spec numeric (``%g``), else ``str``."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return f"{v:g}"
    return str(v)


def _factor_domain_text(fac: dict[str, Any], unit: str) -> str:
    """Plain-English domain for one factor (constant value or vary spec).

    Uses the same vocabulary as the create form so the summary reads back
    what was set: a constant value, a numeric range (+ grid levels), an
    explicit value list, a distribution (normal is noted as truncated to
    the input's bounds), a categorical subset, or a file set.
    """
    u = f" {unit}" if unit else ""
    if fac.get("role") != "vary":
        val = fac.get("value")
        return f"constant = {_fmt_num(val)}{u}" if val is not None else "constant"
    vary = fac.get("vary") or {}
    kind = vary.get("kind")
    if fac.get("type") == "bool" and not vary:
        return "varies over true / false"
    if kind == "range":
        base = f"range {_fmt_num(vary.get('min'))} to {_fmt_num(vary.get('max'))}{u}"
        if vary.get("levels") is not None:
            spacing = str(vary.get("spacing") or "linear")
            base += f", {vary['levels']} levels ({_SPACINGS.get(spacing, spacing)})"
        return base
    if kind == "list":
        vals = ", ".join(_fmt_num(v) for v in (vary.get("values") or []))
        return f"values: {vals}{u}"
    if kind == "distribution":
        dist = vary.get("dist")
        if dist == "normal":
            return (
                f"normal mean={_fmt_num(vary.get('mean'))}, std={_fmt_num(vary.get('std'))} "
                f"(truncated to the input's bounds){u}"
            )
        if dist == "loguniform":
            return f"log-uniform {_fmt_num(vary.get('low'))} to {_fmt_num(vary.get('high'))}{u}"
        return f"uniform {_fmt_num(vary.get('low'))} to {_fmt_num(vary.get('high'))}{u}"
    if kind == "subset":
        return "options: " + ", ".join(str(o) for o in (vary.get("options") or []))
    if kind == "file_list":
        return f"{len(vary.get('cas_hashes') or [])} file(s)"
    if kind == "column":
        return "run-table column"
    return "varies"


def _render_settings_summary(
    study: dict[str, Any],
    labels: dict[str, Any],
    units: dict[str, Any],
    input_groups: list[dict[str, Any]] | None = None,
    input_group_of: dict[str, Any] | None = None,
) -> None:
    """Compact "Study settings" card for the detail page.

    Shows the mode, run facts (cap / generated / skipped / seed / samples)
    and every factor's domain with descriptor labels + units
    ([[feedback_use_labels_not_names]]). When the tool defines input groups,
    the factors are laid out under the same section headings the tool's job
    form (and the study setup) use, in the same order; otherwise they fall
    back to a flat vary-first list.
    """
    if not study:
        return
    sp = study.get("sampling_params") or {}
    fs = study.get("factor_spec") or {}
    mode = str(study.get("mode") or "")
    facts: list[tuple[str, str]] = [
        ("Mode", _MODES.get(mode, mode)),
        ("Runs generated", str(study.get("generated_count") or 0)),
    ]
    skipped = int(study.get("skipped_invalid_count") or 0)
    if skipped:
        facts.append(("Skipped (rules)", str(skipped)))
    facts.append(("Run cap", str(study.get("run_cap") or "")))
    if mode in _SPACE_FILLING:
        seed = study.get("seed")
        facts.append(("Seed", str(seed) if seed is not None else "auto"))
        if sp.get("n_samples") is not None:
            facts.append(("Samples requested", str(sp.get("n_samples"))))
    if mode == "grid" and sp.get("points_per_factor") is not None:
        facts.append(("Default levels", str(sp.get("points_per_factor"))))
    fs_items = list(fs.items())
    group_of = input_group_of or {}
    # Pseudo-inputs carry each factor's group so the shared sectioner lays
    # them out exactly like the setup wizard (column-then-row, "Other inputs"
    # trailing). fs iterates in descriptor order, preserved within each group.
    pseudo = [{"name": name, "group": group_of.get(name)} for name, _ in fs_items]
    sections = group_sections(pseudo, input_groups)
    show_headers = has_groups(sections)

    def _factor_row(name: str, fac: dict[str, Any]) -> None:
        label = str(labels.get(name) or name)
        unit = str(units.get(name) or "")
        is_vary = fac.get("role") == "vary"
        with ui.row().classes("w-full items-center gap-2"):
            ui.label(label).style("flex: 0 0 240px; font-weight: 500;")
            ui.label(_factor_domain_text(fac, unit)).style(
                "" if is_vary else "color: var(--matofx-muted-text);"
            )
            if is_vary:
                ui.label("Vary").style(
                    "font-size: 0.68rem; color: var(--matofx-accent); "
                    "border: 1px solid var(--matofx-accent); border-radius: 4px; "
                    "padding: 0 6px;"
                )

    with (
        ui.card().classes("w-full"),
        ui.expansion("Study settings", icon="tune", value=False).classes("w-full"),
    ):
        with ui.row().classes("gap-5 flex-wrap"):
            for key, val in facts:
                with ui.column().classes("gap-0"):
                    ui.label(key).style(
                        "color: var(--matofx-muted-text); font-size: 0.68rem; "
                        "text-transform: uppercase; letter-spacing: 0.04em;"
                    )
                    ui.label(val).style("font-weight: 600;")
        ui.separator()
        if show_headers:
            for header, members in sections:
                render_group_header(header or OTHER_GROUP_LABEL)
                for m in members:
                    name = str(m["name"])
                    _factor_row(name, fs[name])
        else:
            # No groups declared: keep the flat vary-first ordering as before.
            ordered = [t for t in fs_items if t[1].get("role") == "vary"] + [
                t for t in fs_items if t[1].get("role") != "vary"
            ]
            for name, fac in ordered:
                _factor_row(name, fac)


def _render_detail_header(client: ApiClient, study: dict[str, Any]) -> None:
    name = str(study.get("name") or "")
    # Tool label + version *string*, not the alias / version id
    # ([[feedback_use_labels_not_names]]).
    tool = str(study.get("tool_name") or study.get("tool_alias") or "")
    version = str(study.get("tool_version") or "")
    version_text = f"version {version}" if version else f"version #{study.get('tool_version_id')}"
    state = str(study.get("state") or "draft")
    mode = str(study.get("mode") or "")
    # Single left-aligned cluster (no justify-between) so the state chip +
    # Cancel sit next to the title rather than under the fixed theme / help
    # buttons pinned to the top-right of the content area.
    with ui.row().classes("w-full items-center gap-3"):
        ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/studies")).props(
            "flat round dense"
        ).tooltip("Back to Parametric Studies")
        with ui.column().classes("gap-0"):
            ui.label(name).style(
                "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
            )
            ui.label(f"{tool} · pinned tool {version_text} · {_MODES.get(mode, mode)}").style(
                "color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;"
            )
        _state_chip(state)
        if state in ("draft", "running", "pending_approval"):
            ui.button(
                "Cancel study",
                icon="cancel",
                on_click=lambda: _confirm_cancel(client, int(study["id"])),
            ).props("flat dense color=negative")
    gen = int(study.get("generated_count") or 0)
    skipped = int(study.get("skipped_invalid_count") or 0)
    if gen or skipped:
        ui.label(
            f"{gen} run(s) generated"
            + (f", {skipped} point(s) skipped by cross-field rules" if skipped else "")
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
    if study.get("cancel_requested_at"):
        ui.label(
            "This study was cancelled. Pending jobs were stopped; completed "
            "jobs keep their results."
        ).style("color: var(--matofx-error-text); font-size: 0.9rem;")


def _confirm_cancel(client: ApiClient, study_id: int) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label("Cancel this study?").style("font-size: 1.1rem; font-weight: 600;")
        ui.label(
            "Queued and running jobs will be asked to stop. Completed jobs keep their results."
        ).style("color: #888;")

        async def _confirm() -> None:
            try:
                await client.cancel_study(study_id)
            except ApiError as exc:
                ui.notify(f"Couldn't cancel: {exc.detail}", type="negative")
                return
            dialog.close()
            ui.notify("Study cancelled.", type="info")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Back", on_click=dialog.close).props("flat")
            ui.button("Cancel study", on_click=_confirm).props("color=negative")
    dialog.open()


def _render_children_table(
    client: ApiClient,
    state: dict[str, Any],
    study: dict[str, Any],
    children: list[dict[str, Any]],
) -> None:
    with section_card("trending_up", "Progress"):
        if not children:
            if str(study.get("state")) == "draft":
                ui.label("Not submitted yet.").style("color: var(--matofx-muted-text);")
            else:
                ui.label("No jobs generated.").style("color: var(--matofx-muted-text);")
            return
        # Descriptor label / unit map (from the pinned descriptor on the
        # detail payload) so the rows show labels, not raw input names.
        detail = state.get("detail") or {}
        labels = detail.get("input_labels") or {}
        units = detail.get("input_units") or {}
        counts: dict[str, int] = {}
        for c in children:
            cs = str(c.get("state") or "created")
            counts[cs] = counts.get(cs, 0) + 1
        summary = " · ".join(f"{_STATE_LABEL.get(k, k)}: {v}" for k, v in sorted(counts.items()))
        ui.label(f"{len(children)} jobs — {summary}").style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )
        with ui.column().classes("w-full gap-1"):
            for child in children:
                _render_child_row(client, state, child, labels, units)


def _child_inputs_summary(
    inputs: dict[str, Any], labels: dict[str, Any], units: dict[str, Any]
) -> str:
    """``label=value unit`` pairs for a child row — descriptor labels, not names."""
    parts: list[str] = []
    for key, value in list(inputs.items())[:4]:
        label = str(labels.get(key) or key)
        unit = str(units.get(key) or "")
        parts.append(f"{label}={value}{f' {unit}' if unit else ''}")
    summary = ", ".join(parts)
    if len(inputs) > 4:
        summary += f", +{len(inputs) - 4} more"
    return summary


def _render_child_row(
    client: ApiClient,
    state: dict[str, Any],
    child: dict[str, Any],
    labels: dict[str, Any],
    units: dict[str, Any],
) -> None:
    job_id = int(child.get("id") or 0)
    cstate = str(child.get("state") or "created")
    inputs = child.get("inputs") or {}
    varied = _child_inputs_summary(inputs, labels, units)
    with ui.row().classes("w-full items-center gap-2 p-1"):
        ui.label(f"#{job_id}").style("flex: 0 0 70px; color: var(--matofx-muted-text);")
        ui.label(varied or "—").classes("flex-grow").style("font-size: 0.85rem;")
        dur = "—"
        if child.get("started_at") and child.get("completed_at"):
            dur = format_user_datetime(child.get("completed_at"))
        ui.label(dur).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        ui.chip(_STATE_LABEL.get(cstate, cstate.title())).props(
            f"color={_STATE_COLOR.get(cstate, 'grey')} text-color=white dense"
        )
        if cstate == "failed":
            ui.button(
                "Retry",
                icon="refresh",
                on_click=lambda _e=None, _j=job_id: _do_retry(client, state, _j),
            ).props("flat dense color=warning")


async def _do_retry(client: ApiClient, state: dict[str, Any], job_id: int) -> None:
    try:
        fresh = await client.retry_study_child(int(state["study_id"]), job_id)
    except ApiError as exc:
        ui.notify(f"Couldn't retry: {exc.detail}", type="negative")
        return
    state["detail"] = fresh
    _rerender(state, force=True)
    ui.notify("Job re-queued.", type="positive")


# ========================== Results tab (2-PS-b0) ===================


_ImportanceRenderer = Callable[[dict[str, Any]], None]


def _importance_renderers(
    mode: str,
) -> tuple[_ImportanceRenderer, list[_ImportanceRenderer]]:
    """Pick the lead input-importance card for ``mode`` + the rest (Advanced).

    Designed studies (grid / factorial / screening / RSM designs) lead with
    ANOVA; space-filling / table / list studies lead with the regression
    Sensitivity. Sobol is always supplementary (it needs a fitted surrogate),
    so it never leads. The non-lead methods go under "Advanced statistics".
    """
    lead: _ImportanceRenderer = (
        _render_anova_card if mode in _ANOVA_LEAD_MODES else _render_sensitivity_card
    )
    candidates: tuple[_ImportanceRenderer, ...] = (
        _render_sensitivity_card,
        _render_anova_card,
        _render_sobol_card,
    )
    others = [render for render in candidates if render is not lead]
    return lead, others


def _safe_render(render: Callable[[dict[str, Any]], None], state: dict[str, Any]) -> None:
    """Render one Results-tab card, surfacing a failure in-place."""
    try:
        render(state)
    except Exception as exc:  # diagnostic safety net
        ui.label(f"Couldn't render this section: {exc}").style("color: var(--matofx-error-text);")


def _render_results_panel(
    client: ApiClient,
    state: dict[str, Any],
    study: dict[str, Any],
    results: dict[str, Any] | None,
) -> None:
    """The Results tab body: full-grid matrix + unified plot card + sensitivity (2-PS-b')."""
    if str(study.get("state")) == "draft":
        ui.label("Not submitted yet.").style("color: #888;")
        return
    if results is None:
        ui.label("Couldn't load results.").style("color: var(--matofx-error-text);")
        return
    children = list(results.get("children") or [])
    if not children:
        ui.label("No child jobs yet.").style("color: #888;")
        return

    _render_partial_data_banner(children)

    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Run / response matrix").style("font-weight: 600;")
            sid = int(state["study_id"])

            async def _download() -> None:
                try:
                    data = await client.download_study_results_csv(sid)
                except ApiError as exc:
                    ui.notify(f"Couldn't download: {exc.detail}", type="negative")
                    return
                ui.download.content(data, f"study_{sid:07d}_results.csv")

            with ui.row().classes("items-center gap-2"):
                # A finished study is ready to fit a surrogate on (ADR-0008
                # §3.1) — deep-link into the fit form pre-filled with this
                # study as the source. ``partial`` counts too: a study that
                # finished with some failed/invalid runs is still a good source
                # (the ingest excludes the bad runs, ADR-0017) — and the
                # bracket solve-guard makes ``partial`` the normal outcome.
                # Feature-gated; the form 404s/redirects if surrogates are off.
                _finished = str(study.get("state")) in ("complete", "partial")
                if _finished and surrogate_family_enabled():
                    ui.button(
                        "Build a surrogate",
                        icon="model_training",
                        on_click=lambda _s=sid: ui.navigate.to(
                            f"/models?source_kind=parametric&source_id={_s}"
                        ),
                    ).props("flat dense color=primary")
                ui.button("Download CSV", icon="download", on_click=_download).props("flat dense")
        _render_results_matrix(state)

    # Input-importance methods (Sensitivity / ANOVA / Sobol) rank input
    # influence from different statistical angles; lead with the one that fits
    # this study's design and tuck the rest under "Advanced statistics" so the
    # tab isn't three overlapping rankings (auto-summaries review 2026-06-11).
    # Each card renders independently — a failure surfaces in-place, never
    # blanking the tab.
    lead_importance, other_importance = _importance_renderers(str(study.get("mode") or ""))

    for render in (_render_summary_card, _render_plot_card, lead_importance):
        _safe_render(render, state)

    with ui.expansion("Advanced statistics", icon="science").classes("w-full"):
        ui.label(
            "Other ways to measure input influence. They answer the same question "
            "as the importance card above from different statistical angles - they "
            "usually agree; where they disagree, the response is likely non-linear "
            "or driven by interactions."
        ).style("color: #888; font-size: 0.85rem;")
        for render in other_importance:
            _safe_render(render, state)

    for render in (
        _render_rsm_card,
        _render_profiler_card,
        _render_surrogate_contour_card,
        _render_tradeoffs_card,
    ):
        _safe_render(render, state)


def _render_partial_data_banner(children: list[dict[str, Any]]) -> None:
    """D3 partial-data banner: incomplete sample or failure-biased."""
    total = len(children)
    if total == 0:
        return
    running = sum(1 for c in children if str(c.get("state")) in ("created", "queued", "running"))
    failed = sum(1 for c in children if str(c.get("state")) in ("failed", "cancelled"))
    if running:
        ui.label(
            f"{running} of {total} run(s) still going - the matrix below is "
            "incomplete; fits and sensitivity (2-PS-b) may change."
        ).style("color: #b58900; font-size: 0.9rem;")
    elif failed:
        pct = 100 * failed / total
        ui.label(
            f"{failed} of {total} run(s) failed or were cancelled "
            f"({pct:.0f}%); sensitivity / surrogate fits on this sample "
            "may be unreliable."
        ).style("color: #b58900; font-size: 0.9rem;")


def _varied_inputs(children: list[dict[str, Any]]) -> list[str]:
    """Numeric inputs the study actually swept (>= 2 distinct values).

    Mirrors the server's ``varied_columns`` so plot axes and sensitivity
    only offer the inputs that vary (2-PS-b').
    """
    out: list[str] = []
    for name in _numeric_inputs(children):
        distinct = {
            v
            for c in children
            if isinstance((v := (c.get("inputs") or {}).get(name)), int | float)
            and not isinstance(v, bool)
        }
        if len(distinct) >= 2:
            out.append(name)
    return out


def _option_inputs(children: list[dict[str, Any]]) -> list[str]:
    """Inputs that are a *choice*, not a number — every one the runs carry.

    Kept separate from :func:`_varied_inputs` on purpose. That one mirrors the
    server's ``varied_columns`` and feeds sensitivity, ANOVA and the coverage
    metrics, all of which need real numbers **and** a spread to rank anything
    against; widening it would push options into analyses that cannot use them.

    Deliberately **not** filtered to two-or-more levels, unlike the numeric
    list. A study run over a single material still chose that material, still
    shows it in the run/response matrix, and its one column is a true statement
    about every run in the study — "no spread" is a reason to leave an input
    out of a *ranking*, not out of a *picture*. Excluding it meant the one
    input a single-material study had selected could not be plotted at all.
    """
    names: set[str] = set()
    for c in children:
        names.update(
            k for k, v in (c.get("inputs") or {}).items() if isinstance(v, str) and v != ""
        )
    return sorted(names)


def _input_levels(results: dict[str, Any], children: list[dict[str, Any]], name: str) -> list[str]:
    """Ordered levels for a choice-valued input: declared first, then any extra.

    Declared order is the tool author's (``Low / Medium / High`` must not sort
    into ``High / Low / Medium``), and declared options nobody ran are kept —
    an empty column says the sweep never covered that option.
    """
    declared = (results.get("input_options") or {}).get(name) or []
    return axis_categories(declared, [_var_text(c, f"in:{name}") for c in children])


def _plot_inputs(results: dict[str, Any], children: list[dict[str, Any]]) -> list[str]:
    """Every input worth an axis, in the tool's own declared order.

    Neither numeric nor choice inputs are filtered to "actually varied" here.
    That filter belongs to :func:`_varied_inputs`, which feeds sensitivity,
    ANOVA and the coverage metrics — analyses that **rank** an input against a
    response and so need a spread to rank against. A plot has no such need,
    and the run/response matrix beside it filters nothing either: an input the
    study held constant is still an input the study chose, and one flat axis
    saying so is a true statement about every run.

    Descriptor order, so the parallel-coordinates axes read left to right in
    the same order as the tool's own form rather than alphabetically or by
    which helper happened to list them first.
    """
    names = set(_numeric_inputs(children)) | set(_option_inputs(children))
    declared = [n for n in (results.get("input_labels") or {}) if n in names]
    return declared + sorted(names - set(declared))


def _plot_variables(state: dict[str, Any]) -> dict[str, str]:
    """``{key: label}`` plottable variables — inputs first, then scalar outputs.

    Keys are namespaced (``in:<name>`` / ``out:<name>``) so an input and
    an output that share a name don't collide; values are display labels.

    Inputs lead, in descriptor order, because this dict also orders the
    parallel-coordinates axes: appending a group of inputs after the outputs
    stranded them at the right-hand edge, away from the inputs they belong with.
    """
    results = state.get("results") or {}
    children = list(results.get("children") or [])
    opts: dict[str, str] = {}
    for n in _plot_inputs(results, children):
        opts[f"in:{n}"] = _input_label(results, n)
    for n in _scalar_outputs(children):
        opts[f"out:{n}"] = _output_label(results, n)
    return opts


def sectioned_variables(variables: dict[str, str]) -> dict[str, str]:
    """Split a plot picker's options into "Inputs" and "Outputs" sections."""
    return sectioned_options(
        ("INPUTS", {k: v for k, v in variables.items() if k.startswith("in:")}),
        ("OUTPUTS", {k: v for k, v in variables.items() if k.startswith("out:")}),
    )


def _first_variable(options: dict[str, str], skip: str | None = None) -> str:
    """First real (non-heading) key in a sectioned option dict."""
    return next((k for k in options if k and not is_section_header(k) and k != skip), "")


def _plot_levels(state: dict[str, Any]) -> dict[str, list[str]]:
    """``{plot key: levels}`` for the choice-valued variables only."""
    results = state.get("results") or {}
    children = list(results.get("children") or [])
    return {f"in:{n}": _input_levels(results, children, n) for n in _option_inputs(children)}


def _var_value(child: dict[str, Any], key: str) -> float | None:
    """Numeric value of a namespaced plot variable for one child (or ``None``).

    Choice-valued inputs have no number and return ``None`` here by design —
    the plots place them with :func:`_var_position` instead.
    """
    bucket, _, name = key.partition(":")
    src = child.get("inputs" if bucket == "in" else "scalars") or {}
    v = src.get(name)
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _var_text(child: dict[str, Any], key: str) -> str:
    """A choice-valued variable's level for one child, missing values named."""
    bucket, _, name = key.partition(":")
    src = child.get("inputs" if bucket == "in" else "scalars") or {}
    v = src.get(name)
    return UNSET_LEVEL if v is None else str(v)


# --- labels + numeric helpers + Explore plots (2-PS-b3 / 2-PS-b') --------


def _input_label(results: dict[str, Any], name: str) -> str:
    """Display label (+ unit) for an input name; falls back to the name.

    2-PS-b': the UI shows descriptor labels everywhere, never raw input
    names. ``input_labels`` / ``input_units`` come from the pinned
    descriptor on the results payload.
    """
    label = (results.get("input_labels") or {}).get(name) or name
    unit = (results.get("input_units") or {}).get(name)
    return f"{label} ({unit})" if unit else str(label)


def _output_label(results: dict[str, Any], name: str) -> str:
    """Display label (+ unit) for a scalar output name; falls back to the name."""
    for s in (results.get("response_sources") or {}).get("scalars") or []:
        if str(s.get("name")) == name:
            label = s.get("label") or name
            unit = s.get("unit")
            return f"{label} ({unit})" if unit else str(label)
    return name


def _scalar_outputs(children: list[dict[str, Any]]) -> list[str]:
    """Sorted names of scalar outputs that are numeric across the children."""
    names: set[str] = set()
    for c in children:
        for k, v in (c.get("scalars") or {}).items():
            if isinstance(v, int | float) and not isinstance(v, bool):
                names.add(k)
    return sorted(names)


def _numeric_inputs(children: list[dict[str, Any]]) -> list[str]:
    """Sorted names of inputs that are numeric for every child that has them."""
    names: set[str] = set()
    for c in children:
        for k, v in (c.get("inputs") or {}).items():
            if isinstance(v, int | float) and not isinstance(v, bool):
                names.add(k)
    return sorted(names)


def _scalar_of(child: dict[str, Any], name: str) -> float | None:
    """Numeric value of a scalar output for one child (or ``None``)."""
    v = (child.get("scalars") or {}).get(name)
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _fmt_stat(v: float | None) -> str:
    """Compact 4-significant-figure rendering (scientific for huge/tiny)."""
    return "-" if v is None else f"{v:.4g}"


def _opt_float(v: Any) -> float | None:
    """Parse an optional numeric form field: blank / unparseable -> ``None``."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _opt_int(v: Any, default: int) -> int:
    """Parse a positive integer form field, falling back to ``default``."""
    f = _opt_float(v)
    return int(f) if f is not None and f >= 1 else default


def _run_lines(child: dict[str, Any], pairs: list[tuple[str, Any]]) -> list[str]:
    """``["Run #N", "label: value", ...]`` — numbers to 4 sig figs."""
    lines = [f"Run #{int(child.get('job_id') or 0)}"]
    for label, value in pairs:
        if isinstance(value, int | float) and not isinstance(value, bool):
            lines.append(f"{label}: {float(value):.4g}")
        elif value is not None:
            lines.append(f"{label}: {value}")
    return lines


def _run_hover(child: dict[str, Any], pairs: list[tuple[str, Any]]) -> str:
    """Plotly hover string: bold run number then ``label: value`` lines.

    Used by every per-run scatter so a hovered point always names its run
    and summarises the conditions / values behind it.
    """
    lines = _run_lines(child, pairs)
    return "<br>".join([f"<b>{lines[0]}</b>", *lines[1:]])


def _run_detail(child: dict[str, Any], pairs: list[tuple[str, Any]]) -> str:
    """Plain one-line ``Run #N | label: value | ...`` for a tooltip."""
    return "  |  ".join(_run_lines(child, pairs))


# Highlight colours, shared by every run-overlay plot so the two
# independent highlights read the same everywhere:
#   - matrix selection  -> orange
#   - Pareto-row focus   -> green (coexists with the matrix selection)
_SEL_ORANGE = "rgba(214,95,14,0.95)"
_FOCUS_GREEN = "rgba(46,158,79,0.95)"
_DIM_GREY = "rgba(150,150,150,0.25)"

# Pareto / trade-offs plot palette. Resting state uses the brand teal for
# the optimal (non-dominated) points + front line and a muted grey for the
# dominated ones; the interactive highlights (orange selection, green focus)
# still override per-point. A neutral mid-grey marker outline reads on both
# the light and dark card surface, so it survives a live theme toggle.
_PARETO_TEAL = "#40A1A4"
_DOMINATED_GREY = "rgba(150,150,150,0.45)"
_MARKER_OUTLINE = {"color": "rgba(127,127,127,0.45)", "width": 1}


def _highlight_of(job: int, selected: set[int] | None, focus: int | None) -> str | None:
    """Highlight colour for one run, or ``None`` when nothing is highlighted.

    Focus (the Pareto-clicked run, green) wins over the matrix selection
    (orange); any other run dims when either highlight is active.
    """
    if focus is not None and job == focus:
        return _FOCUS_GREEN
    if selected and job in selected:
        return _SEL_ORANGE
    if selected or focus is not None:
        return _DIM_GREY
    return None


def _run_marker(state: dict[str, Any], jobs: list[int], *, base_size: int = 5) -> dict[str, Any]:
    """Marker dict for a run-overlay trace that honours both highlights.

    A matrix selection paints those runs orange and a Pareto-row click
    paints that run green (both larger), with everything else dimmed — the
    same convention as the unified scatter, so the two selections read the
    same on every plot. With neither active, a neutral marker.
    """
    selected = state.get("_selected_jobs")
    focus = state.get("_focus_job")
    if not (selected or focus is not None):
        return {"color": "rgba(0,0,0,0.45)", "size": base_size}
    colours = [_highlight_of(j, selected, focus) or _DIM_GREY for j in jobs]
    sizes = [
        base_size + 4
        if (_highlight_of(j, selected, focus) or _DIM_GREY) != _DIM_GREY
        else max(3, base_size - 1)
        for j in jobs
    ]
    return {"color": colours, "size": sizes}


def _render_summary_card(state: dict[str, Any]) -> None:
    """Per-response descriptive statistics + distribution (2-PS-c-stats #1).

    Reflects the matrix's column filters and surfaces missing / non-finite
    counts, so a diverged study is obvious at a glance.
    """
    results = state.get("results") or {}
    outputs = _scalar_outputs(list(results.get("children") or []))
    with section_card("query_stats", "Response summary & distribution"):
        if not outputs:
            ui.label("No numeric outputs to summarise yet.").style("color: #888;")
            return
        out_opts = {n: _output_label(results, n) for n in outputs}
        with ui.row().classes("w-full items-center gap-2"):
            sel = ui.select(out_opts, value=outputs[0], label="Output").props("dense")
            min_in = ui.number("Min").props("dense clearable").style("width: 110px;")
            max_in = ui.number("Max").props("dense clearable").style("width: 110px;")
            bins_in = ui.number("Bins", value=20, min=1).props("dense").style("width: 90px;")
            width_in = ui.number("Bin width", min=0).props("dense clearable").style("width: 120px;")
        ui.label(
            "Leave Min / Max blank to fit the data. Set a Bin width to fix the interval "
            "(it overrides Bins)."
        ).style("color: #888; font-size: 0.8rem;")
        body = ui.column().classes("w-full gap-2")

        def _draw() -> None:
            body.clear()
            name = str(sel.value or "")
            vals = [_scalar_of(c, name) for c in _filtered_children(state)]
            s = summarize(vals)
            stats = [
                ("n (finite)", str(s.n)),
                ("missing", str(s.n_missing)),
                ("non-finite", str(s.n_nonfinite)),
                ("min", _fmt_stat(s.minimum)),
                ("p5", _fmt_stat(s.p5)),
                ("median", _fmt_stat(s.median)),
                ("mean", _fmt_stat(s.mean)),
                ("p95", _fmt_stat(s.p95)),
                ("max", _fmt_stat(s.maximum)),
                ("std", _fmt_stat(s.std)),
                ("CV", _fmt_stat(s.cv)),
            ]
            with body:
                with ui.row().classes("w-full gap-5 flex-wrap"):
                    for label, value in stats:
                        with ui.column().classes("gap-0"):
                            ui.label(label).style(
                                "color: #888; font-size: 0.72rem; text-transform: uppercase;"
                            )
                            ui.label(value).style("font-weight: 600;")
                _draw_distribution(
                    results,
                    name,
                    vals,
                    vmin=_opt_float(min_in.value),
                    vmax=_opt_float(max_in.value),
                    nbins=_opt_int(bins_in.value, 20),
                    binwidth=_opt_float(width_in.value),
                )

        # Bins and Bin width are mutually exclusive: setting one clears the other.
        def _on_bins(_e: Any = None) -> None:
            if _opt_float(bins_in.value) is not None:
                width_in.value = None
            _draw()

        def _on_width(_e: Any = None) -> None:
            if _opt_float(width_in.value) is not None:
                bins_in.value = None
            _draw()

        for w in (sel, min_in, max_in):
            w.on("update:model-value", lambda _e=None: _draw())
        bins_in.on("update:model-value", _on_bins)
        width_in.on("update:model-value", _on_width)
        state["_summary_redraw"] = _draw
        _draw()


def _draw_distribution(
    results: dict[str, Any],
    name: str,
    vals: list[float | None],
    *,
    vmin: float | None = None,
    vmax: float | None = None,
    nbins: int = 20,
    binwidth: float | None = None,
) -> None:
    """Histogram of the finite response values, with explicit, user-set bins.

    ``vmin`` / ``vmax`` blank => fit the data. A positive ``binwidth`` fixes
    the interval (and overrides ``nbins``). Bars are built by hand (not
    Plotly's auto-binning) so each bar's hover can spell out its half-open
    interval ``[lo, hi)`` (the last bin is closed) and its run count.
    """
    finite = [v for v in vals if v is not None and math.isfinite(v)]
    if len(finite) < 2:
        ui.label("Not enough finite values to plot a distribution.").style("color: #888;")
        return
    start = vmin if vmin is not None else min(finite)
    end = vmax if vmax is not None else max(finite)
    if end <= start:
        end = start + (abs(start) or 1.0)
    if binwidth and binwidth > 0:
        size = binwidth
        nbins = max(1, math.ceil((end - start) / size))
    else:
        nbins = max(1, int(nbins))
        size = (end - start) / nbins
    if nbins > 500:  # guard against a tiny bin width / huge bin count hanging the browser
        nbins = 500
        size = (end - start) / nbins

    edges = [start + i * size for i in range(nbins + 1)]
    edges[-1] = max(edges[-1], end)  # guard float drift on the closing edge
    counts = [0] * nbins
    n_below = n_above = 0
    for v in finite:
        if v < start:
            n_below += 1
        elif v > edges[-1]:
            n_above += 1
        else:
            idx = min(int((v - start) / size), nbins - 1)  # v == end -> last (closed) bin
            counts[idx] += 1

    dec = _decimals_for(edges)
    label = _output_label(results, name)
    centres = [(edges[i] + edges[i + 1]) / 2 for i in range(nbins)]
    widths = [edges[i + 1] - edges[i] for i in range(nbins)]
    hover = [
        f"{label}<br>Range: [{edges[i]:.{dec}f}, {edges[i + 1]:.{dec}f}"
        f"{']' if i == nbins - 1 else ')'}<br>Num runs: {counts[i]}"
        for i in range(nbins)
    ]
    fig = go.Figure(
        go.Bar(
            x=centres,
            y=counts,
            width=widths,
            marker_color="#2c7fb8",
            customdata=hover,
            hovertemplate="%{customdata}<extra></extra>",
        )
    )
    fig.update_layout(
        xaxis_title=label,
        yaxis_title="Count",
        height=320,
        margin={"l": 60, "r": 20, "t": 20, "b": 50},
        bargap=0,
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")
    if n_below or n_above:
        notes = []
        if n_below:
            notes.append(f"{n_below} run(s) below Min")
        if n_above:
            notes.append(f"{n_above} run(s) above Max")
        ui.label("Excluded from the histogram: " + ", ".join(notes)).style(
            "color: #b58900; font-size: 0.8rem;"
        )


def _render_plot_card(state: dict[str, Any]) -> None:
    """Unified plot card: chart type + X/Y/Z/colour pickers, all in one place (2-PS-b').

    Variables (X/Y/Z/colour) are the varied inputs + scalar outputs, shown
    by label. ``Z = (none)`` gives a 2D scatter; choosing Z makes it 3D.
    Parallel-coordinates and the csv_plot curve-overlay are chart types.
    """
    results = state.get("results") or {}
    children = list(results.get("children") or [])
    variables = _plot_variables(state)
    var_keys = list(variables)
    csv_plots = list((results.get("response_sources") or {}).get("csv_plots") or [])
    # Section headings live in the picker's options (non-selectable), so the
    # option dicts and the "first real variable" defaults differ from the
    # plain variable map.
    sectioned = sectioned_variables(variables)
    none_then_vars = {"": "(none)", **sectioned}

    chart_types: list[str] = []
    if var_keys:
        chart_types += ["Scatter (2D / 3D)", "Parallel coordinates"]
    if csv_plots:
        chart_types.append("Curve overlay")

    with section_card("show_chart", "Plot"):
        if not chart_types:
            ui.label("Nothing to plot yet (no varied inputs or csv_plot outputs).").style(
                "color: #888;"
            )
            return
        with ui.row().classes("w-full items-center gap-2"):
            chart = ui.select(chart_types, value=chart_types[0], label="Chart").props("dense")
            ctrl_box = ui.row().classes("items-center gap-2 flex-grow")
        plot_box = ui.column().classes("w-full")

        def _draw_scatter_now(_e: Any = None) -> None:
            plot_box.clear()
            with plot_box:
                _draw_scatter(
                    state,
                    x_key=str(state["_pl_x"].value or ""),
                    y_key=str(state["_pl_y"].value or ""),
                    z_key=str(state["_pl_z"].value or ""),
                    colour_key=str(state["_pl_c"].value or ""),
                )

        def _draw_parcoords_now(_e: Any = None) -> None:
            plot_box.clear()
            with plot_box:
                _draw_parcoords(
                    state,
                    colour_key=str(state["_pl_c"].value or ""),
                    hide_constant=bool(
                        state.get("_pl_hide_const") and state["_pl_hide_const"].value
                    ),
                )

        async def _draw_overlay(_e: Any = None) -> None:
            await _draw_overlay_plot(state, plot_box, csv_plots, children)

        def _rebuild(_e: Any = None) -> None:
            ctrl_box.clear()
            with ctrl_box:
                if chart.value == "Curve overlay":
                    out_opts = {
                        str(p.get("name") or ""): str(p.get("label") or p.get("name") or "")
                        for p in csv_plots
                    }
                    colour_opts = {
                        "": "(none)",
                        **{n: _input_label(results, n) for n in _numeric_inputs(children)},
                    }
                    state["_ov_out"] = ui.select(
                        out_opts, value=next(iter(out_opts), None), label="CSV output"
                    ).props("dense")
                    state["_ov_x"] = ui.number("X column", value=0, min=0).props("dense")
                    state["_ov_y"] = ui.number("Y column", value=1, min=0).props("dense")
                    state["_ov_colour"] = ui.select(colour_opts, value="", label="Colour by").props(
                        "dense"
                    )
                    for w in (
                        state["_ov_out"],
                        state["_ov_x"],
                        state["_ov_y"],
                        state["_ov_colour"],
                    ):
                        w.on("update:model-value", _draw_overlay)
                    state["_plot_redraw"] = _draw_overlay
                    ui.timer(0.0, _draw_overlay, once=True)
                    return
                if chart.value == "Parallel coordinates":
                    state["_pl_c"] = ui.select(none_then_vars, value="", label="Colour by").props(
                        "dense"
                    )
                    guard_section_headers(state["_pl_c"], _draw_parcoords_now)
                    # Off by default: every input the study chose is worth
                    # showing until the reader says otherwise. Held-constant
                    # inputs are the ones this drops.
                    state["_pl_hide_const"] = ui.checkbox("Hide unvaried axes", value=False).props(
                        "dense"
                    )
                    state["_pl_hide_const"].on("update:model-value", _draw_parcoords_now)
                    state["_plot_redraw"] = _draw_parcoords_now
                    _draw_parcoords_now()
                    return
                # Scatter (2D / 3D): Z empty -> 2D, Z chosen -> 3D.
                first = _first_variable(sectioned)
                state["_pl_x"] = ui.select(sectioned, value=first, label="X").props("dense")
                state["_pl_y"] = ui.select(
                    sectioned, value=_first_variable(sectioned, skip=first) or first, label="Y"
                ).props("dense")
                state["_pl_z"] = ui.select(none_then_vars, value="", label="Z (3D)").props("dense")
                state["_pl_c"] = ui.select(none_then_vars, value="", label="Colour by").props(
                    "dense"
                )
                for w in (state["_pl_x"], state["_pl_y"], state["_pl_z"], state["_pl_c"]):
                    guard_section_headers(w, _draw_scatter_now)
                state["_plot_redraw"] = _draw_scatter_now
                _draw_scatter_now()

        chart.on("update:model-value", _rebuild)
        _rebuild()


async def _draw_overlay_plot(
    state: dict[str, Any],
    plot_box: ui.column,
    csv_plots: list[dict[str, Any]],
    children: list[dict[str, Any]],
) -> None:
    """Overlay every child's csv_plot series, one trace per child (2-PS-b4)."""
    client: ApiClient = state["client"]
    study_id = int(state["study_id"])
    out = state["_ov_out"].value
    plot_box.clear()
    if not out:
        return
    x_col = int(state["_ov_x"].value or 0)
    y_col = int(state["_ov_y"].value or 1)
    colour_by = str(state["_ov_colour"].value or "")
    results = state.get("results") or {}
    try:
        res = await client.get_study_csv_series(
            study_id, output=str(out), x_column=x_col, y_column=y_col
        )
    except ApiError as exc:
        with plot_box:
            ui.label(exc.detail or "Couldn't load the series.").style(
                "color: var(--matofx-error-text);"
            )
        return
    series = list(res.get("children") or [])
    allowed = state.get("_row_filter_jobs")
    if allowed is not None:
        series = [s for s in series if int(s.get("job_id") or 0) in allowed]
    if not series:
        with plot_box:
            ui.label("No series for the current filter / output.").style("color: #888;")
        return
    inputs_by_job = {int(c.get("job_id") or 0): (c.get("inputs") or {}) for c in children}
    meta = next((p for p in csv_plots if str(p.get("name")) == str(out)), {})
    clabel = _input_label(results, colour_by) if colour_by else ""
    fig = go.Figure()
    for s in series:
        jid = int(s.get("job_id") or 0)
        cval = inputs_by_job.get(jid, {}).get(colour_by)
        name = f"{clabel}={cval}" if colour_by else f"#{jid}"
        fig.add_trace(go.Scatter(x=s.get("x") or [], y=s.get("y") or [], mode="lines", name=name))
    fig.update_layout(
        title=str(meta.get("plot_title") or out),
        xaxis_title=str(meta.get("xaxis_title") or f"column {x_col}"),
        yaxis_title=str(meta.get("yaxis_title") or f"column {y_col}"),
        height=460,
        margin={"l": 60, "r": 20, "t": 50, "b": 50},
        legend={"orientation": "h", "y": -0.2},
    )
    with plot_box:
        ui.plotly(themed_plotly(fig)).classes("w-full")


def _render_sensitivity_card(state: dict[str, Any]) -> None:
    """Sensitivity to the varied inputs: per-output tornado + an SRC table (2-PS-b').

    The bar chart ranks the varied inputs for one chosen output (sign =
    direction). The table shows the standardized regression coefficient
    for every varied input against several chosen outputs at once.
    """
    results = state.get("results") or {}
    children = list(results.get("children") or [])
    outputs = _scalar_outputs(children)
    with section_card("insights", "Sensitivity"):
        if not outputs or not _varied_inputs(children):
            ui.label("Needs at least one varied numeric input and one scalar output.").style(
                "color: #888;"
            )
            return
        out_opts = {n: _output_label(results, n) for n in outputs}
        ui.label(
            "How strongly each varied input drives the selected output. "
            "Standardized regression coefficient (SRC): the input's effect with every input put "
            "on a common scale - sign is the direction, magnitude (about 0 to 1) is the relative "
            "strength, and a 95% confidence interval crossing zero means it is not statistically "
            "significant. Pearson measures linear correlation and Spearman measures rank "
            "(monotonic) correlation; a large gap between the two flags a nonlinear "
            "but monotonic effect."
        ).style("color: #888; font-size: 0.85rem;")
        ui.label(
            "When to use it: your quick, always-available read of which inputs "
            "matter and which way. Best when the response varies smoothly; for "
            "strong curvature or interactions, cross-check Global sensitivity "
            "(Sobol) under Advanced statistics, which can see effects a linear "
            "fit cannot."
        ).style("color: #888; font-size: 0.85rem; font-style: italic;")

        with ui.row().classes("w-full items-center gap-2"):
            bar_sel = ui.select(out_opts, value=outputs[0], label="Output").props("dense")
        bar_box = ui.column().classes("w-full")

        async def _redraw_bars() -> None:
            try:
                await _draw_sensitivity_bars(state, bar_box, str(bar_sel.value or ""))
            except Exception as exc:  # surface, don't vanish
                bar_box.clear()
                with bar_box:
                    ui.label(f"Sensitivity chart failed: {exc}").style(
                        "color: var(--matofx-error-text);"
                    )

        bar_sel.on("update:model-value", _redraw_bars)
        ui.timer(0.0, _redraw_bars, once=True)

        ui.separator()
        ui.label("Coefficient table - standardized regression").style("font-weight: 600;")
        ui.label(
            "Each cell is the standardized regression coefficient of that input "
            "for that output (sign = direction, magnitude = relative influence)."
        ).style("color: #888; font-size: 0.85rem;")
        default_cols = outputs[: min(3, len(outputs))]
        table_sel = (
            ui.select(out_opts, value=default_cols, multiple=True, label="Outputs")
            .props("dense")
            .classes("w-full")
        )
        table_box = ui.column().classes("w-full")

        async def _redraw_table() -> None:
            try:
                await _draw_sensitivity_table(
                    state, table_box, [str(v) for v in (table_sel.value or [])]
                )
            except Exception as exc:  # surface, don't vanish
                table_box.clear()
                with table_box:
                    ui.label(f"Sensitivity table failed: {exc}").style(
                        "color: var(--matofx-error-text);"
                    )

        table_sel.on("update:model-value", _redraw_table)
        ui.timer(0.0, _redraw_table, once=True)


async def _draw_sensitivity_bars(
    state: dict[str, Any], plot_box: ui.column, output_name: str
) -> None:
    """Tornado (SRC) + Pearson/Spearman bars for one output vs the varied inputs."""
    client: ApiClient = state["client"]
    study_id = int(state["study_id"])
    results = state.get("results") or {}
    plot_box.clear()
    if not output_name:
        return
    try:
        res = await client.get_study_sensitivity(study_id, response=output_name, source="scalar")
    except ApiError as exc:
        with plot_box:
            ui.label(exc.detail or "Couldn't compute sensitivity.").style(
                "color: var(--matofx-error-text);"
            )
        return
    factors = list(res.get("factors") or [])
    if not res.get("sufficient") or not factors:
        with plot_box:
            ui.label(
                str(res.get("message") or "Not enough completed runs for sensitivity yet.")
            ).style("color: #888;")
        return
    labels = [_input_label(results, str(f.get("factor") or "")) for f in factors]
    src = [f.get("std_coef") for f in factors]
    pear = [f.get("pearson") for f in factors]
    spear = [f.get("spearman") for f in factors]
    height = max(260, 70 + 30 * len(labels))

    def _nan(v: Any) -> float:
        return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else float("nan")

    with plot_box:
        bits: list[str] = []
        for caption, key in (("R-squared", "r_squared"), ("adj R-squared", "adj_r_squared")):
            v = res.get(key)
            if isinstance(v, int | float):
                bits.append(f"{caption} = {float(v):.3f}")
        rmse = res.get("rmse")
        if isinstance(rmse, int | float):
            bits.append(f"RMSE = {float(rmse):.3g}")
        bits.append(f"{int(res.get('n_samples') or 0)} sample(s)")
        ui.label("  -  ".join(bits)).style("color: #888; font-size: 0.85rem;")
        msg = res.get("message")
        if msg:
            ui.label(str(msg)).style("color: #b58900; font-size: 0.85rem;")

        if any(s is not None for s in src):
            srcx = [float(s) if s is not None else 0.0 for s in src]
            ci_lo = [f.get("std_coef_ci_low") for f in factors]
            ci_hi = [f.get("std_coef_ci_high") for f in factors]
            plus = [
                (float(h) - x) if h is not None else 0.0 for x, h in zip(srcx, ci_hi, strict=True)
            ]
            minus = [
                (x - float(lo)) if lo is not None else 0.0
                for x, lo in zip(srcx, ci_lo, strict=True)
            ]
            has_ci = any(p > 0 or m > 0 for p, m in zip(plus, minus, strict=True))
            error_x = (
                {
                    "type": "data",
                    "symmetric": False,
                    "array": plus,
                    "arrayminus": minus,
                    "thickness": 1.2,
                    "color": "#555",
                }
                if has_ci
                else None
            )
            if has_ci:
                nan = float("nan")
                src_customdata = [
                    [
                        float(lo) if lo is not None else nan,
                        float(hi) if hi is not None else nan,
                    ]
                    for lo, hi in zip(ci_lo, ci_hi, strict=True)
                ]
                src_hover = (
                    "%{y}<br>SRC = %{x:.3f}"
                    "<br>95% CI [%{customdata[0]:.3f}, %{customdata[1]:.3f}]<extra></extra>"
                )
            else:
                src_customdata = None
                src_hover = "%{y}<br>SRC = %{x:.3f}<extra></extra>"
            fig = go.Figure(
                go.Bar(
                    x=srcx,
                    y=labels,
                    orientation="h",
                    marker_color=["#2c7fb8" if (s is None or s >= 0) else "#d95f0e" for s in src],
                    error_x=error_x,
                    customdata=src_customdata,
                    hovertemplate=src_hover,
                )
            )
            fig.update_layout(
                title="Standardized regression coefficients (95% CI)",
                xaxis_title=(
                    "Standardized coefficient (sign = direction; CI crossing 0 = not significant)"
                ),
                height=height,
                margin={"l": 170, "r": 20, "t": 50, "b": 50},
            )
            fig.update_yaxes(autorange="reversed")
            ui.plotly(themed_plotly(fig)).classes("w-full")

        fig2 = go.Figure()
        fig2.add_trace(
            go.Bar(
                name="Pearson",
                x=[float(p) if p is not None else 0.0 for p in pear],
                y=labels,
                orientation="h",
                customdata=[_nan(f.get("pearson_p")) for f in factors],
                hovertemplate="%{y}: r=%{x:.3f}, p=%{customdata:.3g}<extra>Pearson</extra>",
            )
        )
        fig2.add_trace(
            go.Bar(
                name="Spearman",
                x=[float(s) if s is not None else 0.0 for s in spear],
                y=labels,
                orientation="h",
                customdata=[_nan(f.get("spearman_p")) for f in factors],
                hovertemplate="%{y}: rho=%{x:.3f}, p=%{customdata:.3g}<extra>Spearman</extra>",
            )
        )
        fig2.update_layout(
            barmode="group",
            title="Correlation with response",
            xaxis_title="Correlation coefficient (-1 to 1)",
            height=height,
            margin={"l": 170, "r": 20, "t": 50, "b": 50},
            legend={"orientation": "h", "y": -0.2},
        )
        fig2.update_yaxes(autorange="reversed")
        ui.plotly(themed_plotly(fig2)).classes("w-full")


async def _draw_sensitivity_table(
    state: dict[str, Any], table_box: ui.column, output_names: list[str]
) -> None:
    """SRC grid: varied inputs (rows) x chosen scalar outputs (columns) (2-PS-b')."""
    client: ApiClient = state["client"]
    study_id = int(state["study_id"])
    results = state.get("results") or {}
    table_box.clear()
    if not output_names:
        with table_box:
            ui.label("Pick one or more outputs.").style("color: #888;")
        return
    try:
        res = await client.get_study_sensitivity_matrix(study_id, responses=output_names)
    except ApiError as exc:
        with table_box:
            ui.label(exc.detail or "Couldn't compute the table.").style(
                "color: var(--matofx-error-text);"
            )
        return
    factors = list(res.get("factors") or [])
    responses = list(res.get("responses") or [])
    if not factors or not responses:
        with table_box:
            ui.label("No varied inputs / outputs to tabulate.").style("color: #888;")
        return
    cols: list[dict[str, Any]] = [
        {"name": "factor", "label": "Input", "field": "factor", "sortable": True, "align": "left"}
    ]
    cols.extend(
        {"name": o, "label": _output_label(results, o), "field": o, "sortable": True}
        for o in responses
    )
    rows: list[dict[str, Any]] = []
    for r in res.get("rows") or []:
        coeffs = r.get("coefficients") or {}
        pvals = r.get("pvalues") or {}
        row: dict[str, Any] = {"factor": _input_label(results, str(r.get("factor") or ""))}
        for o in responses:
            v = coeffs.get(o)
            if isinstance(v, int | float):
                p = pvals.get(o)
                star = "*" if isinstance(p, int | float) and p < 0.05 else ""
                row[o] = f"{float(v):.3f}{star}"
            else:
                row[o] = "-"
        rows.append(row)
    r2 = res.get("r_squared") or {}
    with table_box:
        ui.table(columns=cols, rows=rows).classes("w-full").props("dense flat")
        ui.label("* significant at p < 0.05").style("color: #888; font-size: 0.78rem;")
        caption = "  -  ".join(
            f"{_output_label(results, o)} R-squared = {float(r2[o]):.3f}"
            for o in responses
            if isinstance(r2.get(o), int | float)
        )
        if caption:
            ui.label(caption).style("color: #888; font-size: 0.8rem;")


def _render_tradeoffs_card(state: dict[str, Any]) -> None:
    """Best run per objective + multi-objective Pareto front (2-PS-c-stats #4)."""
    results = state.get("results") or {}
    outputs = _scalar_outputs(list(results.get("children") or []))
    with section_card("balance", "Trade-offs (Pareto) & best runs"):
        if not outputs:
            ui.label("No numeric outputs to optimise.").style("color: #888;")
            return
        out_opts = {n: _output_label(results, n) for n in outputs}
        sel = (
            ui.select(
                out_opts, value=outputs[: min(2, len(outputs))], multiple=True, label="Objectives"
            )
            .props("dense")
            .classes("w-full")
        )
        dir_box = ui.row().classes("w-full items-center gap-3 flex-wrap")
        out_box = ui.column().classes("w-full")
        directions: dict[str, str] = state.setdefault("_obj_dir", {})

        def _draw() -> None:
            out_box.clear()
            objs = [str(v) for v in (sel.value or [])]
            with out_box:
                if not objs:
                    ui.label("Pick at least one objective.").style("color: #888;")
                    return
                children = _filtered_children(state)
                maximize = [directions.get(o, "min") == "max" for o in objs]
                _render_best_runs(results, children, objs, maximize)
                if len(objs) >= 2:
                    _render_pareto(state, results, children, objs, maximize)

        def _rebuild_dirs() -> None:
            dir_box.clear()
            objs = [str(v) for v in (sel.value or [])]
            with dir_box:
                for o in objs:
                    directions.setdefault(o, "min")
                    picker = ui.select(
                        {"min": "minimise", "max": "maximise"},
                        value=directions[o],
                        label=_output_label(results, o),
                    ).props("dense")

                    def _set(_e: Any = None, _o: str = o, _p: Any = picker) -> None:
                        directions[_o] = str(_p.value or "min")
                        _draw()

                    picker.on("update:model-value", _set)
            _draw()

        # A matrix selection (or a Pareto-row click) re-highlights the front.
        state.setdefault("_plot_redraws", []).append(_draw)
        sel.on("update:model-value", lambda _e=None: _rebuild_dirs())
        _rebuild_dirs()


def _render_best_runs(
    results: dict[str, Any],
    children: list[dict[str, Any]],
    objs: list[str],
    maximize: list[bool],
) -> None:
    """Per-objective best / worst run table."""
    cols: list[dict[str, Any]] = [
        {"name": "obj", "label": "Objective", "field": "obj", "align": "left"},
        {"name": "goal", "label": "Goal", "field": "goal"},
        {"name": "best", "label": "Best", "field": "best"},
        {"name": "bestrun", "label": "Best run", "field": "bestrun"},
        {"name": "worst", "label": "Worst", "field": "worst"},
    ]
    rows: list[dict[str, Any]] = []
    for o, mx in zip(objs, maximize, strict=True):
        finite = [
            (int(c.get("job_id") or 0), val)
            for c in children
            if (val := _scalar_of(c, o)) is not None and math.isfinite(val)
        ]
        if not finite:
            rows.append(
                {
                    "obj": _output_label(results, o),
                    "goal": "-",
                    "best": "-",
                    "bestrun": "-",
                    "worst": "-",
                }
            )
            continue
        best = max(finite, key=lambda t: t[1]) if mx else min(finite, key=lambda t: t[1])
        worst = min(finite, key=lambda t: t[1]) if mx else max(finite, key=lambda t: t[1])
        rows.append(
            {
                "obj": _output_label(results, o),
                "goal": "maximise" if mx else "minimise",
                "best": _fmt_stat(best[1]),
                "bestrun": f"#{best[0]}",
                "worst": _fmt_stat(worst[1]),
            }
        )
    ui.table(columns=cols, rows=rows).props("dense flat").classes("w-full")


def _render_pareto(
    state: dict[str, Any],
    results: dict[str, Any],
    children: list[dict[str, Any]],
    objs: list[str],
    maximize: list[bool],
) -> None:
    """Pareto front over 2+ objectives: plot (2D/3D) + the non-dominated runs.

    The scatter honours the matrix selection (selected runs orange, the
    rest dimmed); the run list below is hover-for-details and
    click-to-highlight.
    """
    obj_rows = [[_scalar_of(c, o) for o in objs] for c in children]
    nd = set(pareto_indices(obj_rows, maximize))
    labels = [_output_label(results, o) for o in objs]
    # (child index, [finite objective values]) for every complete run.
    complete: list[tuple[int, list[float]]] = []
    for i in range(len(children)):
        vals = [v for v in obj_rows[i] if v is not None and math.isfinite(v)]
        if len(vals) == len(objs):
            complete.append((i, vals))
    hover = [_run_hover(children[i], list(zip(labels, v, strict=True))) for i, v in complete]
    job_of = [int(children[i].get("job_id") or 0) for i, _ in complete]
    selected: set[int] | None = state.get("_selected_jobs")
    focus: int | None = state.get("_focus_job")
    active = bool(selected) or focus is not None
    # Per-point highlight colour (green focus over orange selection over dim),
    # or None when nothing is highlighted.
    hl = [_highlight_of(j, selected, focus) for j in job_of]

    if len(objs) == 2:
        on = [i in nd for i, _ in complete]
        if active:
            colours = [h or _DIM_GREY for h in hl]
            sizes = [13 if h != _DIM_GREY else 6 for h in hl]
        else:
            colours = [_PARETO_TEAL if o else _DOMINATED_GREY for o in on]
            sizes = [12 if o else 6 for o in on]
        fig = go.Figure(
            go.Scatter(
                x=[v[0] for _, v in complete],
                y=[v[1] for _, v in complete],
                mode="markers",
                marker={"color": colours, "size": sizes, "line": _MARKER_OUTLINE},
                name="runs",
                customdata=hover,
                hovertemplate="%{customdata}<extra></extra>",
            )
        )
        front = sorted(((v[0], v[1]) for i, v in complete if i in nd), key=lambda t: t[0])
        if front:
            fig.add_trace(
                go.Scatter(
                    x=[p[0] for p in front],
                    y=[p[1] for p in front],
                    mode="lines",
                    line={"color": _PARETO_TEAL, "width": 2},
                    name="Pareto front",
                    hoverinfo="skip",
                )
            )
        fig.update_layout(
            xaxis_title=labels[0],
            yaxis_title=labels[1],
            height=420,
            margin={"l": 70, "r": 20, "t": 30, "b": 50},
            legend={"orientation": "h", "y": -0.2},
            hoverlabel={"align": "left"},
        )
        ui.plotly(themed_plotly(fig)).classes("w-full")
    elif len(objs) == 3:
        if active:
            colours3 = [h or _DIM_GREY for h in hl]
            sizes3 = [8 if h != _DIM_GREY else 3 for h in hl]
        else:
            colours3 = [_PARETO_TEAL if i in nd else _DOMINATED_GREY for i, _ in complete]
            sizes3 = [7 if i in nd else 3 for i, _ in complete]
        fig = go.Figure(
            go.Scatter3d(
                x=[v[0] for _, v in complete],
                y=[v[1] for _, v in complete],
                z=[v[2] for _, v in complete],
                mode="markers",
                marker={"size": sizes3, "color": colours3, "line": _MARKER_OUTLINE},
                customdata=hover,
                hovertemplate="%{customdata}<extra></extra>",
            )
        )
        fig.update_layout(
            scene={"xaxis_title": labels[0], "yaxis_title": labels[1], "zaxis_title": labels[2]},
            height=520,
            margin={"l": 0, "r": 0, "t": 30, "b": 0},
        )
        ui.plotly(themed_plotly(fig)).classes("w-full")

    ui.label(
        f"{len(nd)} Pareto-optimal of {len(complete)} complete run(s) - hover a row for "
        "details, click it to highlight that run (green) on the plots. Matrix selections "
        "stay orange; click again to clear."
    ).style("color: #888; font-size: 0.85rem;")

    async def _focus_run(jid: int) -> None:
        # Independent of the matrix selection: a Pareto click sets (or
        # toggles off) the green focus run, leaving any orange selection.
        state["_focus_job"] = None if state.get("_focus_job") == jid else jid
        trig = state.get("_trigger_redraw")
        if trig is not None:
            res = trig()
            if res is not None and hasattr(res, "__await__"):
                await res

    varied = _varied_inputs(children)
    # The non-dominated run list — a hand-built table (it needs per-row
    # click-to-focus + selection/focus tints + hover tooltips that a plain
    # ui.table can't carry), so it's themed by hand to the card idiom:
    # hairline rule lines + muted header, not hard-coded greys.
    with ui.column().classes("w-full gap-0").style("margin-top: 0.5rem;"):
        with (
            ui.row()
            .classes("w-full items-center gap-2 px-2 py-1")
            .style(
                "font-weight: 600; font-size: 0.8rem; color: var(--matofx-muted-text); "
                "border-bottom: 1px solid var(--matofx-card-border);"
            )
        ):
            ui.label("Run").style("width: 70px;")
            for lab in labels:
                ui.label(lab).classes("flex-grow")
        seen: set[int] = set()
        for i in sorted(nd):
            c = children[i]
            jid = int(c.get("job_id") or 0)
            # Guard against the same job surfacing twice (e.g. duplicate
            # result rows): one row per run.
            if jid in seen:
                continue
            seen.add(jid)
            detail_pairs = [(_input_label(results, f), _var_value(c, f"in:{f}")) for f in varied]
            detail_pairs += [(labels[k], _scalar_of(c, o)) for k, o in enumerate(objs)]
            if focus is not None and jid == focus:
                tint = " background: rgba(46,158,79,0.16);"  # green: this Pareto-focused run
            elif selected is not None and jid in selected:
                tint = " background: rgba(214,95,14,0.12);"  # orange: in the matrix selection
            else:
                tint = ""
            row = (
                ui.row()
                .classes("w-full items-center gap-2 px-2 py-1 cursor-pointer hover:bg-gray-50")
                .style("border-bottom: 1px solid var(--matofx-card-border);" + tint)
            )
            row.on("click", lambda _e=None, _j=jid: _focus_run(_j))
            with row:
                ui.label(f"#{jid}").style("width: 70px; color: var(--matofx-muted-text);")
                for o in objs:
                    ui.label(_fmt_stat(_scalar_of(c, o))).classes("flex-grow")
                ui.tooltip(_run_detail(c, detail_pairs)).classes("text-xs")


def _decimals_for(values: list[float]) -> int:
    """A sensible decimal count for hover text, from the spread of values.

    Aims for ~3 significant digits across the data range, clamped to a
    readable 0 to 6 (so a 0..100 range shows 1 dp, 0..1 shows 3, huge
    ranges show 0).
    """
    finite = [v for v in values if math.isfinite(v)]
    if not finite:
        return 2
    rng = max(finite) - min(finite)
    if rng <= 0:
        rng = abs(finite[0]) or 1.0
    return max(0, min(6, 3 - math.floor(math.log10(rng))))


def _draw_scatter(
    state: dict[str, Any], *, x_key: str, y_key: str, z_key: str, colour_key: str
) -> None:
    """2D (Z empty) or 3D (Z set) scatter over the chosen variables (2-PS-b').

    Hover shows each axis by its label (not "x/y/z"), the colour variable
    when set, and numbers rounded to a range-appropriate precision.
    """
    children = _filtered_children(state)
    variables = _plot_variables(state)
    levels = _plot_levels(state)
    # A section heading is a label in the picker, not a variable. It cannot
    # normally be selected, but a stale stored pick must not silently plot an
    # empty chart either.
    x_key, y_key, z_key, colour_key = (
        "" if is_section_header(k) else k for k in (x_key, y_key, z_key, colour_key)
    )
    if not x_key or not y_key:
        ui.label("Pick X and Y variables.").style("color: #888;")
        return

    # A choice-valued variable has no number to plot. It takes one column per
    # level instead, and the level's *name* rides in customdata so the hover
    # reads "Aluminium" rather than the column index.
    cat_x, cat_y = levels.get(x_key), levels.get(y_key)
    cat_z = levels.get(z_key) if z_key else None
    cat_c = levels.get(colour_key) if colour_key else None
    positional = [cat_x, cat_y] + ([cat_z] if z_key else [])
    # Nudge only when no continuous axis is left to spread the runs apart.
    nudge = all(c is not None for c in positional)

    def _position(key: str, cats: list[str] | None) -> list[float | None]:
        if cats is None:
            return [_var_value(c, key) for c in children]
        return category_positions(cats, [_var_text(c, key) for c in children], jitter=nudge)

    px, py = _position(x_key, cat_x), _position(y_key, cat_y)
    pz = _position(z_key, cat_z) if z_key else None
    pc = _position(colour_key, cat_c) if colour_key else None

    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    cs: list[float] = []
    jobs: list[int] = []
    texts: dict[str, list[str]] = {"x": [], "y": [], "z": [], "c": []}
    for i, c in enumerate(children):
        xv, yv = px[i], py[i]
        zv = pz[i] if pz is not None else None
        if xv is None or yv is None or (z_key and zv is None):
            continue
        xs.append(xv)
        ys.append(yv)
        if z_key and zv is not None:
            zs.append(zv)
        cv = pc[i] if pc is not None else None
        cs.append(cv if cv is not None else 0.0)
        jobs.append(int(c.get("job_id") or 0))
        for slot, key, cats in (
            ("x", x_key, cat_x),
            ("y", y_key, cat_y),
            ("z", z_key, cat_z),
            ("c", colour_key, cat_c),
        ):
            if key and cats is not None:
                texts[slot].append(_var_text(c, key))
    if not xs:
        ui.label("No children have values for the chosen variables yet.").style("color: #888;")
        return
    xlab = variables.get(x_key, x_key)
    ylab = variables.get(y_key, y_key)
    xf, yf = _decimals_for(xs), _decimals_for(ys)
    # customdata[0] is always the run number, so every point names its run;
    # each choice-valued axis then appends its level text, and the numeric
    # colour variable (if any) its value. Indices are assigned in order as the
    # hover template is built, so the two can never drift apart.
    customdata: list[list[Any]] = [[float(j)] for j in jobs]

    def _hover_for(slot: str, key: str, cats: list[str] | None, fallback: str) -> str:
        """Level name from customdata for a choice axis, else the raw number."""
        if not key or cats is None:
            return fallback
        for row, text in zip(customdata, texts[slot], strict=True):
            row.append(text)
        return f"%{{customdata[{len(customdata[0]) - 1}]}}"

    x_hover = _hover_for("x", x_key, cat_x, f"%{{x:.{xf}f}}")
    y_hover = _hover_for("y", y_key, cat_y, f"%{{y:.{yf}f}}")
    ht_colour = ""
    if colour_key:
        clab = variables.get(colour_key, colour_key)
        if cat_c is not None:
            ht_colour = f"<br>{clab}: " + _hover_for("c", colour_key, cat_c, "")
        else:
            cf = _decimals_for(cs)
            for row, cval in zip(customdata, cs, strict=True):
                row.append(cval)
            ht_colour = f"<br>{clab}: %{{customdata[{len(customdata[0]) - 1}]:.{cf}f}}"

    # Matrix selection (orange) and a Pareto-focused run (green) highlight
    # those runs here (others dimmed), overriding the colour-by shading.
    selected = state.get("_selected_jobs")
    focus = state.get("_focus_job")
    base = 5 if z_key else 8
    marker: dict[str, Any]
    if selected or focus is not None:
        hl = [_highlight_of(j, selected, focus) for j in jobs]
        marker = {
            "size": [base + 4 if h != _DIM_GREY else max(3, base - 3) for h in hl],
            "color": [h or _DIM_GREY for h in hl],
        }
    elif colour_key:
        marker = {
            "size": base,
            "color": cs,
            "colorscale": "Viridis",
            "showscale": True,
            "colorbar": {"title": variables.get(colour_key, colour_key)},
        }
        if cat_c is not None:
            # Flat equal bands, one per level, each named on the bar — a
            # continuous ramp over level indices would invite the eye to
            # read a halfway option that does not exist.
            marker["colorscale"] = stepped_colorscale(category_colours(len(cat_c)))
            marker["cmin"], marker["cmax"] = -0.5, len(cat_c) - 0.5
            marker["colorbar"] |= {
                "tickmode": "array",
                "tickvals": list(range(len(cat_c))),
                "ticktext": list(cat_c),
            }
    else:
        marker = {"size": base}
    if z_key:
        zlab = variables.get(z_key, z_key)
        zf = _decimals_for(zs)
        z_hover = _hover_for("z", z_key, cat_z, f"%{{z:.{zf}f}}")
        hovertemplate = (
            f"<b>Run #%{{customdata[0]:d}}</b><br>{xlab}: {x_hover}"
            f"<br>{ylab}: {y_hover}"
            f"<br>{zlab}: {z_hover}{ht_colour}<extra></extra>"
        )
        fig = go.Figure(
            go.Scatter3d(
                x=xs,
                y=ys,
                z=zs,
                mode="markers",
                marker=marker,
                customdata=customdata,
                hovertemplate=hovertemplate,
            )
        )
        fig.update_layout(
            scene={
                "xaxis": {"title": xlab, **(category_axis(cat_x) if cat_x else {})},
                "yaxis": {"title": ylab, **(category_axis(cat_y) if cat_y else {})},
                "zaxis": {"title": zlab, **(category_axis(cat_z) if cat_z else {})},
            },
            height=520,
            margin={"l": 0, "r": 0, "t": 30, "b": 0},
        )
    else:
        hovertemplate = (
            f"<b>Run #%{{customdata[0]:d}}</b><br>{xlab}: {x_hover}"
            f"<br>{ylab}: {y_hover}{ht_colour}<extra></extra>"
        )
        fig = go.Figure(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers",
                marker=marker,
                customdata=customdata,
                hovertemplate=hovertemplate,
            )
        )
        fig.update_layout(
            xaxis_title=xlab,
            yaxis_title=ylab,
            height=460,
            margin={"l": 70, "r": 20, "t": 30, "b": 50},
        )
        if cat_x:
            fig.update_xaxes(**category_axis(cat_x))
        if cat_y:
            fig.update_yaxes(**category_axis(cat_y))
    ui.plotly(themed_plotly(fig)).classes("w-full")


def varying_keys(columns: Mapping[str, Sequence[float | None]]) -> list[str]:
    """The plot keys whose runs are not all the same value.

    An axis every line crosses at one height carries no comparison — it is a
    fact about the study, which the Study-settings panel already states in
    words. Offering it is right (see :func:`_plot_inputs`); *drawing* a wall of
    them next to the two that vary is what makes a parallel plot unreadable,
    so the chart offers to drop them.
    """
    out: list[str] = []
    for key, values in columns.items():
        seen = {v for v in values if v is not None}
        if len(seen) > 1:
            out.append(key)
    return out


def _draw_parcoords(state: dict[str, Any], *, colour_key: str, hide_constant: bool = False) -> None:
    """Parallel coordinates over all varied inputs + scalar outputs (2-PS-b')."""
    children = _filtered_children(state)
    variables = _plot_variables(state)
    levels = _plot_levels(state)
    keys = list(variables)
    if is_section_header(colour_key):
        colour_key = ""

    # A choice-valued variable becomes an axis of evenly-spaced ticks carrying
    # the level names. Without this it was excluded from the picker entirely,
    # so a sweep over two materials had no axis for the thing that was swept.
    def _values(key: str) -> list[float | None]:
        cats = levels.get(key)
        if cats is None:
            return [_var_value(c, key) for c in children]
        return category_positions(cats, [_var_text(c, key) for c in children])

    columns = {k: _values(k) for k in keys}
    if hide_constant:
        varying = varying_keys(columns)
        # Never hide everything: a study where nothing varies still deserves
        # its picture rather than an empty frame and no explanation.
        if varying:
            keys = varying
    rows: list[dict[str, float]] = []
    jobs: list[int] = []
    for i, c in enumerate(children):
        vals = {k: columns[k][i] for k in keys}
        if all(v is not None for v in vals.values()):
            rows.append({k: float(v) for k, v in vals.items() if v is not None})
            jobs.append(int(c.get("job_id") or 0))
    if not rows:
        ui.label("Not enough complete rows to draw parallel coordinates.").style("color: #888;")
        return
    dims: list[dict[str, Any]] = []
    for k in keys:
        dim: dict[str, Any] = {"label": variables[k], "values": [r[k] for r in rows]}
        if (cats := levels.get(k)) is not None:
            dim |= {
                "tickvals": list(range(len(cats))),
                "ticktext": list(cats),
                "range": [-0.5, len(cats) - 0.5],
            }
        dims.append(dim)
    # Matrix selection (orange) and a Pareto-focused run (green) highlight
    # those lines (others dimmed grey), overriding the colour-by shading.
    selected = state.get("_selected_jobs")
    focus = state.get("_focus_job")
    line: dict[str, Any] = {}
    if selected or focus is not None:

        def _line_val(j: int) -> float:
            if focus is not None and j == focus:
                return 2.0
            return 1.0 if (selected and j in selected) else 0.0

        line = {
            "color": [_line_val(j) for j in jobs],
            "colorscale": [[0.0, _DIM_GREY], [0.5, _SEL_ORANGE], [1.0, _FOCUS_GREEN]],
            "cmin": 0.0,
            "cmax": 2.0,
        }
    elif colour_key and colour_key in variables:
        line = {
            "color": [r[colour_key] for r in rows],
            "colorscale": "Viridis",
            "showscale": True,
            "colorbar": {"title": variables[colour_key]},
        }
        if (cats := levels.get(colour_key)) is not None:
            line["colorscale"] = stepped_colorscale(category_colours(len(cats)))
            line["cmin"], line["cmax"] = -0.5, len(cats) - 0.5
            line["colorbar"] |= {
                "tickmode": "array",
                "tickvals": list(range(len(cats))),
                "ticktext": list(cats),
            }
    fig = go.Figure(go.Parcoords(dimensions=dims, line=line))
    fig.update_layout(height=460, margin={"l": 80, "r": 60, "t": 50, "b": 30})
    ui.plotly(themed_plotly(fig)).classes("w-full")


def _term_label(results: dict[str, Any], term: str) -> str:
    """Map an ANOVA term ("a" or "a x b") from factor names to input labels."""
    return " x ".join(_input_label(results, part) for part in term.split(" x "))


def _draw_interaction_plot(
    results: dict[str, Any], children: list[dict[str, Any]], out: str, a: str, b: str
) -> None:
    """Interaction plot: mean response vs factor A, one line per level of factor B."""
    groups: dict[tuple[float, float], list[float]] = {}
    for c in children:
        av = _var_value(c, f"in:{a}")
        bv = _var_value(c, f"in:{b}")
        y = _scalar_of(c, out)
        if av is None or bv is None or y is None:
            continue
        groups.setdefault((av, bv), []).append(y)
    if not groups:
        ui.label("No data for this factor pair.").style("color: #888;")
        return
    a_levels = sorted({k[0] for k in groups})
    b_levels = sorted({k[1] for k in groups})
    fig = go.Figure()
    for bl in b_levels:
        xs: list[float] = []
        ys: list[float] = []
        for al in a_levels:
            vals = groups.get((al, bl))
            if vals:
                xs.append(al)
                ys.append(sum(vals) / len(vals))
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="lines+markers",
                name=f"{_input_label(results, b)}={bl:g}",
                hovertemplate=(
                    f"{_input_label(results, a)} = %{{x:.4g}}"
                    f"<br>mean {_output_label(results, out)} = %{{y:.4g}}"
                    f"<extra>{_input_label(results, b)} = {bl:g}</extra>"
                ),
            )
        )
    fig.update_layout(
        xaxis_title=_input_label(results, a),
        yaxis_title=_output_label(results, out),
        height=380,
        margin={"l": 60, "r": 20, "t": 30, "b": 50},
        legend={"orientation": "h", "y": -0.2},
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


def _render_anova_card(state: dict[str, Any]) -> None:
    """Main-effects + interaction plots + ANOVA % contribution (2-PS-c-stats #5)."""
    results = state.get("results") or {}
    children = list(results.get("children") or [])
    outputs = _scalar_outputs(children)
    if not outputs or not _varied_inputs(children):
        return
    client: ApiClient = state["client"]
    study_id = int(state["study_id"])
    with section_card("stacked_bar_chart", "Main effects & ANOVA"):
        ui.label(
            "How much of the variation in the selected output each varied input "
            "explains, by splitting the total spread (sum of squares) across the "
            "factors and their two-way interactions; the residual is whatever is "
            "left unexplained. The main-effects plot shows the average output at "
            "each level of a factor, and the interaction plot shows whether two "
            "factors combine non-additively."
        ).style("color: #888; font-size: 0.85rem;")
        ui.label(
            "When to use it: best for designed studies with repeated, discrete "
            "factor levels (grid / factorial / screening). It quantifies how much "
            "each input and interaction matters, but not a single signed "
            "direction - read the main-effects plot for that."
        ).style("color: #888; font-size: 0.85rem; font-style: italic;")
        sel = ui.select(
            {n: _output_label(results, n) for n in outputs}, value=outputs[0], label="Output"
        ).props("dense")
        box = ui.column().classes("w-full")

        async def _draw() -> None:
            box.clear()
            out = str(sel.value or "")
            try:
                res = await client.get_study_anova(study_id, response=out)
            except ApiError as exc:
                with box:
                    ui.label(exc.detail or "Couldn't compute the ANOVA.").style(
                        "color: var(--matofx-error-text);"
                    )
                return
            with box:
                _draw_anova_body(results, children, out, res)

        sel.on("update:model-value", _draw)
        ui.timer(0.0, _draw, once=True)


def _draw_anova_body(
    results: dict[str, Any],
    children: list[dict[str, Any]],
    out: str,
    res: dict[str, Any],
) -> None:
    """Render the ANOVA bar + main-effects subplots + interaction controls."""
    msg = res.get("message")
    if msg:
        ui.label(str(msg)).style("color: #b58900; font-size: 0.85rem;")
    terms = list(res.get("terms") or [])
    if terms:
        disp = [_term_label(results, str(t.get("term"))) for t in terms] + ["residual"]
        vals = [float(t.get("contribution") or 0.0) for t in terms] + [
            float(res.get("residual") or 0.0)
        ]
        fig = go.Figure(
            go.Bar(
                x=vals,
                y=disp,
                orientation="h",
                marker_color="#2c7fb8",
                hovertemplate="%{y}<br>%{x:.1f}% of total variance<extra></extra>",
            )
        )
        fig.update_layout(
            title="ANOVA % contribution",
            xaxis_title="% of total variance",
            height=max(220, 60 + 28 * len(disp)),
            margin={"l": 160, "r": 20, "t": 50, "b": 40},
        )
        fig.update_yaxes(autorange="reversed")
        ui.plotly(themed_plotly(fig)).classes("w-full")

    me = res.get("main_effects") or {}
    facs = list(me)
    if facs:
        olab = _output_label(results, out)
        sub = make_subplots(
            rows=1, cols=len(facs), subplot_titles=[_input_label(results, f) for f in facs]
        )
        for i, f in enumerate(facs, start=1):
            pts = me[f]
            sub.add_trace(
                go.Scatter(
                    x=[p["level"] for p in pts],
                    y=[p["mean"] for p in pts],
                    mode="lines+markers",
                    showlegend=False,
                    hovertemplate=(
                        f"{_input_label(results, f)} = %{{x:.4g}}"
                        f"<br>mean {olab} = %{{y:.4g}}<extra></extra>"
                    ),
                ),
                row=1,
                col=i,
            )
        sub.update_layout(
            title=f"Main effects on {_output_label(results, out)}",
            height=320,
            margin={"l": 50, "r": 20, "t": 70, "b": 40},
        )
        ui.plotly(themed_plotly(sub)).classes("w-full")

    if len(facs) >= 2:
        ui.label("Interaction").style("font-weight: 600;")
        with ui.row().classes("items-center gap-2"):
            opts = {f: _input_label(results, f) for f in facs}
            a_sel = ui.select(opts, value=facs[0], label="Factor A").props("dense")
            b_sel = ui.select(opts, value=facs[1], label="Factor B").props("dense")
        ibox = ui.column().classes("w-full")

        def _idraw(_e: Any = None) -> None:
            ibox.clear()
            with ibox:
                _draw_interaction_plot(results, children, out, str(a_sel.value), str(b_sel.value))

        a_sel.on("update:model-value", _idraw)
        b_sel.on("update:model-value", _idraw)
        _idraw()


def _render_rsm_card(state: dict[str, Any]) -> None:
    """Quadratic response-surface contour + optimum (2-PS-c-stats #6)."""
    results = state.get("results") or {}
    children = list(results.get("children") or [])
    outputs = _scalar_outputs(children)
    varied = _varied_inputs(children)
    if not outputs or len(varied) < _MIN_RSM_FACTORS:
        return
    client: ApiClient = state["client"]
    study_id = int(state["study_id"])
    with section_card("blur_on", "Response surface (RSM)"):
        opts = {f: _input_label(results, f) for f in varied}
        with ui.row().classes("w-full items-center gap-2"):
            out_sel = ui.select(
                {n: _output_label(results, n) for n in outputs}, value=outputs[0], label="Output"
            ).props("dense")
            x_sel = ui.select(opts, value=varied[0], label="X").props("dense")
            y_sel = ui.select(opts, value=varied[1], label="Y").props("dense")
        box = ui.column().classes("w-full")
        cached: dict[str, Any] = {}

        async def _draw() -> None:
            box.clear()
            try:
                res = await client.get_study_rsm(
                    study_id,
                    response=str(out_sel.value or ""),
                    x=str(x_sel.value or ""),
                    y=str(y_sel.value or ""),
                )
            except ApiError as exc:
                cached.pop("res", None)
                with box:
                    ui.label(exc.detail or "Couldn't fit the response surface.").style(
                        "color: var(--matofx-error-text);"
                    )
                return
            cached["res"] = res
            with box:
                _draw_rsm_body(state, results, children, res)

        def _redraw() -> None:
            # Cheap re-render (no refetch) so a matrix selection re-highlights.
            res = cached.get("res")
            if res is None:
                return
            box.clear()
            with box:
                _draw_rsm_body(state, results, children, res)

        state.setdefault("_plot_redraws", []).append(_redraw)
        for w in (out_sel, x_sel, y_sel):
            w.on("update:model-value", _draw)
        ui.timer(0.0, _draw, once=True)


def _draw_rsm_body(
    state: dict[str, Any],
    results: dict[str, Any],
    children: list[dict[str, Any]],
    res: dict[str, Any],
) -> None:
    """Fit-quality caption + stationary point + contour with the runs overlaid."""
    out = str(res.get("response") or "")
    msg = res.get("message")
    if msg:
        ui.label(str(msg)).style("color: #b58900; font-size: 0.85rem;")
    bits: list[str] = []
    for caption, key in (("R-squared", "r_squared"), ("adj R-squared", "adj_r_squared")):
        v = res.get(key)
        if isinstance(v, int | float):
            bits.append(f"{caption} = {float(v):.3f}")
    rmse = res.get("rmse")
    if isinstance(rmse, int | float):
        bits.append(f"RMSE = {float(rmse):.3g}")
    bits.append(f"{int(res.get('n') or 0)} run(s)")
    ui.label("  -  ".join(bits)).style("color: #888; font-size: 0.85rem;")

    stat = res.get("stationary") or {}
    if stat:
        kind = str(res.get("stationary_kind") or "stationary point")
        parts = [f"{_input_label(results, k)} = {float(v):.4g}" for k, v in stat.items()]
        sval = res.get("stationary_value")
        tail = (
            f" -> {_output_label(results, out)} = {float(sval):.4g}"
            if isinstance(sval, int | float)
            else ""
        )
        ui.label(f"Predicted {kind} at: " + ", ".join(parts) + tail).style(
            "color: #d95f0e; font-size: 0.85rem;"
        )

    x_grid = res.get("x_grid") or []
    y_grid = res.get("y_grid") or []
    z = res.get("z") or []
    if not (x_grid and y_grid and z):
        return
    xk = str(res.get("x") or "")
    yk = str(res.get("y") or "")
    xlab = _input_label(results, xk)
    ylab = _input_label(results, yk)
    olab = _output_label(results, out)
    fig = go.Figure(
        go.Contour(
            x=x_grid,
            y=y_grid,
            z=z,
            colorscale="Viridis",
            colorbar={"title": olab},
            contours={"showlabels": True},
            hovertemplate=(
                f"{xlab}: %{{x:.4g}}<br>{ylab}: %{{y:.4g}}"
                f"<br>{olab} (fitted): %{{z:.4g}}<extra></extra>"
            ),
        )
    )
    varied = _varied_inputs(children)
    run_pts = [
        c
        for c in children
        if _var_value(c, f"in:{xk}") is not None and _var_value(c, f"in:{yk}") is not None
    ]
    if run_pts:
        jobs = [int(c.get("job_id") or 0) for c in run_pts]
        fig.add_trace(
            go.Scatter(
                x=[_var_value(c, f"in:{xk}") for c in run_pts],
                y=[_var_value(c, f"in:{yk}") for c in run_pts],
                mode="markers",
                marker=_run_marker(state, jobs),
                name="runs",
                customdata=[
                    _run_hover(
                        c,
                        [(_input_label(results, f), _var_value(c, f"in:{f}")) for f in varied]
                        + [(olab, _scalar_of(c, out))],
                    )
                    for c in run_pts
                ],
                hovertemplate="%{customdata}<extra></extra>",
            )
        )
    if xk in stat and yk in stat:
        sval = res.get("stationary_value")
        opt_tail = f"<br>{olab}: {float(sval):.4g}" if isinstance(sval, int | float) else ""
        fig.add_trace(
            go.Scatter(
                x=[stat[xk]],
                y=[stat[yk]],
                mode="markers",
                marker={"symbol": "star", "size": 15, "color": "#d95f0e"},
                name="optimum",
                hovertemplate=(
                    f"<b>Predicted optimum</b><br>{xlab}: {float(stat[xk]):.4g}"
                    f"<br>{ylab}: {float(stat[yk]):.4g}{opt_tail}<extra></extra>"
                ),
            )
        )
    fig.update_layout(
        xaxis_title=_input_label(results, xk),
        yaxis_title=_input_label(results, yk),
        height=480,
        margin={"l": 70, "r": 20, "t": 30, "b": 50},
        legend={"orientation": "h", "y": -0.2},
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


def _sweep(lo: float, hi: float, n: int) -> list[float]:
    """``n`` evenly-spaced points from ``lo`` to ``hi`` inclusive."""
    if n <= 1 or hi <= lo:
        return [lo]
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def _output_bounds(results: dict[str, Any], name: str) -> tuple[float | None, float | None]:
    """Declared physical (min, max) for a numeric output (ADR-0017), else (None, None).

    The surrogate is an unconstrained regressor, so its predictions can stray
    outside the physical range of the quantity (e.g. a negative failure index).
    These bounds let the profiler clamp predictions back to what's possible.
    """
    b = (results.get("output_bounds") or {}).get(name) or {}
    return b.get("min"), b.get("max")


def _clamp_value(value: float, lo: float | None, hi: float | None) -> tuple[float, bool]:
    """``(clamped, was_clamped)`` — pin ``value`` into ``[lo, hi]`` (either may be None)."""
    v = value
    clamped = False
    if lo is not None and v < lo:
        v, clamped = float(lo), True
    if hi is not None and v > hi:
        v, clamped = float(hi), True
    return v, clamped


def _profile_fig(
    surrogate: Any,
    f: str,
    label: str,
    resp_label: str,
    setpoint: dict[str, float],
    rng: tuple[float, float],
    bounds: tuple[float | None, float | None] = (None, None),
) -> go.Figure:
    """One profiler cell: predicted response vs factor ``f`` at the setpoint.

    Predictions are clamped to the output's declared physical ``bounds``
    (ADR-0017) so the curve never shows an impossible value.
    """
    lo, hi = rng
    lo_b, hi_b = bounds
    sweep = _sweep(lo, hi, 30)
    means, stds = surrogate.predict([{**setpoint, f: xv} for xv in sweep])
    if means:
        means = [_clamp_value(m, lo_b, hi_b)[0] for m in means]
    line_hover = f"{label}: %{{x:.4g}}<br>{resp_label}: %{{y:.4g}}<extra></extra>"
    fig = go.Figure()
    if means and stds:
        upper = [_clamp_value(m + s, lo_b, hi_b)[0] for m, s in zip(means, stds, strict=True)]
        lower = [_clamp_value(m - s, lo_b, hi_b)[0] for m, s in zip(means, stds, strict=True)]
        fig.add_trace(
            go.Scatter(
                x=[*sweep, *sweep[::-1]],
                y=[*upper, *lower[::-1]],
                fill="toself",
                fillcolor="rgba(44,127,184,0.15)",
                line={"width": 0},
                hoverinfo="skip",
                showlegend=False,
            )
        )
    if means:
        fig.add_trace(
            go.Scatter(
                x=sweep,
                y=means,
                mode="lines",
                line={"color": "#2c7fb8"},
                showlegend=False,
                hovertemplate=line_hover,
            )
        )
    cur = setpoint.get(f, lo)
    cur_mean, _ = surrogate.predict([setpoint])
    if cur_mean:
        fig.add_trace(
            go.Scatter(
                x=[cur],
                y=[cur_mean[0]],
                mode="markers",
                marker={"color": "#d95f0e", "size": 9},
                showlegend=False,
                hovertemplate=f"Set point<br>{label}: %{{x:.4g}}<br>{resp_label}: %{{y:.4g}}"
                "<extra></extra>",
            )
        )
    fig.add_vline(x=cur, line={"color": "#d95f0e", "dash": "dot", "width": 1})
    fig.update_layout(height=160, margin={"l": 44, "r": 8, "t": 6, "b": 26}, showlegend=False)
    fig.update_xaxes(title_text=label, title_font={"size": 10})
    return fig


def _render_profiler_card(state: dict[str, Any]) -> None:
    """JMP-style multi-response Prediction Profiler over surrogate models (2-PS-c)."""
    results = state.get("results") or {}
    children = list(results.get("children") or [])
    outputs = _scalar_outputs(children)
    varied = _varied_inputs(children)
    if not outputs or not varied:
        return
    ranges: dict[str, tuple[float, float]] = {}
    for f in varied:
        vals = [v for c in children if (v := _var_value(c, f"in:{f}")) is not None]
        ranges[f] = (min(vals), max(vals)) if vals else (0.0, 1.0)
    setpoint: dict[str, float] = state.setdefault("_prof_setpoint", {})
    for f in varied:
        setpoint.setdefault(f, (ranges[f][0] + ranges[f][1]) / 2)
    surrogates: dict[str, Surrogate] = {}
    cells: dict[tuple[str, str], Any] = {}
    slider_refs: dict[str, Any] = {}
    chosen: list[str] = []

    with section_card("tune", "Prediction profiler (surrogate)"):
        ui.label(
            "Move a factor's slider — every response profile and the predicted value "
            "update for that operating point (other factors held fixed)."
        ).style("color: #888; font-size: 0.85rem;")
        with ui.row().classes("w-full items-center gap-2"):
            model_sel = ui.select(
                {"gp": "Gaussian process", "poly": "Polynomial"}, value="gp", label="Model"
            ).props("dense")
            out_sel = (
                ui.select(
                    {o: _output_label(results, o) for o in outputs},
                    value=outputs[: min(3, len(outputs))],
                    multiple=True,
                    label="Responses",
                )
                .props("dense")
                .classes("flex-grow")
            )
        with ui.row().classes("w-full items-center gap-2"):
            opt_out = ui.select(
                {o: _output_label(results, o) for o in outputs}, value=outputs[0], label="Optimise"
            ).props("dense")
            opt_dir = ui.select(
                {"max": "maximise", "min": "minimise"}, value="max", label="Goal"
            ).props("dense")
            opt_btn = ui.button("Set to optimum", icon="adjust").props("flat dense")
        status = ui.label("").style("color: #888; font-size: 0.85rem;")
        readout = ui.row().classes("w-full gap-5 flex-wrap")
        slider_box = ui.row().classes("w-full gap-4 flex-wrap")
        grid_box = ui.column().classes("w-full")

        def _update_readout() -> None:
            readout.clear()
            with readout:
                for o in chosen:
                    s = surrogates.get(o)
                    if s is None or not s.fitted:
                        continue
                    means, stds = s.predict([setpoint])
                    if not means:
                        continue
                    lo_b, hi_b = _output_bounds(results, o)
                    val, clamped = _clamp_value(means[0], lo_b, hi_b)
                    txt = _fmt_stat(val) + (f" ± {_fmt_stat(stds[0])}" if stds else "")
                    with ui.column().classes("gap-0"):
                        ui.label(_output_label(results, o)).style(
                            "color: #888; font-size: 0.72rem; text-transform: uppercase;"
                        )
                        ui.label(txt).style("font-weight: 600; color: #d95f0e;")
                        if clamped:
                            ui.label("clamped to physical range").style(
                                "color: #b58900; font-size: 0.68rem;"
                            )

        def _redraw() -> None:
            for o in chosen:
                s = surrogates.get(o)
                if s is None or not s.fitted:
                    continue
                for f in varied:
                    cell = cells.get((o, f))
                    if cell is not None:
                        cell.update_figure(
                            themed_plotly(
                                _profile_fig(
                                    s,
                                    f,
                                    _input_label(results, f),
                                    _output_label(results, o),
                                    setpoint,
                                    ranges[f],
                                    _output_bounds(results, o),
                                )
                            )
                        )
            _update_readout()

        def _rebuild_grid() -> None:
            # One CSS grid for the whole matrix (rather than a flex row per
            # response): a fixed label column + one equal 1fr column per varied
            # factor. This guarantees the three width requirements at once —
            # (a) the grid is w-full so the factor columns consume all the space
            # left of the label column; (b) every factor column is an identical
            # 1fr; (c) the label column is a *fixed* width, so long labels /
            # Q² / "not fitted" text never shove a row's plots sideways, and
            # the columns line up top-to-bottom. ``minmax(0, 1fr)`` lets the
            # cells shrink (with plot min-width:0) instead of overflowing when
            # there are many factors.
            # TODO(next release): add a factor-label header row across the top
            # of the grid so the columns are self-describing (JMP-style),
            # instead of relying on the per-cell x-axis title alone.
            grid_box.clear()
            cells.clear()
            ncol = len(varied)
            with grid_box:
                grid = (
                    ui.element("div")
                    .classes("w-full")
                    .style(
                        "display: grid; "
                        f"grid-template-columns: 160px repeat({ncol}, minmax(0, 1fr)); "
                        "align-items: center; column-gap: 6px; row-gap: 10px;"
                    )
                )
                with grid:
                    for o in chosen:
                        s = surrogates.get(o)
                        head = _output_label(results, o)
                        if s is not None and s.fitted and s.quality.q2 is not None:
                            head += f"  (Q²={s.quality.q2:.2f})"
                        ui.label(head).style(
                            "font-weight: 600; font-size: 0.8rem; overflow-wrap: anywhere;"
                        )
                        if s is None or not s.fitted:
                            # Span the factor columns so the message doesn't
                            # disturb the grid's column geometry.
                            ui.label(str(s.quality.message) if s else "not fitted").style(
                                "color: #b58900; font-size: 0.82rem; grid-column: 2 / -1;"
                            )
                            continue
                        for f in varied:
                            fig = _profile_fig(
                                s,
                                f,
                                _input_label(results, f),
                                _output_label(results, o),
                                setpoint,
                                ranges[f],
                                _output_bounds(results, o),
                            )
                            cells[(o, f)] = (
                                ui.plotly(themed_plotly(fig))
                                .classes("w-full")
                                .style("min-width: 0;")
                            )
                _render_quality_expansion(results, surrogates, chosen)
            _update_readout()

        async def _refit() -> None:
            nonlocal chosen
            chosen = [str(v) for v in (out_sel.value or [])]
            kind: SurrogateKind = "poly" if str(model_sel.value or "gp") == "poly" else "gp"
            if not chosen:
                surrogates.clear()
                grid_box.clear()
                cells.clear()
                readout.clear()
                status.text = "Pick at least one response."
                return
            status.text = "Fitting surrogate(s)…"
            cols = {f: [_var_value(c, f"in:{f}") for c in children] for f in varied}
            surrogates.clear()
            for o in chosen:
                resp = [_scalar_of(c, o) for c in children]
                fitted = await run.io_bound(fit_surrogate, cols, resp, kind=kind)
                if fitted is None:
                    return  # fit cancelled (client navigated away / app stopping)
                surrogates[o] = fitted
            status.text = f"{kind.upper()} surrogate(s) fitted over {len(varied)} varied factor(s)."
            _rebuild_grid()

        def _build_sliders() -> None:
            slider_box.clear()
            slider_refs.clear()
            with slider_box:
                for f in varied:
                    lo, hi = ranges[f]
                    step = (hi - lo) / 100 if hi > lo else 1.0
                    with ui.column().classes("gap-0").style("min-width: 200px;"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(_input_label(results, f)).style(
                                "font-size: 0.78rem; color: #888;"
                            )
                            val_lbl = ui.label(f"{setpoint[f]:.4g}").style(
                                "font-size: 0.78rem; font-weight: 600;"
                            )
                        sl = ui.slider(min=lo, max=hi, value=setpoint[f], step=step)
                        slider_refs[f] = sl

                        def _on_slide(
                            _e: Any = None, _f: str = f, _sl: Any = sl, _lbl: Any = val_lbl
                        ) -> None:
                            setpoint[_f] = float(_sl.value)
                            _lbl.text = f"{float(_sl.value):.4g}"
                            _redraw()

                        sl.on("update:model-value", _on_slide, throttle=0.2)

        async def _optimise() -> None:
            o = str(opt_out.value or "")
            if not o:
                return
            kind: SurrogateKind = "poly" if str(model_sel.value or "gp") == "poly" else "gp"
            s = surrogates.get(o)
            if s is None or not s.fitted or s.kind != kind:
                status.text = f"Fitting {_output_label(results, o)}…"
                cols = {f: [_var_value(c, f"in:{f}") for c in children] for f in varied}
                s = await run.io_bound(
                    fit_surrogate, cols, [_scalar_of(c, o) for c in children], kind=kind
                )
                if s is None:
                    return  # fit cancelled (client navigated away / app stopping)
                surrogates[o] = s
            if not s.fitted:
                status.text = str(s.quality.message) or "Couldn't fit that response."
                return
            status.text = "Optimising…"
            point = await run.io_bound(
                optimize_surrogate, s, maximize=(str(opt_dir.value) == "max")
            )
            if point:
                setpoint.update({k: float(v) for k, v in point.items()})
                _build_sliders()
                _redraw()
                goal = "maximum" if str(opt_dir.value) == "max" else "minimum"
                status.text = (
                    f"Profiler set to the predicted {goal} of {_output_label(results, o)}."
                )

        opt_btn.on_click(_optimise)
        out_sel.on("update:model-value", _refit)
        model_sel.on("update:model-value", _refit)
        _build_sliders()
        # Auto-fit the default responses on open (the fit is offloaded via
        # run.io_bound). A brief loop-stall during the fit no longer forces a
        # reload — the app raises NiceGUI's reconnect_timeout (see ui/app.py).
        ui.timer(0.0, _refit, once=True)


def _render_quality_expansion(
    results: dict[str, Any], surrogates: dict[str, Any], chosen: list[str]
) -> None:
    """Cross-validated predicted-vs-actual scatter per fitted output (trust view)."""
    fitted = [o for o in chosen if (s := surrogates.get(o)) and s.fitted and s.quality.predicted]
    if not fitted:
        return
    with ui.expansion("Model quality — predicted vs actual (cross-validated)").classes("w-full"):
        with ui.row().classes("w-full gap-2 flex-wrap"):
            for o in fitted:
                s = surrogates[o]
                act = s.quality.actual
                pred = s.quality.predicted
                lo = min(*act, *pred)
                hi = max(*act, *pred)
                olab = _output_label(results, o)
                fig = go.Figure()
                fig.add_trace(
                    go.Scatter(
                        x=act,
                        y=pred,
                        mode="markers",
                        marker={"size": 5},
                        hovertemplate=(
                            f"Actual {olab}: %{{x:.4g}}<br>Predicted: %{{y:.4g}}<extra></extra>"
                        ),
                    )
                )
                fig.add_trace(
                    go.Scatter(
                        x=[lo, hi],
                        y=[lo, hi],
                        mode="lines",
                        line={"dash": "dot", "color": "#888"},
                        hoverinfo="skip",
                    )
                )
                fig.update_layout(
                    title=f"{_output_label(results, o)} (Q²={s.quality.q2:.2f})"
                    if s.quality.q2 is not None
                    else _output_label(results, o),
                    xaxis_title="actual",
                    yaxis_title="predicted",
                    height=300,
                    margin={"l": 50, "r": 10, "t": 40, "b": 40},
                    showlegend=False,
                )
                ui.plotly(themed_plotly(fig)).classes("flex-grow").style("min-width: 320px;")


def _render_surrogate_contour_card(state: dict[str, Any]) -> None:
    """Surrogate contour over two factors that re-slices with held-factor sliders (2-PS-c)."""
    results = state.get("results") or {}
    children = list(results.get("children") or [])
    outputs = _scalar_outputs(children)
    varied = _varied_inputs(children)
    if not outputs or len(varied) < _MIN_RSM_FACTORS:
        return
    ranges: dict[str, tuple[float, float]] = {}
    for f in varied:
        vals = [v for c in children if (v := _var_value(c, f"in:{f}")) is not None]
        ranges[f] = (min(vals), max(vals)) if vals else (0.0, 1.0)
    held: dict[str, float] = state.setdefault("_contour_held", {})
    for f in varied:
        held.setdefault(f, (ranges[f][0] + ranges[f][1]) / 2)
    cache: dict[tuple[str, str], Surrogate] = {}

    with section_card("gradient", "Surrogate contour profiler"):
        ui.label(
            "Two-factor contour of the surrogate; the other factors are held at the "
            "sliders below and the contour re-slices as you move them."
        ).style("color: #888; font-size: 0.85rem;")
        opts = {f: _input_label(results, f) for f in varied}
        with ui.row().classes("w-full items-center gap-2"):
            out_sel = ui.select(
                {o: _output_label(results, o) for o in outputs}, value=outputs[0], label="Output"
            ).props("dense")
            model_sel = ui.select(
                {"gp": "Gaussian process", "poly": "Polynomial"}, value="gp", label="Model"
            ).props("dense")
            x_sel = ui.select(opts, value=varied[0], label="X").props("dense")
            y_sel = ui.select(opts, value=varied[1], label="Y").props("dense")
        status = ui.label("").style("color: #888; font-size: 0.85rem;")
        held_box = ui.row().classes("w-full gap-4 flex-wrap")
        plot_box = ui.column().classes("w-full")

        def _draw(s: Surrogate | None) -> None:
            plot_box.clear()
            xf = str(x_sel.value or varied[0])
            yf = str(y_sel.value or varied[1])
            with plot_box:
                if xf == yf:
                    ui.label("Pick two different factors.").style("color: #888;")
                    return
                if s is None or not s.fitted:
                    ui.label(str(s.quality.message) if s else "Surrogate not fitted.").style(
                        "color: #b58900;"
                    )
                    return
                xg = _sweep(*ranges[xf], 25)
                yg = _sweep(*ranges[yf], 25)
                means, _ = s.predict([{**held, xf: xv, yf: yv} for yv in yg for xv in xg])
                z = [means[r * 25 : (r + 1) * 25] for r in range(25)]
                xlab = _input_label(results, xf)
                ylab = _input_label(results, yf)
                olab = _output_label(results, str(out_sel.value or ""))
                fig = go.Figure(
                    go.Contour(
                        x=xg,
                        y=yg,
                        z=z,
                        colorscale="Viridis",
                        colorbar={"title": olab},
                        contours={"showlabels": True},
                        hovertemplate=(
                            f"{xlab}: %{{x:.4g}}<br>{ylab}: %{{y:.4g}}"
                            f"<br>{olab} (predicted): %{{z:.4g}}<extra></extra>"
                        ),
                    )
                )
                run_pts = [
                    c
                    for c in children
                    if _var_value(c, f"in:{xf}") is not None
                    and _var_value(c, f"in:{yf}") is not None
                ]
                if run_pts:
                    out_now = str(out_sel.value or "")
                    jobs = [int(c.get("job_id") or 0) for c in run_pts]
                    fig.add_trace(
                        go.Scatter(
                            x=[_var_value(c, f"in:{xf}") for c in run_pts],
                            y=[_var_value(c, f"in:{yf}") for c in run_pts],
                            mode="markers",
                            marker=_run_marker(state, jobs),
                            name="runs",
                            customdata=[
                                _run_hover(
                                    c,
                                    [
                                        (_input_label(results, f), _var_value(c, f"in:{f}"))
                                        for f in varied
                                    ]
                                    + [(olab, _scalar_of(c, out_now))],
                                )
                                for c in run_pts
                            ],
                            hovertemplate="%{customdata}<extra></extra>",
                        )
                    )
                fig.update_layout(
                    xaxis_title=xlab,
                    yaxis_title=ylab,
                    height=460,
                    margin={"l": 70, "r": 20, "t": 30, "b": 50},
                    legend={"orientation": "h", "y": -0.2},
                )
                ui.plotly(themed_plotly(fig)).classes("w-full")

        def _current() -> Surrogate | None:
            kind = "poly" if str(model_sel.value or "gp") == "poly" else "gp"
            return cache.get((str(out_sel.value or ""), kind))

        def _build_held() -> None:
            held_box.clear()
            axes = {str(x_sel.value or ""), str(y_sel.value or "")}
            with held_box:
                for f in varied:
                    if f in axes:
                        continue
                    lo, hi = ranges[f]
                    step = (hi - lo) / 100 if hi > lo else 1.0
                    with ui.column().classes("gap-0").style("min-width: 200px;"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(_input_label(results, f)).style(
                                "font-size: 0.78rem; color: #888;"
                            )
                            vlbl = ui.label(f"{held[f]:.4g}").style(
                                "font-size: 0.78rem; font-weight: 600;"
                            )
                        sl = ui.slider(min=lo, max=hi, value=held[f], step=step)

                        def _on_held(
                            _e: Any = None, _f: str = f, _sl: Any = sl, _lbl: Any = vlbl
                        ) -> None:
                            held[_f] = float(_sl.value)
                            _lbl.text = f"{float(_sl.value):.4g}"
                            _draw(_current())

                        sl.on("update:model-value", _on_held, throttle=0.2)

        def _on_axis(_e: Any = None) -> None:
            _build_held()
            _draw(_current())

        async def _refit() -> None:
            o = str(out_sel.value or "")
            kind: SurrogateKind = "poly" if str(model_sel.value or "gp") == "poly" else "gp"
            if not o:
                return
            key = (o, kind)
            if key not in cache:
                status.text = "Fitting surrogate…"
                cols = {f: [_var_value(c, f"in:{f}") for c in children] for f in varied}
                fitted = await run.io_bound(
                    fit_surrogate, cols, [_scalar_of(c, o) for c in children], kind=kind
                )
                if fitted is None:
                    return  # fit cancelled — don't poison the cache
                cache[key] = fitted
            s = cache[key]
            status.text = (
                f"surrogate Q² = {s.quality.q2:.3f}"
                if s.fitted and s.quality.q2 is not None
                else (str(s.quality.message) or "")
            )
            _build_held()
            _draw(s)

        # Re-slice (no refit) so a matrix selection re-highlights the runs;
        # no-op until a surrogate has been fitted (avoids a "not fitted" flash).
        def _redraw_highlight() -> None:
            s = _current()
            if s is not None:
                _draw(s)

        state.setdefault("_plot_redraws", []).append(_redraw_highlight)
        x_sel.on("update:model-value", _on_axis)
        y_sel.on("update:model-value", _on_axis)
        out_sel.on("update:model-value", _refit)
        model_sel.on("update:model-value", _refit)
        ui.timer(0.0, _refit, once=True)


def _render_sobol_card(state: dict[str, Any]) -> None:
    """Variance-based (Sobol) global sensitivity from the surrogate (2-PS-c, item 7)."""
    results = state.get("results") or {}
    children = list(results.get("children") or [])
    outputs = _scalar_outputs(children)
    varied = _varied_inputs(children)
    if not outputs or not varied:
        return
    with section_card("hub", "Global sensitivity (Sobol)"):
        ui.label(
            "Variance-based importance from the surrogate: first-order (this factor "
            "alone) vs total-effect (including interactions). Captures the "
            "nonlinearity and interactions the linear regression sensitivity misses."
        ).style("color: #888; font-size: 0.85rem;")
        ui.label(
            "When to use it: the most thorough importance measure when the response "
            "is non-linear - the total-effect index counts everything a factor does, "
            "including through interactions, which the regression Sensitivity cannot "
            "see. It needs a fitted surrogate, so it appears only once one fits; a "
            "large gap between total-effect and first-order signals interaction."
        ).style("color: #888; font-size: 0.85rem; font-style: italic;")
        with ui.row().classes("w-full items-center gap-2"):
            out_sel = ui.select(
                {o: _output_label(results, o) for o in outputs}, value=outputs[0], label="Output"
            ).props("dense")
            model_sel = ui.select(
                {"gp": "Gaussian process", "poly": "Polynomial"}, value="gp", label="Model"
            ).props("dense")
        box = ui.column().classes("w-full")

        async def _draw() -> None:
            box.clear()
            o = str(out_sel.value or "")
            if not o:
                return
            kind: SurrogateKind = "poly" if str(model_sel.value or "gp") == "poly" else "gp"
            cols = {f: [_var_value(c, f"in:{f}") for c in children] for f in varied}
            s = await run.io_bound(
                fit_surrogate, cols, [_scalar_of(c, o) for c in children], kind=kind
            )
            if s is None:
                return  # fit cancelled (client navigated away / app stopping)
            idx = await run.io_bound(sobol_indices, s) if s.fitted else None
            with box:
                if not s.fitted:
                    ui.label(str(s.quality.message) or "Couldn't fit a surrogate.").style(
                        "color: #b58900;"
                    )
                    return
                if not idx:
                    ui.label("Sobol indices unavailable for this surrogate.").style("color: #888;")
                    return
                if s.quality.q2 is not None:
                    ui.label(
                        f"surrogate Q² = {s.quality.q2:.3f} "
                        "(indices are only as trustworthy as the surrogate)"
                    ).style("color: #888; font-size: 0.82rem;")
                ranked = sorted(idx, key=lambda i: i.st, reverse=True)
                labels = [_input_label(results, i.factor) for i in ranked]
                fig = go.Figure()
                fig.add_trace(
                    go.Bar(
                        name="First-order (S1)",
                        x=[i.s1 for i in ranked],
                        y=labels,
                        orientation="h",
                        hovertemplate="%{y}<br>First-order S1 = %{x:.3f}<extra></extra>",
                    )
                )
                fig.add_trace(
                    go.Bar(
                        name="Total-effect (ST)",
                        x=[i.st for i in ranked],
                        y=labels,
                        orientation="h",
                        hovertemplate="%{y}<br>Total-effect ST = %{x:.3f}<extra></extra>",
                    )
                )
                fig.update_layout(
                    barmode="group",
                    title="Sobol sensitivity indices",
                    xaxis_title="Variance share (0 to 1)",
                    height=max(240, 70 + 30 * len(labels)),
                    margin={"l": 170, "r": 20, "t": 50, "b": 50},
                    legend={"orientation": "h", "y": -0.2},
                )
                fig.update_yaxes(autorange="reversed")
                ui.plotly(themed_plotly(fig)).classes("w-full")

        for w in (out_sel, model_sel):
            w.on("update:model-value", _draw)
        ui.timer(0.0, _draw, once=True)


def _column_is_numeric(children: list[dict[str, Any]], bucket: str, key: str) -> bool:
    """True when every present value of ``key`` (in ``inputs``/``scalars``) is numeric."""
    present = [(c.get(bucket) or {}).get(key) for c in children]
    present = [v for v in present if v is not None]
    nums = [v for v in present if isinstance(v, int | float) and not isinstance(v, bool)]
    return bool(present) and len(nums) == len(present)


def _num_value(v: Any) -> float | None:
    """Coerce to float, or ``None`` for non-numeric / bool."""
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


# Client-side AG Grid value formatter (NiceGUI runs a ``:``-prefixed coldef
# value as JS): blanks for empty cells, thousands-separated integers, and
# floats trimmed to ~5 significant figures so a numeric column reads cleanly
# rather than as a ragged jumble of full-precision doubles. Non-numbers pass
# through unchanged.
_NUMERIC_VALUE_FORMATTER = (
    "params => { const v = params.value;"
    " if (v === null || v === undefined || v === '') return '';"
    " if (typeof v !== 'number') return v;"
    " if (Number.isInteger(v)) return v.toLocaleString();"
    " return parseFloat(v.toPrecision(5)).toLocaleString(undefined,"
    " {maximumFractionDigits: 6}); }"
)


def _col_passes(value: Any, cond: Any) -> bool:
    """Whether one cell satisfies one AG Grid filter condition (number/text)."""
    if not isinstance(cond, dict):
        return True
    if "conditions" in cond:  # AG Grid v31 combined model
        sub = [_col_passes(value, c) for c in cond.get("conditions") or []]
        return all(sub) if cond.get("operator", "AND") == "AND" else any(sub)
    if "condition1" in cond:  # legacy combined model
        c1 = _col_passes(value, cond.get("condition1"))
        c2 = _col_passes(value, cond.get("condition2") or {})
        return (c1 and c2) if cond.get("operator", "AND") == "AND" else (c1 or c2)
    op = str(cond.get("type") or "")
    if cond.get("filterType") == "number":
        x = _num_value(value)
        a = _num_value(cond.get("filter"))
        b = _num_value(cond.get("filterTo"))
        if op == "blank":
            return value is None
        if op == "notBlank":
            return value is not None
        if x is None or a is None:
            return op == "notEqual"
        return {
            "equals": x == a,
            "notEqual": x != a,
            "greaterThan": x > a,
            "greaterThanOrEqual": x >= a,
            "lessThan": x < a,
            "lessThanOrEqual": x <= a,
            "inRange": b is not None and a <= x <= b,
        }.get(op, True)
    s = "" if value is None else str(value).lower()
    f = str(cond.get("filter") or "").lower()
    if op == "blank":
        return s == ""
    if op == "notBlank":
        return s != ""
    return {
        "equals": s == f,
        "notEqual": s != f,
        "contains": f in s,
        "notContains": f not in s,
        "startsWith": s.startswith(f),
        "endsWith": s.endswith(f),
    }.get(op, True)


def _passes_filters(row: dict[str, Any], model: dict[str, Any]) -> bool:
    """Whether a row satisfies every active AG Grid column filter."""
    return all(_col_passes(row.get(field), cond) for field, cond in model.items())


def _filtered_children(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Children restricted to the matrix's current column filters (2-PS-b')."""
    children = list((state.get("results") or {}).get("children") or [])
    allowed = state.get("_row_filter_jobs")
    if allowed is None:
        return children
    return [c for c in children if int(c.get("job_id") or 0) in allowed]


def _grouped_field_sections(
    results: dict[str, Any], input_cols: list[str], scalar_cols: list[str]
) -> list[tuple[str, list[tuple[str, str]]]]:
    """Ordered ``(group label, [(field, header)])`` for the Columns popover.

    Groups columns the way the job form groups fields: a leading "Run"
    section, then each input group, then each output group, in descriptor
    order. Section headers are the group *labels*; ungrouped fields fall
    into "Inputs" / "Outputs".
    """
    input_order = list((results.get("input_labels") or {}).keys())
    input_grp = results.get("input_groups") or {}
    output_grp = results.get("output_groups") or {}
    input_grp_labels = results.get("input_group_labels") or {}
    output_grp_labels = results.get("output_group_labels") or {}
    scalar_order = [
        str(s.get("name") or "")
        for s in (results.get("response_sources") or {}).get("scalars") or []
    ]

    def _grouped(
        order: list[str],
        present: list[str],
        grp: dict[str, Any],
        grp_labels: dict[str, Any],
        prefix: str,
        default: str,
    ) -> list[tuple[str, list[tuple[str, str]]]]:
        present_set = set(present)
        names = [n for n in order if n in present_set]
        names += [n for n in present if n not in order]
        out: list[tuple[str, list[tuple[str, str]]]] = []
        index: dict[str, int] = {}
        label = _input_label if prefix == "in_" else _output_label
        for n in names:
            gname = str(grp.get(n) or "")
            key = gname or default  # group identity (so same-label groups stay distinct)
            display = (str(grp_labels.get(gname) or "") if gname else "") or default
            if key not in index:
                index[key] = len(out)
                out.append((display, []))
            out[index[key]][1].append((f"{prefix}{n}", label(results, n)))
        return out

    sections: list[tuple[str, list[tuple[str, str]]]] = [
        ("Run", [("job_id", "Job"), ("state", "State")])
    ]
    sections += _grouped(input_order, input_cols, input_grp, input_grp_labels, "in_", "Inputs")
    sections += _grouped(
        scalar_order, scalar_cols, output_grp, output_grp_labels, "out_", "Outputs"
    )
    return sections


def _render_results_matrix(state: dict[str, Any]) -> None:
    """Full-grid matrix: label headers, sort, per-column filter, grouped show/hide (2-PS-b').

    AG Grid (Community): sorting + floating per-column filters are built in.
    Show/hide is a popover of checkboxes grouped like the job form, driving
    the column API (no recreate, so filters survive). The column filters
    drive the plot card — the plots reflect whatever the matrix is showing.
    Headers are descriptor labels, never raw names; numeric cells stay
    numbers so sorting/filtering are numeric.
    """
    results = state.get("results") or {}
    children = list(results.get("children") or [])
    if not children:
        return
    # Study-level data-health summary (ADR-0017): invalid runs are excluded
    # from surrogates / sensitivity / optimisation, so surface them up front.
    n_invalid = sum(1 for c in children if str(c.get("validity")) == "invalid")
    if n_invalid:
        ui.label(
            f"⚠ {n_invalid} of {len(children)} runs have non-physical outputs "
            "(NaN / out-of-bounds) and are excluded from surrogates, sensitivity "
            "and optimisation. See the Health column."
        ).style("color: #b58900; font-size: 0.85rem;")
    input_cols = sorted({k for c in children for k in (c.get("inputs") or {})})
    scalar_cols = sorted({k for c in children for k in (c.get("scalars") or {})})

    # field -> (header label, numeric?) for every data column.
    fields: list[tuple[str, str, bool]] = [("job_id", "Job", True), ("state", "State", False)]
    if n_invalid:
        fields.append(("validity", "Health", False))
    for ic in input_cols:
        fields.append(
            (f"in_{ic}", _input_label(results, ic), _column_is_numeric(children, "inputs", ic))
        )
    for sc in scalar_cols:
        fields.append(
            (f"out_{sc}", _output_label(results, sc), _column_is_numeric(children, "scalars", sc))
        )

    rows: list[dict[str, Any]] = []
    for c in children:
        inputs = c.get("inputs") or {}
        scalars = c.get("scalars") or {}
        cstate = str(c.get("state") or "")
        row: dict[str, Any] = {
            "job_id": c.get("job_id"),
            "state": _STATE_LABEL.get(cstate, cstate.title() or "-"),
        }
        if n_invalid:
            row["validity"] = "Invalid" if str(c.get("validity")) == "invalid" else "Valid"
        for ic in input_cols:
            row[f"in_{ic}"] = inputs.get(ic)
        for sc in scalar_cols:
            row[f"out_{sc}"] = scalars.get(sc)
        rows.append(row)

    hidden: set[str] = state.setdefault("_hidden_cols", set())
    state["_row_filter_jobs"] = None  # fresh grid -> no filter applied yet
    state["_selected_jobs"] = None  # fresh grid -> nothing matrix-selected (orange)
    state["_focus_job"] = None  # fresh grid -> no Pareto-focused run (green)
    # Registry every plot card appends a lightweight redraw to, so a matrix
    # filter / selection (or a Pareto-row click) refreshes ALL plots, not
    # just the unified scatter. Reset here (matrix renders before the cards).
    state["_plot_redraws"] = []

    # Leading checkbox column for explicit multi-select (+ header select-all,
    # limited to the filtered rows so it composes with the column filters).
    coldefs: list[dict[str, Any]] = [
        {
            "headerName": "",
            "checkboxSelection": True,
            "headerCheckboxSelection": True,
            "headerCheckboxSelectionFilteredOnly": True,
            "width": 44,
            "pinned": "left",
            "sortable": False,
            "filter": False,
            "resizable": False,
        }
    ]
    for field, header, numeric in fields:
        cd: dict[str, Any] = {
            "headerName": header,
            "field": field,
            "filter": "agNumberColumnFilter" if numeric else "agTextColumnFilter",
        }
        # Tidily format every input/output data cell: the formatter is
        # null/non-number safe (it passes text straight through), so we attach
        # it per *cell* rather than gating on the whole column being numeric —
        # otherwise a single blank/NaN cell dropped the formatter and the rest
        # of the column leaked full-precision doubles (3.141592653589793).
        if field.startswith(("in_", "out_")):
            cd[":valueFormatter"] = _NUMERIC_VALUE_FORMATTER
        # Right-align + numeric sort only when the whole column is numeric.
        if numeric and field != "job_id":
            cd["type"] = "numericColumn"
        if field == "job_id":
            cd["maxWidth"] = 110
        if field in hidden:
            cd["hide"] = True
        coldefs.append(cd)

    controls_row = ui.row().classes("w-full items-center justify-end gap-2")
    grid = (
        ui.aggrid(
            {
                "columnDefs": coldefs,
                "rowData": rows,
                "rowSelection": "multiple",
                "suppressRowClickSelection": True,
                "defaultColDef": {
                    "sortable": True,
                    "filter": True,
                    "floatingFilter": True,
                    "resizable": True,
                },
            }
        )
        .classes("w-full")
        .style("height: 55vh;")
    )

    async def _trigger_plot_redraw() -> None:
        # The unified plot card keeps a single slot (overwritten as the chart
        # type changes); every other card appends to ``_plot_redraws``.
        callbacks = []
        single = state.get("_plot_redraw")
        if single is not None:
            callbacks.append(single)
        callbacks.extend(state.get("_plot_redraws") or [])
        for cb in callbacks:
            try:
                res = cb()
                if res is not None and hasattr(res, "__await__"):
                    await res
            except Exception:  # noqa: S112 - one card's redraw must not break the others
                continue

    state["_trigger_redraw"] = _trigger_plot_redraw

    async def _on_filter() -> None:
        model = await grid.run_grid_method("getFilterModel")
        if not model:
            state["_row_filter_jobs"] = None
        else:
            state["_row_filter_jobs"] = {
                int(r["job_id"])
                for r in rows
                if r.get("job_id") is not None and _passes_filters(r, model)
            }
        await _trigger_plot_redraw()
        summary_redraw = state.get("_summary_redraw")
        if callable(summary_redraw):
            summary_redraw()

    async def _on_selection() -> None:
        sel = await grid.run_grid_method("getSelectedRows")
        jobs = {int(r["job_id"]) for r in (sel or []) if r.get("job_id") is not None}
        state["_selected_jobs"] = jobs or None
        await _trigger_plot_redraw()

    grid.on("filterChanged", _on_filter)
    grid.on("selectionChanged", _on_selection)

    sections = _grouped_field_sections(results, input_cols, scalar_cols)
    with controls_row:

        async def _clear_selection() -> None:
            # Clear both highlights: the matrix selection (orange) and the
            # Pareto-row focus (green). deselectAll fires selectionChanged ->
            # _on_selection redraws; trigger a redraw too so a focus-only
            # highlight (no grid rows selected) is cleared as well.
            state["_focus_job"] = None
            await grid.run_grid_method("deselectAll")
            await _trigger_plot_redraw()

        ui.button("Clear selection", icon="filter_alt_off", on_click=_clear_selection).props(
            "flat dense"
        )
    with controls_row, ui.dropdown_button("Columns", icon="view_column").props("flat dense"):
        with ui.column().classes("gap-1 p-2").style("max-height: 60vh; overflow-y: auto;"):
            for group_label, items in sections:
                ui.label(group_label).style(
                    "font-weight: 600; font-size: 0.78rem; "
                    "text-transform: uppercase; color: #888; margin-top: 6px;"
                )
                with ui.column().classes("gap-0 pl-3"):
                    for field, header in items:

                        async def _toggle(e: Any, _f: str = field) -> None:
                            if e.value:
                                hidden.discard(_f)
                            else:
                                hidden.add(_f)
                            await grid.run_grid_method("setColumnsVisible", [_f], bool(e.value))

                        ui.checkbox(header, value=field not in hidden, on_change=_toggle).props(
                            "dense"
                        )

    children_with_files = [c for c in children if c.get("retained_files")]
    if not children_with_files:
        return
    with ui.expansion(f"Per-child files ({len(children_with_files)} child(ren))").classes("w-full"):
        for c in children_with_files:
            _render_child_files_row(state["client"], c)


def _render_child_files_row(client: ApiClient, child: dict[str, Any]) -> None:
    """One row in the drill-down expansion: child + download buttons per retained file."""
    jid = int(child.get("job_id") or 0)
    files = list(child.get("retained_files") or [])
    with ui.row().classes("w-full items-center gap-2 p-1"):
        ui.label(f"#{jid}").style("flex: 0 0 70px; color: #888;")
        for f in files:
            filename = str(f.get("filename") or "")
            kind = str(f.get("kind") or "file")
            leaf = filename.rsplit("/", 1)[-1] or filename or "file"
            ui.button(
                f"{leaf} ({kind})",
                icon="download",
                on_click=lambda _e=None, _j=jid, _fn=filename, _l=leaf: _download_child_file(
                    client, _j, _fn, _l
                ),
            ).props("flat dense")


async def _download_child_file(
    client: ApiClient, job_id: int, filename: str, leaf_name: str
) -> None:
    """Download one retained file from a study child via the job-output endpoint."""
    try:
        data = await client.download_job_output(job_id, filename)
    except ApiError as exc:
        ui.notify(f"Couldn't download: {exc.detail}", type="negative")
        return
    ui.download.content(data, leaf_name)
