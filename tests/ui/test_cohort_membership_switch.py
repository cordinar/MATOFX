"""Turning a Job Cohort's auto-grow switch off (reported 2026-09-21).

Two complaints, both about the same control:

1. Switching auto-grow off answered with *"Preview and tick at least one
   run, or turn auto-grow on."* — a demand that arrived only after Save,
   naming rows, about a switch that is **cohort-wide**. It read as though
   ticking a row were what got switched off.
2. A cohort whose query matches nothing could never be switched off at
   all: the preview builds no table when there are no rows, so there was
   nothing to tick and the guard refused every save, permanently.

"Off" now means "keep what you have": with nothing ticked the cohort
freezes to whatever it currently matches, and ticking is the way to keep
*fewer* than that. A cohort matching nothing still can't be frozen —
there would be nothing in it — but it says so on the form and points at
Archive instead of stranding the curator.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from matofx.ui.pages.cohorts import _CohortBuilder, membership_badge

# ---- the membership chip --------------------------------------------


def test_a_live_cohort_reads_as_auto_growing() -> None:
    label, tip = membership_badge({"auto_grow": True})
    assert label == "Auto-growing"
    assert "join automatically" in tip


def test_a_frozen_cohort_reads_as_fixed() -> None:
    label, _tip = membership_badge({"auto_grow": False})
    assert label == "Fixed set"


def test_a_cohort_defaults_to_auto_growing_when_the_flag_is_absent() -> None:
    """Matches the API default, so a partial row can't read as frozen."""
    assert membership_badge({})[0] == "Auto-growing"


def test_a_frozen_cohort_with_commissioned_studies_says_so() -> None:
    """ "Fixed" alone is a promise the resolver doesn't keep.

    A non-growing cohort still unions in the completed runs of studies it
    commissioned, so its run count moves after the curator was told it
    wouldn't.
    """
    label, tip = membership_badge({"auto_grow": False, "auto_include_study_ids": [27]})
    assert label == "Fixed + commissioned"
    assert "1 study(ies)" in tip


def test_commissioned_studies_do_not_change_a_live_cohort_s_chip() -> None:
    """A live cohort was always going to grow; the studies add nothing to say."""
    assert (
        membership_badge({"auto_grow": True, "auto_include_study_ids": [27]})[0] == "Auto-growing"
    )


# ---- the under-switch explanation -----------------------------------


def _form(*, live: bool, existing: dict[str, Any] | None) -> Any:
    """A stand-in carrying only what ``_refresh_grow_hint`` reads."""
    return SimpleNamespace(
        grow_hint=SimpleNamespace(text=""),
        auto_grow=SimpleNamespace(value=live),
        existing=existing,
    )


def test_the_hint_describes_live_membership_when_the_switch_is_on() -> None:
    form = _form(live=True, existing=None)
    _CohortBuilder._refresh_grow_hint(form)
    assert "join on their own" in form.grow_hint.text


def test_switching_off_promises_to_freeze_what_is_listed() -> None:
    """The fix for complaint 1: say what "off" does, before Save."""
    form = _form(live=False, existing=None)
    _CohortBuilder._refresh_grow_hint(form)
    text = form.grow_hint.text
    assert "freezes to the runs listed below" in text
    # Ticking is optional narrowing, not a precondition.
    assert "only if you want fewer" in text


def test_an_already_frozen_cohort_is_told_its_set_is_kept() -> None:
    """Editing a fixed cohort's name must not imply its runs are at risk."""
    form = _form(live=False, existing={"member_job_ids": [1, 2, 3]})
    _CohortBuilder._refresh_grow_hint(form)
    assert "stays frozen to the runs it already holds" in form.grow_hint.text


def test_the_hint_is_a_no_op_before_the_widget_exists() -> None:
    """``render`` sets the label late; a call before that must not raise."""
    form = SimpleNamespace(grow_hint=None, auto_grow=None, existing=None)
    _CohortBuilder._refresh_grow_hint(form)  # must not raise


def test_the_hint_never_mentions_disabling_a_row() -> None:
    """The switch is cohort-wide; nothing here may suggest otherwise."""
    for live in (True, False):
        form = _form(live=live, existing=None)
        _CohortBuilder._refresh_grow_hint(form)
        assert "row" not in form.grow_hint.text.lower().replace("rows", "")


# ---- freezing from the current matches ------------------------------


class _StubClient:
    def __init__(self, rows: list[dict[str, Any]], acl_hidden: int = 0) -> None:
        self._rows = rows
        self._hidden = acl_hidden
        self.calls = 0

    async def preview_cohort_candidates(self, **_kw: Any) -> dict[str, Any]:
        self.calls += 1
        return {"rows": self._rows, "acl_hidden": self._hidden}


def _builder_for(client: Any, *, tool_alias: str | None = "heat_sink") -> Any:
    return SimpleNamespace(
        client=client,
        tool_alias=tool_alias,
        _selected_bands=lambda: [],
        _gather_filters=lambda: {},
    )


@pytest.mark.asyncio
async def test_current_matches_are_returned_as_the_set_to_freeze() -> None:
    client = _StubClient([{"job_id": 7}, {"job_id": 9}])
    got = await _CohortBuilder._current_match_ids(_builder_for(client))
    assert got == [7, 9]


@pytest.mark.asyncio
async def test_no_matches_returns_an_empty_set_not_a_failure() -> None:
    """The caller distinguishes "nothing matches" (refuse, explain) from
    "the lookup broke" (``None``) — collapsing them would blame the
    curator for a network error."""
    got = await _CohortBuilder._current_match_ids(_builder_for(_StubClient([])))
    assert got == []


@pytest.mark.asyncio
async def test_no_tool_short_circuits_without_calling_the_api() -> None:
    client = _StubClient([{"job_id": 7}])
    got = await _CohortBuilder._current_match_ids(_builder_for(client, tool_alias=None))
    assert got == []
    assert client.calls == 0
