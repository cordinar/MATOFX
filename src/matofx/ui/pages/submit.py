"""Submit-job page (``/ui/scenarios/{scenario_id}/submit``).

Deep-link alias — redirects into the unified My Jobs workspace at
``/my-jobs``. Per ``feedback_my_jobs_unified_workspace.md`` the
workspace is the single interactive surface for picking a tool and
submitting a job; this URL stays as a stable bookmark / quick-launch
target and forwards into the workspace with ``?scenario=&pick_tool=1``
(or ``?scen=&tool=`` when the caller already picked the tool).

The internal helper functions below stay in place because the
home-page quick-launch buttons and the docs reference this URL — but
they're no longer reached: the page handler navigates straight into
the workspace and returns before they run.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.tools.descriptor import tool_descriptions
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import require_logged_in
from matofx.ui.state import current_token


@ui.page("/scenarios/{scenario_id}/submit")
async def submit_page(scenario_id: int, tool: str | None = None) -> None:
    """Forward into the workspace, seeding scenario + (optionally) tool.

    ``?tool=<alias>`` is preserved when present so the workspace lands
    directly on the form; without it the workspace shows the tool
    picker (no draft job is created until the user clicks a tool, per
    Q2=ii in the rewrite plan).
    """
    if not require_logged_in():
        return
    if tool:
        ui.navigate.to(f"/my-jobs?scen={int(scenario_id)}&tool={tool}")
        return
    ui.navigate.to(f"/my-jobs?scen={int(scenario_id)}&pick_tool=1")


async def _render_tool_picker(scenario_id: int, tools: list[dict[str, Any]], body: Any) -> None:
    """Show one card per tool; clicking a card swaps in the form."""
    body.clear()
    with body:
        for tool in tools:
            alias = str(tool["alias"])
            with (
                ui.card()
                .classes("w-full cursor-pointer hover:bg-gray-50")
                .on(
                    "click",
                    lambda _e=None, _a=alias: _on_tool_picked(scenario_id, _a, body),
                )
            ):
                with ui.row().classes("items-center justify-between w-full"):
                    with ui.column().classes("gap-1"):
                        ui.label(str(tool["name"])).style("font-size: 1.1rem; font-weight: 600;")
                        description = str(tool.get("short_description") or "")
                        if description:
                            ui.label(description).style("color: #555; font-size: 0.9rem;")
                    ui.label(f"v{tool.get('version', '')}").style(
                        "color: #888; font-size: 0.85rem;"
                    )


async def _on_tool_picked(scenario_id: int, alias: str, body: Any) -> None:
    """Fetch the descriptor and swap the picker for the form."""
    client = ApiClient(get_app(), token=current_token())
    try:
        detail = await client.get_tool(alias)
    except ApiError as exc:
        ui.notify(f"Couldn't load the tool: {exc.detail}", type="negative")
        return
    descriptor = detail.get("descriptor") or {}
    body.clear()
    with body:
        await _render_form(scenario_id, descriptor)


async def _render_form(scenario_id: int, descriptor: dict[str, Any]) -> None:
    """Build the input form from the descriptor's ``inputs`` list."""
    alias = str(descriptor.get("alias", ""))
    name_label = str(descriptor.get("name", alias))
    ui.label(f"Tool: {name_label}").style("font-size: 1.05rem; font-weight: 600;")
    # Form header shows the fuller long_description when authored, else
    # falls back to the mandatory short one (the split's run-page rule).
    short_desc, long_desc = tool_descriptions(descriptor)
    blurb = long_desc or short_desc
    if blurb:
        ui.label(blurb).style("color: #555; font-size: 0.9rem; white-space: pre-line;")

    job_name = ui.input("Job name *", value=f"{alias} run").classes("w-full")

    # Walk the inputs and render one field per entry.
    field_widgets: dict[str, Any] = {}
    for field in descriptor.get("inputs", []) or []:
        widget = _render_input_field(field)
        if widget is not None:
            field_widgets[str(field["name"])] = (field, widget)

    message = ui.label("").style("color: #c0392b;")

    async def _submit() -> None:
        if not job_name.value or not job_name.value.strip():
            message.text = "Please enter a name for the job."
            return
        inputs_payload: dict[str, Any] = {}
        for name, (field, widget) in field_widgets.items():
            inputs_payload[name] = _coerce_value(field, widget.value)

        client = ApiClient(get_app(), token=current_token())
        try:
            job = await client.submit_job(
                scenario_id,
                name=job_name.value,
                tool_alias=alias,
                inputs=inputs_payload,
            )
        except ApiError as exc:
            message.text = exc.detail or "Submitting the job failed."
            return
        ui.notify("Job submitted.", type="positive")
        ui.navigate.to(f"/jobs/{job['id']}")

    with ui.row().classes("w-full justify-end gap-2"):
        ui.button(
            "Cancel",
            on_click=lambda: ui.navigate.to(f"/scenarios/{scenario_id}"),
        ).props("flat")
        ui.button("Submit", on_click=_submit).props("color=primary")


def _render_input_field(field: dict[str, Any]) -> Any:
    """Render one form field for a descriptor input. Returns the widget."""
    label = str(field.get("label") or field.get("name") or "")
    if field.get("required"):
        label = f"{label} *"
    unit = field.get("unit")
    if unit:
        label = f"{label} ({unit})"
    help_text = field.get("help_text") or ""
    field_type = str(field.get("type") or "string")
    default = field.get("default")

    # Variable type intentionally widened — NiceGUI's per-widget classes
    # (Number, Select, Input, Checkbox, ...) all expose ``.value``, which
    # is the only attribute we read off it later.
    widget: Any
    if field_type in ("integer", "float"):
        widget = ui.number(
            label,
            value=default if isinstance(default, int | float) else None,
            min=field.get("minimum"),
            max=field.get("maximum"),
            step=field.get("precision"),
        ).classes("w-full")
    elif field_type == "boolean":
        widget = ui.checkbox(label, value=bool(default))
    elif field_type == "select":
        options = list(field.get("options") or [])
        widget = ui.select(
            options,
            label=label,
            value=default if default in options else (options[0] if options else None),
        ).classes("w-full")
    elif field_type == "multiselect":
        options = list(field.get("options") or [])
        widget = ui.select(
            options,
            label=label,
            multiple=True,
            value=default if isinstance(default, list) else [],
        ).classes("w-full")
    elif field_type == "file":
        # Phase 1: accept a path-on-the-shared-mount as a string. Real
        # browser file uploads land in Phase 2 alongside the staging API.
        widget = ui.input(label, value=str(default) if default else "").classes("w-full")
    else:  # string / unknown
        widget = ui.input(label, value=str(default) if default is not None else "").classes(
            "w-full"
        )

    if help_text:
        ui.label(str(help_text)).style("color: #777; font-size: 0.8rem;")
    return widget


def _coerce_value(field: dict[str, Any], value: Any) -> Any:
    """Best-effort coerce a form widget's value to the descriptor's type."""
    field_type = str(field.get("type") or "string")
    if value is None or value == "":
        return None
    if field_type == "boolean":
        return bool(value)
    if field_type in ("integer", "float"):
        caster: Any = int if field_type == "integer" else float
        try:
            return caster(value)
        except (TypeError, ValueError):
            return value
    return value
