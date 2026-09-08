"""In-form training-data coverage gauge for surrogate tools (ADR-0008 §7.3).

A surrogate tells the user *where it's confident*. The gauge classifies
each numeric input value against that factor's training range — using the
same :func:`matofx.models.classify_coverage` the design specifies — and
shows a per-factor chip plus a worst-of summary. It is **advisory**:
submission isn't blocked (the optional ``block_extrapolation`` descriptor
toggle handles hard rejection by tightening the input bounds, so the
ordinary form validation enforces it — no path here).

The component subscribes to the form's numeric widgets, so the chips
re-classify live as the user types. It's mounted by the My Jobs
workspace only when the tool's descriptor carries a
``surrogate_model_version_id`` (i.e. it's a synthesised surrogate tool)
and the caller can read the backing model version's factor ranges.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nicegui import ui

from matofx.models import CoverageClass, classify_coverage, worst_coverage

# Per §7.3: in_range (green) · near_edge (yellow) · extrapolation (orange) ·
# far_extrapolation (red). Quasar colour tokens + a short human label.
_CLASS_COLOR: dict[CoverageClass, str] = {
    "in_range": "green",
    "near_edge": "amber",
    "extrapolation": "orange",
    "far_extrapolation": "red",
}
_CLASS_LABEL: dict[CoverageClass, str] = {
    "in_range": "in range",
    "near_edge": "near edge",
    "extrapolation": "extrapolating",
    "far_extrapolation": "far extrapolating",
}


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def render_coverage_gauge(
    factors: list[dict[str, Any]],
    *,
    values: dict[str, Any],
    widgets: dict[str, Any],
    labels: Mapping[str, str] | None = None,
) -> None:
    """Mount the gauge and wire it to ``widgets`` for live re-classification.

    ``factors`` is ``[{name, min, max, unit?}]`` from the model version;
    ``values`` is the form handle's live state dict; ``widgets`` maps each
    input name to its NiceGUI widget (the gauge subscribes to the numeric
    ones). Factors with no numeric training range are skipped.

    ``labels`` maps input name to the descriptor's label
    ([[feedback_use_labels_not_names]]). A model version's ``factors`` record
    only ``{name, min, max}`` — the label is a descriptor fact — so the caller
    supplies it from the tool being submitted. Without it these chips read
    ``fan_duty: far extrapolating`` on a form whose own field is labelled
    "Fan speed", leaving the reader to match them up.
    """
    labels = labels or {}
    ranged = [
        f
        for f in factors
        if _as_float(f.get("min")) is not None and _as_float(f.get("max")) is not None
    ]
    if not ranged:
        return

    with ui.card().classes("w-full").style("background-color: var(--matofx-row-bg);"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.icon("insights").style("color: #888;")
            ui.label("Training-data coverage").style("font-weight: 600;")
            summary_chip = ui.chip("").props("text-color=white").classes("text-xs")
        ui.label(
            "Advisory — the surrogate is most reliable inside its training range. "
            "You can still submit outside it."
        ).style("color: #888; font-size: 0.8rem;")
        chips: dict[str, Any] = {}
        with ui.row().classes("w-full gap-2 flex-wrap"):
            for factor in ranged:
                chips[str(factor["name"])] = (
                    ui.chip("").props("text-color=white").classes("text-xs")
                )

    def _refresh() -> None:
        classes: list[CoverageClass] = []
        for factor in ranged:
            name = str(factor["name"])
            lo = _as_float(factor.get("min"))
            hi = _as_float(factor.get("max"))
            current = _as_float(values.get(name))
            chip = chips[name]
            shown = labels.get(name) or name
            if current is None or lo is None or hi is None:
                chip.text = f"{shown}: —"
                chip.props("color=grey")
                continue
            cls = classify_coverage(current, lo, hi)
            classes.append(cls)
            chip.text = f"{shown}: {_CLASS_LABEL[cls]}"
            chip.props(f"color={_CLASS_COLOR[cls]}")
        worst = worst_coverage(classes)
        summary_chip.text = _CLASS_LABEL[worst] if classes else "no numeric inputs"
        summary_chip.props(f"color={_CLASS_COLOR[worst]}")

    # Live re-classify as the user edits each ranged factor's widget.
    for factor in ranged:
        widget = widgets.get(str(factor["name"]))
        if widget is not None and hasattr(widget, "on_value_change"):
            widget.on_value_change(lambda _e=None: _refresh())
    _refresh()
