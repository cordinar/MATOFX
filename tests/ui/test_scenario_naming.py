"""Unit tests for the next-scenario-name suggestion helper.

Covers the sequential ``Scenario <n>`` numbering, the numeric (not
string) ordering so ``Scenario 10`` follows ``Scenario 9``, and the
fall-back to ``Scenario 1`` when scenarios carry custom names.
"""

from __future__ import annotations

import pytest

from matofx.ui.scenario_naming import next_scenario_name


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        ([], "Scenario 1"),  # empty project
        (["Scenario 1"], "Scenario 2"),
        (["Scenario 1", "Scenario 2", "Scenario 3"], "Scenario 4"),  # the headline case
        (["Scenario 9", "Scenario 10"], "Scenario 11"),  # 10 sorts after 9, not after 1
        (["Scenario 1", "Scenario 9"], "Scenario 10"),  # numeric max, not lexical
        (["Scenario 1", "Scenario 3"], "Scenario 4"),  # gaps → max + 1, not fill
        (["Scenario 2", "Scenario 2"], "Scenario 3"),  # duplicates don't double-count
        (["  scenario 5  ", "SCENARIO 7"], "Scenario 8"),  # case + surrounding space
        (["Baseline", "Hot run"], "Scenario 1"),  # all custom → default
        (["Baseline", "Scenario 2"], "Scenario 3"),  # mix → next after the conforming max
        (["Scenario 1 - hot", "Scenario1", "Scenario one"], "Scenario 1"),  # near-misses ignored
        (["", "Scenario 4"], "Scenario 5"),  # blank names skipped
    ],
)
def test_next_scenario_name(existing: list[str], expected: str) -> None:
    assert next_scenario_name(existing) == expected


def test_next_scenario_name_accepts_a_generator() -> None:
    """Call sites pass a generator over fetched scenarios, not a list."""
    names = (f"Scenario {n}" for n in (1, 2, 3))
    assert next_scenario_name(names) == "Scenario 4"
