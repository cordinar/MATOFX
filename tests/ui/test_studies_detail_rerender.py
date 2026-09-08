"""Regression tests for the study detail page's poll-driven rerender.

The bug: while a study was still running, the 2.5 s poll loop tore down
*both* tab panels on every tick. The user's selected tab wasn't tracked,
so the rebuild kept bouncing reviewers off the Results tab back to
Progress — and even when it didn't, it wiped their grid selection /
filters. The fix makes ``_rerender`` rebuild only the panel the user is
actually on, never tearing down the Results panel underneath them; fresh
data surfaces as a Refresh hint instead.

These exercise the pure decision logic (which panel gets rebuilt for a
given active tab / signature change) by stubbing the actual NiceGUI
rebuild helpers, so no app/browser context is needed.
"""

from __future__ import annotations

from typing import Any

import pytest

from matofx.ui.pages import studies


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Replace the rebuild helpers with counters; return the call tally."""
    tally = {"header": 0, "progress": 0, "results": 0, "hint": 0, "js": 0}

    # `_on_tab_change` also persists the selected tab to the URL via
    # `ui.run_javascript`, which needs a live client + event loop and so
    # raised `assert core.loop is not None` here. It is a NiceGUI side
    # effect like the rebuild helpers below, so it is stubbed the same
    # way — and counted, so the URL persistence is actually asserted
    # rather than merely tolerated.
    def _js(*_args: Any, **_kwargs: Any) -> None:
        tally["js"] += 1

    monkeypatch.setattr(studies.ui, "run_javascript", _js)

    def _bump(key: str):
        def _fn(_state: dict[str, Any]) -> None:
            tally[key] += 1

        return _fn

    monkeypatch.setattr(studies, "_rebuild_header", _bump("header"))
    monkeypatch.setattr(studies, "_rebuild_progress", _bump("progress"))
    monkeypatch.setattr(studies, "_rebuild_results", _bump("results"))
    monkeypatch.setattr(studies, "_refresh_results_hint", _bump("hint"))
    return tally


def _state(
    *,
    active_tab: str,
    child_state: str = "running",
    scalars: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "detail": {
            "study": {"id": 1, "state": "running"},
            "children": [{"id": 11, "state": child_state}],
        },
        "results": {"children": [{"job_id": 11, "scalars": scalars or {}, "retained_files": []}]},
        "last_signature": None,
        "active_tab": active_tab,
        "rendered_results_sig": None,
        "results_dirty": False,
    }


def test_force_rebuilds_every_panel(calls: dict[str, int]) -> None:
    state = _state(active_tab="Results")
    studies._rerender(state, force=True)
    assert (calls["header"], calls["progress"], calls["results"]) == (1, 1, 1)


def test_poll_on_progress_rebuilds_progress_not_results(calls: dict[str, int]) -> None:
    state = _state(active_tab="Progress")
    state["rendered_results_sig"] = studies._results_sig(state)
    studies._rerender(state)  # poll tick, signature differs from None
    assert calls["progress"] == 1
    # Results panel is left intact while off-screen — no teardown, no flag.
    assert calls["results"] == 0
    assert state["results_dirty"] is False


def test_poll_on_results_does_not_tear_it_down_but_flags_dirty(calls: dict[str, int]) -> None:
    state = _state(active_tab="Results")
    state["rendered_results_sig"] = studies._results_sig(state)
    state["last_signature"] = studies._signature(state)
    # A new scalar lands → results slice changes while the user reads the tab.
    state["results"]["children"][0]["scalars"] = {"drag": 1.0}
    studies._rerender(state)
    assert calls["results"] == 0  # never rebuilt under the user
    assert calls["progress"] == 0  # Progress is off-screen; left stale
    assert state["results_dirty"] is True
    assert calls["hint"] == 1


def test_poll_on_results_no_new_data_leaves_clean(calls: dict[str, int]) -> None:
    state = _state(active_tab="Results")
    state["rendered_results_sig"] = studies._results_sig(state)
    state["last_signature"] = studies._signature(state)
    # Child state flips (gate sig changes) but no new scalars/files.
    state["detail"]["children"][0]["state"] = "complete"
    studies._rerender(state)
    assert calls["results"] == 0
    assert state["results_dirty"] is False


def test_unchanged_signature_is_a_noop(calls: dict[str, int]) -> None:
    state = _state(active_tab="Progress")
    state["last_signature"] = studies._signature(state)
    studies._rerender(state)
    # Nothing at all: no panel rebuilt, no hint, and no URL write either
    # (a poll tick must not rewrite history behind the reader).
    assert calls == {"header": 0, "progress": 0, "results": 0, "hint": 0, "js": 0}


def test_switch_to_results_when_dirty_rebuilds(calls: dict[str, int]) -> None:
    state = _state(active_tab="Results")
    state["results_dirty"] = True
    studies._on_tab_change(state, "Results")
    assert calls["results"] == 1


def test_switch_to_results_when_fresh_does_not_rebuild(calls: dict[str, int]) -> None:
    state = _state(active_tab="Progress")
    state["rendered_results_sig"] = studies._results_sig(state)
    state["results_dirty"] = False
    studies._on_tab_change(state, "Results")
    assert calls["results"] == 0
    assert state["active_tab"] == "Results"


def test_tab_change_persists_the_selection_to_the_url(calls: dict[str, int]) -> None:
    """A reload / re-sync must land on the tab the reader was on."""
    state = _state(active_tab="Progress")
    studies._on_tab_change(state, "Results")
    assert state["active_tab"] == "Results"
    assert calls["js"] == 1


def test_switch_to_progress_rebuilds_progress(calls: dict[str, int]) -> None:
    state = _state(active_tab="Results")
    studies._on_tab_change(state, "Progress")
    assert calls["progress"] == 1
    assert state["active_tab"] == "Progress"
