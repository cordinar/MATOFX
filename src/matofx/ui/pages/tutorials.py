"""User-facing tutorials catalogue (``/ui/tutorials``).

The home-redesign help selector (``?`` in the header) offers two
destinations: **Help documentation** (``/help``) and **Tutorials**
(here). Until this page existed, tutorials only surfaced piecemeal — the
general ones on the old Home, per-tool/workflow ones on Quick Access
rows. This page gathers every *active* tutorial the caller can see into
one place, grouped by scope (General · per-tool · per-workflow).

Every tutorial opens in a new tab via the authenticated blob-URL
pattern (the file endpoint needs a bearer token, so a plain link would
lose it).
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token


@ui.page("/tutorials")
async def tutorials_page() -> None:
    """Render the tutorials catalogue grouped by scope."""
    if not require_logged_in():
        return
    render_shell(help_topic="tutorials")

    client = ApiClient(get_app(), token=current_token())

    try:
        rows = await client.list_active_tutorials()
    except ApiError as exc:
        with ui.column().classes("w-full mx-auto p-6 gap-3").style("max-width: 1000px;"):
            ui.label("Tutorials").style("font-size: 1.6rem; font-weight: 650;")
            ui.label(f"Couldn't load tutorials: {exc.detail}").style(
                "color: var(--matofx-error-text);"
            )
        return

    general, by_tool, by_workflow = _bucket_tutorials(rows)
    tool_labels, workflow_labels = await _resolve_labels(client)

    with ui.column().classes("w-full mx-auto p-6 gap-4").style("max-width: 1000px;"):
        ui.label("Tutorials").style("font-size: 1.6rem; font-weight: 650;")
        ui.label(
            "Walkthroughs and recordings uploaded by your administrators. "
            "Tool- and workflow-specific tutorials also appear on each item's "
            "Quick Access row."
        ).style("color: var(--matofx-muted-text);")

        if not rows:
            with ui.card().classes("w-full"):
                ui.label(
                    "No tutorials yet — they'll appear here once an administrator uploads one."
                ).style("color: var(--matofx-muted-text);")
            return

        if general:
            _render_group("General", None, general, client)
        for alias in sorted(by_tool, key=lambda a: str(tool_labels.get(a) or a).lower()):
            _render_group(tool_labels.get(alias, alias), "Tool", by_tool[alias], client)
        for alias in sorted(by_workflow, key=lambda a: str(workflow_labels.get(a) or a).lower()):
            _render_group(workflow_labels.get(alias, alias), "Workflow", by_workflow[alias], client)


def _bucket_tutorials(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Split active tutorials into general / per-tool / per-workflow buckets."""
    general: list[dict[str, Any]] = []
    by_tool: dict[str, list[dict[str, Any]]] = {}
    by_workflow: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        tool_alias = row.get("tool_alias")
        workflow_alias = row.get("workflow_alias")
        if tool_alias:
            by_tool.setdefault(str(tool_alias), []).append(row)
        elif workflow_alias:
            by_workflow.setdefault(str(workflow_alias), []).append(row)
        else:
            general.append(row)
    return general, by_tool, by_workflow


async def _resolve_labels(client: ApiClient) -> tuple[dict[str, str], dict[str, str]]:
    """alias→label maps for tools and workflows (labels, never aliases)."""
    try:
        tool_labels = {
            str(t["alias"]): str(t.get("name") or t["alias"]) for t in await client.list_tools()
        }
    except ApiError:
        tool_labels = {}
    try:
        workflow_labels = {
            str(w["alias"]): str(w.get("name") or w["alias"]) for w in await client.list_workflows()
        }
    except ApiError:
        workflow_labels = {}
    return tool_labels, workflow_labels


def _render_group(
    title: str, kind: str | None, tutorials: list[dict[str, Any]], client: ApiClient
) -> None:
    """One scope card: a heading + a row per tutorial with an Open button."""
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-1"):
        with ui.row().classes("items-center no-wrap").style("gap: 0.5rem;"):
            ui.label(title).style("font-size: 1rem; font-weight: 650;")
            if kind:
                ui.badge(kind).props("color=grey outline")
        for tutorial in tutorials:
            _render_tutorial_row(tutorial, client)


def _render_tutorial_row(tutorial: dict[str, Any], client: ApiClient) -> None:
    """A single tutorial entry: media icon + title + Open."""
    media_type = str(tutorial.get("media_type") or "")
    icon = "picture_as_pdf" if media_type == "document" else "play_circle"
    title = str(tutorial.get("title") or tutorial.get("original_filename") or "Tutorial")
    tutorial_id = int(tutorial["id"])
    with (
        ui.row()
        .classes("items-center w-full no-wrap")
        .style(
            "gap: 0.6rem; padding: 0.4rem 0.2rem; "
            "border-bottom: 1px solid var(--matofx-card-border);"
        )
    ):
        ui.icon(icon).style("color: var(--matofx-accent);")
        ui.label(title).style("flex: 1 1 0; min-width: 0;")
        ui.button(
            "Open",
            icon="open_in_new",
            on_click=lambda _e=None, _id=tutorial_id: _open_tutorial(client, _id),
        ).props("flat dense color=primary size=sm")


async def _open_tutorial(client: ApiClient, tutorial_id: int) -> None:
    """Stream the tutorial bytes (bearer-authenticated) and open in a new tab.

    The file endpoint requires a token, so a plain ``<a target=_blank>``
    would lose it — fetch the bytes through the API client and pop them
    via a blob URL, the same pattern the in-app tutorial buttons use.
    """
    import base64  # noqa: PLC0415
    import json  # noqa: PLC0415

    try:
        async with await client._client() as http:  # noqa: SLF001
            response = await http.get(
                f"/api/tutorials/{tutorial_id}/file",
                headers=client._headers(),  # noqa: SLF001
            )
            response.raise_for_status()
            data = response.content
            mime = response.headers.get("content-type", "application/octet-stream")
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Couldn't open the tutorial: {exc}", type="negative")
        return

    b64 = base64.b64encode(data).decode("ascii")
    ui.run_javascript(
        "(function() {"
        f"  const b64 = {json.dumps(b64)};"
        f"  const mime = {json.dumps(mime)};"
        "  const bin = atob(b64);"
        "  const bytes = new Uint8Array(bin.length);"
        "  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);"
        "  const blob = new Blob([bytes], {type: mime});"
        "  window.open(URL.createObjectURL(blob), '_blank');"
        "})();"
    )
