"""Shared project-metadata form helpers (ADR-0002).

One implementation used everywhere a project is created or edited so
the metadata inputs render and serialise identically: the dashboard's
*New Project*, the Home Quick-Access *Launch in new project* flows
(tool + workflow), and the project page's *Edit* dialog.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from nicegui import ui

from matofx.ui.api_client import ApiClient, ApiError

_SELECT_TYPES = {"single_select", "multi_select"}


class MetadataInputError(ValueError):
    """A user-facing problem with what was typed into the metadata form.

    Raised by :func:`collect` (e.g. a date in the wrong format) so the
    create/edit project flows can show a plain-English message *before*
    anything is sent to the API — instead of surfacing the raw pydantic
    ``date_from_datetime_parsing`` validation error.
    """


async def load_fields(client: ApiClient) -> list[dict[str, Any]]:
    """Active metadata fields (+ active values). Empty list on error."""
    try:
        return await client.list_metadata_fields()
    except ApiError:
        return []


def current_by_field(meta_rows: list[dict[str, Any]]) -> dict[int, list[Any]]:
    """Group a project's metadata rows into ``{field_id: [values]}``.

    The value is whichever typed column is populated (``value_id`` for
    selects, else number, else date) so it can pre-fill the widgets.
    """
    out: dict[int, list[Any]] = {}
    for row in meta_rows:
        if row.get("value_id") is not None:
            key: Any = row["value_id"]
        elif row.get("value_num") is not None:
            key = row["value_num"]
        elif row.get("value_text") is not None:
            key = row["value_text"]
        else:
            key = row.get("value_date")
        out.setdefault(row["field_id"], []).append(key)
    return out


def render_fields(
    fields: list[dict[str, Any]],
    current: dict[int, list[Any]] | None = None,
) -> dict[int, Any]:
    """Render one input per field; return ``{field_id: widget}``.

    ``current`` (from :func:`current_by_field`) pre-fills the widgets
    when editing an existing project.
    """
    current = current or {}
    widgets: dict[int, Any] = {}
    for f in fields:
        label = f["label"] + (" *" if f["required"] else "")
        ftype = f["field_type"]
        cur = current.get(f["id"], [])
        if ftype in _SELECT_TYPES:
            opts = {v["id"]: v["value"] for v in (f.get("values") or [])}
            multi = ftype == "multi_select"
            widgets[f["id"]] = ui.select(
                opts,
                label=label,
                multiple=multi,
                value=(cur if multi else (cur[0] if cur else None)),
            ).classes("w-full")
        elif ftype == "number":
            nf = f.get("number_format") or "decimal"
            if nf == "pattern":
                mask = f.get("format_mask") or ""
                widget = ui.input(
                    label,
                    value=(str(cur[0]) if cur else ""),
                    placeholder=mask,
                ).classes("w-full")
                if mask:
                    # `mask` is admin-defined; a double-quote would close the
                    # `hint` attribute and inject arbitrary Quasar props. Only
                    # the quote can break out of the double-quoted value, so
                    # neutralise it (masks never legitimately contain one).
                    safe_mask = mask.replace('"', "'")
                    widget.props(f'hint="Format: {safe_mask}"')
                widgets[f["id"]] = widget
            else:
                widgets[f["id"]] = ui.number(
                    label,
                    value=(cur[0] if cur else None),
                    format="%d" if nf == "integer" else None,
                ).classes("w-full")
        else:  # date
            widgets[f["id"]] = ui.input(
                label + " (YYYY-MM-DD)", value=(str(cur[0]) if cur else "")
            ).classes("w-full")
    return widgets


def collect(fields: list[dict[str, Any]], widgets: dict[int, Any]) -> list[dict[str, Any]]:
    """Turn the rendered widgets into the metadata API payload."""
    selections: list[dict[str, Any]] = []
    for f in fields:
        w = widgets.get(f["id"])
        if w is None:
            continue
        val = w.value
        if val in (None, "", []):
            continue
        ftype = f["field_type"]
        if ftype == "single_select":
            selections.append({"field_id": f["id"], "value_id": int(val)})
        elif ftype == "multi_select":
            for v in val:
                selections.append({"field_id": f["id"], "value_id": int(v)})
        elif ftype == "number":
            if (f.get("number_format") or "decimal") == "pattern":
                selections.append({"field_id": f["id"], "value_text": str(val).strip()})
            else:
                selections.append({"field_id": f["id"], "value_num": float(val)})
        else:  # date — validate the format here so the user gets a clear
            # message rather than the API's raw pydantic parsing error.
            raw = str(val).strip()
            try:
                _dt.date.fromisoformat(raw)
            except ValueError as exc:
                raise MetadataInputError(
                    f"The format of the date entered for '{f['label']}' is incorrect. "
                    "Kindly insert a date in the correct format YYYY-MM-DD."
                ) from exc
            selections.append({"field_id": f["id"], "value_date": raw})
    return selections


def has_required(fields: list[dict[str, Any]]) -> bool:
    """True if any active field is required (→ must call set_metadata)."""
    return any(f.get("required") for f in fields)
