"""Admin → Lineage (``/admin/lineage`` + ``/admin/lineage/{kind}/{obj_id}``).

The standalone provenance-graph surface from design §6.0: a centre
picker (kind dropdown + id search) plus the shared
:func:`matofx.ui.lineage_view.render_lineage_view` component rendered
in the admin shell. Used for the centres the inline-tab pattern
can't cover — chiefly the admin user-activity case — and as the
canonical deep-link target for the export API and the ADR-0004
report's "Open as PDF report" link.

Permission-gated by ``lineage.view``. The sidebar entry is admin-only
nav membership; a non-admin holder of ``lineage.view`` can still
deep-link here. The actual ACL gate fires server-side — a 403 / 404
surfaces as a friendly inline notice via the component's error path.

Slice 4a (this slice) ships the table-only view. Slice 4b layers
Cytoscape.js + dagre on top in the same component, so the page
itself doesn't change shape when the graph view lands.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.lineage import NodeKind
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.lineage_view import render_lineage_view
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token

# Kinds the picker offers — every member of :class:`NodeKind`, with
# human-readable labels. Reserved-for-later kinds (``optimisation_study``
# / ``job_cohort``) are included so a deep link generated against a
# future ADR validates; selecting one returns the component's "not
# found" error path until the resolver ships.
_KIND_OPTIONS: dict[str, str] = {
    NodeKind.JOB.value: "Job",
    NodeKind.WORKFLOW_RUN.value: "Workflow run",
    NodeKind.PARAMETRIC_STUDY.value: "Parametric study",
    NodeKind.OPTIMISATION_STUDY.value: "Optimisation study",
    NodeKind.JOB_COHORT.value: "Job cohort",
    NodeKind.TOOL_VERSION.value: "Tool version",
    NodeKind.LIBRARY_FILE_VERSION.value: "Library file version",
    NodeKind.MATERIAL_VERSION.value: "Material version",
    NodeKind.SURROGATE_MODEL_VERSION.value: "Surrogate model version",
    NodeKind.SCENARIO.value: "Scenario",
    NodeKind.PROJECT.value: "Project",
    NodeKind.USER.value: "User",
    NodeKind.APPROVAL_REQUEST.value: "Approval request",
    NodeKind.VV_RECORD.value: "V&V record",
}

_DEFAULT_KIND = NodeKind.JOB.value


@ui.page("/admin/lineage")
async def admin_lineage_root_page() -> None:
    """Centre-picker landing surface — no centre yet, just the form."""
    if not require_logged_in():
        return
    render_shell()
    with ui.column().classes("w-full mx-auto p-6 gap-4").style("max-width: 1400px;"):
        _render_title()
        ui.label(
            "Pick a centre node below to assemble its lineage graph. "
            "Every link rendered is read-only and intersects with "
            "your own per-domain access."
        ).style("color: var(--matofx-muted-text);")
        await _render_centre_picker(kind=_DEFAULT_KIND, obj_id="")


@ui.page("/admin/lineage/{kind}/{obj_id}")
async def admin_lineage_page(kind: str, obj_id: str) -> None:
    """Render lineage centred on ``(kind, obj_id)`` inside the admin shell.

    ``obj_id`` is a path string so we can validate friendly-error
    (rather than letting NiceGUI's int coercion blow up); we accept
    only positive integers, anything else falls through to a clean
    error message in the page body.
    """
    if not require_logged_in():
        return
    render_shell()
    with ui.column().classes("w-full mx-auto p-6 gap-4").style("max-width: 1400px;"):
        _render_title()
        await _render_centre_picker(kind=kind, obj_id=obj_id)
        if kind not in _KIND_OPTIONS:
            _render_bad_kind(kind)
            return
        try:
            obj_id_int = int(obj_id)
        except ValueError:
            _render_bad_obj_id(obj_id)
            return
        if obj_id_int < 1:
            _render_bad_obj_id(obj_id)
            return

        container = ui.column().classes("w-full gap-4")
        await render_lineage_view(container, kind=kind, obj_id=obj_id_int)


# ---- shared bits --------------------------------------------------------


def _render_title() -> None:
    """Page heading + explanatory sub-header, identical on both routes."""
    ui.label("Lineage (Provenance Graph)").style(
        "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
    )
    ui.label(
        "This page provides traceability of things such as users, tools, "
        "studies, workflows, and their relationships, alongside a provenance "
        "graph that visualises these dependencies and lineage."
    ).style("color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;")


async def _render_centre_picker(*, kind: str, obj_id: str) -> None:
    """Kind dropdown + populated id selector → navigate to the canonical URL.

    The id selector is a real :class:`ui.select` populated from
    ``/api/lineage/picker/{kind}``. Changing the kind triggers a
    fresh picker fetch + repopulates the id options. ``with_input``
    keeps the id select keyboard-friendly — operators can type to
    filter the dropdown, exactly like a search box.
    """
    initial_kind = kind if kind in _KIND_OPTIONS else _DEFAULT_KIND
    state: dict[str, Any] = {
        "kind": initial_kind,
        "obj_id": obj_id or "",
        "id_options": {},  # int id → label string, populated below
    }

    client = ApiClient(get_app(), token=current_token())

    async def _reload_ids(selected_kind: str, *, prefer_id: str | None = None) -> None:
        try:
            entries = await client.lineage_picker(kind=selected_kind)
        except ApiError as exc:
            ui.notify(f"Couldn't load IDs for {selected_kind}: {exc.detail}", type="negative")
            entries = []
        options = {int(e["id"]): str(e["label"]) for e in entries}
        state["id_options"] = options
        # If the URL came in with an explicit id, keep it selected
        # when it's in the new list; otherwise fall through to the
        # first option (if any), or leave blank.
        new_value: int | None = None
        if prefer_id:
            try:
                pid = int(prefer_id)
                if pid in options:
                    new_value = pid
            except ValueError:
                pass
        if new_value is None and options:
            new_value = next(iter(options))
        id_select.set_options(options, value=new_value)
        state["obj_id"] = str(new_value) if new_value is not None else ""

    async def go() -> None:
        if not state["kind"] or not state["obj_id"]:
            ui.notify("Pick a kind and an ID to open.", type="warning")
            return
        ui.navigate.to(f"/admin/lineage/{state['kind']}/{state['obj_id']}")

    async def _on_kind_change(event: Any) -> None:
        state["kind"] = str(event.value)
        await _reload_ids(state["kind"])

    with (
        ui.row()
        .classes("items-end gap-3 w-full")
        .style("padding: 0.5rem 0; border-bottom: 1px solid var(--matofx-border);")
    ):
        kind_select = ui.select(
            _KIND_OPTIONS,
            value=initial_kind,
            label="Kind",
        ).classes("min-w-64")
        kind_select.on_value_change(_on_kind_change)

        # The id select starts empty + populates from the picker fetch.
        # ``with_input`` makes the dropdown filterable as the user types.
        id_select = (
            ui.select(
                options={},
                value=None,
                label="ID",
                with_input=True,
            )
            .classes("min-w-96")
            .props("clearable use-input input-debounce=200")
        )
        id_select.on_value_change(
            lambda e: state.update(obj_id=str(e.value) if e.value is not None else "")
        )

        ui.button("Open", on_click=go).props("color=primary unelevated")

    # First fetch — pre-select the URL-supplied id when it's in the
    # initial kind's list.
    await _reload_ids(initial_kind, prefer_id=obj_id or None)


def _render_bad_kind(kind: str) -> None:
    """Inline error when the path's ``kind`` isn't a known node kind."""
    with ui.column().classes("w-full gap-2"):
        ui.label("Unknown node kind").style("font-size: 1.1rem; font-weight: 600;")
        ui.label(
            f"'{kind}' isn't a recognised lineage node kind. "
            "Pick one of the kinds in the dropdown above."
        ).style("color: var(--matofx-muted-text);")


def _render_bad_obj_id(obj_id: str) -> None:
    """Inline error when the path's ``obj_id`` isn't a positive integer."""
    with ui.column().classes("w-full gap-2"):
        ui.label("Invalid id").style("font-size: 1.1rem; font-weight: 600;")
        ui.label(f"'{obj_id}' isn't a valid id. Enter a positive integer in the box above.").style(
            "color: var(--matofx-muted-text);"
        )
