"""Section headings in a chart's axis pickers.

A plot picker that lists a tool's inputs and its results in one flat run gives
no clue where one ends and the other begins — reported on the parametric study
Explore plots, and true of the optimisation design-space explorer for the same
reason (objectives and factors, one undivided list).

Quasar *can* grey out an individual option, but only through a per-option
``disable`` flag, and NiceGUI has no public way to set one: ``ChoiceElement``
rebuilds ``_props['options']`` from the labels alone and normalises away
anything written into it — verified both nested and wholesale, the key is gone
on the next read either way. Rather than ship a private reach-in that a NiceGUI
upgrade would break silently, a heading is an ordinary option that **snaps
back**: :func:`guard_section_headers` restores the previous choice and
suppresses the redraw, so picking one is a no-op rather than an empty chart.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from nicegui import ui

SECTION_HEADER_PREFIX = "__section:"
"""Marks a picker entry that is a heading, not a variable."""

_SECTION_RULE = "─" * 12
"""The divider drawn after a heading's name, so it reads as a break."""


def is_section_header(value: Any) -> bool:
    """Is this picker value a heading rather than something plottable?"""
    return str(value or "").startswith(SECTION_HEADER_PREFIX)


def sectioned_options(*groups: tuple[str, Mapping[Any, str]]) -> dict[Any, str]:
    """Concatenate ``(title, options)`` groups, each under its own heading.

    A group with nothing in it contributes no heading: a bare "OUTPUTS" with
    nothing under it says the opposite of what it means. Heading keys are
    numbered rather than derived from the title, so two groups can never
    collide on a shared word.
    """
    out: dict[Any, str] = {}
    for index, (title, options) in enumerate(groups):
        if not options:
            continue
        out[f"{SECTION_HEADER_PREFIX}{index}"] = f"{title} {_SECTION_RULE}"
        out.update(options)
    return out


def guard_section_headers(
    select: ui.select, on_change: Callable[[], None]
) -> Callable[[Any], None]:
    """Wire a picker so its headings behave as labels, not choices.

    Returns the handler so the rule can be exercised directly; firing a Quasar
    event from a test would exercise NiceGUI's plumbing rather than this
    decision.
    """
    state: dict[str, Any] = {"last": select.value, "restoring": False}

    def _changed(_event: Any = None) -> None:
        if state["restoring"]:
            state["restoring"] = False
            return
        if is_section_header(select.value):
            state["restoring"] = True
            select.value = state["last"]
            return
        state["last"] = select.value
        on_change()

    select.on("update:model-value", _changed)
    return _changed
