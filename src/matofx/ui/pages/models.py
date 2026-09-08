"""Surrogate Model registry UI (ADR-0008 §5/§6 / surrogate §12).

Two surfaces, both behind the ``features.surrogate_family`` flag (the
nav entry + these pages are absent when it's off, matching the API which
404s every route — a disabled-feature visit redirects home):

- ``GET /models`` — the catalogue: every model the caller may read
  (their own drafts / pending included, unlike the form picker), with
  scope / tool filters and a "fit a new surrogate" affordance that draws
  its source from a completed parametric study or job cohort.
- ``GET /models/{id}`` — the model detail: version history with
  per-response quality (Q² / RMSE), the two-gate global release controls
  (submit-for-review → directed checker → su/admin release), plus refit,
  scope-change, retire, export-as-tool, and metadata edit.

Curate / release controls resolve against the caller's cached permission
so they're *hidden*, not just server-rejected (§12). The server still
enforces every mutation — this is purely about not showing dead buttons.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import plotly.graph_objects as go
from nicegui import ui

from matofx.ui._coverage_panel import MODES_EXPLAINER, modes_count_note
from matofx.ui._features import surrogate_family_enabled
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in, section_card
from matofx.ui.state import current_token, current_user_id, has_permission
from matofx.ui.theme import themed_plotly

_METHOD_LABELS = {
    "gp": "Gaussian process",
    "gp_matern": "Gaussian process (Matérn)",
    "poly": "Polynomial",
    "gbm": "Gradient boosting",
}

# Field methods add 'auto' (best regressor per POD mode) to the scalar set.
_FIELD_METHOD_LABELS = {**_METHOD_LABELS, "auto": "Auto (best per mode)"}

_REASON_LABELS = {
    "non_finite": "non-finite",
    "below_min": "below min",
    "above_max": "above max",
    "field_non_finite": "non-finite field",
    "field_out_of_bounds": "field out of bounds",
}


def _excluded_note(excluded: dict[str, Any] | None) -> str:
    """Human note for runs left out of a fit (ADR-0017), or '' if none."""
    if not excluded:
        return ""
    n = int(excluded.get("n_excluded") or 0)
    if n <= 0:
        return ""
    counts = excluded.get("reason_counts") or {}
    if counts:
        parts = ", ".join(
            f"{count} {_REASON_LABELS.get(code, code.replace('_', ' '))}"
            for code, count in sorted(counts.items())
        )
        return f"{n} run(s) excluded as invalid ({parts})."
    return f"{n} run(s) excluded as invalid."


_MODELS_CURATE = "models.curate"
_MODELS_CREATE = "models.create"
# The mandatory gate-2 releaser authority mirrors the API
# (``user_has_permission(PROJECTS_SEE_ALL)`` == admin / super-user).
_RELEASE_PERM = "projects.see_all"

_SCOPE_COLOR = {"personal": "blue", "project": "teal", "global": "purple"}
_STATE_COLOR = {"active": "green", "pending": "orange", "retired": "grey"}
_PUBLISH_COLOR = {
    "draft": "grey",
    "in_review": "orange",
    "checked": "blue",
    "published": "green",
    "rejected": "red",
}


def _client() -> ApiClient:
    return ApiClient(get_app(), token=current_token())


def _feature_ok() -> bool:
    """Redirect home when the surrogate-family feature is off (page *absent*)."""
    if not surrogate_family_enabled():
        ui.navigate.to("/home")
        return False
    return True


def _chip(label: str, color: str) -> None:
    ui.chip(label).props(f"color={color} text-color=white").classes("text-xs")


# ===================== catalogue (list + create) =====================


@ui.page("/models")
async def models_page(source_kind: str | None = None, source_id: int | None = None) -> None:
    """The Models catalogue — browse + the fit-a-surrogate affordance.

    Two tabs: *Catalogue* (browse what exists) and *New Surrogate Model*
    (the fit form). ``?source_kind=&source_id=`` deep-links the fit form
    pre-filled from a completed study / cohort (the "Build a surrogate"
    affordance on the Parametric Studies results tab routes here) and lands
    straight on the New Surrogate Model tab.
    """
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    with ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;"):
        ui.label("Surrogate Models").style(
            "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
        )
        ui.label(
            "Fitted fast-running stand-ins for your tools. Browse the catalogue, "
            "fit a new surrogate from a study or cohort, and publish for shared use."
        ).style("color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;")
        # Creating a model needs models.create (or the curate capability —
        # curators can always create). Viewers get just the catalogue; the
        # surface is feature-gated, so it's visible to read regardless.
        can_create = has_permission(_MODELS_CREATE) or has_permission(_MODELS_CURATE)
        if not can_create:
            await _render_catalogue(client)
            return
        with (
            ui.tabs()
            .classes("w-full")
            .props("align=left active-color=primary indicator-color=primary")
            .style("border-bottom: 1px solid var(--matofx-card-border);") as tabs
        ):
            catalogue_tab = ui.tab("Catalogue", icon="trending_up").props("no-caps")
            new_tab = ui.tab("New Surrogate Model", icon="add").props("no-caps")
        # A deep-linked source means the user came here to fit one — open
        # straight on the form rather than the catalogue.
        default_tab = new_tab if source_id is not None else catalogue_tab
        with (
            ui.tab_panels(tabs, value=default_tab)
            .classes("w-full")
            .style("background-color: transparent;")
        ):
            with ui.tab_panel(catalogue_tab).style("background-color: transparent;"):
                await _render_catalogue(client)
            with ui.tab_panel(new_tab).style("background-color: transparent;"):
                await _render_create_form(
                    client, preselect_kind=source_kind, preselect_source_id=source_id
                )


def _model_haystack(model: dict[str, Any]) -> str:
    """Lower-cased search text: name + tool (label & alias) + owner + id."""
    mid = int(model.get("id") or 0)
    bits = [
        str(model.get("name") or ""),
        str(model.get("tool_name") or ""),
        str(model.get("tool_alias") or ""),
        str(model.get("created_by_name") or ""),
        f"#{mid}",
    ]
    return " ".join(bits).lower()


def _model_matches(model: dict[str, Any], query: str, status: str) -> bool:
    """Whether a model passes the catalogue search box + status filter."""
    if status != "all" and str(model.get("state") or "") != status:
        return False
    return not query or query in _model_haystack(model)


async def _render_catalogue(client: ApiClient) -> None:
    """Searchable, filterable list of models the caller can read."""
    with section_card("trending_up", "Catalogue"):
        filt: dict[str, str] = {"q": "", "scope": "", "status": "all"}
        cache: dict[str, list[dict[str, Any]]] = {"models": []}
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            search = (
                ui.input(label="Search", placeholder="name, tool, or owner…")
                .props("clearable dense outlined debounce=200")
                .style("flex: 1 1 240px; min-width: 200px;")
            )
            with search.add_slot("prepend"):
                ui.icon("search")
            scope_sel = (
                ui.select(
                    {
                        "": "All scopes",
                        "personal": "Personal",
                        "project": "Project",
                        "global": "Global",
                    },
                    value="",
                    label="Scope",
                )
                .props("dense outlined")
                .style("min-width: 9rem;")
            )
            ui.select(
                {"all": "All", "active": "Active", "pending": "Pending", "retired": "Retired"},
                value="all",
                label="Status",
            ).props("dense outlined").style("min-width: 9rem;").on_value_change(
                lambda e: _apply(status=str(e.value or "all"))
            )
        count_label = ui.label("").style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        list_box = ui.column().classes("w-full gap-1")

        def _refresh() -> None:
            list_box.clear()
            shown = [m for m in cache["models"] if _model_matches(m, filt["q"], filt["status"])]
            with list_box:
                if not cache["models"]:
                    ui.label("No models match. Fit one below.").style(
                        "color: var(--matofx-muted-text);"
                    )
                elif not shown:
                    ui.label("No models match your search and filter.").style(
                        "color: var(--matofx-muted-text); padding: 0.5rem 0.2rem;"
                    )
                else:
                    for model in shown:
                        _render_model_row(model)
            count_label.text = f"{len(shown)} of {len(cache['models'])} models"

        async def _reload() -> None:
            # Scope is server-side; pull retired too so the Status filter can
            # reach them client-side.
            try:
                cache["models"] = await client.list_models(
                    scope=filt["scope"] or None, include_retired=True
                )
            except ApiError as exc:
                cache["models"] = []
                list_box.clear()
                with list_box:
                    ui.label(f"Couldn't load models: {exc.detail}").style(
                        "color: var(--matofx-error-text);"
                    )
                return
            _refresh()

        def _apply(*, status: str | None = None, query: str | None = None) -> None:
            if status is not None:
                filt["status"] = status
            if query is not None:
                filt["q"] = query.strip().lower()
            _refresh()

        async def _on_scope(value: str) -> None:
            filt["scope"] = value
            await _reload()

        search.on_value_change(lambda e: _apply(query=str(e.value or "")))
        scope_sel.on_value_change(lambda e: _on_scope(str(e.value or "")))
        await _reload()


def _render_model_row(model: dict[str, Any]) -> None:
    mid = int(model.get("id") or 0)
    name = str(model.get("name") or f"Model #{mid}")
    # Tool label, not alias ([[feedback_use_labels_not_names]]).
    tool = str(model.get("tool_name") or model.get("tool_alias") or "")
    project = str(model.get("project_name") or "")
    owner = str(model.get("created_by_name") or "")
    scope = str(model.get("scope") or "personal")
    state = str(model.get("state") or "active")
    with (
        ui.row()
        .classes("w-full items-center gap-3 p-2 rounded cursor-pointer")
        .style("background-color: var(--matofx-row-bg);")
        .on("click", lambda _e=None, _id=mid: ui.navigate.to(f"/models/{_id}"))
    ):
        with ui.column().classes("gap-0 flex-grow").style("min-width: 0;"):
            ui.label(name).style("font-weight: 600;")
            bits = [b for b in (tool, project, f"by {owner}" if owner else "") if b]
            ui.label(" · ".join(bits)).style(
                "color: var(--matofx-muted-text); font-size: 0.8rem; "
                "white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
        _chip(scope.title(), _SCOPE_COLOR.get(scope, "grey"))
        _chip(state.title(), _STATE_COLOR.get(state, "grey"))


async def _render_create_form(
    client: ApiClient,
    *,
    preselect_kind: str | None = None,
    preselect_source_id: int | None = None,
) -> None:
    """Fit a surrogate from a completed parametric study or job cohort."""
    is_curator = has_permission(_MODELS_CURATE)
    with section_card("auto_graph", "Fit a new surrogate"):
        try:
            projects = await client.list_projects()
        except ApiError as exc:
            ui.label(f"Couldn't load the form: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return

        # The Project picker only drives the parametric-study source, so it
        # should list *only* projects that actually contain a study — a
        # project with none would lead to an empty Source picker. Studies are
        # project-scoped (no global list), so probe each project in parallel.
        async def _project_has_study(pid: int) -> int | None:
            try:
                return pid if await client.list_studies(pid) else None
            except ApiError:
                return None

        probed = await asyncio.gather(*(_project_has_study(int(p["id"])) for p in projects))
        projects_with_studies = {pid for pid in probed if pid is not None}
        project_opts = {
            int(p["id"]): str(p.get("name") or p["id"])
            for p in projects
            if int(p["id"]) in projects_with_studies
        }
        scope_opts = {"personal": "Personal", "project": "Project"}
        if is_curator:
            scope_opts["global"] = "Global (needs review + release)"

        # --- Section 1: name -------------------------------------------------
        name_in = ui.input("Model name *").classes("w-full")

        ui.separator()

        # --- Section 2: where the training data comes from -------------------
        kind_sel = ui.select(
            {"parametric": "Parametric study", "cohort": "Job cohort"},
            value="parametric",
            label="Source kind",
        ).classes("w-full")
        # Project only scopes a *parametric* source — cohorts are workspace-wide
        # and belong to no project — so this picker is shown only for the
        # parametric kind (toggled by ``_sync_project_visibility`` below).
        project_sel = ui.select(project_opts, label="Project", clearable=True).classes("w-full")
        project_help = ui.label(
            "Only projects that contain a parametric study are listed. Pick one to "
            "choose its study below; you can also scope the model to it (Scope → Project)."
        ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        source_sel = ui.select({}, label="Source *").classes("w-full")

        ui.separator()

        # --- Section 3: what to learn, how, and where it lives ---------------
        # Display the output *labels*; the value list carries the output
        # *names* (the identifiers the fit needs). Populated from the chosen
        # source's tool descriptor — only numeric scalar outputs can be
        # learned as responses.
        # Responses + methods split scalar | field (ADR-0018). Each side sticks
        # to its own methods (they can differ). The field column is shown only
        # when the chosen source has 3-D field outputs — otherwise the scalar
        # column fills the row and the form reads as a plain scalar-only fit.
        scalar_methods = {
            "gp": "Gaussian process (smooth)",
            "gp_matern": "Gaussian process (Matérn — rougher)",
            "poly": "Polynomial (quadratic)",
            "gbm": "Gradient boosting (non-smooth)",
        }
        with ui.row().classes("w-full gap-3 items-start"):
            scalar_sel = ui.select(
                {}, label="Scalar responses to learn", multiple=True
            ).classes("flex-1 min-w-0")
            field_col = ui.column().classes("flex-1 min-w-0 gap-0")
            with field_col:
                field_sel = ui.select(
                    {}, label="Field responses to learn", multiple=True
                ).classes("w-full")
        with ui.row().classes("w-full gap-3 items-start"):
            method_sel = ui.select(
                scalar_methods, value=["gp"], multiple=True, label="Methods to try (scalar)"
            ).classes("flex-1 min-w-0")
            field_method_col = ui.column().classes("flex-1 min-w-0 gap-0")
            with field_method_col:
                field_method_sel = ui.select(
                    scalar_methods | {"auto": "Auto (best per mode)"},
                    value=["gp"],
                    multiple=True,
                    label="Methods to try (field)",
                ).classes("w-full")
        scope_sel = ui.select(scope_opts, value="personal", label="Scope").classes("w-full")
        field_col.set_visibility(False)
        field_method_col.set_visibility(False)
        ui.label(
            "Pick one or more methods to fit and compare. Gaussian process suits smooth "
            "responses and gives an uncertainty band (Matérn tolerates rougher surfaces); "
            "polynomial is a fast quadratic fit; gradient boosting handles non-smooth / "
            "discontinuous responses (no uncertainty band). For a 3-D field, 'Auto' picks "
            "the best regressor per pattern. Fit & compare shows each method's "
            "goodness-of-fit — scalars by predicted-vs-actual, fields by Field Q² — so you "
            "can keep the best before building. A global model goes through the two-gate "
            "review before anyone else can pick it."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

        # On-the-fly output-range filter — exclude outlier runs from the fit
        # without changing the tool's validity bounds (ADR-0018).
        with ui.expansion("Filter runs by output value (optional)", icon="filter_alt").classes(
            "w-full"
        ):
            ui.label(
                "Fit only on runs whose outputs fall in the ranges below — a quick way to "
                "drop a few extreme runs that skew the fit (e.g. only runs where a peak "
                "stress stays physical). This filters the training set for this fit only; "
                "it doesn't change the tool or its validity bounds."
            ).style("color: var(--matofx-muted-text); font-size: 0.82rem;")
            filter_container = ui.column().classes("w-full gap-2")
            ui.button(
                "Add condition", icon="add", on_click=lambda: _add_filter_row()  # noqa: PLW0108
            ).props("flat dense color=primary")

        def _rebuild_filter_rows() -> None:
            filter_container.clear()
            with filter_container:
                for row in output_filter_rows:
                    with ui.row().classes("w-full gap-2 items-center no-wrap"):
                        sel = ui.select(
                            numeric_output_opts,
                            value=row.get("output"),
                            label="Output",
                            with_input=True,
                        ).classes("flex-1 min-w-0")
                        sel.on_value_change(lambda e, r=row: r.update(output=e.value))
                        mn = ui.number(value=row.get("min"), label="Min").props("dense").classes(
                            "w-32"
                        )
                        mn.on_value_change(lambda e, r=row: r.update(min=e.value))
                        mx = ui.number(value=row.get("max"), label="Max").props("dense").classes(
                            "w-32"
                        )
                        mx.on_value_change(lambda e, r=row: r.update(max=e.value))
                        ui.button(
                            icon="close",
                            on_click=lambda _e=None, rid=row["id"]: _remove_filter_row(rid),
                        ).props("flat dense round")

        def _add_filter_row() -> None:
            _filter_row_id[0] += 1
            output_filter_rows.append(
                {"id": _filter_row_id[0], "output": None, "min": None, "max": None}
            )
            _rebuild_filter_rows()

        def _remove_filter_row(rid: int) -> None:
            output_filter_rows[:] = [r for r in output_filter_rows if r.get("id") != rid]
            _rebuild_filter_rows()

        def _collect_output_filter() -> dict[str, dict[str, float]]:
            out: dict[str, dict[str, float]] = {}
            for r in output_filter_rows:
                name = r.get("output")
                lo, hi = r.get("min"), r.get("max")
                if not name or (lo is None and hi is None):
                    continue
                bound: dict[str, float] = {}
                if lo is not None:
                    bound["min"] = float(lo)
                if hi is not None:
                    bound["max"] = float(hi)
                out[str(name)] = bound
            return out

        # Source options depend on the chosen project + kind. Keep the raw
        # dicts around so we can read each source's tool alias on submit.
        sources: dict[int, dict[str, Any]] = {}
        # {output name: "Label (unit)"} — so the comparison table + plots
        # read in human terms, never the bare identifier.
        resp_display: dict[str, str] = {}
        # Output names that are 3-D field outputs (descriptor ``file_type ==
        # "field"``). These fit on their own via the POD field path (a single
        # field response, server-auto-routed), never the scalar bake-off.
        field_responses: set[str] = set()
        # On-the-fly output-range filter (exclude outlier runs from the fit).
        # numeric_output_opts is every numeric output of the chosen source's
        # tool (what you can filter on); each row is {id, output, min, max}.
        numeric_output_opts: dict[str, str] = {}
        output_filter_rows: list[dict[str, Any]] = []
        _filter_row_id = [0]

        async def _reload_responses() -> None:
            scalar_sel.set_options({})
            scalar_sel.set_value([])
            field_sel.set_options({})
            field_sel.set_value([])
            field_col.set_visibility(False)
            field_method_col.set_visibility(False)
            resp_display.clear()
            if not source_sel.value:
                return
            source = sources.get(int(source_sel.value))
            tool_alias = str((source or {}).get("tool_alias") or "")
            if not tool_alias:
                return
            try:
                detail = await client.get_tool(tool_alias)
            except ApiError:
                return
            descriptor = detail.get("descriptor") or {}
            # Numeric scalar outputs are learnable response axes; a ``field``
            # file output (3-D nodal field) is learnable too, but via the POD
            # field path (fit on its own). Tables / other files / strings are
            # not. The picker shows labels; ``resp_display`` adds the unit for
            # the comparison surfaces, and 3-D fields are tagged so they read
            # clearly and route to the field-fit button.
            scalar_opts: dict[str, str] = {}
            field_opts: dict[str, str] = {}
            field_responses.clear()
            for o in descriptor.get("outputs") or []:
                if not isinstance(o, dict) or not o.get("name"):
                    continue
                otype = o.get("type")
                is_field = otype == "file" and o.get("file_type") == "field"
                if otype != "number" and not is_field:
                    continue
                name = str(o["name"])
                label = str(o.get("label") or name)
                unit = str(o.get("unit") or "")
                if is_field:
                    field_responses.add(name)
                    field_opts[name] = label
                    resp_display[name] = f"{label} (3D field)"
                else:
                    scalar_opts[name] = label
                    resp_display[name] = f"{label} ({unit})" if unit else label
            scalar_sel.set_options(scalar_opts)
            field_sel.set_options(field_opts)
            has_fields = bool(field_opts)
            field_col.set_visibility(has_fields)
            field_method_col.set_visibility(has_fields)
            # Refresh the output-filter dropdowns to this tool's numeric outputs.
            numeric_output_opts.clear()
            numeric_output_opts.update(scalar_opts)
            output_filter_rows.clear()
            _rebuild_filter_rows()

        async def _reload_sources() -> None:
            sources.clear()
            source_sel.set_options({})
            source_sel.set_value(None)
            await _reload_responses()
            is_cohort = kind_sel.value == "cohort"
            # Cohorts are workspace-level objects (no home project), so they
            # list without one; a parametric study is project-scoped, so it
            # needs a project chosen first.
            if not is_cohort and not project_sel.value:
                return
            try:
                if is_cohort:
                    rows = await client.list_cohorts()
                else:
                    rows = await client.list_studies(int(project_sel.value))
            except ApiError as exc:
                ui.notify(f"Couldn't list sources: {exc.detail}", type="negative")
                return
            except Exception:  # never leave the picker silently empty
                ui.notify("Couldn't list sources.", type="negative")
                return
            opts: dict[int, str] = {}
            for r in rows:
                sid = int(r.get("id") or 0)
                sources[sid] = r
                label = str(r.get("name") or f"#{sid}")
                opts[sid] = f"{label} ({r.get('tool_alias') or '?'})"
            source_sel.set_options(opts)

        def _sync_project_visibility() -> None:
            """Show the Project picker only for the parametric source kind."""
            is_parametric = kind_sel.value == "parametric"
            project_sel.set_visibility(is_parametric)
            project_help.set_visibility(is_parametric)

        async def _on_kind_change() -> None:
            _sync_project_visibility()
            await _reload_sources()

        project_sel.on_value_change(lambda _e=None: _reload_sources())
        kind_sel.on_value_change(lambda _e=None: _on_kind_change())
        source_sel.on_value_change(lambda _e=None: _reload_responses())
        _sync_project_visibility()

        # The comparison panel renders here after "Fit & compare".
        results_box = ui.column().classes("w-full gap-3")

        def _tool_alias() -> str:
            source = sources.get(int(source_sel.value)) if source_sel.value else None
            return str((source or {}).get("tool_alias") or "")

        async def _create(method: str, field_method: str | None = None) -> None:
            """Persist the model with the chosen scalar method + the chosen field method.

            ``method`` is the kept scalar method; ``field_method`` the kept field
            method (from the comparison). When ``field_method`` is None it falls
            back to the form's field selector (first pick, which may be 'auto').
            Numbers + fields build together as one model.
            """
            if not name_in.value:
                ui.notify("Give the model a name first.", type="warning")
                return
            tool_alias = _tool_alias()
            if not tool_alias or not source_sel.value:
                ui.notify("Pick a source first.", type="warning")
                return
            all_responses = list(scalar_sel.value or []) + list(field_sel.value or [])
            if not all_responses:
                ui.notify("Pick at least one response (scalar or field) to learn.", type="warning")
                return
            scope = str(scope_sel.value or "personal")
            if scope == "project" and not project_sel.value:
                ui.notify("Pick a project for a project-scoped model.", type="warning")
                return
            field_kind = field_method or (
                str(field_method_sel.value[0])
                if field_sel.value and field_method_sel.value
                else None
            )
            # Progress for both entry points (form "Build model" + comparison
            # "Create model"): a spinner that can't auto-dismiss + disabled
            # buttons, so a long combined/field fit clearly reads as "working".
            build_btn.props("loading")
            build_btn.disable()
            compare_btn.disable()
            progress = ui.notification(
                "Building the surrogate — fitting can take a moment…",
                type="ongoing",
                spinner=True,
                timeout=None,
            )
            model: dict[str, Any] | None = None
            try:
                model = await client.create_model(
                    name=str(name_in.value),
                    tool_alias=tool_alias,
                    scope=scope,
                    source_kind=str(kind_sel.value or "parametric"),
                    source_id=int(source_sel.value),
                    response_names=all_responses,
                    surrogate_kind=method,
                    project_id=int(project_sel.value) if scope == "project" else None,
                    field_surrogate_kind=field_kind,
                    output_filter=_collect_output_filter(),
                )
            except ApiError as exc:
                ui.notify(exc.detail, type="negative")
            except Exception:  # surface any unexpected fit/persist error
                ui.notify(
                    "Couldn't create the model — an unexpected error occurred. Please try again.",
                    type="negative",
                )
            finally:
                with contextlib.suppress(RuntimeError):
                    progress.dismiss()
                    build_btn.props(remove="loading")
                    build_btn.enable()
                    compare_btn.enable()
            if model is None:
                return
            note = _excluded_note(model.get("excluded"))
            ui.notify(
                "Surrogate fitted." + (f" {note}" if note else ""), type="positive"
            )
            ui.navigate.to(f"/models/{int(model['id'])}")

        async def _compare() -> None:
            """Bake off scalar methods and field methods over the same source (ADR-0018).

            Scalars are ranked by cross-validated Q² (predicted-vs-actual);
            fields by energy-weighted Field Q² (+ per-mode Q² and a mean-field
            parity). Persists nothing — you keep the best and Build afterwards.
            """
            scalar_responses = list(scalar_sel.value or [])
            field_resp = list(field_sel.value or [])
            methods = list(method_sel.value or [])
            field_methods = list(field_method_sel.value or [])
            if not source_sel.value or (not scalar_responses and not field_resp):
                ui.notify(
                    "To compare, pick a source and at least one response (scalar or field).",
                    type="warning",
                )
                return
            if scalar_responses and not methods:
                ui.notify("Pick at least one scalar method to compare.", type="warning")
                return
            if field_resp and not field_methods:
                ui.notify("Pick at least one field method to compare.", type="warning")
                return
            tool_alias = _tool_alias()
            if not tool_alias:
                ui.notify("That source has no tool to learn from.", type="warning")
                return
            compare_btn.props("loading")
            compare_btn.disable()
            # An "ongoing" notification has no auto-dismiss timeout — we must
            # close it ourselves in every exit path, or it hangs on screen
            # indefinitely even after the comparison has rendered.
            progress = ui.notification(
                "Fitting the selected methods — this can take a few seconds…",
                type="ongoing",
                spinner=True,
                timeout=None,
            )
            try:
                data = await client.compare_model_fits(
                    tool_alias=tool_alias,
                    source_kind=str(kind_sel.value or "parametric"),
                    source_id=int(source_sel.value),
                    response_names=scalar_responses,
                    methods=methods,
                    field_response_names=field_resp,
                    field_methods=field_methods,
                    output_filter=_collect_output_filter(),
                )
            except ApiError as exc:
                ui.notify(exc.detail, type="negative")
                return
            except Exception:  # never let a fit error die silently
                ui.notify(
                    "Something went wrong fitting the surrogate. Please try again, "
                    "or pick a different source.",
                    type="negative",
                )
                return
            finally:
                # If the client was torn down mid-fit (navigation / dropped
                # socket), these UI touches raise "client deleted" — there's
                # nothing left to update, so swallow it rather than surface a
                # spurious traceback.
                with contextlib.suppress(RuntimeError):
                    progress.dismiss()
                    compare_btn.props(remove="loading")
                    compare_btn.enable()
            # Rendering into a torn-down page (you navigated away as the fit
            # finished) raises "client deleted" — nothing to draw, so no-op.
            with contextlib.suppress(RuntimeError):
                _render_comparison(results_box, data, _create, dict(resp_display))

        async def _build() -> None:
            """Build the model directly from the current selection (ADR-0018).

            Uses the first picked scalar method + the first picked field method
            (which may be 'auto'); ``_create`` owns the spinner + button-disable
            and navigates to the model on success. The server splits the responses
            by the descriptor and writes a ``combined`` model when both are picked.
            """
            if not (scalar_sel.value or field_sel.value):
                ui.notify("Pick at least one response to model.", type="warning")
                return
            method = str(method_sel.value[0]) if method_sel.value else "gp"
            await _create(method)

        with ui.row().classes("items-center gap-2"):
            compare_btn = ui.button("Fit & compare", on_click=_compare).props("color=primary")
            build_btn = ui.button("Build model", on_click=_build).props("outline color=primary")
        ui.label(
            "Both buttons build one model from your selection (numbers + any 3-D fields "
            "together). Fit & compare first bakes off the methods so you can keep the best, "
            "then build the winner from the comparison; Build model skips the comparison and "
            "builds straight away with the first picked method on each side. The result is the "
            "same kind of model either way — view and predict it in 3-D and export it as a tool."
        ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")

        # Deep-link prefill (from the Parametric Studies results tab): resolve
        # the source's project, drive the same reload chain a manual pick
        # would, then land on the source with its responses loaded.
        if preselect_source_id is not None:
            await _prefill_source(
                client,
                kind=str(preselect_kind or "parametric"),
                source_id=int(preselect_source_id),
                project_sel=project_sel,
                kind_sel=kind_sel,
                source_sel=source_sel,
                name_in=name_in,
                reload_sources=_reload_sources,
                reload_responses=_reload_responses,
            )


async def _prefill_source(
    client: ApiClient,
    *,
    kind: str,
    source_id: int,
    project_sel: Any,
    kind_sel: Any,
    source_sel: Any,
    name_in: Any,
    reload_sources: Any,
    reload_responses: Any,
) -> None:
    """Pre-fill the fit form from a deep-linked study / cohort source."""
    project_id: int | None = None
    try:
        if kind == "cohort":
            # Cohorts are workspace-level — no project to resolve or set.
            src_name = (await client.get_cohort(source_id)).get("name")
        else:
            detail = await client.get_study(source_id)
            study = detail.get("study") or {}
            study_pid = study.get("project_id")
            src_name = study.get("name")
            if study_pid is None:
                return
            project_id = int(study_pid)
    except ApiError:
        return
    kind_sel.set_value(kind)
    if project_id is not None:
        project_sel.set_value(project_id)
    await reload_sources()
    source_sel.set_value(source_id)
    await reload_responses()
    if src_name and not name_in.value:
        name_in.set_value(f"{src_name} surrogate")


# ===================== detail =====================


@ui.page("/models/{model_id}")
async def model_detail_page(model_id: int) -> None:
    """The model detail — version history, quality, and lifecycle actions."""
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    body = ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;")
    await _render_detail(client, model_id, body)


async def _render_detail(client: ApiClient, model_id: int, body: ui.column) -> None:
    body.clear()
    with body:
        try:
            detail = await client.get_model(model_id)
        except ApiError as exc:
            ui.label(f"Couldn't load this model: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return
        model = detail.get("model") or {}
        versions = list(detail.get("versions") or [])
        scope = str(model.get("scope") or "personal")
        state = str(model.get("state") or "active")
        # Resolve the tool's friendly name + every input/output label from the
        # source descriptor, so the page reads "Finned Cold Plate", "Mass (g)"
        # and "Fin height (mm)", never the bare aliases / identifiers.
        tool_name, resp_labels, factor_labels = await _tool_meta(
            client, str(model.get("tool_alias") or "")
        )

        async def _refresh() -> None:
            await _render_detail(client, model_id, body)

        # Back link above the title (left-aligned, back-arrow) so it clears
        # the fixed theme / help buttons pinned to the top-right.
        ui.button(
            "Back to models", icon="arrow_back", on_click=lambda: ui.navigate.to("/models")
        ).props("flat dense no-caps").style(
            "color: var(--matofx-muted-text); margin-left: -0.3rem;"
        )
        with ui.row().classes("w-full items-center gap-3"):
            ui.label(str(model.get("name") or f"Model #{model_id}")).style(
                "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
            )
            _chip(scope.title(), _SCOPE_COLOR.get(scope, "grey"))
            _chip(state.title(), _STATE_COLOR.get(state, "grey"))
        # Caption: tool label · source kind (a surrogate stands in for a tool).
        caption_bits = [b for b in (tool_name, "surrogate model") if b]
        if caption_bits:
            ui.label(" · ".join(caption_bits)).style(
                "color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;"
            )

        _render_meta_card(client, model, _refresh, tool_name)
        _render_versions_card(client, model, versions, _refresh, resp_labels, factor_labels)
        _render_model_actions(client, model, _refresh)


def _display_labels(fields: Any) -> dict[str, str]:
    """``{name: "Label (unit)"}`` for one descriptor section.

    Every surface on this page shows what the *tool author* called a thing, so
    the map covers every declared field — including a ``file`` output, which
    is how a 3-D field response is declared. Restricting it to ``number``
    outputs left the field panels, the viewer title and its axes reading
    ``temperature_field``.
    """
    out: dict[str, str] = {}
    for f in fields or []:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        name = str(f["name"])
        label = str(f.get("label") or name)
        unit = str(f.get("unit") or "")
        out[name] = f"{label} ({unit})" if unit else label
    return out


async def _tool_meta(
    client: ApiClient, tool_alias: str
) -> tuple[str, dict[str, str], dict[str, str]]:
    """``(tool display name, output labels, input labels)`` from the descriptor.

    A model version freezes *identifiers* — ``factors`` and ``responses`` carry
    names, never labels — so every human-readable string on this page is
    resolved here from the live source descriptor and threaded down.
    Best-effort: an unreadable tool yields the alias + empty maps, so the page
    falls back to bare identifiers rather than erroring.
    """
    if not tool_alias:
        return "", {}, {}
    try:
        detail = await client.get_tool(tool_alias)
    except ApiError:
        return tool_alias, {}, {}
    descriptor = detail.get("descriptor") or {}
    tool_name = str(detail.get("name") or descriptor.get("name") or tool_alias)
    return (
        tool_name,
        _display_labels(descriptor.get("outputs")),
        _display_labels(descriptor.get("inputs")),
    )


def _render_meta_card(
    client: ApiClient, model: dict[str, Any], refresh: Any, tool_name: str
) -> None:
    mid = int(model.get("id") or 0)
    alias = str(model.get("tool_alias") or "")
    tool_text = f"{tool_name} ({alias})" if tool_name and tool_name != alias else alias
    with section_card("info", "Details"):
        with ui.row().classes("w-full items-center"):
            ui.space()
            ui.button(
                icon="edit", on_click=lambda: _edit_meta_dialog(client, model, refresh)
            ).props("flat dense round")
        ui.label(f"Tool: {tool_text}").style("color: var(--matofx-muted-text);")
        if model.get("description"):
            ui.label(str(model["description"]))
        tags = model.get("tags") or []
        if tags:
            with ui.row().classes("gap-1"):
                for tag in tags:
                    ui.chip(str(tag)).props("color=grey-7 text-color=white").classes("text-xs")
        published = model.get("published_version_id")
        ui.label(
            f"Published version: {published}" if published else "No published version yet."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        del mid


def _render_versions_card(
    client: ApiClient,
    model: dict[str, Any],
    versions: list[dict[str, Any]],
    refresh: Any,
    resp_labels: dict[str, str],
    factor_labels: dict[str, str],
) -> None:
    mid = int(model.get("id") or 0)
    scope = str(model.get("scope") or "personal")
    is_curator = has_permission(_MODELS_CURATE)
    can_release = has_permission(_RELEASE_PERM)
    with section_card("history", "Version history"):
        if not versions:
            ui.label("No versions yet.").style("color: var(--matofx-muted-text);")
            return
        for v in versions:
            _render_version_row(
                client, mid, scope, v, is_curator, can_release, refresh, resp_labels, factor_labels
            )


def _render_version_row(
    client: ApiClient,
    model_id: int,
    scope: str,
    version: dict[str, Any],
    is_curator: bool,
    can_release: bool,
    refresh: Any,
    resp_labels: dict[str, str],
    factor_labels: dict[str, str],
) -> None:
    vid = int(version.get("id") or 0)
    ordinal = version.get("ordinal")
    publish_state = str(version.get("publish_state") or "draft")
    with (
        ui.row()
        .classes("w-full items-center gap-3 p-2 rounded")
        .style("background-color: var(--matofx-row-bg);")
    ):
        ui.label(f"v{ordinal}").style("font-weight: 600;")
        ui.label(str(version.get("kind") or "scalar")).style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )
        method = str(version.get("surrogate_method") or "gp")
        ui.label(_METHOD_LABELS.get(method, method)).style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )
        ui.label(f"{version.get('n_training_points', 0)} pts").style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )
        _chip(publish_state.replace("_", " ").title(), _PUBLISH_COLOR.get(publish_state, "grey"))
        ui.space()
        fitted = version.get("fitted_at")
        if fitted:
            ui.label(format_user_datetime(str(fitted))).style(
                "color: var(--matofx-muted-text); font-size: 0.8rem;"
            )
        # Gate-1 / gate-2 controls only matter for a global model.
        if scope == "global" and publish_state == "draft" and is_curator:
            ui.button(
                "Submit for review",
                on_click=lambda: _submit_review_dialog(client, model_id, vid, refresh),
            ).props("flat dense color=primary")
        if scope == "global" and publish_state == "checked" and can_release:

            async def _release(_vid: int = vid) -> None:
                try:
                    await client.release_model_version(model_id, _vid)
                except ApiError as exc:
                    ui.notify(exc.detail, type="negative")
                    return
                ui.notify("Version released to general use.", type="positive")
                await refresh()

            ui.button("Release", on_click=_release).props("flat dense color=positive")

    quality = version.get("quality") or {}
    kind = str(version.get("kind"))
    # Numbers first, then fields. A ``combined`` version used to lead with the
    # field panels — POD modes and a 3-D viewer — so the scalar Q² table, which
    # is what most readers came for, sat below several screens of them. The two
    # halves are also two different questions, so each gets its own card
    # rather than a run of loose panels under the version row.
    if kind in ("scalar", "combined"):
        # For a combined version, ``quality`` mixes scalar + field blocks; show
        # only the scalar responses in the scalar quality/diagnostics panels.
        scalar_names = {
            str(r.get("name"))
            for r in (version.get("responses") or [])
            if r.get("name") and r.get("kind") != "field"
        }
        scalar_quality = {k: v for k, v in quality.items() if k in scalar_names}
        if scalar_quality:
            with section_card("insights", "Scalar fits"):
                ui.label(
                    "How well the surrogate reproduces each number the tool reports, "
                    "measured on held-out (cross-validated) runs."
                ).style("color: var(--matofx-muted-text); font-size: 0.82rem;")
                _render_quality(scalar_quality, resp_labels)
                _render_fit_diagnostics(scalar_quality, resp_labels)
    if kind in ("field", "combined"):
        with section_card("grain", "Field fits"):
            _render_field_section(client, model_id, version, resp_labels, factor_labels)


def _render_field_section(
    client: ApiClient,
    model_id: int,
    version: dict[str, Any],
    resp_labels: dict[str, str],
    factor_labels: dict[str, str],
) -> None:
    """Lazily mount a predicted-field viewer per field output (field / combined).

    The viewer (sliders + live ``field-predict``) only builds on first expand so
    the version list stays light. A ``combined`` version (ADR-0018) gets one
    viewer per field output, each predicting its own response.
    """
    from matofx.ui._field_surrogate_viewer import render_field_viewer  # noqa: PLC0415

    field_responses = [
        r
        for r in (version.get("responses") or [])
        if r.get("kind") == "field" and r.get("name")
    ]
    if not field_responses:  # defensive — a field/combined version always has one
        return
    single = len(field_responses) == 1
    quality = version.get("quality") or {}

    def _add_field_viewer(name: str, label: str) -> None:
        # Each call owns its own ``exp`` / container / rendered flag, so the
        # lazy ``_run`` closure binds them safely (no shared loop variable).
        rendered = {"done": False}
        holder: dict[str, Any] = {}
        title = "Predicted-field viewer" if single else f"Predicted-field viewer — {label}"

        async def _run() -> None:
            if rendered["done"] or not exp.value:
                return
            rendered["done"] = True
            with holder["container"]:
                await render_field_viewer(
                    client,
                    model_id,
                    version,
                    response_label=label,
                    response_name=name,
                    factor_labels=factor_labels,
                )

        exp = (
            ui.expansion(title, on_value_change=lambda: ui.timer(0.0, _run, once=True))
            .classes("w-full")
            .props("icon=grain dense")
        )
        with exp:
            holder["container"] = ui.column().classes("w-full")

    for fr in field_responses:
        fname = str(fr.get("name"))
        label = resp_labels.get(fname, fname)
        _render_field_quality_panel(quality.get(fname) or {}, label)
        _add_field_viewer(fname, label)


def _render_field_quality_panel(q: dict[str, Any], label: str) -> None:
    """Fit-quality for one field output — the field analogue of the scalar panel.

    Shows the headline Field Q² (energy-weighted per-mode Q²), retained modes +
    energy, reconstruction RMSE, and the per-mode Q² breakdown, so a built field
    surrogate's quality is visible (not just its predict viewer). A low Field Q²
    is flagged with what to try next.

    Renders as a headed block inside the "Field fits" card rather than as a card
    of its own — a combined version has one of these per field output, and a
    card nested in a card reads as a framing error rather than as structure.
    """
    if not q:
        return
    with ui.column().classes("w-full gap-2"):
        ui.label(label).style("font-weight: 650; color: var(--matofx-text);")
        fq = q.get("field_q2")
        modes = q.get("modes_retained")
        energy = q.get("mode_energy")
        rr = q.get("recon_rmse")
        bits: list[str] = []
        if fq is not None:
            bits.append(f"Field Q² {_fmt(fq)}")
        if modes is not None:
            bits.append(f"{modes} modes" + (f" ({energy * 100:.0f}% energy)" if energy else ""))
        if rr is not None:
            bits.append(f"reconstruction RMSE {_fmt(rr)}")
        if bits:
            ui.label(" · ".join(bits)).style("font-weight: 600; color: var(--matofx-text);")
        # "3 modes (99% energy)" is a fact about the archive, not a setting — and
        # it means nothing without saying what a mode is.
        ui.label(MODES_EXPLAINER).style("color: var(--matofx-muted-text); font-size: 0.82rem;")
        note = modes_count_note(modes, energy)
        if note:
            ui.label(note).style("color: var(--matofx-muted-text); font-size: 0.82rem;")
        if q.get("mode_q2"):
            _render_mode_q2_bars("Per-mode Q²", q["mode_q2"], q.get("mode_energy_spectrum") or [])
        if fq is not None and fq < 0.7:
            ui.label(
                "Low Field Q² — the field surrogate isn't tracking the runs well. Try "
                "excluding outlier runs (Fit form → Filter runs by output value), or add "
                "more / better-spread runs, then refit."
            ).style("color: #b58900; font-size: 0.82rem;")


def _render_fit_diagnostics(quality: dict[str, Any], resp_labels: dict[str, str]) -> None:
    """Collapsible predicted-vs-actual parity plots per response (the fit visual).

    Built from the cross-validated ``cv_actual`` / ``cv_predicted`` pairs
    stored at fit time — held-out points, so the scatter is an honest
    goodness-of-fit picture (tight to the diagonal = good). Needs no
    retained training data.
    """
    plottable = {
        r: m
        for r, m in quality.items()
        if isinstance(m, dict) and m.get("cv_actual") and m.get("cv_predicted")
    }
    if not plottable:
        return
    with (
        ui.expansion("Fit diagnostics — predicted vs actual")
        .classes("w-full")
        .props("icon=insights dense")
    ):
        ui.label(
            "Each point is a held-out (cross-validated) run; the dashed line is a "
            "perfect fit. Points hugging the line mean the surrogate predicts well."
        ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        for response, m in plottable.items():
            _render_parity_plot(
                resp_labels.get(str(response), str(response)),
                [float(v) for v in m["cv_actual"]],
                [float(v) for v in m["cv_predicted"]],
                m.get("q2"),
            )


def _render_parity_plot(label: str, actual: list[float], predicted: list[float], q2: Any) -> None:
    lo = min([*actual, *predicted])
    hi = max([*actual, *predicted])
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=[lo, hi],
            y=[lo, hi],
            mode="lines",
            line={"dash": "dash", "color": "#888"},
            name="perfect fit",
            hoverinfo="skip",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=actual,
            y=predicted,
            mode="markers",
            marker={"color": "#2c7fb8", "size": 8},
            name="held-out runs",
            hovertemplate="actual %{x:.4g}<br>predicted %{y:.4g}<extra></extra>",
        )
    )
    title = label if q2 is None else f"{label}  (Q² = {_fmt(q2)})"
    fig.update_layout(
        title=title,
        xaxis_title="Actual",
        yaxis_title="Predicted",
        height=320,
        showlegend=False,
        margin={"l": 60, "r": 20, "t": 50, "b": 50},
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


# ===================== fit comparison (bake-off) =====================


def _render_mode_q2_bars(title: str, mode_q2: list[Any], energy: list[Any]) -> None:
    """Bar chart of each retained POD mode's Q², annotated with its energy share."""
    labels: list[str] = []
    for i in range(len(mode_q2)):
        share = energy[i] if i < len(energy) and energy[i] is not None else None
        labels.append(f"Mode {i + 1}" + (f" ({share * 100:.0f}%)" if share is not None else ""))
    vals = [float(q) if q is not None else 0.0 for q in mode_q2]
    fig = go.Figure(go.Bar(x=labels, y=vals, marker_color="#2c7fb8"))
    fig.update_layout(
        title=title,
        yaxis_title="Q²",
        height=280,
        margin={"l": 50, "r": 20, "t": 50, "b": 70},
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


def _render_field_comparison(data: dict[str, Any], resp_display: dict[str, str]) -> None:
    """Render the field bake-off (ADR-0018): Field Q² table + per-mode bars + parity.

    Fields are ranked by energy-weighted **Field Q²** (POD basis is method-
    independent, so only the per-mode regressor quality differs). Each field also
    gets a per-mode Q² breakdown for the best method and an overlaid mean-field
    predicted-vs-actual parity across methods — the field analogue of the scalar
    scatter.
    """
    field_methods = list(data.get("field_methods") or [])
    field_responses = list(data.get("field_responses") or [])
    best_field = data.get("best_field_overall")
    fitted = [m for m in field_methods if m.get("fitted")]
    if not field_responses:
        return
    ui.separator()
    ui.label("Field surrogates").style("font-weight: 600; color: var(--matofx-text);")
    if not fitted:
        ui.label("No field method could fit this data.").style("color: var(--matofx-error-text);")
        for m in field_methods:
            if m.get("message"):
                label = _FIELD_METHOD_LABELS.get(m["method"], m["method"])
                ui.label(f"{label}: {m['message']}").style(
                    "color: var(--matofx-muted-text); font-size: 0.8rem;"
                )
        return
    fitted.sort(
        key=lambda m: (m.get("mean_field_q2") is not None, m.get("mean_field_q2") or -1e9),
        reverse=True,
    )
    multi = len(field_responses) > 1
    columns: list[dict[str, Any]] = [
        {"name": "method", "label": "Field method", "field": "method", "align": "left"}
    ]
    if multi:
        columns.append(
            {"name": "mean", "label": "Mean Field Q²", "field": "mean", "align": "right"}
        )
    for r in field_responses:
        columns.append(
            {"name": r, "label": resp_display.get(r, r), "field": r, "align": "right"}
        )
    rows: list[dict[str, Any]] = []
    for m in fitted:
        star = "★ " if m["method"] == best_field else ""
        row: dict[str, Any] = {"method": star + _FIELD_METHOD_LABELS.get(m["method"], m["method"])}
        if multi:
            row["mean"] = _fmt(m.get("mean_field_q2"))
        for r in field_responses:
            row[r] = _fmt(((m.get("responses") or {}).get(r) or {}).get("field_q2"))
        rows.append(row)
    ui.table(columns=columns, rows=rows).props("dense flat").classes("w-full")
    ui.label(
        "Field Q² = per-mode Q² weighted by mode energy (max 1.0). ★ marks the best "
        "method per field."
    ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")

    best_method = fitted[0]
    for r in field_responses:
        with (
            ui.expansion(f"{resp_display.get(r, r)} — detail", value=not multi)
            .classes("w-full")
            .props("icon=ssid_chart dense")
        ):
            q = (best_method.get("responses") or {}).get(r) or {}
            if q.get("mode_q2"):
                best_label = _FIELD_METHOD_LABELS.get(best_method["method"], best_method["method"])
                _render_mode_q2_bars(
                    f"{best_label} — per-mode Q²", q["mode_q2"], q.get("mode_energy_spectrum") or []
                )
            series = [
                (
                    _FIELD_METHOD_LABELS.get(m["method"], m["method"]),
                    [float(v) for v in ((m.get("responses") or {}).get(r) or {}).get(
                        "cv_field_actual", []
                    )],
                    [float(v) for v in ((m.get("responses") or {}).get(r) or {}).get(
                        "cv_field_predicted", []
                    )],
                )
                for m in fitted
                if ((m.get("responses") or {}).get(r) or {}).get("cv_field_actual")
            ]
            if series:
                _render_overlaid_parity(f"{resp_display.get(r, r)} — mean field", series)


def _render_comparison(
    box: Any, data: dict[str, Any], on_create: Any, resp_display: dict[str, str]
) -> None:
    """Render the compare-fits bake-off: best-highlighted table + overlaid plots.

    ``data`` is the ``/models/compare-fits`` response; ``on_create`` is the
    form's ``_create(method)`` coroutine, called when the user keeps a method;
    ``resp_display`` maps each response name to its "Label (unit)" so every
    surface reads in human terms.
    """
    box.clear()
    methods = list(data.get("methods") or [])
    responses = list(data.get("responses") or [])
    best_overall = data.get("best_overall")
    best_per = data.get("best_per_response") or {}
    scalar_fitted = [m for m in methods if m.get("fitted")]
    field_methods = list(data.get("field_methods") or [])
    field_fitted = [m for m in field_methods if m.get("fitted")]
    best_field = data.get("best_field_overall")
    with box, section_card("compare_arrows", "Fit comparison"):
        note = _excluded_note(data.get("excluded"))
        if note:
            ui.label(note).style("color: var(--matofx-muted-text); font-size: 0.82rem;")
        if not scalar_fitted and not field_fitted:
            ui.label(
                "No method could fit this data. Try a different source, fewer factors, "
                "or more completed runs."
            ).style("color: var(--matofx-error-text);")
            for m in [*methods, *field_methods]:
                if m.get("message"):
                    label = _FIELD_METHOD_LABELS.get(m["method"], m["method"])
                    ui.label(f"{label}: {m['message']}").style(
                        "color: var(--matofx-muted-text); font-size: 0.8rem;"
                    )
            return

        if responses and scalar_fitted:
            _render_comparison_table(methods, responses, best_overall, best_per, resp_display)
            with (
                ui.expansion("Predicted vs actual (overlaid)", value=True)
                .classes("w-full")
                .props("icon=insights dense")
            ):
                ui.label(
                    "Each method's held-out (cross-validated) predictions; the dashed line "
                    "is a perfect fit. Tighter to the line = better."
                ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
                for resp in responses:
                    series = [
                        (
                            _METHOD_LABELS.get(m["method"], m["method"]),
                            [float(v) for v in (m["quality"].get(resp) or {}).get("cv_actual", [])],
                            [
                                float(v)
                                for v in (m["quality"].get(resp) or {}).get("cv_predicted", [])
                            ],
                        )
                        for m in scalar_fitted
                        if (m["quality"].get(resp) or {}).get("cv_actual")
                    ]
                    if series:
                        _render_overlaid_parity(resp_display.get(resp, resp), series)

        _render_field_comparison(data, resp_display)

        # Keep the best of each side and persist (scalar method + field method).
        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            keep_sel = None
            if scalar_fitted:
                keep_opts = {
                    m["method"]: _METHOD_LABELS.get(m["method"], m["method"]) for m in scalar_fitted
                }
                keep_value = best_overall if best_overall in keep_opts else next(iter(keep_opts))
                keep_sel = ui.select(
                    keep_opts, value=keep_value, label="Keep scalar method"
                ).classes("w-64")
            keep_field_sel = None
            if field_fitted:
                fopts = {
                    m["method"]: _FIELD_METHOD_LABELS.get(m["method"], m["method"])
                    for m in field_fitted
                }
                fvalue = best_field if best_field in fopts else next(iter(fopts))
                keep_field_sel = ui.select(
                    fopts, value=fvalue, label="Keep field method"
                ).classes("w-64")

            async def _keep() -> None:
                keep_btn.props("loading")
                keep_btn.disable()
                try:
                    scalar_method = str(keep_sel.value) if keep_sel is not None else "gp"
                    field_method = (
                        str(keep_field_sel.value) if keep_field_sel is not None else None
                    )
                    await on_create(scalar_method, field_method)
                finally:
                    with contextlib.suppress(RuntimeError):
                        keep_btn.props(remove="loading")
                        keep_btn.enable()

            keep_btn = ui.button("Build with kept methods", on_click=_keep).props("color=primary")
        ui.label(
            "★ marks the best in each column. Higher Q² (max 1.0) is better; field methods "
            "are ranked by Field Q²."
        ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")


def _render_comparison_table(
    methods: list[dict[str, Any]],
    responses: list[str],
    best_overall: str | None,
    best_per: dict[str, str],
    resp_display: dict[str, str],
) -> None:
    """Compact Q² matrix (rows = methods, best-first) with RMSE collapsed below.

    Splitting Q² (the headline, always shown) from RMSE (in an expansion)
    keeps single-number cells so methods compare top-to-bottom without a wide
    horizontal scroll. ★ marks the best method per column; response columns
    read as the tool's label + unit, never the bare name.
    """
    fitted = [m for m in methods if m.get("fitted")]
    fitted.sort(
        key=lambda m: (m.get("mean_q2") is not None, m.get("mean_q2") or -1e9), reverse=True
    )
    unfitted = [m for m in methods if not m.get("fitted")]

    def _method_cell(m: dict[str, Any]) -> str:
        label = _METHOD_LABELS.get(m["method"], m["method"])
        return f"★ {label}" if m["method"] == best_overall else label

    def _matrix(metric: str) -> None:
        columns: list[dict[str, Any]] = [
            {"name": "method", "label": "Method", "field": "method", "align": "left"}
        ]
        if metric == "q2":
            columns.append({"name": "mean", "label": "Mean Q²", "field": "mean", "align": "right"})
        for resp in responses:
            columns.append(
                {
                    "name": resp,
                    "label": resp_display.get(resp, resp),
                    "field": resp,
                    "align": "right",
                }
            )
        rows: list[dict[str, Any]] = []
        for m in fitted:
            row: dict[str, Any] = {"method": _method_cell(m)}
            if metric == "q2":
                row["mean"] = _fmt(m.get("mean_q2"))
            for resp in responses:
                q = m["quality"].get(resp) or {}
                value = _fmt(q.get("q2") if metric == "q2" else q.get("rmse_cv"))
                # Only the headline Q² matrix stars the best (best_per is by Q²).
                row[resp] = (
                    f"★ {value}" if metric == "q2" and best_per.get(resp) == m["method"] else value
                )
            rows.append(row)
        ui.table(columns=columns, rows=rows, row_key="method").classes("w-full").props("dense flat")

    _matrix("q2")
    with ui.expansion("Show RMSE (cross-validated)").classes("w-full").props("dense"):
        ui.label("Lower RMSE is better; each in the response's own units.").style(
            "color: var(--matofx-muted-text); font-size: 0.8rem;"
        )
        _matrix("rmse")
    for m in unfitted:
        label = _METHOD_LABELS.get(m["method"], m["method"])
        ui.label(f"{label}: couldn't fit — {m.get('message') or 'not enough data'}.").style(
            "color: var(--matofx-muted-text); font-size: 0.8rem;"
        )


def _render_overlaid_parity(label: str, series: list[tuple[str, list[float], list[float]]]) -> None:
    """One parity plot with a coloured marker trace per method + the diagonal."""
    all_vals = [v for _, actual, predicted in series for v in (*actual, *predicted)]
    if not all_vals:
        return
    lo, hi = min(all_vals), max(all_vals)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=[lo, hi],
            y=[lo, hi],
            mode="lines",
            line={"dash": "dash", "color": "#888"},
            name="perfect fit",
            hoverinfo="skip",
        )
    )
    for method_label, actual, predicted in series:
        fig.add_trace(
            go.Scatter(
                x=actual,
                y=predicted,
                mode="markers",
                name=method_label,
                marker={"size": 7},
                hovertemplate=(
                    f"{method_label}<br>actual %{{x:.4g}}<br>predicted %{{y:.4g}}<extra></extra>"
                ),
            )
        )
    fig.update_layout(
        title=label,
        xaxis_title="Actual",
        yaxis_title="Predicted",
        height=360,
        margin={"l": 60, "r": 20, "t": 50, "b": 50},
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")


def _render_quality(quality: dict[str, Any], resp_labels: dict[str, str]) -> None:
    """Per-response CV fit metrics under the version row.

    The Response column shows the source tool's output label + unit
    (``Plate + fin mass (g)``), falling back to the bare name only when the
    tool descriptor couldn't be read.
    """
    columns = [
        {"name": "response", "label": "Response", "field": "response", "align": "left"},
        {"name": "q2", "label": "Q²", "field": "q2"},
        {"name": "rmse_cv", "label": "RMSE (CV)", "field": "rmse_cv"},
        {"name": "n", "label": "n", "field": "n"},
    ]
    rows = []
    for response, metrics in quality.items():
        m = metrics if isinstance(metrics, dict) else {}
        rows.append(
            {
                "response": resp_labels.get(str(response), str(response)),
                "q2": _fmt(m.get("q2")),
                "rmse_cv": _fmt(m.get("rmse_cv")),
                "n": m.get("n"),
            }
        )
    ui.table(columns=columns, rows=rows, row_key="response").classes("w-full").props("dense flat")


def _fmt(value: Any) -> Any:
    return round(value, 4) if isinstance(value, (int, float)) else value


def _render_model_actions(client: ApiClient, model: dict[str, Any], refresh: Any) -> None:
    mid = int(model.get("id") or 0)
    scope = str(model.get("scope") or "personal")
    state = str(model.get("state") or "active")
    is_curator = has_permission(_MODELS_CURATE)
    with ui.row().classes("w-full gap-2"):

        async def _refit() -> None:
            try:
                await client.refit_model(mid)
            except ApiError as exc:
                ui.notify(exc.detail, type="negative")
                return
            ui.notify("Refitted — a new version was added.", type="positive")
            await refresh()

        ui.button("Refit", on_click=_refit).props("outline color=primary")
        ui.button("Export as tool", on_click=lambda: _export_dialog(client, mid, refresh)).props(
            "outline"
        )
        # Scope change to/from global is governed elsewhere; offer the
        # ungated tier moves (personal ⇄ project) here.
        # `async` handler, not a sync lambda returning a coroutine: the dialog
        # now loads the project list before building, and a dropped coroutine
        # would simply make the button look dead
        # ([[feedback_nicegui_async_click_chain]]).
        async def _open_scope_dialog() -> None:
            await _scope_dialog(client, model, refresh)

        ui.button("Change scope", on_click=_open_scope_dialog).props("outline")
        if state == "active":
            ui.button("Retire", on_click=lambda: _retire_dialog(client, mid, refresh)).props(
                "outline color=negative"
            )
    del scope, is_curator


# ===================== dialogs =====================


def _edit_meta_dialog(client: ApiClient, model: dict[str, Any], refresh: Any) -> None:
    mid = int(model.get("id") or 0)
    with ui.dialog() as dialog, section_card("edit", "Edit details"):
        name_in = ui.input("Name", value=str(model.get("name") or "")).classes("w-full")
        desc_in = ui.textarea("Description", value=str(model.get("description") or "")).classes(
            "w-full"
        )
        tags_in = ui.input(
            "Tags (comma-separated)", value=", ".join(model.get("tags") or [])
        ).classes("w-full")

        async def _save() -> None:
            tags = [t.strip() for t in str(tags_in.value or "").split(",") if t.strip()]
            try:
                await client.update_model(
                    mid,
                    name=str(name_in.value or ""),
                    description=str(desc_in.value or ""),
                    tags=tags,
                )
            except ApiError as exc:
                ui.notify(exc.detail, type="negative")
                return
            dialog.close()
            ui.notify("Saved.", type="positive")
            await refresh()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=_save).props("color=primary")
    dialog.open()


async def _submit_review_dialog(
    client: ApiClient, model_id: int, version_id: int, refresh: Any
) -> None:
    try:
        approvers = await client.model_approvers()
    except ApiError as exc:
        ui.notify(f"Couldn't load checkers: {exc.detail}", type="negative")
        approvers = []
    with ui.dialog() as dialog, section_card("rate_review", "Submit for review"):
        ui.label(
            "Nominate a Model Curator to technically check this version. Required "
            "when global publish approval is on; leave blank to check directly if "
            "your operator has turned approval off."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        opts = {int(a["user_id"]): str(a.get("full_name") or a["username"]) for a in approvers}
        checker_sel = ui.select(opts, label="Checker", with_input=True).classes("w-full")

        async def _submit() -> None:
            checker = int(checker_sel.value) if checker_sel.value else None
            try:
                await client.submit_model_review(model_id, version_id, checker_user_id=checker)
            except ApiError as exc:
                ui.notify(exc.detail, type="negative")
                return
            dialog.close()
            ui.notify("Submitted for review.", type="positive")
            await refresh()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Submit", on_click=_submit).props("color=primary")
    dialog.open()


async def _scope_dialog(client: ApiClient, model: dict[str, Any], refresh: Any) -> None:
    mid = int(model.get("id") or 0)
    current = str(model.get("scope") or "personal")
    is_curator = has_permission(_MODELS_CURATE)
    # Projects this user could actually host a model in — the ones they created
    # or own. Moving to project scope needs EDIT on the destination, so listing
    # every *visible* project would offer choices that fail on Apply.
    me = current_user_id()
    try:
        projects = await client.list_projects()
    except ApiError:
        projects = []
    project_opts = {
        int(p["id"]): str(p.get("name") or p["id"])
        for p in projects
        if me is not None
        and me in {p.get("creator_user_id"), p.get("owner_user_id")}
    }
    with ui.dialog() as dialog, section_card("swap_horiz", "Change scope"):
        opts = {"personal": "Personal", "project": "Project"}
        if is_curator:
            opts["global"] = "Global (two-gate release)"
        scope_sel = ui.select(opts, value=current, label="New scope").classes("w-full")
        # The destination project. Previously absent entirely, so "Project"
        # always sent no destination and always failed with "A project-scoped
        # model needs a destination project."
        project_sel = ui.select(
            project_opts or {},
            value=model.get("project_id") if model.get("project_id") in project_opts else None,
            label="Destination project *",
        ).classes("w-full")
        no_projects = ui.label(
            "You don't own or manage any project to move this model into. "
            "Create one, or ask its owner to add you."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

        def _refresh_project_visibility() -> None:
            wants_project = str(scope_sel.value or "") == "project"
            project_sel.set_visibility(wants_project and bool(project_opts))
            no_projects.set_visibility(wants_project and not project_opts)

        scope_sel.on_value_change(lambda _e=None: _refresh_project_visibility())
        _refresh_project_visibility()
        ui.label(
            "Promoting to global fits the model into the publish gate; moving "
            "between personal and project is immediate."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

        async def _apply() -> None:
            new_scope = str(scope_sel.value or current)
            if new_scope == "project" and not project_sel.value:
                ui.notify("Pick a destination project.", type="warning")
                return
            try:
                await client.set_model_scope(
                    mid,
                    scope=new_scope,
                    project_id=(
                        int(project_sel.value)
                        if new_scope == "project" and project_sel.value
                        else None
                    ),
                )
            except ApiError as exc:
                ui.notify(exc.detail, type="negative")
                return
            dialog.close()
            ui.notify("Scope updated.", type="positive")
            await refresh()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Apply", on_click=_apply).props("color=primary")
    dialog.open()


def _retire_dialog(client: ApiClient, model_id: int, refresh: Any) -> None:
    with ui.dialog() as dialog, section_card("archive", "Retire model"):
        ui.label(
            "Retiring withdraws the model from pickers. Existing surrogate-tool "
            "jobs keep working; this can't be undone."
        ).style("color: var(--matofx-muted-text);")

        async def _retire() -> None:
            try:
                await client.retire_model(model_id)
            except ApiError as exc:
                ui.notify(exc.detail, type="negative")
                return
            dialog.close()
            ui.notify("Model retired.", type="positive")
            await refresh()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Retire", on_click=_retire).props("color=negative")
    dialog.open()


def _export_dialog(client: ApiClient, model_id: int, refresh: Any) -> None:
    with ui.dialog() as dialog, section_card("build", "Export as tool"):
        ui.label(
            "Synthesise a runnable tool from this model's published version. It "
            "lands in the 'test' state for you to validate before promoting."
        ).style("color: var(--matofx-muted-text);")

        async def _export() -> None:
            try:
                result = await client.export_model_as_tool(model_id)
            except ApiError as exc:
                ui.notify(exc.detail, type="negative")
                return
            dialog.close()
            ui.notify(
                f"Created tool '{result.get('tool_alias')}' ({result.get('tool_state')}).",
                type="positive",
            )
            await refresh()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Export", on_click=_export).props("color=primary")
    dialog.open()
