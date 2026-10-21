"""Optimisation Studies UI (ADR-0009 §15).

Two surfaces, both behind the ``features.optimisation_framework`` flag
(the nav entry + these pages are absent when it's off, matching the API
which 404s every route — a disabled-feature visit redirects home):

- ``GET /optimisation`` — the workspace: your optimisation studies across
  visible projects + the create form (name → project → tool, which mints
  a ``draft`` and drops you into its detail page to configure it).
- ``GET /optimisation/{id}`` — the study detail. A *draft* shows the
  multi-step configuration wizard (factors → responses → strategy +
  acquisition → seed + budget → launch); once launched the same page
  shows the live convergence trace, the current best / Pareto summary,
  and the Halt / Archive / Publish-surrogate controls, polled like a
  parametric-study run.

An optimisation study drives an adaptive outer loop (propose → run →
learn → repeat) over one pinned tool version using MATOFX's own
surrogate engine (the ``bo`` strategy; ADR-0009 amendment 2026-06-08).
The surrogate is the publishable artifact — "Publish surrogate" mints a
registry version from the study's latest working model.
"""

from __future__ import annotations

import contextlib
import logging
import math
import operator
from typing import Any

import plotly.graph_objects as go
from nicegui import ui

from matofx.studies._intsnap import snap_int
from matofx.studies.optimisation.config import is_weighted_sum
from matofx.studies.optimisation.sumcheck import sum_check_notes
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
from matofx.ui._features import materials_database_enabled, optimisation_framework_enabled
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import render_shell, require_logged_in, section_card
from matofx.ui.pages._input_groups import (
    OTHER_GROUP_LABEL,
    group_sections,
    has_groups,
    render_group_header,
    render_member_header,
)
from matofx.ui.state import current_token, has_permission
from matofx.ui.theme import themed_plotly

_log = logging.getLogger(__name__)

_OPTIMISATION_CREATE = "optimisation.create"
_REFRESH_INTERVAL_S = 3.0

# A study in one of these states never changes again, so the detail page
# stops polling once it reaches one (the trace is final).
_TERMINAL_STATES = frozenset({"complete", "cancelled", "failed"})

# Settled-for-polling: the runtime no longer owns the study, so the live
# trace is final and the poll must stop. ``pending_results_approval`` is
# settled too — the campaign has finished and only a human approval (made
# elsewhere) flips it to complete/failed, so polling it just spins forever
# and reads as "stuck on Running".
_SETTLED_STATES = _TERMINAL_STATES | {"pending_results_approval"}

# config factor type ← descriptor input type. Only these three input
# kinds can be a search axis (config.py rule 1); everything else is
# constant-only and isn't offered as a factor.
_INPUT_TYPE_TO_FACTOR: dict[str, str] = {
    "float": "float",
    "integer": "int",
    "select": "categorical",
}

_GOAL_OPTIONS = {
    "": "Don't optimise",
    "minimise": "Minimise",
    "maximise": "Maximise",
    "track": "Track only",
}
_ACQUISITION_OPTIONS = {
    "optimise": "Find the optimum (exploit)",
    "balanced": "Balanced",
    "explore": "Improve the model (explore)",
}
_STRATEGY_OPTIONS = {
    "bo": "Surrogate search / Bayesian optimisation (single)",
    "cmaes": "CMA-ES (single, scales past BO)",
    "de": "Differential evolution (single)",
    "pso": "Particle swarm (single)",
    "ga": "Genetic algorithm (single)",
    "nsga2": "NSGA-II (multi-objective, 2-3)",
    "nsga3": "NSGA-III (many objectives, >3)",
    "moea_d": "MOEA/D (multi-objective)",
    "random": "Random search (baseline)",
    "grid": "Grid search (baseline)",
}
_SCOPE_OPTIONS = {"personal": "Personal (only you)", "project": "Project", "global": "Global"}
_SURROGATE_KIND_OPTIONS = {
    "": "Same as the study used",
    "gp": "Gaussian process (RBF)",
    "gp_matern": "Gaussian process (Matern)",
    "poly": "Polynomial",
    "gbm": "Gradient boosting",
}


def _client() -> ApiClient:
    return ApiClient(get_app(), token=current_token())


def _feature_ok() -> bool:
    """Redirect home when the optimisation-framework feature is off (page *absent*)."""
    if not optimisation_framework_enabled():
        ui.navigate.to("/home")
        return False
    return True


def _state_chip(state: str) -> None:
    color = {
        "draft": "grey",
        "pending_start_approval": "amber",
        "pending_results_approval": "amber",
        "running": "blue",
        "complete": "green",
        "cancelled": "grey",
        "failed": "red",
    }.get(state, "grey")
    ui.chip(state.replace("_", " ").title()).props(f"color={color} text-color=white")


# ===================== workspace (list + create) =====================


@ui.page("/optimisation")
async def optimisation_page() -> None:
    """The Optimisation workspace — your studies + the create form."""
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    with ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;"):
        ui.label("Optimisation").style(
            "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
        )
        ui.label(
            "Drive an adaptive search over one tool: MATOFX builds a surrogate from the "
            "runs so far and proposes the next best run(s), until your budget or a "
            "convergence target is met."
        ).style("color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;")
        # Viewers (no create right) get just the catalogue; the surface is
        # feature-gated, so it's visible to read even without create rights.
        if not has_permission(_OPTIMISATION_CREATE):
            await _render_your_studies(client)
            return
        with (
            ui.tabs()
            .classes("w-full")
            .props("align=left active-color=primary indicator-color=primary")
            .style("border-bottom: 1px solid var(--matofx-card-border);") as tabs
        ):
            catalogue_tab = ui.tab("Catalogue", icon="track_changes").props("no-caps")
            new_tab = ui.tab("New Optimisation Study", icon="add").props("no-caps")
        with (
            ui.tab_panels(tabs, value=catalogue_tab)
            .classes("w-full")
            .style("background-color: transparent;")
        ):
            with ui.tab_panel(catalogue_tab).style("background-color: transparent;"):
                await _render_your_studies(client)
            with ui.tab_panel(new_tab).style("background-color: transparent;"):
                await _render_create_form(client)


# Plain-English status buckets for the catalogue filter — each folds the
# related lifecycle states (mirrors the Parametric Studies catalogue).
_STATE_BUCKETS: dict[str, set[str]] = {
    "draft": {"draft"},
    "running": {"running", "pending_start_approval", "pending_results_approval"},
    "complete": {"complete"},
    "cancelled": {"cancelled", "failed"},
}


def _study_target(study: dict[str, Any]) -> str:
    """The study's target as a label — tool or workflow name, not the alias."""
    if str(study.get("target_kind") or "") == "workflow":
        return str(study.get("workflow_name") or study.get("workflow_alias") or "")
    return str(study.get("tool_name") or study.get("tool_alias") or "")


def _study_haystack(study: dict[str, Any]) -> str:
    """Lower-cased search text: name + target + project + owner + id."""
    sid = int(study.get("id") or 0)
    bits = [
        str(study.get("name") or ""),
        _study_target(study),
        str(study.get("project_name") or ""),
        str(study.get("created_by_name") or ""),
        f"#{sid}",
    ]
    return " ".join(bits).lower()


def _study_matches(study: dict[str, Any], query: str, bucket: str) -> bool:
    """Whether a study passes the catalogue search box + status filter."""
    if bucket != "all" and str(study.get("state") or "") not in _STATE_BUCKETS.get(bucket, set()):
        return False
    return not query or query in _study_haystack(study)


def _render_study_row(study: dict[str, Any]) -> None:
    sid = int(study.get("id") or 0)
    name = str(study.get("name") or f"Study #{sid}")
    target = _study_target(study)
    project = str(study.get("project_name") or "")
    owner = str(study.get("created_by_name") or "")
    state = str(study.get("state") or "draft")
    batch = int(study.get("last_batch_number") or 0)
    with (
        ui.row()
        .classes("w-full items-center gap-3 p-2 rounded cursor-pointer")
        .style("background-color: var(--matofx-row-bg);")
        .on("click", lambda _e=None, _id=sid: ui.navigate.to(f"/optimisation/{_id}"))
    ):
        with ui.column().classes("gap-0 flex-grow").style("min-width: 0;"):
            ui.label(name).style("font-weight: 600;")
            bits = [b for b in (target, project, f"by {owner}" if owner else "") if b]
            if batch:
                bits.append(f"batch {batch}")
            ui.label(" · ".join(bits)).style(
                "color: var(--matofx-muted-text); font-size: 0.8rem; "
                "white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
        _state_chip(state)


async def _render_your_studies(client: ApiClient) -> None:
    """A searchable, filterable row-list of the caller's optimisation studies."""
    with section_card("track_changes", "Your optimisation studies"):
        try:
            projects = await client.list_projects()
        except ApiError as exc:
            ui.label(f"Couldn't load projects: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return
        studies: list[dict[str, Any]] = []
        for proj in projects:
            try:
                studies.extend(await client.list_optimisation_studies(int(proj["id"])))
            except ApiError:
                continue
        if not studies:
            ui.label("No optimisation studies yet. Create one below.").style(
                "color: var(--matofx-muted-text);"
            )
            return
        studies.sort(key=lambda s: str(s.get("created_at") or ""), reverse=True)

        filt: dict[str, str] = {"q": "", "bucket": "all"}
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            search = (
                ui.input(label="Search", placeholder="name, target, project, or owner…")
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
            shown = [s for s in studies if _study_matches(s, filt["q"], filt["bucket"])]
            with list_box:
                if not shown:
                    ui.label("No studies match your search and filter.").style(
                        "color: var(--matofx-muted-text); padding: 0.5rem 0.2rem;"
                    )
                else:
                    for study in shown[:200]:
                        _render_study_row(study)
            count_label.text = f"{len(shown)} of {len(studies)} studies"

        def _apply(bucket: str | None, query: str | None) -> None:
            if bucket is not None:
                filt["bucket"] = bucket
            if query is not None:
                filt["q"] = query.strip().lower()
            _refresh()

        search.on_value_change(lambda e: _apply(None, str(e.value or "")))
        _refresh()


async def _render_create_form(client: ApiClient) -> None:
    """Name → project → target (a tool, or a whole workflow chain; §3.4)."""
    with section_card("add", "New optimisation study"):
        try:
            projects = await client.list_projects()
            tools = await client.list_tools()
            workflows = await client.list_workflows()
        except ApiError as exc:
            ui.label(f"Couldn't load the form: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return
        if not projects:
            ui.label("You need a project first - create one from Projects.").style(
                "color: var(--matofx-muted-text);"
            )
            return

        project_opts = {int(p["id"]): str(p.get("name") or p["id"]) for p in projects}
        tool_opts = {str(t["alias"]): str(t.get("name") or t["alias"]) for t in tools}
        workflow_opts = {str(w["alias"]): str(w.get("name") or w["alias"]) for w in workflows}

        name_in = ui.input("Study name *").classes("w-full")
        project_sel = ui.select(project_opts, label="Project *").classes("w-full")
        target_sel = ui.select(
            {"tool": "A single tool", "workflow": "A workflow chain"},
            value="tool",
            label="What to optimise",
        ).classes("w-full")

        tool_sel = ui.select(tool_opts or {"": "(no tools available)"}, label="Tool").classes(
            "w-full"
        )
        tool_sel.bind_visibility_from(target_sel, "value", lambda v: v == "tool")
        workflow_sel = ui.select(
            workflow_opts or {"": "(no workflows available)"}, label="Workflow"
        ).classes("w-full")
        workflow_sel.bind_visibility_from(target_sel, "value", lambda v: v == "workflow")
        ui.label(
            "You'll choose the factors to vary, the responses to optimise, and the search "
            "strategy on the next screen."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

        async def _create() -> None:
            if not name_in.value or not project_sel.value:
                ui.notify("Name and project are required.", type="warning")
                return
            try:
                if target_sel.value == "workflow":
                    if not workflow_sel.value:
                        ui.notify("Pick a workflow to optimise.", type="warning")
                        return
                    study = await client.create_optimisation_workflow_study(
                        int(project_sel.value),
                        name=str(name_in.value),
                        workflow_alias=str(workflow_sel.value),
                    )
                else:
                    if not tool_sel.value:
                        ui.notify("Pick a tool to optimise.", type="warning")
                        return
                    detail = await client.get_tool(str(tool_sel.value))
                    version_id = detail.get("tool_version_id")
                    if version_id is None:
                        ui.notify("That tool has no published version.", type="negative")
                        return
                    study = await client.create_optimisation_study(
                        int(project_sel.value),
                        name=str(name_in.value),
                        tool_alias=str(tool_sel.value),
                        tool_version_id=int(version_id),
                    )
            except ApiError as exc:
                ui.notify(exc.detail, type="negative")
                return
            ui.navigate.to(f"/optimisation/{int(study['id'])}")

        ui.button("Create & configure", icon="tune", on_click=_create).props("color=primary")


# ===================== detail (draft wizard | live view) =====================


@ui.page("/optimisation/{study_id}")
async def optimisation_detail_page(study_id: int) -> None:
    """One study — the config wizard while draft, the live trace once launched."""
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    body = ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;")
    await _render_detail(client, int(study_id), body)


async def _render_detail(client: ApiClient, study_id: int, body: ui.column) -> None:
    body.clear()
    with body:
        try:
            study = await client.get_optimisation_study(study_id)
        except ApiError as exc:
            ui.label(f"Couldn't load this study: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return

        # Back link above the title (left-aligned, back-arrow) so it clears
        # the fixed theme / help buttons pinned to the top-right.
        ui.button(
            "Back to optimisation studies",
            icon="arrow_back",
            on_click=lambda: ui.navigate.to("/optimisation"),
        ).props("flat dense no-caps").style(
            "color: var(--matofx-muted-text); margin-left: -0.3rem;"
        )
        with ui.row().classes("w-full items-center gap-3"):
            ui.label(str(study.get("name") or f"Study #{study_id}")).style(
                "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
            )
            # Own container so the live poll can flip Running → Complete here
            # without re-rendering the whole detail (which would blank the body).
            chip_box = ui.row().classes("items-center")
            with chip_box:
                _state_chip(str(study.get("state") or "draft"))
        # Caption: target label · pinned version · what's being optimised.
        target = _study_target(study)
        version = str(study.get("tool_version") or study.get("workflow_version") or "")
        kind = (
            "workflow chain" if str(study.get("target_kind") or "") == "workflow" else "single tool"
        )
        caption_bits = [b for b in (target, f"version {version}" if version else "", kind) if b]
        if caption_bits:
            ui.label(" · ".join(caption_bits)).style(
                "color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;"
            )

        state = str(study.get("state"))
        if state == "draft":
            await _render_wizard(client, study, body)
        elif state == "pending_start_approval":
            await _render_pending_approval(client, study, body)
        else:
            await _render_live(client, study, body, chip_box)


# ----- draft configuration wizard -----


async def _render_wizard(client: ApiClient, study: dict[str, Any], body: ui.column) -> None:
    """The multi-step config wizard: factors → responses → strategy → seed/budget → launch.

    Tool and workflow targets (§3.4) share every step but the first two:
    a tool draws factors/responses from its own descriptor; a workflow
    draws *member-qualified* factors (each member's open inputs) and
    responses (each member's numeric outputs).
    """
    factor_widgets: Any
    response_widgets: Any
    # Seed/budget cohort filtering is tool-scoped; a workflow has no single
    # tool, so leave the alias empty (no cohorts match → LHS seeding only).
    # Must be defined for both branches: the Seed & budget step reads it
    # eagerly when the stepper is built, so an undefined name here aborts the
    # wizard render right at that step.
    tool_alias = ""
    if str(study.get("target_kind")) == "workflow":
        workflow_alias = str(
            (study.get("config_json") or {}).get("target", {}).get("workflow_alias")
            or study.get("workflow_alias")
            or ""
        )
        try:
            (
                factor_fields,
                response_fields,
                member_names,
                member_groups,
            ) = await _load_workflow_fields(client, workflow_alias)
        except ApiError as exc:
            ui.label(f"Couldn't load the workflow: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return
        if not any(f.get("type") in _INPUT_TYPE_TO_FACTOR for _m, f in factor_fields):
            ui.label("This workflow has no open numeric/choice inputs to vary.").style(
                "color: var(--matofx-error-text);"
            )
            return
        if not response_fields:
            ui.label("This workflow produces no numeric outputs to optimise.").style(
                "color: var(--matofx-error-text);"
            )
            return
        factor_widgets = _WorkflowFactorWidgets(factor_fields, member_names, member_groups)
        response_widgets = _WorkflowResponseWidgets(response_fields, member_names)
        # Required open inputs (any type) are fixed-value candidates, keyed
        # member.input to match the factor naming (§3.5); labelled by tool name.
        fixed_candidates = [
            {
                "key": f"{member}.{f.get('name')}",
                "label": _qualified_label(
                    member_names.get(member, member), str(f.get("label") or f.get("name") or "")
                ),
                "field": f,
                "member": member,
                "group": f.get("group"),
            }
            for member, f in factor_fields
            if f.get("required")
        ]
        # Fixed-input grouping is member-scoped for a workflow: each member's
        # required inputs nest under that tool's own input groups.
        fixed_input_groups: list[dict[str, Any]] | None = None
        fixed_member_names: dict[str, str] | None = member_names
        fixed_member_groups: dict[str, list[dict[str, Any]]] | None = member_groups
        # Sum-checks are per member descriptor; gather each member's groups,
        # labelled "tool · field" so the banner reads in human terms.
        sum_notes = []
        _fields_by_member: dict[str, list[dict[str, Any]]] = {}
        for member, f in factor_fields:
            _fields_by_member.setdefault(member, []).append(f)
        for member, fields in _fields_by_member.items():
            sum_notes.extend(
                sum_check_notes(fields, prefix=f"{member_names.get(member, member)} · ")
            )
    else:
        tool_alias = str(study.get("tool_alias") or "")
        try:
            tool = await client.get_tool(tool_alias)
        except ApiError as exc:
            ui.label(f"Couldn't load the tool descriptor: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return
        descriptor = tool.get("descriptor") or {}
        inputs = [
            i for i in (descriptor.get("inputs") or []) if i.get("type") in _INPUT_TYPE_TO_FACTOR
        ]
        outputs = [o for o in (descriptor.get("outputs") or []) if o.get("type") == "number"]
        if not inputs:
            ui.label(
                "This tool has no numeric or choice inputs to vary — it can't be optimised."
            ).style("color: var(--matofx-error-text);")
            return
        if not outputs:
            ui.label("This tool produces no numeric outputs to optimise.").style(
                "color: var(--matofx-error-text);"
            )
            return
        factor_widgets = _FactorWidgets(inputs, descriptor.get("input_groups"))
        response_widgets = _ResponseWidgets(outputs)
        # Every required input is a fixed-value candidate (the Fixed inputs
        # step shows the ones not chosen as factors), keyed by input name.
        fixed_candidates = [
            {
                "key": str(i.get("name")),
                "label": str(i.get("label") or i.get("name")),
                "field": i,
                "group": i.get("group"),
            }
            for i in (descriptor.get("inputs") or [])
            if i.get("required")
        ]
        # Tool target: group fixed inputs by the tool's own input groups.
        fixed_input_groups = descriptor.get("input_groups")
        fixed_member_names = None
        fixed_member_groups = None
        sum_notes = sum_check_notes(descriptor.get("inputs") or [])

    fixed_ctl = _FixedInputsControls(
        client,
        fixed_candidates,
        materials_enabled=materials_database_enabled(),
        input_groups=fixed_input_groups,
        member_names=fixed_member_names,
        member_groups=fixed_member_groups,
    )

    # Local accumulator; each step persists its keys to the draft (DB-only,
    # [[feedback_save_db_only]]) so partial progress survives a reload.
    sid = int(study["id"])
    cfg = dict(study.get("config_json") or {})

    async def _patch(patch: dict[str, Any]) -> bool:
        try:
            await client.update_optimisation_study(sid, patch)
        except ApiError as exc:
            ui.notify(exc.detail or "Couldn't save that step.", type="negative")
            return False
        cfg.update(patch)
        return True

    # Eligible start-gate approvers (§14.1) — empty unless the install has
    # an optimisation_approver pool; the Launch step offers a picker so a
    # gated tool can be nominated an approver in the same flow.
    try:
        approvers = await client.list_optimisation_approvers()
    except ApiError:
        approvers = []

    with ui.stepper().props("vertical").classes("w-full") as stepper:
        with ui.step("Factors to vary"):
            ui.label("Tick each input the search may change, and set its range.").style(
                "color: var(--matofx-muted-text);"
            )
            for _value, _labels in sum_notes:
                # These inputs must total a fixed value (e.g. percentages = 100);
                # the optimiser balances them automatically (§3.6), so the user
                # doesn't have to make their ranges add up.
                with (
                    ui.row()
                    .classes("w-full items-center gap-2 q-pa-sm")
                    .style("background: var(--matofx-info-bg, #e8f0fe); border-radius: 6px;")
                ):
                    ui.icon("functions").style("color: #1a56db;")
                    ui.label(
                        f"{', '.join(_labels)} are kept summing to {_value:g} — the optimiser "
                        "balances them automatically, so you can set their ranges independently."
                    ).style("color: #1a3a8a; font-size: 0.85rem;")
            factor_widgets.render()

            async def _next_factors() -> None:
                try:
                    factors = factor_widgets.collect()
                except ValueError as exc:
                    ui.notify(str(exc), type="warning")
                    return
                if await _patch({"factors": factors}):
                    # Rebuild the held-constant form now the varied set is
                    # known. A workflow factor's ``name`` is arbitrary, so key
                    # the varied set by ``member.input`` to match the fixed
                    # candidates; a tool factor's name is the input name.
                    if fixed_ctl.has_candidates:
                        factor_keys = {
                            f"{f['member']}.{f['input']}"
                            if "member" in f and "input" in f
                            else str(f["name"])
                            for f in factors
                        }
                        fixed_ctl.render_into(factor_keys, dict(cfg.get("fixed_inputs") or {}))
                    stepper.next()

            with ui.stepper_navigation():
                ui.button("Next", on_click=_next_factors).props("color=primary")

        if fixed_ctl.has_candidates:
            with ui.step("Fixed inputs"):
                ui.label(
                    "Required inputs the search isn't varying (e.g. material choices) are held "
                    "constant. Set each one — they apply to every run."
                ).style("color: var(--matofx-muted-text);")
                fixed_ctl.bind_container(ui.column().classes("w-full gap-3"))

                async def _next_fixed() -> None:
                    try:
                        patch = fixed_ctl.collect()
                    except ValueError as exc:
                        ui.notify(str(exc), type="warning")
                        return
                    if await _patch({"fixed_inputs": patch}):
                        stepper.next()

                with ui.stepper_navigation():
                    ui.button("Back", on_click=stepper.previous).props("flat")
                    ui.button("Next", on_click=_next_fixed).props("color=primary")

        with ui.step("Responses to optimise"):
            ui.label(
                "Pick a goal for each numeric output. At least one must be minimised or maximised."
            ).style("color: var(--matofx-muted-text);")
            response_widgets.render()

            async def _next_responses() -> None:
                try:
                    responses = response_widgets.collect()
                except ValueError as exc:
                    ui.notify(str(exc), type="warning")
                    return
                if await _patch({"responses": responses}):
                    # The Navigator reads the saved factors + responses and
                    # pre-selects a strategy with a rationale (§5.6); fall back
                    # to the local single/multi nudge if it's unavailable.
                    try:
                        rec = await client.get_optimisation_recommendation(sid)
                        _strategy_ctl.apply_recommendation(rec)
                    except ApiError:
                        _strategy_ctl.sync_to(response_widgets.optimised_count())
                    stepper.next()

            with ui.stepper_navigation():
                ui.button("Back", on_click=stepper.previous).props("flat")
                ui.button("Next", on_click=_next_responses).props("color=primary")

        with ui.step("Strategy"):
            _strategy_ctl = _StrategyControls()
            _strategy_ctl.render()

            async def _next_strategy() -> None:
                patch = _strategy_ctl.collect()
                if await _patch(patch):
                    stepper.next()

            with ui.stepper_navigation():
                ui.button("Back", on_click=stepper.previous).props("flat")
                ui.button("Next", on_click=_next_strategy).props("color=primary")

        with ui.step("Seed & budget"):
            seed_ctl = _SeedBudgetControls(client, tool_alias, _strategy_ctl)
            await seed_ctl.render()

            async def _next_budget() -> None:
                try:
                    patch = seed_ctl.collect()
                except ValueError as exc:
                    ui.notify(str(exc), type="warning")
                    return
                if await _patch(patch):
                    stepper.next()

            with ui.stepper_navigation():
                ui.button("Back", on_click=stepper.previous).props("flat")
                ui.button("Next", on_click=_next_budget).props("color=primary")

        with ui.step("Launch"):
            ui.label(
                "Validate the whole configuration against the tool and start the search. "
                "Once running, the study queues its first batch of runs."
            ).style("color: var(--matofx-muted-text);")
            approver_sel: Any = None
            if approvers:
                ui.label(
                    "If this tool requires sign-off before a campaign starts, nominate an "
                    "approver (the study waits in 'pending start approval' until they agree)."
                ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
                approver_sel = ui.select(
                    {int(a["user_id"]): str(a["username"]) for a in approvers},
                    label="Start approver (only used if the tool gates campaigns)",
                ).classes("w-full")

            async def _launch() -> None:
                approver_id = (
                    int(approver_sel.value)
                    if approver_sel is not None and approver_sel.value
                    else None
                )
                try:
                    launched = await client.configure_optimisation_study(
                        sid, start_approver_user_id=approver_id
                    )
                except ApiError as exc:
                    ui.notify(exc.detail or "The configuration isn't valid yet.", type="negative")
                    return
                if str(launched.get("state")) == "pending_start_approval":
                    ui.notify("Submitted for start approval.", type="positive")
                else:
                    ui.notify("Optimisation launched.", type="positive")
                await _render_detail(client, sid, body)

            with ui.stepper_navigation():
                ui.button("Back", on_click=stepper.previous).props("flat")
                ui.button("Launch optimisation", icon="rocket_launch", on_click=_launch).props(
                    "color=primary"
                )


class _FactorWidgets:
    """Per-input row: an include toggle + a range / choices editor.

    Inputs are laid out under their descriptor input-group headers (in the
    same column-then-row order the job form uses); a tool with no groups
    renders as a flat list, unchanged.
    """

    def __init__(
        self,
        inputs: list[dict[str, Any]],
        input_groups: list[dict[str, Any]] | None = None,
    ) -> None:
        self._inputs = inputs
        self._input_groups = input_groups
        self._rows: dict[str, dict[str, Any]] = {}

    def render(self) -> None:
        sections = group_sections(self._inputs, self._input_groups)
        show_headers = has_groups(sections)
        for header, members in sections:
            if show_headers:
                render_group_header(header or OTHER_GROUP_LABEL)
            for inp in members:
                self._render_factor_card(inp)

    def _render_factor_card(self, inp: dict[str, Any]) -> None:
        name = str(inp["name"])
        ftype = _INPUT_TYPE_TO_FACTOR[str(inp["type"])]
        with ui.card().classes("w-full"):
            label = str(inp.get("label") or name)
            include = ui.checkbox(label)
            editor = ui.row().classes("w-full items-center gap-3 pl-8")
            # ``label`` rides along so a validation message names the
            # factor the way the tick-box above it does, rather than by
            # the descriptor alias nobody outside the tool has seen.
            row: dict[str, Any] = {
                "include": include,
                "type": ftype,
                "name": name,
                "label": label,
            }
            with editor:
                if ftype == "categorical":
                    opts = list(inp.get("options") or [])
                    row["choices"] = (
                        ui.select(opts, multiple=True, label="Choices")
                        .props("use-chips")
                        .classes("w-full")
                    )
                    row["choices"].value = opts
                else:
                    lo = inp.get("minimum")
                    hi = inp.get("maximum")
                    # An integer input varies over whole numbers only, so its
                    # range must be whole too — step the field by 1 and show
                    # no decimals (Phase D; the config validator backstops it).
                    num_kw: dict[str, Any] = {"step": 1, "format": "%.0f"} if ftype == "int" else {}
                    row["min"] = ui.number("Min", value=lo, **num_kw).classes("w-32")
                    row["max"] = ui.number("Max", value=hi, **num_kw).classes("w-32")
                    if inp.get("unit"):
                        ui.label(str(inp["unit"])).style("color: var(--matofx-muted-text);")
                    if ftype == "int":
                        ui.label("whole numbers").style(
                            "color: var(--matofx-muted-text); font-size: 0.8em;"
                        )
            editor.bind_visibility_from(include, "value")
            self._rows[name] = row

    def collect(self) -> list[dict[str, Any]]:
        factors: list[dict[str, Any]] = []
        for name, row in self._rows.items():
            if not row["include"].value:
                continue
            shown = str(row.get("label") or name)
            if row["type"] == "categorical":
                choices = list(row["choices"].value or [])
                if not choices:
                    raise ValueError(f"Factor '{shown}': pick at least one choice.")
                factors.append({"name": name, "type": "categorical", "choices": choices})
            else:
                lo, hi = row["min"].value, row["max"].value
                if lo is None or hi is None:
                    raise ValueError(f"Factor '{shown}': set both min and max.")
                if row["type"] == "int":
                    lo, hi = snap_int(lo), snap_int(hi)
                if hi < lo:
                    raise ValueError(f"Factor '{shown}': max is below min.")
                factors.append({"name": name, "type": row["type"], "min": lo, "max": hi})
        if not factors:
            raise ValueError("Tick at least one factor to vary.")
        return factors


class _ResponseWidgets:
    """Per-output row: a goal select. Scalar responses map straight to the output."""

    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self._outputs = outputs
        self._sels: dict[str, Any] = {}
        self._weights: dict[str, Any] = {}

    def render(self) -> None:
        ui.label(
            "Set a goal per output. With two or more objectives, the weight sets each "
            "objective's relative importance — used to rank the trade-off (Pareto) front "
            "so you can pick the preferred design. Leave at 1 for equal importance."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        for out in self._outputs:
            name = str(out["name"])
            with ui.row().classes("w-full items-center gap-3"):
                ui.label(str(out.get("label") or name)).classes("flex-grow")
                if out.get("unit"):
                    ui.label(str(out["unit"])).style("color: var(--matofx-muted-text);")
                self._sels[name] = ui.select(_GOAL_OPTIONS, value="").classes("w-48")
                weight = ui.number("Weight", value=1.0, min=0.0001, step=0.1).classes("w-28")
                weight.bind_visibility_from(
                    self._sels[name], "value", lambda v: v in ("minimise", "maximise")
                )
                self._weights[name] = weight

    def optimised_count(self) -> int:
        return sum(1 for s in self._sels.values() if s.value in ("minimise", "maximise"))

    def collect(self) -> list[dict[str, Any]]:
        responses: list[dict[str, Any]] = []
        for name, sel in self._sels.items():
            goal = sel.value
            if not goal:
                continue
            resp: dict[str, Any] = {
                "name": name,
                "goal": goal,
                "spec": {"kind": "scalar", "name": name},
            }
            if goal in ("minimise", "maximise"):
                weight = self._weights.get(name)
                if weight is not None and weight.value:
                    resp["weight"] = float(weight.value)
            responses.append(resp)
        if not any(r["goal"] in ("minimise", "maximise") for r in responses):
            raise ValueError("Choose at least one response to minimise or maximise.")
        return responses


async def _load_workflow_fields(
    client: ApiClient, workflow_alias: str
) -> tuple[
    list[tuple[str, dict[str, Any]]],
    list[tuple[str, dict[str, Any]]],
    dict[str, str],
    dict[str, list[dict[str, Any]]],
]:
    """Gather a workflow's member-qualified open inputs + numeric outputs (§3.4).

    Returns ``(factor_fields, response_fields, member_names, member_groups)``.
    The first two are ``(member_alias, field)`` pairs; ``member_names`` maps
    each member alias to its tool's display name so every wizard surface can
    show labels, not aliases ([[feedback_use_labels_not_names]]);
    ``member_groups`` maps each member alias to its tool's ``input_groups`` so
    the factor picker can nest each member's inputs under their own group
    headers. An input is *open* (a factor candidate) unless it's wired from
    another member of the chain (``downstream_input`` + an in-chain
    ``upstream_tool``).
    """
    workflows = await client.list_workflows()
    wf = next((w for w in workflows if str(w.get("alias")) == workflow_alias), None)
    if wf is None:
        return [], [], {}, {}
    members = [str(a) for a in (wf.get("tool_aliases") or [])]
    member_set = set(members)
    factor_fields: list[tuple[str, dict[str, Any]]] = []
    response_fields: list[tuple[str, dict[str, Any]]] = []
    member_names: dict[str, str] = {}
    member_groups: dict[str, list[dict[str, Any]]] = {}
    for member in members:
        tool = await client.get_tool(member)
        member_names[member] = str(tool.get("name") or member)
        descriptor = tool.get("descriptor") or {}
        member_groups[member] = list(descriptor.get("input_groups") or [])
        for inp in descriptor.get("inputs") or []:
            wired = inp.get("downstream_input") and inp.get("upstream_tool") in member_set
            if not wired:
                factor_fields.append((member, inp))
        for out in descriptor.get("outputs") or []:
            if out.get("type") == "number":
                response_fields.append((member, out))
    return factor_fields, response_fields, member_names, member_groups


class _WorkflowFactorWidgets:
    """Member-qualified factor picker — each open input of a chain member (§3.4).

    Same ``render`` / ``collect`` interface as :class:`_FactorWidgets`, but
    rows are keyed ``member.input`` and ``collect`` emits the workflow
    factor shape (``{name, member, input, type, ...}``).
    """

    def __init__(
        self,
        member_fields: list[tuple[str, dict[str, Any]]],
        member_names: dict[str, str] | None = None,
        member_groups: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._fields = [
            (m, f) for (m, f) in member_fields if f.get("type") in _INPUT_TYPE_TO_FACTOR
        ]
        self._member_names = member_names or {}
        self._member_groups = member_groups or {}
        self._rows: dict[str, dict[str, Any]] = {}

    def _members_in_order(self) -> list[str]:
        """Member aliases in first-appearance (workflow chain) order."""
        seen: list[str] = []
        for member, _f in self._fields:
            if member not in seen:
                seen.append(member)
        return seen

    def render(self) -> None:
        for member in self._members_in_order():
            member_inputs = [f for (m, f) in self._fields if m == member]
            render_member_header(self._member_names.get(member, member))
            sections = group_sections(member_inputs, self._member_groups.get(member))
            show_headers = has_groups(sections)
            for header, members in sections:
                if show_headers:
                    render_group_header(header or OTHER_GROUP_LABEL)
                for inp in members:
                    self._render_factor_card(member, inp)

    def _render_factor_card(self, member: str, inp: dict[str, Any]) -> None:
        name = str(inp["name"])
        key = f"{member}.{name}"
        ftype = _INPUT_TYPE_TO_FACTOR[str(inp["type"])]
        with ui.card().classes("w-full"):
            include = ui.checkbox(str(inp.get("label") or name))
            editor = ui.row().classes("w-full items-center gap-3 pl-8")
            row: dict[str, Any] = {
                "include": include,
                "type": ftype,
                "member": member,
                "input": name,
            }
            with editor:
                if ftype == "categorical":
                    opts = list(inp.get("options") or [])
                    row["choices"] = (
                        ui.select(opts, multiple=True, label="Choices")
                        .props("use-chips")
                        .classes("w-full")
                    )
                    row["choices"].value = opts
                else:
                    # Integer inputs vary over whole numbers only (Phase D).
                    num_kw: dict[str, Any] = {"step": 1, "format": "%.0f"} if ftype == "int" else {}
                    row["min"] = ui.number("Min", value=inp.get("minimum"), **num_kw).classes(
                        "w-32"
                    )
                    row["max"] = ui.number("Max", value=inp.get("maximum"), **num_kw).classes(
                        "w-32"
                    )
                    if inp.get("unit"):
                        ui.label(str(inp["unit"])).style("color: var(--matofx-muted-text);")
                    if ftype == "int":
                        ui.label("whole numbers").style(
                            "color: var(--matofx-muted-text); font-size: 0.8em;"
                        )
            editor.bind_visibility_from(include, "value")
            self._rows[key] = row

    def collect(self) -> list[dict[str, Any]]:
        factors: list[dict[str, Any]] = []
        for key, row in self._rows.items():
            if not row["include"].value:
                continue
            base = {
                "name": key,
                "member": row["member"],
                "input": row["input"],
                "type": row["type"],
            }
            if row["type"] == "categorical":
                choices = list(row["choices"].value or [])
                if not choices:
                    raise ValueError(f"Factor '{key}': pick at least one choice.")
                factors.append({**base, "choices": choices})
            else:
                lo, hi = row["min"].value, row["max"].value
                if lo is None or hi is None:
                    raise ValueError(f"Factor '{key}': set both min and max.")
                if row["type"] == "int":
                    lo, hi = snap_int(lo), snap_int(hi)
                if hi < lo:
                    raise ValueError(f"Factor '{key}': max is below min.")
                factors.append({**base, "min": lo, "max": hi})
        if not factors:
            raise ValueError("Tick at least one factor to vary.")
        return factors


class _WorkflowResponseWidgets:
    """Member-qualified response picker — each member's numeric output (§3.4)."""

    def __init__(
        self,
        member_outputs: list[tuple[str, dict[str, Any]]],
        member_names: dict[str, str] | None = None,
    ) -> None:
        self._outputs = member_outputs
        self._member_names = member_names or {}
        self._sels: dict[str, tuple[str, str, Any]] = {}
        self._weights: dict[str, Any] = {}

    def render(self) -> None:
        ui.label(
            "Set a goal per output. With two or more objectives, the weight sets each "
            "objective's relative importance. Leave at 1 for equal importance."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        for member, out in self._outputs:
            name = str(out["name"])
            key = f"{member}.{name}"
            member_label = self._member_names.get(member, member)
            with ui.row().classes("w-full items-center gap-3"):
                ui.label(_qualified_label(member_label, str(out.get("label") or name))).classes(
                    "flex-grow"
                )
                if out.get("unit"):
                    ui.label(str(out["unit"])).style("color: var(--matofx-muted-text);")
                sel = ui.select(_GOAL_OPTIONS, value="").classes("w-48")
                self._sels[key] = (member, name, sel)
                weight = ui.number("Weight", value=1.0, min=0.0001, step=0.1).classes("w-28")
                weight.bind_visibility_from(sel, "value", lambda v: v in ("minimise", "maximise"))
                self._weights[key] = weight

    def optimised_count(self) -> int:
        return sum(1 for (_m, _n, s) in self._sels.values() if s.value in ("minimise", "maximise"))

    def collect(self) -> list[dict[str, Any]]:
        responses: list[dict[str, Any]] = []
        for key, (member, name, sel) in self._sels.items():
            goal = sel.value
            if not goal:
                continue
            resp: dict[str, Any] = {
                "name": key,
                "goal": goal,
                "spec": {"kind": "scalar", "member": member, "name": name},
            }
            if goal in ("minimise", "maximise"):
                weight = self._weights.get(key)
                if weight is not None and weight.value:
                    resp["weight"] = float(weight.value)
            responses.append(resp)
        if not any(r["goal"] in ("minimise", "maximise") for r in responses):
            raise ValueError("Choose at least one response to minimise or maximise.")
        return responses


class _StrategyControls:
    """Strategy + (for ``bo``) acquisition mode. Run mode is real for now."""

    def __init__(self) -> None:
        self._strategy: Any = None
        self._acq: Any = None
        self._acq_box: Any = None
        self._pool: Any = None
        self._badge: Any = None
        self._mop_box: Any = None
        self._mop_enable: Any = None

    def render(self) -> None:
        ui.label(
            "MATOFX's surrogate-driven search (Bayesian optimisation) is the default for a "
            "single objective; the evolutionary strategies cover larger / mixed spaces and "
            "multiple objectives."
        ).style("color: var(--matofx-muted-text);")
        ui.label(
            "With two or more objectives: pick a multi-objective strategy (e.g. NSGA-II) for a "
            "Pareto front, or a single-objective strategy (e.g. Bayesian optimisation) to run a "
            "weighted-sum search that combines them via the weights you set on the responses step."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        self._strategy = ui.select(
            dict(_STRATEGY_OPTIONS),
            value="bo",
            label="Strategy",
        ).classes("w-full")
        self._badge = ui.column().classes("w-full gap-0")
        self._acq_box = ui.column().classes("w-full")
        with self._acq_box:
            ui.label("How should the surrogate pick the next run(s)?").style(
                "color: var(--matofx-muted-text);"
            )
            self._acq = ui.select(
                _ACQUISITION_OPTIONS, value="balanced", label="Acquisition"
            ).classes("w-full")
            self._pool = ui.number(
                "Candidate pool per step", value=512, min=16, max=1_000_000, precision=0
            ).classes("w-64")
            ui.label(
                "How many trial points the surrogate scores before choosing the next run. "
                "Higher = a finer search but slower proposals."
            ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        self._acq_box.bind_visibility_from(self._strategy, "value", lambda v: v == "bo")
        # MOP/CoP variable reduction (§5.7) — only meaningful for the
        # surrogate-search strategy, which builds the metamodel it needs.
        self._mop_box = ui.column().classes("w-full")
        with self._mop_box:
            self._mop_enable = ui.checkbox(
                "Enable MOP/CoP — auto-pick the best metamodel + drop the factors that "
                "barely affect the result"
            )
        self._mop_box.bind_visibility_from(self._strategy, "value", lambda v: v == "bo")

    def sync_to(self, optimised_count: int) -> None:
        """Nudge the default strategy to match the objective count just chosen."""
        if self._strategy is None:
            return
        single_only = {"bo", "cmaes", "de", "pso", "ga"}
        multi_only = {"nsga2", "nsga3", "moea_d"}
        if optimised_count >= 2 and self._strategy.value in single_only:
            self._strategy.value = "nsga2"
        elif optimised_count <= 1 and self._strategy.value in multi_only:
            self._strategy.value = "bo"

    def apply_recommendation(self, rec: dict[str, Any]) -> None:
        """Pre-select the Navigator's pick + show a 'Recommended' badge (§5.6)."""
        if self._strategy is None:
            return
        strat = str(rec.get("strategy") or "")
        if strat in _STRATEGY_OPTIONS:
            self._strategy.value = strat
        acq = rec.get("acquisition")
        if acq and self._acq is not None:
            self._acq.value = str(acq)
        self._badge.clear()
        with self._badge:
            with ui.row().classes("items-center gap-2"):
                ui.icon("auto_awesome").style("color: var(--matofx-primary, #6750a4);")
                ui.label(f"Recommended: {_STRATEGY_OPTIONS.get(strat, strat)}").style(
                    "font-weight: 600;"
                )
            if rec.get("rationale"):
                ui.label(str(rec["rationale"])).style(
                    "color: var(--matofx-muted-text); font-size: 0.85rem;"
                )

    def collect(self) -> dict[str, Any]:
        patch: dict[str, Any] = {"strategy": self._strategy.value, "run_mode": "real"}
        if self._strategy.value == "bo":
            patch["acquisition"] = self._acq.value
            patch["mop"] = {"enabled": bool(self._mop_enable.value), "cop_cutoff": 0.01}
            if self._pool is not None and self._pool.value:
                patch["surrogate_candidate_pool"] = int(self._pool.value)
        return patch


class _FixedInputsControls:
    """Held-constant values for required inputs the search isn't varying (§3.5).

    Tool and workflow targets share this: ``candidates`` is the full list of
    required inputs that *could* need holding — for a tool keyed by input
    name, for a workflow by ``member.input`` (open inputs only; wired ones
    come from upstream). ``render_into`` is called when the user leaves the
    Factors step (the varied set is known only then) and rebuilds the form
    for whatever required inputs aren't factors. Materials reuse the real
    picker so they freeze at queue time exactly like a normal submission.
    """

    def __init__(
        self,
        client: ApiClient,
        candidates: list[dict[str, Any]],
        *,
        materials_enabled: bool,
        input_groups: list[dict[str, Any]] | None = None,
        member_names: dict[str, str] | None = None,
        member_groups: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        # Each candidate: {"key", "label", "field", "group", ["member"]} where
        # field is the raw descriptor input dict, key is the config key
        # (name / member.input), and group is the field's input-group name.
        # Grouping context: a tool target passes ``input_groups``; a workflow
        # target passes ``member_names``/``member_groups`` (which switches the
        # form to member-headed sections). ``member_groups is not None`` is the
        # workflow-mode flag.
        self._client = client
        self._candidates = candidates
        self._materials_enabled = materials_enabled
        self._input_groups = input_groups
        self._member_names = member_names or {}
        self._member_groups = member_groups
        self._container: Any = None
        self._rows: dict[str, dict[str, Any]] = {}

    @property
    def has_candidates(self) -> bool:
        return bool(self._candidates)

    def bind_container(self, container: Any) -> None:
        self._container = container

    def _material_ctx(self) -> Any:
        from matofx.tools.forms import MaterialInputContext  # noqa: PLC0415

        async def _list(material_class: str, required_props: list[str] | None) -> Any:
            return await self._client.materials_picker(
                material_class=material_class, required_props=required_props
            )

        return MaterialInputContext(list_materials=_list)

    def render_into(self, factor_keys: set[str], seed: dict[str, Any]) -> None:
        """Rebuild the held-constant form for required inputs that aren't factors."""
        if self._container is None:
            return
        self._container.clear()
        self._rows = {}
        pending = [c for c in self._candidates if c["key"] not in factor_keys]
        with self._container:
            if not pending:
                ui.label("Every required input is being varied — nothing to hold constant.").style(
                    "color: var(--matofx-muted-text);"
                )
                return
            ui.label(
                "Set a value for each required input the search isn't varying. These are "
                "held constant across every run."
            ).style("color: var(--matofx-muted-text);")
            material_ctx = self._material_ctx() if self._materials_enabled else None
            self._render_grouped(pending, seed, material_ctx)

    def _render_grouped(
        self, pending: list[dict[str, Any]], seed: dict[str, Any], material_ctx: Any
    ) -> None:
        """Lay the pending candidates out under their input-group headers.

        Workflow targets (``member_groups`` set) get a member (tool) header per
        chain step, then that member's own group headers nested beneath; tool
        targets get a single level of the tool's group headers. Either way a
        target with no declared groups renders as a flat list, unchanged.
        """
        if self._member_groups is not None:
            members_seen: list[str] = []
            for cand in pending:
                member = str(cand.get("member") or "")
                if member not in members_seen:
                    members_seen.append(member)
            for member in members_seen:
                member_pending = [c for c in pending if str(c.get("member") or "") == member]
                render_member_header(self._member_names.get(member, member))
                self._render_sections(
                    member_pending, self._member_groups.get(member), seed, material_ctx
                )
        else:
            self._render_sections(pending, self._input_groups, seed, material_ctx)

    def _render_sections(
        self,
        candidates: list[dict[str, Any]],
        input_groups: list[dict[str, Any]] | None,
        seed: dict[str, Any],
        material_ctx: Any,
    ) -> None:
        # ``group_sections`` reads each item's ``group`` key, so it buckets the
        # candidate dicts directly (their ``group`` mirrors the field's).
        sections = group_sections(candidates, input_groups)
        show_headers = has_groups(sections)
        for header, members in sections:
            if show_headers:
                render_group_header(header or OTHER_GROUP_LABEL)
            for cand in members:
                self._render_one(cand, seed.get(cand["key"]), material_ctx)

    def _render_one(self, cand: dict[str, Any], seeded: Any, material_ctx: Any) -> None:
        from matofx.tools.descriptor import InputField  # noqa: PLC0415

        field = cand["field"]
        key = cand["key"]
        label = cand["label"]
        ftype = str(field.get("type"))
        unit = field.get("unit")
        default = seeded if seeded is not None else field.get("default")
        row: dict[str, Any] = {"type": ftype, "label": label}

        if ftype == "material":
            if material_ctx is None:
                ui.label(
                    f"{label}: the materials database is unavailable, so this can't be set."
                ).style("color: var(--matofx-error-text);")
                row["unsupported"] = True
            else:
                from matofx.tools.forms.render import _render_material_field  # noqa: PLC0415

                spec = InputField(
                    name=str(field.get("name")),
                    label=str(field.get("label") or field.get("name")),
                    type="material",
                    required=True,
                    material_class=field.get("material_class"),
                    required_properties=list(field.get("required_properties") or []),
                )
                holder = _render_material_field(spec, label, material_input_context=material_ctx)
                if seeded:
                    holder.value = str(seeded)
                row["holder"] = holder
        elif ftype in ("float", "integer"):
            num_kw: dict[str, Any] = {"step": 1, "format": "%.0f"} if ftype == "integer" else {}
            with ui.row().classes("items-center gap-2"):
                widget = ui.number(label, value=default, **num_kw).classes("w-64")
                if unit:
                    ui.label(str(unit)).style("color: var(--matofx-muted-text);")
            row["widget"] = widget
        elif ftype == "select":
            opts = list(field.get("options") or [])
            row["widget"] = ui.select(
                {o: o for o in opts},
                value=default if default in opts else None,
                label=label,
            ).classes("w-full")
        elif ftype == "multiselect":
            opts = list(field.get("options") or [])
            row["widget"] = (
                ui.select(
                    {o: o for o in opts},
                    multiple=True,
                    value=list(default or []),
                    label=label,
                )
                .props("use-chips")
                .classes("w-full")
            )
        elif ftype == "boolean":
            row["widget"] = ui.checkbox(label, value=bool(default))
        elif ftype == "string":
            row["widget"] = ui.input(
                label, value=str(default) if default is not None else ""
            ).classes("w-full")
        else:  # file / table — not holdable from the wizard
            ui.label(
                f"{label}: a '{ftype}' input can't be held constant for an optimisation. "
                "Vary it, or optimise a tool/workflow that doesn't require it."
            ).style("color: var(--matofx-error-text);")
            row["unsupported"] = True
        self._rows[key] = row

    def collect(self) -> dict[str, Any]:
        """Gather held values; raise ``ValueError`` listing any still unset."""
        out: dict[str, Any] = {}
        missing: list[str] = []
        for key, row in self._rows.items():
            ftype = row["type"]
            if row.get("unsupported"):
                missing.append(row["label"])
                continue
            if ftype == "material":
                value = str(row["holder"].value or "").strip()
                if not value:
                    missing.append(row["label"])
                else:
                    out[key] = value
                continue
            raw = row["widget"].value
            if ftype in ("float", "integer"):
                if raw is None:
                    missing.append(row["label"])
                    continue
                out[key] = snap_int(raw) if ftype == "integer" else float(raw)
            elif ftype == "select":
                if raw in (None, ""):
                    missing.append(row["label"])
                    continue
                out[key] = raw
            elif ftype == "multiselect":
                values = list(raw or [])
                if not values:
                    missing.append(row["label"])
                    continue
                out[key] = values
            elif ftype == "boolean":
                out[key] = bool(raw)
            elif ftype == "string":
                text = str(raw or "").strip()
                if not text:
                    missing.append(row["label"])
                    continue
                out[key] = text
        if missing:
            raise ValueError("Set a value for: " + ", ".join(sorted(missing)) + ".")
        return out


_POPULATION_STRATEGIES: frozenset[str] = frozenset(
    {"nsga2", "nsga3", "moea_d", "ga", "de", "pso", "cmaes"}
)
"""Strategies where a step **is** a generation.

Every one of these builds its pymoo optimiser with ``pop_size=batch_size``, so
the field is not a parallelism knob for them — it sets the population, and a
population of four cannot hold a Pareto front however many generations it runs.
That is worth saying on the form rather than in the source."""

_SUGGESTED_POPULATION = 20
"""Smallest population that reliably holds a front for two or three objectives.

Below roughly this there is too little diversity to recombine; far above it the
budget buys generations nobody gets to run."""


def batch_advice(strategy: str, seed_n: int, max_runs: int) -> tuple[str, int, str]:
    """``(field label, suggested value, note)`` for the runs-per-step field.

    The field means three different things depending on the strategy, and one
    label for all three hides the one that matters most.
    """
    if strategy in _POPULATION_STRATEGIES:
        suggested = max(_SUGGESTED_POPULATION, int(seed_n or 0))
        generations = max(1, int(max_runs or 0) // max(suggested, 1))
        return (
            "Population size (runs per generation)",
            suggested,
            f"This strategy evolves a population, and this field sets its size — "
            f"a step is one generation. {suggested} gives about {generations} "
            f"generation(s) of your {max_runs}-run budget. Below ~15 there is too "
            f"little diversity to hold a front; much above 40 spends the budget on "
            f"a big population rather than on improving it.",
        )
    if strategy == "bo":
        return (
            "Runs per step (proposed together)",
            1,
            "Bayesian optimisation learns from every result, so proposing one run "
            "at a time gives the best answer per run. Raise this only to use idle "
            "compute: the runs in a step are chosen without seeing each other.",
        )
    return (
        "Runs per step",
        min(8, max(1, int(seed_n or 1))),
        "A baseline search with no model to update, so this is purely how many "
        "runs are submitted together — set it to whatever your compute can take.",
    )


class _SeedBudgetControls:
    """Seed (LHS or a saved cohort) + batch size + stopping budget."""

    def __init__(
        self,
        client: ApiClient,
        tool_alias: str,
        strategy_control: Any = None,
    ) -> None:
        self._client = client
        self._tool_alias = tool_alias
        self._cohorts: list[dict[str, Any]] = []
        self._strategy_control = strategy_control
        """The Strategy step's controls, so the batch field can describe itself.

        Optional: the field still works without it, it just cannot say what the
        number means for the chosen strategy."""
        self._batch_note: Any = None
        self._batch_touched = False
        """Set once the user edits the field, after which it is never
        auto-filled again — silently replacing a deliberate number is worse
        than leaving a stale suggestion beside it."""

    async def render(self) -> None:
        # Cohort seeding is only meaningful if the surrogate-family feature
        # has produced cohorts of this tool; load them best-effort. Cohorts are
        # workspace-wide since the Job Cohorts rework ([[project_job_cohorts_rework]]),
        # so we list all and filter to this tool here.
        with contextlib.suppress(ApiError):
            self._cohorts = [
                c
                for c in await self._client.list_cohorts()
                if str(c.get("tool_alias")) == self._tool_alias
            ]

        # Always offer "previous runs" (cohorts-only seed) so the choice is
        # visible even before a cohort exists; the picker + hint guide the
        # user to create one. collect() rejects an empty pick.
        seed_opts = {
            "lhs": "Latin-hypercube sample (fresh start)",
            "cohort": "Start from a cohort of previous runs",
        }
        self._seed_kind = ui.select(seed_opts, value="lhs", label="Initial sample").classes(
            "w-full"
        )

        self._lhs_n = ui.number("Seed runs", value=8, min=1, precision=0).classes("w-48")
        self._lhs_n.bind_visibility_from(self._seed_kind, "value", lambda v: v == "lhs")

        cohort_opts = {int(c["id"]): str(c.get("name") or c["id"]) for c in self._cohorts}
        self._cohort_sel = ui.select(cohort_opts or {0: "—"}, label="Cohort").classes("w-full")
        self._cohort_sel.bind_visibility_from(self._seed_kind, "value", lambda v: v == "cohort")
        if not self._cohorts:
            hint = ui.label(
                "No cohorts of this tool yet — create one from previous runs in the "
                "cohort / surrogate area, then it appears here."
            ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
            hint.bind_visibility_from(self._seed_kind, "value", lambda v: v == "cohort")

        self._batch = ui.number("Runs per step", value=1, min=1, precision=0).classes("w-48")
        self._max_runs = ui.number("Max total runs (budget)", value=40, min=1, precision=0).classes(
            "w-64"
        )
        self._batch_note = ui.label("").style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        self._batch.on_value_change(self._batch_edited)
        # The suggestion depends on the strategy *and* on the two numbers beside
        # it, so every one of them re-derives it.
        self._lhs_n.on_value_change(lambda _e=None: self._refresh_batch_advice())
        self._max_runs.on_value_change(lambda _e=None: self._refresh_batch_advice())
        strategy = getattr(self._strategy_control, "_strategy", None)
        if strategy is not None:
            strategy.on_value_change(lambda _e=None: self._refresh_batch_advice())
        self._refresh_batch_advice()

        self._converge = ui.checkbox("Stop early if the best stops improving")

    def _batch_edited(self, _event: Any = None) -> None:
        """Stop auto-filling once the user has set the number themselves."""
        self._batch_touched = True
        self._refresh_batch_advice()

    def _refresh_batch_advice(self) -> None:
        """Re-label the batch field, re-suggest a value, and explain both.

        A step means something different per strategy — a generation, a batch of
        surrogate proposals, or just parallelism — so one fixed label hid the
        case that matters: for every evolutionary strategy this field **is** the
        population, and the default of 1 gave a population of four.
        """
        if self._batch_note is None:
            return
        strategy = getattr(self._strategy_control, "_strategy", None)
        kind = str(getattr(strategy, "value", "") or "bo")
        seed_n = int(self._lhs_n.value or 0) if self._seed_kind.value == "lhs" else 0
        label, suggested, note = batch_advice(kind, seed_n, int(self._max_runs.value or 0))
        self._batch.props(f'label="{label}"')
        if not self._batch_touched:
            self._batch.value = suggested
            self._batch_note.set_text(note)
            return
        current = int(self._batch.value or 0)
        if current != suggested:
            note = f"{note} Suggested: {suggested} (you have set {current})."
        self._batch_note.set_text(note)

    def collect(self) -> dict[str, Any]:
        if self._max_runs.value is None or int(self._max_runs.value) < 1:
            raise ValueError("Set a positive run budget.")
        if self._batch.value is None or int(self._batch.value) < 1:
            raise ValueError("Runs per step must be at least 1.")

        if self._seed_kind.value == "cohort":
            cohort_id = self._cohort_sel.value
            if not cohort_id:
                raise ValueError("Pick a cohort to seed from.")
            seed: dict[str, Any] = {"kind": "cohort", "cohort_id": int(cohort_id)}
        else:
            n = self._lhs_n.value
            if n is None or int(n) < 1:
                raise ValueError("The seed sample needs at least one run.")
            seed = {"kind": "lhs", "n": int(n)}

        stopping: list[dict[str, Any]] = [{"kind": "max_runs", "value": int(self._max_runs.value)}]
        if self._converge.value:
            stopping.append({"kind": "convergence", "metric": "best_so_far"})

        return {
            "seed": seed,
            "batch_size": int(self._batch.value),
            "stopping_criteria": stopping,
        }


# ----- live view (running / terminal), polled -----


async def _render_pending_approval(
    client: ApiClient, study: dict[str, Any], body: ui.column
) -> None:
    """The compute-cost start gate is open (§14.1) — show the footprint + approve/reject."""
    sid = int(study["id"])
    with section_card("how_to_reg", "Awaiting start approval"):
        ui.label(
            "This study's tool requires sign-off on the compute footprint before the "
            "campaign queues. The nominated approver can approve or reject below."
        ).style("color: var(--matofx-muted-text);")
        try:
            est = await client.get_optimisation_cost_estimate(sid)
            ui.label(
                f"Footprint: up to {est['max_total_children']} runs "
                f"(budget {est['max_runs']} + seed {est['seed_runs']}), "
                f"{est['batch_size']} per step."
            ).style("color: var(--matofx-muted-text); font-size: 0.9rem;")
        except ApiError:
            pass

        reason_in = ui.input("Reason (required to reject)").classes("w-full")

        async def _decide(*, approve: bool) -> None:
            try:
                if approve:
                    await client.approve_optimisation_start(sid)
                else:
                    await client.reject_optimisation_start(sid, reason=str(reason_in.value) or None)
            except ApiError as exc:
                ui.notify(exc.detail or "Couldn't record the decision.", type="negative")
                return
            ui.notify("Start approved." if approve else "Start rejected.", type="positive")
            await _render_detail(client, sid, body)

        with ui.row().classes("w-full items-center gap-2"):
            ui.button(
                "Approve start", icon="check_circle", on_click=lambda: _decide(approve=True)
            ).props("color=primary")
            ui.button("Reject", icon="cancel", on_click=lambda: _decide(approve=False)).props(
                "flat color=negative"
            )
        ui.label("Only the nominated optimisation approver (or an admin) can decide.").style(
            "color: var(--matofx-muted-text); font-size: 0.8rem;"
        )


_OPS = {
    "<=": operator.le,
    "<": operator.lt,
    ">=": operator.ge,
    ">": operator.gt,
    "==": operator.eq,
    "!=": operator.ne,
}


def _fmt(v: Any) -> str:
    """Compact display of a scalar — 4 sig-fig floats, ints as-is."""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return f"{v:.4g}"
    return "—" if v is None else str(v)


def _sig(x: float, n: int = 4) -> float:
    """Round a float to ``n`` significant figures (still a float, so it sorts)."""
    if x == 0 or not math.isfinite(x):
        return x
    return round(x, -math.floor(math.log10(abs(x))) + (n - 1))


def _cell(v: Any) -> Any:
    """Table cell rounded to sensible figures but still numeric (q-table sorts by value)."""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return _sig(v)
    return _fmt(v)


def _unit_suffix(unit: str) -> str:
    """`` (m)`` when a unit is set, else empty — for column headers / hover."""
    return f" ({unit})" if unit else ""


def _batch_label(batch: Any) -> str:
    """Batch number for display — the un-numbered seed batch reads ``seed``."""
    return "seed" if batch in (None, 0) else str(batch)


def _hdr(item: dict[str, Any]) -> str:
    """Column / axis header: descriptor label with its unit, e.g. ``T_max (degC)``."""
    return str(item["label"]) + _unit_suffix(str(item.get("unit", "")))


def _weighted_scores(
    rows: list[dict[str, Any]], objectives: list[dict[str, Any]]
) -> dict[int, float]:
    """Per-run weighted preference score (lower = better) for the Pareto front.

    Each objective is min-max normalised across ``rows``, flipped so lower is
    always better (maximise → ``1 - norm``), scaled by its weight, and summed.
    Lets the user rank a multi-objective trade-off front by their stated
    importance. Runs missing any objective value are omitted.
    """
    spans: list[tuple[float, float]] = []
    for oi in range(len(objectives)):
        vals = [
            float(d["obj_vals"][oi])
            for d in rows
            if isinstance(d["obj_vals"][oi], int | float)
            and not isinstance(d["obj_vals"][oi], bool)
        ]
        spans.append((min(vals), max(vals)) if vals else (0.0, 0.0))
    out: dict[int, float] = {}
    for d in rows:
        total = 0.0
        ok = True
        for oi, obj in enumerate(objectives):
            v = d["obj_vals"][oi]
            if not isinstance(v, int | float) or isinstance(v, bool):
                ok = False
                break
            lo, hi = spans[oi]
            norm = 0.0 if hi == lo else (float(v) - lo) / (hi - lo)
            if obj["goal"] == "maximise":
                norm = 1.0 - norm
            total += float(obj.get("weight", 1.0)) * norm
        if ok:
            out[int(d["job_id"])] = total
    return out


def _wire_row_nav(table: Any, *, prefix: str = "/jobs", key: str = "_job_id") -> None:
    """Click a table row to open ``{prefix}/{row[key]}``; rows must carry ``key``.

    Quasar's ``row-click`` emits ``(evt, row, index)`` — the row dict is
    ``e.args[1]``. The id field rides along on each row without being a visible
    column.
    """

    def _go(e: Any) -> None:
        args = getattr(e, "args", None)
        row = args[1] if isinstance(args, list) and len(args) > 1 else None
        if isinstance(row, dict) and row.get(key):
            ui.navigate.to(f"{prefix}/{int(row[key])}")

    table.on("rowClick", _go)
    table.classes("cursor-pointer")


# Small connecting words + name particles kept lower-case by ``_titlecase``
# (in any position, including first — so "von Mises" reads "von Mises", not
# "Von Mises"). A word the descriptor authored with capitals is preserved
# regardless (it never reaches this set — see ``_titlecase``).
_TITLECASE_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "nor",
        "for",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "as",
        "per",
        "vs",
        "von",
        "van",
        "de",
        "der",
        "den",
        "di",
        "da",
        "del",
        "la",
        "le",
    }
)


def _titlecase(text: str) -> str:
    """Title-case a descriptor label for display, preserving acronyms.

    Capitalises the first letter of each word **only when the word is
    entirely lower-case**, so an already-cased word — an acronym or brand
    name like ``FreeCAD`` / ``CO2`` / ``RPM`` — is left untouched rather
    than flattened by ``str.title()`` (which would give ``Freecad``).
    Small connecting words + name particles (:data:`_TITLECASE_STOP_WORDS`,
    e.g. *of* / *von* / *de*) stay lower-case wherever they appear, so a
    label reads "Finned Heat Sink · Heat Dissipation" and "von Mises Stress"
    even when the descriptor authored its labels lower-case.
    """
    out: list[str] = []
    for w in str(text).split(" "):
        if not w or any(ch.isupper() for ch in w):
            out.append(w)  # empty, or already-cased (acronym / brand) — leave
        elif w in _TITLECASE_STOP_WORDS:
            out.append(w)  # small connecting / particle word — keep lower-case
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def _qualified_label(member_label: str, field_label: str) -> str:
    """``Member · Field`` for a workflow member's variable, both title-cased."""
    return f"{_titlecase(member_label)} · {_titlecase(field_label)}"


def _material_classes(descriptor: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """``input key → material class`` for the descriptor's ``material`` inputs.

    A held material is stored as a bare ``material_version_id``, so the Setup
    card needs the class to look the version's name back up rather than print
    an id at the reader (feedback_use_labels_not_names).
    """
    return {
        f"{prefix}{f['name']}": str(f.get("material_class") or "")
        for f in (descriptor.get("inputs") or [])
        if isinstance(f, dict) and f.get("name") and f.get("type") == "material"
    }


async def _material_version_labels(client: ApiClient, classes: set[str]) -> dict[str, str]:
    """``str(material_version_id) → material name`` across the classes in play.

    Best-effort: the picker only offers each material's latest published
    version, so a study holding an older version simply won't resolve and the
    caller falls back to naming the version id. Matching is by exact version
    id, so a name is never shown against the wrong version.
    """
    out: dict[str, str] = {}
    for material_class in sorted(c for c in classes if c):
        with contextlib.suppress(ApiError):
            for cand in await client.materials_picker(material_class=material_class):
                out[str(cand.get("version_id"))] = str(cand.get("name") or "")
    return out


def _label_maps(
    descriptor: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    """Name → label and name → unit maps for the tool's inputs and outputs."""
    fin = [f for f in (descriptor.get("inputs") or []) if isinstance(f, dict) and f.get("name")]
    fout = [o for o in (descriptor.get("outputs") or []) if isinstance(o, dict) and o.get("name")]
    in_labels = {str(f["name"]): _titlecase(str(f.get("label") or f["name"])) for f in fin}
    out_labels = {str(o["name"]): _titlecase(str(o.get("label") or o["name"])) for o in fout}
    in_units = {str(f["name"]): str(f.get("unit") or "") for f in fin}
    out_units = {str(o["name"]): str(o.get("unit") or "") for o in fout}
    return in_labels, out_labels, in_units, out_units


async def _workflow_label_maps(
    client: ApiClient, workflow_alias: str
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    """Member-qualified label/unit maps for a workflow study (labels-not-names).

    Factors are keyed ``member.input`` (matching the config factor ``name``)
    and objectives by the bare output name (matching
    :func:`_scalar_key_for_response` for a scalar response). Each is also
    registered under the ``member.output`` form so the member-qualified
    response ``name`` resolves too. Labels read ``tool name · field label``.
    """
    in_labels: dict[str, str] = {}
    out_labels: dict[str, str] = {}
    in_units: dict[str, str] = {}
    out_units: dict[str, str] = {}
    workflows = await client.list_workflows()
    wf = next((w for w in workflows if str(w.get("alias")) == workflow_alias), None)
    if wf is None:
        return in_labels, out_labels, in_units, out_units
    for member in (str(a) for a in (wf.get("tool_aliases") or [])):
        with contextlib.suppress(ApiError):
            tool = await client.get_tool(member)
            member_label = str(tool.get("name") or member)
            descriptor = tool.get("descriptor") or {}
            for f in descriptor.get("inputs") or []:
                if isinstance(f, dict) and f.get("name"):
                    name = str(f["name"])
                    in_labels[f"{member}.{name}"] = _qualified_label(
                        member_label, str(f.get("label") or name)
                    )
                    in_units[f"{member}.{name}"] = str(f.get("unit") or "")
            for o in descriptor.get("outputs") or []:
                if isinstance(o, dict) and o.get("name"):
                    name = str(o["name"])
                    label = _qualified_label(member_label, str(o.get("label") or name))
                    out_labels[name] = label
                    out_labels[f"{member}.{name}"] = label
                    unit = str(o.get("unit") or "")
                    out_units[name] = unit
                    out_units[f"{member}.{name}"] = unit
    return in_labels, out_labels, in_units, out_units


async def _workflow_material_classes(client: ApiClient, workflow_alias: str) -> dict[str, str]:
    """``member.input → material class`` for a workflow target's material inputs.

    The member-qualified twin of :func:`_material_classes`; keyed to match the
    ``member.input`` form the config's ``fixed_inputs`` uses.
    """
    out: dict[str, str] = {}
    with contextlib.suppress(ApiError):
        workflows = await client.list_workflows()
        wf = next((w for w in workflows if str(w.get("alias")) == workflow_alias), None)
        if wf is None:
            return out
        for member in (str(a) for a in (wf.get("tool_aliases") or [])):
            with contextlib.suppress(ApiError):
                tool = await client.get_tool(member)
                out.update(_material_classes(tool.get("descriptor") or {}, prefix=f"{member}."))
    return out


def _scalar_key_for_response(resp: dict[str, Any]) -> str:
    """The key a response's value is stored under in a run's scalars.

    A **tool** scalar response keys by its output name (``spec.name``) — what
    the runner's ``_extract_scalars`` writes. A **workflow** scalar response
    keys by ``member.output`` (``spec.member`` + ``spec.name``), matching the
    member-qualified scalars ``WorkflowRunRunner._record_run_result`` writes and
    the runtime's ``response_names``.
    """
    raw_spec = resp.get("spec")
    spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
    if spec.get("kind") == "scalar":
        name = str(spec.get("name") or resp.get("name"))
        member = spec.get("member")
        return f"{member}.{name}" if member else name
    return str(resp.get("name"))


def _objectives_from_config(
    cfg: dict[str, Any], out_labels: dict[str, str], out_units: dict[str, str]
) -> list[dict[str, Any]]:
    """Optimised responses → ``[{key, label, unit, goal}]`` (goal minimise/maximise)."""
    objs: list[dict[str, Any]] = []
    for resp in cfg.get("responses") or []:
        if not isinstance(resp, dict) or resp.get("goal") not in ("minimise", "maximise"):
            continue
        key = _scalar_key_for_response(resp)
        weight = resp.get("weight", 1.0)
        objs.append(
            {
                "key": key,
                "label": out_labels.get(key) or str(resp.get("name") or key),
                "unit": out_units.get(key, ""),
                "goal": str(resp.get("goal")),
                "weight": float(weight) if isinstance(weight, int | float) else 1.0,
            }
        )
    return objs


def _tracked_from_config(
    cfg: dict[str, Any], out_labels: dict[str, str], out_units: dict[str, str]
) -> list[dict[str, Any]]:
    """``goal == "track"`` responses → ``[{key, label, unit}]``.

    "Track only" means *recorded but not chased* — and the recording half was
    missing from the one page where it was declared. The value was collected on
    every run (the runtime's response vector is built from every declared
    response, goal or not) and written to the run's ``study_result`` row, but
    the Runs table, the CSV and the explorer axes were all built from the
    optimised list alone, so a tracked response could be read anywhere except
    the study that asked for it.

    Kept separate from the objectives rather than merged into them: a tracked
    response must never reach the Pareto sort, the weighted score, or the
    best-design ranking. It is context, not a goal.
    """
    tracked: list[dict[str, Any]] = []
    for resp in cfg.get("responses") or []:
        if not isinstance(resp, dict) or resp.get("goal") != "track":
            continue
        key = _scalar_key_for_response(resp)
        tracked.append(
            {
                "key": key,
                "label": out_labels.get(key) or str(resp.get("name") or key),
                "unit": out_units.get(key, ""),
            }
        )
    return tracked


def _factors_from_config(
    cfg: dict[str, Any], in_labels: dict[str, str], in_units: dict[str, str]
) -> list[dict[str, Any]]:
    """Search factors → ``[{key, label, unit, type, min, max, levels}]``.

    ``key`` is the descriptor input name (``member.input`` for a workflow).

    ``levels`` is the factor's declared option list — the ``choices`` the
    wizard saved for a categorical factor — and empty for a numeric one. It is
    what lets the explorer draw a material as a named column rather than
    dropping it: the config has always carried the options, they were simply
    discarded one function short of the plot.

    ``min``/``max`` are the search bounds the wizard saved for a numeric factor
    (``None`` for a categorical one). The Setup card states them so the record
    says what the search was *allowed* to try, not only what it tried.
    """
    facs: list[dict[str, Any]] = []
    for f in cfg.get("factors") or []:
        if isinstance(f, dict) and f.get("name"):
            key = str(f["name"])
            ftype = str(f.get("type") or "")
            categorical = ftype == "categorical"
            choices = f.get("choices") if categorical else None
            facs.append(
                {
                    "key": key,
                    "label": in_labels.get(key) or key,
                    "unit": in_units.get(key, ""),
                    "type": ftype,
                    "min": None if categorical else f.get("min"),
                    "max": None if categorical else f.get("max"),
                    "levels": [str(c) for c in (choices or [])],
                }
            )
    return facs


def _fixed_from_config(
    cfg: dict[str, Any],
    in_labels: dict[str, str],
    in_units: dict[str, str],
    *,
    material_keys: set[str] | None = None,
    material_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Held-constant inputs → ``[{key, label, unit, value}]``, label-ordered.

    These are the required inputs the search is *not* varying: the wizard's
    held-constant step (§3.5) wrote them to ``fixed_inputs`` and every run is
    queued with them underneath its candidate. Reading a finished study they
    are half of what makes the result reproducible — the other half being the
    factor bounds above.
    """
    fixed = cfg.get("fixed_inputs")
    if not isinstance(fixed, dict):
        return []
    material_keys = set(material_keys or ())
    names = material_names or {}
    rows: list[dict[str, Any]] = []
    for key, value in fixed.items():
        k = str(key)
        if k in material_keys:
            # A material rides as its version id; show the name where the
            # picker still offers that version, and always say which version.
            shown = names.get(str(value))
            version = f"material version {_fmt(value)}"
            display = f"{shown} ({version})" if shown else version
        else:
            display = _fmt_held(value)
        rows.append(
            {
                "key": k,
                "label": in_labels.get(k) or k,
                "unit": in_units.get(k, ""),
                "value": value,
                "display": display,
            }
        )
    return sorted(rows, key=lambda r: str(r["label"]).lower())


def _factor_range_text(fac: dict[str, Any]) -> str:
    """A factor's declared search space for display — its bounds, or its levels."""
    levels = list(fac.get("levels") or [])
    if levels:
        return ", ".join(levels)
    lo, hi = fac.get("min"), fac.get("max")
    if lo is None and hi is None:
        return "—"
    return f"{_fmt(lo)} to {_fmt(hi)}"


def _fmt_held(value: Any) -> str:
    """Display a held value — a multiselect input holds a list, not a scalar."""
    if isinstance(value, list | tuple):
        return ", ".join(_fmt(v) for v in value) or "—"
    return _fmt(value)


def _output_constraints_from_config(
    cfg: dict[str, Any], out_labels: dict[str, str]
) -> list[dict[str, Any]]:
    """Output constraints → ``[{key, label, op, value}]`` for feasibility."""
    # response name → scalar key (output name), so a constraint naming a
    # response resolves to the same key the run's scalars use.
    key_by_response = {
        str(r.get("name")): _scalar_key_for_response(r)
        for r in (cfg.get("responses") or [])
        if isinstance(r, dict) and r.get("name")
    }
    cons: list[dict[str, Any]] = []
    for c in cfg.get("constraints") or []:
        if not isinstance(c, dict) or c.get("kind") != "output":
            continue
        resp_name = str(c.get("response"))
        key = key_by_response.get(resp_name, resp_name)
        cons.append(
            {
                "key": key,
                "label": out_labels.get(key) or resp_name,
                "op": str(c.get("op")),
                "value": c.get("value"),
            }
        )
    return cons


def _run_feasible(scalars: dict[str, Any], constraints: list[dict[str, Any]]) -> bool | None:
    """Feasible if every output constraint holds; ``None`` when a value is missing."""
    if not constraints:
        return True
    for c in constraints:
        v = scalars.get(c["key"])
        if not isinstance(v, int | float) or isinstance(v, bool):
            return None
        op = _OPS.get(c["op"])
        if op is None or not isinstance(c["value"], int | float):
            return None
        if not op(float(v), float(c["value"])):
            return False
    return True


def _pareto_indices(points: list[list[float]], senses: list[str]) -> set[int]:
    """Indices of the non-dominated points (each objective in its own sense)."""
    norm = [
        [(-v if s == "maximise" else v) for v, s in zip(p, senses, strict=True)] for p in points
    ]
    front: set[int] = set()
    for i, bi in enumerate(norm):
        dominated = any(
            i != j
            and all(aj[k] <= bi[k] for k in range(len(bi)))
            and any(aj[k] < bi[k] for k in range(len(bi)))
            for j, aj in enumerate(norm)
        )
        if not dominated:
            front.add(i)
    return front


def _derive_runs(
    runs: list[dict[str, Any]],
    objectives: list[dict[str, Any]],
    constraints: list[dict[str, Any]],
    tracked: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Annotate each run with objective + tracked values, feasibility, Pareto flag.

    ``track_vals`` is read from the same ``scalars`` the objectives come from,
    and deliberately takes no part in the Pareto sort below: a tracked response
    is reported, never chased. ``tracked`` defaults to none so a caller that
    predates it keeps working.
    """
    tracked = tracked or []
    derived: list[dict[str, Any]] = []
    for run in runs:
        scalars = run.get("scalars") or {}
        obj_vals = [scalars.get(o["key"]) for o in objectives]
        derived.append(
            {
                **run,
                "obj_vals": obj_vals,
                "track_vals": [scalars.get(t["key"]) for t in tracked],
                "feasible": _run_feasible(scalars, constraints),
                "on_front": False,
            }
        )
    # Pareto over runs that completed, are feasible, and have every objective.
    senses = [o["goal"] for o in objectives]
    eligible = [
        i
        for i, d in enumerate(derived)
        if d["state"] == "complete"
        and d["feasible"] is True
        and all(isinstance(v, int | float) and not isinstance(v, bool) for v in d["obj_vals"])
    ]
    if objectives and eligible:
        pts = [[float(v) for v in derived[i]["obj_vals"]] for i in eligible]
        for local_idx in _pareto_indices(pts, senses):
            derived[eligible[local_idx]]["on_front"] = True
    return derived


def _render_setup_outcome(
    cfg: dict[str, Any],
    conv: dict[str, Any],
    derived: list[dict[str, Any]],
    objectives: list[dict[str, Any]],
    factors: list[dict[str, Any]],
    constraints: list[dict[str, Any]],
    fixed: list[dict[str, Any]] | None = None,
) -> None:
    """Setup + outcome summary card (labels, not names).

    The Setup half states the *whole* search definition — each factor with the
    limits it was allowed to move between, and every input held constant with
    its value — so the card alone says what was run without reopening the
    wizard.
    """
    muted = "color: var(--matofx-muted-text);"
    with section_card("summarize", "Setup & outcome"), ui.row().classes("w-full gap-8 items-start"):
        with ui.column().classes("gap-1"):
            ui.label("Setup").style("font-weight: 600;")
            strat = str(cfg.get("strategy") or "")
            ui.label(f"Strategy: {_STRATEGY_OPTIONS.get(strat, strat or '—')}").style(muted)
            obj_txt = ", ".join(f"{_hdr(o)} ({o['goal']})" for o in objectives) or "—"
            ui.label(f"Objectives: {obj_txt}").style(muted)
            if constraints:
                con_txt = "; ".join(f"{_hdr(c)} {c['op']} {_fmt(c['value'])}" for c in constraints)
                ui.label(f"Constraints: {con_txt}").style(muted)
            budget = next(
                (
                    int(s.get("value") or 0)
                    for s in (cfg.get("stopping_criteria") or [])
                    if isinstance(s, dict) and s.get("kind") == "max_runs"
                ),
                None,
            )
            ui.label(f"Run budget: {budget if budget is not None else '—'}").style(muted)

            # Varied factors, each with the limits the search stayed inside —
            # a categorical factor lists its levels instead of a range.
            ui.label(f"Factors varied ({len(factors)})").style("font-weight: 600; margin-top: 8px;")
            if not factors:
                ui.label("—").style(muted)
            for fac in factors:
                ui.label(f"{_hdr(fac)}: {_factor_range_text(fac)}").style(muted)

            # Everything the search did NOT move, at the value every run used.
            held = list(fixed or [])
            ui.label(f"Held constant ({len(held)})").style("font-weight: 600; margin-top: 8px;")
            if not held:
                ui.label("Nothing held constant — every required input is being varied.").style(
                    muted
                )
            for row in held:
                ui.label(f"{_hdr(row)}: {row['display']}").style(muted)
        with ui.column().classes("gap-1"):
            ui.label("Outcome").style("font-weight: 600;")
            total = len(derived)
            feasible = sum(1 for d in derived if d["feasible"] is True)
            failed = sum(1 for d in derived if d["state"] == "failed")
            front = sum(1 for d in derived if d["on_front"])
            ui.label(f"Runs: {total} ({feasible} feasible, {failed} failed)").style(
                "color: var(--matofx-muted-text);"
            )
            # Candidates the optimiser proposed but dropped before running
            # because they broke an input rule (a validity rule or a
            # formula-driven / cross-field bound). Summed across the trace.
            skipped = sum(int(p.get("n_dropped") or 0) for p in (conv.get("trace") or []))
            if skipped:
                ui.label(f"Skipped: {skipped} candidate(s) that broke an input rule").style(
                    "color: var(--matofx-muted-text);"
                )
            stop = conv.get("stop_reason") or conv.get("state")
            ui.label(f"Status: {str(stop).replace('_', ' ')}").style(
                "color: var(--matofx-muted-text);"
            )
            if len(objectives) >= 2:
                ui.label(f"Pareto front: {front} design(s)").style(
                    "color: var(--matofx-muted-text);"
                )
            # Best-so-far per objective over feasible completed runs.
            for oi, obj in enumerate(objectives):
                vals = [
                    float(d["obj_vals"][oi])
                    for d in derived
                    if d["feasible"] is True
                    and isinstance(d["obj_vals"][oi], int | float)
                    and not isinstance(d["obj_vals"][oi], bool)
                ]
                if vals:
                    best = min(vals) if obj["goal"] == "minimise" else max(vals)
                    ui.label(f"Best {_hdr(obj)}: {_fmt(best)}").style(
                        "color: var(--matofx-muted-text);"
                    )


def _render_runs_table(
    derived: list[dict[str, Any]],
    objectives: list[dict[str, Any]],
    factors: list[dict[str, Any]],
    tracked: list[dict[str, Any]] | None = None,
) -> None:
    """Per-run table: objective + tracked values, factor values, feasibility, Pareto.

    Tracked columns sit **after** the objectives and carry no ★ and no goal
    arrow, so the table still reads as "these are the goals, these came along
    for the ride". They were absent entirely before — the study page was the one
    place a Track-only response could not be read.
    """
    tracked = tracked or []
    with section_card("table_rows", "Runs"):
        with ui.row().classes("w-full items-center"):
            ui.space()

            def _download_csv() -> None:
                headers = (
                    ["run", "batch", "state", "feasible", "on_front"]
                    + [_hdr(o) for o in objectives]
                    + [_hdr(t) for t in tracked]
                    + [_hdr(f) for f in factors]
                )
                lines = [",".join(f'"{h}"' for h in headers)]
                for d in derived:
                    cells = [
                        str(d["job_id"]),
                        _batch_label(d.get("batch")),
                        d["state"],
                        "" if d["feasible"] is None else ("yes" if d["feasible"] else "no"),
                        "yes" if d["on_front"] else "no",
                    ]
                    cells += [_fmt(v) for v in d["obj_vals"]]
                    cells += [_fmt(v) for v in (d.get("track_vals") or [])]
                    cells += [_fmt((d.get("inputs") or {}).get(f["key"])) for f in factors]
                    lines.append(",".join(cells))
                ui.download.content(("\n".join(lines)).encode("utf-8"), "optimisation_runs.csv")

            ui.button("Download CSV", icon="download", on_click=_download_csv).props("flat dense")

        if not derived:
            ui.label("No runs yet.").style("color: var(--matofx-muted-text);")
            return
        columns: list[dict[str, Any]] = [
            {"name": "run", "label": "Run", "field": "run", "sortable": True},
            {"name": "batch", "label": "Batch", "field": "batch", "sortable": True},
            {"name": "state", "label": "State", "field": "state", "sortable": True},
        ]
        for oi, obj in enumerate(objectives):
            columns.append(
                {"name": f"o{oi}", "label": _hdr(obj), "field": f"o{oi}", "sortable": True}
            )
        for ti, trk in enumerate(tracked):
            columns.append(
                {"name": f"t{ti}", "label": _hdr(trk), "field": f"t{ti}", "sortable": True}
            )
        for fi, fac in enumerate(factors):
            columns.append(
                {"name": f"f{fi}", "label": _hdr(fac), "field": f"f{fi}", "sortable": True}
            )
        columns.append({"name": "feasible", "label": "Feasible", "field": "feasible"})
        columns.append({"name": "front", "label": "On front", "field": "front"})
        rows: list[dict[str, Any]] = []
        for d in derived:
            row: dict[str, Any] = {
                "run": f"#{d['job_id']}",
                "_job_id": d["job_id"],
                "batch": _batch_label(d.get("batch")),
                "state": d["state"],
                "feasible": "—" if d["feasible"] is None else ("✓" if d["feasible"] else "✗"),
                "front": "★" if d["on_front"] else "",
            }
            for oi in range(len(objectives)):
                row[f"o{oi}"] = _cell(d["obj_vals"][oi])
            for ti in range(len(tracked)):
                row[f"t{ti}"] = _cell((d.get("track_vals") or [None] * len(tracked))[ti])
            for fi, fac in enumerate(factors):
                row[f"f{fi}"] = _cell((d.get("inputs") or {}).get(fac["key"]))
            rows.append(row)
        ui.label("Click a row to open that run.").style(
            "color: var(--matofx-muted-text); font-size: 0.8rem;"
        )
        # Sortable headers (click to toggle asc/desc) + paginated so long
        # campaigns don't render hundreds of rows at once.
        table = (
            ui.table(columns=columns, rows=rows, row_key="run", pagination=15)
            .classes("w-full")
            .props("dense flat bordered")
        )
        _wire_row_nav(table)


def _render_objective_scatter(
    derived: list[dict[str, Any]],
    objectives: list[dict[str, Any]],
    factors: list[dict[str, Any]],
    out_labels: dict[str, str],
    out_units: dict[str, str],
    *,
    prefs: dict[str, Any],
    tracked: list[dict[str, Any]] | None = None,
) -> None:
    """Scatter of any axes — X/Y, optional Z (3D), and an independent colour axis.

    Each axis (and the colour) can be any objective, tracked response, or
    factor; colour also offers batch (the optimiser iteration — shows search
    progress) and feasibility. Axis/hover labels carry units. Hover = run /
    inputs / outputs / feasibility.

    Tracked responses get their own picker section rather than joining the
    objectives: plotting a goal against something the search never chased is
    the whole point of declaring it Track only, and it is a different kind of
    axis from a goal.
    """
    tracked = tracked or []
    axes = [{"kind": "obj", "idx": i, "label": _hdr(o)} for i, o in enumerate(objectives)]
    axes += [{"kind": "trk", "idx": i, "label": _hdr(t)} for i, t in enumerate(tracked)]
    axes += [{"kind": "fac", "idx": i, "label": _hdr(f)} for i, f in enumerate(factors)]
    if len(axes) < 2:
        return

    def _axis_value(d: dict[str, Any], axis: dict[str, Any]) -> Any:
        if axis["kind"] == "obj":
            return d["obj_vals"][axis["idx"]]
        if axis["kind"] == "trk":
            vals = d.get("track_vals") or []
            idx = axis["idx"]
            return vals[idx] if idx < len(vals) else None
        return (d.get("inputs") or {}).get(factors[axis["idx"]]["key"])

    def _num(v: Any) -> float | None:
        return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None

    def _column(axis: dict[str, Any]) -> list[str]:
        """This axis's value for every run, as text, missing values named."""
        return [UNSET_LEVEL if (v := _axis_value(d, axis)) is None else str(v) for d in derived]

    def _levels(axis: dict[str, Any] | None) -> list[str] | None:
        """Tick labels if this axis is a categorical factor, else ``None``."""
        if axis is None or axis["kind"] != "fac":
            return None
        declared = list(factors[axis["idx"]].get("levels") or [])
        if not declared:
            return None
        return axis_categories(declared, _column(axis))

    def _coords(
        axis: dict[str, Any], cats: list[str] | None, *, jitter: bool
    ) -> list[float | None]:
        """Plot position per run — the number itself, or the level's column."""
        if cats is None:
            return [_num(_axis_value(d, axis)) for d in derived]
        return category_positions(cats, _column(axis), jitter=jitter)

    # Objectives and factors in one undivided list gave no clue where the
    # goals ended and the search variables began — the same complaint the
    # study Explore pickers drew, so the same treatment.
    def _by_kind(kind: str, key: Any = lambda i: i) -> dict[Any, str]:
        return {key(i): str(a["label"]) for i, a in enumerate(axes) if a["kind"] == kind}

    axis_opts = sectioned_options(
        ("OBJECTIVES", _by_kind("obj")),
        ("TRACKED", _by_kind("trk")),
        ("FACTORS", _by_kind("fac")),
    )
    z_opts = {-1: "(none — 2D)", **axis_opts}
    colour_opts = {
        "_batch": "Batch (iteration)",
        "_feasible": "Feasibility",
        **sectioned_options(
            ("OBJECTIVES", _by_kind("obj", lambda i: f"a{i}")),
            ("TRACKED", _by_kind("trk", lambda i: f"a{i}")),
            ("FACTORS", _by_kind("fac", lambda i: f"a{i}")),
        ),
    }
    default_x = 0
    _y = 1 if len(objectives) >= 2 else min(len(objectives), len(axes) - 1)
    default_y = min(_y, len(axes) - 1)
    default_z = 2 if len(objectives) >= 3 else -1

    # Keep the user's axis / colour picks across a Refresh (or poll) of this
    # page — a rebuild restores them from ``prefs`` instead of snapping back to
    # the defaults. Stored picks are validated against the current options so a
    # stale value can never break the plot.
    def _pick(key: str, options: dict[Any, Any], default: Any) -> Any:
        value = prefs.get(key, default)
        if is_section_header(value) or value not in options:
            return default
        return value

    with section_card("scatter_plot", "Design-space explorer"):
        ui.label(
            "Batch = the optimiser iteration a run was proposed in (the seed sample is the "
            "first); colour by batch to watch the search progress. Infeasible runs show as ✕; "
            "Pareto-optimal designs are ringed (on any axes). A factor with options (a "
            "material, a solver) plots as one named column per option — and as a stepped "
            "colour bar — including options the search never tried."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        with ui.row().classes("items-center gap-3 flex-wrap"):
            x_sel = ui.select(
                axis_opts, value=_pick("x", axis_opts, default_x), label="X axis"
            ).classes("w-44")
            y_sel = ui.select(
                axis_opts, value=_pick("y", axis_opts, default_y), label="Y axis"
            ).classes("w-44")
            z_sel = ui.select(z_opts, value=_pick("z", z_opts, default_z), label="Z axis").classes(
                "w-44"
            )
            c_sel = ui.select(
                colour_opts, value=_pick("c", colour_opts, "_batch"), label="Colour"
            ).classes("w-44")
        plot_box = ui.column().classes("w-full")

        def _colour_for(d: dict[str, Any]) -> float:
            sel = str(c_sel.value)
            if sel == "_feasible":
                return 1.0 if d["feasible"] is True else (0.0 if d["feasible"] is False else 0.5)
            if sel.startswith("a"):
                return _num(_axis_value(d, axes[int(sel[1:])])) or 0.0
            return float(d.get("batch") or 0)  # _batch default

        def _colour_title() -> str:
            sel = str(c_sel.value)
            if sel == "_feasible":
                return "Feasible"
            if sel.startswith("a"):
                return str(axes[int(sel[1:])]["label"])
            return "Batch"

        def _draw() -> None:
            plot_box.clear()
            # A heading snaps back before this runs; belt and braces so a
            # stale stored pick can never reach ``int()``.
            if any(is_section_header(s.value) for s in (x_sel, y_sel, z_sel)):
                return
            xa, ya = axes[int(x_sel.value)], axes[int(y_sel.value)]
            zi = int(z_sel.value)
            za = axes[zi] if zi >= 0 else None
            cat_x, cat_y, cat_z = _levels(xa), _levels(ya), _levels(za)
            # Nudge runs apart only when no continuous axis is left to spread
            # them — otherwise a marker would sit where it does not belong.
            positional = [cat_x, cat_y] + ([cat_z] if za is not None else [])
            nudge = all(c is not None for c in positional)
            px, py = _coords(xa, cat_x, jitter=nudge), _coords(ya, cat_y, jitter=nudge)
            pz = _coords(za, cat_z, jitter=nudge) if za is not None else None

            csel = str(c_sel.value)
            c_axis = axes[int(csel[1:])] if csel.startswith("a") else None
            cat_c = _levels(c_axis)
            # Colour never nudges: a band is a band, and the bar names it.
            pc = (
                category_positions(cat_c, _column(c_axis))
                if cat_c is not None and c_axis is not None
                else None
            )

            xs: list[float] = []
            ys: list[float] = []
            zs: list[float] = []
            colours: list[float] = []
            symbols: list[str] = []
            custom: list[list[str]] = []
            pareto: list[tuple[float, float, float]] = []
            for i, d in enumerate(derived):
                xv, yv = px[i], py[i]
                if xv is None or yv is None:
                    continue
                zv = pz[i] if pz is not None else 0.0
                if pz is not None and zv is None:
                    continue
                xs.append(xv)
                ys.append(yv)
                zs.append(zv if zv is not None else 0.0)
                colours.append((pc[i] or 0.0) if pc is not None else _colour_for(d))
                symbols.append("x" if d["feasible"] is False else "circle")
                indent = "&nbsp;&nbsp;&nbsp;&nbsp;"
                feas = "—" if d["feasible"] is None else ("yes" if d["feasible"] else "no")
                inputs_block = (
                    "<br>".join(
                        f"{indent}{f['label']}{_unit_suffix(f.get('unit', ''))}: "
                        f"{_fmt((d.get('inputs') or {}).get(f['key']))}"
                        for f in factors
                    )
                    or f"{indent}—"
                )
                run_scalars = d.get("scalars") or {}
                outputs_block = (
                    "<br>".join(
                        f"{indent}{out_labels.get(k, k)}{_unit_suffix(out_units.get(k, ''))}: "
                        f"{_fmt(v)}"
                        for k, v in run_scalars.items()
                    )
                    or f"{indent}—"
                )
                header = f"#{d['job_id']} — batch {_batch_label(d.get('batch'))} — feasible: {feas}"
                custom.append([header, inputs_block, outputs_block])
                # Pareto membership is a property of the run (decided in
                # objective space), not of the projection — so ring on-front
                # runs wherever they plot, even with a factor on an axis.
                if d["on_front"]:
                    pareto.append((xv, yv, zv if zv is not None else 0.0))

            with plot_box:
                if not xs:
                    ui.label("No runs to plot on these axes yet.").style(
                        "color: var(--matofx-muted-text);"
                    )
                    return
                is_feas_colour = csel == "_feasible"
                colorbar: dict[str, Any] = {"title": _colour_title(), "x": 1.02}
                if is_feas_colour:
                    colorbar |= {"tickvals": [0, 0.5, 1], "ticktext": ["no", "unknown", "yes"]}
                marker: dict[str, Any] = {
                    "size": 6 if za is not None else 11,
                    "color": colours,
                    "colorscale": "Viridis",
                    "colorbar": colorbar,
                    "symbol": symbols,
                    "line": {"width": 1, "color": "#444"},
                }
                if is_feas_colour:
                    # Pin the colour scale to the feasibility range so the bar
                    # always reads 0..1 regardless of which values are present.
                    marker["cmin"] = 0
                    marker["cmax"] = 1
                elif cat_c is not None:
                    # One flat band per level, each tick naming its own level,
                    # so two materials read as two colours and not as 0.
                    marker["colorscale"] = stepped_colorscale(category_colours(len(cat_c)))
                    marker["cmin"], marker["cmax"] = -0.5, len(cat_c) - 0.5
                    colorbar |= {
                        "tickmode": "array",
                        "tickvals": list(range(len(cat_c))),
                        "ticktext": list(cat_c),
                    }
                hover = (
                    "%{customdata[0]}<br>"
                    "Inputs:<br>%{customdata[1]}<br>"
                    "Outputs:<br>%{customdata[2]}<extra></extra>"
                )
                fig = go.Figure()
                if za is not None:
                    fig.add_trace(
                        go.Scatter3d(
                            x=xs,
                            y=ys,
                            z=zs,
                            mode="markers",
                            marker=marker,
                            customdata=custom,
                            hovertemplate=hover,
                            name="Runs",
                        )
                    )
                    if pareto:
                        fig.add_trace(
                            go.Scatter3d(
                                x=[p[0] for p in pareto],
                                y=[p[1] for p in pareto],
                                z=[p[2] for p in pareto],
                                mode="markers",
                                marker={"size": 9, "symbol": "circle-open", "color": "#d62728"},
                                name="Pareto front",
                                hoverinfo="skip",
                            )
                        )
                    fig.update_layout(
                        scene={
                            "xaxis": {
                                "title": xa["label"],
                                **(category_axis(cat_x) if cat_x else {}),
                            },
                            "yaxis": {
                                "title": ya["label"],
                                **(category_axis(cat_y) if cat_y else {}),
                            },
                            "zaxis": {
                                "title": za["label"],
                                **(category_axis(cat_z) if cat_z else {}),
                            },
                        }
                    )
                else:
                    fig.add_trace(
                        go.Scatter(
                            x=xs,
                            y=ys,
                            mode="markers",
                            marker=marker,
                            customdata=custom,
                            hovertemplate=hover,
                            name="Runs",
                        )
                    )
                    if pareto:
                        fig.add_trace(
                            go.Scatter(
                                x=[p[0] for p in pareto],
                                y=[p[1] for p in pareto],
                                mode="markers",
                                marker={
                                    "size": 18,
                                    "symbol": "circle-open",
                                    "color": "#d62728",
                                    "line": {"width": 2},
                                },
                                name="Pareto front",
                                hoverinfo="skip",
                            )
                        )
                    fig.update_layout(xaxis_title=xa["label"], yaxis_title=ya["label"])
                    if cat_x:
                        fig.update_xaxes(**category_axis(cat_x))
                    if cat_y:
                        fig.update_yaxes(**category_axis(cat_y))
                # Legend horizontal along the top so it never collides with the
                # colour bar pinned to the right edge (the reported overlap).
                fig.update_layout(
                    height=480 if za is not None else 440,
                    margin={"l": 60, "r": 90, "t": 44, "b": 50},
                    legend={
                        "orientation": "h",
                        "yanchor": "bottom",
                        "y": 1.02,
                        "xanchor": "left",
                        "x": 0,
                    },
                )
                ui.plotly(themed_plotly(fig)).classes("w-full")

        def _save_and_draw() -> None:
            prefs["x"], prefs["y"] = x_sel.value, y_sel.value
            prefs["z"], prefs["c"] = z_sel.value, c_sel.value
            _draw()

        for _sel in (x_sel, y_sel, z_sel, c_sel):
            guard_section_headers(_sel, _save_and_draw)
        _draw()


async def _render_live(
    client: ApiClient, study: dict[str, Any], body: ui.column, state_chip_box: ui.row
) -> None:
    """Convergence trace + current best + controls, polled while running."""
    sid = int(study["id"])
    strategy = str((study.get("config_json") or {}).get("strategy") or "")

    cfg = dict(study.get("config_json") or {})

    # Label + unit maps from the tool descriptor (labels-not-names,
    # feedback_use_labels_not_names). Best-effort: workflow targets or a load
    # failure fall back to raw names / no units.
    in_labels: dict[str, str] = {}
    out_labels: dict[str, str] = {}
    in_units: dict[str, str] = {}
    out_units: dict[str, str] = {}
    material_classes: dict[str, str] = {}
    tool_label = str(study.get("tool_alias") or study.get("workflow_alias") or "—")
    if str(study.get("target_kind")) == "workflow" and study.get("workflow_alias"):
        wf_alias = str(study.get("workflow_alias"))
        with contextlib.suppress(ApiError):
            workflows = await client.list_workflows()
            wf = next((w for w in workflows if str(w.get("alias")) == wf_alias), None)
            if wf is not None:
                tool_label = str(wf.get("name") or tool_label)
        in_labels, out_labels, in_units, out_units = await _workflow_label_maps(client, wf_alias)
        material_classes = await _workflow_material_classes(client, wf_alias)
    elif study.get("tool_alias"):
        with contextlib.suppress(ApiError):
            tool = await client.get_tool(str(study.get("tool_alias")))
            tool_label = str(tool.get("name") or tool_label)
            in_labels, out_labels, in_units, out_units = _label_maps(tool.get("descriptor") or {})
            material_classes = _material_classes(tool.get("descriptor") or {})
    objectives = _objectives_from_config(cfg, out_labels, out_units)
    # Responses the study declared but never chased (§3.2 ``goal: track``).
    # Reported alongside the goals everywhere a run is listed or plotted.
    tracked = _tracked_from_config(cfg, out_labels, out_units)
    factors = _factors_from_config(cfg, in_labels, in_units)
    fixed = _fixed_from_config(
        cfg,
        in_labels,
        in_units,
        material_keys=set(material_classes),
        material_names=await _material_version_labels(client, set(material_classes.values())),
    )
    constraints = _output_constraints_from_config(cfg, out_labels)
    # Weighted-sum study (single-objective strategy over several objectives):
    # the search drives one scalar, so the UI treats it as single-objective for
    # convergence + ranks the results by the weighted score rather than a front.
    is_ws = is_weighted_sum(cfg)

    target_word = "Workflow" if str(study.get("target_kind")) == "workflow" else "Tool"
    with ui.row().classes("w-full items-center gap-3"):
        ui.label(f"{target_word}: {tool_label}").style("color: var(--matofx-muted-text);")
        ui.label(f"Strategy: {_STRATEGY_OPTIONS.get(strategy, strategy)}").style(
            "color: var(--matofx-muted-text);"
        )
        stop_reason = study.get("stop_reason")
        if stop_reason:
            ui.label(f"Stopped: {str(stop_reason).replace('_', ' ')}").style(
                "color: var(--matofx-muted-text);"
            )
        ui.label(f"Batch {study.get('last_batch_number') or 0}").style(
            "color: var(--matofx-muted-text);"
        )

    # The campaign has finished but the tool gates results release: make that
    # explicit so the page doesn't just look "stuck" (it no longer polls).
    if str(study.get("state")) == "pending_results_approval":
        with (
            ui.row()
            .classes("w-full items-center gap-2 q-pa-sm")
            .style("background: var(--matofx-warning-bg, #fff3e0); border-radius: 6px;")
        ):
            ui.icon("hourglass_top").style("color: #b36b00;")
            ui.label(
                "All runs finished — the results are awaiting approval before the study "
                "completes. An eligible approver releases them from the Approvals area."
            ).style("color: #8a5a00;")

    # Auto-generated results summary (OUTPUTS/designs/auto-summaries.md).
    # Renders only once the study is terminal (the card 404s while running);
    # best-effort, so a failure here never blocks the live trace below.
    from matofx.ui._summary_card import render_summary_card  # noqa: PLC0415

    await render_summary_card(client, "optimisation_study", sid)

    # Two zones (feedback_studies_results_tab_no_teardown):
    #  - live_area: lightweight read-only cards (summary + convergence), safe to
    #    rebuild every poll.
    #  - explorer_area: the heavy INTERACTIVE sections (scatter with axis pickers,
    #    sortable/paginated tables) — rendered once and only re-drawn on an
    #    explicit Refresh, so a 3 s poll never resets your axis picks, sort, or
    #    scroll position. The poll surfaces a "new runs" hint instead.
    live_area = ui.column().classes("w-full gap-4")

    async def _fetch() -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        try:
            conv = await client.get_optimisation_convergence(sid)
        except ApiError as exc:
            live_area.clear()
            with live_area:
                ui.label(f"Couldn't load the trace: {exc.detail}").style(
                    "color: var(--matofx-error-text);"
                )
            return None, []
        try:
            runs = (await client.get_optimisation_run_results(sid)).get("runs") or []
        except ApiError:
            runs = []
        return conv, runs

    def _render_live_area(conv: dict[str, Any], derived: list[dict[str, Any]]) -> None:
        live_area.clear()
        with live_area:
            try:
                _render_setup_outcome(cfg, conv, derived, objectives, factors, constraints, fixed)
                _render_convergence_plot(
                    conv, multi=len(objectives) >= 2 and not is_ws, weighted_sum=is_ws
                )
            except Exception:
                _log.exception("Optimisation live render failed for study %s", sid)
                ui.label("Couldn't render the live trace — see the server log.").style(
                    "color: var(--matofx-error-text);"
                )

    # Controls live in their own container so the poll can refresh them on
    # terminal WITHOUT tearing down the body.
    controls = ui.row().classes("w-full items-center gap-2")

    def _build_controls(current_state: str) -> None:
        controls.clear()
        with controls:
            if current_state == "running":

                async def _halt() -> None:
                    try:
                        await client.halt_optimisation_study(sid)
                    except ApiError as exc:
                        ui.notify(exc.detail, type="negative")
                        return
                    ui.notify(
                        "Halt requested — the study stops at the next batch.", type="positive"
                    )

                ui.button("Halt", icon="stop_circle", on_click=_halt).props("flat color=negative")

            # A completed study can be continued: raise the run budget and it
            # resumes from its persisted state (more batches, same search).
            if current_state == "complete" and not study.get("archived_at"):
                _orig_budget = next(
                    (
                        int(c["value"])
                        for c in (cfg.get("stopping_criteria") or [])
                        if isinstance(c, dict) and c.get("kind") == "max_runs"
                    ),
                    20,
                )
                with ui.row().classes("items-center gap-2"):
                    more = ui.number("Add runs", value=_orig_budget, min=1, precision=0).classes(
                        "w-28"
                    )

                    async def _continue(more: Any = more) -> None:
                        runs = int(more.value or 0)
                        if runs < 1:
                            ui.notify("Add at least one run to continue.", type="warning")
                            return
                        try:
                            await client.continue_optimisation_study(sid, additional_runs=runs)
                        except ApiError as exc:
                            ui.notify(exc.detail or "Couldn't continue the study.", type="negative")
                            return
                        ui.notify(f"Continuing — {runs} more run(s) added.", type="positive")
                        await _render_detail(client, sid, body)

                    ui.button("Continue", icon="play_arrow", on_click=_continue).props(
                        "flat color=primary"
                    )

            if strategy == "bo":
                ui.button(
                    "Publish surrogate",
                    icon="publish",
                    on_click=lambda: _open_publish_dialog(client, sid),
                ).props("flat color=primary")

            if not study.get("archived_at"):

                async def _archive() -> None:
                    try:
                        await client.archive_optimisation_study(sid)
                    except ApiError as exc:
                        ui.notify(exc.detail, type="negative")
                        return
                    ui.notify("Study archived.", type="positive")
                    await _render_detail(client, sid, body)

                ui.button("Archive", icon="archive", on_click=_archive).props("flat")

    _build_controls(str(study.get("state")))

    explorer_state: dict[str, int] = {"n": -1}
    # Survives Refresh/poll rebuilds so the scatter keeps the user's axis/colour
    # picks (and any future explorer preferences) instead of resetting.
    scatter_prefs: dict[str, Any] = {}
    with ui.row().classes("w-full items-center gap-3"):
        # ``_refresh_explorer`` is defined below; the lambda defers the lookup.
        ui.button(
            "Refresh results",
            icon="refresh",
            on_click=lambda: _refresh_explorer(),  # noqa: PLW0108 - deferred lookup
        ).props("flat dense")
        refresh_hint = ui.label("").style("color: #b36b00; font-size: 0.85rem;")
    explorer_area = ui.column().classes("w-full gap-4")

    def _render_explorer(conv: dict[str, Any], derived: list[dict[str, Any]]) -> None:
        explorer_area.clear()
        refresh_hint.set_text("")
        explorer_state["n"] = len(derived)
        with explorer_area:
            try:
                if objectives:
                    _render_objective_scatter(
                        derived,
                        objectives,
                        factors,
                        out_labels,
                        out_units,
                        prefs=scatter_prefs,
                        tracked=tracked,
                    )
                _render_runs_table(derived, objectives, factors, tracked)
                if objectives:
                    _render_best_designs(
                        derived, objectives, factors, weighted_sum=is_ws, tracked=tracked
                    )
                else:
                    _render_current_best(conv, {**in_labels, **out_labels})
            except Exception:
                _log.exception("Optimisation explorer render failed for study %s", sid)
                ui.label("Couldn't render the results — see the server log.").style(
                    "color: var(--matofx-error-text);"
                )

    async def _refresh_explorer() -> None:
        conv2, runs2 = await _fetch()
        if conv2 is None:
            return
        derived2 = _derive_runs(runs2, objectives, constraints, tracked)
        _render_live_area(conv2, derived2)
        _render_explorer(conv2, derived2)

    # Sensitivity (MOP/CoP) — available for any surrogate (`bo`) study on a tool
    # target (it builds the metamodel CoP needs), run on demand. The mop.enabled
    # flag only controls auto-freezing low-CoP factors during the search.
    if strategy == "bo" and str(study.get("target_kind")) != "workflow":
        _render_mop_card(client, sid, study, in_labels)

    # Initial render of both zones.
    conv, runs = await _fetch()
    if conv is not None:
        derived0 = _derive_runs(runs, objectives, constraints, tracked)
        _render_live_area(conv, derived0)
        _render_explorer(conv, derived0)

    # Poll while the loop still owns the study; settled traces are final.
    if conv is not None and str(conv.get("state")) not in _SETTLED_STATES:

        async def _poll() -> None:
            fresh, runs_f = await _fetch()
            if fresh is None:
                return
            derived_f = _derive_runs(runs_f, objectives, constraints, tracked)
            _render_live_area(fresh, derived_f)
            # New runs since the explorer was last drawn? Hint, don't redraw —
            # redrawing would reset the user's axis picks / sort / scroll.
            if len(derived_f) != explorer_state["n"]:
                delta = len(derived_f) - explorer_state["n"]
                refresh_hint.set_text(
                    f"{delta} new run(s) — click Refresh results to update the plots & tables."
                    if delta > 0
                    else "Results updated — click Refresh results."
                )
            if str(fresh.get("state")) in _SETTLED_STATES:
                # Settle on the final results and stop polling. Flip the header
                # chip (Running → Complete / Pending results approval) in place
                # so it no longer needs a navigate-away-and-back to update.
                state_chip_box.clear()
                with state_chip_box:
                    _state_chip(str(fresh.get("state")))
                _render_explorer(fresh, derived_f)
                _build_controls(str(fresh.get("state")))
                _timer.cancel()

        _timer = ui.timer(_REFRESH_INTERVAL_S, _poll)


def _render_convergence_plot(
    conv: dict[str, Any], multi: bool = False, weighted_sum: bool = False
) -> None:
    # The trace metric is: the Pareto-front hypervolume for a true multi-
    # objective study, the weighted-sum scalar for a weighted-sum study, else
    # the single best-so-far (runtime ``_compute_metrics``). Label to match.
    metric_label = (
        "Weighted objective"
        if weighted_sum
        else ("Hypervolume" if multi else "Best objective value")
    )
    with section_card("trending_up", "Convergence — hypervolume" if multi else "Convergence"):
        if multi:
            ui.label(
                "Hypervolume of the Pareto front vs batch — higher means a wider, "
                "better trade-off surface; a plateau suggests the front has converged."
            ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        elif weighted_sum:
            ui.label(
                "Weighted sum of your objectives vs batch — lower is better; the search "
                "drives this single combined score."
            ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        trace = [t for t in (conv.get("trace") or []) if t.get("metric") is not None]
        if not trace:
            ui.label(
                "No completed batches yet — the trace appears after the first runs finish."
            ).style("color: var(--matofx-muted-text);")
            return
        xs = [int(t.get("batch") or 0) for t in trace]
        ys = [float(t["metric"]) for t in trace]
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="lines+markers",
                name=metric_label,
                customdata=[[_batch_label(b), _fmt(y)] for b, y in zip(xs, ys, strict=True)],
                hovertemplate=(
                    f"Batch: %{{customdata[0]}}<br>"
                    f"{metric_label}: %{{customdata[1]}}<extra></extra>"
                ),
            )
        )
        fig.update_layout(
            xaxis_title="Batch",
            yaxis_title=metric_label,
            height=360,
            margin={"l": 60, "r": 20, "t": 20, "b": 50},
        )
        ui.plotly(themed_plotly(fig)).classes("w-full")


def _render_current_best(conv: dict[str, Any], labels: dict[str, str]) -> None:
    """Best point / Pareto front, keyed by descriptor labels not raw names."""

    def _lbl(key: str) -> str:
        return labels.get(key, key)

    best = conv.get("current_best_json")
    with section_card("emoji_events", "Current best"):
        if not best:
            ui.label("No best point yet.").style("color: var(--matofx-muted-text);")
            return
        if "pareto_front" in best:
            front = list(best.get("pareto_front") or [])
            ui.label(f"Pareto front: {len(front)} non-dominated point(s).").style(
                "color: var(--matofx-muted-text);"
            )
            if front:
                keys = sorted({k for pt in front for k in pt})
                columns = [{"name": k, "label": _lbl(k), "field": k} for k in keys]
                labelled = [{k: _fmt(v) for k, v in pt.items()} for pt in front[:50]]
                ui.table(columns=columns, rows=labelled).classes("w-full")
            return
        rows = [{"key": _lbl(k), "value": _fmt(v)} for k, v in best.items()]
        ui.table(
            columns=[
                {"name": "key", "label": "Parameter / response", "field": "key"},
                {"name": "value", "label": "Value", "field": "value"},
            ],
            rows=rows,
        ).classes("w-full")


def _render_best_designs(
    derived: list[dict[str, Any]],
    objectives: list[dict[str, Any]],
    factors: list[dict[str, Any]],
    weighted_sum: bool = False,
    tracked: list[dict[str, Any]] | None = None,
) -> None:
    """Best design(s) WITH their inputs.

    Single objective → runs ranked best-first. Two or more → ranked by a
    **weighted preference score** (lower = better) with per-objective weight
    inputs you can edit **live** to re-rank. A Pareto study lists the non-
    dominated front; a weighted-sum study lists every feasible run (the search
    optimised the weighted sum directly). Sortable columns; "Show top N" caps
    rows; click a row to open the run.

    ``tracked`` responses appear as extra columns after the objectives. They
    take no part in the ranking, the front, or the weighted score — but the
    front is exactly where they earn their keep: choosing between two
    non-dominated designs is what a tracked response is *for*, and the front
    was the one table that could not show you one.
    """
    tracked = tracked or []
    multi = len(objectives) >= 2
    title = "Best designs"
    if multi:
        title += " — weighted-sum ranking" if weighted_sum else " — Pareto front"
    else:
        title += " (ranked)"
    with section_card("workspace_premium", title):

        def _feasible_complete() -> list[dict[str, Any]]:
            return [
                d
                for d in derived
                if d["state"] == "complete"
                and d["feasible"] is True
                and all(
                    isinstance(d["obj_vals"][i], int | float)
                    and not isinstance(d["obj_vals"][i], bool)
                    for i in range(len(objectives))
                )
            ]

        # A true multi-objective study ranks its Pareto front; a weighted-sum or
        # single-objective study ranks every feasible completed run.
        if multi and not weighted_sum:
            rows_src = [d for d in derived if d["on_front"]]
        else:
            rows_src = _feasible_complete()
        if not multi and rows_src:
            obj0 = objectives[0]
            rows_src.sort(key=lambda d: float(d["obj_vals"][0]), reverse=obj0["goal"] == "maximise")
        if not rows_src:
            ui.label("No feasible completed runs yet.").style("color: var(--matofx-muted-text);")
            return

        weight_inputs: dict[int, Any] = {}
        if multi:
            ui.label(
                "Weights set each objective's importance — lower weighted score is better. "
                "Edit them to re-rank live."
            ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
            with ui.row().classes("items-center gap-3 flex-wrap"):
                for oi, obj in enumerate(objectives):
                    weight_inputs[oi] = ui.number(
                        f"{obj['label']} weight",
                        value=float(obj.get("weight", 1.0)),
                        min=0.0001,
                        step=0.1,
                    ).classes("w-40")

        columns: list[dict[str, Any]] = [
            {"name": "run", "label": "Run", "field": "run", "sortable": True}
        ]
        if multi:
            columns.append(
                {"name": "score", "label": "Weighted score", "field": "score", "sortable": True}
            )
        for oi, obj in enumerate(objectives):
            columns.append(
                {"name": f"o{oi}", "label": _hdr(obj), "field": f"o{oi}", "sortable": True}
            )
        for ti, trk in enumerate(tracked):
            columns.append(
                {"name": f"t{ti}", "label": _hdr(trk), "field": f"t{ti}", "sortable": True}
            )
        for fi, fac in enumerate(factors):
            columns.append(
                {"name": f"f{fi}", "label": _hdr(fac), "field": f"f{fi}", "sortable": True}
            )
        columns.append({"name": "feasible", "label": "Feasible", "field": "feasible"})

        top_n = ui.number(
            "Show top", value=min(10, len(rows_src)), min=1, max=len(rows_src), precision=0
        ).classes("w-32")
        ui.label("Click a row to open that design's run.").style(
            "color: var(--matofx-muted-text); font-size: 0.8rem;"
        )

        def _live_objectives() -> list[dict[str, Any]]:
            if not multi:
                return objectives
            out: list[dict[str, Any]] = []
            for oi, obj in enumerate(objectives):
                w = weight_inputs[oi].value if weight_inputs.get(oi) else obj.get("weight", 1.0)
                out.append({**obj, "weight": float(w) if w else 1.0})
            return out

        def _rows() -> list[dict[str, Any]]:
            ordered = rows_src
            scores: dict[int, float] = {}
            if multi:
                scores = _weighted_scores(rows_src, _live_objectives())
                ordered = sorted(rows_src, key=lambda d: scores.get(int(d["job_id"]), math.inf))
            n = int(top_n.value or len(ordered))
            out: list[dict[str, Any]] = []
            for d in ordered[:n]:
                row: dict[str, Any] = {
                    "run": f"#{d['job_id']}",
                    "_job_id": d["job_id"],
                    "feasible": "—" if d["feasible"] is None else ("✓" if d["feasible"] else "✗"),
                }
                if multi:
                    sc = scores.get(int(d["job_id"]))
                    row["score"] = _sig(sc) if isinstance(sc, float) else "—"
                for oi in range(len(objectives)):
                    row[f"o{oi}"] = _cell(d["obj_vals"][oi])
                for ti in range(len(tracked)):
                    row[f"t{ti}"] = _cell((d.get("track_vals") or [None] * len(tracked))[ti])
                for fi, fac in enumerate(factors):
                    row[f"f{fi}"] = _cell((d.get("inputs") or {}).get(fac["key"]))
                out.append(row)
            return out

        table = (
            ui.table(columns=columns, rows=_rows(), row_key="run")
            .classes("w-full")
            .props("dense flat bordered")
        )
        _wire_row_nav(table)

        def _apply() -> None:
            table.rows = _rows()
            table.update()

        top_n.on_value_change(lambda _e: _apply())
        for weight in weight_inputs.values():
            weight.on_value_change(lambda _e: _apply())


def _render_mop_card(
    client: ApiClient, study_id: int, study: dict[str, Any], in_labels: dict[str, str]
) -> None:
    """Sensitivity / MOP card — run the CoP analysis + choose frozen factors (§5.7)."""
    with section_card("insights", "Sensitivity (MOP / CoP)"):
        ui.label(
            "Build the best metamodel over the runs so far, see how much each factor drives "
            "the result, and freeze the ones that barely matter so the search speeds up."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        panel = ui.column().classes("w-full gap-2")

        async def _run() -> None:
            try:
                state = await client.compute_optimisation_mop(study_id)
            except ApiError as exc:
                ui.notify(exc.detail or "Couldn't run the analysis.", type="negative")
                return
            _render_mop_result(client, study_id, state, panel, in_labels)

        ui.button("Run analysis", icon="insights", on_click=_run).props("flat color=primary")
        existing = study.get("mop_state")
        if existing:
            _render_mop_result(client, study_id, existing, panel, in_labels)


def _render_mop_result(
    client: ApiClient,
    study_id: int,
    state: dict[str, Any],
    panel: ui.column,
    in_labels: dict[str, str],
) -> None:
    """Render a CoP bar chart + per-factor freeze checkboxes into ``panel``.

    Factors display by descriptor label (feedback_use_labels_not_names); the
    raw factor name is kept only as the key sent back to the freeze API.
    """
    panel.clear()
    with panel:
        if state.get("message"):
            ui.label(str(state["message"])).style("color: var(--matofx-muted-text);")
            return
        q2 = state.get("q2")
        q2_text = f"{float(q2):.3f}" if isinstance(q2, int | float) else "n/a"
        ui.label(f"Best metamodel: {state.get('best_kind')} (Q² = {q2_text})").style(
            "color: var(--matofx-muted-text);"
        )
        cop = list(state.get("cop") or [])
        if cop:
            fig = go.Figure(
                go.Bar(
                    x=[float(c.get("cop") or 0.0) for c in cop],
                    y=[in_labels.get(str(c.get("factor")), str(c.get("factor"))) for c in cop],
                    orientation="h",
                )
            )
            fig.update_layout(
                xaxis_title="Coefficient of Prognosis (share of variance explained)",
                height=max(220, 44 * len(cop)),
                margin={"l": 140, "r": 20, "t": 20, "b": 40},
            )
            ui.plotly(themed_plotly(fig)).classes("w-full")

        frozen = set(state.get("frozen_factors") or [])
        ui.label("Freeze (hold at nominal, drop from the search):").style("font-weight: 600;")
        checks: dict[str, Any] = {}
        for c in cop:
            factor = str(c.get("factor"))
            label = in_labels.get(factor, factor)
            checks[factor] = ui.checkbox(
                f"{label}  (CoP {float(c.get('cop') or 0.0):.3f})", value=factor in frozen
            )

        async def _apply() -> None:
            chosen = [f for f, cb in checks.items() if cb.value]
            try:
                updated = await client.set_optimisation_mop_frozen_factors(study_id, chosen)
            except ApiError as exc:
                ui.notify(exc.detail or "Couldn't update frozen factors.", type="negative")
                return
            ui.notify(
                "Frozen factors updated — held at nominal from the next batch.", type="positive"
            )
            _render_mop_result(client, study_id, updated, panel, in_labels)

        ui.button("Apply frozen factors", on_click=_apply).props("flat color=primary")


def _open_publish_dialog(client: ApiClient, study_id: int) -> None:
    """Mint a registry version from the study's latest working surrogate (§16.4)."""
    with ui.dialog() as dialog, ui.card().classes("min-w-96"):
        ui.label("Publish surrogate").style("font-weight: 600; font-size: 1.1rem;")
        ui.label(
            "Save the study's current surrogate as a model version you can browse, "
            "approve, and reuse."
        ).style("color: var(--matofx-muted-text);")
        name_in = ui.input("Model name (optional)").classes("w-full")
        scope_sel = ui.select(_SCOPE_OPTIONS, value="personal", label="Scope").classes("w-full")
        kind_sel = ui.select(_SURROGATE_KIND_OPTIONS, value="", label="Surrogate type").classes(
            "w-full"
        )

        async def _publish() -> None:
            try:
                result = await client.publish_optimisation_surrogate(
                    study_id,
                    name=str(name_in.value) or None,
                    scope=str(scope_sel.value or "personal"),
                    surrogate_kind=str(kind_sel.value) or None,
                )
            except ApiError as exc:
                ui.notify(exc.detail or "Couldn't publish the surrogate.", type="negative")
                return
            dialog.close()
            ui.notify("Surrogate published.", type="positive")
            ui.navigate.to(f"/models/{int(result['model_id'])}")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Publish", on_click=_publish).props("color=primary")
    dialog.open()
