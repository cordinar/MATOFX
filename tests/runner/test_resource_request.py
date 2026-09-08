"""What we actually ask the scheduler for (D-11).

Until 2026-08-13 ``_resource_request_for`` read only the nullable
``Tool.resource_request`` override, and nothing on the install path ever
populates it — so every HPC job went out as ``--cpus-per-task=1
--mem=512M`` with no ``--time``, whatever the tool's descriptor declared.
A tool asking for 16 cores got one; a tool asking for 64 GB got 512 MB
and was OOM-killed, which reads as the tool's fault rather than ours.

Found on ENV-A rather than here, because ``sacct`` reports what the
controller was actually asked for, while every unit test to that point
asserted on values we had passed in by hand. These tests close that gap:
they start from a descriptor, which is where the numbers really come
from.
"""

from __future__ import annotations

from typing import Any

import pytest

from matofx.runner.supervisor import _resource_request_for


class _FakeTool:
    """Minimal stand-in — the resolver only reads these two attributes."""

    def __init__(
        self,
        resources: dict[str, Any] | None = None,
        resource_request: Any = None,
    ) -> None:
        self.descriptor: Any = {"alias": "t", "resources": resources or {}}
        self.resource_request = resource_request


def test_descriptor_resources_reach_the_scheduler() -> None:
    """The regression itself: declared numbers must survive to the request."""
    rr = _resource_request_for(
        _FakeTool({"cpu_cores": 16, "ram_mb": 65536, "walltime_minutes": 240})
    )
    assert rr.cpu_cores == 16
    assert rr.memory_mb == 65536
    assert rr.walltime_minutes == 240


def test_a_tool_declaring_nothing_keeps_the_old_defaults() -> None:
    """Tools with no resources block must behave exactly as before."""
    rr = _resource_request_for(_FakeTool({}))
    assert (rr.cpu_cores, rr.memory_mb) == (1, 512)
    assert rr.walltime_minutes is None
    assert rr.gpu_count == 0


def test_no_walltime_declared_means_no_time_flag() -> None:
    """``None`` must stay ``None`` — the builder omits ``--time`` entirely.

    A default here would be worse than none: it would silently kill
    legitimate long solves on installs that never asked for a limit.
    """
    assert _resource_request_for(_FakeTool({"cpu_cores": 4})).walltime_minutes is None


def test_the_override_beats_the_descriptor_field_by_field() -> None:
    """``resource_request`` overrules per key, and only per key."""
    rr = _resource_request_for(
        _FakeTool(
            {"cpu_cores": 16, "ram_mb": 65536, "walltime_minutes": 240},
            {"cores": 2, "queue_override": "debug"},
        )
    )
    assert rr.cpu_cores == 2, "override should win"
    assert rr.memory_mb == 65536, "descriptor should show through where the override is silent"
    assert rr.walltime_minutes == 240
    assert rr.queue_override == "debug"


def test_gpu_memory_maps_to_one_device() -> None:
    """The descriptor states GPU *memory*; ``--gres`` counts whole devices.

    Only the sign is used, matching how the scheduler already routes GPU
    work. An override naming ``gpu_count`` says what it means and wins.
    """
    assert _resource_request_for(_FakeTool({"gpu_memory_mb": 24000})).gpu_count == 1
    assert _resource_request_for(_FakeTool({"gpu_memory_mb": 0})).gpu_count == 0
    assert (
        _resource_request_for(_FakeTool({"gpu_memory_mb": 24000}, {"gpu_count": 4})).gpu_count == 4
    )


@pytest.mark.parametrize("junk", [None, "not-a-dict", 42, []])
def test_a_malformed_override_falls_back_rather_than_failing(junk: Any) -> None:
    """A bad override must never stop a submission — the descriptor still applies."""
    rr = _resource_request_for(_FakeTool({"cpu_cores": 8, "ram_mb": 4096}, junk))
    assert (rr.cpu_cores, rr.memory_mb) == (8, 4096)


@pytest.mark.parametrize("junk", [None, "nope", 3.5, True])
def test_non_integer_values_are_ignored_not_coerced(junk: Any) -> None:
    """Including ``True`` — a bool is an int in Python and must not become 1 core."""
    rr = _resource_request_for(_FakeTool({"cpu_cores": junk, "ram_mb": junk}))
    assert (rr.cpu_cores, rr.memory_mb) == (1, 512)


def test_a_descriptor_without_a_resources_block_is_survivable() -> None:
    """Older descriptors, and rows whose JSON is not the shape we expect."""
    tool = _FakeTool()
    tool.descriptor = {"alias": "t"}
    assert _resource_request_for(tool).cpu_cores == 1
    tool.descriptor = "not a dict"
    assert _resource_request_for(tool).memory_mb == 512
