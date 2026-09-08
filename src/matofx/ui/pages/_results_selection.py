"""Selection-state + Compare-handoff helpers for the warehouse page.

Per ``feedback_unbounded_selection_capped_actions`` — tick boxes on
the warehouse page are unbounded; per-action caps surface via
disabled-button tooltips. This module is the **pure logic** the page
shell + the api_client tests both consume; the NiceGUI shell wires
the live widgets to it but doesn't own the rules.

Selection rules (ADR-0012 §8.1 / 2026-05-29 design call):

- Unbounded tick-box selection — every row may be ticked.
- "View in Compare" button activates only when 2 ≤ N ≤ 6 (the same
  ``Compare.MAX_SELECTION`` ceiling — past 6 the per-row grid gets
  unreadable). Below 2 the button is disabled; above 6 it stays
  disabled with the cap-tooltip.
- The Compare hand-off encodes the selection into the same query
  shape Compare's own URL uses
  (:func:`matofx.ui.pages.compare.selection.build_query`), so the
  warehouse never has to know Compare's internals beyond the mode +
  the ``jobs=`` key.

The module is dependency-free (no NiceGUI, no DB, no httpx) — same
discipline as ``compare/selection.py`` so unit tests can exercise it
in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlencode

from matofx.ui.pages.compare.selection import (
    MAX_SELECTION as COMPARE_MAX_SELECTION,
)
from matofx.ui.pages.compare.selection import (
    MIN_SELECTION as COMPARE_MIN_SELECTION,
)
from matofx.ui.pages.compare.selection import Selection as CompareSelection
from matofx.ui.pages.compare.selection import build_query as build_compare_query

# Re-exported for callers that want to surface the limits in tooltips
# without having to import them from the Compare module directly.
COMPARE_MIN: int = COMPARE_MIN_SELECTION
COMPARE_MAX: int = COMPARE_MAX_SELECTION


@dataclass(frozen=True)
class CompareAction:
    """The action the "View in Compare" button surfaces to the user.

    The page renders ``label`` on the button; if ``enabled`` is
    ``False`` the button is disabled with ``tooltip`` showing the
    reason. ``href`` is the URL to navigate to on click (only valid
    when ``enabled``).
    """

    enabled: bool
    label: str
    tooltip: str
    href: str | None = None


@dataclass(frozen=True)
class SelectionState:
    """The current warehouse selection — pure state + derived behaviour.

    ``ticked`` is the set of job ids the user has selected; order is
    not significant (the persistent "N selected" pill displays the
    count). Selection survives pagination and filter changes — rows
    that leave the filter stay ticked but visually hidden, and the
    pill count still includes them
    (feedback_unbounded_selection_capped_actions).
    """

    ticked: frozenset[int] = field(default_factory=frozenset)

    @property
    def count(self) -> int:
        return len(self.ticked)

    def tick(self, job_id: int) -> SelectionState:
        return SelectionState(ticked=self.ticked | {job_id})

    def untick(self, job_id: int) -> SelectionState:
        return SelectionState(ticked=self.ticked - {job_id})

    def clear(self) -> SelectionState:
        return SelectionState()

    def compare_action(self) -> CompareAction:
        """Derive the "View in Compare" button's state from the selection.

        Behaviour matrix:

        - ``count == 0`` → button label "View in Compare", disabled,
          tooltip "Tick at least 2 rows to compare".
        - ``count == 1`` → label "View in Compare (1)", disabled,
          tooltip "Tick at least 2 rows to compare".
        - ``2 <= count <= MAX_SELECTION`` → label "View in Compare (N)",
          enabled, ``href`` set to the Compare deep-link.
        - ``count > MAX_SELECTION`` → label "View in Compare (N)",
          disabled, tooltip "Compare is limited to 6 rows — narrow
          your selection".
        """
        n = self.count
        if n == 0:
            return CompareAction(
                enabled=False,
                label="View in Compare",
                tooltip=f"Tick at least {COMPARE_MIN} rows to compare",
            )
        label = f"View in Compare ({n})"
        if n < COMPARE_MIN:
            return CompareAction(
                enabled=False,
                label=label,
                tooltip=f"Tick at least {COMPARE_MIN} rows to compare",
            )
        if n > COMPARE_MAX:
            return CompareAction(
                enabled=False,
                label=label,
                tooltip=(f"Compare is limited to {COMPARE_MAX} rows — narrow your selection"),
            )
        # 2 ≤ n ≤ 6 — surface the deep-link.
        return CompareAction(
            enabled=True,
            label=label,
            tooltip="",
            href=build_compare_href(self.ticked),
        )


def build_compare_href(job_ids: frozenset[int]) -> str:
    """Build the ``/compare?...`` URL for the supplied job-id selection.

    Routes through :func:`matofx.ui.pages.compare.selection.build_query`
    so the warehouse hand-off shares Compare's URL contract — adding
    a new selection mode to Compare propagates to the warehouse
    automatically. Stable id order so the URL doesn't shuffle when
    nothing semantic changed (sorted ascending).
    """
    sel = CompareSelection(mode="tool", ids=tuple(sorted(job_ids)))
    params = build_compare_query(sel)
    return "/compare?" + urlencode(params)


def encode_job_ids_for_export(job_ids: frozenset[int]) -> str:
    """Render the ticked set into the ``job_ids=`` query value for export.

    Empty selection → empty string (matches the API's "explicit empty
    allow-list" semantic, but the warehouse export bar never sends
    that — the "Download selected" button is disabled at ``count == 0``).
    Order is ascending for stable URL fingerprints.
    """
    return ",".join(str(i) for i in sorted(job_ids))
