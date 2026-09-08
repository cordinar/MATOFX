"""Phase 1: AOS + GPU is refused at descriptor parse time.

The :class:`ToolDescriptor` model validator rejects descriptors that
pair an AOS execution backend with a non-zero GPU memory request.
HPC + GPU stays valid; local + GPU stays valid; AOS without GPU
stays valid.

The runtime placeholder :class:`AosGpuScheduler` exists as the typed
import path Phase 2 will fill in. Every method raises
:class:`AosGpuNotImplementedError` for now.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from matofx.tools import (
    AosGpuNotImplementedError,
    AosGpuScheduler,
    ExecutionBackend,
    ResourceRequirements,
    ToolDescriptor,
)


def _descriptor(**overrides: object) -> ToolDescriptor:
    base: dict[str, object] = {
        "alias": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "description": "Demo tool.",
        "main_script": "static/main.py",
    }
    base.update(overrides)
    return ToolDescriptor(**base)  # type: ignore[arg-type]


# --- descriptor-time refusal -----------------------------------------


def test_aos_calculator_with_gpu_refused() -> None:
    with pytest.raises(ValidationError, match="GPU"):
        _descriptor(
            execution_backend=ExecutionBackend(kind="aos_calculator", server="aos1"),
            resources=ResourceRequirements(gpu_memory_mb=8 * 1024),
        )


def test_aos_repeat_calculator_with_gpu_refused() -> None:
    with pytest.raises(ValidationError, match="GPU"):
        _descriptor(
            execution_backend=ExecutionBackend(kind="aos_repeat_calculator", server="aos1"),
            resources=ResourceRequirements(gpu_memory_mb=4 * 1024),
        )


def test_aos_without_gpu_passes() -> None:
    """The refusal is GPU-specific; CPU-only AOS jobs are fine."""
    d = _descriptor(
        execution_backend=ExecutionBackend(kind="aos_calculator", server="aos1"),
        resources=ResourceRequirements(gpu_memory_mb=0),
    )
    assert d.execution_backend.kind == "aos_calculator"
    assert d.resources.gpu_memory_mb == 0


def test_hpc_with_gpu_passes() -> None:
    """GPU jobs are fine on HPC — the cluster scheduler places the GPU."""
    d = _descriptor(
        execution_backend=ExecutionBackend(kind="hpc_batch", queue="gpu"),
        resources=ResourceRequirements(gpu_memory_mb=24 * 1024),
    )
    assert d.execution_backend.kind == "hpc_batch"
    assert d.resources.gpu_memory_mb == 24 * 1024


def test_subprocess_with_gpu_passes() -> None:
    """Local subprocess + GPU is fine; the OS does the placement."""
    d = _descriptor(
        execution_backend=ExecutionBackend(kind="subprocess"),
        resources=ResourceRequirements(gpu_memory_mb=8 * 1024),
    )
    assert d.execution_backend.kind == "subprocess"
    assert d.resources.gpu_memory_mb == 8 * 1024


def test_error_message_points_at_hpc() -> None:
    """The refusal message tells the operator how to fix it."""
    with pytest.raises(ValidationError) as ei:
        _descriptor(
            execution_backend=ExecutionBackend(kind="aos_calculator", server="aos1"),
            resources=ResourceRequirements(gpu_memory_mb=1),
        )
    msg = str(ei.value)
    assert "hpc_batch" in msg
    assert "GPU" in msg or "gpu_memory_mb" in msg


# --- runtime placeholder --------------------------------------------


def test_scheduler_reserve_raises_not_implemented() -> None:
    sched = AosGpuScheduler()
    with pytest.raises(AosGpuNotImplementedError) as ei:
        sched.reserve(gpu_memory_mb=1024, host_id=1)
    assert "GPU" in str(ei.value)
    assert "HPC" in str(ei.value)


def test_scheduler_release_raises_not_implemented() -> None:
    sched = AosGpuScheduler()
    with pytest.raises(AosGpuNotImplementedError):
        sched.release(host_id=1, reservation_id="x")


def test_scheduler_available_memory_raises_not_implemented() -> None:
    sched = AosGpuScheduler()
    with pytest.raises(AosGpuNotImplementedError):
        sched.available_memory_mb(host_id=1)


def test_aos_gpu_error_is_not_implemented_subclass() -> None:
    """``except NotImplementedError`` catches our typed error too."""
    sched = AosGpuScheduler()
    with pytest.raises(NotImplementedError):
        sched.reserve(gpu_memory_mb=1, host_id=1)
