"""Tests for local host-capacity detection + budget resolution."""

from __future__ import annotations

from matofx.runner.hostcaps import (
    HostCapacity,
    detect_host_capacity,
    reset_capacity_cache,
    resolve_local_budget,
)
from matofx.runner.settings import DEFAULT_RUNTIME_SETTINGS


def test_detect_host_capacity_is_positive_and_cached() -> None:
    reset_capacity_cache()
    first = detect_host_capacity()
    assert first.cpu_cores >= 1
    assert first.memory_mb >= 1
    # Second call returns the identical cached instance.
    assert detect_host_capacity() is first


def test_explicit_budget_overrides_detection() -> None:
    settings = DEFAULT_RUNTIME_SETTINGS.__class__(
        local_concurrency=2,
        heartbeat_local_aos_interval_s=2.0,
        heartbeat_hpc_running_interval_s=10.0,
        heartbeat_hpc_queued_interval_s=30.0,
        cancel_grace_s=30,
        study_concurrency_cap=10,
        local_cpu_cores=8,
        local_memory_mb=16384,
    )
    budget = resolve_local_budget(settings)
    assert budget.cpu_cores == 8
    assert budget.memory_mb == 16384  # explicit budget honoured verbatim, no reserve


def test_auto_detect_applies_memory_reserve(monkeypatch) -> None:
    reset_capacity_cache()
    monkeypatch.setattr(
        "matofx.runner.hostcaps.detect_host_capacity",
        lambda: HostCapacity(cpu_cores=16, memory_mb=32_000),
    )
    settings = DEFAULT_RUNTIME_SETTINGS.__class__(
        local_concurrency=2,
        heartbeat_local_aos_interval_s=2.0,
        heartbeat_hpc_running_interval_s=10.0,
        heartbeat_hpc_queued_interval_s=30.0,
        cancel_grace_s=30,
        study_concurrency_cap=10,
        local_cpu_cores=0,  # auto-detect
        local_memory_mb=0,  # auto-detect
        local_memory_reserve_mb=2048,
    )
    budget = resolve_local_budget(settings)
    assert budget.cpu_cores == 16
    assert budget.memory_mb == 32_000 - 2048


def test_oversized_reserve_floors_budget_at_one(monkeypatch) -> None:
    """A reserve larger than total RAM must never wedge the queue with a 0 budget."""
    reset_capacity_cache()
    monkeypatch.setattr(
        "matofx.runner.hostcaps.detect_host_capacity",
        lambda: HostCapacity(cpu_cores=4, memory_mb=1024),
    )
    settings = DEFAULT_RUNTIME_SETTINGS.__class__(
        local_concurrency=2,
        heartbeat_local_aos_interval_s=2.0,
        heartbeat_hpc_running_interval_s=10.0,
        heartbeat_hpc_queued_interval_s=30.0,
        cancel_grace_s=30,
        study_concurrency_cap=10,
        local_cpu_cores=0,
        local_memory_mb=0,
        local_memory_reserve_mb=100_000,  # absurdly large
    )
    budget = resolve_local_budget(settings)
    assert budget.memory_mb == 1  # floored, not negative or zero
