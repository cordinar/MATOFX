"""A ``goal: track`` response is reported on the study that declared it.

"Track only" means *recorded but not chased*, and the recording half was
missing from the one page where it was declared. The value was collected on
every run — the runtime builds its response vector from **every** declared
response, goal or not — and written to the run's ``study_result`` row, but the
Runs table, the CSV, the Pareto/best-designs table and the explorer axes were
all built from the optimised list alone. A tracked response could be read on
the job page, in Compare, or via a cohort: anywhere except the study that asked
for it.

These cover the two halves that must stay true together: a tracked response is
**shown** everywhere a run is listed or plotted, and it is **never** treated as
a goal (no Pareto membership, no weighted score, no ranking).
"""

from __future__ import annotations

from typing import Any

from matofx.ui.pages.optimisation import (
    _derive_runs,
    _objectives_from_config,
    _tracked_from_config,
    _weighted_scores,
)

# The shape of the live "Cold finned tool - using new surrogate" study: two
# minimised objectives and two tracked responses.
CONFIG: dict[str, Any] = {
    "responses": [
        {"name": "mass", "goal": "minimise", "spec": {"kind": "scalar", "name": "mass"}},
        {"name": "fan_power", "goal": "track", "spec": {"kind": "scalar", "name": "fan_power"}},
        {"name": "t_source", "goal": "minimise", "spec": {"kind": "scalar", "name": "t_source"}},
        {"name": "t_mean", "goal": "track", "spec": {"kind": "scalar", "name": "t_mean"}},
    ]
}
OUT_LABELS = {
    "mass": "Plate + fin mass",
    "t_source": "Peak component temperature",
    "fan_power": "Fan power draw",
    "t_mean": "Mean plate temperature",
}
OUT_UNITS = {"mass": "g", "t_source": "degC", "fan_power": "W", "t_mean": "degC"}


def _runs() -> list[dict[str, Any]]:
    return [
        {
            "job_id": 1,
            "state": "complete",
            "batch": 1,
            "scalars": {"mass": 50.0, "t_source": 120.0, "fan_power": 0.75, "t_mean": 76.5},
            "inputs": {},
        },
        {
            "job_id": 2,
            "state": "complete",
            "batch": 1,
            "scalars": {"mass": 75.0, "t_source": 75.0, "fan_power": 0.67, "t_mean": 48.7},
            "inputs": {},
        },
        {
            "job_id": 3,
            "state": "complete",
            "batch": 2,
            "scalars": {"mass": 80.0, "t_source": 130.0, "fan_power": 0.90, "t_mean": 88.0},
            "inputs": {},
        },
    ]


# --- parsing ----------------------------------------------------------------


def test_tracked_responses_are_parsed_with_their_labels_and_units() -> None:
    tracked = _tracked_from_config(CONFIG, OUT_LABELS, OUT_UNITS)
    assert [t["key"] for t in tracked] == ["fan_power", "t_mean"]
    assert [t["label"] for t in tracked] == ["Fan power draw", "Mean plate temperature"]
    assert [t["unit"] for t in tracked] == ["W", "degC"]


def test_objectives_and_tracked_are_disjoint() -> None:
    """The two lists partition the responses; nothing appears in both."""
    objectives = _objectives_from_config(CONFIG, OUT_LABELS, OUT_UNITS)
    tracked = _tracked_from_config(CONFIG, OUT_LABELS, OUT_UNITS)
    assert [o["key"] for o in objectives] == ["mass", "t_source"]
    assert not {o["key"] for o in objectives} & {t["key"] for t in tracked}


def test_a_study_with_no_tracked_responses_yields_none() -> None:
    cfg = {"responses": [r for r in CONFIG["responses"] if r["goal"] != "track"]}
    assert _tracked_from_config(cfg, OUT_LABELS, OUT_UNITS) == []


def test_a_workflow_response_keys_by_member_and_output() -> None:
    """A workflow scalar is stored member-qualified, and the key must match."""
    cfg = {
        "responses": [
            {
                "name": "outlet_t",
                "goal": "track",
                "spec": {"kind": "scalar", "member": "solver", "name": "t_out"},
            }
        ]
    }
    assert _tracked_from_config(cfg, {}, {})[0]["key"] == "solver.t_out"


# --- derived values ---------------------------------------------------------


def test_tracked_values_ride_on_every_run() -> None:
    objectives = _objectives_from_config(CONFIG, OUT_LABELS, OUT_UNITS)
    tracked = _tracked_from_config(CONFIG, OUT_LABELS, OUT_UNITS)
    derived = _derive_runs(_runs(), objectives, [], tracked)
    assert [d["track_vals"] for d in derived] == [
        [0.75, 76.5],
        [0.67, 48.7],
        [0.90, 88.0],
    ]


def test_a_missing_tracked_value_is_none_not_an_error() -> None:
    """A run that failed before writing an output must not break the table."""
    objectives = _objectives_from_config(CONFIG, OUT_LABELS, OUT_UNITS)
    tracked = _tracked_from_config(CONFIG, OUT_LABELS, OUT_UNITS)
    runs = _runs()
    runs[0]["scalars"].pop("t_mean")
    derived = _derive_runs(runs, objectives, [], tracked)
    assert derived[0]["track_vals"] == [0.75, None]


def test_derive_runs_without_tracked_still_works() -> None:
    """Back-compat: the argument is optional and defaults to none."""
    objectives = _objectives_from_config(CONFIG, OUT_LABELS, OUT_UNITS)
    derived = _derive_runs(_runs(), objectives, [])
    assert all(d["track_vals"] == [] for d in derived)


# --- a tracked response is never a goal -------------------------------------


def test_a_tracked_response_never_reaches_the_pareto_front() -> None:
    """Run 3 is dominated on both objectives, however good its tracked values.

    If a tracked response leaked into the objective list the front would
    change shape — which is the whole reason the two lists stay separate.
    """
    objectives = _objectives_from_config(CONFIG, OUT_LABELS, OUT_UNITS)
    tracked = _tracked_from_config(CONFIG, OUT_LABELS, OUT_UNITS)
    derived = _derive_runs(_runs(), objectives, [], tracked)
    on_front = {d["job_id"] for d in derived if d["on_front"]}
    assert on_front == {1, 2}


def test_the_weighted_score_ignores_tracked_values() -> None:
    """The score is built from ``obj_vals`` alone, so tracking changes nothing."""
    objectives = _objectives_from_config(CONFIG, OUT_LABELS, OUT_UNITS)
    tracked = _tracked_from_config(CONFIG, OUT_LABELS, OUT_UNITS)
    with_tracked = _derive_runs(_runs(), objectives, [], tracked)
    without = _derive_runs(_runs(), objectives, [])
    assert _weighted_scores(with_tracked, objectives) == _weighted_scores(without, objectives)


def test_pareto_membership_is_unchanged_by_tracking() -> None:
    objectives = _objectives_from_config(CONFIG, OUT_LABELS, OUT_UNITS)
    tracked = _tracked_from_config(CONFIG, OUT_LABELS, OUT_UNITS)
    a = [d["on_front"] for d in _derive_runs(_runs(), objectives, [], tracked)]
    b = [d["on_front"] for d in _derive_runs(_runs(), objectives, [])]
    assert a == b
