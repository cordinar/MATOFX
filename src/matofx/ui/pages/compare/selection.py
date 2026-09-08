"""URL state + selection validation for the Compare workspace.

The Compare page is deep-linkable:

- ``/compare?mode=tool&jobs=1,2,3`` — same-tool job comparison.
- ``/compare?mode=scenario&scenarios=4,5,6`` — scenario comparison.
- ``/compare?mode=workflow&workflows=7,8`` — same-workflow run comparison.

This module turns the raw query-string into a validated
:class:`Selection` value object and produces plain-English error
messages when the URL is unusable. Pure logic — no NiceGUI / network
dependency — so the parser is unit-testable in isolation.

Selection rules (locked 2026-05-09):

- 2 to 6 items, inclusive. Below the floor we tell the user "pick at
  least two…", above the ceiling we tell them "the most you can compare
  is six".
- For ``mode=tool``, every selected job must share a ``tool_alias``.
  The cross-tool detection lives here so the workspace's My-Jobs
  multi-select entry path can short-circuit before navigating.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

CompareMode = Literal["tool", "workflow", "scenario"]
"""The three comparison modes the page understands. ``tool`` and
``workflow`` are fully wired; ``scenario`` parses + has a picker, with
its Compare rendering landing in a follow-up slice."""

VALID_MODES: frozenset[str] = frozenset({"tool", "workflow", "scenario"})
"""Closed vocabulary. Anything outside this set falls back to ``tool``
with a friendly note rather than 500-ing on a stray query parameter."""

MIN_SELECTION = 2
"""Inclusive floor — diff and overlays need at least two things to
diff against. The "Compare N selected" toolbar in My Jobs only appears
once the user has ticked this many."""

MAX_SELECTION = 6
"""Inclusive ceiling. Above this the per-row grid gets unreadable on
laptop screens; the legacy app capped at the same number."""


@dataclass(frozen=True)
class Selection:
    """One parsed, validated comparison request.

    ``ids`` are the resource ids the page should fetch. Their meaning
    depends on :attr:`mode`: for ``mode='tool'`` they're job ids, for
    ``mode='scenario'`` they're scenario ids, etc. The page resolves
    them in mode-specific code paths — keeping the value object dumb
    means we can validate the URL without a DB round-trip.
    """

    mode: CompareMode
    ids: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SelectionError:
    """A user-facing reason the supplied URL state is unusable.

    The page renders :attr:`message` directly, no translation needed.
    Bundled (rather than raised) because a Compare page lands on a
    soft-failure surface — the user sees the error inline and can
    pick a different selection without the URL bouncing.
    """

    message: str


_TOOL_PARAM_KEY = "jobs"
_WORKFLOW_PARAM_KEY = "workflows"
_SCENARIO_PARAM_KEY = "scenarios"
_MODE_PARAM_KEY = "mode"

_PARAM_KEY_FOR_MODE: dict[str, str] = {
    "tool": _TOOL_PARAM_KEY,
    "workflow": _WORKFLOW_PARAM_KEY,
    "scenario": _SCENARIO_PARAM_KEY,
}


def parse_selection(query_params: dict[str, str]) -> Selection | SelectionError:
    """Turn a raw query-string mapping into a :class:`Selection`.

    Returns a :class:`SelectionError` for any input the page can't
    sensibly act on. Empty / missing query parameters resolve to a
    blank ``mode='tool'`` selection (the user lands on the empty Select
    tab) — that's not an error.
    """
    raw_mode = (query_params.get(_MODE_PARAM_KEY) or "").strip().lower()
    if not raw_mode:
        return Selection(mode="tool", ids=())
    if raw_mode not in VALID_MODES:
        return SelectionError(
            message=(
                f"Compare doesn't recognise the mode {raw_mode!r}. "
                "Pick one of: jobs of one tool, or scenarios."
            )
        )
    mode: CompareMode = raw_mode  # type: ignore[assignment]

    ids_param = query_params.get(_PARAM_KEY_FOR_MODE[mode])
    if not ids_param:
        return Selection(mode=mode, ids=())

    parsed_ids: list[int] = []
    for raw in ids_param.split(","):
        chunk = raw.strip()
        if not chunk:
            continue
        try:
            parsed_ids.append(int(chunk))
        except ValueError:
            return SelectionError(
                message=(
                    f"Compare couldn't read the id {chunk!r} from the URL. "
                    "Each id must be a whole number."
                )
            )

    # Preserve order but drop duplicates — the user probably meant the
    # set, and a doubled id would inflate the diff trivially.
    deduped: list[int] = []
    seen: set[int] = set()
    for value in parsed_ids:
        if value not in seen:
            deduped.append(value)
            seen.add(value)

    if len(deduped) < MIN_SELECTION and deduped:
        return SelectionError(
            message=(
                f"Pick at least {MIN_SELECTION} to compare — you need two or more "
                "to see how they differ."
            )
        )
    if len(deduped) > MAX_SELECTION:
        return SelectionError(
            message=(
                f"The most you can compare at once is {MAX_SELECTION}. "
                f"You picked {len(deduped)} — drop a few and try again."
            )
        )
    return Selection(mode=mode, ids=tuple(deduped))


def build_query(selection: Selection) -> dict[str, str]:
    """Render a selection back into the query-string shape the URL uses.

    The Compare page uses this when the user's local picks change so
    the URL can be history-pushed and a deep link copied from the
    address bar reproduces the same view.
    """
    if not selection.ids:
        return {_MODE_PARAM_KEY: selection.mode}
    return {
        _MODE_PARAM_KEY: selection.mode,
        _PARAM_KEY_FOR_MODE[selection.mode]: ",".join(str(i) for i in selection.ids),
    }


def detect_cross_tool(tool_aliases: Iterable[str]) -> bool:
    """``True`` when the supplied jobs reference more than one tool alias.

    The My-Jobs multi-select entry path uses this to refuse a
    cross-tool selection up-front — comparing jobs of different tools
    isn't meaningful (their inputs and outputs don't share schemas),
    so the workspace shows "Comparing across tools isn't supported"
    and offers a Clear-selection link rather than navigating to a
    Compare page that would render an empty diff.
    """
    seen: set[str] = set()
    for alias in tool_aliases:
        if alias:
            seen.add(alias)
        if len(seen) > 1:
            return True
    return False


CROSS_TOOL_MESSAGE = "Comparing across tools isn't supported — pick jobs of one tool to compare."
"""User-facing copy for the My-Jobs entry guard. Centralised so the
phrasing stays consistent between the floating-toolbar surface and the
Compare page itself."""
