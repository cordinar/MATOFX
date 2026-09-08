"""Pure-logic helpers for the descriptor-wizard KPI editor (slice 11).

The wizard's per-output editor card calls these to:

- Decide whether the "Mark as KPI" checkbox is enabled (only on
  ``type == 'number'`` outputs — non-numeric outputs would fail
  the slice-1 ``_kpi_consistency`` validator on save).
- Count declared KPIs across the live ``outputs`` list for the
  "KPIs declared" badge in the Outputs tab header.
- Resolve the autocomplete pool for ``kpi_category`` from the
  catalog of categories already used in the install (passed in by
  the wizard; the API surfaces this in v1.x).

All pure logic — no NiceGUI, no httpx — so the unit tests can
exercise the rules without the wizard's full mount surface.
"""

from __future__ import annotations

from typing import Any


def kpi_eligible(field_type: str | None) -> bool:
    """``True`` when an output of this type can carry ``kpi = true``.

    Only ``"number"`` outputs are eligible — the slice-1
    ``OutputField._kpi_consistency`` validator rejects KPI flags on
    every other type. The wizard surfaces the constraint by
    disabling the checkbox + showing a tooltip.
    """
    return field_type == "number"


def count_declared_kpis(outputs: list[dict[str, Any]]) -> int:
    """Count outputs in the wizard state that are flagged ``kpi = true``."""
    return sum(1 for f in outputs if isinstance(f, dict) and bool(f.get("kpi")))


def clear_kpi_overrides(field: dict[str, Any]) -> None:
    """Drop ``kpi_label`` / ``kpi_unit`` / ``kpi_category`` from a field.

    Called when the user un-ticks "Mark as KPI" on an output — the
    slice-1 ``_kpi_consistency`` validator rejects descriptors where
    the overrides are set without ``kpi = true``, so the wizard
    sweeps them out to keep the next save clean.
    """
    for key in ("kpi_label", "kpi_unit", "kpi_category"):
        field.pop(key, None)


def kpis_declared_badge_text(outputs: list[dict[str, Any]]) -> str:
    """One-line summary for the Outputs tab header.

    ``"0 KPIs declared"`` (with the count) — keeps the UI honest
    even when no KPIs are flagged so the author notices the
    absence.
    """
    n = count_declared_kpis(outputs)
    return f"{n} KPI{'s' if n != 1 else ''} declared"


def category_autocomplete_options(
    existing_categories: list[str], *, current_value: str | None = None
) -> list[str]:
    """Build the ``kpi_category`` autocomplete combo's option list.

    Deduplicates against ``existing_categories`` (the install's
    in-use vocabulary, fetched once by the wizard) and surfaces
    the current field's value at the front so the picker isn't
    blank when the user re-opens the editor.
    """
    seen: set[str] = set()
    out: list[str] = []
    if current_value:
        out.append(current_value)
        seen.add(current_value)
    for c in existing_categories:
        if isinstance(c, str) and c and c not in seen:
            out.append(c)
            seen.add(c)
    return out


def disabled_checkbox_tooltip() -> str:
    """The tooltip the wizard shows over the disabled KPI checkbox."""
    return "KPIs must be scalar-numeric (type='number')"
