"""Job Search (``/ui/job-search``) — find completed runs by their inputs.

Port of the legacy ``functions/job_search.py`` "find similar runs"
surface. Distinct from :mod:`my_jobs` (which is *navigation* —
Project/Scenario/Tool/Job pickers cascading into a workspace) and from
``/compare`` (which is *output overlay* — given N already-known jobs,
draw their CSV / table outputs side by side). Job Search is the
*discovery* layer in between: pick a tool, optionally filter by any
subset of its input values (with ± tolerance for numerics), see every
completed run that matches.

URL: ``/job-search`` — no query parameters; the page is always entered
fresh from the home-page button. The user picks a tool, fills in
filters, hits Apply.

Page shape:

1. Tool selector + Mode toggle (``All`` / ``Filtered``).
2. Filter card (revealed when Mode = Filtered + tool picked) — per-input
   widgets mirroring the submit-form widget shape, plus a numeric
   tolerance ``%`` input that applies to every numeric filter.
3. Results table — multi-row tick selection of completed runs.
4. Action row — **View inputs (N)** (≥1 ticked) and **Compare these
   (N)** (≥2 ticked, navigates to ``/compare``).
5. Inline tabs — one per ticked job, each showing a read-only key/value
   list of the job's inputs (grouped by descriptor) plus a
   **Use as starting point** button that opens a project/scenario
   picker and creates a draft job seeded with the source run's inputs
   (lands on ``/my-jobs`` opened on the new draft).
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_job_id, format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.scenario_naming import next_scenario_name
from matofx.ui.state import current_token

_NUMERIC_INPUT_TYPES: frozenset[str] = frozenset({"integer", "float"})
"""Descriptor input types that get the ± tolerance treatment server-side.

Mirrors ``_NUMERIC_INPUT_TYPES`` in :mod:`matofx.api.routers.tools`.
The page only uses the set to decide which widget to render and to
suppress the table-input filter (table inputs aren't filterable in v1).
"""

_FILTERABLE_TYPES: frozenset[str] = frozenset(
    {"string", "integer", "float", "boolean", "select", "multiselect", "file"}
)
"""Descriptor input types the search page knows how to filter on.

``table`` falls outside this set: filtering by a row-shaped value isn't
meaningful. Tools whose only inputs are tables surface a "no
filterable inputs" hint in the filter card.
"""


@ui.page("/job-search")
async def job_search_page(tool: str | None = None) -> None:
    """Render the Job Search page.

    ``tool`` is an optional ``?tool=<alias>`` deep-link (used by the global
    search 'Tools' results) that pre-selects that tool on load.
    """
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())

    # Mutable state shared across closures. The whole page is one
    # async function so closures can mutate freely without instance
    # ceremony — same shape used by the Create Job(s) chooser.
    state: dict[str, Any] = {
        "tools": [],  # list[dict] from list_tools()
        "tool_alias": None,  # currently picked tool's alias
        "descriptor": {},  # dict — current tool's descriptor JSON
        "filter_widgets": {},  # field_name → ui widget (read .value at search time)
        "ref_widgets": {},  # field_name → ui widget for the Similar-mode reference point
        "seed_job_id": None,  # int | None — seed run when Similar mode = "Existing job"
        "results": [],  # list[dict] — VisibleJobResponse rows from last search
        "results_inputs": {},  # job_id → dict | None (loaded on View inputs)
        "ticked_ids": set(),  # set[int] of selected job ids
        "show_closeness": False,  # render the Closeness column (Similar mode results)
        "weighting_note": "",  # how the last Similar search weighted the ranking
    }

    with ui.column().classes("w-full mx-auto p-6 gap-4").style("max-width: 1500px;"):
        ui.label("Job Search").style("font-size: 1.5rem; font-weight: 600;")
        ui.label(
            "Find completed runs of a tool by their input values. The page "
            "shows every match across the projects you can see; if a result "
            "lives in a project you don't have access to, the row would be "
            "hidden anyway by the underlying access checks."
        ).style("color: #888;")

        message = ui.label("").style("color: var(--matofx-error-text);")

        # ---------- Step 1: Tool + Mode --------------------------------
        with ui.card().classes("w-full"), ui.row().classes("w-full items-end gap-4 no-wrap"):
            tool_select = (
                ui.select(options={}, label="Tool", with_input=True)
                .props("dense")
                .style("flex: 1 1 0; min-width: 0;")
            )
            with ui.column().classes("gap-1").style("flex: 0 0 auto;"):
                ui.label("Mode").style("font-size: 0.75rem; color: #888;")
                mode_radio = ui.radio(["All", "Filtered", "Similar"], value="All").props(
                    "inline color=primary dense"
                )

        # ---------- Step 2: Filters expansion -------------------------
        filter_expansion = (
            ui.expansion("Filters", icon="filter_list", value=True)
            .classes("w-full")
            .props("header-class=text-weight-medium")
        )
        with filter_expansion:
            with ui.card().classes("w-full").style("background: rgba(255,255,255,0.03);"):
                ui.label("Numeric match tolerance").style("font-weight: 600;")
                ui.label(
                    "Set how far a stored value may differ from the number you "
                    "type and still count as a match. For example, at 25% a "
                    "filter of 100 matches any run between 75 and 125. This "
                    "applies to every numeric filter below."
                ).style("color: #888; font-size: 0.85rem;")
                tolerance_input = (
                    ui.number(
                        label="Numeric ± tolerance (%)",
                        value=25.0,
                        min=0.0,
                        max=1000.0,
                        step=1.0,
                    )
                    .props("dense")
                    .classes("w-full")
                    .style("max-width: 320px;")
                )
            with ui.card().classes("w-full").style("background: rgba(255,255,255,0.03);"):
                ui.label("Filter by input values").style("font-weight: 600;")
                ui.label(
                    "Fill in only the inputs you want to constrain and leave the "
                    "rest blank. A completed run has to match every field you "
                    "set to appear in the results; numeric fields match within "
                    "the tolerance above. Then click Apply filters."
                ).style("color: #888; font-size: 0.85rem;")
                filter_grid = ui.column().classes("w-full gap-2")
            with ui.row().classes("w-full justify-end gap-2"):
                reset_btn = ui.button("Reset filters", icon="refresh").props("flat dense")
                apply_btn = ui.button("Apply filters", icon="search").props("color=primary dense")
        filter_expansion.set_visibility(False)

        # ---------- Step 2b: Similar expansion ------------------------
        similar_expansion = (
            ui.expansion("Find similar runs", icon="travel_explore", value=True)
            .classes("w-full")
            .props("header-class=text-weight-medium")
        )
        with similar_expansion:
            ui.label(
                "Rank completed runs by how close their inputs are to a "
                "reference point. Numeric inputs are compared on a normalised "
                "scale; text / choice inputs must match exactly. When a recent "
                "study of this tool is available the ranking is weighted by "
                "each input's measured sensitivity, otherwise every input "
                "counts equally."
            ).style("color: #888; font-size: 0.85rem;")
            with ui.row().classes("items-center gap-2"):
                ui.label("Reference from").style("font-size: 0.75rem; color: #888;")
                seed_mode_radio = ui.radio(
                    ["Typed values", "Existing run"], value="Typed values"
                ).props("inline color=primary dense")
            seed_job_select = (
                ui.select(options={}, label="Existing run", with_input=True)
                .props("dense clearable")
                .classes("w-full")
            )
            seed_job_select.set_visibility(False)
            ref_card = ui.card().classes("w-full").style("background: rgba(255,255,255,0.03);")
            with ref_card:
                ui.label("Reference input values").style("font-weight: 600;")
                ui.label(
                    "Enter the inputs you want to rank against — you don't have "
                    "to fill in everything; leave the rest blank. MATOFX finds "
                    "the completed runs whose inputs are closest to what you "
                    "type, then click Find similar."
                ).style("color: #888; font-size: 0.85rem;")
                ref_grid = ui.column().classes("w-full gap-2")
            with ui.card().classes("w-full").style("background: rgba(255,255,255,0.03);"):
                ui.label("How many runs to return").style("font-weight: 600;")
                ui.label(
                    "Cap the number of closest-matching runs shown in the "
                    "results, ranked nearest first. Increase it to see more "
                    "of the long tail, up to 500."
                ).style("color: #888; font-size: 0.85rem;")
                top_n_input = (
                    ui.number(label="Max results", value=25, min=1, max=500, step=1)
                    .props("dense")
                    .classes("w-full")
                    .style("max-width: 320px;")
                )
            with ui.row().classes("w-full justify-end gap-2"):
                similar_reset_btn = ui.button("Reset reference", icon="refresh").props("flat dense")
                find_similar_btn = ui.button("Find similar", icon="travel_explore").props(
                    "color=primary dense"
                )
        similar_expansion.set_visibility(False)

        # ---------- Step 3: Results expansion -------------------------
        results_expansion = (
            ui.expansion("Results", icon="list", value=True)
            .classes("w-full")
            .props("header-class=text-weight-medium")
        )
        with results_expansion:
            results_summary = ui.label("").style("color: #888; font-size: 0.85rem;")
            results_list = (
                ui.column()
                .classes("w-full gap-1")
                .style(
                    "max-height: 420px; overflow-y: auto; "
                    "border: 1px solid rgba(255,255,255,0.15); "
                    "border-radius: 4px; padding: 0.25rem;"
                )
            )
            with ui.row().classes("w-full items-center gap-2"):
                view_btn = ui.button("View Jobs", icon="visibility").props(
                    "color=primary dense disable"
                )
                compare_btn = ui.button("Compare these", icon="compare").props(
                    "color=primary outline dense disable"
                )
                clear_btn = ui.button("Clear selection").props("flat dense")
        results_expansion.set_visibility(False)

        # ---------- Step 4: Inline tabs (View Jobs) -------------------
        tabs_card = ui.card().classes("w-full")
        with tabs_card:
            ui.label("Selected jobs").style("font-weight: 600;")
            tabs_container = ui.column().classes("w-full gap-2")
        tabs_card.set_visibility(False)

        # ---------- helpers --------------------------------------------

        async def _load_tools(preselect: str | None = None) -> None:
            try:
                tools = await client.list_tools()
            except ApiError as exc:
                message.text = f"Couldn't load tools: {exc.detail}"
                return
            state["tools"] = tools
            tool_select.options = {
                str(t.get("alias") or ""): _tool_label(t) for t in tools if t.get("alias")
            }
            tool_select.update()
            # Deep-link (?tool=<alias>): open ready on that tool. Setting the
            # value fires on_value_change → _on_tool_change, which loads the
            # descriptor and results just as a manual pick would.
            if preselect and preselect in tool_select.options:
                tool_select.value = preselect

        def _refresh_mode_surfaces() -> None:
            """Show / hide the Filters and Similar cards based on mode + tool.

            Each mode owns one card: ``Filtered`` reveals the Filters
            expansion, ``Similar`` reveals the Find-similar expansion,
            ``All`` reveals neither (results auto-load on tool pick). A
            card is only ever shown once a tool is picked so its
            per-input widgets have something to render.
            """
            has_tool = state["tool_alias"] is not None
            filtered = mode_radio.value == "Filtered" and has_tool
            similar = mode_radio.value == "Similar" and has_tool
            filter_expansion.set_visibility(filtered)
            similar_expansion.set_visibility(similar)
            if filtered:
                filter_expansion.value = True
            if similar:
                similar_expansion.value = True
                _refresh_seed_surfaces()

        async def _on_tool_change(_e: Any = None) -> None:
            alias = tool_select.value
            state["tool_alias"] = alias
            state["filter_widgets"] = {}
            state["ref_widgets"] = {}
            state["seed_job_id"] = None
            filter_grid.clear()
            ref_grid.clear()
            seed_job_select.options = {}
            seed_job_select.value = None
            seed_job_select.update()
            results_expansion.set_visibility(False)
            tabs_card.set_visibility(False)
            state["results"] = []
            state["ticked_ids"] = set()
            state["show_closeness"] = False
            results_list.clear()
            tabs_container.clear()
            message.text = ""
            if alias is None:
                _refresh_mode_surfaces()
                return
            # Load the descriptor up front — needed for filter / reference
            # rendering and the inputs-tab grouping. Cached on state so
            # we don't re-fetch on every Apply.
            try:
                tool_detail = await client.get_tool(alias)
            except ApiError as exc:
                message.text = f"Couldn't load the tool: {exc.detail}"
                state["descriptor"] = {}
                return
            descriptor = dict(tool_detail.get("descriptor") or {})
            state["descriptor"] = descriptor
            await _build_input_widgets(filter_grid, state["filter_widgets"])
            await _build_input_widgets(ref_grid, state["ref_widgets"])
            _refresh_mode_surfaces()
            # The existing-run picker was cleared above; if it's the active
            # reference source for the new tool, repopulate it now — the
            # seed-mode radio doesn't fire on a tool change, so nothing
            # else would reload the list (it'd stay empty otherwise).
            if mode_radio.value == "Similar" and seed_mode_radio.value == "Existing run":
                await _load_seed_jobs()
            # In All mode the page is meant to surface every visible
            # completed run as soon as a tool is picked — no extra
            # button click needed. Filtered / Similar modes wait for the
            # user to set their constraints / reference first.
            if mode_radio.value == "All":
                await _apply_filters()

        async def _on_mode_change(_e: Any = None) -> None:
            """Mode toggle: All → re-run with no filters; others → wait."""
            _refresh_mode_surfaces()
            if state["tool_alias"] is None:
                return
            if mode_radio.value == "All":
                await _apply_filters()
            elif (
                mode_radio.value == "Similar"
                and seed_mode_radio.value == "Existing run"
                and not seed_job_select.options
            ):
                # Entering Similar with the existing-run source already
                # selected (e.g. from a prior visit) — load its runs.
                await _load_seed_jobs()

        async def _build_input_widgets(grid: Any, store: dict[str, Any]) -> None:
            """Render one widget per filterable input into ``grid``.

            Shared by the Filtered card (``filter_grid`` / ``filter_widgets``)
            and the Similar card's typed-reference grid (``ref_grid`` /
            ``ref_widgets``): both want the same per-input widget shape, so
            the only difference is which grid + widget store they fill.
            """
            descriptor = state["descriptor"]
            inputs = list(descriptor.get("inputs") or [])
            if not inputs:
                with grid:
                    ui.label("This tool has no inputs to search on.").style(
                        "color: #888; font-size: 0.85rem;"
                    )
                return

            filterable = [inp for inp in inputs if str(inp.get("type") or "") in _FILTERABLE_TYPES]
            if not filterable:
                with grid:
                    ui.label(
                        "This tool's inputs aren't searchable (table inputs "
                        "aren't supported in search yet)."
                    ).style("color: #888; font-size: 0.85rem;")
                return

            # Two-column layout to keep the card compact.
            with grid, ui.row().classes("w-full gap-3 items-start"):
                left = ui.column().classes("gap-2").style("flex: 1 1 0; min-width: 0;")
                right = ui.column().classes("gap-2").style("flex: 1 1 0; min-width: 0;")
            for index, inp in enumerate(filterable):
                target = left if index % 2 == 0 else right
                with target:
                    widget = await _make_filter_widget(inp)
                if widget is not None:
                    store[str(inp["name"])] = widget

        async def _make_filter_widget(inp: dict[str, Any]) -> Any:
            """Build the one filter widget for descriptor field ``inp``.

            Returns the widget so :func:`_apply_filters` can read its
            ``.value`` at search time. Each widget defaults to the
            "no constraint" empty value, regardless of the descriptor's
            declared default — a search filter pre-populated with the
            descriptor default would silently constrain the search,
            which isn't what the user means.
            """
            label = _input_label_with_unit(inp)
            kind = str(inp.get("type") or "")
            if kind in _NUMERIC_INPUT_TYPES:
                return (
                    ui.number(label=label, value=None, step=inp.get("precision") or 1)
                    .props("dense clearable")
                    .classes("w-full")
                )
            if kind == "boolean":
                # Tri-state via select: blank = no filter; True / False = literal match.
                return (
                    ui.select(
                        options={"": "(any)", "True": "True", "False": "False"},
                        label=label,
                        value="",
                    )
                    .props("dense")
                    .classes("w-full")
                )
            if kind == "select":
                options = list(inp.get("options") or [])
                return (
                    ui.select(options=["", *options], label=label, value="")
                    .props("dense clearable")
                    .classes("w-full")
                )
            if kind == "multiselect":
                options = list(inp.get("options") or [])
                return (
                    ui.select(options=options, label=label, multiple=True, value=[])
                    .props("dense clearable")
                    .classes("w-full")
                )
            if kind == "file":
                # File dropdown built from previously-used paths for this
                # field — port of the legacy "Choose file" select. Async
                # because it round-trips the distinct-values endpoint.
                try:
                    distinct = await client.list_distinct_input_values(
                        str(state["tool_alias"]), field=str(inp["name"])
                    )
                except ApiError:
                    distinct = []
                file_options: dict[str, str] = {"": "(any)"}
                for value in distinct:
                    file_options[str(value)] = str(value)
                return (
                    ui.select(options=file_options, label=label, value="")
                    .props("dense clearable")
                    .classes("w-full")
                )
            # string fallback — and a defensive default for anything we
            # haven't enumerated.
            return ui.input(label=label).props("dense clearable").classes("w-full")

        def _read_filter_values(widgets: dict[str, Any] | None = None) -> dict[str, Any]:
            """Snapshot a widget store into the API payload shape.

            Defaults to the Filtered card's ``filter_widgets``; the
            Similar card passes its ``ref_widgets`` to read the typed
            reference point through the same coercion rules.

            Empty strings, ``None``, and empty lists are dropped — the
            server treats those as "no constraint" anyway, but pre-
            stripping keeps the network payload small. Boolean
            tri-state strings ``"True"`` / ``"False"`` are coerced to
            real booleans here so the equality match works against the
            Python ``True`` / ``False`` stored on the job.
            """
            descriptor = state["descriptor"]
            inputs_meta = {
                str(inp.get("name") or ""): inp for inp in (descriptor.get("inputs") or [])
            }
            out: dict[str, Any] = {}
            store = widgets if widgets is not None else state["filter_widgets"]
            for name, widget in store.items():
                meta = inputs_meta.get(name) or {}
                kind = str(meta.get("type") or "")
                value = getattr(widget, "value", None)
                if _is_empty_filter_value(value):
                    continue
                if kind == "boolean":
                    if value == "True":
                        value = True
                    elif value == "False":
                        value = False
                    else:
                        continue
                out[name] = value
            return out

        async def _apply_filters() -> None:
            """Run a search and re-render the results panel.

            On success collapses the Filters expansion so the Results
            panel takes centre stage; the user can re-expand the
            header to refine and Apply again.
            """
            if state["tool_alias"] is None:
                message.text = "Pick a tool first."
                return
            filters = _read_filter_values() if mode_radio.value == "Filtered" else {}
            tol = float(tolerance_input.value or 25.0)
            try:
                rows = await client.search_jobs_by_inputs(
                    str(state["tool_alias"]),
                    filters=filters,
                    numeric_tolerance_pct=tol,
                )
            except ApiError as exc:
                message.text = f"Search failed: {exc.detail}"
                return
            message.text = ""
            state["results"] = rows
            state["results_inputs"] = {}
            state["ticked_ids"] = set()
            state["show_closeness"] = False
            tabs_card.set_visibility(False)
            tabs_container.clear()
            _render_results_list()
            results_expansion.set_visibility(True)
            results_expansion.value = True
            # Collapse the Filters expansion (only meaningful in
            # Filtered mode — All mode keeps it hidden anyway).
            if mode_radio.value == "Filtered":
                filter_expansion.value = False
            _refresh_action_buttons()

        # ---------- Similar-mode helpers -------------------------------

        def _refresh_seed_surfaces() -> None:
            """Toggle the typed-reference grid vs the seed-run picker.

            ``Typed values`` reveals the per-input reference widgets;
            ``Existing run`` reveals the completed-run dropdown instead.
            Only ever called while the Similar card is visible.
            """
            typed = seed_mode_radio.value == "Typed values"
            ref_card.set_visibility(typed)
            seed_job_select.set_visibility(not typed)

        async def _on_seed_mode_change(_e: Any = None) -> None:
            _refresh_seed_surfaces()
            if seed_mode_radio.value == "Existing run" and not seed_job_select.options:
                await _load_seed_jobs()

        async def _load_seed_jobs() -> None:
            """Populate the seed-run dropdown with the tool's completed runs.

            Reuses the boolean Job Search endpoint with no filters to get
            the ACL-correct set of completed runs, then labels each by job
            id + name so the user can pick the run to rank against.
            """
            alias = state["tool_alias"]
            if alias is None:
                return
            try:
                rows = await client.search_jobs_by_inputs(str(alias), filters={})
            except ApiError as exc:
                message.text = f"Couldn't load existing runs: {exc.detail}"
                return
            options: dict[int, str] = {}
            for row in rows:
                jid = int(row["id"])
                name = str(row.get("name") or "—")
                options[jid] = f"{format_job_id(jid)} — {name}"
            seed_job_select.options = options
            seed_job_select.update()

        async def _apply_similar() -> None:
            """Run a find-similar search and re-render the results panel.

            Resolves the reference from the active seed sub-mode (typed
            values or an existing run), calls the ranking endpoint, and
            renders the ranked rows with a Closeness column plus a note
            saying how "similar" was weighted.
            """
            if state["tool_alias"] is None:
                message.text = "Pick a tool first."
                return
            typed = seed_mode_radio.value == "Typed values"
            reference_inputs = _read_filter_values(state["ref_widgets"]) if typed else {}
            reference_job_id = None if typed else seed_job_select.value
            if typed and not reference_inputs:
                message.text = "Enter at least one reference value to rank against."
                return
            if not typed and reference_job_id is None:
                message.text = "Pick a run to find similar runs to."
                return
            top_n = int(top_n_input.value or 25)
            seed_id = int(reference_job_id) if reference_job_id is not None else None
            try:
                payload = await client.find_similar_jobs(
                    str(state["tool_alias"]),
                    reference_inputs=reference_inputs,
                    reference_job_id=seed_id,
                    top_n=top_n,
                )
            except ApiError as exc:
                message.text = f"Find similar failed: {exc.detail}"
                return
            message.text = ""
            state["results"] = list(payload.get("results") or [])
            state["results_inputs"] = {}
            state["ticked_ids"] = set()
            state["show_closeness"] = True
            state["weighting_note"] = _weighting_note(payload)
            tabs_card.set_visibility(False)
            tabs_container.clear()
            _render_results_list()
            results_expansion.set_visibility(True)
            results_expansion.value = True
            similar_expansion.value = False
            _refresh_action_buttons()

        def _reset_reference() -> None:
            _reset_widgets(state["ref_widgets"])
            seed_job_select.value = None
            seed_job_select.update()

        def _render_results_list() -> None:
            """Replace the rows in the results scroll panel.

            Each result is a ``ui.row`` carrying a tickbox + a few
            label columns. Sticking with row-of-checkboxes (rather than
            ``ui.table`` with selection="multiple") gives us full
            control over what 'Use as starting point' surfaces later
            and matches the Create Job(s) chooser style.
            """
            results_list.clear()
            rows = state["results"]
            show_closeness = state["show_closeness"]
            if not rows:
                results_summary.text = (
                    "No completed runs to rank — try a looser reference."
                    if show_closeness
                    else "No completed jobs match the current filters."
                )
                return
            if show_closeness:
                results_summary.text = (
                    f"{len(rows)} run{'s' if len(rows) != 1 else ''}, closest first. "
                    f"{state['weighting_note']}"
                )
            else:
                results_summary.text = (
                    f"{len(rows)} completed job{'s' if len(rows) != 1 else ''} match."
                )
            with results_list:
                # Header strip so users can read the columns.
                with (
                    ui.row()
                    .classes("w-full items-center gap-2 no-wrap")
                    .style(
                        "padding: 0.25rem 0.5rem; font-weight: 600; font-size: 0.8rem; color: #888;"
                    )
                ):
                    ui.label("").style("flex: 0 0 28px;")
                    if show_closeness:
                        ui.label("Closeness").style("flex: 0 0 110px;")
                    ui.label("Job ID").style("flex: 0 0 110px;")
                    ui.label("Name").style("flex: 1 1 0; min-width: 0;")
                    ui.label("Project").style("flex: 1 1 0; min-width: 0;")
                    ui.label("Scenario").style("flex: 1 1 0; min-width: 0;")
                    ui.label("Submitter").style("flex: 0 0 160px;")
                    ui.label("Created").style("flex: 0 0 240px;")
                for row in rows:
                    _render_result_row(row)

        def _render_result_row(row: dict[str, Any]) -> None:
            job_id = int(row["id"])
            with (
                ui.row()
                .classes("w-full items-center gap-2 no-wrap")
                .style("padding: 0.25rem 0.5rem;")
            ):
                cb = ui.checkbox(
                    value=job_id in state["ticked_ids"],
                ).style("flex: 0 0 28px;")

                def _on_check(_e: Any = None, _id: int = job_id, _cb: Any = cb) -> None:
                    if _cb.value:
                        state["ticked_ids"].add(_id)
                    else:
                        state["ticked_ids"].discard(_id)
                    _refresh_action_buttons()

                cb.on_value_change(_on_check)
                if state["show_closeness"]:
                    ui.label(_closeness_label(row.get("closeness_pct"))).style(
                        "flex: 0 0 110px; font-weight: 600;"
                    ).tooltip(
                        "How close this run's inputs are to the reference "
                        "(100% = an exact match on every weighted input)."
                    )
                ui.label(format_job_id(job_id)).style("flex: 0 0 110px;")
                ui.label(str(row.get("name") or "—")).style("flex: 1 1 0; min-width: 0;")
                ui.label(str(row.get("project_name") or "—")).style("flex: 1 1 0; min-width: 0;")
                ui.label(str(row.get("scenario_name") or "—")).style("flex: 1 1 0; min-width: 0;")
                ui.label(str(row.get("submitter_name") or "—")).style(
                    "flex: 0 0 160px; font-size: 0.85rem;"
                )
                ui.label(format_user_datetime(row.get("created_at"))).style(
                    "flex: 0 0 240px; font-size: 0.85rem;"
                )

        def _refresh_action_buttons() -> None:
            n = len(state["ticked_ids"])
            if n >= 1:
                view_btn.props(remove="disable")
                view_btn.text = f"View Jobs ({n})"
            else:
                view_btn.props("disable color=primary dense")
                view_btn.text = "View Jobs"
            if n >= 2:
                compare_btn.props(remove="disable")
                compare_btn.text = f"Compare these ({n})"
            else:
                compare_btn.props("disable color=primary outline dense")
                compare_btn.text = "Compare these"

        def _clear_selection() -> None:
            state["ticked_ids"] = set()
            tabs_card.set_visibility(False)
            tabs_container.clear()
            _render_results_list()
            _refresh_action_buttons()

        async def _on_view_jobs() -> None:
            """Reveal the per-job tabs for every ticked row.

            Each tab renders the same Inputs + Outputs + downloads
            layout as the My Jobs workspace's complete-state view —
            we delegate to :func:`matofx.ui.pages.job._render_body`
            so the two surfaces stay byte-for-byte consistent. A
            per-tab "Use as starting point" button sits at the top
            so the user can pick any one of the ticked jobs as the
            seed for a new draft.

            On reveal, collapses the Results expansion so the per-job
            view dominates the screen; the user can re-expand the
            header to change the selection.
            """
            ticked = sorted(state["ticked_ids"])
            if not ticked:
                return
            tabs_container.clear()
            with tabs_container, ui.tabs().classes("w-full") as tabs:
                tab_handles: list[Any] = []
                for jid in ticked:
                    tab_handles.append(ui.tab(format_job_id(jid)))
            with tabs_container, ui.tab_panels(tabs, value=tab_handles[0]).classes("w-full"):
                for jid, tab_handle in zip(ticked, tab_handles, strict=True):
                    with ui.tab_panel(tab_handle):
                        await _render_job_view_panel(jid)
            tabs_card.set_visibility(True)
            results_expansion.value = False

        async def _render_job_view_panel(job_id: int) -> None:
            """One per-job tab: copy-action button on top + the legacy body.

            Delegates the Inputs + Outputs + downloads rendering to
            :func:`matofx.ui.pages.job._render_body` so the layout
            matches what My Jobs shows for a complete job. The
            ``_render_body`` import is local to keep this module's
            top-level imports lean and to avoid an import cycle if
            ``job.py`` ever needs to call into job-search helpers.
            """
            from matofx.ui.pages.job import _render_body  # noqa: PLC0415

            with ui.column().classes("w-full gap-3") as panel:
                with ui.row().classes("w-full items-center justify-end"):
                    ui.button(
                        "Use as starting point",
                        icon="content_copy",
                        on_click=lambda _e=None, _id=job_id: _open_copy_dialog(_id),
                    ).props("color=primary dense").tooltip(
                        "Open a project + scenario picker, then create a "
                        "new draft job seeded with this run's inputs."
                    )
                body_container = ui.column().classes("w-full gap-3")
                try:
                    job = await client.get_job(job_id)
                except ApiError as exc:
                    ui.label(f"Couldn't load this job: {exc.detail}").style(
                        "color: var(--matofx-error-text);"
                    )
                    return
                # Cache the inputs so the copy dialog can seed without
                # a second round-trip.
                state["results_inputs"][job_id] = dict(job.get("inputs") or {})
                await _render_body(client, job, job_id, body_container)
            _ = panel  # retained for the with-statement context

        def _open_copy_dialog(source_job_id: int) -> None:
            """Pick a project + scenario, then create a seeded draft.

            Reuses the same scenario-filter rule as Create Job(s): only
            scenarios that don't already host this tool are offered, so
            the seeded draft can land without colliding with an
            existing draft (the draft endpoint is idempotent and would
            silently keep the existing inputs).
            """
            tool_alias = str(state["tool_alias"] or "")
            tool_meta = next(
                (t for t in state["tools"] if str(t.get("alias")) == tool_alias),
                None,
            )
            tool_label = _tool_label(tool_meta)

            with ui.dialog() as dialog, ui.card().style("min-width: 520px; max-width: 90vw;"):
                ui.label(f"Use job {format_job_id(source_job_id)} as a starting point").style(
                    "font-size: 1.1rem; font-weight: 600;"
                )
                ui.label(
                    f"Pick the project and scenario for the new {tool_label} "
                    "draft. Scenarios already running this tool are hidden — "
                    "click 'New scenario' to add one."
                ).style("color: #888;")

                project_select = (
                    ui.select(options={}, label="Project", with_input=True)
                    .props("dense")
                    .classes("w-full")
                )
                scenario_select = (
                    ui.select(options={}, label="Scenario", with_input=True)
                    .props("disable dense")
                    .classes("w-full")
                )
                no_scenarios_hint = ui.label("").style("color: #888; font-size: 0.85rem;")
                no_scenarios_hint.set_visibility(False)
                new_scenario_btn = ui.button("New scenario", icon="add").props(
                    "flat dense color=primary disable"
                )
                new_scenario_panel = ui.row().classes("w-full items-end gap-2")
                new_scenario_panel.set_visibility(False)
                with new_scenario_panel:
                    new_scenario_name = (
                        ui.input("New scenario name", value="Scenario 1")
                        .props("dense")
                        .classes("col")
                        .style("flex: 1 1 0;")
                    )
                    create_scenario_btn = ui.button("Create").props("color=primary dense")
                    cancel_new_btn = ui.button("Cancel").props("flat dense")

                dialog_message = ui.label("").style("color: var(--matofx-error-text);")

                with ui.row().classes("w-full justify-between items-center"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    create_btn = (
                        ui.button("Create draft").props("color=primary disable").classes("w-40")
                    )

                # All scenario names in the picked project — the dropdown only
                # shows the ones not already running this tool, so we keep the
                # full list to suggest the next sequential "New scenario" name.
                scenario_names: list[str] = []

                async def _load_projects() -> None:
                    try:
                        projects = await client.list_projects()
                    except ApiError as exc:
                        dialog_message.text = exc.detail
                        return
                    project_select.options = {int(p["id"]): str(p["name"]) for p in projects}
                    project_select.update()

                async def _on_project_change(_e: Any = None) -> None:
                    scenario_select.value = None
                    scenario_select.options = {}
                    scenario_select.props("disable dense")
                    new_scenario_btn.props("disable")
                    _hide_new_panel()
                    no_scenarios_hint.set_visibility(False)
                    create_btn.props("disable color=primary")
                    if project_select.value is None:
                        scenario_select.update()
                        return
                    try:
                        scenarios = await client.list_scenarios(int(project_select.value))
                    except ApiError as exc:
                        dialog_message.text = exc.detail
                        return
                    available: dict[int, str] = {}
                    for scenario in scenarios:
                        sid = int(scenario["id"])
                        try:
                            jobs = await client.list_jobs(sid)
                        except ApiError:
                            jobs = []
                        if any(str(j.get("tool_alias")) == tool_alias for j in jobs):
                            continue
                        available[sid] = str(scenario.get("name") or "—")
                    scenario_names[:] = [str(s.get("name") or "") for s in scenarios]
                    new_scenario_name.value = next_scenario_name(scenario_names)
                    scenario_select.options = available
                    new_scenario_btn.props(remove="disable")
                    if available:
                        scenario_select.props(remove="disable")
                    else:
                        no_scenarios_hint.text = (
                            "Every scenario in this project already has a run "
                            "of this tool. Click 'New scenario' to add one."
                        )
                        no_scenarios_hint.set_visibility(True)
                    scenario_select.update()

                def _show_new_panel() -> None:
                    new_scenario_panel.set_visibility(True)
                    scenario_select.value = None
                    scenario_select.props("disable dense")
                    scenario_select.update()
                    _refresh_create_state()

                def _hide_new_panel() -> None:
                    new_scenario_panel.set_visibility(False)
                    new_scenario_name.value = next_scenario_name(scenario_names)
                    if project_select.value is not None and scenario_select.options:
                        scenario_select.props(remove="disable")
                    scenario_select.update()
                    _refresh_create_state()

                async def _create_scenario_inline() -> None:
                    name = (new_scenario_name.value or "").strip()
                    if not name:
                        dialog_message.text = "Please enter a scenario name."
                        return
                    pid = project_select.value
                    if pid is None:
                        dialog_message.text = "Pick a project first."
                        return
                    try:
                        scenario = await client.create_scenario(int(pid), name=name, description="")
                    except ApiError as exc:
                        dialog_message.text = exc.detail
                        return
                    sid = int(scenario["id"])
                    _hide_new_panel()
                    # Re-load scenarios so the new one appears in the
                    # dropdown, then auto-select it.
                    try:
                        scenarios = await client.list_scenarios(int(pid))
                    except ApiError as exc:
                        dialog_message.text = exc.detail
                        return
                    scenario_names[:] = [str(s.get("name") or "") for s in scenarios]
                    new_scenario_name.value = next_scenario_name(scenario_names)
                    scenario_select.options = {
                        int(s["id"]): str(s.get("name") or "—") for s in scenarios
                    }
                    scenario_select.props(remove="disable")
                    scenario_select.value = sid
                    scenario_select.update()

                def _refresh_create_state() -> None:
                    if new_scenario_panel.visible:
                        ok = bool((new_scenario_name.value or "").strip())
                    else:
                        ok = scenario_select.value is not None
                    if ok:
                        create_btn.props(remove="disable")
                    else:
                        create_btn.props("disable color=primary")

                async def _create_draft() -> None:
                    sid = scenario_select.value
                    if sid is None:
                        dialog_message.text = "Please pick a scenario."
                        return
                    seed_inputs = state["results_inputs"].get(source_job_id) or {}
                    if not seed_inputs:
                        # Fall back to a fresh fetch in case the user
                        # opened the dialog before View Inputs ran.
                        try:
                            job = await client.get_job(source_job_id)
                            seed_inputs = dict(job.get("inputs") or {})
                            state["results_inputs"][source_job_id] = seed_inputs
                        except ApiError as exc:
                            dialog_message.text = exc.detail
                            return
                    create_btn.props("disable color=primary")
                    create_btn.text = "Creating…"
                    try:
                        new_job = await client.create_or_get_draft_job(
                            int(sid),
                            tool_alias=tool_alias,
                            inputs=seed_inputs,
                        )
                    except ApiError as exc:
                        dialog_message.text = exc.detail
                        create_btn.props(remove="disable")
                        create_btn.text = "Create draft"
                        return
                    dialog.close()
                    ui.navigate.to(f"/my-jobs?job={int(new_job['id'])}")

                project_select.on_value_change(_on_project_change)
                scenario_select.on_value_change(lambda _e: _refresh_create_state())
                new_scenario_name.on_value_change(lambda _e: _refresh_create_state())
                new_scenario_btn.on_click(_show_new_panel)
                create_scenario_btn.on_click(_create_scenario_inline)
                cancel_new_btn.on_click(_hide_new_panel)
                create_btn.on_click(_create_draft)

                ui.timer(0.05, _load_projects, once=True)
            dialog.open()

        def _on_compare() -> None:
            ticked = sorted(state["ticked_ids"])
            if len(ticked) < 2:
                return
            ids_param = ",".join(str(i) for i in ticked)
            ui.navigate.to(f"/compare?mode=tool&jobs={ids_param}")

        def _reset_widgets(widgets: dict[str, Any]) -> None:
            for widget in widgets.values():
                kind = type(widget).__name__
                if kind == "Number":
                    widget.value = None
                elif kind == "Select":
                    widget.value = [] if getattr(widget, "_props", {}).get("multiple") else ""
                elif kind == "Input":
                    widget.value = ""
                elif kind == "Checkbox":
                    widget.value = False

        def _reset_filters() -> None:
            _reset_widgets(state["filter_widgets"])

        # ---------- wire events ---------------------------------------
        tool_select.on_value_change(_on_tool_change)
        mode_radio.on_value_change(_on_mode_change)
        apply_btn.on_click(_apply_filters)
        reset_btn.on_click(_reset_filters)
        seed_mode_radio.on_value_change(_on_seed_mode_change)
        find_similar_btn.on_click(_apply_similar)
        similar_reset_btn.on_click(_reset_reference)
        view_btn.on_click(_on_view_jobs)
        compare_btn.on_click(_on_compare)
        clear_btn.on_click(_clear_selection)

        ui.timer(0.05, lambda: _load_tools(tool), once=True)


# ---------- Pure helpers (testable in isolation) ----------------------


def _tool_label(tool: dict[str, Any] | None) -> str:
    """Pretty `name vX.Y.Z` label per the locked formatting rule."""
    if tool is None:
        return ""
    name = str(tool.get("name") or tool.get("alias") or "")
    version = str(tool.get("version") or "")
    return f"{name} v{version}" if version else name


def _is_empty_filter_value(value: Any) -> bool:
    """``True`` when the filter widget effectively has no constraint set.

    Treats ``None`` / empty string / empty list as "no constraint";
    leaves numeric ``0`` and boolean ``False`` alone — both are valid
    filter values the user might genuinely want to match against.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, list):
        return len(value) == 0
    return False


def _input_label_with_unit(inp: dict[str, Any]) -> str:
    label = str(inp.get("label") or inp.get("name") or "")
    unit = str(inp.get("unit") or "")
    if unit:
        return f"{label} ({unit})"
    return label


def _closeness_label(closeness_pct: Any) -> str:
    """Render a ``closeness_pct`` value as a whole-percent string.

    Defensive against a missing / non-numeric value (``"—"``) so a
    malformed row never blanks the column.
    """
    if not isinstance(closeness_pct, (int, float)) or isinstance(closeness_pct, bool):
        return "—"
    return f"{round(float(closeness_pct))}%"


def _weighting_note(payload: dict[str, Any]) -> str:
    """One-line description of how a find-similar ranking was weighted.

    Reads the ``weighting`` / ``weighting_source`` fields of the
    ``job-search/similar`` response and turns them into the human note
    shown under the results table — so the user knows whether "similar"
    was measured by a study's sensitivities or by equal weighting.
    """
    if str(payload.get("weighting") or "") == "sensitivity":
        source = str(payload.get("weighting_source") or "").strip()
        if source:
            return f"Ranked by input sensitivity from {source}."
        return "Ranked by input sensitivity."
    return "Ranked with equal weighting (no recent study found for this tool)."
