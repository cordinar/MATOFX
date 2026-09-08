"""Coverage analysis must not run on the event loop (ADR-0020 finding 76).

The analysis is NumPy-heavy and **unbounded in the archive's size** — 1.85 s
for the cohort panel on a real 708-run archive, 0.84 s for a suggestion batch,
and more again for the fit-time pass, which runs the per-response GP half as
well. Run inline in an ``async def``, all of that stalls every other request
on the server, and it worsens as a customer's archive grows: the biggest
customer hurts most.

The regression these guard against is **silent** — the answer is identical,
the server simply stops serving anyone else while it is computed — so nothing
in an ordinary functional suite would ever notice.

Asserted structurally rather than by wall-clock: a timing threshold is flaky
on a loaded CI box and does not say what went wrong. "This call happens inside
an ``async def``" is the precise defect, and the AST can see it exactly.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import time
from pathlib import Path
from typing import Any

import pytest

from matofx.models import service as model_service
from matofx.studies.cohort import service as cohort_service

_ANALYSIS_CALLS = frozenset(
    {"analyse_design", "analyse_responses", "suggest_design_points", "_coverage_columns"}
)
"""Entry points heavy enough that calling one on the event loop is a defect."""


def _blocking_calls_in_async_bodies(path: Path) -> list[str]:
    """``file:line`` for every analysis call lexically inside an ``async def``.

    Lexical containment is the right test: a call sitting in a plain ``def``
    — even a closure defined inside an async method — is fine, because that
    is precisely the shape ``asyncio.to_thread`` takes.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []

    def walk(node: ast.AST, in_async: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                walk(child, True)
                continue
            if isinstance(child, ast.FunctionDef | ast.Lambda):
                # A sync function resets the context: this is the body that
                # gets handed to a thread.
                walk(child, False)
                continue
            if (
                in_async
                and isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id in _ANALYSIS_CALLS
            ):
                found.append(f"{path.name}:{child.lineno} {child.func.id}(...)")
            walk(child, in_async)

    walk(tree, False)
    return found


_INLINE = """
async def f():
    d = analyse_design(p, e)
"""

_NESTED_ASYNC = """
async def f():
    async def g():
        return suggest_design_points(p, e)
    return await g()
"""

_BY_NAME = """
async def f():
    return await asyncio.to_thread(analyse_design, p, e)
"""

_CLOSURE = """
async def f():
    def _compute():
        return analyse_design(p, e)
    return await asyncio.to_thread(_compute)
"""

_SYNC_HELPER = """
def helper(t):
    return _coverage_columns(t)
"""


@pytest.mark.parametrize(
    ("snippet", "flagged"),
    [
        (_INLINE, True),
        (_NESTED_ASYNC, True),
        (_BY_NAME, False),
        (_CLOSURE, False),
        (_SYNC_HELPER, False),
    ],
    ids=["inline", "nested-async", "by-name", "closure", "sync-helper"],
)
def test_the_detector_catches_a_regression(
    tmp_path: Path, snippet: str, flagged: bool
) -> None:
    """A structural test is worthless if it cannot fail.

    The first two cases are the regression; the other three are the legal
    shapes an offload takes, and flagging one of those would make the guard
    something a developer learns to work around rather than to satisfy.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(snippet, encoding="utf-8")
    assert bool(_blocking_calls_in_async_bodies(probe)) is flagged


@pytest.mark.parametrize(
    "module",
    [cohort_service, model_service],
    ids=["cohort-service", "model-service"],
)
def test_no_analysis_call_sits_in_an_async_body(module: Any) -> None:
    """The property itself, checked exactly.

    ``asyncio.to_thread(analyse_design, ...)`` passes the function *by name*
    rather than calling it, so it is not an ``ast.Call`` to that name and is
    correctly not flagged.
    """
    offenders = _blocking_calls_in_async_bodies(Path(inspect.getfile(module)))
    assert not offenders, "coverage analysis on the event loop: " + "; ".join(offenders)


@pytest.mark.parametrize(
    ("owner", "method"),
    [
        (cohort_service.CohortService, "coverage_preview"),
        (cohort_service.CohortService, "coverage_suggest"),
        (model_service.ModelService, "_add_version"),
    ],
    ids=["cohort-panel", "suggestions", "fit-time"],
)
def test_each_path_hands_its_work_to_a_thread(owner: type, method: str) -> None:
    """And the positive half: the offload is actually there.

    Without this, deleting the analysis entirely would satisfy the test above.
    """
    source = inspect.getsource(getattr(owner, method))
    assert "to_thread" in source, f"{owner.__name__}.{method} no longer offloads its analysis"


# --------------------------------------------------------------------------
# The probe that proves the above is worth asserting
# --------------------------------------------------------------------------


async def _worst_loop_stall(coro: Any) -> float:
    """The longest gap the event loop went unserved while ``coro`` ran.

    A heartbeat ticks every millisecond. Offloaded work keeps being serviced;
    inline work cannot let the heartbeat fire at all until it finishes, so the
    gap equals the work's duration.
    """
    gaps: list[float] = []
    stop = False

    async def _heartbeat() -> None:
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.001)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(_heartbeat())
    await asyncio.sleep(0.01)
    try:
        await coro
    finally:
        stop = True
        await beat
    return max(gaps) if gaps else 0.0


def _busy(seconds: float) -> str:
    """Busy-wait — a stand-in for the NumPy pass, which also holds the GIL."""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass
    return "done"


@pytest.mark.asyncio
async def test_inline_work_really_does_stall_the_loop() -> None:
    """The control. Without this, the two tests above could be asserting a
    property that does not matter."""

    async def _inline() -> None:
        _busy(0.30)

    stall = await _worst_loop_stall(_inline())
    assert stall > 0.25, f"the probe could not see a 0.30 s inline block (saw {stall:.3f} s)"


@pytest.mark.asyncio
async def test_the_same_work_offloaded_does_not() -> None:
    """And the converse — which is exactly the change finding 76 made."""

    async def _offloaded() -> None:
        await asyncio.to_thread(_busy, 0.30)

    stall = await _worst_loop_stall(_offloaded())
    assert stall < 0.10, f"an offloaded 0.30 s block still stalled the loop {stall:.3f} s"
