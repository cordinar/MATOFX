"""Group a tool's descriptor inputs by their declared input group.

The job-submit form already renders a tool's input groups (see
``tools/forms/render.py``). The parametric-study and optimisation setup
wizards historically flattened those groups away and listed inputs in raw
authoring order. These helpers let the wizards bucket inputs by their
declared group and render the group's ``label`` as a section header — in the
descriptor's column-then-row order — with any ungrouped inputs collected into
a trailing "Other inputs" section.

The grouping logic is a pure function (:func:`group_sections`) so it can be
unit-tested without NiceGUI; the small ``render_*_header`` helpers draw the
matching section chrome and are the only NiceGUI-coupled part.
"""

from __future__ import annotations

from typing import Any

OTHER_GROUP_LABEL = "Other inputs"

# Ordered ``(header, inputs)`` sections. ``header`` is a group's label, or
# ``None`` for the ungrouped bucket (callers render it as ``OTHER_GROUP_LABEL``
# only when at least one real group is also present).
Sections = list[tuple[str | None, list[dict[str, Any]]]]


def group_sections(
    inputs: list[dict[str, Any]],
    input_groups: list[dict[str, Any]] | None,
) -> Sections:
    """Bucket ``inputs`` by their descriptor group, preserving order.

    Groups are ordered by their grid placement (``column`` then ``row``, the
    same order the job form uses); inputs keep their authoring order within a
    group. A group left empty after any caller-side filtering is dropped, and
    inputs whose ``group`` is unset — or names a group the descriptor doesn't
    declare — fall into a trailing ``None`` bucket.

    When the descriptor declares no groups at all, returns a single
    ``[(None, inputs)]`` section, so a caller can tell "no grouping" apart via
    :func:`has_groups` and render a flat list exactly as before.
    """
    groups = [g for g in (input_groups or []) if isinstance(g, dict) and g.get("name")]
    if not groups:
        return [(None, list(inputs))]

    ordered = sorted(groups, key=lambda g: (int(g.get("column") or 1), int(g.get("row") or 1)))
    label_by_name = {str(g["name"]): str(g.get("label") or g["name"]) for g in groups}

    buckets: dict[str | None, list[dict[str, Any]]] = {}
    for inp in inputs:
        gname = inp.get("group")
        key = str(gname) if gname in label_by_name else None
        buckets.setdefault(key, []).append(inp)

    sections: Sections = []
    for group in ordered:
        members = buckets.get(str(group["name"]))
        if members:
            sections.append((label_by_name[str(group["name"])], members))
    if buckets.get(None):
        sections.append((None, buckets[None]))
    return sections


def has_groups(sections: Sections) -> bool:
    """True when ``sections`` carries at least one real (named) group header.

    Callers use this to decide whether to draw section headers at all: a tool
    whose inputs are entirely ungrouped yields a single unnamed section and is
    rendered flat, unchanged from before grouping existed.
    """
    return any(header is not None for header, _ in sections)


def render_group_header(label: str) -> None:
    """Draw a group section header (bold accent label + thin separator)."""
    from nicegui import ui  # noqa: PLC0415

    ui.label(label).classes("w-full").style(
        "font-weight: 600; margin-top: 0.35rem; color: var(--matofx-accent);"
    )
    ui.separator()


def render_member_header(label: str) -> None:
    """Draw a workflow-member (tool step) header — the top level for a chain.

    Slightly stronger than a group header so members read as the outer level
    and each member's own input groups nest beneath.
    """
    from nicegui import ui  # noqa: PLC0415

    with ui.row().classes("w-full items-center gap-2").style("margin-top: 0.5rem;"):
        ui.icon("account_tree").style("color: var(--matofx-accent); font-size: 1.1rem;")
        ui.label(label).style(
            "font-weight: 700; color: var(--matofx-label-text); font-size: 1.02rem;"
        )
    ui.separator()
