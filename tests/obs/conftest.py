"""Shared fixtures for matofx.obs tests."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from matofx.config import LogSettings
from matofx.obs import clear_context, configure


@pytest.fixture(autouse=True)
def _reset_context() -> Iterator[None]:
    """Make sure context fields don't leak across tests."""
    clear_context()
    yield
    clear_context()


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """A StringIO wired up as the obs sink for the duration of a test.

    Yields the buffer; tests read it back with ``read_lines(buf)``.
    """
    buf = io.StringIO()
    configure(LogSettings(level="INFO", ensure_ascii=False), stream=buf)
    try:
        yield buf
    finally:
        # Reset the root logger so other tests don't see this handler.
        root = logging.getLogger()
        for h in list(root.handlers):
            if h.get_name() == "matofx.obs":
                root.removeHandler(h)


def read_lines(buf: io.StringIO) -> list[dict[str, object]]:
    """Parse the buffer's contents into a list of JSON records."""
    text = buf.getvalue()
    return [json.loads(line) for line in text.splitlines() if line.strip()]
