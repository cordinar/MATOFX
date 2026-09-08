"""Materials database UI (ADR-0007 §8 / designs/materials-database.md §8).

Two surfaces, both behind the ``features.materials_database`` flag (the
nav entry + these pages are absent when it's off, matching the API which
404s every route — a disabled-feature visit redirects home):

- ``GET /materials`` — the catalog: filter / search materials, a
  "New material" create dialog (class-driven property grid + approver
  picker), and a "Manage classes" dialog (the class + expected-property
  schema admin).
- ``GET /materials/{id}`` — the material detail: card metadata edit
  (optimistic), full version history with each version's property grid,
  an add-version dialog, and retire.

Materials are **global** (no personal/project tiering — a deliberate
contrast with the file library); reads are open to any authenticated
user, while create / edit / retire need ``materials.curate`` (enforced
by the service). Approve / reject happen in the existing Approvals
inbox (the directed-approver gate). UI gating reads the caller's
resolved ``materials.curate`` so the curator controls are shown to the
right people; the service stays the authoritative gate.
"""

from __future__ import annotations

import re
from typing import Any

from nicegui import ui

from matofx.ui._features import materials_database_enabled
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token, has_permission
from matofx.ui.units import UNIT_CUSTOM_VALUE, UNIT_NONE_VALUE, UNIT_OPTIONS, resolve_unit

# Mirrors :func:`matofx.materials.slugify` — kept inline so the UI bundle
# doesn't pull the materials-service module just for one regex.
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _suggest_slug(name: str) -> str:
    """Lowercase + non-alphanumeric → ``_`` + trim; live preview helper."""
    return _SLUG_RE.sub("_", (name or "").strip().lower()).strip("_")[:60]


_MATERIALS_CURATE = "materials.curate"
_MATERIALS_CREATE = "materials.create"

_STATE_COLOR: dict[str, str] = {"active": "positive", "pending": "warning", "retired": "grey"}
_PUBLISH_LABEL: dict[str, str] = {
    "draft": "Draft",
    "pending": "Pending review",
    "published": "Published",
    "rejected": "Rejected",
}
_PUBLISH_COLOR: dict[str, str] = {
    "draft": "grey",
    "pending": "warning",
    "published": "positive",
    "rejected": "negative",
}


def _client() -> ApiClient:
    return ApiClient(get_app(), token=current_token())


def _feature_ok() -> bool:
    """Redirect home when the feature is off (page *absent*, design §12)."""
    if not materials_database_enabled():
        ui.navigate.to("/home")
        return False
    return True


def _state_chip(state: str) -> None:
    if state == "active":
        return  # the common case reads as no chip — only flag exceptions
    ui.chip(state.title()).props(f"color={_STATE_COLOR.get(state, 'grey')} text-color=white dense")


def _publish_chip(publish_state: str) -> None:
    ui.chip(_PUBLISH_LABEL.get(publish_state, publish_state.title())).props(
        f"color={_PUBLISH_COLOR.get(publish_state, 'grey')} text-color=white dense"
    )


def _parse_list(text: str) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


# ===================== catalog (list + create) =======================

_GROUP_BY_OPTS = {"none": "No grouping", "class": "Group by class"}


@ui.page("/materials")
async def materials_page() -> None:
    """The Materials catalog — search / filter + create + class admin."""
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    try:
        me = await client.me()
    except ApiError:
        me = {}
    is_curator = _MATERIALS_CURATE in (me.get("permissions") or [])
    # Adding/versioning materials needs materials.create (or curate —
    # curators can always contribute); governance (classes) stays curate.
    can_create = is_curator or has_permission(_MATERIALS_CREATE)
    try:
        classes = await client.list_material_classes()
    except ApiError:
        classes = []

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-0"):
                ui.label("Materials").style("font-size: 1.5rem; font-weight: 600;")
                ui.label(
                    "Reusable, curator-governed material cards with versioned, "
                    "schema-validated property sets."
                ).style("color: #888;")
            with ui.row().classes("items-center gap-2"):
                if is_curator:
                    ui.button(
                        "Manage classes",
                        icon="category",
                        on_click=lambda: _open_classes_dialog(client, is_curator, _reload),
                    ).props("flat")
                if can_create:
                    ui.button(
                        "New material",
                        icon="add",
                        on_click=lambda: _open_create_dialog(client, is_curator),
                    ).props("color=primary")

        filters: dict[str, Any] = {"class_id": None, "include_retired": False}
        # Enriched materials cached between keystrokes so the search box
        # and the group-by selector filter/regroup in memory (cumulative
        # with the server-side class / retired filters) without refetching.
        cache: dict[str, list[dict[str, Any]]] = {"materials": []}

        # Filter row — search + dropdowns, all cumulative (above results).
        with ui.card().classes("w-full"), ui.row().classes("w-full items-center gap-3 flex-wrap"):
            search = (
                ui.input("Search by name, alias, tag, class or description")
                .props("clearable dense")
                .classes("grow")
            )
            class_opts: dict[Any, str] = {None: "All classes"}
            class_opts.update({int(c["id"]): str(c["name"]) for c in classes})
            class_sel = (
                ui.select(class_opts, value=None, label="Class").props("dense").classes("w-44")
            )
            group_sel = (
                ui.select(_GROUP_BY_OPTS, value="none", label="Group by")
                .props("dense")
                .classes("w-40")
            )
            retired_cb = ui.checkbox("Include retired")

        results = ui.column().classes("w-full gap-2")

        def _render() -> None:
            results.clear()
            term = (search.value or "").strip().lower()
            shown = [e for e in cache["materials"] if not term or term in e["search_blob"]]
            with results:
                if not shown:
                    ui.label(
                        "No materials match your search and filters."
                        if (term or filters["class_id"])
                        else "No materials yet — create one with “New material”."
                    ).style("color: #888;")
                    return
                _render_grouped(shown, str(group_sel.value or "none"))

        async def _reload() -> None:
            results.clear()
            try:
                materials = await client.list_materials(
                    class_id=filters["class_id"],
                    include_retired=bool(filters["include_retired"]),
                )
            except ApiError as exc:
                cache["materials"] = []
                with results:
                    ui.label(f"Couldn't load materials: {exc.detail}").style(
                        "color: var(--matofx-error-text);"
                    )
                return
            cache["materials"] = [_enrich_material(m) for m in materials]
            _render()

        def _apply_filters(_e: Any = None) -> None:
            filters["class_id"] = class_sel.value
            filters["include_retired"] = bool(retired_cb.value)
            ui.timer(0.0, _reload, once=True)

        class_sel.on("update:model-value", _apply_filters)
        retired_cb.on("update:model-value", _apply_filters)
        search.on("update:model-value", lambda _e=None: _render())
        group_sel.on("update:model-value", lambda _e=None: _render())
        await _reload()


def _enrich_material(m: dict[str, Any]) -> dict[str, Any]:
    """Wrap a material with a lowercased search blob (name/alias/tag/class/desc)."""
    parts = [
        str(m.get("name") or ""),
        str(m.get("class_name") or ""),
        str(m.get("description") or ""),
        *[str(a) for a in (m.get("aliases") or [])],
        *[str(t) for t in (m.get("tags") or [])],
    ]
    return {"material": m, "search_blob": " ".join(parts).lower()}


def _render_grouped(entries: list[dict[str, Any]], group_by: str) -> None:
    """Render material cards, optionally bucketed by class."""
    if group_by == "class":
        groups: dict[str, list[dict[str, Any]]] = {}
        for e in entries:
            groups.setdefault(str(e["material"].get("class_name") or "Other"), []).append(e)
        for class_name in sorted(groups):
            _render_group_header(class_name, len(groups[class_name]))
            for e in groups[class_name]:
                _render_material_card(e)
    else:
        for e in entries:
            _render_material_card(e)


def _render_group_header(label: str, count: int) -> None:
    ui.label(f"{label}  ({count})").style(
        "font-weight: 600; color: var(--matofx-muted-text); font-size: 0.85rem; margin-top: 0.4rem;"
    )


def _render_material_card(entry: dict[str, Any]) -> None:
    """One material as a Projects-style card (clickable to its detail page)."""
    m = entry["material"]
    mid = int(m.get("id") or 0)
    name = str(m.get("name") or f"Material #{mid}")
    desc = str(m.get("description") or "").strip()
    version_count = int(m.get("version_count") or 0)
    aliases = list(m.get("aliases") or [])
    tags = list(m.get("tags") or [])
    with (
        ui.card()
        .classes("w-full cursor-pointer hover:bg-gray-50")
        .on("click", lambda _e=None, _id=mid: ui.navigate.to(f"/materials/{_id}"))
    ):
        with ui.column().classes("gap-1 w-full"):
            with ui.row().classes("w-full items-center gap-2"):
                ui.label(name).style("font-size: 1.05rem; font-weight: 600;").classes("flex-grow")
                ui.label(str(m.get("class_name") or "")).style("color: #888; font-size: 0.85rem;")
                if version_count:
                    ui.badge(f"v{version_count}").props("color=grey text-color=white").style(
                        "font-size: 0.7rem;"
                    )
                _state_chip(str(m.get("state") or "active"))
            if aliases or tags:
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    if aliases:
                        ui.label("aka " + ", ".join(aliases[:4])).style(
                            "color: #888; font-size: 0.8rem;"
                        )
                    for tag in tags[:6]:
                        ui.chip(str(tag)).props("dense outline")
            if desc:
                ui.label(desc[:160] + ("…" if len(desc) > 160 else "")).style(
                    "color: #555; font-size: 0.85rem;"
                )


# ----------------------- property-grid helper ------------------------


def _render_property_grid(
    box: ui.column, class_props: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Render a value input per expected class property; return the widget map.

    The returned ``{prop_name: {"value": widget, "unit": str, "required": bool}}``
    is read by the create / add-version submit to build the property payload.
    """
    box.clear()
    widgets: dict[str, dict[str, Any]] = {}
    with box:
        if not class_props:
            ui.label("Pick a class to enter its properties.").style(
                "color: #888; font-size: 0.85rem;"
            )
            return widgets
        ui.label("Properties").style("font-weight: 600;")
        for cp in class_props:
            prop = str(cp.get("prop_name") or "")
            unit = str(cp.get("unit") or "")
            required = bool(cp.get("required"))
            with ui.row().classes("w-full items-center gap-2"):
                star = " *" if required else ""
                num = ui.number(f"{prop}{star}").props("dense").classes("flex-grow")
                ui.label(unit).style("color: #888; min-width: 4rem;")
            widgets[prop] = {"value": num, "unit": unit, "required": required}
    return widgets


def _collect_properties(
    widgets: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Read the property widgets into the payload; return (rows, error)."""
    rows: list[dict[str, Any]] = []
    for prop, w in widgets.items():
        val = w["value"].value
        if val is None or str(val) == "":
            if w["required"]:
                return [], f"'{prop}' is required."
            continue
        rows.append({"prop_name": prop, "value": float(val), "unit": w["unit"]})
    return rows, None


# ---------------------------- create ---------------------------------


def _open_create_dialog(client: ApiClient, is_curator: bool) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 560px; max-width: 92vw;"):
        ui.label("New material").style("font-size: 1.1rem; font-weight: 600;")
        name_in = ui.input("Name *").classes("w-full")
        class_sel = ui.select({}, label="Class *").classes("w-full")
        desc_in = ui.textarea("Description").classes("w-full")
        aliases_in = ui.input("Aliases (comma-separated)").classes("w-full")
        tags_in = ui.input("Tags (comma-separated)").classes("w-full")
        prov_in = ui.input("Provenance (its source) *").classes("w-full")
        prop_box = ui.column().classes("w-full gap-2")
        approver_box = ui.column().classes("w-full gap-2")
        message = ui.label("").style("color: var(--matofx-error-text);")

        ctx: dict[str, Any] = {"classes": [], "prop_widgets": {}, "approver_sel": None}

        async def _load() -> None:
            try:
                classes = await client.list_material_classes()
            except ApiError as exc:
                message.text = f"Couldn't load classes: {exc.detail}"
                classes = []
            ctx["classes"] = classes
            class_sel.options = {int(c["id"]): str(c["name"]) for c in classes}
            class_sel.update()
            await _add_approver_picker(client, approver_box, ctx, is_curator)

        def _on_class(_e: Any = None) -> None:
            chosen = next((c for c in ctx["classes"] if int(c["id"]) == class_sel.value), None)
            props = list(chosen.get("properties") or []) if chosen else []
            ctx["prop_widgets"] = _render_property_grid(prop_box, props)

        class_sel.on("update:model-value", _on_class)

        async def _submit() -> None:
            message.text = ""
            if not (name_in.value or "").strip():
                message.text = "Give the material a name."
                return
            if not class_sel.value:
                message.text = "Pick a class."
                return
            if not (prov_in.value or "").strip():
                message.text = "Add a provenance note (its source)."
                return
            rows, err = _collect_properties(ctx["prop_widgets"])
            if err:
                message.text = err
                return
            body: dict[str, Any] = {
                "name": (name_in.value or "").strip(),
                "class_id": int(class_sel.value),
                "description": (desc_in.value or "").strip(),
                "aliases": _parse_list(aliases_in.value or ""),
                "tags": _parse_list(tags_in.value or ""),
                "provenance": (prov_in.value or "").strip(),
                "properties": rows,
            }
            asel = ctx.get("approver_sel")
            if asel is not None and asel.value:
                body["approver_user_id"] = int(asel.value)
            try:
                created = await client.create_material(body=body)
            except ApiError as exc:
                message.text = exc.detail or "Couldn't create the material."
                return
            dialog.close()
            ui.notify("Material created.", type="positive")
            ui.navigate.to(f"/materials/{int(created['material']['id'])}")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create", icon="save", on_click=_submit).props("color=primary")
    dialog.open()
    ui.timer(0.0, _load, once=True)


async def _add_approver_picker(
    client: ApiClient, box: ui.column, ctx: dict[str, Any], is_curator: bool
) -> None:
    """Reviewer picker — required to publish a change when the gate is on."""
    box.clear()
    ctx["approver_sel"] = None
    try:
        approvers = await client.materials_approvers()
    except ApiError:
        approvers = []
    with box:
        if approvers:
            opts = {
                int(a["user_id"]): f"{a.get('full_name') or a['username']} ({a['username']})"
                for a in approvers
            }
            ctx["approver_sel"] = ui.select(opts, label="Reviewer to approve the change").classes(
                "w-full"
            )
            ui.label(
                "A materials change needs one curator (not you) to review it. "
                "If your install lets curators publish directly, you can leave this blank."
                if is_curator
                else "A materials change needs one curator (not you) to review it."
            ).style("color: #888; font-size: 0.8rem;")
        else:
            ui.label(
                "No other materials curators are available to review a change — "
                "ask an administrator to grant the materials-curator capability."
            ).style("color: #888; font-size: 0.8rem;")


# -------------------------- class admin ------------------------------


def _make_unit_picker(initial_unit: str = "") -> tuple[Any, Any, dict[str, Any]]:
    """Shared unit picker: the UNIT_OPTIONS select + a reveal-on-Custom input.

    Returns ``(select_widget, custom_widget, unit_state)`` where the value
    is read with ``resolve_unit(unit_state['choice'], unit_state['custom'])``.
    """
    if initial_unit and initial_unit in UNIT_OPTIONS:
        choice, custom = initial_unit, ""
    elif initial_unit:
        choice, custom = UNIT_CUSTOM_VALUE, initial_unit
    else:
        choice, custom = UNIT_NONE_VALUE, ""
    unit_state: dict[str, Any] = {"choice": choice, "custom": custom}
    sel = (
        ui.select(UNIT_OPTIONS, label="Unit *", value=choice, with_input=True)
        .props("dense")
        .classes("flex-1 min-w-40")
    )
    custom_in = (
        ui.input(
            "Custom unit",
            value=custom,
            on_change=lambda e: unit_state.__setitem__("custom", e.value or ""),
        )
        .props("dense")
        .classes("flex-1 min-w-32")
    )
    custom_in.set_visibility(choice == UNIT_CUSTOM_VALUE)

    def _on_choice(e: Any) -> None:
        unit_state["choice"] = e.value
        custom_in.set_visibility(e.value == UNIT_CUSTOM_VALUE)

    sel.on_value_change(_on_choice)
    return sel, custom_in, unit_state


def _open_classes_dialog(client: ApiClient, is_curator: bool, on_close: Any = None) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 620px; max-width: 92vw;"):
        ui.label("Material classes").style("font-size: 1.1rem; font-weight: 600;")
        ui.label(
            "A class defines the expected properties (display name, identifier, "
            "unit) a material of that class should carry. Required properties are "
            "enforced when a material is created."
        ).style("color: #888; font-size: 0.85rem;")

        # The create section (top) calls back into the list refresh, which
        # is defined below; a holder lets the section reference it without
        # a forward-declaration ordering problem.
        refresh_holder: dict[str, Any] = {"fn": None}

        # Top: create a class AND add its first properties in one place.
        if is_curator:
            _build_create_section(client, refresh_holder)
            ui.separator()

        list_box = ui.column().classes("w-full gap-2")
        # User-opened class expansions; survives the refresh so a class the
        # user expanded stays open after they add / edit / remove a property.
        open_classes: set[int] = set()

        async def _refresh() -> None:
            list_box.clear()
            try:
                classes = await client.list_material_classes(include_retired=True)
            except ApiError as exc:
                with list_box:
                    ui.label(f"Couldn't load classes: {exc.detail}").style(
                        "color: var(--matofx-error-text);"
                    )
                return
            with list_box:
                if not classes:
                    ui.label("No classes yet.").style("color: #888;")
                for c in classes:
                    _render_class_row(client, c, is_curator, _refresh, open_classes=open_classes)

        refresh_holder["fn"] = _refresh

        def _close() -> None:
            dialog.close()
            if on_close is not None:
                ui.timer(0.0, on_close, once=True)

        ui.separator()
        with ui.row().classes("w-full justify-end"):
            ui.button("Close", on_click=_close).props("flat")
    dialog.open()
    ui.timer(0.0, _refresh, once=True)


def _build_create_section(client: ApiClient, refresh_holder: dict[str, Any]) -> None:
    """Top-of-dialog form: create a class and add its properties in one box.

    Each **Add property** saves the property — creating the class on the
    first one — so the curator never has to leave the box to find the class
    again. They keep adding properties, then **Close** the dialog. A
    separate button creates a class with no properties; **Start another
    class** resets the form for the next one.
    """
    create_ctx: dict[str, Any] = {"class_id": None, "added": []}

    ui.label("Create a class").style("font-weight: 600;")
    with ui.row().classes("w-full items-end gap-2"):
        name_in = ui.input("New class name *").props("dense").classes("flex-1 min-w-40")
        desc_in = ui.input("Description").props("dense").classes("flex-1 min-w-40")
    status = ui.label("").style("color: var(--matofx-accent); font-size: 0.8rem;")

    ui.label("Add a property").style("font-weight: 600; margin-top: 0.2rem;")
    with ui.row().classes("w-full items-end gap-2 flex-wrap"):
        p_name = ui.input("Display name *").props("dense").classes("flex-1 min-w-32")
        slug_state: dict[str, Any] = {"value": "", "user_set": False}
        p_slug = ui.input("Identifier (snake_case)").props("dense").classes("flex-1 min-w-40")
        unit_sel, unit_custom, unit_state = _make_unit_picker()
        p_req = ui.checkbox("Required", value=True)
    ui.label("The identifier is immutable once the property is added.").style(
        "color: #888; font-size: 0.72rem;"
    )

    def _on_name(_e: Any = None) -> None:
        if slug_state["user_set"]:
            return
        suggested = _suggest_slug(p_name.value or "")
        slug_state["value"] = suggested
        p_slug.value = suggested

    def _on_slug(_e: Any = None) -> None:
        slug_state["value"] = p_slug.value or ""
        slug_state["user_set"] = bool(p_slug.value)

    p_name.on_value_change(_on_name)
    p_slug.on_value_change(_on_slug)

    def _clear_property_fields() -> None:
        p_name.value = ""
        p_slug.value = ""
        slug_state["value"] = ""
        slug_state["user_set"] = False
        unit_sel.value = UNIT_NONE_VALUE
        unit_state["choice"] = UNIT_NONE_VALUE
        unit_state["custom"] = ""
        unit_custom.value = ""
        unit_custom.set_visibility(False)
        p_req.value = True

    async def _refresh_list() -> None:
        fn = refresh_holder.get("fn")
        if fn is not None:
            await fn()

    async def _ensure_class() -> int | None:
        """Return the (creating if needed) class id, or None on a problem."""
        if create_ctx["class_id"] is not None:
            return int(create_ctx["class_id"])
        cname = (name_in.value or "").strip()
        if not cname:
            ui.notify("Give the class a name first.", type="warning")
            return None
        try:
            created = await client.create_material_class(
                name=cname, description=(desc_in.value or "").strip()
            )
        except ApiError as exc:
            ui.notify(f"Couldn't create the class: {exc.detail}", type="negative")
            return None
        create_ctx["class_id"] = int(created["id"])
        # Lock the class identity once created; offer a reset instead.
        name_in.props("readonly")
        desc_in.props("readonly")
        empty_btn.set_visibility(False)
        new_class_btn.set_visibility(True)
        return int(create_ctx["class_id"])

    async def _add_property(_e: Any = None) -> None:
        cname = (name_in.value or "").strip()
        clean = (p_name.value or "").strip()
        unit = resolve_unit(unit_state["choice"], unit_state["custom"])
        if not cname:
            ui.notify("Give the class a name first.", type="warning")
            return
        if not clean:
            ui.notify("The property needs a display name.", type="warning")
            return
        if not unit:
            ui.notify("The property needs a unit — pick one or type a custom one.", type="warning")
            return
        cid = await _ensure_class()
        if cid is None:
            return
        try:
            await client.add_material_class_property(
                cid,
                prop_name=clean,
                unit=unit,
                required=bool(p_req.value),
                slug=(p_slug.value or "").strip() or None,
            )
        except ApiError as exc:
            ui.notify(f"Couldn't add the property: {exc.detail}", type="negative")
            return
        create_ctx["added"].append(clean)
        status.text = (
            f"Class “{cname}” saved — properties: {', '.join(create_ctx['added'])}. "
            "Add another, or Close when done."
        )
        _clear_property_fields()
        ui.notify("Property added.", type="positive")
        await _refresh_list()

    async def _create_empty(_e: Any = None) -> None:
        cid = await _ensure_class()
        if cid is None:
            return
        status.text = (
            f"Class “{(name_in.value or '').strip()}” saved (no properties yet). "
            "Add properties, or Close when done."
        )
        ui.notify("Class created.", type="positive")
        await _refresh_list()

    def _reset(_e: Any = None) -> None:
        create_ctx["class_id"] = None
        create_ctx["added"] = []
        name_in.value = ""
        desc_in.value = ""
        name_in.props(remove="readonly")
        desc_in.props(remove="readonly")
        status.text = ""
        _clear_property_fields()
        new_class_btn.set_visibility(False)
        empty_btn.set_visibility(True)

    with ui.row().classes("w-full items-center gap-2"):
        ui.button("Add property", icon="add", on_click=_add_property).props("color=primary")
        empty_btn = ui.button("Create class (no properties)", on_click=_create_empty).props("flat")
        new_class_btn = ui.button("Start another class", icon="refresh", on_click=_reset).props(
            "flat"
        )
        new_class_btn.set_visibility(False)


def _render_class_property_row(
    client: ApiClient,
    class_id: int,
    cp: dict[str, Any],
    is_curator: bool,
    reload: Any,
) -> None:
    """One expected-property row in the class admin — read-only for non-curators."""
    pid = int(cp.get("id") or 0)
    prop_name_str = str(cp.get("prop_name") or "")
    slug_str = str(cp.get("slug") or "")
    req_chip = " (required)" if cp.get("required") else ""
    with ui.row().classes("w-full items-center gap-2"):
        with ui.column().classes("gap-0 flex-grow"):
            ui.label(f"• {prop_name_str} [{cp.get('unit')}]{req_chip}").style("font-size: 0.85rem;")
            if slug_str:
                ui.label(f"  identifier: {slug_str}").style(
                    "color: #888; font-size: 0.72rem; font-family: monospace;"
                )
        if not is_curator:
            return
        ui.button(
            icon="edit",
            on_click=lambda _e=None, _pid=pid, _cp=dict(cp): _open_edit_class_property_dialog(
                client, class_id, _pid, _cp, reload
            ),
        ).props("flat dense").tooltip("Edit name / unit / required")
        ui.button(
            icon="delete_outline",
            on_click=lambda _e=None, _pid=pid, _name=prop_name_str: _confirm_remove_class_property(
                client, class_id, _pid, _name, reload
            ),
        ).props("flat dense color=negative").tooltip("Remove from this class")


def _open_edit_class_property_dialog(
    client: ApiClient,
    class_id: int,
    property_id: int,
    cp: dict[str, Any],
    reload: Any,
) -> None:
    """Edit one class-property row (forward-only — past versions keep their snapshots)."""
    with ui.dialog() as dialog, ui.card().style("min-width: 480px;"):
        ui.label(f"Edit '{cp.get('prop_name')}'").style("font-size: 1.05rem; font-weight: 600;")
        ui.label(
            "Forward-only: existing materials keep their original property names + "
            "units (their versions are immutable). Renames affect new versions only."
        ).style("color: #888; font-size: 0.78rem;")

        # Slug is the immutable identifier — show it but don't let the user edit.
        slug_val = str(cp.get("slug") or "")
        if slug_val:
            ui.input("Identifier (immutable)", value=slug_val).props("dense readonly").classes(
                "w-full"
            )
        p_name = ui.input("Display name", value=str(cp.get("prop_name") or "")).props("dense")
        # Same shared unit picker as the New-property form.
        initial_unit = str(cp.get("unit") or "")
        unit_state: dict[str, Any] = {
            "choice": initial_unit if initial_unit in UNIT_OPTIONS else UNIT_CUSTOM_VALUE,
            "custom": "" if initial_unit in UNIT_OPTIONS else initial_unit,
        }
        p_unit_sel = (
            ui.select(
                UNIT_OPTIONS,
                label="Unit *",
                value=unit_state["choice"],
                with_input=True,
            )
            .props("dense")
            .classes("w-full")
        )
        p_unit_custom = (
            ui.input(
                "Custom unit",
                value=unit_state["custom"],
                on_change=lambda e: unit_state.__setitem__("custom", e.value or ""),
            )
            .props("dense")
            .classes("w-full")
        )
        p_unit_custom.set_visibility(unit_state["choice"] == UNIT_CUSTOM_VALUE)

        def _on_unit_choice(e: Any) -> None:
            unit_state["choice"] = e.value
            p_unit_custom.set_visibility(e.value == UNIT_CUSTOM_VALUE)

        p_unit_sel.on_value_change(_on_unit_choice)
        p_req = ui.checkbox("Required", value=bool(cp.get("required")))
        message = ui.label("").style("color: var(--matofx-error-text);")

        async def _save() -> None:
            new_name = (p_name.value or "").strip()
            new_unit = resolve_unit(unit_state["choice"], unit_state["custom"])
            if not new_name:
                message.text = "Property needs a name."
                return
            if not new_unit:
                message.text = "Property needs a unit — pick one or type a custom one."
                return
            body: dict[str, Any] = {
                "prop_name": new_name,
                "unit": new_unit,
                "required": bool(p_req.value),
            }
            try:
                await client.update_material_class_property(class_id, property_id, body=body)
            except ApiError as exc:
                message.text = exc.detail or "Couldn't save."
                return
            dialog.close()
            ui.notify("Property updated.", type="positive")
            await reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", icon="save", on_click=_save).props("color=primary")
    dialog.open()


def _confirm_remove_class_property(
    client: ApiClient,
    class_id: int,
    property_id: int,
    prop_name: str,
    reload: Any,
) -> None:
    """Confirm + delete one class-property row (no cascade to past versions)."""
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label(f"Remove '{prop_name}'?").style("font-size: 1.05rem; font-weight: 600;")
        ui.label(
            "Forward-only — existing materials keep their stored values for this "
            "property (their versions are immutable). New material versions added "
            "after this won't need it."
        ).style("color: #888; font-size: 0.85rem;")

        async def _confirm() -> None:
            try:
                await client.remove_material_class_property(class_id, property_id)
            except ApiError as exc:
                ui.notify(f"Couldn't remove: {exc.detail}", type="negative")
                return
            dialog.close()
            ui.notify("Property removed.", type="info")
            await reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Remove", icon="delete_outline", on_click=_confirm).props("color=negative")
    dialog.open()


def _render_class_row(
    client: ApiClient,
    c: dict[str, Any],
    is_curator: bool,
    reload: Any,
    *,
    open_classes: set[int] | None = None,
) -> None:
    cid = int(c.get("id") or 0)
    # The dialog rebuilds itself on every Save; the caller passes a set
    # of "user-opened" class ids that survives the rebuild so a class
    # the user expanded stays expanded after they add/edit a property.
    opens: set[int] = open_classes if open_classes is not None else set()
    exp = ui.expansion(
        f"{c.get('name')}  ({len(c.get('properties') or [])} properties)",
        value=cid in opens,
    ).classes("w-full")

    def _track_open(e: Any, _cid: int = cid) -> None:
        if e.value:
            opens.add(_cid)
        else:
            opens.discard(_cid)

    exp.on_value_change(_track_open)
    with exp:
        if c.get("state") == "retired":
            ui.label("Retired — no new materials can be created in this class.").style(
                "color: #888; font-size: 0.8rem;"
            )
        for cp in c.get("properties") or []:
            _render_class_property_row(client, cid, cp, is_curator, reload)
        if not is_curator:
            return
        with ui.row().classes("w-full items-end gap-2 flex-wrap"):
            p_name = ui.input("Display name").props("dense").classes("flex-1 min-w-32")
            # Slug = the immutable snake_case identifier. Auto-fills from
            # the display name; user can override (e.g. to match an
            # established convention) but it's frozen after Add.
            slug_state: dict[str, Any] = {"value": "", "user_set": False}
            p_slug = ui.input("Identifier (snake_case)").props("dense").classes("flex-1 min-w-40")
            ui.label("(immutable once added)").style(
                "color: #888; font-size: 0.7rem; margin-bottom: 0.7rem;"
            )

            def _on_name(_e: Any = None) -> None:
                if slug_state["user_set"]:
                    return
                suggested = _suggest_slug(p_name.value or "")
                slug_state["value"] = suggested
                p_slug.value = suggested

            def _on_slug(_e: Any = None) -> None:
                # User typed in the slug field → stop auto-filling from the name.
                slug_state["value"] = p_slug.value or ""
                slug_state["user_set"] = bool(p_slug.value)

            p_name.on_value_change(_on_name)
            p_slug.on_value_change(_on_slug)
            # Shared unit picker: the same UNIT_OPTIONS catalogue the
            # descriptor wizard uses, with a reveal-on-Custom text input
            # so a user can still type a one-off unit when needed.
            unit_state: dict[str, Any] = {"choice": UNIT_NONE_VALUE, "custom": ""}
            p_unit_sel = (
                ui.select(
                    UNIT_OPTIONS,
                    label="Unit *",
                    value=UNIT_NONE_VALUE,
                    with_input=True,
                )
                .props("dense")
                .classes("flex-1 min-w-40")
            )
            p_unit_custom = (
                ui.input(
                    "Custom unit",
                    on_change=lambda e: unit_state.__setitem__("custom", e.value or ""),
                )
                .props("dense")
                .classes("flex-1 min-w-32")
            )
            p_unit_custom.set_visibility(False)

            def _on_unit_choice(e: Any) -> None:
                unit_state["choice"] = e.value
                p_unit_custom.set_visibility(e.value == UNIT_CUSTOM_VALUE)

            p_unit_sel.on_value_change(_on_unit_choice)
            p_req = ui.checkbox("Required", value=True)

            async def _add_prop(_e: Any = None, _cid: int = cid) -> None:
                clean_name = (p_name.value or "").strip()
                resolved_unit = resolve_unit(unit_state["choice"], unit_state["custom"])
                if not clean_name:
                    ui.notify("Property needs a display name.", type="warning")
                    return
                if not resolved_unit:
                    ui.notify(
                        "Property needs a unit — pick one or type a custom one.",
                        type="warning",
                    )
                    return
                # If the user typed something in the slug field, send it;
                # otherwise leave it None so the server derives it from
                # the display name (the canonical slugify rule).
                explicit_slug = (p_slug.value or "").strip() or None
                try:
                    await client.add_material_class_property(
                        _cid,
                        prop_name=clean_name,
                        unit=resolved_unit,
                        required=bool(p_req.value),
                        slug=explicit_slug,
                    )
                except ApiError as exc:
                    ui.notify(f"Couldn't add property: {exc.detail}", type="negative")
                    return
                ui.notify("Property added.", type="positive")
                await reload()

            ui.button("Add property", icon="add", on_click=_add_prop).props("flat dense")
            if c.get("state") != "retired":

                async def _retire(_e: Any = None, _cid: int = cid) -> None:
                    try:
                        await client.retire_material_class(_cid)
                    except ApiError as exc:
                        ui.notify(f"Couldn't retire: {exc.detail}", type="negative")
                        return
                    ui.notify("Class retired.", type="info")
                    await reload()

                ui.button("Retire class", icon="delete_outline", on_click=_retire).props(
                    "flat dense color=negative"
                )


# ============================ detail =================================


@ui.page("/materials/{material_id}")
async def material_detail_page(material_id: int) -> None:
    """One material — card metadata, version history + property grids, retire."""
    if not require_logged_in() or not _feature_ok():
        return
    render_shell()
    client = _client()
    try:
        me = await client.me()
    except ApiError:
        me = {}
    is_curator = _MATERIALS_CURATE in (me.get("permissions") or [])

    with ui.column().classes("w-full max-w-4xl mx-auto p-6 gap-3"):
        body = ui.column().classes("w-full gap-3")
    await _render_detail(client, int(material_id), body, is_curator)


async def _render_detail(
    client: ApiClient, material_id: int, body: ui.column, is_curator: bool
) -> None:
    body.clear()
    try:
        detail = await client.get_material(material_id)
    except ApiError as exc:
        with body:
            ui.label(f"Couldn't load this material: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
        return

    async def _reload() -> None:
        await _render_detail(client, material_id, body, is_curator)

    m = detail.get("material") or {}
    versions = detail.get("versions") or []
    with body:
        _render_detail_header(client, m, _reload)
        _render_metadata_card(client, m, _reload)
        _render_versions_card(client, material_id, m, versions, is_curator, _reload)
        # Provenance for the latest version (ADR-0010 slice 5). Same
        # latest-version-only convention as the library detail page;
        # per-version selector follows when ADR-0011 wires V&V records
        # in.
        if versions:
            latest_version_id = int(versions[0].get("id") or 0)
            if latest_version_id > 0:
                from matofx.ui.lineage_view import (  # noqa: PLC0415
                    render_provenance_section,
                )

                render_provenance_section(
                    kind="material_version",
                    obj_id=latest_version_id,
                    label=f"Provenance (version {versions[0].get('ordinal') or '?'})",
                )


def _render_detail_header(client: ApiClient, m: dict[str, Any], reload: Any) -> None:
    mid = int(m.get("id") or 0)
    name = str(m.get("name") or f"Material #{mid}")
    state = str(m.get("state") or "active")
    with ui.row().classes("w-full items-center justify-between gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/materials")).props(
                "flat round dense"
            ).tooltip("Back to Materials")
            ui.label(name).style("font-size: 1.4rem; font-weight: 600;")
            ui.label(str(m.get("class_name") or "")).style("color: #888;")
            _state_chip(state)
        if state != "retired":
            ui.button(
                "Retire",
                icon="delete_outline",
                on_click=lambda: _confirm_retire(client, mid, reload),
            ).props("flat dense color=negative")


def _render_metadata_card(client: ApiClient, m: dict[str, Any], reload: Any) -> None:
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Details").style("font-weight: 600;")
            ui.button(
                "Edit", icon="edit", on_click=lambda: _open_metadata_dialog(client, m, reload)
            ).props("flat dense")
        desc = str(m.get("description") or "").strip()
        ui.label(desc or "No description.").style("color: #888;" if not desc else "")
        ui.label(f"Class: {m.get('class_name') or '—'}").style("color: #888; font-size: 0.85rem;")
        with ui.row().classes("items-center gap-4 flex-wrap"):
            aliases = list(m.get("aliases") or [])
            if aliases:
                ui.label(f"Aliases: {', '.join(aliases)}").style("color: #888; font-size: 0.85rem;")
            conf = str(m.get("confidentiality") or "")
            if conf:
                ui.label(f"Confidentiality: {conf}").style("color: #888; font-size: 0.85rem;")
            for tag in list(m.get("tags") or []):
                ui.chip(str(tag)).props("dense outline")
        ui.label(
            f"Created {format_user_datetime(str(m.get('created_at') or ''))} · "
            f"steward #{m.get('steward_user_id')}"
        ).style("color: #888; font-size: 0.8rem;")


def _render_versions_card(
    client: ApiClient,
    material_id: int,
    m: dict[str, Any],
    versions: list[dict[str, Any]],
    is_curator: bool,
    reload: Any,
) -> None:
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Versions").style("font-weight: 600;")
            if str(m.get("state")) != "retired":
                ui.button(
                    "Add version",
                    icon="add",
                    on_click=lambda: _open_add_version_dialog(
                        client, material_id, m, is_curator, reload
                    ),
                ).props("flat dense")
        if not versions:
            ui.label("No versions yet.").style("color: #888;")
            return
        for v in versions:
            _render_version_block(v)


def _render_version_block(v: dict[str, Any]) -> None:
    with (
        ui.column()
        .classes("w-full gap-1 p-2")
        .style("border: 1px solid var(--matofx-card-border); border-radius: 4px;")
    ):
        with ui.row().classes("w-full items-center gap-3"):
            ui.label(f"v{v.get('ordinal')}").style("font-weight: 600;")
            _publish_chip(str(v.get("publish_state") or "draft"))
            ui.space()
            eff = v.get("effective_date")
            if eff:
                ui.label(f"effective {eff}").style("color: #888; font-size: 0.8rem;")
        prov = str(v.get("provenance") or "")
        if prov:
            ui.label(f"Provenance: {prov}").style("color: #888; font-size: 0.8rem;")
        props = list(v.get("properties") or [])
        if not props:
            ui.label("No properties recorded for this version.").style(
                "color: #888; font-size: 0.8rem; font-style: italic;"
            )
        if props:
            ui.table(
                columns=[
                    {
                        "name": "prop_name",
                        "label": "Property",
                        "field": "prop_name",
                        "align": "left",
                    },
                    {"name": "value", "label": "Value", "field": "value"},
                    {"name": "unit", "label": "Unit", "field": "unit"},
                ],
                rows=[
                    {
                        "prop_name": p.get("prop_name"),
                        "value": p.get("value"),
                        "unit": p.get("unit"),
                    }
                    for p in props
                ],
            ).classes("w-full").props("dense flat")


# ----------------------------- dialogs -------------------------------


def _open_metadata_dialog(client: ApiClient, m: dict[str, Any], reload: Any) -> None:
    mid = int(m.get("id") or 0)
    with ui.dialog() as dialog, ui.card().style("min-width: 480px;"):
        ui.label("Edit details").style("font-size: 1.1rem; font-weight: 600;")
        name_in = ui.input("Name *", value=str(m.get("name") or "")).classes("w-full")
        desc_in = ui.textarea("Description", value=str(m.get("description") or "")).classes(
            "w-full"
        )
        aliases_in = ui.input(
            "Aliases (comma-separated)", value=", ".join(list(m.get("aliases") or []))
        ).classes("w-full")
        tags_in = ui.input(
            "Tags (comma-separated)", value=", ".join(list(m.get("tags") or []))
        ).classes("w-full")
        conf_in = ui.input("Confidentiality", value=str(m.get("confidentiality") or "")).classes(
            "w-full"
        )
        message = ui.label("").style("color: var(--matofx-error-text);")

        async def _save() -> None:
            body: dict[str, Any] = {
                "name": (name_in.value or "").strip(),
                "description": desc_in.value or "",
                "aliases": _parse_list(aliases_in.value or ""),
                "tags": _parse_list(tags_in.value or ""),
                "confidentiality": (conf_in.value or "").strip() or None,
                "expected_version": int(m.get("version") or 0),
            }
            try:
                await client.update_material(mid, body=body)
            except ApiError as exc:
                message.text = exc.detail or "Couldn't save."
                return
            dialog.close()
            ui.notify("Saved.", type="positive")
            await reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", icon="save", on_click=_save).props("color=primary")
    dialog.open()


def _open_add_version_dialog(
    client: ApiClient, material_id: int, m: dict[str, Any], is_curator: bool, reload: Any
) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 520px; max-width: 92vw;"):
        ui.label("Add a version").style("font-size: 1.1rem; font-weight: 600;")
        prov_in = ui.input("Provenance (its source) *").classes("w-full")
        ref_in = ui.input("Reference (optional)").classes("w-full")
        notes_in = ui.textarea("What changed (optional)").classes("w-full")
        prop_box = ui.column().classes("w-full gap-2")
        approver_box = ui.column().classes("w-full gap-2")
        message = ui.label("").style("color: var(--matofx-error-text);")
        ctx: dict[str, Any] = {"prop_widgets": {}, "approver_sel": None}

        async def _load() -> None:
            # Build the property grid from the material's class schema.
            props: list[dict[str, Any]] = []
            try:
                classes = await client.list_material_classes()
                chosen = next(
                    (c for c in classes if int(c["id"]) == int(m.get("class_id") or 0)), None
                )
                props = list(chosen.get("properties") or []) if chosen else []
            except ApiError:
                props = []
            ctx["prop_widgets"] = _render_property_grid(prop_box, props)
            await _add_approver_picker(client, approver_box, ctx, is_curator)

        async def _save() -> None:
            message.text = ""
            if not (prov_in.value or "").strip():
                message.text = "Add a provenance note."
                return
            rows, err = _collect_properties(ctx["prop_widgets"])
            if err:
                message.text = err
                return
            body: dict[str, Any] = {
                "provenance": (prov_in.value or "").strip(),
                "reference": (ref_in.value or "").strip() or None,
                "version_notes": (notes_in.value or "").strip() or None,
                "properties": rows,
            }
            asel = ctx.get("approver_sel")
            if asel is not None and asel.value:
                body["approver_user_id"] = int(asel.value)
            try:
                await client.add_material_version(material_id, body=body)
            except ApiError as exc:
                message.text = exc.detail or "Couldn't add the version."
                return
            dialog.close()
            ui.notify("Version added.", type="positive")
            await reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Add version", icon="add", on_click=_save).props("color=primary")
    dialog.open()
    ui.timer(0.0, _load, once=True)


def _confirm_retire(client: ApiClient, material_id: int, reload: Any) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label("Retire this material?").style("font-size: 1.1rem; font-weight: 600;")
        ui.label(
            "It will stop appearing in pickers and can't be edited. Nothing is "
            "deleted — retire is a state change."
        ).style("color: #888;")

        async def _confirm() -> None:
            try:
                await client.retire_material(material_id)
            except ApiError as exc:
                ui.notify(f"Couldn't retire: {exc.detail}", type="negative")
                return
            dialog.close()
            ui.notify("Material retired.", type="info")
            await reload()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Retire", icon="delete_outline", on_click=_confirm).props("color=negative")
    dialog.open()
