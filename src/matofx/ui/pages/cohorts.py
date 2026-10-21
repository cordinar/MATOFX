"""Job Cohorts UI (ADR-0008 §3 / surrogate §12).

Two surfaces, both behind the ``features.surrogate_family`` flag (the
nav entry + these pages are absent when it's off, matching the API which
404s every route — a disabled-feature visit redirects home):

- ``GET /cohorts`` — the workspace: every cohort + the builder (pick a
  tool → preview its completed runs across the whole workspace and all
  sources → keep the query live (auto-grow) or tick a fixed set). Curate-
  gated; the builder is shown only to ``cohorts.curate`` holders (D6).
- ``GET /cohorts/{id}`` — the cohort detail: its definition, the live /
  fixed membership (with the "(N hidden by access controls)" footnote),
  and the lazily-populated member results matrix (§3.3). An *active*
  cohort can be archived (no-delete, D20).

A cohort is a workspace-level object keyed on one tool (D5); membership
follows ``auto_grow`` (D4) and is always intersected with the caller's
per-project ACL (D7). The Results explorer parity the parametric studies
surface has is a later slice — this v1 shows the member results matrix
(job + scalars) directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Callable, Mapping
from typing import Any

from nicegui import ui

from matofx.studies.coverage import snap_to_step
from matofx.ui._coverage_panel import (
    PAIR_COLUMNS_NOTE,
    arrangement_note,
    axis_levels,
    decimals_for,
    envelope_rows,
    factor_labels,
    factor_table_rows,
    fill_scope_caveats,
    format_range,
    format_value,
    grid_axes,
    headline,
    histogram_counts,
    level_counts,
    measured_over_note,
    nice_bins,
    pair_abstain_note,
    pair_abstains,
    pair_chip,
    pair_legend,
    pair_spread_chip,
    pair_spread_header,
    pairs_note,
    plan_gap_note,
    plan_today_line,
    provenance_note,
    strip_positions,
    suggestion_columns,
    unmovable_note,
    values_for,
    verdict_chip,
    with_pinned_values,
)
from matofx.ui._features import design_space_coverage_enabled, surrogate_family_enabled
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in, section_card
from matofx.ui.state import current_token, has_permission

_COHORTS_CURATE = "cohorts.curate"
_COHORTS_CREATE = "cohorts.create"
# Fitting a surrogate *from* a cohort is a models right, not a cohorts
# one — the same pair the Models page gates its fit form on.
_MODELS_CURATE = "models.curate"
_MODELS_CREATE = "models.create"


def _client() -> ApiClient:
    return ApiClient(get_app(), token=current_token())


def _feature_ok() -> bool:
    """Redirect home when the surrogate-family feature is off (page *absent*)."""
    if not surrogate_family_enabled():
        ui.navigate.to("/home")
        return False
    return True


def _state_chip(state: str) -> None:
    color = "green" if state == "active" else "grey"
    ui.chip(state.title()).props(f"color={color} text-color=white")


# ===================== workspace (list + create) =====================


@ui.page("/cohorts")
async def cohorts_page() -> None:
    """The Job Cohorts workspace — your cohorts + the create form."""
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    with ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;"):
        ui.label("Job Cohorts").style(
            "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
        )
        ui.label(
            "Gather past completed runs of one tool into a live, refreshing dataset "
            "you can build a surrogate on."
        ).style("color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;")
        # Creating a cohort needs cohorts.create (or the curate capability —
        # curators can always create). Without it there's nothing to tab
        # between, so a viewer keeps the plain catalogue list.
        if has_permission(_COHORTS_CREATE) or has_permission(_COHORTS_CURATE):
            with (
                ui.tabs()
                .classes("w-full")
                .props("align=left active-color=primary indicator-color=primary")
                .style("border-bottom: 1px solid var(--matofx-card-border);") as tabs
            ):
                catalogue_tab = ui.tab("Catalogue", icon="grid_view").props("no-caps")
                new_tab = ui.tab("New Job Cohort", icon="add").props("no-caps")
            with (
                ui.tab_panels(tabs, value=catalogue_tab)
                .classes("w-full")
                .style("background-color: transparent;")
            ):
                with ui.tab_panel(catalogue_tab).style("background-color: transparent;"):
                    await _render_your_cohorts(client)
                with ui.tab_panel(new_tab).style("background-color: transparent;"):
                    await _render_create_form(client)
        else:
            await _render_your_cohorts(client)


async def _render_your_cohorts(client: ApiClient) -> None:
    """A compact list of every cohort (workspace-wide; open read)."""
    with section_card("grid_view", "Cohorts"):
        try:
            rows = await client.list_cohorts()
        except ApiError as exc:
            ui.label(f"Couldn't load cohorts: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return
        if not rows:
            ui.label("No cohorts yet.").style("color: var(--matofx-muted-text);")
            return
        rows.sort(key=lambda c: str(c.get("created_at") or ""), reverse=True)
        for cohort in rows[:50]:
            _render_cohort_row(cohort)


def membership_badge(cohort: dict[str, Any]) -> tuple[str, str]:
    """The membership chip + its tooltip for one cohort row.

    Three states, not two. A cohort that is *not* auto-growing but has
    commissioned studies attached still unions in their completed runs at
    resolve time (ADR-0020 §11.5 / migration 0096), so a bare "Fixed set"
    is a promise the resolver does not keep — the run count moves after
    the curator was told it wouldn't. The detail page already explains
    this; the list row used to flatten it away.
    """
    if bool(cohort.get("auto_grow", True)):
        return (
            "Auto-growing",
            "Live membership — new completed runs that match join automatically.",
        )
    commissioned = len(cohort.get("auto_include_study_ids") or [])
    if commissioned:
        return (
            "Fixed + commissioned",
            "Frozen to a picked set of runs, plus the completed runs of "
            f"{commissioned} study(ies) this cohort commissioned — those still "
            "arrive as they finish.",
        )
    return ("Fixed set", "Fixed membership — a frozen, hand-picked set of runs.")


def _render_cohort_row(cohort: dict[str, Any]) -> None:
    cid = int(cohort.get("id") or 0)
    name = str(cohort.get("name") or f"Cohort #{cid}")
    # Tool label, not alias ([[feedback_use_labels_not_names]]) — fall back
    # to the alias only if the label didn't resolve.
    tool = str(cohort.get("tool_name") or cohort.get("tool_alias") or "")
    state = str(cohort.get("state") or "active")
    grow_label, grow_tip = membership_badge(cohort)
    count = cohort.get("member_count")
    creator = str(cohort.get("created_by_name") or "")
    with (
        ui.row()
        .classes("w-full items-center gap-3 p-2 rounded cursor-pointer")
        .style("background-color: var(--matofx-row-bg);")
        .on("click", lambda _e=None, _id=cid: ui.navigate.to(f"/cohorts/{_id}"))
    ):
        with ui.column().classes("gap-0 flex-grow").style("min-width: 0;"):
            ui.label(name).style("font-weight: 600;")
            meta_bits = [tool] if tool else []
            if count is not None:
                meta_bits.append(f"{count} run{'s' if count != 1 else ''}")
            if creator:
                meta_bits.append(f"by {creator}")
            ui.label(" · ".join(meta_bits)).style(
                "color: var(--matofx-muted-text); font-size: 0.8rem;"
            )
        ui.label(grow_label).style("color: var(--matofx-muted-text); font-size: 0.75rem;").tooltip(
            grow_tip
        )
        _state_chip(state)


_SOURCE_LABELS = {
    "scenario": "Standalone",
    "workflow": "Workflow",
    "parametric_study": "Parametric study",
    "optimisation_study": "Optimisation",
}

_PREVIEW_COLUMNS = [
    {"name": "job_id", "label": "Run", "field": "job_id", "align": "left"},
    {"name": "project", "label": "Project", "field": "project", "align": "left"},
    {"name": "source", "label": "Source", "field": "source", "align": "left"},
    # The kind alone does not identify a run's origin: an archive with several
    # studies showed identical "Parametric study" rows, so picking runs out of
    # one of them meant cross-referencing job ids by hand.
    {"name": "source_name", "label": "Study", "field": "source_name", "align": "left"},
    {"name": "owner", "label": "Launched by", "field": "owner", "align": "left"},
    {"name": "completed", "label": "Completed", "field": "completed", "align": "left"},
]


class _CohortBuilder:
    """Shared create/edit form: tool → version bands → filters → preview → save.

    ``existing`` (a CohortResponse dict) switches it to edit mode: the tool
    is fixed and every field is pre-filled. The version picker offers
    ``major.minor`` bands (patches included); the advanced-filters panel
    offers per-input min/max ranges and a completed-date window.
    """

    def __init__(
        self,
        client: ApiClient,
        project_names: dict[int, str],
        *,
        existing: dict[str, Any] | None = None,
    ) -> None:
        self.client = client
        self.project_names = project_names
        self.existing = existing
        self.tool_alias: str | None = str(existing["tool_alias"]) if existing else None
        self.bands: list[dict[str, Any]] = []
        self.numeric_inputs: list[dict[str, Any]] = []
        self.range_inputs: dict[str, tuple[Any, Any]] = {}
        self.table: Any = None
        # Elements assigned in render().
        self.name_in: Any = None
        self.desc_in: Any = None
        self.version_sel: Any = None
        self.auto_grow: Any = None
        self.after_in: Any = None
        self.before_in: Any = None
        self.owner_sel: Any = None
        self.source_sel: Any = None
        self.parametric_sel: Any = None
        self.optimisation_sel: Any = None
        self.user_names: dict[int, str] = {}
        self.study_row: Any = None
        self.parametric_names: dict[int, str] = {}
        self.optimisation_names: dict[int, str] = {}
        self.meta_box: Any = None
        self.preview_box: Any = None
        self.preview_timer: Any = None
        self.grow_hint: Any = None
        self.previewed: bool = False
        """Set once the user has asked for a preview. Until then a filter
        change refreshes nothing — running a query nobody requested is its
        own surprise."""

    async def render(self, tools: list[dict[str, Any]]) -> None:
        ex = self.existing
        self.name_in = ui.input("Cohort name *", value=str(ex["name"]) if ex else "").classes(
            "w-full"
        )
        self.desc_in = ui.input(
            "Description", value=str(ex.get("description") or "") if ex else ""
        ).classes("w-full")
        if ex:
            ui.label(f"Tool: {ex.get('tool_alias')} (fixed)").style("color: #888;")
        else:
            tool_opts = {str(t["alias"]): str(t.get("name") or t["alias"]) for t in tools}
            ui.select(tool_opts, label="Tool", on_change=self._on_tool_change).classes("w-full")
        ui.label(
            "Gathers completed runs of this tool — wherever they ran (standalone jobs, "
            "workflows, parametric studies, optimisations) and in any project you can see."
        ).style("color: #888; font-size: 0.85rem;")
        self.meta_box = ui.column().classes("w-full gap-2")
        self.auto_grow = ui.switch(
            "Keep including new matching runs (auto-grow)",
            value=bool(ex.get("auto_grow", True)) if ex else True,
            on_change=self._on_auto_grow_change,
        )
        ui.label("This applies to the whole cohort, not to any one run.").style(
            "color: #888; font-size: 0.8rem;"
        )
        # What turning the switch off will actually do, spelled out and kept
        # true as the switch moves. The old static line said the cohort was
        # "frozen to the runs you tick below" — but nothing was below until
        # you separately pressed Preview, and the requirement to tick only
        # surfaced as a toast *after* Save, which read as though the tick
        # itself were the thing being switched off.
        self.grow_hint = ui.label("").style("color: #888; font-size: 0.8rem;")
        self._refresh_grow_hint()
        self.preview_box = ui.column().classes("w-full gap-2")
        with ui.row().classes("gap-2"):
            ui.button("Preview matching runs", on_click=self._preview).props("outline")
            ui.button("Save changes" if ex else "Create cohort", on_click=self._submit).props(
                "color=primary"
            )
        if self.tool_alias:
            await self._load_meta(self.tool_alias)
        # An already-fixed cohort opens with its runs on screen: the list is
        # its membership, not a suggestion, and editing it blind is how a
        # curator loses a curated set without noticing.
        if ex and not bool(self.auto_grow.value) and self.tool_alias:
            await self._preview()

    async def _on_tool_change(self, event: Any) -> None:
        self.tool_alias = str(event.value) if event.value else None
        if self.tool_alias:
            await self._load_meta(self.tool_alias)

    async def _load_meta(self, alias: str) -> None:
        self.meta_box.clear()
        self.preview_box.clear()
        self.table = None
        self.range_inputs = {}
        self.previewed = False
        if self.preview_timer is not None:
            self.preview_timer.cancel()
            self.preview_timer = None
        try:
            meta = await self.client.get_cohort_build_meta(alias)
        except ApiError as exc:
            with self.meta_box:
                ui.label(exc.detail).style("color: var(--matofx-error-text);")
            return
        self.bands = list(meta.get("version_bands") or [])
        self.numeric_inputs = list(meta.get("numeric_inputs") or [])
        # Facets, not the whole directory / study list: every option here is one
        # that actually produced a run of this tool the caller can see, so a
        # filter can never come back empty because of the option itself.
        self.user_names = {int(f["id"]): str(f["name"]) for f in (meta.get("launchers") or [])}
        self.parametric_names = {
            int(f["id"]): str(f["name"]) for f in (meta.get("parametric_studies") or [])
        }
        self.optimisation_names = {
            int(f["id"]): str(f["name"]) for f in (meta.get("optimisation_studies") or [])
        }
        with self.meta_box:
            band_opts = {
                i: f"{b['major_minor']} (latest {b['latest_version']})"
                for i, b in enumerate(self.bands)
            }
            self.version_sel = ui.select(
                band_opts,
                label="Tool versions (major.minor — all patches, incl. future, included)",
                multiple=True,
                value=self._default_band_indices(),
                on_change=self._filters_changed,
            ).classes("w-full")
            with ui.expansion("Advanced filters").classes("w-full"):
                existing_ranges = (self.existing or {}).get("input_ranges") or {}
                if self.numeric_inputs:
                    ui.label("Input ranges").style("font-weight: 500;")
                for ni in self.numeric_inputs:
                    bound = existing_ranges.get(ni["name"]) or {}
                    with ui.row().classes("items-center gap-2 w-full"):
                        ui.label(str(ni["label"])).style("min-width: 180px;")
                        lo = (
                            ui.number("min", value=bound.get("min"))
                            .props("dense")
                            .on("blur", self._filters_changed)
                        )
                        hi = (
                            ui.number("max", value=bound.get("max"))
                            .props("dense")
                            .on("blur", self._filters_changed)
                        )
                        self.range_inputs[str(ni["name"])] = (lo, hi)
                # Who launched the run. The query has always supported this
                # filter; only the builder had no way to say it. The options are
                # the people who actually ran *this* tool, not the directory.
                owner_opts = dict(sorted(self.user_names.items(), key=lambda kv: kv[1].casefold()))
                parametric_opts = dict(
                    sorted(self.parametric_names.items(), key=lambda kv: kv[1].casefold())
                )
                optimisation_opts = dict(
                    sorted(self.optimisation_names.items(), key=lambda kv: kv[1].casefold())
                )

                # Two per row rather than a stack of full-width selects: these
                # are short, related choices, and reading them as a column made
                # the panel far taller than the answers it holds. ``flex-wrap``
                # so a narrow window drops to one per line rather than squeezing
                # two into an unusable width.
                ui.label("Where the runs came from").style("font-weight: 500; margin-top: 0.35rem;")
                # Stored since migration 0097. The caption used to say the
                # opposite - "narrow this preview only ... not saved on the
                # cohort" - which survived the change and told a live cohort's
                # owner that a filter it is still honouring had been discarded.
                # The user chapter has said "saved with the cohort" since; this
                # is the surface catching up.
                ui.label(
                    "Source and study help you find runs in a long list - and, like the "
                    "filters above, they are saved with the cohort, so a live one keeps "
                    "honouring them."
                ).style("color: #888; font-size: 0.8rem;")
                _pick = "dense outlined use-chips"
                _grow = "flex: 1 1 18rem; min-width: 16rem;"
                with ui.row().classes("w-full gap-2 flex-wrap items-start"):
                    self.owner_sel = (
                        ui.select(
                            owner_opts,
                            label="Launched by",
                            multiple=True,
                            value=[
                                int(u)
                                for u in ((self.existing or {}).get("owner_user_ids") or [])
                                if int(u) in owner_opts
                            ],
                            on_change=self._filters_changed,
                        )
                        .props(_pick)
                        .style(_grow)
                    )
                    self.source_sel = (
                        ui.select(
                            dict(_SOURCE_LABELS),
                            label="Source",
                            multiple=True,
                            value=[
                                str(k)
                                for k in ((self.existing or {}).get("source_kinds") or [])
                                if str(k) in _SOURCE_LABELS
                            ],
                            on_change=self._source_changed,
                        )
                        .props(_pick)
                        .style(_grow)
                    )
                with ui.row().classes("w-full gap-2 flex-wrap items-start") as study_row:
                    self.parametric_sel = (
                        ui.select(
                            parametric_opts,
                            label="Parametric studies",
                            multiple=True,
                            value=[
                                int(i)
                                for i in ((self.existing or {}).get("parametric_study_ids") or [])
                                if int(i) in parametric_opts
                            ],
                            on_change=self._filters_changed,
                        )
                        .props(_pick)
                        .style(_grow)
                    )
                    self.optimisation_sel = (
                        ui.select(
                            optimisation_opts,
                            label="Optimisation studies",
                            multiple=True,
                            value=[
                                int(i)
                                for i in ((self.existing or {}).get("optimisation_study_ids") or [])
                                if int(i) in optimisation_opts
                            ],
                            on_change=self._filters_changed,
                        )
                        .props(_pick)
                        .style(_grow)
                    )
                self.study_row = study_row
                # Studies this cohort commissioned to fill its own gaps. Their
                # completed runs join the membership in *either* mode, so with
                # no way to untick one there was no way to shrink a cohort
                # back: narrowing the picked runs to a single study still
                # resolved to the commissioned runs as well.
                #
                # Options are the candidate facets **plus** whatever is already
                # attached. A commissioned study whose runs fall outside the
                # current filters would otherwise be absent from the list and
                # saving would silently detach it — the same class of bug this
                # control exists to fix.
                commissioned_ids = [
                    int(i) for i in ((self.existing or {}).get("auto_include_study_ids") or [])
                ]
                if commissioned_ids:
                    commissioned_opts = dict(parametric_opts)
                    for study_id in commissioned_ids:
                        commissioned_opts.setdefault(study_id, f"Study {study_id}")
                    with ui.row().classes("w-full gap-2 flex-wrap items-start"):
                        self.commissioned_sel = (
                            ui.select(
                                commissioned_opts,
                                label="Commissioned studies (their completed runs join too)",
                                multiple=True,
                                value=commissioned_ids,
                                on_change=self._filters_changed,
                            )
                            .props(_pick)
                            .style(_grow)
                        )
                    ui.label(
                        "Untick a study to detach it. The runs themselves are not "
                        "touched. On a live cohort they may still match the filters "
                        "on their own and stay in — drop those individually."
                    ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
                self._refresh_source_visibility()
                after_val = _date_part((self.existing or {}).get("completed_after"))
                self.after_in = (
                    ui.input("Completed after", value=after_val, on_change=self._filters_changed)
                    .props("type=date")
                    .classes("w-full")
                )
                self.before_in = (
                    ui.input(
                        "Completed before",
                        value=_date_part((self.existing or {}).get("completed_before")),
                        on_change=self._filters_changed,
                    )
                    .props("type=date")
                    .classes("w-full")
                )

    def _refresh_source_visibility(self) -> None:
        """Show a study picker only for a source kind that is actually selected.

        Offering "which parametric study?" while the source filter excludes
        parametric runs asks a question whose answer cannot change the result.
        A kind with no studies behind it stays hidden too.
        """
        chosen = set(self.source_sel.value or []) if self.source_sel is not None else set()
        show_parametric = "parametric_study" in chosen and bool(self.parametric_names)
        show_optimisation = "optimisation_study" in chosen and bool(self.optimisation_names)
        if self.parametric_sel is not None:
            self.parametric_sel.set_visibility(show_parametric)
        if self.optimisation_sel is not None:
            self.optimisation_sel.set_visibility(show_optimisation)
        # Collapse the row itself too, or an empty band of whitespace sits
        # between the filters and the dates whenever neither picker applies.
        if self.study_row is not None:
            self.study_row.set_visibility(show_parametric or show_optimisation)

    async def _source_changed(self, _event: Any = None) -> None:
        """A source change re-gates the study pickers, then re-previews.

        Selections for a kind that has just been de-selected are cleared: a
        hidden picker holding a value would keep filtering invisibly.
        """
        chosen = set(self.source_sel.value or []) if self.source_sel is not None else set()
        if self.parametric_sel is not None and "parametric_study" not in chosen:
            self.parametric_sel.value = []
        if self.optimisation_sel is not None and "optimisation_study" not in chosen:
            self.optimisation_sel.value = []
        self._refresh_source_visibility()
        await self._filters_changed()

    def _refresh_grow_hint(self) -> None:
        """Keep the under-switch explanation true to the switch's position."""
        if self.grow_hint is None:
            return
        ex = self.existing
        if bool(self.auto_grow.value):
            self.grow_hint.text = (
                "On — the cohort re-checks for matches every time it's read, so new "
                "runs join on their own."
            )
            return
        if ex and ex.get("member_job_ids"):
            self.grow_hint.text = (
                "Off — the cohort stays frozen to the runs it already holds. Tick rows "
                "below to replace that set with a different one."
            )
            return
        self.grow_hint.text = (
            "Off — on save the cohort freezes to the runs listed below and stops "
            "taking on new ones. Tick rows only if you want fewer than all of them."
        )

    async def _on_auto_grow_change(self, _event: Any = None) -> None:
        """React to the auto-grow switch moving.

        Turning it **off** runs the preview, because that list is what the
        cohort is about to be frozen to — deciding to freeze without being
        shown what you are freezing is the whole complaint. It also makes a
        no-match query visible *now* rather than as a refusal after Save.
        """
        self._refresh_grow_hint()
        if not bool(self.auto_grow.value) and self.tool_alias:
            await self._preview()

    async def _current_match_ids(self) -> list[int] | None:
        """The job ids this query matches right now, or ``None`` on failure.

        Used to freeze a cohort that is being switched off auto-grow without
        any rows ticked: "off" means "keep what you have", so the honest
        snapshot is everything the *current form* resolves to — not an error
        telling the curator to go and tick things by hand.
        """
        if not self.tool_alias:
            return []
        try:
            data = await self.client.preview_cohort_candidates(
                tool_alias=self.tool_alias,
                tolerated_version_bands=self._selected_bands() or None,
                **self._gather_filters(),
            )
        except ApiError as exc:
            ui.notify(f"Couldn't work out which runs to freeze: {exc.detail}", type="negative")
            return None
        rows = list(data.get("rows") or [])
        hidden = int(data.get("acl_hidden") or 0)
        if hidden:
            # A live cohort resolves per reader; a frozen one is fixed by
            # whoever froze it. Runs this curator can't see would be dropped
            # silently, so say so rather than quietly shrinking the cohort.
            ui.notify(
                f"{hidden} matching run(s) aren't visible to you and won't be included "
                "in the frozen set.",
                type="warning",
            )
        return [int(r["job_id"]) for r in rows]

    async def _filters_changed(self, _event: Any = None) -> None:
        """Re-run the preview whenever a filter moves (debounced).

        **The preview has to answer the filter you are looking at.** It used
        to refresh only when the button was pressed, so a changed range left
        a stale list on screen with nothing to say it was stale -- and the
        only way to discover that was to collapse the panel and reopen it,
        which nothing on the page suggested. A filter you cannot see the
        effect of is a filter you cannot trust.

        Debounced because a typed bound fires per keystroke and each refresh
        is a real query: the timer is cancelled and restarted on every change,
        so only the last one runs.
        """
        if self.preview_timer is not None:
            self.preview_timer.cancel()
        # Nothing has been previewed yet -- don't surprise the user with a
        # query they never asked for just because they touched a box.
        if not self.previewed:
            return
        self.preview_timer = ui.timer(_PREVIEW_DEBOUNCE_SECONDS, self._preview, once=True)

    def _default_band_indices(self) -> list[int]:
        ex = self.existing
        if ex and ex.get("tolerated_version_bands"):
            chosen = set(ex["tolerated_version_bands"])
            idxs = [i for i, b in enumerate(self.bands) if b.get("major_minor") in chosen]
            if idxs:
                return idxs
        if ex and ex.get("tolerated_tool_version_ids"):
            tol = set(ex["tolerated_tool_version_ids"])
            idxs = [i for i, b in enumerate(self.bands) if tol & set(b.get("version_ids") or [])]
            if idxs:
                return idxs
        # New cohort: tolerate *every* installed version line by default so it
        # gathers all of the tool's completed runs. Defaulting to only the
        # newest band silently excluded runs made on earlier versions — which
        # reads as the cohort "not picking up all the jobs". A curator can
        # still narrow the multiselect to specific bands when they only want
        # one version line.
        return list(range(len(self.bands)))

    def _selected_bands(self) -> list[str]:
        """The ``major.minor`` strings of the selected version bands."""
        if not self.version_sel or not self.version_sel.value:
            return []
        return [str(self.bands[i]["major_minor"]) for i in self.version_sel.value]

    def _gather_filters(self) -> dict[str, Any]:
        """The filter state this form is asserting -- every key, always.

        **An emptied box has to travel.** This used to omit any filter the
        user had cleared, which on a PATCH reads as "leave it as it was", so a
        range or date could be added but never removed: the boxes came back
        empty, Save appeared to work, and the cohort kept filtering. Sending
        ``None`` says what the form actually means -- *there is no bound here*
        -- and the create path ignores nulls anyway.
        """
        ranges: dict[str, dict[str, float]] = {}
        for name, (lo, hi) in self.range_inputs.items():
            bound: dict[str, float] = {}
            if lo.value is not None:
                bound["min"] = lo.value
            if hi.value is not None:
                bound["max"] = hi.value
            if bound:
                ranges[name] = bound
        after = str(self.after_in.value) if self.after_in and self.after_in.value else None
        before = str(self.before_in.value) if self.before_in and self.before_in.value else None

        def _chosen(widget: Any) -> list[Any]:
            return list(widget.value or []) if widget is not None else []

        return {
            "input_ranges": ranges or None,
            "owner_user_ids": [int(u) for u in _chosen(self.owner_sel)] or None,
            "completed_after": after,
            "completed_before": before,
            # Stored filters since migration 0097 — they travel with Save, so a
            # re-opened cohort shows what was chosen and a live one keeps
            # honouring it.
            "source_kinds": [str(k) for k in _chosen(self.source_sel)] or None,
            "parametric_study_ids": [int(i) for i in _chosen(self.parametric_sel)] or None,
            "optimisation_study_ids": [int(i) for i in _chosen(self.optimisation_sel)] or None,
        }

    def _commissioned_payload(self) -> dict[str, Any]:
        """The update-only half of the form, kept out of :meth:`_gather_filters`.

        ``_gather_filters`` feeds **preview and create as well as update**, and
        both of those bodies forbid unknown fields — a cohort cannot commission
        a study before it exists, so ``auto_include_study_ids`` is on the
        update schema alone. Putting it in the shared dict made every preview
        of an edited cohort fail with ``extra_forbidden``.

        Empty when the control was never rendered: sending it from a form that
        did not show the field would detach every commissioned study on any
        unrelated save.
        """
        widget = getattr(self, "commissioned_sel", None)
        if widget is None:
            return {}
        return {"auto_include_study_ids": [int(i) for i in (widget.value or [])]}

    async def _preview(self) -> None:
        self.preview_box.clear()
        self.table = None
        if not self.tool_alias:
            ui.notify("Pick a tool first.", type="warning")
            return
        # From here on the list tracks the filters (see `_filters_changed`).
        self.previewed = True
        try:
            data = await self.client.preview_cohort_candidates(
                tool_alias=self.tool_alias,
                tolerated_version_bands=self._selected_bands() or None,
                **self._gather_filters(),
            )
        except ApiError as exc:
            with self.preview_box:
                ui.label(exc.detail).style("color: var(--matofx-error-text);")
            return
        rows = list(data.get("rows") or [])
        hidden = int(data.get("acl_hidden") or 0)
        live = bool(self.auto_grow.value) if self.auto_grow is not None else True
        with self.preview_box:
            if not rows:
                ui.label("No completed runs match this query yet.").style("color: #888;")
                if not live:
                    # Surface the dead end here, not as a refusal after Save.
                    ui.label(
                        "There is nothing to freeze, so auto-grow can't be turned off "
                        "yet. Widen the filters until the cohort matches something — or "
                        "archive it if you want it to stop gathering runs altogether."
                    ).style("color: #9a7d0a; font-size: 0.8rem;")
                return
            note = f"{len(rows)} matching run(s)."
            if hidden:
                note += f" ({hidden} more hidden by access controls.)"
            ui.label(note).style("color: #888; font-size: 0.85rem;")
            ui.label(
                (
                    f"With auto-grow off, saving freezes the cohort to these "
                    f"{len(rows)} run(s). Tick rows only to keep fewer than all of them."
                    if not live
                    else "Tick rows to pick an explicit set, if you turn auto-grow off."
                )
                + " This list updates as you change the filters above."
            ).style("color: #888; font-size: 0.8rem;")
            table_rows = [
                {
                    "job_id": int(r["job_id"]),
                    "project": self.project_names.get(int(r["project_id"]), str(r["project_id"])),
                    "source": _SOURCE_LABELS.get(str(r.get("source")), str(r.get("source"))),
                    "source_name": str(r.get("source_name") or "—"),
                    "owner": self.user_names.get(
                        int(r["created_by_user_id"]), str(r["created_by_user_id"])
                    )
                    if r.get("created_by_user_id") is not None
                    else "—",
                    "completed": (
                        format_user_datetime(str(r["completed_at"]))
                        if r.get("completed_at")
                        else "—"
                    ),
                }
                for r in rows
            ]
            self.table = ui.table(
                columns=_PREVIEW_COLUMNS,
                rows=table_rows,
                row_key="job_id",
                selection="multiple",
            ).classes("w-full")

    async def _submit(self) -> None:
        ex = self.existing
        if not self.name_in.value or not self.tool_alias:
            ui.notify("A name and a tool are required.", type="warning")
            return
        live = bool(self.auto_grow.value)
        selected = list(self.table.selected) if self.table is not None else []
        member_ids: list[int] | None = [int(r["job_id"]) for r in selected] if selected else None
        if not live and member_ids is None and not (ex and ex.get("member_job_ids")):
            # Nothing ticked and nothing already frozen. "Stop growing" means
            # "keep what you have", so freeze everything the query matches
            # right now — ticking is for keeping *fewer* than that. Demanding
            # a manual tick here is what made the switch look per-row.
            member_ids = await self._current_match_ids()
            if member_ids is None:
                return  # the lookup itself failed; it already said so
            if not member_ids:
                # A fixed cohort with nothing in it is refused by the service
                # too — say why here rather than letting the save round-trip
                # to a raw config error, and name the way out.
                ui.notify(
                    "This cohort matches no runs, so there is nothing to freeze. "
                    "Widen the filters until it matches something, or archive the "
                    "cohort if you want it to stop gathering runs altogether.",
                    type="warning",
                    close_button=True,
                    timeout=None,
                )
                return
        bands = self._selected_bands() or None
        filters = self._gather_filters()
        try:
            if ex:
                cohort = await self.client.update_cohort(
                    int(ex["id"]),
                    name=str(self.name_in.value),
                    description=str(self.desc_in.value or ""),
                    tolerated_version_bands=bands,
                    auto_grow=live,
                    member_job_ids=member_ids,
                    **filters,
                    **self._commissioned_payload(),
                )
            else:
                cohort = await self.client.create_cohort(
                    name=str(self.name_in.value),
                    description=str(self.desc_in.value or ""),
                    tool_alias=self.tool_alias,
                    tolerated_version_bands=bands,
                    auto_grow=live,
                    member_job_ids=member_ids,
                    **filters,
                )
        except ApiError as exc:
            ui.notify(exc.detail, type="negative")
            return
        ui.notify("Saved." if ex else "Cohort created.", type="positive")
        ui.navigate.to(f"/cohorts/{int(cohort['id'])}")


def _date_part(value: Any) -> str:
    """The ``YYYY-MM-DD`` head of an ISO timestamp (for a date input prefill)."""
    if not value:
        return ""
    return str(value)[:10]


async def _render_create_form(client: ApiClient) -> None:
    """Render the new-cohort builder."""
    with section_card("add", "New cohort"):
        try:
            tools = await client.list_tools()
            projects = await client.list_projects()
        except ApiError as exc:
            ui.label(f"Couldn't load the form: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return
        if not tools:
            ui.label("No tools are available to you yet.").style("color: var(--matofx-muted-text);")
            return
        project_names = {int(p["id"]): str(p.get("name") or p["id"]) for p in projects}
        await _CohortBuilder(client, project_names).render(tools)


# ===================== detail =====================


@ui.page("/cohorts/{cohort_id}")
async def cohort_detail_page(cohort_id: int) -> None:
    """The cohort detail — saved query, live membership, member results."""
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    body = ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;")
    await _render_detail(client, cohort_id, body)


async def _render_detail(client: ApiClient, cohort_id: int, body: ui.column) -> None:
    body.clear()
    with body:
        try:
            cohort = await client.get_cohort(cohort_id)
        except ApiError as exc:
            ui.label(f"Couldn't load this cohort: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return

        # Back link above the title — left-aligned with a back-arrow, so it
        # no longer sits under (and clash with) the fixed theme / help
        # buttons pinned to the top-right of the content area.
        ui.button(
            "Back to cohorts", icon="arrow_back", on_click=lambda: ui.navigate.to("/cohorts")
        ).props("flat dense no-caps").style(
            "color: var(--matofx-muted-text); margin-left: -0.3rem;"
        )

        with ui.row().classes("w-full items-center gap-3"):
            ui.label(str(cohort.get("name") or f"Cohort #{cohort_id}")).style(
                "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
            )
            _state_chip(str(cohort.get("state") or "active"))

        # Edit / Archive / Build a surrogate at the top, right under the
        # title — the primary actions shouldn't be buried below the
        # results matrix. Each is gated on its own right: curating the
        # cohort and fitting a surrogate from it are different jobs.
        is_active = str(cohort.get("state")) == "active"
        can_curate = is_active and has_permission(_COHORTS_CURATE)
        # A cohort is a first-class surrogate training source — the same
        # one the fit form offers under "Cohort" — so it gets the same
        # affordance a finished parametric study has. The deep link
        # pre-fills the form; the fit itself materialises the cohort's
        # results server-side, so a cohort whose results were never
        # opened still trains on its live membership.
        can_fit = surrogate_family_enabled() and (
            has_permission(_MODELS_CREATE) or has_permission(_MODELS_CURATE)
        )

        async def _edit() -> None:
            await _render_edit(client, cohort, body)

        async def _archive() -> None:
            try:
                await client.archive_cohort(cohort_id)
            except ApiError as exc:
                ui.notify(exc.detail, type="negative")
                return
            ui.notify("Cohort archived.", type="positive")
            await _render_detail(client, cohort_id, body)

        if can_curate or can_fit:
            with ui.row().classes("gap-2 items-center"):
                if can_curate:
                    ui.button("Edit cohort", on_click=_edit).props("flat color=primary")
                    ui.button("Archive cohort", on_click=_archive).props("flat color=negative")
                if can_fit:
                    ui.button(
                        "Build a surrogate",
                        icon="model_training",
                        on_click=lambda _c=cohort_id: ui.navigate.to(
                            f"/models?source_kind=cohort&source_id={_c}"
                        ),
                    ).props("flat color=primary")

        with section_card("settings", "Definition"):
            # Tool label, not alias ([[feedback_use_labels_not_names]]).
            tool_label = str(cohort.get("tool_name") or cohort.get("tool_alias") or "")
            ui.label(f"Tool: {tool_label}").style("color: var(--matofx-muted-text);")
            # Show the actual version strings, not the (meaningless) id count.
            versions = cohort.get("tolerated_versions") or []
            bands = cohort.get("tolerated_version_bands") or []
            if versions:
                versions_text = ", ".join(str(v) for v in versions)
                # A band cohort tolerates whole major.minor lines (incl. future
                # patches) — note which, so the list reads as a snapshot.
                if bands:
                    versions_text += f" (tolerates {', '.join(str(b) for b in bands)})"
            else:
                versions_text = "(none)"
            ui.label(f"Tolerated tool versions: {versions_text}").style(
                "color: var(--matofx-muted-text);"
            )
            if cohort.get("auto_grow", True):
                ui.label("Membership: live — auto-grows as new runs match.").style(
                    "color: var(--matofx-muted-text);"
                )
            else:
                # The *curated* set, which is not the same number as the membership
                # whenever the cohort also drops runs or auto-includes a study.
                # Quoting it as "Membership: N" put a second, different count on
                # the same page as the resolved one in Members & results, and the
                # curator had no way to tell which was wrong. The authoritative
                # count stays where the runs themselves are listed.
                members = cohort.get("member_job_ids") or []
                excluded = cohort.get("excluded_job_ids") or []
                commissioned = cohort.get("auto_include_study_ids") or []
                parts = [f"Membership: fixed — {len(members)} run(s) picked"]
                if excluded:
                    parts.append(f"{len(excluded)} dropped")
                if commissioned:
                    parts.append(
                        f"plus completed runs of {len(commissioned)} commissioned study(ies)"
                    )
                ui.label(", ".join(parts) + ".").style("color: var(--matofx-muted-text);")
                if commissioned:
                    # The picked count is not the membership, and both numbers
                    # sit on one page: this line said "20 picked" while the
                    # coverage panel below said 60, with nothing on screen to
                    # reconcile them.
                    ui.label(
                        "Those commissioned runs are counted in the coverage and results "
                        "panels below, so the resolved total is larger than the picked "
                        "count above. Edit the cohort to detach a study."
                    ).style("color: #b58900; font-size: 0.85rem;")
            if cohort.get("project_ids"):
                ui.label(f"Restricted to {len(cohort['project_ids'])} project(s).").style(
                    "color: var(--matofx-muted-text);"
                )
            else:
                ui.label("Searches the whole workspace.").style("color: var(--matofx-muted-text);")
            last = cohort.get("last_resolved_at")
            if last:
                ui.label(f"Membership last resolved: {format_user_datetime(str(last))}").style(
                    "color: var(--matofx-muted-text); font-size: 0.85rem;"
                )

        await _render_coverage(client, cohort_id, cohort)
        await _render_results(client, cohort_id)


async def _render_edit(client: ApiClient, cohort: dict[str, Any], body: ui.column) -> None:
    """Render the builder in edit mode for an existing cohort (curate-gated)."""
    body.clear()
    with body:

        async def _cancel() -> None:
            await _render_detail(client, int(cohort["id"]), body)

        # Back link above the card (matching the detail page) — it both
        # cancels the edit and keeps the card clear of the fixed theme /
        # help buttons pinned to the top-right of the content area.
        ui.button("Back to cohorts", icon="arrow_back", on_click=_cancel).props(
            "flat dense no-caps"
        ).style("color: var(--matofx-muted-text); margin-left: -0.3rem;")

        with section_card("edit", f"Edit cohort — {cohort.get('name')}"):
            try:
                projects = await client.list_projects()
            except ApiError as exc:
                ui.label(f"Couldn't load the form: {exc.detail}").style(
                    "color: var(--matofx-error-text);"
                )
                return
            project_names = {int(p["id"]): str(p.get("name") or p["id"]) for p in projects}
            await _CohortBuilder(client, project_names, existing=cohort).render([])

            ui.button("Cancel", on_click=_cancel).props("flat")


async def _render_results(client: ApiClient, cohort_id: int) -> None:
    """Live membership + the lazily-populated member-results matrix (§3.3)."""
    with section_card("insights", "Members & results"):
        try:
            results = await client.get_cohort_results(cohort_id)
        except ApiError as exc:
            ui.label(f"Couldn't resolve membership: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
            return
        rows = list(results.get("rows") or [])
        hidden = int(results.get("acl_hidden") or 0)
        ui.label(f"{len(rows)} member run(s).").style("color: var(--matofx-muted-text);")
        if hidden:
            ui.label(f"({hidden} run(s) hidden by access controls)").style(
                "color: var(--matofx-muted-text); font-size: 0.85rem;"
            )
        if not rows:
            ui.label("No completed runs match this cohort yet.").style(
                "color: var(--matofx-muted-text);"
            )
            return

        # Input + response columns come from the API already keyed (deduped)
        # and carrying the descriptor label + unit, so the headers read
        # "Heat Dissipation (W)" not the internal name. Fall back to the
        # union of keys present if the server sent none (older API).
        input_meta = list(results.get("input_columns") or [])
        out_meta = list(results.get("columns") or [])
        if not out_meta:
            seen: list[str] = []
            for r in rows:
                for k in r.get("scalars") or {}:
                    if k not in seen:
                        seen.append(k)
            out_meta = [{"key": k, "label": k, "unit": ""} for k in seen]

        def _header(col: dict[str, Any]) -> str:
            label = str(col.get("label") or col.get("key") or "")
            unit = str(col.get("unit") or "")
            return f"{label} ({unit})" if unit else label

        input_keys = [str(c.get("key")) for c in input_meta]
        out_keys = [str(c.get("key")) for c in out_meta]
        # Namespace the table field keys so an input and an output that
        # happen to share a name don't collide in the row dict.
        columns = [{"name": "job_id", "label": "Job", "field": "job_id", "align": "left"}]
        columns += [
            {"name": f"in_{c.get('key')}", "label": _header(c), "field": f"in_{c.get('key')}"}
            for c in input_meta
        ]
        columns += [
            {"name": f"out_{c.get('key')}", "label": _header(c), "field": f"out_{c.get('key')}"}
            for c in out_meta
        ]
        table_rows = [
            {
                "job_id": r.get("job_id"),
                **{f"in_{k}": (r.get("inputs") or {}).get(k) for k in input_keys},
                **{f"out_{k}": (r.get("scalars") or {}).get(k) for k in out_keys},
            }
            for r in rows
        ]
        if input_keys:
            ui.label("Inputs, then results — one row per member run.").style(
                "color: var(--matofx-muted-text); font-size: 0.8rem;"
            )
        table = ui.table(columns=columns, rows=table_rows, row_key="job_id").classes("w-full")
        # Make the Job cell a link to the run's My Jobs page.
        table.add_slot(
            "body-cell-job_id",
            '<q-td :props="props">'
            '<a :href="`/ui/my-jobs?job=${props.value}`" '
            'style="color: var(--matofx-accent); text-decoration: none; font-weight: 600;">'
            "#{{ props.value }}</a>"
            "</q-td>",
        )


# --- design-space coverage (ADR-0020 §11) ---------------------------------

_SCATTER_FACTOR_CAP = 4
"""Factors drawn in the pairwise matrix. Four is six panels; eleven would be
fifty-five and unreadable, which is the whole reason for a cap."""


async def _render_coverage(client: ApiClient, cohort_id: int, cohort: dict[str, Any]) -> None:
    """The coverage panel: is this cohort fit to learn from? (§11).

    Absent — not empty — when the feature is off, matching the API. The
    analysis is read-only and open to anyone who can see the cohort;
    spending compute on the runs it suggests is a separate right and a
    later slice.
    """
    if not design_space_coverage_enabled():
        return

    with section_card("insights", "Is this fit to learn from?"):
        container = ui.column().classes("w-full gap-3")
        overrides: dict[str, dict[str, Any]] = {}

        async def _load() -> None:
            container.clear()
            with container:
                _working("Measuring coverage…")
            try:
                report = await client.cohort_coverage(cohort_id, envelope=list(overrides.values()))
            except ApiError as exc:
                container.clear()
                with container:
                    ui.label(exc.detail).style("color: var(--matofx-error-text);")
                return
            except Exception:
                # Not only ``ApiError``. The UI talks to the API in-process, so
                # a server-side fault — a response the schema refuses, say —
                # arrives here as the raw exception rather than an HTTP error,
                # escapes this handler, and blanks the whole page with the
                # spinner still on it. Reported as "stuck on Measuring
                # coverage…" ([[feedback_page_body_pre_render_catch_all]]).
                _log.exception("Cohort coverage failed for cohort %s", cohort_id)
                container.clear()
                with container:
                    ui.label(
                        "Couldn't measure coverage for this cohort — see the server log."
                    ).style("color: var(--matofx-error-text);")
                return
            container.clear()
            with container:
                _render_coverage_body(report, overrides, _load, cohort_id, cohort)

        await _load()


def _section(title: str, *, icon: str, open_: bool = False) -> Any:
    """One collapsible section of the panel, all styled alike.

    The panel had grown into a flat run of headings, tables, legends and
    expansions at four different weights, and nothing said which belonged
    to which. Every block is now a section with the same header treatment,
    so the page has a structure to scan rather than a sequence to read.

    Headers use Quasar's own ``text-primary`` / ``text-weight-medium`` rather
    than a bespoke stylesheet: the accent then follows the app's theme, in
    both light and dark, without a second definition to keep in step.
    """
    return (
        ui.expansion(title, icon=icon, value=open_)
        .classes("w-full")
        .props('dense expand-separator header-class="text-primary text-weight-medium"')
    )


def _render_coverage_body(
    report: dict[str, Any],
    overrides: dict[str, dict[str, Any]],
    reload_: Any,
    cohort_id: int,
    cohort: dict[str, Any] | None = None,
) -> None:
    """The panel, in the order the questions get asked (§11).

    Verdict first, because it is the answer. Then the evidence behind it, at
    each of the two scales it was measured on. Then the setting that governs
    it, closed by default -- most readers never change the range, and putting
    a form above the answer buried it. Then what to do about it.
    """
    _render_verdict(report)

    with _section("Each input on its own", icon="tune", open_=True):
        _render_factor_strip(report)

    with _section("How the inputs combine", icon="grid_on", open_=True):
        _render_pair_strip(report)
        _render_scatter(report)

    with _section("Over what range should this model be trusted?", icon="straighten"):
        _render_envelope_form(report, overrides, reload_)

    _render_suggestions(report, overrides, cohort_id, cohort or {})


def _render_verdict(report: dict[str, Any]) -> None:
    """Traffic light, the headline in plain terms, and the warnings."""
    label, colour = verdict_chip(str(report.get("verdict") or ""))
    with ui.row().classes("items-center gap-3 w-full"):
        ui.badge(label).props(f"color={colour}").style("font-size: 0.85rem; padding: 0.35rem .7rem")
        ui.label(headline(report)).style("font-size: 0.95rem;")

    # **Is it patchy because the design is bad, or because it is small?** The
    # verdict cannot say: it is measured against a fixed 30-runs-per-input
    # standard, so a small archive reads thin however well it is arranged.
    # The engine has always computed the other reading and shown it nowhere.
    # Two sentences, and at most one of them ever appears. The measured one
    # is silent below the density where it can separate a good design from a
    # bad one; the provenance one speaks only there, and only where every run
    # came from a space-filling study. Where neither can say anything the
    # panel says nothing, which is the honest outcome.
    for sentence in (arrangement_note(report), provenance_note(report)):
        if sentence:
            ui.label(sentence).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

    diagnostics = report.get("diagnostics") or {}
    outside = int(diagnostics.get("n_outside_envelope") or 0)
    mode = str(diagnostics.get("grouping_mode") or "n/a")
    notes: list[str] = []
    if outside:
        notes.append(f"{outside} run(s) sit outside the range you've declared.")
    if mode == "pooled":
        notes.append("Options are judged together rather than split, to avoid a false alarm.")
    if notes:
        ui.label(" ".join(notes)).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

    for warning in report.get("warnings") or []:
        ui.label(str(warning.get("message") or "")).style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )


def _render_envelope_form(
    report: dict[str, Any],
    overrides: dict[str, dict[str, Any]],
    reload_: Any,
) -> None:
    """One row per factor — the plain question, pre-populated (§11.1)."""
    rows = envelope_rows(list(report.get("envelope") or []))
    if not rows:
        return
    # The section wrapper supplies the heading; this draws only its contents.
    with ui.column().classes("w-full gap-2"):
        ui.label(
            "These start at the range your runs actually cover. Narrow them if you only "
            "trust part of it — the runs outside still train the model, they just stop "
            "counting towards coverage of the range you're claiming."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

        inputs: dict[str, tuple[Any, Any]] = {}
        for row in rows:
            with ui.row().classes("w-full items-center gap-3"):
                # The label, never the internal name
                # ([[feedback_use_labels_not_names]]). `row.name` is still
                # the key the override payload is sent under, below.
                ui.label(row.label).style("min-width: 12rem;")
                if row.editable:
                    # Step and display at the input's own increment, so the
                    # box offers values the tool would accept and shows them
                    # to the precision it actually claims.
                    digits = decimals_for(row.precision)
                    number_args: dict[str, Any] = {"precision": digits}
                    if row.precision:
                        number_args["step"] = row.precision
                    lo = ui.number(label="Lowest", value=row.lo, **number_args).props(
                        "dense outlined"
                    )
                    hi = ui.number(label="Highest", value=row.hi, **number_args).props(
                        "dense outlined"
                    )
                    inputs[row.name] = (lo, hi)
                if row.note:
                    ui.label(row.note).style("color: var(--matofx-muted-text); font-size: 0.8rem;")

        async def _apply() -> None:
            overrides.clear()
            for name, (lo, hi) in inputs.items():
                overrides[name] = {"name": name, "lo": lo.value, "hi": hi.value}
            await reload_()

        ui.button("Re-check coverage", on_click=_apply).props("flat color=primary")


def _render_factor_strip(report: dict[str, Any]) -> None:
    """The two-range per-input table (§4.5).

    **Simulated range and input range are different questions.** Everything
    the panel measured used to be scored against the range the runs happened
    to span, which is self-referential: "98% covered" meant *98% of the range
    you chose to run*, and could never report that a tenth of the tool's own
    range had been explored. Both are shown, each with its own tag, and a row
    opens the two histograms side by side.
    """
    rows = factor_table_rows(report)
    if not rows:
        return

    points = list(report.get("points") or [])
    with ui.column().classes("w-full gap-1"):
        ui.label(
            "Simulated range is what these runs used; input range is what the "
            "tool allows. Click a row to see the spread of values."
        ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")

        # A real ``<table>``, sized to its own content and centred. A CSS grid
        # of fractional columns stretched five short cells across the whole
        # panel, so a compact five-value row read as five unrelated captions.
        with _centred_table(("Input", "Simulated range", "Coverage", "Input range", "Coverage")):
            for row in rows:
                _render_factor_table_row(row, points)


def _render_factor_table_row(row: dict[str, Any], points: list[dict[str, Any]]) -> None:
    """One input's line, every cell opening the same pair of histograms.

    Clicking either range or either tag opens the *same* dialog, showing both
    distributions together: a reader who clicks the input-range tag is asking
    the same underlying question as one who clicks the simulated-range tag,
    and answering it twice in two places is how the two readings drift apart.
    """

    def _open() -> None:
        _show_histograms(row, points)

    with ui.element("tr").style("cursor: pointer;").on("click", _open) as line:
        with _cell():
            ui.label(row["label"]).style("font-size: 0.85rem; font-weight: 500;")
        with _cell(align="right"):
            ui.label(row["simulated_range"]).style("font-size: 0.85rem;")
        with _cell():
            ui.badge(row["simulated_chip"]).props(f"color={row['simulated_colour']} outline").style(
                "font-size: 0.7rem;"
            )
        with _cell(align="right"):
            ui.label(row["declared_range"]).style("font-size: 0.85rem;")
        with _cell():
            ui.badge(row["declared_chip"]).props(f"color={row['declared_colour']} outline").style(
                "font-size: 0.7rem;"
            )
    line.classes("matofx-hover-row")


def _centred_table(headings: tuple[str, ...]) -> Any:
    """A plain ``<table>`` sized to its content and centred in the panel.

    The tables here hold five and three short values; laid out on a grid of
    fractional columns they stretched the full panel width and the eye had to
    travel between related numbers. ``width: auto`` lets the columns size
    themselves, ``margin: 0 auto`` puts the block in the middle.
    """
    table = ui.element("table").style("width: auto; margin: 0 auto; border-collapse: collapse;")
    with table:
        with ui.element("thead"), ui.element("tr"):
            for heading in headings:
                with ui.element("th").style(
                    "padding: 0.2rem 0.85rem; text-align: left; white-space: nowrap; "
                    "border-bottom: 1px solid var(--matofx-rule, #d3dbe0);"
                ):
                    ui.label(heading).style(
                        "font-size: 0.72rem; font-weight: 600; letter-spacing: 0.04em; "
                        "color: var(--matofx-muted-text); text-transform: uppercase;"
                    )
        return ui.element("tbody")


def _cell(*, align: str = "left") -> Any:
    """One ``<td>``, padded and never wrapped mid-number."""
    return ui.element("td").style(
        f"padding: 0.22rem 0.85rem; text-align: {align}; white-space: nowrap;"
    )


def _show_histograms(row: dict[str, Any], points: list[dict[str, Any]]) -> None:
    """Both distributions for one input, side by side and labelled (§4.5).

    Same values, two frames. The left one spans what the runs used, so it
    shows how evenly they are spread; the right spans what the tool allows,
    so it shows how much of that was ever tried. An input pinned at 60 out of
    a permitted 10-120 is a single bar in the middle of an empty axis on the
    right, and that picture is the entire point.
    """
    factor = row["factor"]
    values = values_for(points, row["name"])
    precision = row.get("precision")

    with ui.dialog() as dialog, ui.card().style("min-width: min(56rem, 92vw);"):
        ui.label(row["label"]).style("font-weight: 600; font-size: 1rem;")
        if not values:
            ui.label("No numeric values were recorded for this input.").style(
                "color: var(--matofx-muted-text);"
            )
        else:
            with ui.row().classes("w-full gap-4 flex-wrap"):
                _histogram(
                    "Across the simulated range",
                    row["simulated_range"],
                    values,
                    factor.get("simulated_lo"),
                    factor.get("simulated_hi"),
                    precision,
                )
                if factor.get("declared_lo") is None or factor.get("declared_hi") is None:
                    with ui.column().style("min-width: 24rem;"):
                        ui.label("Across the input range").style("font-weight: 500;")
                        ui.label(
                            "This tool doesn't state a minimum and maximum for this "
                            "input, so there's no range to measure against."
                        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
                else:
                    _histogram(
                        "Across the input range",
                        row["declared_range"],
                        values,
                        factor.get("declared_lo"),
                        factor.get("declared_hi"),
                        precision,
                    )
        ui.button("Close", on_click=dialog.close).props("flat")
    dialog.open()


def _histogram(
    title: str,
    subtitle: str,
    values: list[float],
    lo: Any,
    hi: Any,
    precision: float | None,
) -> None:
    """One labelled histogram over exactly ``lo``..``hi``."""
    import plotly.graph_objects as go  # noqa: PLC0415

    from matofx.ui.theme import themed_plotly  # noqa: PLC0415

    low = float(lo) if lo is not None else min(values)
    high = float(hi) if hi is not None else max(values)
    edges = nice_bins(low, high, len(values))
    counts = histogram_counts(values, edges)
    centres = [(a + b) / 2.0 for a, b in itertools.pairwise(edges)]
    widths = [(b - a) * 0.96 for a, b in itertools.pairwise(edges)]

    with ui.column().classes("gap-0").style("min-width: 24rem; flex: 1 1 24rem;"):
        ui.label(title).style("font-weight: 500;")
        ui.label(subtitle).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        figure = go.Figure(
            go.Bar(
                x=centres,
                y=counts,
                width=widths,
                marker={"color": "#40A1A4"},
                hovertemplate="%{y} runs<extra></extra>",
            )
        )
        figure.update_layout(
            margin={"l": 44, "r": 12, "t": 8, "b": 36},
            height=240,
            showlegend=False,
            bargap=0.0,
            xaxis={"range": [low, high], "title": ""},
            yaxis_title="runs",
        )
        ui.plotly(themed_plotly(figure)).classes("w-full")
        ui.label(
            f"{len(values)} runs, from {format_value(min(values), precision)} "
            f"to {format_value(max(values), precision)}"
        ).style("color: var(--matofx-muted-text); font-size: 0.75rem;")


def _render_pair_strip(report: dict[str, Any]) -> None:
    """One line per pair of inputs -- the level between a row and the verdict.

    **The finding that had nowhere to appear.** A one-factor-at-a-time archive
    passes every per-factor row and, at six or more inputs, passes the joint
    score too; the only place it showed was the scatter plot, where the user
    had to notice a cross and interpret it unaided. These rows say it.
    """
    pairs = list(report.get("per_pair") or [])
    note = pairs_note(report)
    if not pairs:
        # An empty sweep still has something to say (see ``pairs_note``): a
        # cohort too small to judge its pairs used to render this section blank,
        # which reads as "no problems found".
        if note:
            ui.label(note).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        return
    with ui.column().classes("w-full gap-1"):
        if note:
            ui.label(note).style("color: var(--matofx-muted-text); font-size: 0.8rem;")

        # **Two columns, because one number was answering two questions and
        # only the answer that read badly was on screen.** See `pair_chip`.
        ui.label(PAIR_COLUMNS_NOTE).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        with _centred_table(("Pair", "Runs", "Enough runs?", pair_spread_header(pairs))):
            for pair in pairs:
                label, colour = pair_chip(pair)
                spread_label, spread_colour = pair_spread_chip(pair)
                a = str(pair.get("a_label") or pair.get("a") or "")
                b = str(pair.get("b_label") or pair.get("b") or "")
                # A row that does not vote is dimmed rather than hidden: it is
                # still the honest picture of that pair, and hiding it would
                # leave the reader unable to see why the verdict ignored it.
                faded = "opacity: 0.62;" if pair_abstains(pair) else ""
                with ui.element("tr"):
                    with _cell():
                        ui.label(f"{a} vs {b}").style(f"font-size: 0.85rem; {faded}")
                    with _cell(align="right"):
                        ui.label(str(pair.get("n_points") or 0)).style(
                            f"font-size: 0.85rem; color: var(--matofx-muted-text); {faded}"
                        )
                    with _cell():
                        ui.badge(label).props(f"color={colour} outline").style(
                            f"font-size: 0.7rem; {faded}"
                        )
                    with _cell():
                        # **Chip and finding on one line.** The finding was a
                        # `display: block` under the badge, which pushed the
                        # column to two rows and left the chips no longer
                        # lining up down the table. A nowrap flex row keeps
                        # the badge whole and the sentence beside it, so every
                        # chip sits on the same baseline as its neighbours.
                        with (
                            ui.row()
                            .classes("items-center")
                            .style("gap: 0.5rem; flex-wrap: nowrap;")
                        ):
                            ui.badge(spread_label).props(f"color={spread_colour} outline").style(
                                f"font-size: 0.7rem; flex: 0 0 auto; {faded}"
                            )
                            # **The finding, not just the chip.** Four rows of
                            # "Barely spread" against one input read as noise
                            # and were dismissed as noise; the span that
                            # produced them is the most serious thing on the
                            # panel.
                            if pair.get("detail"):
                                ui.label(str(pair["detail"])).style(
                                    "color: var(--matofx-muted-text); font-size: 0.75rem; "
                                    f"white-space: nowrap; {faded}"
                                )

        # The floor comes off the report, not from re-reading config here: a
        # second copy of the number can drift from the one the engine applied,
        # and a stored report must explain itself with *its* floor, not
        # today's.
        abstain = pair_abstain_note(
            pairs, floor=int(report.get("min_discriminating_pair_runs") or 0)
        )
        if abstain:
            ui.label(abstain).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        _render_pair_legend()


def _render_pair_legend() -> None:
    """What each tag means, **once**, outside the table.

    Fifteen rows repeating the same three sentences is a wall nobody reads,
    and a tag whose meaning is only discoverable by hovering is a tag with no
    meaning at all.
    """
    with ui.expansion("What these tags mean").classes("w-full").props("dense"):
        for label, colour, meaning in pair_legend():
            with ui.row().classes("items-start gap-2 w-full").style("margin: 0.2rem 0;"):
                ui.badge(label).props(f"color={colour} outline").style(
                    "font-size: 0.7rem; min-width: 11rem;"
                )
                ui.label(meaning).style("font-size: 0.82rem; flex: 1;")


def _render_scatter(report: dict[str, Any]) -> None:
    """The pairwise scatter matrix — the visual payoff (§11.4)."""
    envelope = list(report.get("envelope") or [])
    points = list(report.get("points") or [])
    # Categorical axes join the matrix (a choice against a number is a strip,
    # a choice against a choice a tally). Left out, a material that was only
    # ever run in one corner of the space was invisible on every panel — and
    # invisible on every continuous *row* too, which is the whole reason
    # categorical imbalance is the silent one of the three failure modes.
    axes = grid_axes(envelope)
    if len(axes) < 2 or not points:
        return

    labels = factor_labels(envelope)
    n_panels = len(axes) * (len(axes) - 1) // 2
    with (
        ui.expansion(f"Pairwise plots — all {n_panels} combinations", icon="scatter_plot")
        .classes("w-full")
        .props("dense") as panel
    ):
        # **Built only when opened.** Every pair is drawn, with no selection,
        # so a thirteen-input tool is seventy-eight Plotly figures; building
        # those on every page load would make the panel slow for everyone to
        # serve the minority who open them.
        # Seventy-eight figures on a wide tool: say so while they build,
        # or the expansion looks like it opened onto nothing.
        async def _toggle(event: Any) -> None:
            if event.value:
                await _open_scatter_grid(panel, axes, labels, points, report)

        panel.on_value_change(_toggle)


async def _open_scatter_grid(
    panel: Any,
    axes: list[str],
    labels: dict[str, str],
    points: list[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    """Show a spinner, let it reach the browser, then build in its place.

    **The await is the whole point.** Building the figures is synchronous and
    holds the event loop, so a spinner created immediately before it is still
    sitting in the outgoing queue when the work starts and never renders --
    the panel simply freezes, which is exactly what the spinner was added to
    prevent. Yielding once flushes the pending update first.

    Async all the way up, or the coroutine is created and dropped and nothing
    happens at all ([[feedback_nicegui_async_click_chain]]).
    """
    if getattr(panel, "_grid_built", False):
        return
    with panel:
        placeholder = ui.column()
        with placeholder:
            _working("Drawing the pairwise plots…")
    await asyncio.sleep(_SPINNER_FLUSH_SECONDS)
    _build_scatter_grid(panel, axes, labels, points, report)
    placeholder.delete()


def _build_scatter_grid(
    panel: Any,
    axes: list[str],
    labels: dict[str, str],
    points: list[dict[str, Any]],
    report: dict[str, Any],
    suggested: list[dict[str, Any]] | None = None,
) -> None:
    """The lower-triangle matrix — every pair, laid out as a matrix (§11.4).

    A flat wrap of panels made the reader work out which pair each one was.
    Laid out as a triangle, column *i* is always the same input and row *j*
    always the same, so a row or column can be read as "everything against
    this input" — which is how somebody actually hunts for the empty corner.

    Ordered worst-covered first so the failures sit top-left, where the eye
    starts.
    """
    # A held-constant axis is not on the point rows — see ``with_pinned_values``.
    # Without this the material column drew and nothing landed on it.
    points = with_pinned_values(points, report.get("envelope") or [])
    if getattr(panel, "_grid_built", False):
        return
    panel._grid_built = True

    import plotly.graph_objects as go  # noqa: PLC0415

    from matofx.ui.theme import themed_plotly  # noqa: PLC0415

    scores = {
        str(p.get("a")): float(p.get("coverage_score") or 0.0)
        for p in (report.get("per_pair") or [])
    }
    levels = axis_levels(list(report.get("envelope") or []))
    # Option axes sort last: the score that orders the rest is a covering
    # distance, and a set of options has no distance to measure. Ranking them
    # by a number they do not have would be worse than putting them at the end.
    ordered = sorted(
        axes,
        key=lambda n: (n in levels, scores.get(n, 1.0), labels.get(n, n)),
    )
    # **Axes span the tool's own range, not the runs'.** Scaled to the values
    # present, a cross fills its panel corner to corner and looks covered; the
    # empty space only appears when the frame is the space the tool actually
    # allows. Falls back to the observed spread where the tool declares no
    # bounds -- there is no other frame to draw.
    ranges = _axis_ranges(report, points)

    with panel:
        ui.label(
            "Each panel plots two inputs against each other, over the full range "
            "the tool allows; a blank area is a combination no run has tried. "
            "An input with a fixed set of options gets a column per option — or, "
            "against another such input, a count of the runs at each combination. "
            "Worst-covered inputs first; option pickers last, since the score "
            "that orders the rest is a distance and options have none."
        ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
        # **The scroller and the grid have to be different elements.** With
        # `overflow-x: auto` and `max-width: 100%` on the *same* box, the box
        # is both the thing being measured and the thing doing the measuring,
        # and a sub-pixel rounding in Plotly's own sizing is enough to trip
        # the bar permanently. A wrapper at the panel's width, holding a grid
        # sized to its own content, only scrolls when the content is wider.
        with (
            ui.element("div").classes("w-full").style("overflow-x: auto; padding-bottom: 0.2rem;"),
            ui.element("div").style(
                f"display: grid; width: max-content; "
                f"grid-template-columns: repeat({len(ordered) - 1}, {_PLOT_CELL}); "
                f"gap: 0.25rem;"
            ),
        ):
            for row, y_name in enumerate(ordered[1:], start=1):
                for col, x_name in enumerate(ordered[:-1]):
                    if col >= row:
                        # Upper triangle is the same pair mirrored; drawing it
                        # doubles the work to say the same thing twice.
                        ui.element("div")
                        continue
                    _scatter_cell(
                        go,
                        themed_plotly,
                        points,
                        suggested,
                        x_name,
                        y_name,
                        labels,
                        ranges,
                        levels,
                    )


_PLOT_CELL = "11.9rem"
_PLOT_HEIGHT = 188
"""Panel size for the pairwise matrix.

A quarter larger than the first cut, which was legible only to somebody who
already knew what they were looking for."""


def _axis_ranges(
    report: dict[str, Any], points: list[dict[str, Any]]
) -> dict[str, tuple[float, float]]:
    """``{input: (lo, hi)}`` for plotting -- the tool's range where it has one."""
    out: dict[str, tuple[float, float]] = {}
    for factor in report.get("envelope") or []:
        name = str(factor.get("name") or "")
        lo, hi = factor.get("declared_lo"), factor.get("declared_hi")
        if lo is None or hi is None or float(hi) <= float(lo):
            observed = values_for(points, name)
            if not observed or max(observed) <= min(observed):
                continue
            lo, hi = min(observed), max(observed)
        out[name] = (float(lo), float(hi))
    return out


def _strip_cell(
    go: Any,
    theme: Any,
    points: list[dict[str, Any]],
    suggested: list[dict[str, Any]] | None,
    x_name: str,
    y_name: str,
    labels: dict[str, str],
    ranges: dict[str, tuple[float, float]],
    levels: dict[str, list[str]],
) -> None:
    """A choice against a number: one column of runs per option."""
    cat_name = x_name if x_name in levels else y_name
    num_name = y_name if cat_name == x_name else x_name
    cat_levels = levels[cat_name]

    def _trace(rows: list[dict[str, Any]], **marker: Any) -> Any:
        positions = strip_positions(cat_levels, [r.get(cat_name) for r in rows])
        numbers = [r.get(num_name) for r in rows]
        keep = [(p, n) for p, n in zip(positions, numbers, strict=True) if p is not None]
        cats = [p for p, _ in keep]
        nums = [n for _, n in keep]
        x, y = (cats, nums) if cat_name == x_name else (nums, cats)
        return go.Scatter(x=x, y=y, mode="markers", marker=marker)

    figure = go.Figure(_trace(points, size=3, color="#3B82C4", opacity=0.65))
    if suggested:
        figure.add_trace(_trace(suggested, size=5, color="#E8B23A", line={"width": 0.5}))

    category_axis: dict[str, Any] = {
        "title": {"text": labels.get(cat_name, cat_name), "font": {"size": 9}},
        "tickmode": "array",
        "tickvals": list(range(len(cat_levels))),
        "ticktext": cat_levels,
        "range": [-0.5, len(cat_levels) - 0.5],
        "tickfont": {"size": 8},
    }
    number_axis: dict[str, Any] = {
        "title": {"text": labels.get(num_name, num_name), "font": {"size": 9}},
        "tickfont": {"size": 8},
    }
    if num_name in ranges:
        number_axis["range"] = list(ranges[num_name])
    x_axis, y_axis = (
        (category_axis, number_axis) if cat_name == x_name else (number_axis, category_axis)
    )
    figure.update_layout(
        margin={"l": 30, "r": 6, "t": 18, "b": 28},
        height=_PLOT_HEIGHT,
        showlegend=False,
        xaxis=x_axis,
        yaxis=y_axis,
    )
    ui.plotly(theme(figure)).style(f"width: {_PLOT_CELL}; height: {_PLOT_HEIGHT}px;")


def _count_grid_cell(
    go: Any,
    theme: Any,
    points: list[dict[str, Any]],
    x_name: str,
    y_name: str,
    x_levels: list[str],
    y_levels: list[str],
    labels: dict[str, str],
) -> None:
    """Two choices against each other: how many runs at each combination."""
    grid = level_counts(points, x_levels, y_levels, x_name, y_name)
    figure = go.Figure(
        go.Heatmap(
            z=grid,
            x=x_levels,
            y=y_levels,
            text=[[str(v) for v in row] for row in grid],
            texttemplate="%{text}",
            textfont={"size": 9},
            colorscale=[[0.0, "#F2F4F7"], [1.0, "#3B82C4"]],
            showscale=False,
            hovertemplate="%{y} / %{x}: %{z} run(s)<extra></extra>",
        )
    )
    figure.update_layout(
        margin={"l": 30, "r": 6, "t": 18, "b": 28},
        height=_PLOT_HEIGHT,
        xaxis={
            "title": {"text": labels.get(x_name, x_name), "font": {"size": 9}},
            "tickfont": {"size": 8},
        },
        yaxis={
            "title": {"text": labels.get(y_name, y_name), "font": {"size": 9}},
            "tickfont": {"size": 8},
        },
    )
    ui.plotly(theme(figure)).style(f"width: {_PLOT_CELL}; height: {_PLOT_HEIGHT}px;")


def _scatter_cell(
    go: Any,
    theme: Any,
    points: list[dict[str, Any]],
    suggested: list[dict[str, Any]] | None,
    x_name: str,
    y_name: str,
    labels: dict[str, str],
    ranges: dict[str, tuple[float, float]],
    levels: dict[str, list[str]] | None = None,
) -> None:
    """One panel of the matrix, existing runs and any proposed ones.

    Three shapes, because a pair of axes is not always two numbers:

    - **number x number** — the scatter.
    - **choice x number** — a strip: one column per option, runs nudged apart
      inside it. A material that was only ever run in one corner of the space
      is invisible on every continuous axis and obvious here.
    - **choice x choice** — a tally grid, where a zero cell is a combination
      nobody has run.
    """
    levels = levels or {}
    x_levels, y_levels = levels.get(x_name), levels.get(y_name)
    if x_levels and y_levels:
        _count_grid_cell(go, theme, points, x_name, y_name, x_levels, y_levels, labels)
        return
    if x_levels or y_levels:
        _strip_cell(go, theme, points, suggested, x_name, y_name, labels, ranges, levels)
        return
    figure = go.Figure(
        go.Scatter(
            x=[p.get(x_name) for p in points],
            y=[p.get(y_name) for p in points],
            mode="markers",
            marker={"size": 3, "color": "#3B82C4", "opacity": 0.65},
            name="existing runs",
        )
    )
    if suggested:
        # New runs in a colour that reads as "added", so the space being
        # filled is visible at a glance rather than inferred from a table.
        figure.add_trace(
            go.Scatter(
                x=[p.get(x_name) for p in suggested],
                y=[p.get(y_name) for p in suggested],
                mode="markers",
                marker={"size": 5, "color": "#E8B23A", "line": {"width": 0.5}},
                name="proposed runs",
            )
        )
    x_axis: dict[str, Any] = {
        "title": {"text": labels.get(x_name, x_name), "font": {"size": 9}},
        "tickfont": {"size": 8},
    }
    y_axis: dict[str, Any] = {
        "title": {"text": labels.get(y_name, y_name), "font": {"size": 9}},
        "tickfont": {"size": 8},
    }
    if x_name in ranges:
        x_axis["range"] = list(ranges[x_name])
    if y_name in ranges:
        y_axis["range"] = list(ranges[y_name])

    figure.update_layout(
        margin={"l": 30, "r": 6, "t": 18, "b": 28},
        height=_PLOT_HEIGHT,
        showlegend=False,
        xaxis=x_axis,
        yaxis=y_axis,
    )
    ui.plotly(theme(figure)).style(f"width: {_PLOT_CELL}; height: {_PLOT_HEIGHT}px;")


# --- §11.5 suggest runs, and commit them ----------------------------------

_STUDY_CREATE = "parametric_studies.create"

_PREVIEW_DEBOUNCE_SECONDS = 0.6
"""Quiet period before a changed filter re-runs the preview.

Long enough that typing "12.5" is one query rather than four, short enough
that the list feels tied to the control."""


_FILL_SIMULATED = "simulated"
_FILL_DECLARED = "declared"

_FILL_CHOICES: dict[str, str] = {
    _FILL_SIMULATED: "The range these runs already cover",
    _FILL_DECLARED: "The full range the tool allows",
}
"""Which region the proposed runs should fill (§11.5).

**Two different jobs, and the right one depends on what the model is for.**
Filling the *simulated* range tightens a model over the operating window
somebody has already chosen -- the safe, cheap answer, and the default,
because widening a model's claims should never be something that happens
because a button defaulted to it. Filling the *declared* range extends it
across everything the tool accepts, which is what you want before a model is
asked about a design nobody has tried yet.

They are not variations on a theme: the second commissions runs in regions the
archive has never visited, and the coverage figures it quotes are measured
over the wider space, so the same batch reads worse against it. Saying which
is which is the difference between a choice and a surprise."""


def _declared_overrides(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Envelope overrides that open every varying axis to the tool's bounds.

    Axes the tool leaves unbounded keep the observed spread -- there is no
    wider range to claim, and inventing one would propose runs at values the
    form would reject.
    """
    return _bounds_overrides(report, kinds=("continuous", "integer"))


def _constant_overrides(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Overrides that turn held-constant inputs back into real axes (§11.5).

    "Always 60" is a fact about the runs so far, not a decision that it must
    stay 60. Giving such an axis the tool's own bounds promotes it to a
    varying one, so suggestions move it and every other reading -- the joint
    radius, the pairwise sweep, the histograms, the plots -- takes it into
    account, rather than each of them carrying a special case.

    Two shapes, because a domain is bounded or enumerated but not both. A
    held-constant **number** is promoted by the tool's declared bounds; a
    held-constant **choice** — a select, or a boolean — by its declared
    options. The engine decides which applies and records it as
    ``varying_kind``; this only has to send the right kind of override.

    Refused where the descriptor declares no domain at all. A material may
    not enumerate its options, so there is genuinely nothing to sweep.
    """
    numeric = _bounds_overrides(report, kinds=("pinned",), require_varying_kind=True)
    return numeric + _level_overrides(report)


_log = logging.getLogger(__name__)

VARYING_KINDS: tuple[str, ...] = ("continuous", "integer", "categorical")
"""The axis kinds a suggested batch can actually move.

Named rather than written out at each use. The literal appeared in two places
within a day of each other, which is how the pairwise matrices drifted apart —
and a test forbids the bare tuple precisely so the third copy cannot happen.
"""

_MEASURE_FULL = "full"
_MEASURE_CHOSEN = "chosen"
_MEASURE_EVERY_INPUT = "Every input"
_MEASURE_EVERY_EXPLORED = "Every explored input"
"""Two readings of the same option, and the difference is not cosmetic.

An input that was held constant and is still unticked sits **outside the
design space either way** — the figures never described it and will not start.
Calling that state "every input" claims a completeness the numbers do not
have. Once every such input is ticked they really are all in play, and the
plainer word becomes the honest one.
"""

_MEASURE_CHOICES = {
    _MEASURE_FULL: _MEASURE_EVERY_INPUT,
    _MEASURE_CHOSEN: "Only the ones I ticked",
}
"""What the coverage figures are a statement about.

Titled *"Measure coverage against"* with options *"The whole design space"* and
*"Only the inputs I chose"*, which read as a third, independent scope and
collided with **Fill across** — one says a *range*, this says a *set of
inputs*. Both options now name the ticks above them, so the control reads as
a follow-on question about the choice just made rather than a new one.
"""


def hold_value(factor: dict[str, Any]) -> Any:
    """A defensible value to hold a varying axis at while a batch runs.

    The midpoint of the range for a number, the first declared option for a
    choice. Every run in the batch lands on this value, so it puts a spike in
    that input's distribution — which is why :func:`held_rows` shows it and
    lets the user move it rather than leaving it implied.

    **Snapped to the input's own increment.** The midpoint of an observed
    2.0-3.5 on an input stepping in halves is ``2.75``, and a hold is emitted
    into every proposed run verbatim: ``Envelope.denormalise`` snaps the axes
    the batch moves and copies pinned values straight through. So the one
    route that produced a value the form would reject was the one nobody
    could see — see :func:`snap_to_step`.
    """
    lo, hi = factor.get("lo"), factor.get("hi")
    if lo is not None and hi is not None:
        mid = (float(lo) + float(hi)) / 2.0
        if factor.get("kind") == "integer":
            return round(mid)
        return snap_to_step(mid, _as_step(factor.get("precision")), lo=float(lo), hi=float(hi))
    levels = [str(v) for v in (factor.get("levels") or factor.get("declared_levels") or [])]
    return levels[0] if levels else factor.get("pinned_value")


def _as_step(value: Any) -> float | None:
    """A descriptor precision as a positive float, or ``None``."""
    try:
        step = float(value)
    except (TypeError, ValueError):
        return None
    return step if step > 0.0 else None


def held_rows(report: dict[str, Any], held: set[str]) -> list[dict[str, Any]]:
    """The axes in ``held`` that a batch would actually have to pin somewhere.

    Only a **varying** axis qualifies: one the archive already held constant
    is pinned whatever the user ticks, and offering to choose its value would
    imply the batch could move it.
    """
    return [
        factor
        for factor in (report.get("envelope") or [])
        if str(factor.get("name") or "") in held and factor.get("kind") in VARYING_KINDS
    ]


def hold_overrides(
    report: dict[str, Any],
    held: set[str],
    values: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Overrides that take the axes in ``held`` out of the batch.

    **Unconditional.** These were gated on the narrowed scope, so under the
    default *"Every input"* unticking an input did nothing whatever and the
    batch went on proposing runs that moved it — reported as "I unticked Fin
    height … and I get suggestions for Fin height".

    Which inputs the batch moves and which the figures describe are two
    questions, and this answers only the first. Pinning is what stops the walk
    moving an axis; whether the *measurement* also pins it is decided by
    sending these as the plan's ``measure_envelope`` or not
    (:func:`_measure_envelope`).

    ``values`` carries what the user typed into :func:`held_rows`; anything
    absent falls back to :func:`hold_value`.
    """
    out: list[dict[str, Any]] = []
    for factor in held_rows(report, held):
        name = str(factor.get("name") or "")
        chosen = (values or {}).get(name)
        out.append({"name": name, "hold": hold_value(factor) if chosen is None else chosen})
    return out


def measurement_envelope(
    rows: list[dict[str, Any]], *, narrowed: bool
) -> list[dict[str, Any]] | None:
    """The space the figures describe, when it differs from the batch's.

    ``None`` under the narrowed reading, where the two coincide and there is
    nothing to send. Otherwise the same rows **minus the pins**: an unticked
    input must stay out of the batch and stay in the assessment, and the pin is
    what does the first of those.
    """
    if narrowed:
        return None
    return [row for row in rows if "hold" not in row]


def explorable_axes(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Every axis a batch could be asked to explore, varying or not.

    A held-constant axis qualifies when the descriptor declares a domain to
    open it up into (``varying_kind``); a varying one always does.
    """
    out: list[dict[str, Any]] = []
    for factor in report.get("envelope") or []:
        kind = factor.get("kind")
        varying = kind in VARYING_KINDS
        if varying or (kind == "pinned" and factor.get("varying_kind")):
            out.append(factor)
    return out


def _level_overrides(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Overrides that open a held-constant *choice* across its options.

    Needs two or more levels for the same reason the numeric form needs
    ``hi > lo``: one option is what the axis already was, and promoting to it
    would report an axis as varying that no suggestion can move.
    """
    out: list[dict[str, Any]] = []
    for factor in report.get("envelope") or []:
        if factor.get("kind") != "pinned" or factor.get("varying_kind") != "categorical":
            continue
        levels = [str(v) for v in (factor.get("declared_levels") or []) if str(v)]
        if len(levels) < 2:
            continue
        out.append({"name": str(factor.get("name") or ""), "levels": levels})
    return out


def _bounds_overrides(
    report: dict[str, Any],
    *,
    kinds: tuple[str, ...],
    require_varying_kind: bool = False,
) -> list[dict[str, Any]]:
    """Overrides setting each matching axis to the tool's declared bounds."""
    out: list[dict[str, Any]] = []
    for factor in report.get("envelope") or []:
        if factor.get("kind") not in kinds:
            continue
        if require_varying_kind and not factor.get("varying_kind"):
            continue
        lo, hi = factor.get("declared_lo"), factor.get("declared_hi")
        if lo is None or hi is None or float(hi) <= float(lo):
            continue
        out.append({"name": str(factor.get("name") or ""), "lo": float(lo), "hi": float(hi)})
    return out


def _heading(text: str, *, top: str = "0.8rem") -> Any:
    """A small caps section label — the one style every control group uses."""
    return ui.label(text).style(
        "font-size: 0.75rem; font-weight: 600; letter-spacing: 0.04em; "
        f"color: var(--matofx-muted-text); text-transform: uppercase; margin-top: {top};"
    )


def _hint(text: str) -> Any:
    """The explanatory line under a control."""
    return ui.label(text).style(
        "color: var(--matofx-muted-text); font-size: 0.8rem; line-height: 1.35;"
    )


class _ChoiceButtons:
    """A row of buttons where exactly one is filled and the rest are outlined.

    Built by hand rather than from ``q-btn-toggle``. The component joins its
    segments into a single pill by design -- edges touching, corners shared --
    and every attempt to prise them apart fought it: a hand-drawn border gave
    two sets of edges, ``color=white`` only suited the light theme, and
    ``dense`` squashed the labels. Two buttons with a gap between them are
    what was wanted, and two buttons is what this draws.

    The accent is Quasar's ``primary``, which the app's theme already binds to
    the MATOFX teal, so the fill and the outline follow the theme in both light
    and dark rather than naming a colour twice.
    """

    def __init__(
        self,
        choices: dict[str, str],
        *,
        value: str,
        on_change: Callable[[str], None] | None = None,
    ) -> None:
        self.value = value
        self._on_change = on_change
        self._buttons: dict[str, Any] = {}
        with ui.row().classes("items-center").style("gap: 0.6rem; flex-wrap: wrap;"):
            for key, label in choices.items():
                self._buttons[key] = ui.button(
                    label, on_click=lambda _e=None, k=key: self.select(k)
                ).props("no-caps rounded color=primary")
        self._paint()

    def select(self, key: str, *, notify: bool = True) -> None:
        """Make ``key`` the active choice, telling the caller unless silenced.

        ``notify=False`` is for the code resetting the control rather than the
        user pressing it — a reset that fired the callback would re-enter
        whatever is doing the resetting.
        """
        if key not in self._buttons:
            return
        self.value = key
        self._paint()
        if notify and self._on_change is not None:
            self._on_change(key)

    def set_label(self, key: str, text: str) -> None:
        """Retitle one choice in place, leaving the selection alone."""
        button = self._buttons.get(key)
        if button is not None:
            button.set_text(text)

    def set_enabled(self, enabled: bool) -> None:
        """Grey the whole group out when the choice cannot mean anything.

        Offering a live control whose two options describe the same thing
        invites the reader to hunt for a difference that is not there.
        """
        for button in self._buttons.values():
            button.set_enabled(enabled)

    def _paint(self) -> None:
        """Filled for the active choice, transparent-with-an-outline for the rest."""
        for key, button in self._buttons.items():
            if key == self.value:
                button.props(remove="outline")
                button.props("unelevated")
            else:
                button.props(remove="unelevated")
                button.props("outline")


def _render_suggestions(
    report: dict[str, Any],
    overrides: dict[str, dict[str, Any]],
    cohort_id: int,
    cohort: dict[str, Any] | None = None,
) -> None:
    """The suggest-runs control and its commit half (§11.5).

    Reading the analysis is open; **committing** runs spends real compute, so
    the create-study half is gated on the study-create permission and
    **hidden rather than disabled** for a user without it — a greyed button
    that never explains itself is worse than no button.
    """
    if not _has_actionable_axis(report):
        return

    client = _client()
    # **A card, not another collapsed line.** This is the one thing on the
    # panel that *does* something about what everything above it reports, and
    # as a dense expansion it read as a fourth heading and got skipped.
    with (
        ui.card()
        .classes("w-full")
        .style("margin-top: 1rem; border-left: 4px solid var(--matofx-accent, #40A1A4);")
    ):
        with ui.row().classes("items-center gap-2"):
            ui.icon("auto_fix_high").style("color: var(--matofx-accent, #40A1A4);")
            ui.label("Fill the gaps").style("font-weight: 600; font-size: 1rem;")
        ui.label(
            "Work out how many more runs it would take to cover this space, "
            "what each batch size would buy, and exactly which runs to make."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

        widen = _declared_overrides(report)
        constants = _constant_overrides(report)

        # Order matters. "Which inputs?" is the question a reader answers
        # first, and both scope controls below are follow-ons from it — one
        # about the range those inputs sweep, one about what the resulting
        # figures describe. They used to be stacked in the reverse order with
        # a section heading each, which read as three unrelated settings.
        axes = explorable_axes(report)
        promotable = {c["name"] for c in constants}
        varying = {str(f.get("name")) for f in axes if f.get("kind") != "pinned"}
        _heading("Inputs to explore")
        axis_boxes: dict[str, Any] = {}
        with ui.row().classes("w-full items-center gap-x-5 gap-y-1 flex-wrap"):
            for factor in axes:
                name = str(factor.get("name") or "")
                held = factor.get("kind") == "pinned"
                label = str(factor.get("label") or name)
                if held:
                    label += "  (held so far)"
                axis_boxes[name] = ui.checkbox(
                    label, value=not held, on_change=lambda _e=None: _axes_changed()
                ).props("dense")
        if promotable:
            _hint(
                "Inputs that were the same in every run start unticked — nothing here "
                "can say what changing them would do. Tick one and the batch sweeps it: "
                "numbers across the tool's range, choices across their options."
            )

        # **What an unticked input is held at, said out loud.** Every run in
        # the batch carries this value, so unticking one input silently put a
        # spike into that input's distribution at a value the archive may
        # never have used — and the fill preview cannot show it, because the
        # preview is drawn over the batch's own space, where the axis has
        # been pinned out of existence. Editable because the midpoint is a
        # default, not a decision: "hold it at the value we actually ship"
        # is the common case and there was no way to say it.
        held_values: dict[str, Any] = {}
        held_panel = ui.column().classes("w-full gap-1").style("margin-top: 0.4rem;")

        def _render_held() -> None:
            """Redraw the held-value rows for whatever is currently unticked.

            Values live in ``held_values`` rather than in the widgets, so
            re-ticking an input and unticking it again brings back what the
            user chose rather than resetting to the midpoint.
            """
            held_panel.clear()
            rows = held_rows(report, {n for n, box in axis_boxes.items() if not box.value})
            if not rows:
                return
            with held_panel:
                _hint(
                    "Every run in the batch shares one value for the inputs you have "
                    "unticked. These are the values it will use — change any of them "
                    "if a different one is what you actually build."
                )
                with ui.row().classes("w-full items-center gap-x-5 gap-y-1 flex-wrap"):
                    for factor in rows:
                        _held_row(factor)

        def _held_row(factor: dict[str, Any]) -> None:
            """One "held at" control, drawn the way the axis's kind allows."""
            name = str(factor.get("name") or "")
            label = f"{factor.get('label') or name} held at"
            current = held_values.get(name)
            if current is None:
                current = hold_value(factor)
                held_values[name] = current

            def _changed(value: Any) -> None:
                held_values[name] = value
                _scope_changed()

            levels = [str(v) for v in (factor.get("levels") or [])]
            if factor.get("kind") == "categorical" and levels:
                ui.select(
                    levels,
                    label=label,
                    value=str(current),
                    on_change=lambda e: _changed(str(e.value)),
                ).props("dense outlined").style("min-width: 12rem;")
                return
            step = _as_step(factor.get("precision"))
            args: dict[str, Any] = {"precision": decimals_for(step)}
            if step:
                args["step"] = step
            ui.number(
                label=label,
                value=current,
                min=factor.get("lo"),
                max=factor.get("hi"),
                on_change=lambda e, f=factor: _changed(_snap_held(f, e.value)),
                **args,
            ).props("dense outlined").style("min-width: 10rem;")

        def _snap_held(factor: dict[str, Any], value: Any) -> Any:
            """A typed hold, put back on the input's own grid.

            The box steps in the right increment, but nothing stops a value
            being pasted or typed between two steps — and a hold reaches
            every proposed run untouched.
            """
            try:
                number = float(value)
            except (TypeError, ValueError):
                return hold_value(factor)
            lo, hi = factor.get("lo"), factor.get("hi")
            snapped = snap_to_step(
                number,
                _as_step(factor.get("precision")),
                lo=None if lo is None else float(lo),
                hi=None if hi is None else float(hi),
            )
            return round(snapped) if factor.get("kind") == "integer" else snapped

        def _scope_note(value: str) -> str:
            return (
                "Proposed runs stay inside the range these runs already cover."
                if value == _FILL_SIMULATED
                else "Proposed runs spread across everything the tool accepts, "
                "including regions no run has ever visited."
            )

        has_options = any(f.get("kind") == "categorical" for f in axes)

        def _refresh_caveats() -> None:
            """Keep the *Fill across* caveats true to what is ticked.

            The promoted half depends on the tick list, so it cannot be
            written once: an input only sweeps the tool's range in spite of
            this control while it is ticked.
            """
            text = fill_scope_caveats(
                has_options=has_options,
                promoted=[
                    str(f.get("label") or f.get("name"))
                    for f in axes
                    if f.get("kind") == "pinned"
                    and str(f.get("name") or "") in promotable
                    and axis_boxes[str(f.get("name") or "")].value
                ],
            )
            scope_caveats.set_text(text)
            scope_caveats.set_visibility(bool(text))

        def _measure_note(value: str) -> str:
            if value == _MEASURE_FULL:
                return (
                    "Unticked inputs keep reporting their own gaps, so a batch that "
                    "explores only some of them will not reach well covered — it has "
                    "not covered the rest. This describes your whole archive."
                )
            return (
                "Unticked inputs are set aside and the figures describe only the "
                "smaller space you picked. Easier to satisfy, so read a verdict here "
                "as a statement about those inputs rather than about the archive."
            )

        with ui.row().classes("w-full items-start gap-6 flex-wrap").style("margin-top: 0.7rem;"):
            with ui.column().classes("gap-1").style("flex: 1 1 20rem; min-width: 18rem;"):
                _heading("Fill across", top="0")
                scope = _ChoiceButtons(
                    dict(_FILL_CHOICES)
                    if widen
                    else {_FILL_SIMULATED: _FILL_CHOICES[_FILL_SIMULATED]},
                    value=_FILL_SIMULATED,
                    on_change=lambda v: _scope_changed(note=_scope_note(v)),
                )
                scope_note = _hint(_scope_note(_FILL_SIMULATED))
                # Two things this control does not govern, and both surface
                # as a proposed run somebody did not expect to see.
                scope_caveats = _hint("")
                _refresh_caveats()
                if not widen:
                    _hint(
                        "This tool states no minimum and maximum for its inputs, so "
                        "there is no wider range to fill."
                    )
            with ui.column().classes("gap-1").style("flex: 1 1 20rem; min-width: 18rem;"):
                _heading("Coverage figures describe", top="0")
                measure = _ChoiceButtons(
                    dict(_MEASURE_CHOICES),
                    value=_MEASURE_FULL,
                    on_change=lambda v: _scope_changed(note=_measure_note(v), target="measure"),
                )
                measure_note = _hint(_measure_note(_MEASURE_FULL))
                measure_idle = _hint("")

        def _axes_changed(*, rebuild: bool = True) -> None:
            """Keep the second scope's wording and liveness true to the ticks.

            Two independent questions, which is why there are three states.

            *Is there anything to leave out?* Only a **varying** input counts:
            one held constant is already outside the design space, so unticking
            it leaves nothing behind for the figures to keep reporting. With
            nothing left out the two options describe the same space, and a
            live control whose options are identical invites a reader to hunt
            for a difference that is not there — so it greys out.

            *Is every input actually in play?* A promotable input still
            unticked is not measured either way, so the first option reads
            **"Every explored input"**; once they are all ticked it earns the
            plainer **"Every input"**.
            """
            left_out = any(name in varying and not box.value for name, box in axis_boxes.items())
            held_out = any(name in promotable and not box.value for name, box in axis_boxes.items())
            measure.set_label(
                _MEASURE_FULL,
                _MEASURE_EVERY_EXPLORED if held_out else _MEASURE_EVERY_INPUT,
            )
            measure.set_enabled(left_out)
            if not left_out:
                measure.select(_MEASURE_FULL, notify=False)
                measure_note.set_text(_measure_note(_MEASURE_FULL))
                measure_idle.set_text(
                    "Every explored input so far is ticked."
                    if held_out
                    else "Every input is ticked, so there is nothing to leave out."
                )
            measure_note.set_visibility(left_out)
            measure_idle.set_visibility(not left_out)
            _refresh_caveats()
            _render_held()
            if rebuild:
                _scope_changed()

        # Nothing is unticked at first, so the second scope starts inert and
        # there is nothing being held.
        _axes_changed(rebuild=False)

        body = ui.column().classes("w-full gap-2")
        chosen = ui.column().classes("w-full gap-2")
        state: dict[str, Any] = {
            "points": [],
            "labels": {},
            "n": 0,
            "envelope": [],
            "report": report,
        }

        def _envelope() -> list[dict[str, Any]]:
            """The envelope the user has chosen to fill.

            Later choices replace the bounds typed into the form for the axes
            they cover; anything narrowed by hand on an axis neither option
            touches still stands.
            """
            chosen_rows: dict[str, dict[str, Any]] = {}
            if scope.value == _FILL_DECLARED:
                chosen_rows.update({r["name"]: r for r in _declared_overrides(report)})
            explored = {n for n, box in axis_boxes.items() if box.value}
            # A ticked held-constant input is promoted; an unticked one is
            # pinned so the batch cannot move it. **Always** — whether the
            # figures still describe it is a separate question, answered by
            # `_measure_envelope` below.
            chosen_rows.update(
                {r["name"]: r for r in _constant_overrides(report) if r["name"] in explored}
            )
            chosen_rows.update(
                {
                    r["name"]: r
                    for r in hold_overrides(
                        report,
                        {n for n in axis_boxes if n not in explored},
                        values=held_values,
                    )
                }
            )
            typed = list(overrides.values())
            return [
                *(o for o in typed if o.get("name") not in chosen_rows),
                *chosen_rows.values(),
            ]

        def _left_out_labels() -> list[str]:
            """Inputs the figures still cover that this batch cannot move.

            Empty under the narrowed reading, where the unticked inputs have
            been set aside from the measurement too and there is no mismatch
            left to explain. **Labels, never internal names**
            ([[feedback_use_labels_not_names]]).
            """
            if measure.value == _MEASURE_CHOSEN:
                return []
            held = {n for n, box in axis_boxes.items() if not box.value}
            return [str(f.get("label") or f.get("name")) for f in held_rows(report, held)]

        def _measure_envelope() -> list[dict[str, Any]] | None:
            """The space the figures describe, when it differs from the batch's.

            Under *"Only the ones I ticked"* they are the same space, so there
            is nothing to send. Under *"Every input"* the unticked axes must
            still be measured, so the pins that keep the batch off them are
            stripped back out for the measurement alone.
            """
            return measurement_envelope(_envelope(), narrowed=measure.value == _MEASURE_CHOSEN)

        def _show(n: int, option: dict[str, Any] | None = None) -> None:
            """Draw the gain and the runs for the chosen batch size.

            The batch is the plan's first ``n`` rows -- exact, not an
            approximation, because the suggestion walk is greedy.
            """
            chosen.clear()
            state["n"] = n
            rows = state["points"][:n]
            with chosen:
                if option:
                    verdict_label, colour = verdict_chip(str(option.get("verdict") or ""))
                    with ui.row().classes("items-center gap-2"):
                        ui.label(f"With {len(rows)} more runs:").style(
                            "font-weight: 500; font-size: 0.9rem;"
                        )
                        ui.badge(verdict_label).props(f"color={colour} outline").style(
                            "font-size: 0.7rem;"
                        )
                        ui.label(
                            f"biggest untried gap closes to "
                            f"{_pct(option.get('gap_after'))} of the space."
                        ).style("font-size: 0.85rem;")
                if len(rows) < n:
                    # Never let a typed number imply runs that do not exist.
                    ui.label(
                        f"Only {len(rows)} runs can be proposed here — the remaining "
                        f"inputs have no untried values left to suggest."
                    ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
                ui.label("The runs this would add:").style("font-weight: 500; font-size: 0.9rem;")
                _render_suggested_table(rows, state["labels"])
                _render_fill_preview(state["report"], rows)
                if has_permission(_STUDY_CREATE):
                    _render_create_study(client, cohort_id, state, cohort or {})

        async def _plan(sizes: list[int] | None = None) -> None:
            body.clear()
            chosen.clear()
            with body:
                _working("Working out what those runs would buy…")
            try:
                plan = await client.cohort_coverage_plan(
                    cohort_id,
                    envelope=_envelope(),
                    measure_envelope=_measure_envelope(),
                    sizes=sizes,
                )
            except ApiError as exc:
                body.clear()
                with body:
                    ui.label(exc.detail).style("color: var(--matofx-error-text);")
                return
            state["points"] = list(plan.get("points") or [])
            state["labels"] = dict(plan.get("factor_labels") or {})
            state["envelope"] = _envelope()
            # **The preview has to describe the space being filled**, which is
            # not the one the panel above describes once a scope is widened or
            # a held-constant input opened up: those change which axes exist.
            # Re-reading the report under the chosen envelope is what makes
            # the histograms and pairwise plots agree with the batch.
            try:
                state["report"] = await client.cohort_coverage(
                    cohort_id, envelope=state["envelope"]
                )
            except ApiError:
                # A preview is worth less than the plan it accompanies; fall
                # back to the panel's own report rather than losing both.
                state["report"] = report
            body.clear()
            with body:
                _render_plan_table(
                    plan,
                    _show,
                    _left_out_labels(),
                    measured_over_note(
                        widened=scope.value == _FILL_DECLARED,
                        narrowed=measure.value == _MEASURE_CHOSEN,
                    ),
                )
            # A typed size has exactly one answer, so show it rather than
            # making the user click the single row that came back.
            options = list(plan.get("options") or [])
            if sizes and len(options) == 1:
                _show(int(options[0].get("n") or 0), options[0])

        async def _plan_default() -> None:
            """Price the offered ladder.

            A named handler rather than `lambda: _plan()`: NiceGUI hands its
            callback the click event, and `_plan`'s first parameter is
            `sizes` -- passing it one would price a batch of "click event".
            It must stay `async` and `await`, or the coroutine is dropped and
            the button looks dead ([[feedback_nicegui_async_click_chain]]).
            """
            await _plan()

        async def _plan_typed() -> None:
            """Price a size the user typed, rather than one we offered.

            The ladder is a starting point, not a menu: somebody who knows
            they can afford seven runs should be able to ask about seven.
            """
            try:
                n = int(typed.value or 0)
            except (TypeError, ValueError):
                n = 0
            if n <= 0:
                ui.notify("Enter how many runs you want to price.", type="warning")
                return
            await _plan([n])

        def _scope_changed(note: str | None = None, target: str = "fill") -> None:
            """A priced batch belongs to one scope; drop it when that changes.

            Three controls can change the scope now — where to fill across,
            which inputs to explore, and what the figures are measured against
            — so the note being updated has to be named rather than assumed.
            """
            if note is not None:
                (measure_note if target == "measure" else scope_note).set_text(note)
            body.clear()
            chosen.clear()

        with ui.row().classes("items-center gap-3 w-full"):
            ui.button("Work out how many runs I need", on_click=_plan_default).props(
                "color=primary no-caps"
            )
            ui.label("or price a number of your own:").style(
                "color: var(--matofx-muted-text); font-size: 0.85rem;"
            )
            typed = (
                ui.number(label="Runs", min=1, max=250, precision=0)
                .props("dense outlined")
                .style("min-width: 9rem;")
            )
            ui.button("Check", on_click=_plan_typed).props("outline dense no-caps")


_SPINNER_FLUSH_SECONDS = 0.05
"""Long enough for a pending UI update to reach the browser before the event
loop is taken by synchronous work, short enough to be imperceptible."""


def _working(message: str) -> None:
    """A spinner beside a message, for anything the user waits on.

    Every slow path here is slow for a reason nobody can see: a covering
    radius over a few hundred runs, fifteen reference layouts, or seventy-eight
    Plotly figures. Bare text reads as a finished (and empty) answer, which is
    how a two-second wait becomes "it's broken".
    """
    with ui.row().classes("items-center gap-2"):
        ui.spinner(size="1.1rem")
        ui.label(message).style("color: var(--matofx-muted-text);")


def _render_plan_table(
    plan: dict[str, Any],
    on_pick: Any,
    left_out: list[str] | None = None,
    measured_over: str = "",
) -> None:
    """The batch-size cost/benefit table (§11.5).

    Answers "how many runs do I need?" rather than asking it. Each row is a
    batch size priced against the same measurement the verdict uses, so the
    user can see where the curve flattens and stop there instead of guessing
    a number -- which is what the old spinner asked them to do.

    ``left_out`` names the inputs the figures still cover that this batch
    cannot move. Under the default reading that is the normal state once
    anything is unticked, and it is why a table of rows can sit perfectly
    still at every size -- which reads as a broken feature unless it is said.
    """
    options = list(plan.get("options") or [])
    for warning in plan.get("warnings") or []:
        ui.label(str(warning.get("message") or "")).style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )
    if not options:
        ui.label("There are no runs worth suggesting here.").style(
            "color: var(--matofx-muted-text);"
        )
        return

    now_label, now_colour = verdict_chip(str(plan.get("current_verdict") or ""))
    with ui.row().classes("items-center gap-2"):
        ui.label("Today:").style("font-size: 0.85rem;")
        ui.badge(now_label).props(f"color={now_colour} outline").style("font-size: 0.7rem;")
        ui.label(plan_today_line(plan)).style("font-size: 0.85rem;")
    # **Which space "Today" was taken over.** It is the space the *figures*
    # describe; the verdict badge at the top of the panel is the space the
    # report describes. Under a widened scope the two routinely disagree,
    # and a reader comparing them with nothing to explain the difference
    # concludes one of them is wrong.
    if measured_over:
        ui.label(measured_over).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
    ui.label(plan_gap_note(plan)).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
    note = unmovable_note(left_out or [])
    if note:
        ui.label(note).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
    if plan.get("already_good"):
        ui.label(
            "These runs already cover the space well — anything below would "
            "tighten it further rather than fix a problem."
        ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")

    with ui.column().classes("w-full gap-1"):
        for option in options:
            verdict_label, colour = verdict_chip(str(option.get("verdict") or ""))
            n = int(option.get("n") or 0)
            with ui.row().classes("w-full items-center gap-3"):
                ui.button(
                    f"{n} runs",
                    on_click=lambda _e=None, k=n, o=option: on_pick(k, o),
                ).props("outline dense no-caps color=primary").style("min-width: 8rem;")
                ui.badge(verdict_label).props(f"color={colour} outline").style("font-size: 0.7rem;")
                if int(plan.get("measured_axes") or 0) > 0:
                    ui.label(f"gap closes to {_pct(option.get('gap_after'))} of the space").style(
                        "font-size: 0.85rem;"
                    )
                # The gap is a covering radius and says nothing about option
                # balance, so a batch sweeping two materials would otherwise
                # read as buying nothing at all.
                short_before = int(plan.get("current_levels_short") or 0)
                short_after = int(option.get("levels_short") or 0)
                if short_before or short_after:
                    ui.label(f"· options under-run {short_before} → {short_after}").style(
                        "font-size: 0.85rem; color: var(--matofx-muted-text);"
                    )
                if option.get("reaches_good"):  # the knee of the curve
                    # The knee of the curve. Without it every row looks like
                    # an equally good buy, which is how a user ends up
                    # commissioning twenty runs to gain what eight would.
                    ui.label("— enough to call it well covered").style(
                        "color: var(--matofx-ok-text, #2C7A4B); font-size: 0.8rem;"
                    )
    # The rating is an *efficiency*: it asks whether the runs are as well
    # spread as that many runs could be, so a bigger archive is held to a
    # higher standard and the rating can dip while the gaps genuinely close.
    # The gap column is the monotone one, which is why it leads each row.
    if int(plan.get("measured_axes") or 0) > 0:
        ui.label(
            "The gap always shrinks as you add runs. The rating can still move "
            "either way — it measures how well spread the runs are for how many "
            "there are, so a larger set is judged against a higher bar."
        ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")


def _pct(value: Any) -> str:
    """A 0..1 fraction as a whole-number percentage."""
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return "0%"
    return f"{round(max(0.0, min(1.0, number)) * 100)}%"


def _render_fill_preview(report: dict[str, Any], suggested: list[dict[str, Any]]) -> None:
    """What the archive would look like with the proposed runs in it.

    **The table says which runs; this says what they do.** A list of numbers
    cannot show that a batch lands squarely in the empty half of a square,
    which is the only thing the reader actually wants to know before spending
    solver time. Existing runs in blue, proposed in amber, in both the
    per-input histograms and the pairwise panels.
    """
    envelope = list(report.get("envelope") or [])
    points = list(report.get("points") or [])
    # **One list for both rows.** They were built separately — numbers only for
    # the histograms, ``grid_axes`` for the matrix — so an input could appear in
    # one and not the other. Same axes, and each drawn the way its kind allows.
    matrix_axes = grid_axes(envelope)
    if not matrix_axes or not points or not suggested:
        return

    labels = factor_labels(envelope)
    precision = {str(f.get("name")): f.get("precision") for f in envelope}

    # It read as a heading rather than a control, so it went unclicked: an
    # accent rule, a tinted bar and an explicit "click to open" say what it is.
    with (
        ui.expansion("See where these runs would go — click to open", icon="insights")
        .classes("w-full")
        .props("dense expand-icon=expand_more")
        .style(
            "border: 1px solid var(--matofx-accent, #40A1A4); border-radius: 6px; "
            "background: color-mix(in srgb, var(--matofx-accent, #40A1A4) 12%, "
            "transparent); margin-top: 0.4rem;"
        ) as panel
    ):

        async def _toggle(event: Any) -> None:
            if event.value:
                await _open_fill_preview(
                    panel, matrix_axes, labels, precision, points, suggested, report
                )

        panel.on_value_change(_toggle)


async def _open_fill_preview(
    panel: Any,
    matrix_axes: list[str],
    labels: dict[str, str],
    precision: dict[str, Any],
    points: list[dict[str, Any]],
    suggested: list[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    """Show a spinner, let it reach the browser, then build in its place."""
    if getattr(panel, "_fill_built", False):
        return
    with panel:
        placeholder = ui.column()
        with placeholder:
            _working("Drawing where the new runs would go…")
    await asyncio.sleep(_SPINNER_FLUSH_SECONDS)
    _build_fill_preview(panel, matrix_axes, labels, precision, points, suggested, report)
    placeholder.delete()


def _build_fill_preview(
    panel: Any,
    matrix_axes: list[str],
    labels: dict[str, str],
    precision: dict[str, Any],
    points: list[dict[str, Any]],
    suggested: list[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    """Histograms and the pairwise matrix, existing against proposed."""
    if getattr(panel, "_fill_built", False):
        return
    panel._fill_built = True

    with panel:
        ui.label("Each input, before and after").style("font-weight: 500; font-size: 0.85rem;")
        with ui.row().classes("w-full gap-3 flex-wrap"):
            # **The same axes as the matrix below, by construction.** The two
            # rows answer one question between them — what would the archive
            # become? — so an input in one and missing from the other is a
            # reader hunting for a panel that is not there. They were built
            # from different lists, so a held-constant choice drew a strip in
            # the matrix and no bar up here.
            hist_envelope = report.get("envelope") or []
            levels_by_axis = axis_levels(hist_envelope)
            filled = with_pinned_values(points, hist_envelope)
            for name in matrix_axes:
                levels = levels_by_axis.get(name)
                if levels:
                    # Counts per option are the whole story; there is nothing
                    # to bin.
                    _stacked_levels(
                        labels.get(name, name),
                        [str(row.get(name)) for row in filled if row.get(name) is not None],
                        [str(row.get(name)) for row in suggested if row.get(name) is not None],
                        levels,
                    )
                else:
                    # ``filled``, not ``points`` — the same reason the matrix
                    # fills them (:func:`with_pinned_values`). A held-constant
                    # axis is absent from the raw rows, so now that a pinned
                    # *number* earns a chart the existing runs would have had
                    # no bar at all on it: an empty histogram beside a full
                    # amber spike, reading as "nothing was ever run here".
                    _stacked_histogram(
                        labels.get(name, name),
                        values_for(filled, name),
                        values_for(suggested, name),
                        precision.get(name),
                    )
        ui.label("Every pair, before and after").style(
            "font-weight: 500; font-size: 0.85rem; margin-top: 0.6rem;"
        )
        _build_scatter_grid(_FreshPanel(), matrix_axes, labels, points, report, suggested=suggested)


class _FreshPanel:
    """A stand-in that lets the shared grid builder draw into the current slot.

    ``_build_scatter_grid`` latches so an expansion cannot rebuild itself on
    every toggle; the fill preview has its own latch one level up, so it
    passes a fresh object rather than defeating that one.
    """

    _grid_built = False

    def __enter__(self) -> _FreshPanel:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _stacked_histogram(
    label: str,
    existing: list[float],
    proposed: list[float],
    precision: Any,
) -> None:
    """One input's spread, existing stacked with proposed.

    Stacked rather than side by side: the question is what the archive would
    *become*, and two separate bars invite the reader to compare heights
    instead of reading the total.
    """
    import plotly.graph_objects as go  # noqa: PLC0415

    from matofx.ui.theme import themed_plotly  # noqa: PLC0415

    values = existing + proposed
    if not values:
        return
    lo, hi = min(values), max(values)
    edges = nice_bins(lo, hi, len(values))
    centres = [(a + b) / 2.0 for a, b in itertools.pairwise(edges)]
    widths = [(b - a) * 0.96 for a, b in itertools.pairwise(edges)]

    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            x=centres,
            y=histogram_counts(existing, edges),
            width=widths,
            name="existing",
            marker={"color": "#3B82C4"},
        )
    )
    figure.add_trace(
        go.Bar(
            x=centres,
            y=histogram_counts(proposed, edges),
            width=widths,
            name="proposed",
            marker={"color": "#E8B23A"},
        )
    )
    figure.update_layout(
        barmode="stack",
        margin={"l": 36, "r": 8, "t": 24, "b": 28},
        height=180,
        bargap=0.0,
        showlegend=False,
        title={"text": label, "font": {"size": 11}},
        xaxis={"range": [lo, hi], "tickfont": {"size": 8}},
        yaxis={"tickfont": {"size": 8}},
    )
    with ui.column().classes("gap-0").style("width: 15rem;"):
        ui.plotly(themed_plotly(figure)).style("width: 15rem; height: 180px;")
        ui.label(
            f"{len(existing)} existing + {len(proposed)} proposed  "
            f"({format_range(lo, hi, precision)})"
        ).style("color: var(--matofx-muted-text); font-size: 0.72rem;")


def _stacked_levels(
    label: str,
    existing: list[str],
    proposed: list[str],
    levels: list[str],
) -> None:
    """One option-valued input's spread, existing stacked with proposed.

    The categorical twin of :func:`_stacked_histogram`, drawn to the same size
    so the two sit in one row. Every declared option gets a column even at
    zero — an empty column is the point, exactly as on the pairwise strips: it
    says the archive has never tried that option, and the footer names which.
    """
    import plotly.graph_objects as go  # noqa: PLC0415

    from matofx.ui.theme import themed_plotly  # noqa: PLC0415

    if not levels:
        return
    existing_counts = [existing.count(level) for level in levels]
    proposed_counts = [proposed.count(level) for level in levels]

    figure = go.Figure()
    figure.add_trace(
        go.Bar(x=levels, y=existing_counts, name="existing", marker={"color": "#3B82C4"})
    )
    figure.add_trace(
        go.Bar(x=levels, y=proposed_counts, name="proposed", marker={"color": "#E8B23A"})
    )
    figure.update_layout(
        barmode="stack",
        margin={"l": 36, "r": 8, "t": 24, "b": 28},
        height=180,
        showlegend=False,
        title={"text": label, "font": {"size": 11}},
        xaxis={"tickfont": {"size": 8}, "type": "category"},
        yaxis={"tickfont": {"size": 8}},
    )
    untried = [level for level, n in zip(levels, existing_counts, strict=True) if not n]
    with ui.column().classes("gap-0").style("width: 15rem;"):
        ui.plotly(themed_plotly(figure)).style("width: 15rem; height: 180px;")
        ui.label(
            f"{len(existing)} existing + {len(proposed)} proposed  "
            + (f"(never run: {', '.join(untried)})" if untried else f"({len(levels)} options)")
        ).style("color: var(--matofx-muted-text); font-size: 0.72rem;")


def _render_suggested_table(points: list[dict[str, Any]], labels: dict[str, str]) -> None:
    """The proposed runs and their input values, before anything is committed.

    Headed by descriptor **labels** ([[feedback_use_labels_not_names]]); the
    row data stays keyed by internal name, which is what the study builder
    needs downstream.
    """
    if not points:
        return
    columns = suggestion_columns(points)
    ui.table(
        columns=[
            {"name": c, "label": labels.get(c, c), "field": c, "align": "left"} for c in columns
        ],
        rows=[{c: p.get(c) for c in columns} for p in points],
    ).classes("w-full").props("dense flat")


def default_study_name(cohort: Mapping[str, Any]) -> str:
    """The pre-filled name for a gap-filling study.

    Named after the **tool**, not the cohort: a project accumulates these, and
    "Fill coverage gaps" three times over says nothing about which is which.
    Labels, never aliases ([[feedback_use_labels_not_names]]), with the alias
    only as the fallback a cohort whose tool no longer resolves would need.
    """
    label = str(cohort.get("tool_name") or cohort.get("tool_alias") or "").strip()
    return f"{label} - Fill coverage gaps" if label else "Fill coverage gaps"


def _render_create_study(
    client: ApiClient,
    cohort_id: int,
    state: dict[str, Any],
    cohort: Mapping[str, Any] | None = None,
) -> None:
    """The commit half — a governed draft study, never an auto-submitted run.

    Takes the envelope from ``state`` rather than the live controls: it must be
    the one the batch on screen was priced against, and re-reading the controls
    would let a scope toggled after the plan was drawn commission runs over a
    region the user never saw costed.
    """
    cohort = cohort or {}
    taken: set[str] = set()

    with ui.column().classes("w-full gap-1"):
        with ui.row().classes("w-full items-start gap-3"):
            name = (
                ui.input(label="Study name", value=default_study_name(cohort))
                .props("dense outlined")
                .style("min-width: 20rem;")
            )
            project = (
                ui.select({}, label="Project", with_input=True)
                .props("dense outlined")
                .style("min-width: 16rem;")
            )
            # Handler wired below, once `_create` exists: the button sits
            # inside the row for layout, and `_create` closes over widgets
            # this row is still building.
            create = (
                ui.button("Create a parametric study and add to Cohort")
                .props("color=primary no-caps")
                .style("margin-top: 0.15rem;")
            )

        clash = ui.label("").style("color: var(--matofx-error-text, #A93226); font-size: 0.8rem;")
        clash.visible = False

    async def _load_projects() -> None:
        """Fill the picker with the projects this user can actually see.

        A free-text id asked the user to know a database key, and accepted any
        integer they typed -- including one they have no access to, which comes
        back as a permission error only after the batch has been priced.
        """
        try:
            projects = await client.list_projects()
        except ApiError as exc:
            clash.set_text(exc.detail)
            clash.visible = True
            return
        project.options = {int(p["id"]): str(p.get("name") or p["id"]) for p in projects}
        if len(project.options) == 1:
            project.value = next(iter(project.options))
        project.update()
        await _check_clash()

    async def _check_clash() -> None:
        """Warn *before* the click when the name is already used in that project.

        The server would reject it anyway, but only after the user had pressed
        a button they had every reason to expect to work. The check is a read,
        so doing it as they type costs nothing they are not already paying.
        """
        nonlocal taken
        if not project.value:
            _set_clash(shown=False)
            return
        try:
            studies = await client.list_studies(int(project.value))
        except ApiError:
            # A picker that cannot list is not a name that is free; say
            # nothing rather than claim the name is available.
            _set_clash(shown=False)
            return
        taken = {str(st.get("name") or "").strip().casefold() for st in studies}
        _set_clash(shown=str(name.value or "").strip().casefold() in taken)

    def _set_clash(*, shown: bool) -> None:
        """Red outline, red text and a reason -- or none of the three."""
        clash.visible = shown
        if shown:
            clash.set_text(
                "A study with this name already exists in the chosen project. "
                "Change the name to continue."
            )
            name.props("error")
            name.props('error-message=""')
        else:
            name.props(remove="error")

    def _name_typed() -> None:
        """Re-check against the list already loaded -- no request per keystroke."""
        _set_clash(shown=str(name.value or "").strip().casefold() in taken)

    name.on_value_change(lambda _e: _name_typed())
    project.on_value_change(lambda _e: _check_clash())

    async def _create() -> None:
        if not project.value:
            ui.notify("Choose a project for the new study.", type="warning")
            return
        await _check_clash()
        if clash.visible:
            ui.notify("That study name is already used in this project.", type="warning")
            return
        # Generating the design and queueing its runs is a round-trip that takes
        # a visible moment. Without a busy state the button looks inert, so it
        # invites a second click — and a second click here means a second study
        # with a clashing name. Same idiom as the model build button: spinner on
        # the control, a notification that cannot auto-dismiss, both cleared in
        # `finally` so an error path never leaves the page stuck.
        create.props("loading")
        create.disable()
        progress = ui.notification(
            "Creating the study and queueing its runs…",
            type="ongoing",
            spinner=True,
            timeout=None,
        )
        try:
            study = await client.cohort_coverage_create_study(
                cohort_id,
                n=int(state["n"] or len(state["points"])),
                project_id=int(project.value),
                name=str(name.value or default_study_name(cohort)),
                # The same envelope the plan was priced against — a study
                # commissioned over a different region than the one the
                # user was shown would deliver a gain nobody asked for.
                envelope=state["envelope"],
            )
        except ApiError as exc:
            ui.notify(exc.detail, type="negative")
            return
        finally:
            with contextlib.suppress(RuntimeError):
                progress.dismiss()
                create.props(remove="loading")
                create.enable()
        # Say what actually happened. `submit` honours the tool's own approval
        # policy, so the same button ends in two different places and the
        # message must not claim the wrong one.
        # Not `state` — that name is the enclosing batch dict this function
        # already reads, and shadowing it makes every earlier `state[...]` an
        # UnboundLocalError.
        study_state = str(study.get("state") or "")
        if study_state == "pending_approval":
            ui.notify(
                "Study created and submitted — it needs approval before the runs start.",
                type="positive",
            )
        else:
            ui.notify("Study created — the runs are queued.", type="positive")
        ui.navigate.to(f"/studies/{study['id']}")

    create.on("click", _create)
    ui.timer(0.0, _load_projects, once=True)

    ui.label(
        "The runs are queued straight away — you have already seen each of them above. "
        "If this tool needs approval before running, the study waits at that gate "
        "instead. Either way, results join this cohort as they finish, so the coverage "
        "above updates without you adding them by hand."
    ).style("color: var(--matofx-muted-text); font-size: 0.8rem;")


def _has_actionable_axis(report: dict[str, Any]) -> bool:
    """Whether any axis a new run could move exists at all.

    **Two kinds of axis were missing from this test**, and both hid the card
    on the cohort that most needed it.

    A varying **choice** is something a batch can move — the engine now plans
    a batch of nothing but options (``_fill_option_gaps``), and gating the
    card on a numeric axis made that unreachable. And a **promotable
    held-constant** input is the whole point of the tick list: on an archive
    where every number happened to be held at one value, the one control that
    would open them up was not drawn at all.

    Still false for a genuinely immovable design: ``derived`` axes (a
    snapshot index arrives from the solver) and a pinned input the descriptor
    gives no domain to sweep.
    """
    return any(
        (f.get("actionable", True) and f.get("kind") in VARYING_KINDS)
        or (f.get("kind") == "pinned" and f.get("varying_kind"))
        for f in (report.get("envelope") or [])
    )
