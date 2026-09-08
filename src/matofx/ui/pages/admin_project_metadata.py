"""Admin: Project Metadata taxonomy (``/ui/admin/project-metadata``).

Define the install-wide project metadata fields and their allowed
values (ADR-0002). Single/multi-select fields carry a value list;
date/number fields don't. Nothing is deleted — retiring a field or
value flips it inactive (kept on projects already using it, never
offered for new selections).

Permissions: requires :attr:`Permission.PROJECTS_MANAGE_METADATA`
(seeded on ``administrator`` + ``super_user``; migration 0036).
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token

# Type identifiers map to the friendly labels shown in the picker and on
# the field cards — the operator never sees the raw ``single_select``.
_FIELD_TYPE_LABELS = {
    "single_select": "Single select",
    "multi_select": "Multi-select",
    "date": "Date",
    "number": "Number",
}
_SELECT_TYPES = {"single_select", "multi_select"}
_NUMBER_FORMATS = {
    "integer": "Integer (whole number)",
    "decimal": "Decimal",
    "pattern": "Custom format (mask)",
}
_MASK_HINT = "0 = digit, A = letter; other characters are literal (e.g. AA-000-0000)"
#: Display-order choices — a 1-20 dropdown rather than a free number box.
_ORDER_CHOICES = list(range(1, 21))


def _order_value(raw: Any) -> int:
    """Clamp a stored display order onto the 1-20 dropdown range."""
    try:
        return min(max(int(raw), 1), 20)
    except (TypeError, ValueError):
        return 1


def _field_type_label(field_type: str) -> str:
    """Friendly label for a field type (falls back to the raw id)."""
    return _FIELD_TYPE_LABELS.get(field_type, field_type)


def _number_format_summary(field: dict[str, Any]) -> str:
    """Human label for a number field's format, for the card subtitle."""
    nf = field.get("number_format") or "decimal"
    if nf == "pattern" and field.get("format_mask"):
        return str(field["format_mask"])
    return nf


@ui.page("/admin/project-metadata")
async def admin_project_metadata_page() -> None:
    """Render the metadata taxonomy management surface."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
        ui.label("Project metadata").style("font-size: 1.5rem; font-weight: 600;")
        ui.label(
            "Fields here appear on every project's create/edit form. "
            "Retiring a field or value keeps it on projects that already "
            "use it but stops offering it for new selections — nothing is "
            "ever deleted."
        ).style("color: #555;")

        body = ui.column().classes("w-full gap-4")

        async def _refresh() -> None:
            body.clear()
            try:
                fields = await client.admin_list_metadata_fields()
            except ApiError as exc:
                with body:
                    ui.label(f"Couldn't load the taxonomy: {exc.detail}").style("color: #c0392b;")
                return
            with body:
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label(f"{len(fields)} field(s)").style("font-weight: 600;")
                    ui.button(
                        "New field",
                        on_click=lambda: _new_field_dialog(client, _refresh),
                    ).props("color=primary")
                if not fields:
                    ui.label("No metadata fields yet. Click 'New field' to add one.").style(
                        "color: #555;"
                    )
                for field in fields:
                    _render_field_card(client, field, _refresh)

        await _refresh()


def _render_field_card(client: ApiClient, field: dict[str, Any], refresh: Any) -> None:
    active = bool(field["active"])
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between w-full"):
            with ui.column().classes("gap-0"):
                title = f"{field['label']}  ·  {_field_type_label(field['field_type'])}"
                if field["field_type"] == "number":
                    title += f"  ({_number_format_summary(field)})"
                ui.label(title).style(
                    "font-size: 1.1rem; font-weight: 600;"
                    + ("" if active else " color: #999; text-decoration: line-through;")
                )
                # Capitalised, evenly-dotted summary line.
                tags = [f"Key: {field['key']}"]
                if field["required"]:
                    tags.append("Required")
                if field["show_in_table"]:
                    tags.append("Shown on Projects list")
                if not active:
                    tags.append("RETIRED")
                ui.label("  ·  ".join(tags)).style("color: #777; font-size: 0.85rem;")
            with ui.row().classes("gap-2"):
                ui.button(
                    "Edit",
                    on_click=lambda f=field: _edit_field_dialog(client, f, refresh),
                ).props("flat")
                ui.button(
                    "Reactivate" if not active else "Retire",
                    on_click=lambda f=field, a=active: _toggle_field(
                        client, f["id"], not a, refresh
                    ),
                ).props("flat color=" + ("primary" if not active else "negative"))

        if field["field_type"] in _SELECT_TYPES:
            ui.separator()
            values = field.get("values") or []
            value_count = len(values)
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("Allowed values").style("font-weight: 600;")
                # A retired field's values are governed by the field's
                # state — adding to it makes no sense, so hide the button.
                if active:
                    ui.button(
                        "Add value",
                        on_click=lambda f=field, n=value_count: _new_value_dialog(
                            client, f["id"], n, refresh
                        ),
                    ).props("flat dense color=primary")
            if not values:
                ui.label("No values yet.").style("color: #999; font-size: 0.85rem;")
            elif active:
                ui.label("Drag rows to reorder.").style("color: #999; font-size: 0.8rem;")
                _render_sortable_values(client, int(field["id"]), values, refresh)
            else:
                # Field retired: show every value struck through so nobody
                # mistakes them for live choices, and don't offer per-value
                # actions (reactivate the field first, then values one by
                # one). Each value keeps its own stored active state.
                ui.label(
                    "This field is retired — its values are hidden from new projects. "
                    "Reactivate the field to manage values again."
                ).style("color: #999; font-size: 0.8rem;")
                _render_retired_values(values)


def _render_sortable_values(
    client: ApiClient,
    field_id: int,
    values: list[dict[str, Any]],
    refresh: Any,
) -> None:
    """Render a field's values as draggable rows that persist new order.

    Native HTML5 drag-and-drop (no JS dependency). ``dragstart`` records
    the dragged value id; dropping on another row computes the new order
    (insert after when dragging down, before when dragging up so both
    ends are reachable), persists it, and refreshes.
    """
    order = [int(v["id"]) for v in values]
    dragged: dict[str, int | None] = {"id": None}

    async def _drop_on(target_id: int) -> None:
        src = dragged["id"]
        dragged["id"] = None
        if src is None or src == target_id:
            return
        src_index, target_index = order.index(src), order.index(target_id)
        without = [vid for vid in order if vid != src]
        insert_at = without.index(target_id) + (1 if src_index < target_index else 0)
        without.insert(insert_at, src)
        try:
            await client.reorder_metadata_values(field_id, value_ids=without)
        except ApiError as exc:
            ui.notify(exc.detail, type="negative")
        await refresh()

    def _make_dragstart(value_id: int) -> Any:
        def _handler() -> None:
            dragged["id"] = value_id

        return _handler

    def _make_drop(value_id: int) -> Any:
        async def _handler() -> None:
            await _drop_on(value_id)

        return _handler

    for value in values:
        v_active = bool(value["active"])
        row = (
            ui.row()
            .classes("items-center justify-between w-full")
            .style("border: 1px solid #eee; border-radius: 4px; padding: 2px 8px; cursor: move;")
        )
        row.props("draggable=true")
        row.on("dragstart", _make_dragstart(int(value["id"])))
        row.on("dragover.prevent", lambda: None)
        row.on("drop", _make_drop(int(value["id"])))
        with row:
            with ui.row().classes("items-center gap-2"):
                ui.icon("drag_indicator").style("color: #bbb;")
                ui.label(value["value"] + ("" if v_active else "  (retired)")).style(
                    "color: #999; text-decoration: line-through;" if not v_active else ""
                )
            ui.button(
                "Reactivate" if not v_active else "Retire",
                on_click=lambda vid=value["id"], a=v_active: _toggle_value(
                    client, vid, not a, refresh
                ),
            ).props("flat dense color=" + ("primary" if not v_active else "negative"))


def _render_retired_values(values: list[dict[str, Any]]) -> None:
    """Static, struck-through value list shown when the field is retired.

    No drag handles and no per-value buttons: while the field is retired
    its values are all hidden from new projects, so showing live actions
    would be misleading. Individual values keep their own stored active
    state for when the field is reactivated.
    """
    for value in values:
        with (
            ui.row()
            .classes("items-center w-full")
            .style("border: 1px solid #eee; border-radius: 4px; padding: 2px 8px;")
        ):
            ui.label(str(value["value"])).style("color: #999; text-decoration: line-through;")


def _caption(text: str) -> None:
    """A small grey explanatory line under a form control."""
    ui.label(text).style("color: #777; font-size: 0.8rem; margin-top: -6px;")


def _new_field_dialog(client: ApiClient, refresh: Any) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 480px; max-width: 560px;"):
        ui.label("New metadata field").style("font-size: 1.2rem; font-weight: 600;")
        ui.label(
            "The Key and Type are fixed once the field is created — projects "
            "store their values against them, so they can't be changed later. "
            "For example, a field with key 'region' and type Single select "
            "stays that way; to change them, retire this field and create a new "
            "one. Everything else (label, values, order…) stays editable."
        ).style("color: #777; font-size: 0.82rem;")

        key = ui.input("Key (stable, lowercase, no spaces) *").classes("w-full")
        _caption("The internal identifier — lowercase, no spaces (e.g. 'region'). Immutable.")
        label = ui.input("Label *").classes("w-full")
        _caption(
            "The human-friendly name shown on every project's form and, if you "
            "tick the box below, beside the value on the Projects list (e.g. 'Region')."
        )
        field_type = ui.select(_FIELD_TYPE_LABELS, value="single_select", label="Type").classes(
            "w-full"
        )
        number_format = ui.select(_NUMBER_FORMATS, value="decimal", label="Number format").classes(
            "w-full"
        )
        mask = ui.input("Format mask", placeholder="AA-000-0000").classes("w-full")
        mask.props(f'hint="{_MASK_HINT}"')

        # --- Inline allowed-values editor (select types only) ------------
        values_block = ui.column().classes("w-full gap-1")
        rows: list[tuple[Any, Any]] = []
        with values_block:
            ui.label("Allowed values").style("font-weight: 600;")
            values_hint = ui.label("").style("color: #777; font-size: 0.8rem;")
            rows_box = ui.column().classes("w-full gap-1")

            def _add_row(_e: Any = None) -> None:
                with rows_box:
                    row = ui.row().classes("items-center w-full no-wrap gap-2")
                    with row:
                        inp = ui.input("Value").classes("grow")
                        ui.button(
                            icon="delete",
                            on_click=lambda r=row: _remove_row(r),
                        ).props("flat dense color=negative")
                rows.append((row, inp))

            def _remove_row(row: Any) -> None:
                for idx, (r, _inp) in enumerate(rows):
                    if r is row:
                        r.delete()
                        rows.pop(idx)
                        break

            ui.button("Add another value", icon="add", on_click=_add_row).props(
                "flat dense color=primary"
            )

        def _sync_format() -> None:
            ftype = str(field_type.value)
            is_number = ftype == "number"
            is_select = ftype in _SELECT_TYPES
            number_format.set_visibility(is_number)
            mask.set_visibility(is_number and number_format.value == "pattern")
            # Date/number fields take no values — only selects do.
            values_block.set_visibility(is_select)
            values_hint.text = (
                "Add at least two values for a multi-select."
                if ftype == "multi_select"
                else "Add at least one value for a single-select."
            )

        field_type.on_value_change(_sync_format)
        number_format.on_value_change(_sync_format)
        # Seed two empty rows so a select field is ready to fill in.
        _add_row()
        _add_row()
        _sync_format()

        required = ui.checkbox("Required on every project")
        show_in_table = ui.checkbox("Show this field's value on the Projects list")
        _caption(
            "When ticked, the value appears under each project on the Projects list "
            "(Dashboard → Projects), so you can see it without opening the project."
        )
        order = ui.select(_ORDER_CHOICES, value=1, label="Display order").classes("w-full")
        _caption("Where this field sits on the project form (1 = first).")
        message = ui.label("").style("color: #c0392b;")

        async def _create() -> None:
            if not (key.value or "").strip() or not (label.value or "").strip():
                message.text = "Key and label are required."
                return
            ftype = str(field_type.value)
            is_number = ftype == "number"
            is_select = ftype in _SELECT_TYPES
            nf = str(number_format.value) if is_number else None
            mk = str(mask.value).strip() if (is_number and nf == "pattern") else None
            if is_number and nf == "pattern" and not mk:
                message.text = "Enter a format mask (e.g. AA-000-0000)."
                return

            # Selects must carry their values up front (single ≥ 1, multi ≥ 2),
            # so the field is usable the moment it's saved.
            values: list[str] = []
            if is_select:
                values = [str(i.value).strip() for _r, i in rows if str(i.value or "").strip()]
                if len(values) != len({v.casefold() for v in values}):
                    message.text = "Each value must be unique."
                    return
                minimum = 2 if ftype == "multi_select" else 1
                if len(values) < minimum:
                    message.text = (
                        "Add at least two values for a multi-select."
                        if minimum == 2
                        else "Add at least one value for a single-select."
                    )
                    return

            try:
                created = await client.create_metadata_field(
                    key=str(key.value).strip(),
                    label=str(label.value).strip(),
                    field_type=ftype,
                    required=bool(required.value),
                    display_order=int(order.value or 1),
                    show_in_table=bool(show_in_table.value),
                    number_format=nf,
                    format_mask=mk,
                )
                for idx, value in enumerate(values):
                    await client.create_metadata_value(
                        int(created["id"]), value=value, display_order=idx
                    )
            except ApiError as exc:
                message.text = exc.detail
                await refresh()
                return
            dialog.close()
            ui.notify("Field created.", type="positive")
            await refresh()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create", on_click=_create).props("color=primary")
    dialog.open()


def _edit_field_dialog(client: ApiClient, field: dict[str, Any], refresh: Any) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 460px;"):
        ui.label(f"Edit '{field['label']}'").style("font-size: 1.2rem; font-weight: 600;")
        ui.label(
            f"Key: {field['key']}  ·  Type: {_field_type_label(field['field_type'])} "
            "(both immutable — projects depend on them)"
        ).style("color: #777; font-size: 0.85rem;")
        label = ui.input("Label *", value=field["label"]).classes("w-full")
        is_number = field["field_type"] == "number"
        number_format = ui.select(
            _NUMBER_FORMATS,
            value=(field.get("number_format") or "decimal"),
            label="Number format",
        ).classes("w-full")
        number_format.set_visibility(is_number)
        mask = ui.input(
            "Format mask", value=field.get("format_mask") or "", placeholder="AA-000-0000"
        ).classes("w-full")
        mask.props(f'hint="{_MASK_HINT}"')

        def _sync_mask() -> None:
            mask.set_visibility(is_number and number_format.value == "pattern")

        number_format.on_value_change(_sync_mask)
        _sync_mask()

        required = ui.checkbox("Required", value=bool(field["required"]))
        show_in_table = ui.checkbox(
            "Show this field's value on the Projects list", value=bool(field["show_in_table"])
        )
        _caption(
            "When ticked, the value appears under each project on the Projects list "
            "(Dashboard → Projects), so you can see it without opening the project."
        )
        order = ui.select(
            _ORDER_CHOICES, value=_order_value(field["display_order"]), label="Display order"
        ).classes("w-full")
        message = ui.label("").style("color: #c0392b;")

        async def _save() -> None:
            changes: dict[str, Any] = {
                "label": str(label.value).strip(),
                "required": bool(required.value),
                "show_in_table": bool(show_in_table.value),
                "display_order": int(order.value or 0),
            }
            if is_number:
                nf = str(number_format.value)
                mk = str(mask.value).strip() if nf == "pattern" else None
                if nf == "pattern" and not mk:
                    message.text = "Enter a format mask (e.g. AA-000-0000)."
                    return
                changes["number_format"] = nf
                changes["format_mask"] = mk
            try:
                await client.update_metadata_field(field["id"], **changes)
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Field updated.", type="positive")
            await refresh()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=_save).props("color=primary")
    dialog.open()


def _new_value_dialog(client: ApiClient, field_id: int, next_order: int, refresh: Any) -> None:
    with ui.dialog() as dialog, ui.card().style("min-width: 420px;"):
        ui.label("Add allowed value").style("font-size: 1.2rem; font-weight: 600;")
        value = ui.input("Value *").props("autofocus").classes("w-full")
        _caption("The new value is added to the end of the list — drag to reorder afterwards.")
        message = ui.label("").style("color: #c0392b;")

        async def _create() -> None:
            if not (value.value or "").strip():
                message.text = "Enter a value."
                return
            try:
                await client.create_metadata_value(
                    field_id,
                    value=str(value.value).strip(),
                    display_order=next_order,
                )
            except ApiError as exc:
                message.text = exc.detail
                return
            dialog.close()
            ui.notify("Value added.", type="positive")
            await refresh()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Add", on_click=_create).props("color=primary")
    dialog.open()


async def _toggle_field(client: ApiClient, field_id: int, active: bool, refresh: Any) -> None:
    try:
        await client.update_metadata_field(field_id, active=active)
    except ApiError as exc:
        ui.notify(exc.detail, type="negative")
        return
    ui.notify("Field reactivated." if active else "Field retired.", type="positive")
    await refresh()


async def _toggle_value(client: ApiClient, value_id: int, active: bool, refresh: Any) -> None:
    try:
        await client.update_metadata_value(value_id, active=active)
    except ApiError as exc:
        ui.notify(exc.detail, type="negative")
        return
    ui.notify("Value reactivated." if active else "Value retired.", type="positive")
    await refresh()
