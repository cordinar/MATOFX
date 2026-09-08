"""A queued job must start saying "Running" without a page refresh.

The reported bug: submit a job, watch My Jobs, and the badge stays
*Queued* however long it runs. Refreshing the page fixes it, which is
the tell — the state was only ever read once, when the page was built.

``_render_inflight`` drew its status row from the job dict it was handed
at page load and never touched it again; the two-second tick only did
something on a *terminal* state. So the entire queued → running
transition was invisible, and so were the "Started:" timestamp and the
state of the Cancel button.

These tests drive the tick directly (the timer's callback is reachable;
nothing here needs a running event loop) and assert what the user sees.
"""

from __future__ import annotations

from typing import Any

import pytest
from nicegui import Client, ui
from nicegui.page import page as page_decorator

from matofx.ui.pages.job import _TERMINAL_STATES as TERMINAL
from matofx.ui.pages.job import _cancel_pending_changed, _render_inflight

TOOL_META = {"name": "Batch Mixing", "version": "1.2.0"}


@pytest.fixture
def slot() -> Any:
    """A container the render functions can build into, with no browser."""

    def _unused() -> None:
        """Never called — `page` only needs a target to decorate."""

    client = Client(page_decorator("/__inflight_probe__")(_unused), request=None)
    with client, ui.column() as column:
        yield column


def _job(state: str, **over: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": 7,
        "state": state,
        "tool_alias": "batch_mixing",
        "started_at": None,
        "completed_at": None,
        "cancel_requested_at": None,
    }
    job.update(over)
    return job


class _FakeClient:
    """Feeds a scripted sequence of job states to the tick."""

    def __init__(self, *jobs: dict[str, Any]) -> None:
        # These are what ``get_job`` returns on successive ticks; the
        # state the page was *built* from is passed to the renderer.
        self._jobs = list(jobs)
        self.log_calls = 0

    async def get_job(self, _job_id: int) -> dict[str, Any]:
        return self._jobs.pop(0) if len(self._jobs) > 1 else self._jobs[0]

    async def get_job_logs(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        self.log_calls += 1
        return {
            "stdout": "",
            "stderr": "",
            "launcher": "",
            "stdout_offset": 0,
            "stderr_offset": 0,
            "launcher_offset": 0,
            "truncated": False,
        }


def _timer(container: Any) -> Any:
    return next(e for e in container.descendants() if isinstance(e, ui.timer))


def _badges(container: Any) -> list[str]:
    return [str(e.text) for e in container.descendants() if isinstance(e, ui.badge) and e.text]


def _labels(container: Any) -> list[str]:
    return [str(e.text) for e in container.descendants() if isinstance(e, ui.label) and e.text]


# ---- the bug --------------------------------------------------------


async def test_queued_becomes_running_without_a_page_refresh(slot: Any) -> None:
    client = _FakeClient(_job("running", started_at="2026-08-31T09:00:00Z"))
    with slot:
        body = ui.column()
    with body:
        await _render_inflight(client, _job("queued"), 7, body, TOOL_META)
    assert "Queued" in _badges(body)

    await _timer(body).callback()

    assert "Running" in _badges(body)
    assert "Queued" not in _badges(body)


async def test_the_started_timestamp_appears_when_the_job_starts(slot: Any) -> None:
    """It was drawn from a dict that had no ``started_at`` yet, and never redrawn."""
    client = _FakeClient(_job("running", started_at="2026-08-31T09:00:00Z"))
    with slot:
        body = ui.column()
    with body:
        await _render_inflight(client, _job("queued"), 7, body, TOOL_META)
    assert not any(text.startswith("Started:") for text in _labels(body))

    await _timer(body).callback()

    assert any(text.startswith("Started:") for text in _labels(body))


async def test_a_cancel_from_elsewhere_replaces_the_cancel_button(slot: Any) -> None:
    """Otherwise a second tab leaves a live button for an already-cancelled job."""
    client = _FakeClient(_job("running", cancel_requested_at="2026-08-31T09:05:00Z"))
    with slot:
        body = ui.column()
    with body:
        await _render_inflight(client, _job("running"), 7, body, TOOL_META)
    assert any(isinstance(e, ui.button) and e.text == "Cancel job" for e in body.descendants())

    await _timer(body).callback()

    assert not any(isinstance(e, ui.button) and e.text == "Cancel job" for e in body.descendants())
    assert any("Cancel requested" in text for text in _labels(body))


async def test_an_unchanged_state_does_not_repaint(slot: Any) -> None:
    """Repainting every two seconds would fight the Cancel button's own clicks."""
    client = _FakeClient(_job("running"))
    with slot:
        body = ui.column()
    with body:
        await _render_inflight(client, _job("running"), 7, body, TOOL_META)
    badge = next(e for e in body.descendants() if isinstance(e, ui.badge))

    await _timer(body).callback()

    assert next(e for e in body.descendants() if isinstance(e, ui.badge)) is badge


# ---- terminal states -------------------------------------------------


def test_cancelled_counts_as_terminal() -> None:
    """The tick and My Jobs' outer poller had drifted on exactly this.

    The tick used to stop only on complete/failed, so it kept polling a
    cancelled job that was never coming back until the slower outer
    timer noticed. One set, two callers.
    """
    assert {"complete", "failed", "cancelled"} == TERMINAL


# ---- the repaint trigger --------------------------------------------


@pytest.mark.parametrize(
    ("fresh", "shown", "expected"),
    [
        ({"cancel_requested_at": "2026-08-31T09:00:00Z"}, {"cancel_requested_at": None}, True),
        ({"cancel_requested_at": None}, {"cancel_requested_at": None}, False),
        (
            {"cancel_requested_at": "2026-08-31T09:00:00Z"},
            {"cancel_requested_at": "2026-08-31T09:00:00Z"},
            False,
        ),
    ],
)
def test_cancel_pending_changed(fresh: Any, shown: Any, expected: bool) -> None:
    assert _cancel_pending_changed(fresh, shown) is expected
