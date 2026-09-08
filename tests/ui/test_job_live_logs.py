"""A running job's log view must survive being updated.

The reported bug: expand ``stdout`` on a running job and the section
snaps shut again a couple of seconds later, over and over, so the log
cannot be read while it is being written — the only time anyone wants
to read it.

The cause was that the poll cleared its container and re-ran the render,
so the ``ui.expansion`` the user had just opened was destroyed and a new,
closed one built in its place. Open-state is a property of the widget,
and the widget was gone.

These tests hold the invariant directly: **the same element objects must
still be there after a poll**, and whatever the user set on them must
still be set. Asserting on identity rather than on appearance is
deliberate — a rebuilt-but-identical-looking tree is exactly the bug.
"""

from __future__ import annotations

from typing import Any

import pytest
from nicegui import Client, ui
from nicegui.page import page as page_decorator

from matofx.ui.api_client import ApiError
from matofx.ui.pages.job import _LiveLogs


@pytest.fixture
def slot() -> Any:
    """A container the render functions can build into, with no browser."""

    def _unused() -> None:
        """Never called — `page` only needs a target to decorate."""

    client = Client(page_decorator("/__live_logs_probe__")(_unused), request=None)
    with client, ui.column() as column:
        yield column


class _FakeClient:
    """Stands in for ApiClient; records what tail mode asked for."""

    def __init__(self, *responses: dict[str, Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def get_job_logs(
        self, job_id: int, *, since: dict[str, int] | None = None, tail: bool = False
    ) -> dict[str, Any]:
        self.calls.append({"job_id": job_id, "since": dict(since or {}), "tail": tail})
        return self._responses.pop(0) if self._responses else _payload()


class _FailingClient:
    async def get_job_logs(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        raise ApiError(503, "the runner is not answering")


def _payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "stdout": "",
        "stderr": "",
        "launcher": "",
        "stdout_offset": 0,
        "stderr_offset": 0,
        "launcher_offset": 0,
        "truncated": False,
    }
    base.update(over)
    return base


def _expansions(view: _LiveLogs) -> list[Any]:
    return [e for e in view._container.descendants() if isinstance(e, ui.expansion)]


def _lines(log: ui.log) -> list[str]:
    return [str(child.text) for child in log.default_slot.children]


# ---- the bug --------------------------------------------------------


async def test_polling_does_not_destroy_the_expansion(slot: Any) -> None:
    """The section the user opened is still the same element afterwards."""
    with slot:
        view = _LiveLogs()
    client = _FakeClient(
        _payload(stdout="line one\n", stdout_offset=9),
        _payload(stdout="line two\n", stdout_offset=18),
    )
    await view.poll(client, 1)
    first = _expansions(view)
    assert len(first) == 1

    await view.poll(client, 1)
    second = _expansions(view)
    assert second[0] is first[0], "the expansion was rebuilt — this is the reported bug"


async def test_an_opened_section_stays_open_across_polls(slot: Any) -> None:
    with slot:
        view = _LiveLogs()
    client = _FakeClient(
        _payload(stderr="warning\n", stderr_offset=8),
        _payload(stderr="another\n", stderr_offset=16),
    )
    await view.poll(client, 1)
    stderr_section = _expansions(view)[0]
    stderr_section.value = True  # the user expands it

    await view.poll(client, 1)
    assert stderr_section.value is True


async def test_stdout_opens_by_default(slot: Any) -> None:
    """It is the thing being watched; the others stay out of the way."""
    with slot:
        view = _LiveLogs()
    await view.poll(_FakeClient(_payload(stdout="go\n", stdout_offset=3)), 1)
    assert _expansions(view)[0].value is True


async def test_lines_accumulate_rather_than_replace(slot: Any) -> None:
    with slot:
        view = _LiveLogs()
    client = _FakeClient(
        _payload(stdout="one\n", stdout_offset=4),
        _payload(stdout="two\nthree\n", stdout_offset=14),
    )
    await view.poll(client, 1)
    await view.poll(client, 1)
    assert _lines(view._views["stdout"]) == ["one", "two", "three"]


# ---- tail-mode plumbing ---------------------------------------------


async def test_offsets_from_one_response_are_sent_on_the_next_request(slot: Any) -> None:
    """Otherwise every poll re-reads the whole growing file."""
    with slot:
        view = _LiveLogs()
    client = _FakeClient(
        _payload(stdout="abc\n", stdout_offset=4, stderr_offset=7, launcher_offset=2),
        _payload(),
    )
    await view.poll(client, 42)
    await view.poll(client, 42)
    assert client.calls[0]["since"] == {"stdout": 0, "stderr": 0, "launcher": 0}
    assert client.calls[1]["since"] == {"stdout": 4, "stderr": 7, "launcher": 2}


async def test_the_live_view_always_asks_for_tail_mode(slot: Any) -> None:
    with slot:
        view = _LiveLogs()
    client = _FakeClient(_payload())
    await view.poll(client, 1)
    assert client.calls[0]["tail"] is True


async def test_a_truncated_log_clears_the_view_instead_of_splicing(slot: Any) -> None:
    """A rotated log appended to the old one reads as one impossible run."""
    with slot:
        view = _LiveLogs()
    client = _FakeClient(
        _payload(stdout="old run\n", stdout_offset=8),
        _payload(stdout="new run\n", stdout_offset=8, truncated=True),
    )
    await view.poll(client, 1)
    await view.poll(client, 1)
    assert _lines(view._views["stdout"]) == ["new run"]


# ---- sections appear only when they have something to say ------------


async def test_an_empty_stream_gets_no_section(slot: Any) -> None:
    with slot:
        view = _LiveLogs()
    await view.poll(_FakeClient(_payload(stdout="only stdout\n", stdout_offset=12)), 1)
    assert set(view._views) == {"stdout"}


async def test_the_launcher_section_appears_when_the_launcher_speaks(slot: Any) -> None:
    with slot:
        view = _LiveLogs()
    await view.poll(_FakeClient(_payload(launcher="bad interpreter\n", launcher_offset=16)), 1)
    assert set(view._views) == {"launcher"}


# ---- failures -------------------------------------------------------


async def test_a_fetch_failure_is_reported_once_not_every_tick(slot: Any) -> None:
    """A two-second timer would otherwise write a wall of identical errors."""
    with slot:
        view = _LiveLogs()
    client = _FailingClient()
    await view.poll(client, 1)
    await view.poll(client, 1)
    await view.poll(client, 1)
    assert _lines(view._views["stdout"]) == ["(could not load logs: the runner is not answering)"]


async def test_a_blank_separator_line_survives_a_chunk_boundary(slot: Any) -> None:
    """Solvers separate blocks with blank lines; those are content.

    Stripping every trailing newline rather than the one line terminator
    quietly deleted them whenever a poll landed on the boundary.
    """
    with slot:
        view = _LiveLogs()
    client = _FakeClient(
        _payload(stdout="Time = 1\nresidual 0.5\n\n", stdout_offset=22),
        _payload(stdout="Time = 2\n", stdout_offset=31),
    )
    await view.poll(client, 1)
    await view.poll(client, 1)
    assert _lines(view._views["stdout"]) == ["Time = 1", "residual 0.5", "", "Time = 2"]
