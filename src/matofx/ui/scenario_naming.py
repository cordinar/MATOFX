"""Suggest the next sequential default name for a new scenario.

When a user adds a scenario to an existing project, the launch dialogs
pre-fill a name so they can usually just click through. We look at the
scenarios already in the project and, when they follow the default
``Scenario <n>`` convention, suggest the next number — so a project that
already holds ``Scenario 1``..``Scenario 3`` pre-fills ``Scenario 4``.
Projects whose scenarios carry custom names (nothing matching the
convention) fall back to ``Scenario 1``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Exact ``Scenario <n>`` only — a leading/trailing space is tolerated but
# anything else (``Scenario 1 - hot``, ``My run``) is treated as a custom
# name and ignored for numbering.
_SCENARIO_NUMBER = re.compile(r"^scenario\s+(\d+)$", re.IGNORECASE)


def next_scenario_name(existing_names: Iterable[str]) -> str:
    """Return the suggested default name for a new scenario.

    Counts only names of the exact form ``Scenario <n>`` (case-
    insensitive, surrounding whitespace ignored) and returns
    ``Scenario <highest + 1>``. The parsed integers are compared — not
    the strings — so ``Scenario 10`` sorts after ``Scenario 9`` rather
    than after ``Scenario 1``. With no conforming names, returns
    ``Scenario 1``.
    """
    highest = 0
    for name in existing_names:
        match = _SCENARIO_NUMBER.match((name or "").strip())
        if match:
            highest = max(highest, int(match.group(1)))
    return f"Scenario {highest + 1}"
