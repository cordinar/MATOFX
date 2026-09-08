"""Unit tests for the pure aggregation helpers in :mod:`matofx.usage.service`.

These exercise the resource parsing, runtime/core-hours maths, KPI delta
computation, and the series / leaderboard reductions without a database
— the end-to-end query path is covered by the router tests.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from matofx.usage import JobFilters
from matofx.usage.service import (
    _active_users_trend,
    _aggregate,
    _consumption_by_team,
    _consumption_by_tool,
    _exit_code_counts,
    _humanize_role,
    _infra_stats,
    _JobRow,
    _jobs_by_role,
    _jobs_by_team_named,
    _jobs_by_user,
    _kpi,
    _peak_concurrency,
    _peak_concurrency_by_day,
    _percentile,
    _queue_wait_series,
    _resources,
    _runtime_distribution,
    _runtime_hours,
    _series,
    _shift_window,
    _success_rate,
    _success_trend,
    _throughput_series,
    _top_failing,
    _top_tools,
)

_BASE = _dt.datetime(2026, 5, 20, 12, 0, tzinfo=_dt.UTC)


def _row(
    state: str,
    *,
    ran_minutes: int | None = None,
    wait_min: int = 0,
    cores: float = 1.0,
    gpu: float = 0.0,
    alias: str = "alpha",
    name: str = "Alpha",
    user: int = 1,
    created: _dt.datetime | None = None,
    exit_code: int | None = None,
    target: str = "local",
) -> _JobRow:
    """Build a ``_JobRow``.

    ``wait_min`` offsets ``started_at`` from ``created_at`` (queue wait);
    ``ran_minutes`` then sets ``completed_at`` that far after the start.
    A row with neither never started.
    """
    created_at = created or _BASE
    started = completed = None
    if ran_minutes is not None or wait_min:
        started = created_at + _dt.timedelta(minutes=wait_min)
    if ran_minutes is not None:
        completed = (started or created_at) + _dt.timedelta(minutes=ran_minutes)
    return _JobRow(
        state=state,
        created_at=created_at,
        started_at=started,
        completed_at=completed,
        tool_alias=alias,
        tool_name=name,
        cores=cores,
        gpu_count=gpu,
        user_id=user,
        exit_code=exit_code,
        execution_target=target,
    )


def test_resources_defaults_and_coercion() -> None:
    assert _resources(None) == (1.0, 0.0)
    assert _resources({"cores": 8, "gpu_count": 2}) == (8.0, 2.0)
    assert _resources({"cores": "4"}) == (4.0, 0.0)
    # bool is an int subclass but is never a valid resource count.
    assert _resources({"cores": True}) == (1.0, 0.0)
    # zero / missing cores fall back to one core.
    assert _resources({"cores": 0}) == (1.0, 0.0)


def test_runtime_hours() -> None:
    assert _runtime_hours(_row("complete", ran_minutes=60)) == pytest.approx(1.0)
    assert _runtime_hours(_row("complete", ran_minutes=90)) == pytest.approx(1.5)
    assert _runtime_hours(_row("running")) is None


def test_aggregate_success_rate_and_core_hours() -> None:
    rows = [
        _row("complete", ran_minutes=60, cores=2.0, user=1),
        _row("complete", ran_minutes=60, cores=2.0, user=2),
        _row("failed", user=3),
    ]
    agg = _aggregate(rows)
    assert agg.jobs_total == 3
    assert agg.active_users == 3
    # 2 complete of 3 terminal → 66.67%.
    assert agg.success_rate == pytest.approx(200.0 / 3.0)
    # Two 1-hour jobs at 2 cores each = 4 core-hours; the failed job
    # never ran so it contributes nothing.
    assert agg.core_hours == pytest.approx(4.0)
    assert agg.gpu_hours == pytest.approx(0.0)


def test_aggregate_no_terminal_jobs_has_undefined_success_rate() -> None:
    agg = _aggregate([_row("running"), _row("queued")])
    assert agg.success_rate is None
    assert agg.jobs_total == 2


def test_gpu_hours_counted() -> None:
    agg = _aggregate([_row("complete", ran_minutes=120, cores=4.0, gpu=2.0)])
    assert agg.core_hours == pytest.approx(8.0)
    assert agg.gpu_hours == pytest.approx(4.0)


def test_kpi_delta() -> None:
    assert _kpi(10.0, 5.0).delta_pct == pytest.approx(100.0)
    assert _kpi(5.0, 10.0).delta_pct == pytest.approx(-50.0)
    # No prior value, or a zero prior, yields no delta.
    assert _kpi(10.0, 0.0).delta_pct is None
    assert _kpi(10.0, None).delta_pct is None
    # Undefined current value passes through.
    assert _kpi(None, 5.0).value is None


def test_shift_window_moves_back_one_span() -> None:
    f = JobFilters(since=_BASE, until=_BASE + _dt.timedelta(days=30))
    prior = _shift_window(f)
    assert prior.until == f.since
    assert prior.since == f.since - _dt.timedelta(days=30)


def test_series_groups_by_day_and_state() -> None:
    day2 = _BASE + _dt.timedelta(days=1)
    rows = [
        _row("complete", created=_BASE),
        _row("complete", created=_BASE),
        _row("failed", created=day2),
    ]
    series = _series(rows)
    counts = {(p.date, p.state): p.count for p in series}
    assert counts[(_BASE.date(), "complete")] == 2
    assert counts[(day2.date(), "failed")] == 1


def test_top_tools_orders_by_count() -> None:
    rows = [
        _row("complete", alias="alpha", name="Alpha"),
        _row("complete", alias="alpha", name="Alpha"),
        _row("complete", alias="beta", name="Beta"),
    ]
    top = _top_tools(rows)
    assert top[0].tool_alias == "alpha"
    assert top[0].count == 2
    assert top[1].tool_alias == "beta"
    assert top[1].count == 1


def test_percentile() -> None:
    assert _percentile([], 0.5) == 0.0
    assert _percentile([5.0], 0.95) == pytest.approx(5.0)
    assert _percentile([0.0, 1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.0)
    # Linear interpolation between the two points.
    assert _percentile([0.0, 10.0], 0.95) == pytest.approx(9.5)


def test_consumption_by_tool_sorted_by_core_hours() -> None:
    rows = [
        _row("complete", ran_minutes=60, cores=2.0, alias="alpha", name="Alpha"),
        _row("complete", ran_minutes=60, cores=2.0, alias="alpha", name="Alpha"),
        _row("complete", ran_minutes=30, cores=4.0, alias="beta", name="Beta"),
    ]
    by_tool = _consumption_by_tool(rows)
    assert by_tool[0].tool_alias == "alpha"
    assert by_tool[0].core_hours == pytest.approx(4.0)
    assert by_tool[0].jobs == 2
    assert by_tool[1].tool_alias == "beta"
    assert by_tool[1].core_hours == pytest.approx(2.0)


def test_consumption_by_team_attributes_to_each_team() -> None:
    user_teams = {1: [(10, "R&D")], 2: [(10, "R&D"), (20, "M&S")]}
    rows = [
        _row("complete", ran_minutes=60, cores=1.0, user=1),
        _row("complete", ran_minutes=60, cores=1.0, user=2),
        _row("complete", ran_minutes=60, cores=1.0, user=3),  # no team
    ]
    by_team = {c.team_name: c for c in _consumption_by_team(rows, user_teams)}
    assert by_team["R&D"].jobs == 2
    assert by_team["R&D"].core_hours == pytest.approx(2.0)
    assert by_team["M&S"].jobs == 1
    assert by_team["(No team)"].team_id is None
    assert by_team["(No team)"].jobs == 1


def test_runtime_distribution() -> None:
    rows = [
        _row("complete", ran_minutes=60, alias="alpha", name="Alpha"),
        _row("complete", ran_minutes=120, alias="alpha", name="Alpha"),
        _row("running", alias="alpha", name="Alpha"),  # never ran → excluded
    ]
    dist = _runtime_distribution(rows)
    assert len(dist) == 1
    assert dist[0].runs == 2
    assert dist[0].p50_hours == pytest.approx(1.5)
    assert dist[0].max_hours == pytest.approx(2.0)


def test_queue_wait_series() -> None:
    rows = [_row("complete", ran_minutes=10, wait_min=30)]
    series = _queue_wait_series(rows)
    assert len(series) == 1
    assert series[0].value == pytest.approx(0.5)  # 30 min wait → 0.5h


def test_throughput_series() -> None:
    day2 = _BASE + _dt.timedelta(days=1)
    rows = [
        _row("complete", created=_BASE, ran_minutes=10),  # completes same day (_BASE)
        _row("queued", created=_BASE),  # submitted day1, never completed
        _row("complete", created=day2, ran_minutes=10),  # submitted+completed day2
    ]
    points = {p.date: p for p in _throughput_series(rows)}
    assert points[_BASE.date()].submitted == 2
    assert points[_BASE.date()].completed == 1
    assert points[day2.date()].submitted == 1
    assert points[day2.date()].completed == 1


def test_success_rate() -> None:
    assert _success_rate([_row("complete"), _row("complete"), _row("failed")]) == pytest.approx(
        200.0 / 3.0
    )
    # No terminal jobs → undefined.
    assert _success_rate([_row("running"), _row("queued")]) is None


def test_success_trend_per_day() -> None:
    day2 = _BASE + _dt.timedelta(days=1)
    rows = [
        _row("complete", created=_BASE),
        _row("failed", created=_BASE),
        _row("complete", created=day2),
        _row("running", created=day2),  # non-terminal → ignored
    ]
    trend = {p.date: p.value for p in _success_trend(rows)}
    assert trend[_BASE.date()] == pytest.approx(50.0)
    assert trend[day2.date()] == pytest.approx(100.0)


def test_top_failing() -> None:
    rows = [
        _row("failed", alias="alpha", name="Alpha"),
        _row("failed", alias="alpha", name="Alpha"),
        _row("complete", alias="alpha", name="Alpha"),
        _row("failed", alias="beta", name="Beta"),
        _row("complete", alias="gamma", name="Gamma"),  # never fails → excluded
    ]
    failing = _top_failing(rows)
    assert [f.tool_alias for f in failing] == ["alpha", "beta"]
    assert failing[0].failures == 2
    assert failing[0].failure_rate == pytest.approx(200.0 / 3.0)  # 2 of 3 terminal
    assert failing[1].failure_rate == pytest.approx(100.0)


def test_exit_code_counts() -> None:
    rows = [
        _row("complete", exit_code=0),
        _row("complete", exit_code=0),
        _row("failed", exit_code=1),
        _row("running", exit_code=None),  # no code → excluded
    ]
    counts = {c.exit_code: c.count for c in _exit_code_counts(rows)}
    assert counts == {0: 2, 1: 1}


# ---- People & Adoption ----------------------------------------------------


def test_humanize_role() -> None:
    assert _humanize_role("super_user") == "Super User"
    assert _humanize_role("regular_user") == "Regular User"
    assert _humanize_role("") == "Unknown"


def test_active_users_trend() -> None:
    day2 = _BASE + _dt.timedelta(days=1)
    rows = [
        _row("complete", created=_BASE, user=1),
        _row("complete", created=_BASE, user=2),
        _row("complete", created=_BASE, user=1),  # same user, same day
        _row("complete", created=day2, user=1),
    ]
    trend = {p.date: p.value for p in _active_users_trend(rows)}
    assert trend[_BASE.date()] == pytest.approx(2.0)
    assert trend[day2.date()] == pytest.approx(1.0)


def test_jobs_by_user_team_role() -> None:
    directory = {1: ("Alice", "regular_user"), 2: ("Bob", "administrator")}
    user_teams = {1: [(10, "R&D")], 2: [(10, "R&D"), (20, "M&S")]}
    rows = [_row("complete", user=1), _row("complete", user=1), _row("complete", user=2)]

    by_user = {n.name: n.count for n in _jobs_by_user(rows, directory)}
    assert by_user == {"Alice": 2, "Bob": 1}

    by_team = {n.name: n.count for n in _jobs_by_team_named(rows, user_teams)}
    assert by_team == {"R&D": 3, "M&S": 1}

    by_role = {n.name: n.count for n in _jobs_by_role(rows, directory)}
    assert by_role == {"Regular User": 2, "Administrator": 1}


# ---- Seats & concurrency --------------------------------------------------


def test_peak_concurrency() -> None:
    t0 = _BASE
    hour = _dt.timedelta(hours=1)
    intervals = [
        (t0, t0 + 2 * hour),
        (t0 + hour, t0 + 3 * hour),  # overlaps the first
        (t0 + 5 * hour, t0 + 6 * hour),  # separate
    ]
    assert _peak_concurrency(intervals) == 2
    # Closed intervals: a zero-length session still counts as one present.
    assert _peak_concurrency([(t0, t0)]) == 1
    # Touching endpoints count as overlap (closed-interval convention).
    assert _peak_concurrency([(t0, t0 + hour), (t0 + hour, t0 + 2 * hour)]) == 2


def test_peak_concurrency_by_day() -> None:
    t0 = _dt.datetime(2026, 5, 20, 9, 0, tzinfo=_dt.UTC)
    hour = _dt.timedelta(hours=1)
    intervals = [(t0, t0 + 2 * hour), (t0 + hour, t0 + 3 * hour)]
    since = _dt.datetime(2026, 5, 20, 0, 0, tzinfo=_dt.UTC)
    until = _dt.datetime(2026, 5, 20, 23, 59, 59, tzinfo=_dt.UTC)
    series = _peak_concurrency_by_day(intervals, since, until)
    assert len(series) == 1
    assert series[0].value == pytest.approx(2.0)


# ---- Infrastructure -------------------------------------------------------


def test_infra_stats() -> None:
    rows = [
        _row("complete", ran_minutes=60, cores=2.0, target="local"),
        _row("complete", ran_minutes=60, cores=1.0, gpu=1.0, target="aos"),
        _row("failed", target="local"),
    ]
    stats = _infra_stats(rows)
    by_target = {t.target: t for t in stats.by_target}
    assert by_target["local"].jobs == 2
    assert by_target["local"].core_hours == pytest.approx(2.0)
    assert by_target["aos"].jobs == 1
    assert stats.cpu_jobs == 2
    assert stats.gpu_jobs == 1
    assert stats.cpu_core_hours == pytest.approx(2.0)
    assert stats.gpu_core_hours == pytest.approx(1.0)
